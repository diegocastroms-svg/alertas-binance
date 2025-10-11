# ============================================
# 📁 main.py — v2.4 oficial (Pump 5m Inteligente)
# ============================================
# Base: v2.3 Dynamic estável (mantida 100%)
# Adições:
#   ✅ Novo sistema de alertas Pump (5m):
#       - Pré-confirmação (EMA9>MA20>MA50 e preço<MA200)
#       - Entrada segura (EMA9>MA20>MA50 e preço>MA200, RSI 55–70, volume↑)
#       - Saída (perda EMA9 ou queda RSI ≥10 pts)
#   ✅ Link abre direto no gráfico do app Binance
#   ✅ Cooldown 15 min mantido
#   ✅ Top 50 SPOT USDT por volume (atualiza 1h)
# ============================================

import os
import asyncio
import aiohttp
import threading
from datetime import datetime, timedelta
from statistics import mean
from flask import Flask

# ======================
# 🔧 Variáveis de ambiente
# ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://api.binance.com/api/v3"

# ======================
# ⚙️ Configurações gerais
# ======================
TOP_N = 50
COOLDOWN_MIN = 15
COOLDOWN = timedelta(minutes=COOLDOWN_MIN)
TOP_REFRESH_EVERY = timedelta(hours=1)
ANTI_LIST = ["USD","FDUSD","BUSD","TUSD","USDC","DAI","AEUR","EUR","PYUSD"]

cooldown_pump = {}
top_pairs_cache = []
next_top_refresh_at = None

# ======================
# 🌐 Flask (Render keep-alive)
# ======================
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "OK — BOT v2.4 ativo", 200

# ======================
# ✉️ Telegram
# ======================
async def send_telegram(msg: str, html: bool = True):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": True}
    if html:
        payload["parse_mode"] = "HTML"
    async with aiohttp.ClientSession() as s:
        await s.post(url, data=payload)

# ======================
# 🔗 Link — gráfico direto no app Binance
# ======================
def binance_chart_link(symbol: str) -> str:
    base = symbol.replace("USDT", "")
    return f"binance://app/spot/trade?symbol={base}_USDT"

def chart_link_line(symbol: str, tf_label: str) -> str:
    return f'🔗 <a href="{binance_chart_link(symbol)}">Ver gráfico {tf_label} no app da Binance</a>'

# ======================
# 📊 Indicadores
# ======================
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

# ======================
# 🧩 Funções Binance
# ======================
async def get_json(session, url):
    async with session.get(url) as resp:
        return await resp.json()

async def get_ticker_24h(session):
    return await get_json(session, f"{BASE}/ticker/24hr")

async def get_klines(session, symbol, interval, limit=240):
    url = f"{BASE}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return await get_json(session, url)

# ======================
# 🔍 Scanner TOP 50 SPOT
# ======================
async def compute_top50(session):
    tickers = await get_ticker_24h(session)
    if not isinstance(tickers, list): return []
    ranked = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"): continue
        base = sym.replace("USDT", "")
        if any(x in base for x in ANTI_LIST): continue
        try:
            qv = float(t.get("quoteVolume", "0") or 0.0)
        except:
            qv = 0.0
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
            await send_telegram("🔄 Lista TOP 50 SPOT atualizada — monitorando novas moedas (por volume 24h).")
        next_top_refresh_at = now + TOP_REFRESH_EVERY
    return top_pairs_cache

# ======================
# ⚡ Pump Inteligente (5m)
# ======================
async def pump_detector(session, symbol):
    now = datetime.now()
    if symbol in cooldown_pump and now - cooldown_pump[symbol] < COOLDOWN:
        return

    k5 = await get_klines(session, symbol, "5m", 240)
    if not isinstance(k5, list) or len(k5) < 210: return
    c5 = [float(c[4]) for c in k5]
    v5 = [float(c[5]) for c in k5]
    price = c5[-1]

    ema9 = ema(c5,9)
    ma20 = ma(c5,20)
    ma50 = ma(c5,50)
    ma200 = ma(c5,200)
    rsi14 = rsi(c5,14)
    vol_ratio = (v5[-1]/ma(v5,20)) if ma(v5,20) else None
    if not all([ema9, ma20, ma50, ma200, rsi14]): return

    # 🔸 Pré-confirmação (abaixo da MA200)
    if ema9 > ma20 > ma50 and price < ma200 and rsi14 > 55:
        msg = (
            f"🟢 <b>[PUMP 5m — PRÉ-CONFIRMAÇÃO]</b> {symbol}\n"
            f"EMA9>MA20>MA50 com <b>preço abaixo da MA200</b>\n"
            f"RSI={rsi14:.1f} • Vol≈{(vol_ratio or 0):.1f}x\n"
            f"💰 Preço: {price:.6f}\n"
            f"🕒 {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"{chart_link_line(symbol, '5m')}\n{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_pump[symbol] = now
        return

    # 🟢 Entrada segura (acima da MA200)
    if ema9 > ma20 > ma50 and price > ma200 and 55 <= rsi14 <= 70 and (vol_ratio or 0) >= 1.5:
        msg = (
            f"✅ <b>[PUMP 5m — ENTRADA SEGURA]</b> {symbol}\n"
            f"EMA9>MA20>MA50 e preço>MA200\n"
            f"RSI={rsi14:.1f} • Vol≈{(vol_ratio or 0):.1f}x\n"
            f"💰 Preço: {price:.6f}\n"
            f"{chart_link_line(symbol, '5m')}\n{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_pump[symbol] = now
        return

    # 🔻 Saída — perdendo força
    if price < ema9 or rsi14 < 50:
        msg = (
            f"⚠️ <b>[PUMP 5m — SAÍDA]</b> {symbol}\n"
            f"Perdendo força • RSI={rsi14:.1f}\n"
            f"💰 Preço: {price:.6f}\n"
            f"{chart_link_line(symbol, '5m')}\n{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_pump[symbol] = now

# ======================
# 🔁 Loop principal
# ======================
async def main_loop():
    await send_telegram("✅ <b>BOT ATIVO — Multi-Setup v2.4</b>\n🚀 Pump Inteligente 5m com link direto no app Binance")
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
    app.run(host="0.0.0.0", port=port, use_reloader=False)
