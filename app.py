#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import json
import random
import time
import threading
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template_string, send_file
import requests

# ==================== CONFIGURACIÓN TELEGRAM ====================
TELEGRAM_BOT_TOKEN = "7920655514:AAEH1vWk2hOkNfN_eREpe6DrPBz1mZNAQYw"
TELEGRAM_CHAT_ID = "7587515668"
TELEGRAM_ENABLED = True

# ==================== CONFIGURACIÓN PRINCIPAL ====================
PAIS = "53"
MENSAJE = "Cubanos, el momento es ahora. La libertad no se pide, se conquista. ¡Por una Cuba libre!"
INTENTOS_POR_NUMERO = 3
INTERVALO = 2
MAX_INTENTOS_LIMITE = 3
MAX_PROXIES = 100

API_KEYS = ["textbelt"]

# Archivos
BLACKLIST_FILE = "proxy_blacklist.json"
NUMBERS_BLACKLIST_FILE = "numbers_blacklist.json"
CONFIG_FILE = "sms_config.json"
LOG_FILE = "sms_pro.log"

app = Flask(__name__)
app.secret_key = 'tu_clave_secreta_cambia_esto'

# ==================== LOGGING ====================
class TelegramHandler(logging.Handler):
    def __init__(self, bot_token, chat_id):
        super().__init__()
        self.bot_token = bot_token
        self.chat_id = chat_id
    
    def emit(self, record):
        if not TELEGRAM_ENABLED:
            return
        try:
            msg = self.format(record)
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {"chat_id": self.chat_id, "text": msg[:4096], "parse_mode": "HTML"}
            thread = threading.Thread(target=self._send_telegram, args=(url, data))
            thread.daemon = True
            thread.start()
        except:
            pass
    
    def _send_telegram(self, url, data):
        try:
            requests.post(url, data=data, timeout=5)
        except:
            pass

def setup_logging():
    logger = logging.getLogger('SMSPro')
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    if TELEGRAM_ENABLED:
        telegram_handler = TelegramHandler(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        telegram_handler.setFormatter(formatter)
        logger.addHandler(telegram_handler)
    
    return logger

logger = setup_logging()

# ==================== UTILIDADES ====================
def send_telegram_message(message):
    """Envía un mensaje a Telegram"""
    if not TELEGRAM_ENABLED:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message[:4096], "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=5)
    except:
        pass

def send_telegram_result(numero, success, text_id=None, error=None, intento=None, total_intentos=None):
    """Envía el resultado de cada número a Telegram"""
    if not TELEGRAM_ENABLED:
        return
    
    if success:
        mensaje = (
            f"✅ <b>ENVIADO</b>\n"
            f"📱 Número: <code>+{PAIS}{numero}</code>\n"
            f"🆔 ID: <code>{text_id or 'OK'}</code>\n"
            f"🔄 Intento: {intento}/{total_intentos}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
    else:
        mensaje = (
            f"❌ <b>FALLIDO</b>\n"
            f"📱 Número: <code>+{PAIS}{numero}</code>\n"
            f"⚠️ Error: <code>{error or 'Desconocido'}</code>\n"
            f"🔄 Intento: {intento}/{total_intentos}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
    
    # Enviar en hilo separado para no bloquear
    thread = threading.Thread(target=send_telegram_message, args=(mensaje,))
    thread.daemon = True
    thread.start()

def load_json(file, default=None):
    try:
        with open(file, 'r') as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def save_json(file, data):
    with open(file, 'w') as f:
        json.dump(data, f, indent=2)

def random_user_agent():
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/119.0.6045.160 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1_1) AppleWebKit/605.1.15 Version/17.1 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 Chrome/120.0.6099.230 Mobile Safari/537.36",
    ]
    return random.choice(uas)

def is_blacklisted(item, blacklist):
    if not item or item in [None, "directo"]:
        return False
    if item in blacklist:
        try:
            expiry = datetime.fromisoformat(blacklist[item])
            if datetime.now() < expiry:
                return True
            else:
                del blacklist[item]
                return False
        except:
            return False
    return False

def add_blacklist(item, blacklist, file):
    if not item or item in [None, "directo"]:
        return
    expiry = (datetime.now() + timedelta(hours=24)).isoformat()
    blacklist[item] = expiry
    save_json(file, blacklist)

# ==================== PROXIES ====================
def get_proxies(limit=100):
    proxies = []
    sources = [
        "https://api.proxyscrape.com/?request=getproxies&proxytype=http&timeout=5000&country=all&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=http",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
    ]
    
    for url in sources:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                for line in r.text.splitlines():
                    line = line.strip()
                    if line and ':' in line and not line.startswith('#'):
                        proxies.append(f"http://{line}")
                if len(proxies) >= limit:
                    break
        except:
            continue
    
    blacklist = load_json(BLACKLIST_FILE)
    proxies = list(set(proxies))
    proxies = [p for p in proxies if not is_blacklisted(p, blacklist)]
    
    return proxies[:limit]

def test_proxy(proxy):
    try:
        r = requests.get('https://www.google.com', 
                        proxies={"http": proxy, "https": proxy}, 
                        timeout=3)
        if r.status_code == 200:
            return proxy
    except:
        pass
    return None

def get_working_proxies(proxy_list, max_workers=20):
    if not proxy_list:
        return []
    
    blacklist = load_json(BLACKLIST_FILE)
    proxy_list = [p for p in proxy_list if not is_blacklisted(p, blacklist)]
    
    if not proxy_list:
        return []
    
    working = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(test_proxy, p): p for p in proxy_list}
        for future in as_completed(futures):
            res = future.result()
            if res:
                working.append(res)
    
    return working

