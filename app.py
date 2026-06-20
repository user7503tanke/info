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
MAX_PROXIES = 200
TIMEOUT_PROXY = 5
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
        funcional INTEGER DEFAULT 0
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
  #      "https://api.proxyscrape.com/?request=getproxies&proxytype=http&timeout=5000&country=all&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=http",
#        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
   #     "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
   #     "https://raw.githubusercontent.com/roosterkid/openproxylist/main/http.txt",
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

def get_cached_proxies(limit=50, only_functional=False):
    """Obtiene proxies de la caché"""
    ahora = datetime.now().isoformat()
    
    if only_functional:
        # Solo proxies que han funcionado antes
        result = db_execute(
            """SELECT proxy FROM proxies_cache 
               WHERE activo = 1 
               AND funcional = 1
               AND proxy NOT IN (
                   SELECT proxy FROM proxies_ban 
                   WHERE expira > ?
               )
               ORDER BY veces_usado ASC, fallos ASC 
               LIMIT ?""",
            (ahora, limit)
        )
    else:
        # Todos los proxies no baneados
        result = db_execute(
            """SELECT proxy FROM proxies_cache 
               WHERE activo = 1 
               AND proxy NOT IN (
                   SELECT proxy FROM proxies_ban 
                   WHERE expira > ?
               )
               ORDER BY funcional DESC, veces_usado ASC, fallos ASC 
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
            to_insert.append((proxy, datetime.now().isoformat(), 0, 0, 1, None, None, 0))
    
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
                   ORDER BY ultimo_uso ASC, funcional DESC
                   LIMIT ?
               )""",
            (total - MAX_PROXIES,)
        )
    
    logger.info(f"✅ Caché actualizada: {len(to_insert)} nuevos proxies, {total} activos")
    return len(to_insert)

def test_proxy(proxy):
    """Prueba si un proxy funciona - usa un endpoint más confiable"""
    try:
        # Usar multiple endpoints para probar
        test_urls = [
            'https://httpbin.org/ip',
            'https://api.ipify.org',
            'https://ifconfig.me/ip'
        ]
        
        for url in test_urls:
            try:
                r = requests.get(url, proxies={"http": proxy, "https": proxy}, timeout=TIMEOUT_PROXY)
                if r.status_code == 200:
                    # Actualizar estado funcional en DB
                    db_execute(
                        "UPDATE proxies_cache SET funcional = 1, ultima_prueba = ? WHERE proxy = ?",
                        (datetime.now().isoformat(), proxy)
                    )
                    return True
            except:
                continue
        
        # Si fallan todos los endpoints, marcar como no funcional
        db_execute(
            "UPDATE proxies_cache SET funcional = 0, ultima_prueba = ? WHERE proxy = ?",
            (datetime.now().isoformat(), proxy)
        )
        return False
    except:
        return False

def get_working_proxies(limit=20, force_refresh=False):
    """Obtiene proxies funcionales - prioriza los que ya funcionaron"""
    
    # Primero intentar con proxies funcionales conocidos
    functional = get_cached_proxies(limit, only_functional=True)
    
    if functional and not force_refresh:
        # Verificar que sigan funcionando (solo algunos)
        working = []
        for proxy in functional[:limit]:
            # Si el proxy ha sido usado recientemente, confiar en él
            result = db_execute(
                "SELECT ultimo_exito FROM proxies_cache WHERE proxy = ?",
                (proxy,)
            )
            if result and result[0][0]:
                try:
                    ultimo_exito = datetime.fromisoformat(result[0][0])
                    if (datetime.now() - ultimo_exito) < timedelta(minutes=5):
                        # Si tuvo éxito en los últimos 5 minutos, usarlo sin probar
                        working.append(proxy)
                        continue
                except:
                    pass
            
            # Probar proxy
            if test_proxy(proxy):
                working.append(proxy)
        
        if working:
            logger.debug(f"✅ Usando {len(working)} proxies funcionales conocidos")
            return working
    
    # Si no hay funcionales o se fuerza refresh, probar todos
    logger.info("🔄 Buscando proxies funcionales...")
    cached = get_cached_proxies(limit * 3, only_functional=False)
    
    if not cached:
        refresh_proxy_cache()
        cached = get_cached_proxies(limit * 3, only_functional=False)
    
    working = []
    tested = 0
    
    for proxy in cached:
        if tested >= limit * 2:
            break
        
        # No probar proxies ya baneados
        if is_proxy_banned(proxy):
            continue
        
        if test_proxy(proxy):
            working.append(proxy)
            # Marcar como funcional
            db_execute(
                "UPDATE proxies_cache SET funcional = 1, ultimo_uso = ? WHERE proxy = ?",
                (datetime.now().isoformat(), proxy)
            )
        
        tested += 1
        
        if len(working) >= limit:
            break
    
    if working:
        logger.info(f"✅ Encontrados {len(working)} proxies funcionales")
    else:
        logger.warning("⚠️ No se encontraron proxies funcionales")
    
    return working

