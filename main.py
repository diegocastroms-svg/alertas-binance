# ============================================
# 📁 main_v2_3_preconfirm5m.py
# ============================================
# Base: v2.3 Dynamic Volume Scanner (Aurora + Diego)
# Atualização única:
#   ➕ Novo alerta no 5m — pré-confirmação abaixo da MA200
# ============================================

import os
import asyncio
import aiohttp
import threading
from datetime import datetime, timedelta
from statistics import mean
from flask import Flask

# -----------------------------
# 🔧 Variáveis de ambiente
# -----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://api.binance.com/api/v3"

# -----------------------------
# ⚙️ Parâmetros gerais
# -----------------------------
TOP_N = 50
COOLDOWN_MIN = 15
COOLDOWN = timedelta(minutes=COOLDOWN_MIN)
TOP_REFRESH_EVERY = timedelta(hours=1)
ANTI_LIST = ["USD","FDUSD","BUSD","TUSD","USDC","DAI","AEUR","EUR","PYUSD"]

cooldown_pump = {}
cooldown_day  = {}
cooldown_swing = {}

top_pairs_cache = []
next_top_refresh_at = None

# -----------------------------
# 🌐 Flask (Render keep-alive)
# -----------------------------
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "OK — v2.3 preconfirm5m", 200

# -----------------------------
# ✉️ Telegram
# -----------------------------
async def send_telegram(msg: str, html: bool = True):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": True}
    if html:
        payload["parse_mode"] = "HTML"
    async with aiohttp.ClientSession() as s:
        await s.post(url, data=payload)

# -----------------------------
# 🔗 Link direto pro gráfico
# -----------------------------
def binance_chart_link(symbol: str) -> str:
    base = symbol.replace("USDT", "")
    return f"https://www.binance.com/en/trade/{base}_USDT?ref=open_in_app&layout=pro"

def chart_link_line(symbol: str, tf_label: str) -> str:
    return f'🔗 <a href="{binance_chart_link(symbol)}">Ver gráfico {tf_label} no app da Binance</a>'

# -----------------------------
# 🧮 Indicadores simples
# -----------------------------
def ma(series, p):
    if len(series) < p: return None
    return mean(series[-p:])

def ema(series, p):
    if len(series) < p: return None
    k = 2/(p+1)
    e = series[-p]
    for x in series[-p+1:]:
        e = x*k + e*(1-k)
    return e

def rsi(series, p=14):
    if len(series) < p+1: return None
    gains, losses = [], []
    for i in range(-p, 0):
        diff = series[i] - series[i-1]
        (gains if diff>0 else losses).append(abs(diff))
    ag = mean(gains) if gains else 0.0
    al = mean(losses) if losses else 1e-9
    rs = ag/al
    return 100 - (100/(1+rs))

# -----------------------------
# 🔍 Funções Binance
# -----------------------------
async def get_json(session, url):
    async with session.get(url) as resp:
        return await resp.json()

async def get_ticker_24h(session):
    return await get_json(session, f"{BASE}/ticker/24hr")

async def get_klines(session, symbol, interval, limit=240):
    url = f"{BASE}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return await get_json(session, url)

# -----------------------------
# 🔍 Scanner TOP 50 SPOT
# -----------------------------
async def compute_top50(session):
    tickers = await get_ticker_24h(session)
    if not isinstance(tickers, list): return []
    ranked = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"): continue
        base = sym.replace("USDT", "")
        if any(x in base for x in ANTI_LIST): continue
        try: qv = float(t.get("quoteVolume", "0") or 0.0)
        except: qv = 0.0
        ranked.append((sym, qv))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s,_ in ranked[:TOP_N]]

async def ensure_top_pairs(session, force=False):
    global top_pairs_cache, next_top_refresh_at
    now = datetime.utcnow()
    if force or next_top_refresh_at is None or now >= next_top_refresh_at:
        new_list = await compute_top50(session)
        if new_list and new_list != top_pairs_cache:
            top_pairs_cache = new_list
            await send_telegram("🔄 Lista TOP 50 SPOT atualizada — monitorando novas moedas (baseado em volume 24h).")
        next_top_refresh_at = now + TOP_REFRESH_EVERY
    return top_pairs_cache

# -----------------------------
# ⚡ Pump detector (5m)
# -----------------------------
async def pump_detector(session, symbol):
    now = datetime.now()
    if symbol in cooldown_pump and now - cooldown_pump[symbol] < COOLDOWN:
        return

    k5 = await get_klines(session, symbol, "5m", 240)
    if not isinstance(k5, list) or len(k5) < 210: return
    c5 = [float(c[4]) for c in k5]
    v5 = [float(c[5]) for c in k5]
    price = c5[-1]

    ema9_5  = ema(c5,9)
    ma20_5  = ma(c5,20)
    ma50_5  = ma(c5,50)
    ma200_5 = ma(c5,200)
    rsi14_5 = rsi(c5,14)
    if not all([ema9_5, ma20_5, ma50_5, ma200_5, rsi14_5]): return

    # 🔹 NOVO ALERTA: pré-confirmação 5m (abaixo da MA200)
    if ema9_5 > ma20_5 > ma50_5 and price < ma200_5:
        msg_pre = (
            f"🟢 {symbol}\n"
            f"Tendência pré-confirmada — EMA9>MA20>MA50 abaixo da MA200 (5m)\n"
            f"💰 Preço: {price:.6f}\n"
            f"{chart_link_line(symbol,'5m')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg_pre)
        cooldown_pump[symbol] = now

    # 🔹 Alerta normal de cruzamento (mantido)
    if ema9_5 > ma20_5 and rsi14_5 > 50:
        msg = (
            f"🚀 {symbol}\n"
            f"Tendência de alta iniciada (5m)\n"
            f"EMA9>MA20 • RSI={rsi14_5:.1f}\n"
            f"💰 Preço: {price:.6f}\n"
            f"{chart_link_line(symbol,'5m')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_pump[symbol] = now

# -----------------------------
# 🔁 Loop principal
# -----------------------------
async def main_loop():
    await send_telegram("Bot iniciado com sucesso ✅", html=False)
    await asyncio.sleep(1)
    await send_telegram("✅ <b>BOT ATIVO — v2.3 preconfirm5m</b>\n🧠 Novo alerta 5m ativo.")

    while True:
        try:
            async with aiohttp.ClientSession() as s:
                pairs = await ensure_top_pairs(s)
                if not pairs:
                    await asyncio.sleep(10)
                    continue
                tasks = [pump_detector(s, sym) for sym in pairs]
                await asyncio.gather(*tasks)
            await asyncio.sleep(60)
        except Exception as e:
            print("Erro loop:", e)
            await asyncio.sleep(10)

def _start():
    asyncio.run(main_loop())

if __name__ == "__main__":
    threading.Thread(target=_start, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
