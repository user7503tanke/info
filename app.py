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
from flask import Flask, request, jsonify, render_template_string
import requests

TELEGRAM_BOT_TOKEN = "7920655514:AAEH1vWk2hOkNfN_eREpe6DrPBz1mZNAQYw"
TELEGRAM_CHAT_ID = "7587515668"
TELEGRAM_ENABLED = True

PAIS = "53"
MENSAJE = "Ya basta de sombra. Merecemos sol. Despierten, que el futuro no espera."
INTENTOS_POR_NUMERO = 2
INTERVALO = 1
MAX_INTENTOS_LIMITE = 2
MAX_PROXIES = 100
TIMEOUT_PROXY = 5
TIMEOUT_SMS = 10

API_KEYS = ["textbelt"]

BLACKLIST_FILE = "proxy_blacklist.json"
NUMBERS_BLACKLIST_FILE = "numbers_blacklist.json"
CONFIG_FILE = "sms_config.json"
LOG_FILE = "sms_pro.log"

app = Flask(__name__)
app.secret_key = 'tu_clave_secreta_cambia_esto'

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

def random_user_agent():
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/119.0.6045.160 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.1 Safari/605.1.15",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1_1 like Mac OS X) AppleWebKit/605.1.15 Version/17.1 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 Chrome/120.0.6099.230 Mobile Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36 Edg/120.0.2210.91",
    ]
    return random.choice(uas)

def send_telegram_message(message):
    if not TELEGRAM_ENABLED:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message[:4096], "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=5)
    except:
        pass

def send_telegram_result(numero, success, text_id=None, error=None, intento=None, total_intentos=None):
    if not TELEGRAM_ENABLED:
        return
    
    if success:
        mensaje = f"✅ <b>ENVIADO</b>\n📱 +{PAIS}{numero}\n🆔 {text_id or 'OK'}\n🔄 {intento}/{total_intentos}"
    else:
        mensaje = f"❌ <b>FALLIDO</b>\n📱 +{PAIS}{numero}\n⚠️ {error or 'Desconocido'}\n🔄 {intento}/{total_intentos}"
    
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

def is_blacklisted(item, blacklist):
    if not item:
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
    if not item:
        return
    expiry = (datetime.now() + timedelta(hours=24)).isoformat()
    blacklist[item] = expiry
    save_json(file, blacklist)

