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
MENSAJE = "Cubanos, el momento es ahora. La libertad no se pide, se conquista. Cada día que callan es un día que ganan los opresores. Levántense, unan sus voces, tomen las calles. El mundo los respalda y no los abandonaremos. ¡Por una Cuba libre, ahora y siempre!"
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
    if not TELEGRAM_ENABLED:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message[:4096], "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=5)
    except:
        pass

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

def process_number(numero, config, working_proxies, proxy_blacklist, numbers_blacklist):
    phone = '+' + config['pais'] + numero
    message = config['mensaje']
    max_intentos = config['intentos']
    
    if is_blacklisted(numero, numbers_blacklist):
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
        
        success, text_id, error = send_sms(phone, message, api_key, proxy)
        
        if error in ["TIMEOUT", "CONNECTION_ERROR"] or "ConnectionError" in str(error):
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
        
        if success:
            if proxy:
                add_blacklist(proxy, proxy_blacklist, BLACKLIST_FILE)
                if proxy in working_proxies:
                    working_proxies.remove(proxy)
            return True, text_id
        
        if error and any(p in error.lower() for p in ["only one", "limit", "quota"]):
            intentos_limite += 1
            intentos_reales += 1
            
            if intentos_limite >= MAX_INTENTOS_LIMITE:
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
        
        intentos_reales += 1
        if intentos_reales < max_intentos:
            time.sleep(config['intervalo'] + random.uniform(0, 1))
    
    return False, "agotado"

def ejecutar_envio(numeros, config):
    try:
        logger.info(f"🚀 INICIANDO ENVÍO: {len(numeros)} números")
        send_telegram_message(f"🚀 <b>INICIANDO ENVÍO</b>\n📱 Números: {len(numeros)}\n📝 Mensaje: {config['mensaje'][:50]}...")
        
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
            success, result = process_number(numero, config, working_proxies, proxy_blacklist, numbers_blacklist)
            
            if success:
                stats['enviados'] += 1
            elif result == "blacklist":
                stats['blacklist'] += 1
            elif result == "limite_blacklist":
                stats['limite_blacklist'] += 1
                stats['blacklist'] += 1
            else:
                stats['fallidos'] += 1
            
            if i % 10 == 0:
                logger.info(f"📊 Progreso: {i}/{stats['total']} | ✅ {stats['enviados']}")
            
            if i < stats['total']:
                time.sleep(config['intervalo'] * 0.5 + random.uniform(0, 2))
        
        resumen = (
            f"📊 <b>RESUMEN FINAL</b>\n"
            f"📱 Total: {stats['total']}\n"
            f"✅ Enviados: {stats['enviados']}\n"
            f"❌ Fallidos: {stats['fallidos']}\n"
            f"⏭️ Blacklist: {stats['blacklist']}"
        )
        if stats['limite_blacklist'] > 0:
            resumen += f"\n🚫 Blacklist límite: {stats['limite_blacklist']}"
        
        logger.info(resumen)
        send_telegram_message(resumen)
        
        stats['fecha'] = datetime.now().isoformat()
        save_json("sms_stats.json", stats)
        
        return stats
    except Exception as e:
        logger.error(f"Error en ejecución: {e}")
        send_telegram_message(f"❌ <b>ERROR</b>\n{str(e)}")
        raise

