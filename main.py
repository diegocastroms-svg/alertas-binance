# main_short_v0.py — BOT CURTO (5m/15m) • SPOT/USDT • Render-ready
# ---------------------------------------------------------------
# Variáveis de ambiente no Render:
#   TELEGRAM_TOKEN      -> token do bot (BotFather)
#   TELEGRAM_CHAT_ID    -> id do chat/usuário/grupo
#   PORT                -> fornecida automaticamente pelo Render
#
# requirements.txt:
#   Flask==3.0.3
#   requests==2.31.0

import os
import time
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
K_LIMIT = 300                 # barras por série (suficiente para MA200/RSI)
TOP_N = 50                    # Top 50 por volume (SPOT/USDT)
TOP_REFRESH_SEC = 3600        # recarrega Top 50 a cada 1h
SCAN_SLEEP = 300              # varredura a cada 5 min
COOLDOWN_SEC = 15 * 60        # 15 min por par + tipo de alerta
MAX_WORKERS = 40              # threads por lote (estável p/ Render)

# Excluir tokens alavancados/sintéticos e anti-USD
EXCLUDE_KEYWORDS = ("UP","DOWN","BULL","BEAR","2L","2S","3L","3S","4L","4S","5L","5S","1000")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","").strip()

BR_TZ = timezone(timedelta(hours=-3))  # Horário de Brasília

# Estado
cooldowns = defaultdict(dict)    # cooldowns[symbol][alert_key] = last_ts
current_top = []                 # lista Top N atual
last_top_update = 0

# Flask (healthcheck Render)
app = Flask(__name__)
@app.route("/")
def health(): return "OK", 200

# ==========================
# UTILITÁRIOS
# ==========================
def now_br_str() -> str:
    return datetime.now(BR_TZ).strftime("%Y-%m-%d %H:%M")

