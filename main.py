# main.py — v1_zero_final_blindado
# ---------------------------------------------
# Variáveis de ambiente exigidas no Render:
# TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, (PORT é fornecida pelo Render)
#
# Requisitos (requirements.txt):
# Flask==3.0.3
# requests==2.31.0
#
# Observação: sem numpy/pandas/aiohttp. Tudo síncrono, robusto, estável.

import os
import time
import math
import threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import requests
from flask import Flask

# ==========================
# CONFIG
# ==========================
BINANCE = "https://api.binance.com"
INTERVAL_5M = "5m"
INTERVAL_15M = "15m"
K_LIMIT = 300

TOP_N = 50                    # monitorar as 50 maiores por volume
TOP_REFRESH_SEC = 3600        # refazer Top 50 a cada 1h
SCAN_SLEEP = 300              # varredura a cada 5m (compatível com 5m)
COOLDOWN_SEC = 15 * 60        # 15 min por par + tipo de alerta
MAX_WORKERS = 50              # limite técnico de threads

EXCLUDE_KEYWORDS = ("UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "4L", "4S", "5L", "5S", "1000")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BR_TZ = timezone(timedelta(hours=-3))

# Estado
cooldowns = defaultdict(dict)     # cooldowns[symbol][alert_key] = last_ts
current_top = []                  # lista dos TOP_N por volume (SPOT/USDT válidos)
last_top_update = 0

# Flask (healthcheck Render)
app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

# ==========================
# UTILITÁRIOS
# ==========================
def now_br_str():
    return datetime.now(BR_TZ).strftime("%Y-%m-%d %H:%M")

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("AVISO: TELEGRAM_TOKEN/CHAT_ID não configurados. Mensagem não enviada.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True  # remove banner do link
        }
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print("Erro Telegram:", e)

def fetch_json(url, params=None, timeout=15):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("fetch_json erro:", e)
    return None

def get_klines(symbol, interval, limit=K_LIMIT):
    data = fetch_json(f"{BINANCE}/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        return [], []
    closes = [float(x[4]) for x in data]
    vols = [float(x[5]) for x in data]
    return closes, vols

def sma(values, period):
    out, s, q = [], 0.0, []
    for v in values:
        q.append(v); s += v
        if len(q) > period:
            s -= q.pop(0)
        out.append(s / period if len(q) == period else None)
    return out

def ema(values, period):
    out = []
    k = 2 / (period + 1)
    ema_val = None
    for v in values:
        ema_val = v if ema_val is None else v * k + ema_val * (1 - k)
        out.append(ema_val)
    return out

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(diff, 0.0))
        losses.append(-min(diff, 0.0))
    # RMA (Wilder)
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - (100 / (1 + rs))

def is_leveraged_or_bad(symbol: str, base_asset: str) -> bool:
    s = symbol.upper()
    b = base_asset.upper()
    if s.endswith("USD") or b.endswith("USD"):
        return True
    for kw in EXCLUDE_KEYWORDS:
        if kw in s:
            return True
    return False

def get_valid_spot_usdt():
    """Somente SPOT/USDT válidos (sem UP/DOWN/BULL/BEAR, sem '...USDUSDT')."""
    info = fetch_json(f"{BINANCE}/api/v3/exchangeInfo")
    if not info or "symbols" not in info:
        return []
    out = []
    for s in info["symbols"]:
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        symbol = s.get("symbol", "")
        base = s.get("baseAsset", "")
        if is_leveraged_or_bad(symbol, base):
            continue
        out.append(symbol)
    return out

def get_top50_volume_spot_usdt():
    """Ordena por volume/quoteVolume em 24h e retorna os TOP_N válidos."""
    all_valid = set(get_valid_spot_usdt())
    data = fetch_json(f"{BINANCE}/api/v3/ticker/24hr")
    if not data:
        return []
    ranked = []
    for t in data:
        sym = t.get("symbol", "")
        if sym in all_valid:
            # quoteVolume costuma ser mais estável; fallback para volume
            qv = t.get("quoteVolume") or t.get("volume") or "0"
            try:
                ranked.append((sym, float(qv)))
            except:
                pass
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:TOP_N]]

def check_cooldown(symbol, key):
    now = time.time()
    last = cooldowns[symbol].get(key, 0)
    if now - last < COOLDOWN_SEC:
        return True
    cooldowns[symbol][key] = now
    return False

def chart_link(symbol, interval="5m"):
    # Link web; preview desabilitado, mas continua clicável
    return f"https://www.binance.com/en/trade?symbol={symbol}&type=spot"

