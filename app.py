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
BAN_DURATION_HOURS = 24  # Duración de blacklist en horas

# ============ BASE DE DATOS ============
DB_FILE = "sms_bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Tabla de números baneados (con expiración)
    c.execute('''CREATE TABLE IF NOT EXISTS numbers_ban (
        numero TEXT,
        pais TEXT,
        fecha_ban TIMESTAMP,
        expira TIMESTAMP,
        razon TEXT,
        PRIMARY KEY (numero, pais)
    )''')
    
    # Tabla de proxies baneados (con expiración)
    c.execute('''CREATE TABLE IF NOT EXISTS proxies_ban (
        proxy TEXT PRIMARY KEY,
        fecha_ban TIMESTAMP,
        expira TIMESTAMP,
        razon TEXT,
        fallos_consecutivos INTEGER DEFAULT 0
    )''')
    
    # Tabla de proxies activos (cache)
    c.execute('''CREATE TABLE IF NOT EXISTS proxies_cache (
        proxy TEXT PRIMARY KEY,
        ultimo_uso TIMESTAMP,
        veces_usado INTEGER DEFAULT 0,
        fallos INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1,
        ultimo_exito TIMESTAMP
    )''')
    
    # Tabla de estadísticas
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    
    db_handler = DatabaseLogHandler()
    db_handler.setFormatter(formatter)
    logger.addHandler(db_handler)
    
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
    """Obtiene proxies de la caché que no estén baneados"""
    ahora = datetime.now().isoformat()
    result = db_execute(
        """SELECT proxy FROM proxies_cache 
           WHERE activo = 1 
           AND proxy NOT IN (
               SELECT proxy FROM proxies_ban 
               WHERE expira > ?
           )
           ORDER BY veces_usado ASC, fallos ASC 
           LIMIT ?""",
        (ahora, limit)
    )
    return [r[0] for r in result]

def refresh_proxy_cache():
    """Actualiza la caché de proxies"""
    logger.info("🔄 Actualizando caché de proxies...")
    
    new_proxies = get_proxies_from_sources(MAX_PROXIES)
    
    if not new_proxies:
        logger.warning("⚠️ No se obtuvieron proxies de fuentes externas")
        return 0
    
    existing = db_execute("SELECT proxy FROM proxies_cache")
    existing_set = {r[0] for r in existing}
    
    to_insert = []
    for proxy in new_proxies:
        if proxy not in existing_set:
            to_insert.append((proxy, datetime.now().isoformat(), 0, 0, 1, None))
    
    if to_insert:
        db_execute_many(
            """INSERT OR IGNORE INTO proxies_cache 
               (proxy, ultimo_uso, veces_usado, fallos, activo, ultimo_exito) 
               VALUES (?, ?, ?, ?, ?, ?)""",
            to_insert
        )
    
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
    cached = get_cached_proxies(limit * 2)
    
    if cached:
        working = []
        for proxy in cached[:limit]:
            if test_proxy(proxy):
                working.append(proxy)
                db_execute(
                    "UPDATE proxies_cache SET ultimo_uso = ?, veces_usado = veces_usado + 1 WHERE proxy = ?",
                    (datetime.now().isoformat(), proxy)
                )
            else:
                db_execute(
                    "UPDATE proxies_cache SET fallos = fallos + 1 WHERE proxy = ?",
                    (proxy,)
                )
                if db_execute("SELECT fallos FROM proxies_cache WHERE proxy = ?", (proxy,))[0][0] >= 5:
                    db_execute("UPDATE proxies_cache SET activo = 0 WHERE proxy = ?", (proxy,))
        
        if working:
            return working
    
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

def ban_proxy(proxy, razon="fallo", hours=BAN_DURATION_HOURS):
    """Bannea un proxy por N horas"""
    ahora = datetime.now()
    expira = ahora + timedelta(hours=hours)
    
    db_execute(
        """INSERT OR REPLACE INTO proxies_ban (proxy, fecha_ban, expira, razon) 
           VALUES (?, ?, ?, ?)""",
        (proxy, ahora.isoformat(), expira.isoformat(), razon)
    )
    db_execute("UPDATE proxies_cache SET activo = 0 WHERE proxy = ?", (proxy,))
    
    logger.info(f"🚫 Proxy baneado 24h: {proxy} - {razon} (expira: {expira.strftime('%H:%M:%S')})")

def ban_number(numero, pais, razon="exitoso", hours=BAN_DURATION_HOURS):
    """Bannea un número por N horas"""
    ahora = datetime.now()
    expira = ahora + timedelta(hours=hours)
    
    db_execute(
        """INSERT OR REPLACE INTO numbers_ban (numero, pais, fecha_ban, expira, razon) 
           VALUES (?, ?, ?, ?, ?)""",
        (numero, pais, ahora.isoformat(), expira.isoformat(), razon)
    )
    
    logger.info(f"🚫 Número baneado 24h: +{pais}{numero} - {razon} (expira: {expira.strftime('%H:%M:%S')})")

