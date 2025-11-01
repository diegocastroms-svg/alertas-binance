# main.py — V6.2 OURO CONFLUÊNCIA CURTA (AGRESSIVA + TAXA)
# RSI 40–80 | MACD >= 0 | EMA9 > EMA20 (3m)
# MACD >= 0 em 5m, 15m, 30m e 1h
# Liquidez >= 20M USDT | Cooldown 10 min
# Taxa de acerto em tempo real (últimas 50 operações)

import os, asyncio, aiohttp, time, threading, statistics
from datetime import datetime, timedelta
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 10 * 60
TOP_N = 50
REQ_TIMEOUT = 10
RESULT_DELAY = 15 * 60  # 15 min após alerta para avaliar acerto
PERCENT_THRESHOLD = 1.0  # ±1% para considerar gain/loss

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return "OURO CONFLUÊNCIA CURTA AGRESSIVA V6.2 + TAXA", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%d/%m %H:%M:%S")

async def tg(session, text):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        async with session.post(url, data=data, timeout=10) as r: await r.text()
    except: pass

def ema(seq, span):
    if len(seq) < span: return []
    alpha = 2 / (span + 1)
    out, e = [], seq[0]
    for x in seq:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def macd_hist(seq):
    if len(seq) < 35: return 0.0
    ema_fast = ema(seq, 12)
    ema_slow = ema(seq, 26)
    macd_line = [a - b for a, b in zip(ema_fast, ema_slow)]
    signal = ema(macd_line, 9)
    return macd_line[-1] - signal[-1]

def calc_rsi(seq, period=14):
    if len(seq) <= period: return 50
    gains, losses = [], []
    for i in range(1, len(seq)):
        diff = seq[i] - seq[i-1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0: return 70
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

LAST_ALERT = {}
TRADES = []  # lista de (symbol, entry_price, timestamp)

def allowed(symbol):
    ts = LAST_ALERT.get(symbol, 0)
    return time.time() - ts > COOLDOWN_SEC

def mark(symbol):
    LAST_ALERT[symbol] = time.time()

def calc_taxa():
    if not TRADES: return 0.0
    ganhos = [t for t in TRADES if t.get("resultado") == "GAIN"]
    return (len(ganhos) / len(TRADES)) * 100 if TRADES else 0.0

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=100):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            return await r.json()
    except:
        return []

async def get_top_symbols(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
    except:
        return []
    out = []
    for d in data:
        try:
            s = d["symbol"]
            if not s.endswith("USDT"): continue
            qv = float(d["quoteVolume"])
            if qv >= 20_000_000:
                out.append((s, qv))
        except:
            continue
    out.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in out[:TOP_N]]

# ---------------- SCANNER ----------------
async def scan_symbol(session, symbol):
    try:
        k3m = await get_klines(session, symbol, "3m", 100)
        k5m = await get_klines(session, symbol, "5m", 100)
        k15m = await get_klines(session, symbol, "15m", 100)
        k30m = await get_klines(session, symbol, "30m", 100)
        k1h = await get_klines(session, symbol, "1h", 100)
        if not all([k3m, k5m, k15m, k30m, k1h]): return

        c3 = [float(x[4]) for x in k3m]
        c5 = [float(x[4]) for x in k5m]
        c15 = [float(x[4]) for x in k15m]
        c30 = [float(x[4]) for x in k30m]
        c1h = [float(x[4]) for x in k1h]

        ema9_3 = ema(c3, 9)[-1]
        ema20_3 = ema(c3, 20)[-1]
        macd_3 = macd_hist(c3)
        rsi_3 = calc_rsi(c3)

        cond_3m = ema9_3 > ema20_3 and 40 <= rsi_3 <= 80 and macd_3 >= 0
        cond_5m = macd_hist(c5) >= 0
        cond_15 = macd_hist(c15) >= 0
        cond_30 = macd_hist(c30) >= 0
        cond_1h = macd_hist(c1h) >= 0

        if all([cond_3m, cond_5m, cond_15, cond_30, cond_1h]) and allowed(symbol):
            mark(symbol)
            entry_price = c3[-1]
            TRADES.append({"symbol": symbol, "entry": entry_price, "time": time.time(), "resultado": None})
            taxa = calc_taxa()

            msg = (
                f"🔥 <b>TENDÊNCIA CURTA AGRESSIVA</b> 🔥\n"
                f"<b>{symbol}</b>\n"
                f"💹 RSI(3m): {rsi_3:.1f}\n"
                f"📊 MACD(3m): {macd_3:.5f}\n"
                f"📈 Taxa: {taxa:.1f}% ({len([t for t in TRADES if t.get('resultado')=='GAIN'])}/"
                f"{len(TRADES)})\n"
                f"🕒 {now_br()}\n"
                f"<a href='https://www.binance.com/en/trade/{symbol}'>ABRIR</a>"
            )
            await tg(session, msg)
            print(f"[ALERTA] {symbol} | RSI={rsi_3:.1f} | MACD={macd_3:.4f}")

    except Exception as e:
        print(f"[ERRO] {symbol}: {e}")

# ---------------- AVALIAR RESULTADOS ----------------
async def avaliar_trades(session):
    while True:
        for t in TRADES:
            if t["resultado"] is not None: continue
            if time.time() - t["time"] >= RESULT_DELAY:
                k = await get_klines(session, t["symbol"], "3m", 5)
                if not k: continue
                last_close = float(k[-1][4])
                var = ((last_close - t["entry"]) / t["entry"]) * 100
                if var >= PERCENT_THRESHOLD:
                    t["resultado"] = "GAIN"
                elif var <= -PERCENT_THRESHOLD:
                    t["resultado"] = "LOSS"
        await asyncio.sleep(60)

# ---------------- LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(avaliar_trades(session))
        while True:
            symbols = await get_top_symbols(session)
            if not symbols:
                await asyncio.sleep(10)
                continue
            await asyncio.gather(*(scan_symbol(session, s) for s in symbols))
            await asyncio.sleep(60)

def start_bot():
    asyncio.run(main_loop())

def run_flask():
    threading.Thread(target=start_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

if __name__ == "__main__":
    run_flask()