def ban_proxy(proxy, razon="fallo", hours=BAN_DURATION_HOURS):
    ahora = datetime.now()
    expira = ahora + timedelta(hours=hours)
    
    db_execute(
        """INSERT OR REPLACE INTO proxies_ban (proxy, fecha_ban, expira, razon) 
           VALUES (?, ?, ?, ?)""",
        (proxy, ahora.isoformat(), expira.isoformat(), razon)
    )
    db_execute("UPDATE proxies_cache SET activo = 0, funcional = 0 WHERE proxy = ?", (proxy,))
    
    logger.info(f"🚫 Proxy baneado 24h: {proxy} - {razon}")

def ban_number(numero, pais, razon="exitoso", hours=BAN_DURATION_HOURS):
    ahora = datetime.now()
    expira = ahora + timedelta(hours=hours)
    
    db_execute(
        """INSERT OR REPLACE INTO numbers_ban (numero, pais, fecha_ban, expira, razon) 
           VALUES (?, ?, ?, ?, ?)""",
        (numero, pais, ahora.isoformat(), expira.isoformat(), razon)
    )
    
    logger.info(f"🚫 Número baneado 24h: +{pais}{numero} - {razon}")

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
                # Errores que indican límite diario
                if error and any(p in error.lower() for p in ["only one", "limit", "quota", "1 per day", "per day"]):
                    return False, None, "LIMIT_DAILY"
                # Errores de texto de prueba
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
        logger.debug(f"⏭️ Número baneado: +{pais}{numero}")
        return False, "banned"
    
    phone = '+' + pais + numero
    
    intentos = 0
    while intentos < max_intentos:
        # Si no hay proxies, intentar recargar
        if not working_proxies:
            logger.warning("⚠️ Sin proxies, recargando...")
            new_proxies = get_working_proxies(15, force_refresh=True)
            if new_proxies:
                working_proxies.extend(new_proxies)
                logger.info(f"✅ Recargados {len(new_proxies)} proxies")
            else:
                logger.warning("⚠️ No se pudieron obtener proxies, esperando 10s...")
                time.sleep(10)
                continue
        
        # Seleccionar proxy
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
            logger.info(f"✅ ÉXITO: +{pais}{numero} - Baneando proxy y número por 24h")
            
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
        
        # Manejar errores específicos
        if error == "LIMIT_DAILY":
            logger.warning(f"⚠️ Límite diario con proxy {proxy} - Baneando solo proxy")
            ban_proxy(proxy, "limite_diario", BAN_DURATION_HOURS)
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            continue
        
        if error == "TEST_TEXT_DISABLED":
            logger.warning(f"⚠️ Test texts disabled con proxy {proxy} - Baneando solo proxy")
            ban_proxy(proxy, "test_texts_disabled", BAN_DURATION_HOURS)
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            continue
        
        if error in ["TIMEOUT", "CONNECTION_ERROR"]:
            logger.warning(f"⚠️ Error de conexión con {proxy} - Baneando proxy")
            ban_proxy(proxy, error, BAN_DURATION_HOURS)
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            continue
        
        if error and "invalid" in error.lower():
            logger.warning(f"⚠️ Número inválido: +{pais}{numero}")
            ban_number(numero, pais, "numero_invalido", 720)
            return False, "invalid"
        
        if error and "blacklist" in error.lower():
            logger.warning(f"⚠️ Número en blacklist: +{pais}{numero}")
            ban_number(numero, pais, "blacklist_textbelt", 720)
            return False, "banned"
        
        # Otros errores: banear proxy
        logger.warning(f"⚠️ Error '{error}' con proxy {proxy} - Baneando proxy")
        ban_proxy(proxy, f"error_{error[:30]}", BAN_DURATION_HOURS)
        if proxy in working_proxies:
            working_proxies.remove(proxy)
    
    return False, "max_intentos"