# ==================== HTML TEMPLATE ====================
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SMS Pro - Envío Masivo</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        .header {
            background: rgba(255,255,255,0.95);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            text-align: center;
        }
        .header h1 {
            color: #333;
            font-size: 2.5em;
            margin-bottom: 5px;
        }
        .header h1 span {
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .header p {
            color: #666;
            font-size: 1.1em;
        }
        .grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 30px;
            margin-bottom: 30px;
        }
        @media (max-width: 768px) {
            .grid {
                grid-template-columns: 1fr;
            }
        }
        .card {
            background: rgba(255,255,255,0.95);
            border-radius: 20px;
            padding: 25px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            transition: transform 0.3s;
        }
        .card:hover {
            transform: translateY(-5px);
        }
        .card h2 {
            color: #333;
            font-size: 1.5em;
            margin-bottom: 20px;
            border-bottom: 3px solid #667eea;
            padding-bottom: 10px;
        }
        .card h2 i {
            margin-right: 10px;
        }
        .form-group {
            margin-bottom: 15px;
        }
        .form-group label {
            display: block;
            color: #555;
            font-weight: 600;
            margin-bottom: 5px;
            font-size: 0.9em;
        }
        .form-group input, .form-group textarea, .form-group select {
            width: 100%;
            padding: 12px 15px;
            border: 2px solid #e1e1e1;
            border-radius: 10px;
            font-size: 1em;
            transition: border 0.3s;
            font-family: inherit;
        }
        .form-group input:focus, .form-group textarea:focus, .form-group select:focus {
            outline: none;
            border-color: #667eea;
        }
        .form-group textarea {
            min-height: 80px;
            resize: vertical;
        }
        .form-row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        .btn {
            padding: 12px 30px;
            border: none;
            border-radius: 10px;
            font-size: 1em;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            width: 100%;
        }
        .btn-primary {
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
        }
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 20px rgba(102, 126, 234, 0.4);
        }
        .btn-danger {
            background: #ff6b6b;
            color: white;
        }
        .btn-danger:hover {
            background: #ee5a24;
            transform: translateY(-2px);
        }
        .btn-success {
            background: #00b894;
            color: white;
        }
        .btn-success:hover {
            background: #00a884;
            transform: translateY(-2px);
        }
        .btn-warning {
            background: #fdcb6e;
            color: #333;
        }
        .btn-warning:hover {
            background: #fdcb6e;
            transform: translateY(-2px);
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 15px;
            margin-top: 20px;
        }
        .stat-item {
            background: rgba(255,255,255,0.95);
            border-radius: 15px;
            padding: 20px;
            text-align: center;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
        }
        .stat-item .number {
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
        }
        .stat-item .label {
            color: #666;
            font-size: 0.9em;
            margin-top: 5px;
        }
        .stat-item.success .number { color: #00b894; }
        .stat-item.danger .number { color: #ff6b6b; }
        .stat-item.warning .number { color: #fdcb6e; }
        .alert {
            padding: 15px 20px;
            border-radius: 10px;
            margin-bottom: 15px;
        }
        .alert-success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .alert-danger {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .alert-info {
            background: #d1ecf1;
            color: #0c5460;
            border: 1px solid #bee5eb;
        }
        .log-container {
            background: #1e1e1e;
            color: #d4d4d4;
            border-radius: 10px;
            padding: 15px;
            max-height: 300px;
            overflow-y: auto;
            font-family: 'Consolas', monospace;
            font-size: 0.9em;
            margin-top: 10px;
        }
        .log-entry {
            padding: 2px 0;
            border-bottom: 1px solid #2d2d2d;
        }
        .log-entry .time {
            color: #858585;
            margin-right: 10px;
        }
        .log-entry .level-info { color: #4fc3f7; }
        .log-entry .level-success { color: #81c784; }
        .log-entry .level-warning { color: #ffb74d; }
        .log-entry .level-error { color: #ff6b6b; }
        .loading {
            display: none;
            text-align: center;
            padding: 20px;
        }
        .spinner {
            border: 4px solid #f3f3f3;
            border-top: 4px solid #667eea;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 0 auto;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .mode-selector {
            display: flex;
            gap: 10px;
            margin-bottom: 15px;
        }
        .mode-btn {
            flex: 1;
            padding: 10px;
            border: 2px solid #e1e1e1;
            border-radius: 10px;
            background: white;
            cursor: pointer;
            transition: all 0.3s;
            text-align: center;
            font-weight: 600;
        }
        .mode-btn.active {
            border-color: #667eea;
            background: #f0f4ff;
            color: #667eea;
        }
        .mode-btn:hover {
            border-color: #667eea;
        }
        .hidden {
            display: none;
        }
        .badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 20px;
            font-size: 0.75em;
            font-weight: 600;
        }
        .badge-success { background: #00b894; color: white; }
        .badge-danger { background: #ff6b6b; color: white; }
        .badge-warning { background: #fdcb6e; color: #333; }
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <h1>📱 <span>SMS Pro</span></h1>
            <p>Envío masivo de SMS con Textbelt y Proxies</p>
            <div style="margin-top: 10px;">
                <span class="badge badge-success">● Activo</span>
                <span class="badge badge-warning">🔑 {{ api_keys_count }} claves</span>
                <span class="badge badge-info">🌐 {{ proxies_count }} proxies</span>
            </div>
        </div>

        <!-- Stats -->
        <div class="stats" id="stats">
            <div class="stat-item">
                <div class="number" id="total">0</div>
                <div class="label">Total Números</div>
            </div>
            <div class="stat-item success">
                <div class="number" id="enviados">0</div>
                <div class="label">✅ Enviados</div>
            </div>
            <div class="stat-item danger">
                <div class="number" id="fallidos">0</div>
                <div class="label">❌ Fallidos</div>
            </div>
            <div class="stat-item warning">
                <div class="number" id="blacklist">0</div>
                <div class="label">⏭️ Blacklist</div>
            </div>
        </div>

        <!-- Main Grid -->
        <div class="grid">
            <!-- Panel de Envío -->
            <div class="card">
                <h2>✉️ Enviar SMS</h2>
                
                <div class="mode-selector">
                    <button class="mode-btn active" onclick="switchMode('lista')" id="mode-lista">📋 Lista</button>
                    <button class="mode-btn" onclick="switchMode('rango')" id="mode-rango">📊 Rango</button>
                </div>

                <!-- Modo Lista -->
                <div id="modo-lista">
                    <div class="form-group">
                        <label>📱 Números (uno por línea)</label>
                        <textarea id="numeros-lista" rows="5" placeholder="59642359&#10;55721087&#10;59042427">59642359&#10;55721087&#10;59042427</textarea>
                    </div>
                </div>

                <!-- Modo Rango -->
                <div id="modo-rango" class="hidden">
                    <div class="form-row">
                        <div class="form-group">
                            <label>📌 Número de inicio</label>
                            <input type="text" id="rango-inicio" value="59545678" placeholder="ej: 59545678">
                        </div>
                        <div class="form-group">
                            <label>📌 Número de fin</label>
                            <input type="text" id="rango-fin" value="59545700" placeholder="ej: 59999999">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>📊 Total a generar</label>
                        <input type="text" id="rango-total" readonly style="background: #f5f5f5;">
                    </div>
                </div>

                <div class="form-group">
                    <label>📝 Mensaje</label>
                    <textarea id="mensaje" rows="2">{{ mensaje }}</textarea>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>🌍 País</label>
                        <input type="text" id="pais" value="{{ pais }}">
                    </div>
                    <div class="form-group">
                        <label>🔄 Intentos</label>
                        <input type="number" id="intentos" value="{{ intentos }}" min="1" max="10">
                    </div>
                </div>

                <button class="btn btn-primary" onclick="enviar()">🚀 Enviar SMS</button>
                <div class="loading" id="loading">
                    <div class="spinner"></div>
                    <p style="margin-top: 10px; color: #666;">Enviando mensajes...</p>
                </div>
            </div>

            <!-- Panel de Control -->
            <div class="card">
                <h2>⚙️ Control</h2>
                
                <div class="form-group">
                    <label>🔑 Claves API (separadas por coma)</label>
                    <input type="text" id="api-keys" value="{{ api_keys|join(', ') }}">
                </div>

                <div class="form-row">
                    <button class="btn btn-success" onclick="actualizarClaves()">💾 Guardar Claves</button>
                    <button class="btn btn-warning" onclick="recargarProxies()">🌐 Recargar Proxies</button>
                </div>

                <div style="margin-top: 15px;">
                    <button class="btn btn-danger" onclick="limpiarBlacklist()">🗑️ Limpiar Blacklist</button>
                </div>

                <div style="margin-top: 20px;">
                    <h3 style="color: #555; font-size: 1em; margin-bottom: 10px;">📊 Estado</h3>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                        <div style="background: #f5f5f5; padding: 10px; border-radius: 10px;">
                            <small style="color: #888;">Proxies activos</small>
                            <div style="font-size: 1.5em; font-weight: bold; color: #667eea;" id="proxies-activos">{{ proxies_count }}</div>
                        </div>
                        <div style="background: #f5f5f5; padding: 10px; border-radius: 10px;">
                            <small style="color: #888;">En blacklist</small>
                            <div style="font-size: 1.5em; font-weight: bold; color: #ff6b6b;" id="blacklist-count">{{ blacklist_count }}</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Logs -->
        <div class="card">
            <h2>📋 Logs en tiempo real</h2>
            <div class="log-container" id="logs">
                <div class="log-entry">
                    <span class="time">[Sistema]</span>
                    <span class="level-info">Servidor iniciado correctamente</span>
                </div>
            </div>
        </div>
    </div>

    <script>
        // ==================== Variables Globales ====================
        let modoActual = 'lista';
        let enviando = false;

        // ==================== Modos ====================
        function switchMode(mode) {
            modoActual = mode;
            document.getElementById('modo-lista').classList.toggle('hidden', mode !== 'lista');
            document.getElementById('modo-rango').classList.toggle('hidden', mode !== 'rango');
            document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
            document.getElementById(`mode-${mode}`).classList.add('active');
            if (mode === 'rango') calcularRango();
        }

        // ==================== Calcular Rango ====================
        function calcularRango() {
            const inicio = document.getElementById('rango-inicio').value;
            const fin = document.getElementById('rango-fin').value;
            if (inicio && fin) {
                try {
                    const total = parseInt(fin) - parseInt(inicio) + 1;
                    document.getElementById('rango-total').value = total > 0 ? `${total} números` : 'Rango inválido';
                } catch {
                    document.getElementById('rango-total').value = 'Error';
                }
            }
        }

        document.getElementById('rango-inicio').addEventListener('input', calcularRango);
        document.getElementById('rango-fin').addEventListener('input', calcularRango);

        // ==================== Enviar SMS ====================
        function enviar() {
            if (enviando) {
                alert('Ya hay un envío en progreso');
                return;
            }

            let numeros = [];
            let modo = modoActual;

            if (modo === 'lista') {
                const textarea = document.getElementById('numeros-lista');
                numeros = textarea.value.split('\\n').map(n => n.trim()).filter(n => n);
                if (numeros.length === 0) {
                    alert('Ingresa al menos un número');
                    return;
                }
            } else {
                const inicio = document.getElementById('rango-inicio').value.trim();
                const fin = document.getElementById('rango-fin').value.trim();
                if (!inicio || !fin) {
                    alert('Ingresa inicio y fin del rango');
                    return;
                }
                numeros = ['RANGO:' + inicio + ':' + fin];
            }

            const data = {
                modo: modo,
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
            .then(response => response.json())
            .then(data => {
                if (data.status === 'aceptado') {
                    agregarLog('success', `✅ ${data.mensaje}`);
                    // Actualizar stats
                    document.getElementById('total').textContent = data.numeros || '...';
                } else {
                    agregarLog('error', `❌ Error: ${data.error || 'Desconocido'}`);
                }
            })
            .catch(error => {
                agregarLog('error', `❌ Error: ${error.message}`);
            })
            .finally(() => {
                enviando = false;
                document.getElementById('loading').style.display = 'none';
                document.querySelector('.btn-primary').disabled = false;
                actualizarStats();
            });
        }

        // ==================== Actualizar Claves ====================
        function actualizarClaves() {
            const keys = document.getElementById('api-keys').value.split(',').map(k => k.trim()).filter(k => k);
            fetch('/api/claves', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({claves: keys})
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'ok') {
                    agregarLog('success', `✅ ${data.mensaje}`);
                } else {
                    agregarLog('error', `❌ Error: ${data.error}`);
                }
            })
            .catch(error => {
                agregarLog('error', `❌ Error: ${error.message}`);
            });
        }

        // ==================== Recargar Proxies ====================
        function recargarProxies() {
            agregarLog('info', '🔄 Recargando proxies...');
            fetch('/api/proxies/recargar', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'ok') {
                    agregarLog('success', `✅ ${data.mensaje}`);
                    document.getElementById('proxies-activos').textContent = data.proxies || '0';
                } else {
                    agregarLog('error', `❌ Error: ${data.error}`);
                }
            })
            .catch(error => {
                agregarLog('error', `❌ Error: ${error.message}`);
            });
        }

        // ==================== Limpiar Blacklist ====================
        function limpiarBlacklist() {
            if (!confirm('¿Eliminar toda la blacklist?')) return;
            
            fetch('/api/blacklist/limpiar', {
                method: 'DELETE'
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'ok') {
                    agregarLog('success', `✅ ${data.mensaje}`);
                    document.getElementById('blacklist-count').textContent = '0';
                } else {
                    agregarLog('error', `❌ Error: ${data.error}`);
                }
            })
            .catch(error => {
                agregarLog('error', `❌ Error: ${error.message}`);
            });
        }

        // ==================== Actualizar Stats ====================
        function actualizarStats() {
            fetch('/api/stats')
            .then(response => response.json())
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

        // ==================== Logs ====================
        function agregarLog(tipo, mensaje) {
            const logContainer = document.getElementById('logs');
            const time = new Date().toLocaleTimeString();
            const entry = document.createElement('div');
            entry.className = 'log-entry';
            const levelClass = `level-${tipo}`;
            entry.innerHTML = `<span class="time">[${time}]</span><span class="${levelClass}">${mensaje}</span>`;
            logContainer.appendChild(entry);
            logContainer.scrollTop = logContainer.scrollHeight;
            
            // Mantener solo últimos 100 logs
            while (logContainer.children.length > 100) {
                logContainer.removeChild(logContainer.firstChild);
            }
        }

        // ==================== Polling de Logs ====================
        function pollLogs() {
            fetch('/api/logs')
            .then(response => response.json())
            .then(data => {
                if (data.status === 'ok' && data.logs) {
                    data.logs.forEach(log => {
                        const tipo = log.includes('✅') ? 'success' : 
                                   log.includes('❌') || log.includes('Error') ? 'error' :
                                   log.includes('⚠️') ? 'warning' : 'info';
                        agregarLog(tipo, log);
                    });
                }
            })
            .catch(() => {});
        }

        // ==================== Polling de Stats ====================
        setInterval(actualizarStats, 5000);
        setInterval(pollLogs, 3000);

        // Inicializar
        document.addEventListener('DOMContentLoaded', function() {
            actualizarStats();
            calcularRango();
            agregarLog('info', '🚀 Panel de control iniciado');
        });
    </script>
</body>
</html>
'''

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
        proxies_count=0,  # Se actualizará dinámicamente
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
            # Modo rango
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
        
        # Guardar en archivo
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
            # Últimas 20 líneas
            logs = [line.strip() for line in lines[-20:] if line.strip()]
            return jsonify({"status": "ok", "logs": logs})
    except:
        return jsonify({"status": "ok", "logs": []})

# ==================== MAIN ====================
if __name__ == '__main__':
    # Modo desarrollo
    logger.info("🚀 SMS Pro iniciado en modo desarrollo")
    app.run(host='0.0.0.0', port=5000, debug=True)
else:
    # Modo producción (Gunicorn)
    logger.info("🚀 SMS Pro iniciado en modo producción")
    send_telegram_message("🚀 <b>SMS Pro iniciado</b>\nServidor en ejecución")
