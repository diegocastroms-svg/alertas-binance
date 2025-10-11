# =====================================================
# 📁 main_v2_8_full.py — Multi-Setup completo e funcional
# =====================================================
# TFs ativos: 5m, 15m, 1h, 4h (todos enviam alerta)
# Novo: "Tendência Iniciando (5m)" (queda → lateral → EMA9 cruza MA20/MA50 sob MA200)
# Top 50 SPOT/USDT por volume 24h (refresh 1h) • Cooldown 15min/par/TF
# Deep link p/ app Binance • Flask use_reloader=False
# =====================================================

import os, asyncio, aiohttp, threading
from datetime import datetime, timedelta
from statistics import mean
from flask import Flask

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://api.binance.com/api/v3"

TOP_N = 50
COOLDOWN_MIN = 15
COOLDOWN = timedelta(minutes=COOLDOWN_MIN)
TOP_REFRESH_EVERY = timedelta(hours=1)
ANTI_LIST = ["USD","FDUSD","BUSD","TUSD","USDC","DAI","AEUR","EUR","PYUSD"]

cooldowns = {tf: {} for tf in ["5m","15m","1h","4h"]}
top_pairs_cache, next_top_refresh_at = [], None

app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "OK — v2.8 FULL", 200

# ----------------- Telegram -----------------
async def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    async with aiohttp.ClientSession() as s:
        try: await s.post(url, data=data)
        except Exception as e: print("Telegram:", e)

# --------------- Helpers/Links --------------
def deep_link(symbol: str) -> str:
    base = symbol.replace("USDT","")
    return f"binance://app/spot/trade?symbol={base}_USDT"

def link_line(symbol: str, tf: str) -> str:
    return f"🔗 <a href='{deep_link(symbol)}'>Ver gráfico {tf} no app da Binance</a>"

# ----------------- Indicadores --------------
def ma(xs, p): return mean(xs[-p:]) if len(xs) >= p else None
def ema(xs, p):
    if len(xs) < p: return None
    k = 2/(p+1); e = xs[-p]
    for x in xs[-p+1:]: e = x*k + e*(1-k)
    return e
def rsi(xs, p=14):
    if len(xs) < p+1: return None
    g,l=[],[]
    for i in range(-p,0):
        d = xs[i] - xs[i-1]
        (g if d>0 else l).append(abs(d))
    ag = mean(g) if g else 0.0
    al = mean(l) if l else 1e-9
    rs = ag/al
    return 100 - (100/(1+rs))

def near(a,b,pct=0.006):
    if a is None or b is None or b == 0: return False
    return abs(a-b)/abs(b) < pct

def vol_ratio(v_now, vma20): return (v_now/vma20) if vma20 and vma20>0 else 1.0

def was_falling_then_sideways(closes):
    if len(closes) < 60: return False
    ma20_now = ma(closes,20)
    ma20_prev = ma(closes[:-20],20) if len(closes) >= 40 else None
    falling = (ma20_prev is not None and ma20_now is not None and ma20_now < ma20_prev)
    win = closes[-6:]; amp = max(win) - min(win)
    base = ma20_now or closes[-1]
    sideways = base > 0 and (amp/base) < 0.01
    return falling and sideways

# ----------------- Binance API --------------
async def get_json(session, url):
    async with session.get(url) as r: return await r.json()

async def get_tickers(session): return await get_json(session, f"{BASE}/ticker/24hr")

async def get_klines(session, sym, interval, limit=240):
    return await get_json(session, f"{BASE}/klines?symbol={sym}&interval={interval}&limit={limit}")

# ------------- Top 50 SPOT/USDT ------------
async def compute_top50(session):
    data = await get_tickers(session)
    if not isinstance(data, list): return []
    ranked=[]
    for t in data:
        s = t.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(x in s.replace("USDT","") for x in ANTI_LIST): continue
        try: qv = float(t.get("quoteVolume","0") or 0.0)
        except: qv = 0.0
        ranked.append((s,qv))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s,_ in ranked[:TOP_N]]

async def ensure_top(session):
    global top_pairs_cache, next_top_refresh_at
    now = datetime.utcnow()
    if not next_top_refresh_at or now >= next_top_refresh_at:
        newlist = await compute_top50(session)
        if newlist:
            top_pairs_cache = newlist
            await send_telegram("🔄 TOP 50 SPOT/USDT atualizado (volume 24h).")
        next_top_refresh_at = now + TOP_REFRESH_EVERY
    return top_pairs_cache