def is_proxy_banned(proxy):
    """Verifica si un proxy está baneado (no expirado)"""
    ahora = datetime.now().isoformat()
    result = db_execute(
        "SELECT proxy FROM proxies_ban WHERE proxy = ? AND expira > ?",
        (proxy, ahora)
    )
    return len(result) > 0

def is_number_banned(numero, pais):
    """Verifica si un número está baneado (no expirado)"""
    ahora = datetime.now().isoformat()
    result = db_execute(
        "SELECT numero FROM numbers_ban WHERE numero = ? AND pais = ? AND expira > ?",
        (numero, pais, ahora)
    )
    return len(result) > 0

def clean_expired_bans():
    """Limpia bans expirados (opcional, ya que las queries verifican expira)"""
    ahora = datetime.now().isoformat()
    db_execute("DELETE FROM proxies_ban WHERE expira <= ?", (ahora,))
    db_execute("DELETE FROM numbers_ban WHERE expira <= ?", (ahora,))

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
                error = result.get('error', 'Error desconocido')
                # Detectar error de límite (solo 1 por día)
                if error and any(p in error.lower() for p in ["only one", "limit", "quota", "1 per day"]):
                    return False, None, "LIMIT_DAILY"
                return False, None, error
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
        
        api_key = "textbelt"
        intentos += 1
        logger.debug(f"📤 Enviando a +{pais}{numero} (intento {intentos}/{max_intentos}) con {proxy}")
        
        success, text_id, error = send_sms(phone, mensaje, api_key, proxy)
        
        if success:
            # ✅ ÉXITO: BANEAR PROXY Y NÚMERO POR 24 HORAS
            logger.info(f"✅ ÉXITO: +{pais}{numero} - Baneando proxy y número por 24h")
            
            # Banear proxy (24h)
            ban_proxy(proxy, "exitoso_enviado", BAN_DURATION_HOURS)
            
            # Banear número (24h)
            ban_number(numero, pais, "exitoso_recibido", BAN_DURATION_HOURS)
            
            # Actualizar estadísticas del proxy
            db_execute(
                """UPDATE proxies_cache 
                   SET ultimo_uso = ?, veces_usado = veces_usado + 1, ultimo_exito = ? 
                   WHERE proxy = ?""",
                (datetime.now().isoformat(), datetime.now().isoformat(), proxy)
            )
            
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            
            return True, text_id
        
        # Manejar errores específicos
        if error == "LIMIT_DAILY":
            # 🚫 LÍMITE DIARIO: Solo banear proxy, NO el número
            logger.warning(f"⚠️ Límite diario alcanzado con proxy {proxy} - Baneando solo proxy")
            ban_proxy(proxy, "limite_diario_alcanzado", BAN_DURATION_HOURS)
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            # No baneamos el número, podemos intentar con otro proxy
            continue
        
        if error in ["TIMEOUT", "CONNECTION_ERROR"]:
            # Error de conexión: banear proxy
            ban_proxy(proxy, error, BAN_DURATION_HOURS)
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            continue
        
        if error and "invalid" in error.lower():
            # Número inválido: banear número permanentemente
            ban_number(numero, pais, "numero_invalido", 720)  # 30 días
            return False, "invalid"
        
        if error and "blacklist" in error.lower():
            # Número en blacklist de Textbelt
            ban_number(numero, pais, "blacklist_textbelt", 720)  # 30 días
            return False, "banned"
        
        # Otros errores: banear proxy
        ban_proxy(proxy, f"error_{error[:30]}", BAN_DURATION_HOURS)
        if proxy in working_proxies:
            working_proxies.remove(proxy)
    
    return False, "max_intentos"