def procesar_lote(numeros_lote, config, stats, lote_num, total_lotes):
    """Procesa un lote de números con mejor manejo de proxies"""
    working_proxies = get_working_proxies(20, force_refresh=True)
    
    if not working_proxies:
        logger.error(f"❌ Lote {lote_num}: No hay proxies funcionales")
        # Intentar obtener proxies sin probar (modo rápido)
        working_proxies = get_cached_proxies(20, only_functional=False)
        if not working_proxies:
            return stats
    
    logger.info(f"📦 Procesando lote {lote_num}/{total_lotes} ({len(numeros_lote)} números) con {len(working_proxies)} proxies")
    
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
        
        # Recargar proxies si es necesario
        if len(working_proxies) < 3:
            logger.info("🔄 Recargando proxies...")
            new_proxies = get_working_proxies(15, force_refresh=True)
            if new_proxies:
                working_proxies.extend(new_proxies)
                logger.info(f"✅ {len(new_proxies)} proxies añadidos")
            else:
                # Si no hay proxies funcionales, usar caché sin probar
                new_proxies = get_cached_proxies(10, only_functional=False)
                if new_proxies:
                    working_proxies.extend(new_proxies)
                    logger.info(f"⚠️ Usando {len(new_proxies)} proxies sin probar (modo rápido)")
        
        # Esperar entre envíos
        time.sleep(config.get('intervalo', INTERVALO) * 0.3 + random.uniform(0, 0.3))
    
    return stats