def get_proxies(limit=10):
    proxies = []
    sources = [
        "https://api.proxyscrape.com/?request=getproxies&proxytype=http&timeout=5000&country=all&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=http",
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
    
    blacklist = load_json(BLACKLIST_FILE)
    proxies = list(set(proxies))
    proxies = [p for p in proxies if not is_blacklisted(p, blacklist)]
    
    return proxies[:limit]

def test_proxy(proxy):
    try:
        r = requests.get('https://www.google.com', proxies={"http": proxy, "https": proxy}, timeout=3)
        if r.status_code == 200:
            return proxy
    except:
        pass
    return None

def get_working_proxies(proxy_list, max_workers=10):
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
                if len(working) >= 100:
                    break
    
    return working

def send_sms(phone, message, api_key, proxy):
    url = "https://textbelt.com/text"
    data = {"phone": phone, "message": message, "key": api_key}
    headers = {"User-Agent": random_user_agent()}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    try:
        r = requests.post(url, data=data, headers=headers, proxies=proxies, timeout=10)
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

def process_number(numero, config, working_proxies, proxy_blacklist, numbers_blacklist):
    phone = '+' + config['pais'] + numero
    message = config['mensaje']
    max_intentos = config['intentos']
    
    if is_blacklisted(numero, numbers_blacklist):
        return False, "blacklist"
    
    intentos_reales = 0
    
    while intentos_reales < max_intentos:
        if not working_proxies:
            logger.warning("⚠️ No hay proxies, recargando...")
            proxy_list = get_proxies(MAX_PROXIES)
            if proxy_list:
                new_proxies = get_working_proxies(proxy_list)
                working_proxies.extend(new_proxies)
                logger.info(f"✅ Recargados {len(new_proxies)} proxies")
            if not working_proxies:
                time.sleep(5)
                continue
        
        proxy = random.choice(working_proxies)
        
        if proxy and is_blacklisted(proxy, proxy_blacklist):
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            continue
        
        api_key = random.choice(API_KEYS)
        intento_actual = intentos_reales + 1
        
        success, text_id, error = send_sms(phone, message, api_key, proxy)
        
        if error in ["TIMEOUT", "CONNECTION_ERROR"]:
            add_blacklist(proxy, proxy_blacklist, BLACKLIST_FILE)
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            time.sleep(0.5)
            continue
        
        if success:
            add_blacklist(proxy, proxy_blacklist, BLACKLIST_FILE)
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            return True, text_id
        
        if error and any(p in error.lower() for p in ["only one", "limit", "quota"]):
            intentos_reales += 1
            if intentos_reales >= max_intentos:
                add_blacklist(numero, numbers_blacklist, NUMBERS_BLACKLIST_FILE)
                return False, "limite_blacklist"
            
            add_blacklist(proxy, proxy_blacklist, BLACKLIST_FILE)
            if proxy in working_proxies:
                working_proxies.remove(proxy)
            
            if len(working_proxies) < 10:
                proxy_list = get_proxies(MAX_PROXIES)
                if proxy_list:
                    new_proxies = get_working_proxies(proxy_list)
                    working_proxies.extend(new_proxies)
            
            if intentos_reales < max_intentos:
                time.sleep(config['intervalo'] + random.uniform(0, 0.5))
            continue
        
        intentos_reales += 1
        add_blacklist(proxy, proxy_blacklist, BLACKLIST_FILE)
        if proxy in working_proxies:
            working_proxies.remove(proxy)
        
        if len(working_proxies) < 10:
            proxy_list = get_proxies(MAX_PROXIES)
            if proxy_list:
                new_proxies = get_working_proxies(proxy_list)
                working_proxies.extend(new_proxies)
        
        if intentos_reales < max_intentos:
            time.sleep(config['intervalo'] + random.uniform(0, 0.5))
    
    return False, "agotado"

def ejecutar_envio(numeros, config):
    try:
        total = len(numeros)
        logger.info(f"🚀 INICIANDO ENVÍO: {total} números")
        send_telegram_message(f"🚀 <b>INICIANDO</b>\n📱 Total: {total}\n⏰ {datetime.now().strftime('%H:%M:%S')}")
        
        # Obtener proxies iniciales
        proxy_list = get_proxies(MAX_PROXIES)
        working_proxies = get_working_proxies(proxy_list) if proxy_list else []
        
        if not working_proxies:
            logger.error("❌ No hay proxies funcionales")
            send_telegram_message("❌ <b>ERROR</b>\nNo hay proxies funcionales")
            return {"total": total, "enviados": 0, "fallidos": total, "blacklist": 0}
        
        logger.info(f"✅ {len(working_proxies)} proxies funcionales")
        send_telegram_message(f"✅ <b>PROXIES</b>\n{len(working_proxies)} funcionales")
        
        proxy_blacklist = load_json(BLACKLIST_FILE)
        numbers_blacklist = load_json(NUMBERS_BLACKLIST_FILE)
        
        stats = {"total": total, "enviados": 0, "fallidos": 0, "blacklist": 0}
        sin_proxies = 0
        
        for i, numero in enumerate(numeros, 1):
            logger.info(f"▶ [{i}/{total}] +{config['pais']} {numero}")
            
            success, result = process_number(numero, config, working_proxies, proxy_blacklist, numbers_blacklist)
            
            if success:
                stats['enviados'] += 1
                send_telegram_result(numero, True, result, None, 1, config['intentos'])
            elif result == "blacklist" or result == "limite_blacklist":
                stats['blacklist'] += 1
            else:
                stats['fallidos'] += 1
            
            # Mostrar progreso cada 5 números
            if i % 5 == 0:
                logger.info(f"📊 Progreso: {i}/{total} | ✅ {stats['enviados']} | ❌ {stats['fallidos']} | 🔌 {len(working_proxies)}")
            
            # Si no hay proxies, esperar y recargar
            if not working_proxies:
                sin_proxies += 1
                if sin_proxies > 3:
                    logger.error("❌ Sin proxies después de 3 intentos, deteniendo")
                    send_telegram_message("❌ <b>DETENIDO</b>\nSin proxies disponibles")
                    break
                logger.info("🔄 Recargando proxies...")
                proxy_list = get_proxies(MAX_PROXIES)
                if proxy_list:
                    working_proxies = get_working_proxies(proxy_list)
                    logger.info(f"✅ Recargados {len(working_proxies)} proxies")
                time.sleep(5)
                continue
            
            if i < total:
                time.sleep(config['intervalo'] * 0.5 + random.uniform(0, 1))
        
        resumen = (
            f"📊 <b>RESUMEN</b>\n"
            f"📱 Total: {stats['total']}\n"
            f"✅ Enviados: {stats['enviados']}\n"
            f"❌ Fallidos: {stats['fallidos']}\n"
            f"⏭️ Blacklist: {stats['blacklist']}\n"
            f"🔌 Proxies: {len(working_proxies)}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        
        logger.info(resumen)
        send_telegram_message(resumen)
        
        stats['fecha'] = datetime.now().isoformat()
        save_json("sms_stats.json", stats)
        
        return stats
    except Exception as e:
        logger.error(f"Error: {e}")
        send_telegram_message(f"❌ <b>ERROR</b>\n{str(e)}")
        raise

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SMS Pro</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height:100vh; padding:20px; }
        .container { max-width:1200px; margin:0 auto; }
        .header { background:rgba(255,255,255,0.95); border-radius:20px; padding:30px; margin-bottom:30px; text-align:center; }
        .header h1 { color:#333; font-size:2.5em; }
        .header h1 span { background:linear-gradient(135deg, #667eea, #764ba2); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
        .grid { display:grid; grid-template-columns:1fr 1fr; gap:30px; margin-bottom:30px; }
        @media (max-width:768px) { .grid { grid-template-columns:1fr; } }
        .card { background:rgba(255,255,255,0.95); border-radius:20px; padding:25px; }
        .card h2 { color:#333; margin-bottom:20px; border-bottom:3px solid #667eea; padding-bottom:10px; }
        .form-group { margin-bottom:15px; }
        .form-group label { display:block; color:#555; font-weight:600; margin-bottom:5px; font-size:0.9em; }
        .form-group input, .form-group textarea { width:100%; padding:12px 15px; border:2px solid #e1e1e1; border-radius:10px; font-size:1em; font-family:inherit; }
        .form-group input:focus, .form-group textarea:focus { outline:none; border-color:#667eea; }
        .form-group textarea { min-height:80px; resize:vertical; }
        .form-row { display:grid; grid-template-columns:1fr 1fr; gap:15px; }
        .btn { padding:12px 30px; border:none; border-radius:10px; font-size:1em; font-weight:600; cursor:pointer; width:100%; transition:0.3s; }
        .btn-primary { background:linear-gradient(135deg, #667eea, #764ba2); color:white; }
        .btn-primary:hover { transform:translateY(-2px); }
        .btn-success { background:#00b894; color:white; }
        .btn-warning { background:#fdcb6e; color:#333; }
        .btn-danger { background:#ff6b6b; color:white; }
        .stats { display:grid; grid-template-columns:repeat(4,1fr); gap:15px; margin-top:20px; }
        .stat-item { background:rgba(255,255,255,0.95); border-radius:15px; padding:20px; text-align:center; }
        .stat-item .number { font-size:2em; font-weight:bold; color:#667eea; }
        .stat-item .label { color:#666; font-size:0.9em; margin-top:5px; }
        .stat-item.success .number { color:#00b894; }
        .stat-item.danger .number { color:#ff6b6b; }
        .stat-item.warning .number { color:#fdcb6e; }
        .mode-selector { display:flex; gap:10px; margin-bottom:15px; }
        .mode-btn { flex:1; padding:10px; border:2px solid #e1e1e1; border-radius:10px; background:white; cursor:pointer; text-align:center; font-weight:600; }
        .mode-btn.active { border-color:#667eea; background:#f0f4ff; color:#667eea; }
        .hidden { display:none; }
        .log-container { background:#1e1e1e; color:#d4d4d4; border-radius:10px; padding:15px; max-height:300px; overflow-y:auto; font-family:monospace; font-size:0.9em; margin-top:10px; }
        .log-entry { padding:2px 0; border-bottom:1px solid #2d2d2d; }
        .log-entry .time { color:#858585; margin-right:10px; }
        .log-entry .level-info { color:#4fc3f7; }
        .log-entry .level-success { color:#81c784; }
        .log-entry .level-warning { color:#ffb74d; }
        .log-entry .level-error { color:#ff6b6b; }
        .loading { display:none; text-align:center; padding:20px; }
        .spinner { border:4px solid #f3f3f3; border-top:4px solid #667eea; border-radius:50%; width:40px; height:40px; animation:spin 1s linear infinite; margin:0 auto; }
        @keyframes spin { 0% { transform:rotate(0deg); } 100% { transform:rotate(360deg); } }
        .badge { display:inline-block; padding:3px 10px; border-radius:20px; font-size:0.75em; font-weight:600; }
        .badge-success { background:#00b894; color:white; }
        .badge-warning { background:#fdcb6e; color:#333; }
        .badge-info { background:#4fc3f7; color:#333; }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>📱 <span>SMS Pro</span></h1>
        <p>Envío masivo de SMS con Textbelt y Proxies</p>
        <div style="margin-top:10px;">
            <span class="badge badge-success">● Activo</span>
            <span class="badge badge-warning">🔑 {{ api_keys_count }} claves</span>
            <span class="badge badge-info">🌐 {{ proxies_count }} proxies</span>
        </div>
    </div>

    <div class="stats" id="stats">
        <div class="stat-item"><div class="number" id="total">0</div><div class="label">Total</div></div>
        <div class="stat-item success"><div class="number" id="enviados">0</div><div class="label">✅ Enviados</div></div>
        <div class="stat-item danger"><div class="number" id="fallidos">0</div><div class="label">❌ Fallidos</div></div>
        <div class="stat-item warning"><div class="number" id="blacklist">0</div><div class="label">⏭️ Blacklist</div></div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>✉️ Enviar SMS</h2>
            <div class="mode-selector">
                <button class="mode-btn active" onclick="switchMode('lista')" id="mode-lista">📋 Lista</button>
                <button class="mode-btn" onclick="switchMode('rango')" id="mode-rango">📊 Rango</button>
            </div>
            <div id="modo-lista">
                <div class="form-group">
                    <label>📱 Números (uno por línea)</label>
                    <textarea id="numeros-lista" rows="5" placeholder="59642359&#10;55721087">59642359&#10;55721087</textarea>
                </div>
            </div>
            <div id="modo-rango" class="hidden">
                <div class="form-row">
                    <div class="form-group"><label>Inicio</label><input type="text" id="rango-inicio" value="59545678"></div>
                    <div class="form-group"><label>Fin</label><input type="text" id="rango-fin" value="59545700"></div>
                </div>
                <div class="form-group"><label>Total</label><input type="text" id="rango-total" readonly style="background:#f5f5f5;"></div>
            </div>
            <div class="form-group"><label>📝 Mensaje</label><textarea id="mensaje" rows="2">{{ mensaje }}</textarea></div>
            <div class="form-row">
                <div class="form-group"><label>🌍 País</label><input type="text" id="pais" value="{{ pais }}"></div>
                <div class="form-group"><label>🔄 Intentos</label><input type="number" id="intentos" value="{{ intentos }}" min="1" max="10"></div>
            </div>
            <button class="btn btn-primary" onclick="enviar()">🚀 Enviar SMS</button>
            <div class="loading" id="loading"><div class="spinner"></div><p style="margin-top:10px;color:#666;">Enviando...</p></div>
        </div>

        <div class="card">
            <h2>⚙️ Control</h2>
            <div class="form-group"><label>🔑 Claves API</label><input type="text" id="api-keys" value="{{ api_keys|join(', ') }}"></div>
            <div class="form-row">
                <button class="btn btn-success" onclick="actualizarClaves()">💾 Guardar</button>
                <button class="btn btn-warning" onclick="recargarProxies()">🌐 Recargar</button>
            </div>
            <div style="margin-top:15px;"><button class="btn btn-danger" onclick="limpiarBlacklist()">🗑️ Limpiar Blacklist</button></div>
            <div style="margin-top:20px;">
                <h3 style="color:#555;font-size:1em;margin-bottom:10px;">📊 Estado</h3>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
                    <div style="background:#f5f5f5;padding:10px;border-radius:10px;">
                        <small style="color:#888;">Proxies</small>
                        <div style="font-size:1.5em;font-weight:bold;color:#667eea;" id="proxies-activos">{{ proxies_count }}</div>
                    </div>
                    <div style="background:#f5f5f5;padding:10px;border-radius:10px;">
                        <small style="color:#888;">Blacklist</small>
                        <div style="font-size:1.5em;font-weight:bold;color:#ff6b6b;" id="blacklist-count">{{ blacklist_count }}</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>📋 Logs</h2>
        <div class="log-container" id="logs">
            <div class="log-entry"><span class="time">[Sistema]</span><span class="level-info">Servidor iniciado</span></div>
        </div>
    </div>
</div>

<script>
let modoActual = 'lista';
let enviando = false;

function switchMode(mode) {
    modoActual = mode;
    document.getElementById('modo-lista').classList.toggle('hidden', mode !== 'lista');
    document.getElementById('modo-rango').classList.toggle('hidden', mode !== 'rango');
    document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('mode-' + mode).classList.add('active');
    if (mode === 'rango') calcularRango();
}

function calcularRango() {
    const inicio = document.getElementById('rango-inicio').value;
    const fin = document.getElementById('rango-fin').value;
    if (inicio && fin) {
        try {
            const total = parseInt(fin) - parseInt(inicio) + 1;
            document.getElementById('rango-total').value = total > 0 ? total + ' números' : 'Rango inválido';
        } catch { document.getElementById('rango-total').value = 'Error'; }
    }
}

document.getElementById('rango-inicio').addEventListener('input', calcularRango);
document.getElementById('rango-fin').addEventListener('input', calcularRango);

function enviar() {
    if (enviando) { alert('Ya hay un envío en progreso'); return; }
    let numeros = [];
    if (modoActual === 'lista') {
        numeros = document.getElementById('numeros-lista').value.split('\\n').map(n => n.trim()).filter(n => n);
        if (numeros.length === 0) { alert('Ingresa al menos un número'); return; }
    } else {
        const inicio = document.getElementById('rango-inicio').value.trim();
        const fin = document.getElementById('rango-fin').value.trim();
        if (!inicio || !fin) { alert('Ingresa inicio y fin'); return; }
        numeros = ['RANGO:' + inicio + ':' + fin];
    }

    const data = {
        modo: modoActual,
        numeros: numeros,
        pais: document.getElementById('pais').value || '53',
        mensaje: document.getElementById('mensaje').value,
        intentos: parseInt(document.getElementById('intentos').value) || 3
    };

    enviando = true;
    document.getElementById('loading').style.display = 'block';
    document.querySelector('.btn-primary').disabled = true;

    fetch('/api/enviar', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'aceptado') {
            agregarLog('success', '✅ ' + data.mensaje);
            document.getElementById('total').textContent = data.numeros || '...';
        } else {
            agregarLog('error', '❌ Error: ' + (data.error || 'Desconocido'));
        }
    })
    .catch(e => agregarLog('error', '❌ Error: ' + e.message))
    .finally(() => {
        enviando = false;
        document.getElementById('loading').style.display = 'none';
        document.querySelector('.btn-primary').disabled = false;
        actualizarStats();
    });
}

function actualizarClaves() {
    const keys = document.getElementById('api-keys').value.split(',').map(k => k.trim()).filter(k => k);
    fetch('/api/claves', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({claves: keys})
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'ok') agregarLog('success', '✅ ' + data.mensaje);
        else agregarLog('error', '❌ Error: ' + data.error);
    })
    .catch(e => agregarLog('error', '❌ Error: ' + e.message));
}

function recargarProxies() {
    agregarLog('info', '🔄 Recargando proxies...');
    fetch('/api/proxies/recargar', { method: 'POST' })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'ok') {
            agregarLog('success', '✅ ' + data.mensaje);
            document.getElementById('proxies-activos').textContent = data.proxies || '0';
        } else agregarLog('error', '❌ Error: ' + data.error);
    })
    .catch(e => agregarLog('error', '❌ Error: ' + e.message));
}

function limpiarBlacklist() {
    if (!confirm('¿Eliminar toda la blacklist?')) return;
    fetch('/api/blacklist/limpiar', { method: 'DELETE' })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'ok') {
            agregarLog('success', '✅ ' + data.mensaje);
            document.getElementById('blacklist-count').textContent = '0';
        } else agregarLog('error', '❌ Error: ' + data.error);
    })
    .catch(e => agregarLog('error', '❌ Error: ' + e.message));
}

function actualizarStats() {
    fetch('/api/stats')
    .then(r => r.json())
    .then(data => {
        if (data.status === 'ok') {
            document.getElementById('total').textContent = data.total || '0';
            document.getElementById('enviados').textContent = data.enviados || '0';
            document.getElementById('fallidos').textContent = data.fallidos || '0';
            document.getElementById('blacklist').textContent = data.blacklist || '0';
        }
    })
    .catch(() => {});
}

function agregarLog(tipo, mensaje) {
    const container = document.getElementById('logs');
    const time = new Date().toLocaleTimeString();
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = '<span class="time">[' + time + ']</span><span class="level-' + tipo + '">' + mensaje + '</span>';
    container.appendChild(entry);
    container.scrollTop = container.scrollHeight;
    while (container.children.length > 100) container.removeChild(container.firstChild);
}

function pollLogs() {
    fetch('/api/logs')
    .then(r => r.json())
    .then(data => {
        if (data.status === 'ok' && data.logs) {
            data.logs.forEach(log => {
                const tipo = log.includes('✅') ? 'success' : log.includes('❌') ? 'error' : log.includes('⚠️') ? 'warning' : 'info';
                agregarLog(tipo, log);
            });
        }
    })
    .catch(() => {});
}

setInterval(actualizarStats, 5000);
setInterval(pollLogs, 3000);

document.addEventListener('DOMContentLoaded', function() {
    actualizarStats();
    calcularRango();
    agregarLog('info', '🚀 Panel iniciado');
});
</script>
</body>
</html>
'''

@app.route('/')
def index():
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
        
        thread = threading.Thread(target=ejecutar_envio, args=(numeros, config))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "status": "aceptado",
            "mensaje": f"Enviando a {len(numeros)} números",
            "numeros": len(numeros)
        }), 202
        
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/claves', methods=['POST'])
def api_claves():
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
        
        return jsonify({"status": "ok", "mensaje": f"{len(API_KEYS)} claves guardadas"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/proxies/recargar', methods=['POST'])
def api_recargar_proxies():
    try:
        proxies = get_proxies(MAX_PROXIES)
        working = get_working_proxies(proxies)
        return jsonify({"status": "ok", "mensaje": f"{len(working)} proxies", "proxies": len(working)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/blacklist/limpiar', methods=['DELETE'])
def api_limpiar_blacklist():
    try:
        save_json(BLACKLIST_FILE, {})
        save_json(NUMBERS_BLACKLIST_FILE, {})
        return jsonify({"status": "ok", "mensaje": "Blacklist limpiada"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/stats', methods=['GET'])
def api_stats():
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
    try:
        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()
            logs = [line.strip() for line in lines[-20:] if line.strip()]
            return jsonify({"status": "ok", "logs": logs})
    except:
        return jsonify({"status": "ok", "logs": []})

