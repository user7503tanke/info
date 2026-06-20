#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import json
import random
import time
import threading
import logging
import sqlite3
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify
import requests
from functools import wraps

# ============ CONFIGURACIÓN ============
TELEGRAM_BOT_TOKEN = "7920655514:AAEH1vWk2hOkNfN_eREpe6DrPBz1mZNAQYw"
TELEGRAM_CHAT_ID = "7587515668"

PAIS = "53"
MENSAJE_DEFAULT = "Ya basta de sombra. Merecemos sol. Despierten, que el futuro no espera."
INTENTOS_POR_NUMERO = 10
INTERVALO = 1
MAX_PROXIES = 100
TIMEOUT_PROXY = 10
TIMEOUT_SMS = 15

# ============ BASE DE DATOS ============
DB_FILE = "sms_bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Tabla de números bloqueados (ban)
    c.execute('''CREATE TABLE IF NOT EXISTS numbers_ban (
        numero TEXT PRIMARY KEY,
        pais TEXT,
        fecha_ban TIMESTAMP,
        razon TEXT,
        intentos_fallidos INTEGER DEFAULT 0
    )''')
    
    # Tabla de proxies bloqueados
    c.execute('''CREATE TABLE IF NOT EXISTS proxies_ban (
        proxy TEXT PRIMARY KEY,
        fecha_ban TIMESTAMP,
        razon TEXT,
        fallos_consecutivos INTEGER DEFAULT 0
    )''')
    
    # Tabla de proxies activos (cache)
    c.execute('''CREATE TABLE IF NOT EXISTS proxies_cache (
        proxy TEXT PRIMARY KEY,
        ultimo_uso TIMESTAMP,
        veces_usado INTEGER DEFAULT 0,
        fallos INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1
    )''')
    
    # Tabla de estadísticas
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY,
        fecha TIMESTAMP,
        enviados INTEGER DEFAULT 0,
        fallidos INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        blacklist INTEGER DEFAULT 0
    )''')
    
    # Tabla de configuración
    c.execute('''CREATE TABLE IF NOT EXISTS config (
        clave TEXT PRIMARY KEY,
        valor TEXT
    )''')
    
    # Tabla de logs
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha TIMESTAMP,
        nivel TEXT,
        mensaje TEXT
    )''')
    
    # Tabla de tareas activas
    c.execute('''CREATE TABLE IF NOT EXISTS tareas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tarea_id TEXT UNIQUE,
        fecha_inicio TIMESTAMP,
        total_numeros INTEGER,
        enviados INTEGER DEFAULT 0,
        fallidos INTEGER DEFAULT 0,
        estado TEXT DEFAULT 'activa',
        datos TEXT
    )''')
    
    conn.commit()
    conn.close()

def db_execute(query, params=()):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(query, params)
    conn.commit()
    result = c.fetchall()
    conn.close()
    return result

def db_execute_many(query, params_list):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.executemany(query, params_list)
    conn.commit()
    conn.close()

# ============ LOGGING ============
class DatabaseLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            nivel = record.levelname.lower()
            db_execute(
                "INSERT INTO logs (fecha, nivel, mensaje) VALUES (?, ?, ?)",
                (datetime.now().isoformat(), nivel, msg[:500])
            )
        except:
            pass

class TelegramLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg[:4096], "parse_mode": "HTML"}
            threading.Thread(target=lambda: requests.post(url, data=data, timeout=5)).start()
        except:
            pass

def setup_logging():
    logger = logging.getLogger('SMSBot')
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
    # Handler consola
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    
    # Handler base de datos
    db_handler = DatabaseLogHandler()
    db_handler.setFormatter(formatter)
    logger.addHandler(db_handler)
    
    # Handler Telegram
    tg_handler = TelegramLogHandler()
    tg_handler.setFormatter(formatter)
    logger.addHandler(tg_handler)
    
    return logger

logger = setup_logging()

# ============ FUNCIONES DE PROXY ============
def get_proxies_from_sources(limit=100):
    """Obtiene proxies de fuentes externas"""
    proxies = []
    sources = [
        "https://api.proxyscrape.com/?request=getproxies&proxytype=http&timeout=5000&country=all&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=http",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    ]
    
    for url in sources:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                for line in r.text.splitlines():
                    line = line.strip()
                    if line and ':' in line and not line.startswith('#'):
                        proxy = line.replace('http://', '').replace('https://', '')
                        if proxy.count(':') == 1:
                            proxies.append(f"http://{proxy}")
                if len(proxies) >= limit:
                    break
        except:
            continue
    
    return list(set(proxies))[:limit]