# ----------------- 5m -----------------------
async def analyze_5m(session, sym):
    now = datetime.utcnow()
    if sym in cooldowns["5m"] and now - cooldowns["5m"][sym] < COOLDOWN: return
    k = await get_klines(session, sym, "5m", 240)
    if not k or len(k) < 210: return
    c = [float(x[4]) for x in k]; v = [float(x[5]) for x in k]; price = c[-1]
    e9 = ema(c,9); m20 = ma(c,20); m50 = ma(c,50); m200 = ma(c,200); r = rsi(c,14)
    if not all([e9,m20,m50,m200,r]): return
    vr = vol_ratio(v[-1], ma(v,20))

    # Tendência iniciando (queda → lateral → cruzamento sob MA200)
    if e9 > m20 > m50 and price < m200 and r > 50 and was_falling_then_sideways(c):
        msg = (f"🟢 <b>[TENDÊNCIA INICIANDO (5m)]</b> {sym}\n"
               f"Queda→lateral e EMA9 cruzou MA20/MA50 <b>abaixo da MA200</b>.\n"
               f"RSI={r:.1f} • Vol≈{vr:.1f}x\n💰 {price:.6f}\n{link_line(sym,'5m')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["5m"][sym]=now; return

    # Pré-confirmação (abaixo MA200)
    if e9 > m20 > m50 and price < m200 and r > 55:
        msg = (f"🟢 <b>[PUMP 5m — PRÉ-CONFIRMAÇÃO]</b> {sym}\n"
               f"EMA9>MA20>MA50 e preço<MA200 • RSI={r:.1f} • Vol≈{vr:.1f}x\n"
               f"💰 {price:.6f}\n{link_line(sym,'5m')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["5m"][sym]=now; return

    # Entrada segura (acima MA200, força e volume)
    if e9 > m20 > m50 and price > m200 and 55 <= r <= 70 and vr >= 1.5:
        msg = (f"✅ <b>[PUMP 5m — ENTRADA SEGURA]</b> {sym}\n"
               f"EMA9>MA20>MA50 e preço>MA200 • RSI={r:.1f} • Vol≈{vr:.1f}x\n"
               f"💰 {price:.6f}\n{link_line(sym,'5m')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["5m"][sym]=now; return

    # Saída (perdendo força)
    if price < e9 or r < 50:
        msg = (f"⚠️ <b>[PUMP 5m — SAÍDA]</b> {sym}\n"
               f"Perdendo força • RSI={r:.1f}\n💰 {price:.6f}\n{link_line(sym,'5m')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["5m"][sym]=now

# ----------------- 15m ----------------------
async def analyze_15m(session, sym):
    now = datetime.utcnow()
    if sym in cooldowns["15m"] and now - cooldowns["15m"][sym] < COOLDOWN: return
    k = await get_klines(session, sym, "15m", 240)
    if not k or len(k) < 210: return
    c = [float(x[4]) for x in k]; v = [float(x[5]) for x in k]; price = c[-1]
    e9 = ema(c,9); m20 = ma(c,20); m50 = ma(c,50); m200 = ma(c,200); r = rsi(c,14)
    if not all([e9,m20,m50,m200,r]): return
    e9p = ema(c[:-1],9); m200p = ma(c[:-1],200)

    trend_up = (e9 > m20 > m50) and (price > m200)
    touch = near(price, e9) or near(price, m20)

    # Reteste confirmado (pullback)
    if trend_up and touch and r > 55:
        msg = (f"🟣 <b>[15m — RETESTE CONFIRMADO]</b> {sym}\n"
               f"Reteste EMA9/MA20 e retomada • RSI={r:.1f}\n"
               f"💰 {price:.6f}\n{link_line(sym,'15m')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["15m"][sym]=now; return

    # Reteste fraco
    if trend_up and touch and r < 50:
        msg = (f"🟠 <b>[15m — RETESTE FRACO]</b> {sym}\n"
               f"Perdendo força após reteste • RSI={r:.1f}\n"
               f"💰 {price:.6f}\n{link_line(sym,'15m')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["15m"][sym]=now; return

    # Pré-confirmação (EMA9 cruza MA200 pra cima)
    if e9p is not None and m200p is not None and e9p <= m200p and e9 > m200:
        msg = (f"🟣 <b>[15m — PRÉ-CONFIRMAÇÃO]</b> {sym}\n"
               f"EMA9 cruzou MA200 pra cima • RSI={r:.1f}\n"
               f"💰 {price:.6f}\n{link_line(sym,'15m')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["15m"][sym]=now; return

    # Tendência confirmada 15m
    if m20 > m50 > m200 and r > 55:
        msg = (f"🟣 <b>[15m — TENDÊNCIA CONFIRMADA]</b> {sym}\n"
               f"MA20>MA50>MA200 • RSI={r:.1f}\n"
               f"💰 {price:.6f}\n{link_line(sym,'15m')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["15m"][sym]=now; return

    # Reteste MA200 (continuação)
    if trend_up and near(price, m200) and r > 55:
        msg = (f"🟣 <b>[15m — RETESTE MA200]</b> {sym}\n"
               f"Continuação após MA200 • RSI={r:.1f}\n"
               f"💰 {price:.6f}\n{link_line(sym,'15m')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["15m"][sym]=now

# ----------------- 1h -----------------------
async def analyze_1h(session, sym):
    now = datetime.utcnow()
    if sym in cooldowns["1h"] and now - cooldowns["1h"][sym] < COOLDOWN: return
    k = await get_klines(session, sym, "1h", 240)
    if not k or len(k) < 210: return
    c = [float(x[4]) for x in k]; price = c[-1]
    e9 = ema(c,9); m20 = ma(c,20); m50 = ma(c,50); m200 = ma(c,200); r = rsi(c,14)
    if not all([e9,m20,m50,m200,r]): return
    e9p = ema(c[:-1],9); m200p = ma(c[:-1],200)

    # Pré-confirmação 1h
    if e9p is not None and m200p is not None and e9p <= m200p and e9 > m200:
        msg = (f"🟡 <b>[1h — PRÉ-CONFIRMAÇÃO]</b> {sym}\n"
               f"EMA9 acima da MA200 • RSI={r:.1f}\n"
               f"💰 {price:.6f}\n{link_line(sym,'1h')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["1h"][sym]=now; return

    # Tendência confirmada 1h
    if m20 > m50 > m200 and r > 60:
        msg = (f"🟡 <b>[1h — TENDÊNCIA CONFIRMADA]</b> {sym}\n"
               f"MA20>MA50>MA200 • RSI={r:.1f}\n"
               f"💰 {price:.6f}\n{link_line(sym,'1h')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["1h"][sym]=now; return

    # Saída macro 1h
    if price < e9 or r < 50:
        msg = (f"⚠️ <b>[1h — SAÍDA]</b> {sym}\n"
               f"Perdendo força • RSI={r:.1f}\n💰 {price:.6f}\n{link_line(sym,'1h')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["1h"][sym]=now

# ----------------- 4h -----------------------
async def analyze_4h(session, sym):
    now = datetime.utcnow()
    if sym in cooldowns["4h"] and now - cooldowns["4h"][sym] < COOLDOWN: return
    k = await get_klines(session, sym, "4h", 240)
    if not k or len(k) < 210: return
    c = [float(x[4]) for x in k]; price = c[-1]
    e9 = ema(c,9); m20 = ma(c,20); m50 = ma(c,50); m200 = ma(c,200); r = rsi(c,14)
    if not all([e9,m20,m50,m200,r]): return

    # Pré-confirmação 4h (sob MA200)
    if e9 > m20 > m50 and price < m200 and r > 55:
        msg = (f"🔵 <b>[4h — PRÉ-CONFIRMAÇÃO]</b> {sym}\n"
               f"EMA9>MA20>MA50 e preço<MA200 • RSI={r:.1f}\n"
               f"💰 {price:.6f}\n{link_line(sym,'4h')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["4h"][sym]=now; return

    # Tendência confirmada 4h
    if e9 > m20 > m50 > m200 and 60 <= r <= 75:
        msg = (f"🔵 <b>[4h — TENDÊNCIA CONFIRMADA]</b> {sym}\n"
               f"EMA9>MA20>MA50>MA200 • RSI={r:.1f}\n"
               f"💰 {price:.6f}\n{link_line(sym,'4h')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["4h"][sym]=now; return

    # Saída macro 4h
    if price < m50 or r < 50:
        msg = (f"⚠️ <b>[4h — SAÍDA]</b> {sym}\n"
               f"Perda de força macro • RSI={r:.1f}\n💰 {price:.6f}\n{link_line(sym,'4h')}\n{'━'*28}")
        await send_telegram(msg); cooldowns["4h"][sym]=now

# ----------------- LOOP ---------------------
async def main_loop():
    await send_telegram("✅ <b>BOT ATIVO — Multi-Setup Completo v2.8</b>\n🕒 5m • 15m • 1h • 4h\n💹 Alerta novo: <b>Tendência Iniciando (5m)</b>")
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                pairs = await ensure_top(s)
                if not pairs: await asyncio.sleep(10); continue
                tasks=[]
                for sym in pairs:
                    tasks += [analyze_5m(s,sym), analyze_15m(s,sym), analyze_1h(s,sym), analyze_4h(s,sym)]
                await asyncio.gather(*tasks)
            await asyncio.sleep(60)
        except Exception as e:
            print("Loop:", e); await asyncio.sleep(10)

def _start(): asyncio.run(main_loop())

if __name__ == "__main__":
    threading.Thread(target=_start, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