def ejecutar_envio(numeros, config):
    """Ejecuta el envío masivo"""
    try:
        # Limpiar bans expirados
        clean_expired_bans()
        
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
                logger.info(f"✅ Enviado: +{config['pais']}{numero} (ID: {result}) - Proxy y número baneados 24h")
            elif result == "banned":
                stats['banned'] += 1
                logger.info(f"⏭️ Baneado: +{config['pais']}{numero}")
            elif result == "invalid":
                stats['invalid'] += 1
                logger.info(f"⚠️ Inválido: +{config['pais']}{numero}")
            else:
                stats['fallidos'] += 1
                logger.info(f"❌ Fallido: +{config['pais']}{numero}")
            
            # Mostrar progreso cada 5 números
            if i % 5 == 0:
                logger.info(f"📊 Progreso: {i}/{total} | ✅ {stats['enviados']} | ❌ {stats['fallidos']} | ⏭️ {stats['banned']}")
                send_telegram_message(
                    f"📊 <b>PROGRESO</b>\n"
                    f"📱 {i}/{total}\n"
                    f"✅ {stats['enviados']} enviados (baneados 24h)\n"
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
            f"✅ Enviados: {stats['enviados']} (baneados 24h)\n"
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
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message[:4096], "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        logger.error(f"Error enviando mensaje a Telegram: {e}")

def process_telegram_message(message):
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
        # Contar bans activos (no expirados)
        ahora = datetime.now().isoformat()
        proxies_banned = db_execute("SELECT COUNT(*) FROM proxies_ban WHERE expira > ?", (ahora,))[0][0]
        numbers_banned = db_execute("SELECT COUNT(*) FROM numbers_ban WHERE expira > ?", (ahora,))[0][0]
        proxies_activos = db_execute("SELECT COUNT(*) FROM proxies_cache WHERE activo = 1")[0][0]
        stats = db_execute("SELECT total, enviados, fallidos FROM stats ORDER BY id DESC LIMIT 1")
        
        msg = (
            f"📊 <b>ESTADO DEL SISTEMA</b>\n"
            f"🔄 Proxies activos: {proxies_activos}\n"
            f"🚫 Proxies baneados: {proxies_banned}\n"
            f"🚫 Números baneados: {numbers_banned}\n"
            f"📱 Último envío: {stats[0][1] if stats else 0} enviados / {stats[0][2] if stats else 0} fallidos\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        send_telegram_message(msg)
        return
    
    if message.startswith('/proxies'):
        ahora = datetime.now().isoformat()
        proxies = db_execute(
            """SELECT p.proxy, p.veces_usado, p.fallos, 
               CASE WHEN b.proxy IS NOT NULL THEN 'baneado' ELSE 'activo' END as estado
               FROM proxies_cache p
               LEFT JOIN proxies_ban b ON p.proxy = b.proxy AND b.expira > ?
               WHERE p.activo = 1
               ORDER BY p.veces_usado DESC 
               LIMIT 10""",
            (ahora,)
        )
        if proxies:
            msg = "🌐 <b>PROXIES</b>\n\n"
            for p, usado, fallos, estado in proxies:
                emoji = "🔴" if estado == "baneado" else "🟢"
                msg += f"{emoji} {p}\n  Usado: {usado} | Fallos: {fallos}\n\n"
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
        
        if ',' in numeros_str:
            for n in numeros_str.split(','):
                n = n.strip()
                if n:
                    numeros.append(n)
        elif '-' in numeros_str:
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
        
        config = {
            "pais": PAIS,
            "mensaje": MENSAJE_DEFAULT,
            "intentos": INTENTOS_POR_NUMERO,
            "intervalo": INTERVALO
        }
        
        send_telegram_message(f"🚀 <b>INICIANDO ENVÍO</b>\n📱 {len(numeros)} números\n⏰ {datetime.now().strftime('%H:%M:%S')}")
        
        def run_send():
            try:
                stats = ejecutar_envio(numeros, config)
                send_telegram_message(
                    f"✅ <b>ENVÍO COMPLETADO</b>\n"
                    f"📱 {stats['total']} números\n"
                    f"✅ {stats['enviados']} enviados (baneados 24h)\n"
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
            "/help - Esta ayuda\n\n"
            "🔄 <b>Política de bans:</b>\n"
            "• SMS exitoso → proxy y número baneados 24h\n"
            "• Límite diario → solo proxy baneado 24h"
        )
        return

def telegram_polling():
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
    return "OK", 200

@app.route('/health')
def health():
    ahora = datetime.now().isoformat()
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "proxies_activos": db_execute("SELECT COUNT(*) FROM proxies_cache WHERE activo = 1")[0][0],
        "proxies_baneados": db_execute("SELECT COUNT(*) FROM proxies_ban WHERE expira > ?", (ahora,))[0][0],
        "numeros_baneados": db_execute("SELECT COUNT(*) FROM numbers_ban WHERE expira > ?", (ahora,))[0][0]
    })

@app.route('/webhook', methods=['POST'])
def webhook():
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
    try:
        init_db()
        logger.info("🗄️ Base de datos inicializada")
        
        refresh_proxy_cache()
        
        tg_thread = threading.Thread(target=telegram_polling)
        tg_thread.daemon = True
        tg_thread.start()
        logger.info("🤖 Polling de Telegram iniciado")
        
        # Limpiar bans expirados al inicio
        clean_expired_bans()
        
        send_telegram_message(
            "🤖 <b>BOT SMS PRO</b>\n\n"
            "✅ Sistema iniciado\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "🔄 <b>Política de bans:</b>\n"
            "• SMS exitoso → proxy y número baneados 24h\n"
            "• Límite diario → solo proxy baneado 24h\n\n"
            "Usa /help para ver comandos"
        )
        
        logger.info("🌐 Servidor iniciado en http://0.0.0.0:5000")
     #   app.run(host='0.0.0.0', port=5000, debug=False)
        
    except Exception as e:
        logger.error(f"❌ Error en main: {e}")
        send_telegram_message(f"❌ <b>ERROR CRÍTICO</b>\n{str(e)}")
        sys.exit(1)

main()