def get_cached_proxies(limit=100):
    """Obtiene proxies de la caché que no estén banneados"""
    result = db_execute(
        """SELECT proxy FROM proxies_cache 
           WHERE activo = 1 
           AND proxy NOT IN (SELECT proxy FROM proxies_ban)
           ORDER BY veces_usado ASC, fallos ASC 
           LIMIT ?""",
        (limit,)
    )
    return [r[0] for r in result]

def refresh_proxy_cache():
    """Actualiza la caché de proxies"""
    logger.info("🔄 Actualizando caché de proxies...")
    
    # Obtener proxies de fuentes
    new_proxies = get_proxies_from_sources(MAX_PROXIES)
    
    if not new_proxies:
        logger.warning("⚠️ No se obtuvieron proxies de fuentes externas")
        return 0
    
    # Obtener proxies existentes
    existing = db_execute("SELECT proxy FROM proxies_cache")
    existing_set = {r[0] for r in existing}
    
    # Insertar nuevos proxies
    to_insert = []
    for proxy in new_proxies:
        if proxy not in existing_set:
            to_insert.append((proxy, datetime.now().isoformat(), 0, 0, 1))
    
    if to_insert:
        db_execute_many(
            "INSERT OR IGNORE INTO proxies_cache (proxy, ultimo_uso, veces_usado, fallos, activo) VALUES (?, ?, ?, ?, ?)",
            to_insert
        )
    
    # Marcar proxies antiguos como inactivos si hay muchos
    total = db_execute("SELECT COUNT(*) FROM proxies_cache WHERE activo = 1")[0][0]
    if total > MAX_PROXIES * 2:
        db_execute(
            """UPDATE proxies_cache SET activo = 0 
               WHERE proxy IN (
                   SELECT proxy FROM proxies_cache 
                   WHERE activo = 1 
                   ORDER BY ultimo_uso ASC 
                   LIMIT ?
               )""",
            (total - MAX_PROXIES,)
        )
    
    logger.info(f"✅ Caché actualizada: {len(to_insert)} nuevos proxies, {total} activos")
    return len(to_insert)

def test_proxy(proxy):
    """Prueba si un proxy funciona"""
    try:
        r = requests.get('https://www.google.com', proxies={"http": proxy, "https": proxy}, timeout=3)
        if r.status_code == 200:
            return True
    except:
        pass
    return False

def get_working_proxies(limit=20):
    """Obtiene proxies funcionales"""
    # Primero intentar con caché
    cached = get_cached_proxies(limit * 2)
    
    if cached:
        # Probar los proxies en caché
        working = []
        for proxy in cached[:limit]:
            if test_proxy(proxy):
                working.append(proxy)
                db_execute(
                    "UPDATE proxies_cache SET ultimo_uso = ?, veces_usado = veces_usado + 1 WHERE proxy = ?",
                    (datetime.now().isoformat(), proxy)
                )
            else:
                # Marcar como fallido
                db_execute(
                    "UPDATE proxies_cache SET fallos = fallos + 1 WHERE proxy = ?",
                    (proxy,)
                )
                if db_execute("SELECT fallos FROM proxies_cache WHERE proxy = ?", (proxy,))[0][0] >= 5:
                    db_execute("UPDATE proxies_cache SET activo = 0 WHERE proxy = ?", (proxy,))
        
        if working:
            return working
    
    # Si no hay proxies funcionales, refrescar caché
    refresh_proxy_cache()
    cached = get_cached_proxies(limit)
    
    working = []
    for proxy in cached:
        if test_proxy(proxy):
            working.append(proxy)
            db_execute(
                "UPDATE proxies_cache SET ultimo_uso = ?, veces_usado = veces_usado + 1 WHERE proxy = ?",
                (datetime.now().isoformat(), proxy)
            )
        if len(working) >= limit:
            break
    
    return working

def ban_proxy(proxy, razon="fallo"):
    """Bannea un proxy"""
    db_execute(
        "INSERT OR REPLACE INTO proxies_ban (proxy, fecha_ban, razon) VALUES (?, ?, ?)",
        (proxy, datetime.now().isoformat(), razon)
    )
    db_execute("UPDATE proxies_cache SET activo = 0 WHERE proxy = ?", (proxy,))
    logger.info(f"🚫 Proxy baneado: {proxy} - {razon}")