def msg(symbol, emoji, titulo, motivo, price, rsi_val, ema9, ma20, ma50, ma200, label):
    return (
        f"{emoji} <b>{symbol}</b>\n"
        f"🧭 <b>{titulo}</b>\n"
        f"📊 {motivo}\n"
        f"💰 Preço: {price:.6f}\n"
        f"📈 EMA9: {ema9:.5f} | MA20: {ma20:.5f} | MA50: {ma50:.5f}\n"
        f"🌙 MA200: {ma200:.5f}\n"
        f"🧪 RSI: {rsi_val:.1f}\n"
        f"🇧🇷 {now_br_str()}\n"
        f"🔗 <a href='{chart_link(symbol, label)}'>Ver gráfico {label} (Binance)</a>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )

def cross_up(prev_a, now_a, prev_b, now_b):
    if prev_a is None or prev_b is None or now_a is None or now_b is None:
        return False
    return prev_a <= prev_b and now_a > now_b

# ==========================
# ANÁLISE DE ALERTAS
# ==========================
def analyze_symbol(symbol):
    try:
        # ----- 5m -----
        c5, v5 = get_klines(symbol, INTERVAL_5M, K_LIMIT)
        if len(c5) < 210:
            return

        ema9_5 = ema(c5, 9)
        ma20_5 = sma(c5, 20)
        ma50_5 = sma(c5, 50)
        ma200_5 = sma(c5, 200)
        rsi5 = rsi(c5, 14)
        price5 = c5[-1]

        e9_5, m20_5, m50_5, m200_5 = ema9_5[-1], ma20_5[-1], ma50_5[-1], ma200_5[-1]
        e9p_5, m20p_5 = ema9_5[-2], ma20_5[-2]

        # (5m) TENDÊNCIA INICIANDO — EMA9 cruza MA20 p/ cima, e MA20 > MA50 (estrutura virando) ainda ABAIXO da MA200
        if cross_up(e9p_5, e9_5, m20p_5, m20_5) and (m20_5 is not None and m50_5 is not None and m200_5 is not None):
            if (m20_5 > m50_5) and (e9_5 < m200_5):
                key = "inicio_5m"
                if not check_cooldown(symbol, key):
                    send_telegram(msg(
                        symbol, "🟢", "TENDÊNCIA INICIANDO (5m)",
                        "EMA9 cruzou MA20 p/ cima (abaixo da MA200) após queda/lateralização",
                        price5, rsi5 or 0.0, e9_5, m20_5, m50_5, m200_5, "5m"
                    ))

        # (5m) PRÉ-CONFIRMAÇÃO — EMA9 e MA20 ACIMA da MA200
        if (e9_5 is not None and m20_5 is not None and m200_5 is not None):
            if e9_5 > m200_5 and m20_5 > m200_5:
                key = "preconf_5m"
                if not check_cooldown(symbol, key):
                    send_telegram(msg(
                        symbol, "🔵", "PRÉ-CONFIRMAÇÃO (5m)",
                        "EMA9 e MA20 cruzaram p/ cima da MA200",
                        price5, rsi5 or 0.0, e9_5, m20_5, m50_5, m200_5, "5m"
                    ))

        # ----- 15m -----
        c15, v15 = get_klines(symbol, INTERVAL_15M, K_LIMIT)
        if len(c15) < 210:
            return

        ema9_15 = ema(c15, 9)
        ma20_15 = sma(c15, 20)
        ma50_15 = sma(c15, 50)
        ma200_15 = sma(c15, 200)
        rsi15 = rsi(c15, 14)
        price15 = c15[-1]

        e9_15, m20_15, m50_15, m200_15 = ema9_15[-1], ma20_15[-1], ma50_15[-1], ma200_15[-1]
        e9p_15, m200p_15 = ema9_15[-2], ma200_15[-2]

        # (15m) TENDÊNCIA PRÉ-CONFIRMADA — EMA9 cruza a MA200 p/ cima
        if cross_up(e9p_15, e9_15, m200p_15, m200_15):
            key = "preconf_15m"
            if not check_cooldown(symbol, key):
                send_telegram(msg(
                    symbol, "🟣", "TENDÊNCIA PRÉ-CONFIRMADA (15m)",
                    "EMA9 cruzou a MA200 p/ cima",
                    price15, rsi15 or 0.0, e9_15, m20_15, m50_15, m200_15, "15m"
                ))

        # (15m) TENDÊNCIA CONFIRMADA — MA20 e MA50 acima da MA200
        if (m20_15 is not None and m50_15 is not None and m200_15 is not None):
            if (m20_15 > m200_15) and (m50_15 > m200_15):
                key = "conf_15m"
                if not check_cooldown(symbol, key):
                    send_telegram(msg(
                        symbol, "🟣", "TENDÊNCIA CONFIRMADA (15m)",
                        "MA20 e MA50 cruzaram p/ cima da MA200",
                        price15, rsi15 or 0.0, e9_15, m20_15, m50_15, m200_15, "15m"
                    ))

        # (15m) RETESTE CONFIRMADO — preço testou EMA9 ou MA20 (~0.6%), RSI>55 e vol>média20
        vol_avg_15 = sum(v15[-20:]) / 20 if len(v15) >= 20 else None
        touched_ema9 = e9_15 and abs(price15 - e9_15) / price15 < 0.006
        touched_ma20 = m20_15 and abs(price15 - m20_15) / price15 < 0.006
        if (touched_ema9 or touched_ma20) and rsi15 and rsi15 > 55 and vol_avg_15 and v15[-1] > vol_avg_15 and price15 > (m20_15 or 0):
            key = "reteste_ok_15m"
            if not check_cooldown(symbol, key):
                send_telegram(msg(
                    symbol, "🟢", "RETESTE CONFIRMADO (15m)",
                    "Preço testou EMA9/MA20 e retomou com força (RSI>55, vol>média)",
                    price15, rsi15, e9_15, m20_15, m50_15, m200_15, "15m"
                ))

        # (15m) RETESTE FRACO — preço testou e perdeu, RSI<50
        if (touched_ema9 or touched_ma20) and rsi15 and rsi15 < 50 and price15 < (e9_15 or price15):
            key = "reteste_fraco_15m"
            if not check_cooldown(symbol, key):
                send_telegram(msg(
                    symbol, "🟠", "RETESTE FRACO (15m)",
                    "Preço testou EMA9/MA20 e perdeu força — possível queda",
                    price15, rsi15, e9_15, m20_15, m50_15, m200_15, "15m"
                ))

        # (15m) RETESTE MA200 — continuação de alta (teste da 200 e volta)
        touched_ma200 = m200_15 and abs(price15 - m200_15) / price15 < 0.006
        if touched_ma200 and rsi15 and rsi15 > 55 and price15 > m200_15:
            key = "reteste_ma200_15m"
            if not check_cooldown(symbol, key):
                send_telegram(msg(
                    symbol, "🟢", "RETESTE MA200 (15m)",
                    "Preço testou a MA200 e retomou com confirmação dos indicadores",
                    price15, rsi15, e9_15, m20_15, m50_15, m200_15, "15m"
                ))

    except Exception as e:
        # Proteção: um erro em 1 moeda não derruba as demais
        print(f"Erro analisando {symbol}:", e)

# ==========================
# TOP 50 E LOOP
# ==========================
def refresh_top():
    global current_top, last_top_update
    new_top = get_top50_volume_spot_usdt()
    if new_top:
        current_top = new_top
        last_top_update = time.time()
        send_telegram(f"🔄 Lista TOP {TOP_N} atualizada automaticamente ({len(current_top)} pares) 🇧🇷")

def worker(symbol):
    analyze_symbol(symbol)
    # Pequeno espaçamento para não saturar a API em rajada
    time.sleep(0.1)

def main_loop():
    # Mensagens iniciais
    send_telegram(f"✅ BOT ATIVO — SPOT/USDT\n"
                  f"⏱️ Cooldown: 15 min por par/alerta\n"
                  f"🔁 Atualização TOP {TOP_N}: a cada 1h\n"
                  f"🇧🇷 {now_br_str()}")

    # Carrega TOP inicialmente
    refresh_top()
    if current_top:
        topo = ", ".join(current_top[:5])
        send_telegram(f"📦 Pares carregados (TOP {TOP_N}): {topo} …")

    while True:
        # Atualiza Top 50 por hora
        if time.time() - last_top_update >= TOP_REFRESH_SEC or not current_top:
            refresh_top()

        # Analisa TOP atual com pool de threads limitada
        if current_top:
            threads = []
            for sym in current_top:
                t = threading.Thread(target=worker, args=(sym,), daemon=True)
                threads.append(t)
            # inicia em lotes para não passar do MAX_WORKERS
            for i in range(0, len(threads), MAX_WORKERS):
                batch = threads[i:i+MAX_WORKERS]
                for t in batch: t.start()
                for t in batch: t.join()

        time.sleep(SCAN_SLEEP)

# ==========================
# START (Render-ready)
# ==========================
def start_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    # Flask em thread separada (para o Render reconhecer a porta)
    threading.Thread(target=start_flask, daemon=True).start()
    # Loop principal
    main_loop()