# ==================== SMS ====================
def send_sms(phone, message, api_key, proxy=None):
    url = "https://textbelt.com/text"
    data = {"phone": phone, "message": message, "key": api_key}
    headers = {"User-Agent": random_user_agent()}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    try:
        r = requests.post(url, data=data, headers=headers, proxies=proxies, timeout=15)
        if r.status_code == 200:
            result = r.json()
            if result.get('success'):
                return True, result.get('textId', 'OK'), None
            else:
                return False, None, result.get('error', 'Error')
        else:
            return False, None, f"HTTP {r.status_code}"
    except requests.exceptions.Timeout:
        return False, None, "TIMEOUT"
    except requests.exceptions.ConnectionError:
        return False, None, "CONNECTION_ERROR"
    except Exception as e:
        return False, None, str(e)[:50]

def process_number(numero, config, working_proxies, proxy_blacklist, numbers_blacklist, stats):
    """Procesa un número y envía resultado a Telegram"""
    phone = '+' + config['pais'] + numero
    message = config['mensaje']
    max_intentos = config['intentos']
    
    # Verificar blacklist de números
    if is_blacklisted(numero, numbers_blacklist):
        mensaje = f"⏭️ <b>BLACKLIST</b>\n📱 Número: <code>+{config['pais']}{numero}</code>\n⏰ 24h de bloqueo"
        send_telegram_message(mensaje)
        return False, "blacklist"
    
    intentos_reales = 0
    intentos_limite = 0
    
    while intentos_reales < max_intentos:
        proxy = random.choice(working_proxies) if working_proxies else None
        api_key = random.choice(API_KEYS)
        
        if proxy and is_blacklisted(proxy, proxy_blacklist):
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            continue
        
        intento_actual = intentos_reales + 1
        
        # Enviar SMS
        success, text_id, error = send_sms(phone, message, api_key, proxy)
        
        # Si es error de conexión, NO cuenta como intento
        if error in ["TIMEOUT", "CONNECTION_ERROR"] or "ConnectionError" in str(error):
            logger.warning(f"⚠️ Error conexión {numero}: {error} (no cuenta)")
            
            # Enviar notificación de error de conexión
            mensaje = (
                f"⚠️ <b>ERROR DE CONEXIÓN</b>\n"
                f"📱 Número: <code>+{config['pais']}{numero}</code>\n"
                f"🔌 Error: <code>{error}</code>\n"
                f"🔄 Reintentando... (no cuenta como intento)"
            )
            send_telegram_message(mensaje)
            
            if proxy:
                add_blacklist(proxy, proxy_blacklist, BLACKLIST_FILE)
                if proxy in working_proxies:
                    working_proxies.remove(proxy)
            if not working_proxies:
                new_proxies = get_proxies(50)
                if new_proxies:
                    working_proxies.extend(get_working_proxies(new_proxies))
                if not working_proxies:
                    working_proxies.append(None)
            time.sleep(1)
            continue
        
        # Si es éxito
        if success:
            logger.info(f"✅ ENVIADO {numero} | ID: {text_id}")
            
            # Enviar resultado a Telegram
            send_telegram_result(numero, True, text_id, None, intento_actual, max_intentos)
            
            if proxy:
                add_blacklist(proxy, proxy_blacklist, BLACKLIST_FILE)
                if proxy in working_proxies:
                    working_proxies.remove(proxy)
            return True, text_id
        
        # Si es error de límite (only one per day)
        if error and any(p in error.lower() for p in ["only one", "limit", "quota"]):
            intentos_limite += 1
            intentos_reales += 1
            
            logger.warning(f"❌ Límite {numero}: {error} ({intentos_limite}/{MAX_INTENTOS_LIMITE})")
            
            # Enviar notificación de límite
            mensaje = (
                f"⚠️ <b>LÍMITE ALCANZADO</b>\n"
                f"📱 Número: <code>+{config['pais']}{numero}</code>\n"
                f"📊 Intento: {intentos_limite}/{MAX_INTENTOS_LIMITE}\n"
                f"⏰ Esperando nuevo proxy..."
            )
            send_telegram_message(mensaje)
            
            if intentos_limite >= MAX_INTENTOS_LIMITE:
                # Enviar notificación de blacklist
                mensaje = (
                    f"🚫 <b>BLACKLIST POR LÍMITE</b>\n"
                    f"📱 Número: <code>+{config['pais']}{numero}</code>\n"
                    f"⚠️ {MAX_INTENTOS_LIMITE} intentos de límite alcanzados\n"
                    f"⏰ Bloqueado por 24h"
                )
                send_telegram_message(mensaje)
                add_blacklist(numero, numbers_blacklist, NUMBERS_BLACKLIST_FILE)
                return False, "limite_blacklist"
            
            if proxy:
                add_blacklist(proxy, proxy_blacklist, BLACKLIST_FILE)
                if proxy in working_proxies:
                    working_proxies.remove(proxy)
            
            if not working_proxies:
                new_proxies = get_proxies(50)
                if new_proxies:
                    working_proxies.extend(get_working_proxies(new_proxies))
                if not working_proxies:
                    working_proxies.append(None)
            
            if intentos_reales < max_intentos:
                time.sleep(config['intervalo'] + random.uniform(0, 1))
            continue
        
        # Otros errores
        logger.error(f"❌ Error {numero}: {error}")
        intentos_reales += 1
        
        # Enviar resultado fallido a Telegram
        send_telegram_result(numero, False, None, error, intento_actual, max_intentos)
        
        if intentos_reales < max_intentos:
            time.sleep(config['intervalo'] + random.uniform(0, 1))
    
    # Si llegamos aquí, se agotaron los intentos
    mensaje = (
        f"❌ <b>AGOTADO</b>\n"
        f"📱 Número: <code>+{config['pais']}{numero}</code>\n"
        f"⚠️ {max_intentos} intentos fallidos\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )
    send_telegram_message(mensaje)
    
    return False, "agotado"

def ejecutar_envio(numeros, config):
    try:
        logger.info(f"🚀 INICIANDO ENVÍO: {len(numeros)} números")
        
        # Enviar inicio a Telegram
        mensaje_inicio = (
            f"🚀 <b>INICIANDO ENVÍO MASIVO</b>\n"
            f"📱 Total números: {len(numeros)}\n"
            f"📝 Mensaje: {config['mensaje'][:100]}...\n"
            f"🔄 Intentos por número: {config['intentos']}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        send_telegram_message(mensaje_inicio)
        
        # Obtener proxies
        proxy_list = get_proxies(MAX_PROXIES)
        if proxy_list:
            working_proxies = get_working_proxies(proxy_list)
            if not working_proxies:
                working_proxies = [None]
        else:
            working_proxies = [None]
        
        proxy_blacklist = load_json(BLACKLIST_FILE)
        numbers_blacklist = load_json(NUMBERS_BLACKLIST_FILE)
        
        stats = {"total": len(numeros), "enviados": 0, "fallidos": 0, "blacklist": 0, "limite_blacklist": 0}
        
        for i, numero in enumerate(numeros, 1):
            logger.info(f"▶ [{i}/{stats['total']}] +{config['pais']} {numero}")
            
            # Enviar notificación de inicio de número
            mensaje_progreso = (
                f"🔄 <b>PROCESANDO</b> [{i}/{stats['total']}]\n"
                f"📱 Número: <code>+{config['pais']}{numero}</code>"
            )
            send_telegram_message(mensaje_progreso)
            
            success, result = process_number(
                numero, config, working_proxies,
                proxy_blacklist, numbers_blacklist, stats
            )
            
            if success:
                stats['enviados'] += 1
            elif result == "blacklist":
                stats['blacklist'] += 1
            elif result == "limite_blacklist":
                stats['limite_blacklist'] += 1
                stats['blacklist'] += 1
            else:
                stats['fallidos'] += 1
            
            # Enviar resumen cada 10 números
            if i % 10 == 0:
                resumen_parcial = (
                    f"📊 <b>PROGRESO</b> [{i}/{stats['total']}]\n"
                    f"✅ Enviados: {stats['enviados']}\n"
                    f"❌ Fallidos: {stats['fallidos']}\n"
                    f"⏭️ Blacklist: {stats['blacklist']}"
                )
                send_telegram_message(resumen_parcial)
                logger.info(f"📊 Progreso: {i}/{stats['total']} | ✅ {stats['enviados']}")
            
            if i < stats['total']:
                time.sleep(config['intervalo'] * 0.5 + random.uniform(0, 2))
        
        # Resumen final
        resumen = (
            f"📊 <b>RESUMEN FINAL</b>\n"
            f"📱 Total: {stats['total']}\n"
            f"✅ Enviados: {stats['enviados']}\n"
            f"❌ Fallidos: {stats['fallidos']}\n"
            f"⏭️ Blacklist: {stats['blacklist']}"
        )
        if stats['limite_blacklist'] > 0:
            resumen += f"\n🚫 Blacklist límite: {stats['limite_blacklist']}"
        resumen += f"\n⏰ {datetime.now().strftime('%H:%M:%S')}"
        
        logger.info(resumen)
        send_telegram_message(resumen)
        
        stats['fecha'] = datetime.now().isoformat()
        save_json("sms_stats.json", stats)
        
        return stats
    except Exception as e:
        logger.error(f"Error en ejecución: {e}")
        send_telegram_message(f"❌ <b>ERROR CRÍTICO</b>\n{str(e)}")
        raise

# ==================== HTML TEMPLATE (COMPLETO) ====================
# El HTML es el mismo que antes, se omitió por límite de caracteres
# pero en el código completo va incluido

# ==================== ENDPOINTS API ====================
@app.route('/')
def index():
    """Página principal"""
    proxy_blacklist = load_json(BLACKLIST_FILE)
    return render_template_string(
        HTML_TEMPLATE,
        pais=PAIS,
        mensaje=MENSAJE,
        intentos=INTENTOS_POR_NUMERO,
        api_keys=API_KEYS,
        api_keys_count=len(API_KEYS),
        proxies_count=0,
        blacklist_count=len(proxy_blacklist)
    )

@app.route('/api/enviar', methods=['POST'])
def api_enviar():
    """Endpoint para enviar SMS"""
    try:
        data = request.get_json()
        modo = data.get('modo', 'lista')
        numeros = []
        
        if modo == 'lista':
            numeros = data.get('numeros', [])
        else:
            rango_data = data.get('numeros', [])
            if rango_data and len(rango_data) > 0:
                partes = rango_data[0].split(':')
                if len(partes) == 3 and partes[0] == 'RANGO':
                    inicio = partes[1]
                    fin = partes[2]
                    try:
                        inicio_int = int(inicio)
                        fin_int = int(fin)
                        for i in range(inicio_int, fin_int + 1):
                            numeros.append(str(i).zfill(8))
                        if len(numeros) > 100000:
                            numeros = numeros[:100000]
                    except:
                        pass
        
        if not numeros:
            return jsonify({"error": "No hay números válidos"}), 400
        
        config = {
            "pais": data.get('pais', PAIS),
            "mensaje": data.get('mensaje', MENSAJE),
            "intentos": data.get('intentos', INTENTOS_POR_NUMERO),
            "intervalo": INTERVALO
        }
        
        # Ejecutar en hilo separado
        thread = threading.Thread(target=ejecutar_envio, args=(numeros, config))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "status": "aceptado",
            "mensaje": f"Enviando a {len(numeros)} números en segundo plano",
            "numeros": len(numeros)
        }), 202
        
    except Exception as e:
        logger.error(f"Error en endpoint: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/claves', methods=['POST'])
def api_claves():
    """Actualizar claves API"""
    try:
        data = request.get_json()
        nuevas_claves = data.get('claves', [])
        
        if not nuevas_claves:
            return jsonify({"error": "No se enviaron claves"}), 400
        
        global API_KEYS
        API_KEYS = nuevas_claves
        
        config = load_json(CONFIG_FILE, {})
        config['api_keys'] = API_KEYS
        save_json(CONFIG_FILE, config)
        
        logger.info(f"🔑 Claves API actualizadas: {len(API_KEYS)}")
        return jsonify({"status": "ok", "mensaje": f"{len(API_KEYS)} claves guardadas"})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/proxies/recargar', methods=['POST'])
def api_recargar_proxies():
    """Recargar proxies"""
    try:
        proxies = get_proxies(MAX_PROXIES)
        working = get_working_proxies(proxies)
        count = len(working)
        
        logger.info(f"🌐 Proxies recargados: {count} funcionales")
        return jsonify({"status": "ok", "mensaje": f"{count} proxies funcionales", "proxies": count})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/blacklist/limpiar', methods=['DELETE'])
def api_limpiar_blacklist():
    """Limpiar toda la blacklist"""
    try:
        save_json(BLACKLIST_FILE, {})
        save_json(NUMBERS_BLACKLIST_FILE, {})
        logger.info("🗑️ Blacklist limpiada")
        send_telegram_message("🗑️ <b>Blacklist limpiada</b>")
        return jsonify({"status": "ok", "mensaje": "Blacklist limpiada exitosamente"})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Obtener estadísticas"""
    try:
        stats = load_json("sms_stats.json", {})
        return jsonify({
            "status": "ok",
            "total": stats.get('total', 0),
            "enviados": stats.get('enviados', 0),
            "fallidos": stats.get('fallidos', 0),
            "blacklist": stats.get('blacklist', 0)
        })
    except:
        return jsonify({"status": "ok", "total": 0, "enviados": 0, "fallidos": 0, "blacklist": 0})

@app.route('/api/logs', methods=['GET'])
def api_logs():
    """Obtener últimos logs"""
    try:
        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()
            logs = [line.strip() for line in lines[-20:] if line.strip()]
            return jsonify({"status": "ok", "logs": logs})
    except:
        return jsonify({"status": "ok", "logs": []})