def ejecutar_envio(numeros, config):
    """Ejecuta el envío masivo por lotes"""
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
        
        send_telegram_message(
            f"🚀 <b>INICIANDO ENVÍO</b>\n"
            f"📱 {total} números\n"
            f"📦 {total_lotes} lotes\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        
        stats = {"total": total, "enviados": 0, "fallidos": 0, "banned": 0, "invalid": 0}
        
        for idx, lote in enumerate(lotes, 1):
            logger.info(f"📦 Iniciando lote {idx}/{total_lotes}")
            
            # Limpiar bans expirados antes de cada lote
            clean_expired_bans()
            
            stats = procesar_lote(lote, config, stats, idx, total_lotes)
            
            # Reportar progreso
            enviados_total = stats['enviados']
            fallidos_total = stats['fallidos']
            banned_total = stats['banned']
            procesados = enviados_total + fallidos_total + banned_total
            
            logger.info(f"📊 Lote {idx}/{total_lotes}: ✅ {enviados_total} | ❌ {fallidos_total} | ⏭️ {banned_total}")
            
            send_telegram_message(
                f"📊 <b>PROGRESO</b>\n"
                f"📦 Lote {idx}/{total_lotes}\n"
                f"📱 {procesados}/{total}\n"
                f"✅ {enviados_total} enviados (baneados 24h)\n"
                f"❌ {fallidos_total} fallidos\n"
                f"⏭️ {banned_total} baneados"
            )
            
            # Esperar entre lotes
            if idx < total_lotes:
                time.sleep(3)
        
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
    """Genera números en un rango de manera eficiente"""
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
        ahora = datetime.now().isoformat()
        proxies_banned = db_execute("SELECT COUNT(*) FROM proxies_ban WHERE expira > ?", (ahora,))[0][0]
        numbers_banned = db_execute("SELECT COUNT(*) FROM numbers_ban WHERE expira > ?", (ahora,))[0][0]
        proxies_activos = db_execute("SELECT COUNT(*) FROM proxies_cache WHERE activo = 1")[0][0]
        proxies_funcionales = db_execute("SELECT COUNT(*) FROM proxies_cache WHERE funcional = 1 AND activo = 1")[0][0]
        stats = db_execute("SELECT total, enviados, fallidos FROM stats ORDER BY id DESC LIMIT 1")
        
        msg = (
            f"📊 <b>ESTADO DEL SISTEMA</b>\n"
            f"🔄 Proxies totales: {proxies_activos}\n"
            f"✅ Proxies funcionales: {proxies_funcionales}\n"
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
            """SELECT p.proxy, p.veces_usado, p.fallos, p.funcional,
               CASE WHEN b.proxy IS NOT NULL THEN 'baneado' ELSE 'activo' END as estado
               FROM proxies_cache p
               LEFT JOIN proxies_ban b ON p.proxy = b.proxy AND b.expira > ?
               WHERE p.activo = 1
               ORDER BY p.funcional DESC, p.veces_usado DESC 
               LIMIT 15""",
            (ahora,)
        )
        if proxies:
            msg = "🌐 <b>PROXIES</b>\n\n"
            for p, usado, fallos, funcional, estado in proxies:
                if estado == "baneado":
                    emoji = "🔴"
                elif funcional:
                    emoji = "🟢"
                else:
                    emoji = "🟡"
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
        # Marcar todos como no funcionales para forzar prueba
        db_execute("UPDATE proxies_cache SET funcional = 0")
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
                partes_rango = numeros_str.split('-')
                if len(partes_rango) != 2:
                    send_telegram_message("⚠️ Formato de rango inválido. Ej: /enviar 59540000-59540100")
                    return
                
                inicio = int(partes_rango[0].strip())
                fin = int(partes_rango[1].strip())
                
                if fin <= inicio:
                    send_telegram_message("⚠️ El fin debe ser mayor que el inicio")
                    return
                
                total_rango = fin - inicio + 1
                
                if total_rango > MAX_RANGO_NUMEROS:
                    send_telegram_message(f"⚠️ Rango demasiado grande (máx {MAX_RANGO_NUMEROS} números)")
                    return
                
                if total_rango > MAX_NUMEROS_POR_ENVIO:
                    send_telegram_message(
                        f"⚠️ El rango tiene {total_rango} números, se procesarán en lotes de {LOTE_SIZE}\n"
                        f"📦 Total lotes: {(total_rango + LOTE_SIZE - 1) // LOTE_SIZE}"
                    )
                
                logger.info(f"📊 Generando {total_rango} números en rango {inicio}-{fin}")
                numeros = generar_numeros_rango(inicio, fin)
                
            except ValueError:
                send_telegram_message("⚠️ Formato de rango inválido. Usa números. Ej: /enviar 59540000-59540100")
                return
            except Exception as e:
                send_telegram_message(f"⚠️ Error en el rango: {str(e)}")
                return
        else:
            numeros = [numeros_str]
        
        if not numeros:
            send_telegram_message("⚠️ No se encontraron números válidos")
            return
        
        if len(numeros) > MAX_NUMEROS_POR_ENVIO:
            send_telegram_message(f"⚠️ Demasiados números (máx {MAX_NUMEROS_POR_ENVIO})")
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
            f"📦 {total_lotes} lotes de {LOTE_SIZE}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        
        def run_send():
            try:
                stats = ejecutar_envio(numeros, config)
                send_telegram_message(
                    f"✅ <b>ENVÍO COMPLETADO</b>\n"
                    f"📱 {stats['total']} números\n"
                    f"✅ {stats['enviados']} enviados (baneados 24h)\n"
                    f"❌ {stats['fallidos']} fallidos\n"
                    f"⏭️ {stats['banned']} baneados\n"
                    f"⚠️ {stats['invalid']} inválidos"
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
            "📱 <b>Límites:</b>\n"
            f"• Máx números por envío: {MAX_NUMEROS_POR_ENVIO}\n"
            f"• Máx rango: {MAX_RANGO_NUMEROS} números\n"
            f"• Lotes de {LOTE_SIZE} números\n\n"
            "🔄 <b>Política de bans:</b>\n"
            "• SMS exitoso → proxy y número baneados 24h\n"
            "• Límite diario → solo proxy baneado 24h\n"
            "• Error de conexión → proxy baneado 24h"
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
        "proxies_funcionales": db_execute("SELECT COUNT(*) FROM proxies_cache WHERE funcional = 1 AND activo = 1")[0][0],
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
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "🔄 <b>Mejoras implementadas:</b>\n"
            "• Proxies funcionales marcados para uso rápido\n"
            "• Modo rápido: usa proxies sin probar si no hay funcionales\n"
            "• Errores 'test texts' banean solo el proxy\n"
            "• Recarga inteligente de proxies\n\n"
            "Usa /help para ver comandos"
        )
        
        logger.info("🌐 Servidor iniciado en http://0.0.0.0:5000")
  #      app.run(host='0.0.0.0', port=5000, debug=False)
        
    except Exception as e:
        logger.error(f"❌ Error en main: {e}")
        send_telegram_message(f"❌ <b>ERROR CRÍTICO</b>\n{str(e)}")
        sys.exit(1)

main()