def is_proxy_banned(proxy):
    """Verifica si un proxy está baneado"""
    result = db_execute("SELECT proxy FROM proxies_ban WHERE proxy = ?", (proxy,))
    return len(result) > 0

# ============ FUNCIONES DE NÚMEROS ============
def ban_number(numero, pais, razon="limite_alcanzado"):
    """Bannea un número"""
    db_execute(
        "INSERT OR REPLACE INTO numbers_ban (numero, pais, fecha_ban, razon) VALUES (?, ?, ?, ?)",
        (numero, pais, datetime.now().isoformat(), razon)
    )
    logger.info(f"🚫 Número baneado: +{pais}{numero} - {razon}")

def is_number_banned(numero, pais):
    """Verifica si un número está baneado"""
    result = db_execute(
        "SELECT numero FROM numbers_ban WHERE numero = ? AND pais = ?",
        (numero, pais)
    )
    return len(result) > 0

def increment_number_failures(numero, pais):
    """Incrementa el contador de fallos de un número"""
    db_execute(
        """INSERT INTO numbers_ban (numero, pais, intentos_fallidos, fecha_ban) 
           VALUES (?, ?, 1, ?) 
           ON CONFLICT(numero) DO UPDATE SET 
           intentos_fallidos = intentos_fallidos + 1""",
        (numero, pais, datetime.now().isoformat())
    )
    
    # Verificar si supera el límite
    result = db_execute(
        "SELECT intentos_fallidos FROM numbers_ban WHERE numero = ? AND pais = ?",
        (numero, pais)
    )
    if result and result[0][0] >= INTENTOS_POR_NUMERO:
        ban_number(numero, pais, "demasiados_fallos")
        return True
    return False

# ============ FUNCIONES DE SMS ============
def random_user_agent():
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/119.0.6045.160 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1_1 like Mac OS X) AppleWebKit/605.1.15 Version/17.1 Mobile/15E148 Safari/604.1",
    ]
    return random.choice(uas)

def send_sms(phone, message, api_key, proxy):
    """Envía un SMS usando Textbelt"""
    url = "https://textbelt.com/text"
    data = {"phone": phone, "message": message, "key": api_key}
    headers = {"User-Agent": random_user_agent()}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    try:
        r = requests.post(url, data=data, headers=headers, proxies=proxies, timeout=TIMEOUT_SMS)
        if r.status_code == 200:
            result = r.json()
            if result.get('success'):
                return True, result.get('textId', 'OK'), None
            else:
                return False, None, result.get('error', 'Error desconocido')
        else:
            return False, None, f"HTTP {r.status_code}"
    except requests.exceptions.Timeout:
        return False, None, "TIMEOUT"
    except requests.exceptions.ConnectionError:
        return False, None, "CONNECTION_ERROR"
    except Exception as e:
        return False, None, str(e)[:50]

def process_number(numero, config, working_proxies):
    """Procesa un número individual"""
    pais = config.get('pais', PAIS)
    mensaje = config.get('mensaje', MENSAJE_DEFAULT)
    max_intentos = config.get('intentos', INTENTOS_POR_NUMERO)
    
    # Verificar si el número está baneado
    if is_number_banned(numero, pais):
        logger.debug(f"⏭️ Número baneado: +{pais}{numero}")
        return False, "banned"
    
    phone = '+' + pais + numero
    
    intentos = 0
    while intentos < max_intentos:
        # Obtener proxy
        if not working_proxies:
            logger.warning("⚠️ Sin proxies, recargando...")
            new_proxies = get_working_proxies(10)
            if new_proxies:
                working_proxies.extend(new_proxies)
            else:
                time.sleep(3)
                continue
        
        proxy = random.choice(working_proxies)
        
        # Verificar si el proxy está baneado
        if is_proxy_banned(proxy):
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            continue
        
        # Usar API key (solo textbelt por ahora)
        api_key = "textbelt"
        
        intentos += 1
        logger.debug(f"📤 Enviando a +{pais}{numero} (intento {intentos}/{max_intentos}) con {proxy}")
        
        success, text_id, error = send_sms(phone, mensaje, api_key, proxy)
        
        if success:
            # Marcar proxy como usado
            db_execute(
                "UPDATE proxies_cache SET ultimo_uso = ?, veces_usado = veces_usado + 1 WHERE proxy = ?",
                (datetime.now().isoformat(), proxy)
            )
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            return True, text_id
        
        # Manejar errores
        if error in ["TIMEOUT", "CONNECTION_ERROR"]:
            ban_proxy(proxy, error)
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            continue
        
        if error and any(p in error.lower() for p in ["only one", "limit", "quota"]):
            # Límite de API alcanzado
            if increment_number_failures(numero, pais):
                return False, "banned"
            continue
        
        # Otros errores
        if error and "invalid" in error.lower():
            # Número inválido
            ban_number(numero, pais, "numero_invalido")
            return False, "invalid"
        
        # Error genérico
        if error and "blacklist" in error.lower():
            ban_number(numero, pais, "blacklist")
            return False, "banned"
        
        # Incrementar fallos
        if increment_number_failures(numero, pais):
            return False, "banned"
    
    return False, "max_intentos"