def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log("AVISO: TELEGRAM_TOKEN/CHAT_ID não configurados. Mensagem não enviada.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        log(f"Erro Telegram: {e}")

def fetch_json(url, params=None, timeout=15):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log(f"fetch_json erro: {e}")
    return None

def get_klines(symbol: str, interval: str, limit: int = K_LIMIT):
    data = fetch_json(f"{BINANCE}/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        return [], []
    closes = [float(x[4]) for x in data]
    vols   = [float(x[5]) for x in data]
    return closes, vols

def sma(values, period: int):
    out, s, q = [], 0.0, []
    for v in values:
        q.append(v); s += v
        if len(q) > period:
            s -= q.pop(0)
        out.append((s / period) if len(q) == period else None)
    return out

def ema(values, period: int):
    out = []
    k = 2 / (period + 1)
    e = None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out

def rsi(values, period: int = 14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i-1]
        gains.append(max(d, 0.0))
        losses.append(-min(d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - (100 / (1 + rs))

def cross_up(prev_a, now_a, prev_b, now_b) -> bool:
    if any(x is None for x in (prev_a, now_a, prev_b, now_b)):
        return False
    return prev_a <= prev_b and now_a > now_b

def check_cooldown(symbol: str, key: str) -> bool:
    now = time.time()
    last = cooldowns[symbol].get(key, 0)
    if now - last < COOLDOWN_SEC:
        return True  # ainda em cooldown
    cooldowns[symbol][key] = now
    return False

def chart_link(symbol: str, label: str) -> str:
    return f"https://www.binance.com/en/trade?symbol={symbol}&type=spot"

def fmt_msg(symbol, emoji, titulo, motivo, price, rsi_val, e9, m20, m50, m200, label):
    return (
        f"{emoji} <b>{symbol}</b>\n"
        f"🧭 <b>{titulo}</b>\n"
        f"📊 {motivo}\n"
        f"💰 Preço: {price:.6f}\n"
        f"📈 EMA9: {e9:.5f} | MA20: {m20:.5f} | MA50: {m50:.5f}\n"
        f"🌙 MA200: {m200:.5f}\n"
        f"🧪 RSI: {rsi_val:.1f}\n"
        f"🇧🇷 {now_br_str()}\n"
        f"🔗 <a href='{chart_link(symbol,label)}'>Ver gráfico {label} (Binance)</a>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )

# ==========================
# COLETA TOP 50 SPOT/USDT
# ==========================
def is_filtered_symbol(symbol: str, base_asset: str) -> bool:
    s = symbol.upper()
    b = base_asset.upper()
    if s.endswith("USD") or b.endswith("USD"):   # anti-USD (queremos USDT)
        return True
    for kw in EXCLUDE_KEYWORDS:
        if kw in s:
            return True
    return False

def get_valid_spot_usdt():
    info = fetch_json(f"{BINANCE}/api/v3/exchangeInfo")
    out = []
    if not info or "symbols" not in info:
        return out
    for s in info["symbols"]:
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        symbol = s.get("symbol", "")
        base   = s.get("baseAsset", "")
        if is_filtered_symbol(symbol, base):
            continue
        out.append(symbol)
    return out

def get_top50():
    valid = set(get_valid_spot_usdt())
    data = fetch_json(f"{BINANCE}/api/v3/ticker/24hr")
    ranked = []
    if not data:
        return []
    for t in data:
        sym = t.get("symbol", "")
        if sym in valid:
            try:
                qv = float(t.get("quoteVolume") or 0.0)
                ranked.append((sym, qv))
            except:
                pass
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:TOP_N]]

# ==========================
# ANÁLISE (5m/15m)
# ==========================
def analyze_symbol(symbol: str):
    try:
        # -------- 5m --------
        c5, v5 = get_klines(symbol, INTERVAL_5M, K_LIMIT)
        if len(c5) < 210:
            return
        ema9_5 = ema(c5, 9)
        ma20_5 = sma(c5, 20)
        ma50_5 = sma(c5, 50)
        ma200_5 = sma(c5, 200)
        r5 = rsi(c5, 14)
        p5 = c5[-1]
        e9_5, m20_5, m50_5, m200_5 = ema9_5[-1], ma20_5[-1], ma50_5[-1], ma200_5[-1]
        e9p_5, m20p_5 = ema9_5[-2], ma20_5[-2]

        # (5m) MERCADO CAIU + LATERALIZANDO (pré-alerta)
        # Queda: MA20 descendente nos últimos 10 candles; Lateralização: amplitude < ~1% do preço médio recente
        if ma20_5[-10] and ma20_5[-1] and ma20_5[-1] < ma20_5[-10]:
            ult = c5[-20:]
            if ult and (max(ult) - min(ult)) < 0.01 * (sum(ult) / len(ult)):
                key = "queda_lat_5m"
                if not check_cooldown(symbol, key):
                    send_telegram(
                        f"🔻 <b>{symbol}</b>\n"
                        f"💬 Mercado em queda, lateralizando — monitorando possível alta\n"
                        f"🇧🇷 {now_br_str()}\n"
                        f"🔗 <a href='{chart_link(symbol,'5m')}'>Ver gráfico 5m</a>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━"
                    )

        # (5m) TENDÊNCIA INICIANDO — EMA9 cruza MA20 p/ cima e ainda abaixo da MA200
        if cross_up(e9p_5, e9_5, m20p_5, m20_5) and (e9_5 is not None and m20_5 is not None and m200_5 is not None) and (e9_5 < m200_5):
            key = "inicio_5m"
            if not check_cooldown(symbol, key):
                send_telegram(fmt_msg(
                    symbol, "🟢", "TENDÊNCIA INICIANDO (5m)",
                    "EMA9 cruzou MA20 p/ cima (abaixo da MA200) após queda/lateralização",
                    p5, r5 or 0.0, e9_5, m20_5, m50_5, m200_5, "5m"
                ))

        # -------- 15m --------
        c15, v15 = get_klines(symbol, INTERVAL_15M, K_LIMIT)
        if len(c15) < 210:
            return
        ema9_15 = ema(c15, 9)
        ma20_15 = sma(c15, 20)
        ma50_15 = sma(c15, 50)
        ma200_15 = sma(c15, 200)
        r15 = rsi(c15, 14)
        p15 = c15[-1]
        e9_15, m20_15, m50_15, m200_15 = ema9_15[-1], ma20_15[-1], ma50_15[-1], ma200_15[-1]
        e9p_15, m200p_15 = ema9_15[-2], ma200_15[-2]

        # (15m) TENDÊNCIA PRÉ-CONFIRMADA — EMA9 cruzou MA200 p/ cima
        if cross_up(e9p_15, e9_15, m200p_15, m200_15):
            key = "preconf_15m"
            if not check_cooldown(symbol, key):
                send_telegram(fmt_msg(
                    symbol, "🔵", "TENDÊNCIA PRÉ-CONFIRMADA (15m)",
                    "EMA9 cruzou a MA200 p/ cima",
                    p15, r15 or 0.0, e9_15, m20_15, m50_15, m200_15, "15m"
                ))

        # (15m) RETESTE CONFIRMADO — tocou EMA9/MA20, RSI>55, vol>média20 e preço acima da MA20
        vol_avg_15 = sum(v15[-20:]) / 20 if len(v15) >= 20 else None
        touch9 = (e9_15 is not None) and abs(p15 - e9_15) / p15 < 0.006
        touch20 = (m20_15 is not None) and abs(p15 - m20_15) / p15 < 0.006

        if (touch9 or touch20) and r15 and r15 > 55 and vol_avg_15 and v15[-1] > vol_avg_15 and (m20_15 is not None) and p15 > m20_15:
            key = "reteste_ok_15m"
            if not check_cooldown(symbol, key):
                send_telegram(fmt_msg(
                    symbol, "🟢", "RETESTE CONFIRMADO (15m)",
                    "Preço testou EMA9/MA20 e retomou com força (RSI>55, vol>média)",
                    p15, r15, e9_15, m20_15, m50_15, m200_15, "15m"
                ))

        # (15m) RETESTE FRACO — tocou e perdeu, RSI<50 e preço abaixo da EMA9
        if (touch9 or touch20) and r15 and r15 < 50 and (e9_15 is not None) and p15 < e9_15:
            key = "reteste_fraco_15m"
            if not check_cooldown(symbol, key):
                send_telegram(fmt_msg(
                    symbol, "🟠", "RETESTE FRACO (15m)",
                    "Preço testou EMA9/MA20 e perdeu força — possível queda",
                    p15, r15, e9_15, m20_15, m50_15, m200_15, "15m"
                ))

    except Exception as e:
        log(f"Erro analisando {symbol}: {e}")

# ==========================
# LOOP PRINCIPAL
# ==========================
def refresh_top():
    global current_top, last_top_update
    top = get_top50()
    if top:
        current_top = top
        last_top_update = time.time()
        send_telegram(f"🔄 TOP {TOP_N} atualizado automaticamente ({len(current_top)} pares) 🇧🇷")
        log(f"TOP atualizado: {len(current_top)} pares")

def worker(sym):
    analyze_symbol(sym)
    time.sleep(0.1)  # suaviza rajada na API

def main_loop():
    send_telegram(
        f"✅ BOT CURTO ATIVO — SPOT/USDT\n"
        f"⏱️ Cooldown: 15 min por par/alerta\n"
        f"🔁 Atualização TOP {TOP_N}: a cada 1h\n"
        f"🇧🇷 {now_br_str()}"
    )

    refresh_top()
    if current_top:
        send_telegram(f"📦 Top 5: {', '.join(current_top[:5])}")

    while True:
        if (time.time() - last_top_update >= TOP_REFRESH_SEC) or (not current_top):
            refresh_top()

        if current_top:
            threads = []
            for s in current_top:
                threads.append(threading.Thread(target=worker, args=(s,), daemon=True))

            # roda em lotes p/ estabilidade
            for i in range(0, len(threads), MAX_WORKERS):
                batch = threads[i:i+MAX_WORKERS]
                for t in batch: t.start()
                for t in batch: t.join()

        time.sleep(SCAN_SLEEP)

# ==========================
# START (Render)
# ==========================
def start_flask():
    port = int(os.environ.get("PORT", "5000"))
    log(f"FLASK ouvindo em 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    main_loop()
```0
