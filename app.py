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
INTENTOS_POR_NUMERO = 3  # Reducido a 3 intentos
INTERVALO = 0.3  # Intervalo más rápido
MAX_PROXIES = 500
TIMEOUT_PROXY = 3
TIMEOUT_SMS = 15
BAN_DURATION_HOURS = 24

MAX_NUMEROS_POR_ENVIO = 5000
MAX_RANGO_NUMEROS = 50000
LOTE_SIZE = 500

# ============ BASE DE DATOS ============
DB_FILE = "sms_bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS numbers_ban (
        numero TEXT,
        pais TEXT,
        fecha_ban TIMESTAMP,
        expira TIMESTAMP,
        razon TEXT,
        PRIMARY KEY (numero, pais)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS proxies_ban (
        proxy TEXT PRIMARY KEY,
        fecha_ban TIMESTAMP,
        expira TIMESTAMP,
        razon TEXT,
        fallos_consecutivos INTEGER DEFAULT 0
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS proxies_cache (
        proxy TEXT PRIMARY KEY,
        ultimo_uso TIMESTAMP,
        veces_usado INTEGER DEFAULT 0,
        fallos INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1,
        ultimo_exito TIMESTAMP,
        ultima_prueba TIMESTAMP,
        funcional INTEGER DEFAULT 1
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha TIMESTAMP,
        enviados INTEGER DEFAULT 0,
        fallidos INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        blacklist INTEGER DEFAULT 0
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS config (
        clave TEXT PRIMARY KEY,
        valor TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha TIMESTAMP,
        nivel TEXT,
        mensaje TEXT
    )''')
    
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
def get_proxies_from_sources(limit=200):
    proxies = []
    sources = [
        "https://api.proxyscrape.com/?request=getproxies&proxytype=http&timeout=5000&country=all&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=http",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "https://raw.githubusercontent.com/roosterkid/openproxylist/main/http.txt",
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

def refresh_proxy_cache():
    """Actualiza la caché de proxies - sin probarlos"""
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
            to_insert.append((proxy, datetime.now().isoformat(), 0, 0, 1, None, None, 1))
    
    if to_insert:
        db_execute_many(
            """INSERT OR IGNORE INTO proxies_cache 
               (proxy, ultimo_uso, veces_usado, fallos, activo, ultimo_exito, ultima_prueba, funcional) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
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

def get_working_proxies(limit=30):
    """Obtiene proxies de la caché - SIN PROBAR (modo ultra rápido)"""
    ahora = datetime.now().isoformat()
    
    # Obtener proxies activos no baneados
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
    
    proxies = [r[0] for r in result]
    
    if proxies:
        logger.info(f"✅ Obtenidos {len(proxies)} proxies de caché (sin probar)")
    else:
        logger.warning("⚠️ No hay proxies en caché, recargando...")
        refresh_proxy_cache()
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
        proxies = [r[0] for r in result]
    
    return proxies

def ban_proxy(proxy, razon="fallo", hours=BAN_DURATION_HOURS):
    ahora = datetime.now()
    expira = ahora + timedelta(hours=hours)
    
    db_execute(
        """INSERT OR REPLACE INTO proxies_ban (proxy, fecha_ban, expira, razon) 
           VALUES (?, ?, ?, ?)""",
        (proxy, ahora.isoformat(), expira.isoformat(), razon)
    )
    db_execute("UPDATE proxies_cache SET activo = 0, funcional = 0 WHERE proxy = ?", (proxy,))
    
    logger.info(f"🚫 Proxy baneado: {proxy} - {razon}")

def ban_number(numero, pais, razon="exitoso", hours=BAN_DURATION_HOURS):
    ahora = datetime.now()
    expira = ahora + timedelta(hours=hours)
    
    db_execute(
        """INSERT OR REPLACE INTO numbers_ban (numero, pais, fecha_ban, expira, razon) 
           VALUES (?, ?, ?, ?, ?)""",
        (numero, pais, ahora.isoformat(), expira.isoformat(), razon)
    )
    
    logger.info(f"🚫 Número baneado: +{pais}{numero} - {razon}")

def is_proxy_banned(proxy):
    ahora = datetime.now().isoformat()
    result = db_execute(
        "SELECT proxy FROM proxies_ban WHERE proxy = ? AND expira > ?",
        (proxy, ahora)
    )
    return len(result) > 0

def is_number_banned(numero, pais):
    ahora = datetime.now().isoformat()
    result = db_execute(
        "SELECT numero FROM numbers_ban WHERE numero = ? AND pais = ? AND expira > ?",
        (numero, pais, ahora)
    )
    return len(result) > 0

def clean_expired_bans():
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
                if error and any(p in error.lower() for p in ["only one", "limit", "quota", "1 per day", "per day"]):
                    return False, None, "LIMIT_DAILY"
                if error and "test texts" in error.lower():
                    return False, None, "TEST_TEXT_DISABLED"
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
    pais = config.get('pais', PAIS)
    mensaje = config.get('mensaje', MENSAJE_DEFAULT)
    max_intentos = config.get('intentos', INTENTOS_POR_NUMERO)
    
    if is_number_banned(numero, pais):
        return False, "banned"
    
    phone = '+' + pais + numero
    
    intentos = 0
    while intentos < max_intentos:
        # Si no hay proxies, recargar rápido
        if not working_proxies:
            logger.warning("⚠️ Sin proxies, recargando...")
            new_proxies = get_working_proxies(20)
            if new_proxies:
                working_proxies.extend(new_proxies)
            else:
                time.sleep(1)
                continue
        
        proxy = random.choice(working_proxies)
        
        if is_proxy_banned(proxy):
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            continue
        
        api_key = "textbelt"
        intentos += 1
        
        success, text_id, error = send_sms(phone, mensaje, api_key, proxy)
        
        if success:
            logger.info(f"✅ ÉXITO: +{pais}{numero}")
            
            ban_proxy(proxy, "exitoso_enviado", BAN_DURATION_HOURS)
            ban_number(numero, pais, "exitoso_recibido", BAN_DURATION_HOURS)
            
            db_execute(
                """UPDATE proxies_cache 
                   SET ultimo_uso = ?, veces_usado = veces_usado + 1, ultimo_exito = ?, funcional = 1
                   WHERE proxy = ?""",
                (datetime.now().isoformat(), datetime.now().isoformat(), proxy)
            )
            
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            
            return True, text_id
        
        # Manejar errores - baneamos el proxy en casi todos los casos
        if error in ["LIMIT_DAILY", "TEST_TEXT_DISABLED", "TIMEOUT", "CONNECTION_ERROR"]:
            ban_proxy(proxy, error, BAN_DURATION_HOURS)
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            continue
        
        if error and "invalid" in error.lower():
            ban_number(numero, pais, "numero_invalido", 720)
            return False, "invalid"
        
        if error and "blacklist" in error.lower():
            ban_number(numero, pais, "blacklist_textbelt", 720)
            return False, "banned"
        
        # Otros errores: banear proxy
        ban_proxy(proxy, f"error_{error[:30]}", BAN_DURATION_HOURS)
        if proxy in working_proxies:
            working_proxies.remove(proxy)
    
    return False, "max_intentos"

def procesar_lote(numeros_lote, config, stats, lote_num, total_lotes):
    """Procesa un lote de números - modo ultra rápido"""
    
    # Obtener proxies SIN PROBAR
    working_proxies = get_working_proxies(30)
    
    if not working_proxies:
        logger.error(f"❌ Lote {lote_num}: No hay proxies")
        stats['fallidos'] += len(numeros_lote)
        return stats
    
    logger.info(f"📦 Lote {lote_num}/{total_lotes}: {len(numeros_lote)} números, {len(working_proxies)} proxies (sin probar)")
    
    for i, numero in enumerate(numeros_lote, 1):
        success, result = process_number(numero, config, working_proxies)
        
        if success:
            stats['enviados'] += 1
        elif result == "banned":
            stats['banned'] += 1
        elif result == "invalid":
            stats['invalid'] += 1
        else:
            stats['fallidos'] += 1
        
        # Recargar proxies si quedan pocos
        if len(working_proxies) < 5:
            new_proxies = get_working_proxies(20)
            if new_proxies:
                working_proxies.extend(new_proxies)
                logger.info(f"✅ Recargados {len(new_proxies)} proxies")
        
        # Esperar entre envíos (más rápido)
        time.sleep(config.get('intervalo', INTERVALO) + random.uniform(0, 0.1))
    
    return stats

def ejecutar_envio(numeros, config):
    try:
        clean_expired_bans()
        
        total = len(numeros)
        logger.info(f"🚀 INICIANDO ENVÍO: {total} números")
        
        # Dividir en lotes
        lotes = []
        for i in range(0, total, LOTE_SIZE):
            lotes.append(numeros[i:i+LOTE_SIZE])
        
        total_lotes = len(lotes)
        logger.info(f"📦 Dividido en {total_lotes} lotes de {LOTE_SIZE} números")
        
        stats = {"total": total, "enviados": 0, "fallidos": 0, "banned": 0, "invalid": 0}
        
        for idx, lote in enumerate(lotes, 1):
            logger.info(f"📦 Iniciando lote {idx}/{total_lotes}")
            
            stats = procesar_lote(lote, config, stats, idx, total_lotes)
            
            # Reportar progreso cada lote
            enviados_total = stats['enviados']
            fallidos_total = stats['fallidos']
            banned_total = stats['banned']
            procesados = enviados_total + fallidos_total + banned_total
            
            logger.info(f"📊 Lote {idx}/{total_lotes}: ✅ {enviados_total} | ❌ {fallidos_total} | ⏭️ {banned_total}")
            
            send_telegram_message(
                f"📊 <b>PROGRESO</b>\n"
                f"📦 Lote {idx}/{total_lotes}\n"
                f"📱 {procesados}/{total}\n"
                f"✅ {enviados_total} enviados\n"
                f"❌ {fallidos_total} fallidos\n"
                f"⏭️ {banned_total} baneados"
            )
            
            if idx < total_lotes:
                time.sleep(2)
        
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
            f"📦 Total lotes: {total_lotes}\n"
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

def generar_numeros_rango(inicio, fin):
    numeros = []
    for i in range(inicio, fin + 1):
        numeros.append(str(i).zfill(8))
    return numeros

def process_telegram_message(message):
    if not message:
        return
    
    logger.info(f"📩 Comando Telegram: {message[:100]}")
    
    if message.startswith('/start'):
        send_telegram_message(
            "🤖 <b>Bot SMS Pro</b>\n\n"
            "⚡ Modo ultra rápido activado\n"
            "Comandos:\n"
            "/enviar <números> - Enviar SMS\n"
            "/estado - Ver estado\n"
            "/proxies - Ver proxies\n"
            "/stats - Estadísticas\n"
            "/refresh - Actualizar proxies\n"
            "/unban <número> - Desbanear\n"
            "/help - Ayuda"
        )
        return
    
    if message.startswith('/estado'):
        ahora = datetime.now().isoformat()
        proxies_banned = db_execute("SELECT COUNT(*) FROM proxies_ban WHERE expira > ?", (ahora,))[0][0]
        numbers_banned = db_execute("SELECT COUNT(*) FROM numbers_ban WHERE expira > ?", (ahora,))[0][0]
        proxies_activos = db_execute("SELECT COUNT(*) FROM proxies_cache WHERE activo = 1")[0][0]
        stats = db_execute("SELECT total, enviados, fallidos FROM stats ORDER BY id DESC LIMIT 1")
        
        msg = (
            f"📊 <b>ESTADO</b>\n"
            f"🔄 Proxies: {proxies_activos}\n"
            f"🚫 Proxies baneados: {proxies_banned}\n"
            f"🚫 Números baneados: {numbers_banned}\n"
            f"📱 Último: {stats[0][1] if stats else 0} enviados"
        )
        send_telegram_message(msg)
        return
    
    if message.startswith('/proxies'):
        ahora = datetime.now().isoformat()
        proxies = db_execute(
            """SELECT proxy, veces_usado, fallos,
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
                msg += f"{emoji} {p}\n  Usado: {usado}\n\n"
            send_telegram_message(msg)
        else:
            send_telegram_message("⚠️ No hay proxies")
        return
    
    if message.startswith('/stats'):
        stats = db_execute("SELECT fecha, total, enviados, fallidos, blacklist FROM stats ORDER BY id DESC LIMIT 5")
        if stats:
            msg = "📊 <b>ÚLTIMOS ENVÍOS</b>\n\n"
            for fecha, total, enviados, fallidos, blacklist in stats:
                msg += f"📅 {fecha[:16]}\n  Total: {total} | ✅ {enviados} | ❌ {fallidos} | ⏭️ {blacklist}\n\n"
            send_telegram_message(msg)
        else:
            send_telegram_message("📊 No hay estadísticas")
        return
    
    if message.startswith('/refresh'):
        send_telegram_message("🔄 Actualizando proxies...")
        count = refresh_proxy_cache()
        send_telegram_message(f"✅ Proxies actualizados: {count} nuevos")
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
            send_telegram_message("⚠️ Uso: /enviar <números>")
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
                partes_rango = numeros_str.split('-')
                inicio = int(partes_rango[0].strip())
                fin = int(partes_rango[1].strip())
                
                if fin <= inicio:
                    send_telegram_message("⚠️ El fin debe ser mayor que el inicio")
                    return
                
                total_rango = fin - inicio + 1
                
                if total_rango > MAX_RANGO_NUMEROS:
                    send_telegram_message(f"⚠️ Rango muy grande (máx {MAX_RANGO_NUMEROS})")
                    return
                
                logger.info(f"📊 Generando {total_rango} números")
                numeros = generar_numeros_rango(inicio, fin)
                
            except:
                send_telegram_message("⚠️ Formato inválido. Ej: /enviar 59540000-59540100")
                return
        else:
            numeros = [numeros_str]
        
        if not numeros:
            send_telegram_message("⚠️ No hay números válidos")
            return
        
        if len(numeros) > MAX_NUMEROS_POR_ENVIO:
            send_telegram_message(f"⚠️ Demasiados (máx {MAX_NUMEROS_POR_ENVIO})")
            return
        
        config = {
            "pais": PAIS,
            "mensaje": MENSAJE_DEFAULT,
            "intentos": INTENTOS_POR_NUMERO,
            "intervalo": INTERVALO
        }
        
        total_lotes = (len(numeros) + LOTE_SIZE - 1) // LOTE_SIZE
        
        send_telegram_message(
            f"🚀 <b>INICIANDO ENVÍO</b>\n"
            f"📱 {len(numeros)} números\n"
            f"📦 {total_lotes} lotes"
        )
        
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
            "/start - Comandos\n"
            "/enviar <números> - Enviar SMS\n"
            "/estado - Estado\n"
            "/proxies - Ver proxies\n"
            "/stats - Estadísticas\n"
            "/refresh - Actualizar proxies\n"
            "/unban <número> - Desbanear\n"
            "/help - Ayuda\n\n"
            "⚡ <b>Modo ultra rápido:</b>\n"
            "• Proxies sin probar\n"
            "• Intervalo de 0.3s\n"
            "• 3 intentos por número\n"
            "• Bans de 24h"
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
            logger.error(f"Error en polling: {e}")
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
        
        clean_expired_bans()
        
        send_telegram_message(
            "🤖 <b>BOT SMS PRO</b>\n\n"
            "✅ Sistema iniciado\n"
            "⚡ <b>Modo ultra rápido</b>\n"
            "• Proxies sin probar\n"
            "• Envío inmediato\n"
            "Usa /help para comandos"
        )
        
        logger.info("🌐 Servidor iniciado")
       # app.run(host='0.0.0.0', port=5000, debug=False)
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        send_telegram_message(f"❌ <b>ERROR</b>\n{str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