def ejecutar_envio(numeros, config):
    """Ejecuta el envío masivo"""
    try:
        total = len(numeros)
        logger.info(f"🚀 INICIANDO ENVÍO: {total} números")
        
        # Obtener proxies funcionales
        working_proxies = get_working_proxies(20)
        if not working_proxies:
            logger.error("❌ No hay proxies funcionales disponibles")
            send_telegram_message("❌ <b>ERROR</b>\nNo hay proxies funcionales disponibles")
            return {"total": total, "enviados": 0, "fallidos": total}
        
        logger.info(f"✅ {len(working_proxies)} proxies funcionales")
        
        stats = {"total": total, "enviados": 0, "fallidos": 0, "banned": 0, "invalid": 0}
        
        for i, numero in enumerate(numeros, 1):
            logger.info(f"▶ [{i}/{total}] Procesando +{config['pais']}{numero}")
            
            success, result = process_number(numero, config, working_proxies)
            
            if success:
                stats['enviados'] += 1
                logger.info(f"✅ Enviado: +{config['pais']}{numero} (ID: {result})")
            elif result == "banned":
                stats['banned'] += 1
                logger.info(f"⏭️ Baneado: +{config['pais']}{numero}")
            elif result == "invalid":
                stats['invalid'] += 1
                logger.info(f"⚠️ Inválido: +{config['pais']}{numero}")
            else:
                stats['fallidos'] += 1
                logger.info(f"❌ Fallido: +{config['pais']}{numero}")
            
            # Mostrar progreso
            if i % 5 == 0:
                logger.info(f"📊 Progreso: {i}/{total} | ✅ {stats['enviados']} | ❌ {stats['fallidos']} | ⏭️ {stats['banned']}")
                send_telegram_message(
                    f"📊 <b>PROGRESO</b>\n"
                    f"📱 {i}/{total}\n"
                    f"✅ {stats['enviados']} enviados\n"
                    f"❌ {stats['fallidos']} fallidos\n"
                    f"⏭️ {stats['banned']} baneados"
                )
            
            # Recargar proxies si es necesario
            if len(working_proxies) < 5:
                logger.info("🔄 Recargando proxies...")
                new_proxies = get_working_proxies(10)
                if new_proxies:
                    working_proxies.extend(new_proxies)
                    logger.info(f"✅ {len(new_proxies)} proxies añadidos")
            
            # Esperar entre envíos
            if i < total:
                time.sleep(config.get('intervalo', INTERVALO) * 0.5 + random.uniform(0, 0.5))
        
        # Guardar estadísticas
        db_execute(
            "INSERT INTO stats (fecha, enviados, fallidos, total, blacklist) VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), stats['enviados'], stats['fallidos'], stats['total'], stats['banned'])
        )
        
        resumen = (
            f"📊 <b>RESUMEN FINAL</b>\n"
            f"📱 Total: {total}\n"
            f"✅ Enviados: {stats['enviados']}\n"
            f"❌ Fallidos: {stats['fallidos']}\n"
            f"⏭️ Baneados: {stats['banned']}\n"
            f"⚠️ Inválidos: {stats['invalid']}\n"
            f"🔌 Proxies disponibles: {len(working_proxies)}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        
        logger.info(resumen)
        send_telegram_message(resumen)
        
        return stats
        
    except Exception as e:
        logger.error(f"❌ Error en envío: {e}")
        send_telegram_message(f"❌ <b>ERROR</b>\n{str(e)}")
        raise

# ============ FUNCIONES TELEGRAM ============
def send_telegram_message(message):
    """Envía un mensaje a Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message[:4096], "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        logger.error(f"Error enviando mensaje a Telegram: {e}")

def process_telegram_message(message):
    """Procesa comandos de Telegram"""
    if not message:
        return
    
    logger.info(f"📩 Comando Telegram: {message[:100]}")
    
    if message.startswith('/start'):
        send_telegram_message(
            "🤖 <b>Bot SMS Pro</b>\n\n"
            "Comandos disponibles:\n"
            "/enviar <números> - Enviar SMS\n"
            "/estado - Ver estado del sistema\n"
            "/proxies - Ver proxies disponibles\n"
            "/stats - Ver estadísticas\n"
            "/refresh - Actualizar proxies\n"
            "/unban <número> - Desbanear número\n"
            "/help - Este mensaje\n\n"
            "📱 <b>Formato para enviar:</b>\n"
            "/enviar 59642359,55721087\n"
            "/enviar 59642359-59642400 (rango)"
        )
        return
    
    if message.startswith('/estado'):
        stats = db_execute("SELECT total, enviados, fallidos FROM stats ORDER BY id DESC LIMIT 1")
        proxies = db_execute("SELECT COUNT(*) FROM proxies_cache WHERE activo = 1")
        banned = db_execute("SELECT COUNT(*) FROM numbers_ban")
        
        msg = (
            f"📊 <b>ESTADO DEL SISTEMA</b>\n"
            f"🔄 Proxies activos: {proxies[0][0] if proxies else 0}\n"
            f"🚫 Números baneados: {banned[0][0] if banned else 0}\n"
            f"📱 Último envío: {stats[0][1] if stats else 0} enviados / {stats[0][2] if stats else 0} fallidos\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        send_telegram_message(msg)
        return
    
    if message.startswith('/proxies'):
        proxies = db_execute(
            "SELECT proxy, veces_usado, fallos FROM proxies_cache WHERE activo = 1 ORDER BY veces_usado DESC LIMIT 10"
        )
        if proxies:
            msg = "🌐 <b>PROXIES ACTIVOS</b>\n\n"
            for p, usado, fallos in proxies:
                msg += f"• {p}\n  Usado: {usado} | Fallos: {fallos}\n"
            send_telegram_message(msg)
        else:
            send_telegram_message("⚠️ No hay proxies activos")
        return
    
    if message.startswith('/stats'):
        stats = db_execute("SELECT fecha, total, enviados, fallidos, blacklist FROM stats ORDER BY id DESC LIMIT 5")
        if stats:
            msg = "📊 <b>ÚLTIMOS ENVÍOS</b>\n\n"
            for fecha, total, enviados, fallidos, blacklist in stats:
                msg += f"📅 {fecha[:16]}\n  Total: {total} | ✅ {enviados} | ❌ {fallidos} | ⏭️ {blacklist}\n\n"
            send_telegram_message(msg)
        else:
            send_telegram_message("📊 No hay estadísticas disponibles")
        return
    
    if message.startswith('/refresh'):
        send_telegram_message("🔄 Actualizando proxies...")
        count = refresh_proxy_cache()
        send_telegram_message(f"✅ Proxies actualizados: {count} nuevos proxies añadidos")
        return
    
    if message.startswith('/unban'):
        parts = message.split()
        if len(parts) > 1:
            numero = parts[1].strip()
            db_execute("DELETE FROM numbers_ban WHERE numero = ?", (numero,))
            send_telegram_message(f"✅ Número {numero} desbaneado")
        else:
            send_telegram_message("⚠️ Uso: /unban <número>")
        return
    
    if message.startswith('/enviar'):
        parts = message.split(maxsplit=1)
        if len(parts) < 2:
            send_telegram_message("⚠️ Uso: /enviar <números> (separados por coma o rango)")
            return
        
        numeros_str = parts[1].strip()
        numeros = []
        
        # Procesar números
        if ',' in numeros_str:
            # Lista separada por comas
            for n in numeros_str.split(','):
                n = n.strip()
                if n:
                    numeros.append(n)
        elif '-' in numeros_str:
            # Rango
            try:
                inicio, fin = numeros_str.split('-')
                inicio = int(inicio.strip())
                fin = int(fin.strip())
                if fin > inicio and fin - inicio <= 10000:
                    for i in range(inicio, fin + 1):
                        numeros.append(str(i).zfill(8))
                else:
                    send_telegram_message("⚠️ Rango demasiado grande (máx 10000 números)")
                    return
            except:
                send_telegram_message("⚠️ Formato de rango inválido. Ej: /enviar 59540000-59540100")
                return
        else:
            numeros = [numeros_str]
        
        if not numeros:
            send_telegram_message("⚠️ No se encontraron números válidos")
            return
        
        if len(numeros) > 500:
            send_telegram_message("⚠️ Demasiados números (máx 500)")
            return
        
        # Configurar envío
        config = {
            "pais": PAIS,
            "mensaje": MENSAJE_DEFAULT,
            "intentos": INTENTOS_POR_NUMERO,
            "intervalo": INTERVALO
        }
        
        # Iniciar envío en hilo separado
        send_telegram_message(f"🚀 <b>INICIANDO ENVÍO</b>\n📱 {len(numeros)} números\n⏰ {datetime.now().strftime('%H:%M:%S')}")
        
        def run_send():
            try:
                stats = ejecutar_envio(numeros, config)
                send_telegram_message(
                    f"✅ <b>ENVÍO COMPLETADO</b>\n"
                    f"📱 {stats['total']} números\n"
                    f"✅ {stats['enviados']} enviados\n"
                    f"❌ {stats['fallidos']} fallidos\n"
                    f"⏭️ {stats['banned']} baneados"
                )
            except Exception as e:
                send_telegram_message(f"❌ <b>ERROR</b>\n{str(e)}")
        
        thread = threading.Thread(target=run_send)
        thread.daemon = True
        thread.start()
        return
    
    if message.startswith('/help'):
        send_telegram_message(
            "📚 <b>AYUDA</b>\n\n"
            "/start - Ver comandos\n"
            "/enviar <números> - Enviar SMS\n"
            "/estado - Estado del sistema\n"
            "/proxies - Ver proxies\n"
            "/stats - Estadísticas\n"
            "/refresh - Actualizar proxies\n"
            "/unban <número> - Desbanear número\n"
            "/help - Esta ayuda"
        )
        return

def telegram_polling():
    """Polling de mensajes de Telegram"""
    last_update_id = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            
            r = requests.get(url, params=params, timeout=35)
            if r.status_code == 200:
                data = r.json()
                if data.get('ok'):
                    for update in data.get('result', []):
                        last_update_id = update['update_id']
                        if 'message' in update:
                            msg = update['message']
                            if 'text' in msg:
                                process_telegram_message(msg['text'])
        except Exception as e:
            logger.error(f"Error en polling de Telegram: {e}")
            time.sleep(5)

# ============ FLASK APP ============
app = Flask(__name__)

@app.route('/')
def index():
    """Endpoint simple"""
    return "OK", 200

@app.route('/health')
def health():
    """Health check"""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "proxies": db_execute("SELECT COUNT(*) FROM proxies_cache WHERE activo = 1")[0][0],
        "banned": db_execute("SELECT COUNT(*) FROM numbers_ban")[0][0]
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook para recibir mensajes de Telegram"""
    try:
        data = request.get_json()
        if data and 'message' in data:
            msg = data['message']
            if 'text' in msg:
                process_telegram_message(msg['text'])
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        return jsonify({"ok": False}), 500

# ============ MAIN ============
def main():
    """Punto de entrada principal"""
    try:
        # Inicializar base de datos
        init_db()
        logger.info("🗄️ Base de datos inicializada")
        
        # Inicializar caché de proxies
        refresh_proxy_cache()
        
        # Iniciar hilo de polling de Telegram
        tg_thread = threading.Thread(target=telegram_polling)
        tg_thread.daemon = True
        tg_thread.start()
        logger.info("🤖 Polling de Telegram iniciado")
        
        # Enviar mensaje de inicio
        send_telegram_message(
            "🤖 <b>BOT SMS PRO</b>\n\n"
            "✅ Sistema iniciado\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "Usa /help para ver comandos"
        )
        
        # Iniciar servidor Flask
        logger.info("🌐 Servidor iniciado en http://0.0.0.0:5000")
        app.run(host='0.0.0.0', port=5000, debug=False)
        
    except Exception as e:
        logger.error(f"❌ Error en main: {e}")
        send_telegram_message(f"❌ <b>ERROR CRÍTICO</b>\n{str(e)}")
        sys.exit(1)

