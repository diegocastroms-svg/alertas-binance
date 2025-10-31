# main_breakout_v1_render_hibrido.py
# V5.2 – OURO CONFLUÊNCIA TOTAL (RSI 45–68)

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 15 * 60
TOP_N = 50
REQ_TIMEOUT = 8
VERSION = "V5.2 - OURO CONFLUÊNCIA TOTAL"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | 3m | 5m | 15m | 30m | 1h | 50 pares", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " BR"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        print(f"[TG] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=REQ_TIMEOUT)
    except Exception as e:
        print(f"[TG ERRO] {e}")

def fmt_price(x: float) -> str:
    return f"{x:.8f}".rstrip("0").rstrip(".") or "0"

def ema(seq, span):
    if not seq: return []
    alpha = 2.0 / (span + 1.0)
    out = [seq[0]]
    e = seq[0]
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def calc_rsi(seq, period=14):
    if len(seq) < period + 1: return [50.0] * len(seq)
    gains, losses = [], []
    for i in range(1, len(seq)):
        diff = seq[i] - seq[i-1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    rsi = []
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / (avg_loss + 1e-12)
    rsi.append(100 - (100 / (1 + rs)))
    for i in range(period, len(seq) - 1):
        diff = seq[i] - seq[i-1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi.append(100 - (100 / (1 + rs)))
    return [50.0] * (len(seq) - len(rsi)) + rsi

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=100):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
            return data if isinstance(data, list) and len(data) > 0 else []
    except:
        return []

async def get_top_usdt_symbols(session):
    try:
        url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
        blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","EUR","BRL","PERP","TEST")
        pares = []
        for d in data:
            s = d.get("symbol", "")
            if not s.endswith("USDT"): continue
            if any(x in s for x in blocked): continue
            qv = float(d.get("quoteVolume", 0) or 0)
            pares.append((s, qv))
        pares.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in pares[:TOP_N]]
    except:
        return []

# ---------------- COOLDOWNS ----------------
cooldowns = {}
def can_alert(symbol, tipo, cooldown_sec):
    now = time.time()
    key = f"{symbol}_{tipo}"
    last = cooldowns.get(key, 0)
    if now - last > cooldown_sec:
        cooldowns[key] = now
        return True
    return False

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        k3 = await get_klines(session, symbol, "3m", 100)
        k5 = await get_klines(session, symbol, "5m", 100)
        k15 = await get_klines(session, symbol, "15m", 100)
        k30 = await get_klines(session, symbol, "30m", 100)
        if not (len(k3) and len(k5) and len(k15) and len(k30)): return

        c3 = [float(k[4]) for k in k3]
        c5 = [float(k[4]) for k in k5]
        c15 = [float(k[4]) for k in k15]
        c30 = [float(k[4]) for k in k30]
        o3 = [float(k[1]) for k in k3]
        o5 = [float(k[1]) for k in k5]
        o15 = [float(k[1]) for k in k15]
        v3 = [float(k[5]) for k in k3]
        v5 = [float(k[5]) for k in k5]
        v15 = [float(k[5]) for k in k15]
        v30 = [float(k[5]) for k in k30]

        i3, i5, i15, i30 = len(c3)-1, len(c5)-1, len(c15)-1, len(c30)-1
        volmed3, volmed5, volmed15 = sum(v3[-10:])/10, sum(v5[-10:])/10, sum(v15[-10:])/10

        # 3M – Cruzamento 9/20
        if (
            ema(c3,9)[i3-1] < ema(c3,20)[i3-1]
            and ema(c3,9)[i3] > ema(c3,20)[i3]
            and v3[i3] > 1.3 * volmed3
            and c3[i3] > o3[i3]
        ):
            if can_alert(symbol, "3m_cruzamento", 15*60):
                msg = (
                    f"⚡ <b>[3m] Cruzamento 9/20 Detectado</b>\n"
                    f"{symbol} | RSI e Volume crescentes\n"
                    f"⏰ {now_br()}\n"
                    f"🔗 https://www.binance.com/pt-BR/trade/{symbol}?type=spot"
                )
                await tg(session, msg)

        # 5M – Cruzamento 20/50
        if (
            ema(c5,20)[i5-1] < ema(c5,50)[i5-1]
            and ema(c5,20)[i5] > ema(c5,50)[i5]
            and v5[i5] > 1.3 * volmed5
            and c5[i5] > o5[i5]
        ):
            if can_alert(symbol, "5m_cruzamento", 15*60):
                msg = (
                    f"🟢 <b>[5m] Cruzamento 20/50 Confirmado</b>\n"
                    f"{symbol} | Tendência curta virando pra alta\n"
                    f"⏰ {now_br()}\n"
                    f"🔗 https://www.binance.com/pt-BR/trade/{symbol}?type=spot"
                )
                await tg(session, msg)

        # 15M – Continuação (EMA9>20>50)
        if ema(c15,9)[i15] > ema(c15,20)[i15] > ema(c15,50)[i15]:
            if can_alert(symbol, "15m_tendencia", 15*60):
                msg = (
                    f"📈 <b>[15m] Continuação de Alta</b>\n"
                    f"{symbol} | Médias alinhadas 9>20>50\n"
                    f"⏰ {now_br()}\n"
                    f"🔗 https://www.binance.com/pt-BR/trade/{symbol}?type=spot"
                )
                await tg(session, msg)

        # 💎 CONFLUÊNCIA TOTAL MACD (RSI 15m 45–68)
        rsi15 = calc_rsi(c15,14)[i15]
        if (
            ema(c3,9)[i3] > ema(c3,20)[i3]
            and ema(c5,9)[i5] > ema(c5,20)[i5]
            and ema(c15,9)[i15] > ema(c15,20)[i15]
            and ema(c30,9)[i30] > ema(c30,20)[i30]
            and 45 <= rsi15 <= 68
        ):
            if can_alert(symbol, "CONFLUENCIA_TOTAL", 15*60):
                preco = c5[-1]
                stop = min(c5[-3], ema(c5,21)[-1])
                risco = preco - stop
                alvo_1 = preco + 2.5 * risco
                alvo_2 = preco + 5.0 * risco
                tp_parcial = preco + risco
                msg = (
                    f"💎 <b>Confluência Total MACD</b>\n"
                    f"{symbol}\n"
                    f"3m✅ 5m✅ 15m✅ 30m✅\n"
                    f"RSI15: {rsi15:.1f}\n\n"
                    f"💰 Preço: {fmt_price(preco)}\n"
                    f"🛡️ Stop: {fmt_price(stop)}\n"
                    f"🎯 Alvo1: {fmt_price(alvo_1)} (1:2.5)\n"
                    f"🎯 Alvo2: {fmt_price(alvo_2)} (1:5)\n"
                    f"💫 Parcial: {fmt_price(tp_parcial)} (1:1)\n\n"
                    f"⏰ {now_br()}\n"
                    f"🔗 https://www.binance.com/pt-BR/trade/{symbol}?type=spot"
                )
                await tg(session, msg)

    except Exception as e:
        print(f"[ERRO] {symbol}: {e}")

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"<b>{VERSION} ATIVO</b>\n3m | 5m | 15m | 30m | 1h | {len(symbols)} pares\n{now_br()}\n──────────────────────────────")
        while True:
            await asyncio.gather(*[scan_symbol(session, s) for s in symbols])
            await asyncio.sleep(15)

def start_bot():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception as e:
            print(f"[LOOP ERRO] {e}")
            time.sleep(5)

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
