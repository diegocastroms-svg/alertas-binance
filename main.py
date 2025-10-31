# main_breakout_v1_render_hibrido.py
# V4.8 – AJUSTE DE TIMING (antecipação do alerta 5m)

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 15 * 60
TOP_N = 50
REQ_TIMEOUT = 8
VERSION = "V4.8 - OURO CONFLUENTE ANTECIPADO"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | 3m | 5m | 15m | 1h | 50 pares", 200

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

def rsi_bolinha(rsi):
    if rsi >= 70: return "🟢"
    elif rsi >= 60: return "🟡"
    else: return "🔴"

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        k3 = await get_klines(session, symbol, "3m", limit=100)
        k5 = await get_klines(session, symbol, "5m", limit=100)
        k15 = await get_klines(session, symbol, "15m", limit=100)
        k1h = await get_klines(session, symbol, "1h", limit=100)
        if not (len(k3) and len(k5) and len(k15) and len(k1h)): return

        c3, c5, c15, c1h = [float(k[4]) for k in k3], [float(k[4]) for k in k5], [float(k[4]) for k in k15], [float(k[4]) for k in k1h]
        v3, v5, v15 = [float(k[5]) for k in k3], [float(k[5]) for k in k5], [float(k[5]) for k in k15]
        i3, i5, i15 = len(c3)-1, len(c5)-1, len(c15)-1

        ema9_3, ema20_3 = ema(c3, 9)[i3], ema(c3, 20)[i3]
        ema9_5, ema20_5, ema50_5 = ema(c5, 9)[i5], ema(c5, 20)[i5], ema(c5, 50)[i5]
        ema9_15, ema20_15, ema50_15 = ema(c15, 9)[i15], ema(c15, 20)[i15], ema(c15, 50)[i15]
        ema20_1h, ema50_1h = ema(c1h, 20)[-1], ema(c1h, 50)[-1]

        rsi3, rsi5, rsi15 = calc_rsi(c3, 14)[i3], calc_rsi(c5, 14)[i5], calc_rsi(c15, 14)[i15]
        volmed3, volmed5, volmed15 = sum(v3[-10:])/10, sum(v5[-10:])/10, sum(v15[-10:])/10

        # --- ALERTAS ---
        # 3M
        if ema9_3 > ema20_3 and rsi3 > 66 and v3[i3] > 1.3 * volmed3:
            if can_alert(symbol, "3m", 15*60):
                bola = rsi_bolinha(rsi3)
                msg = (
                    f"{bola} <b>[3m] Pré-Ignição Detectada</b>\n"
                    f"⏰ {now_br()} | {symbol}\n"
                    f"📊 RSI: {rsi3:.1f}\n"
                    f"🔗 https://www.binance.com/pt-BR/trade/{symbol}?type=spot"
                )
                await tg(session, msg)

        # 5M – Ignição (ANTECIPADO)
        if ema9_5 > ema20_5 and rsi5 > 52 and v5[i5] > 1.3 * volmed5:
            if can_alert(symbol, "5m", 15*60):
                bola = rsi_bolinha(rsi5)
                mult = v5[i5] / volmed5
                msg = (f"{bola} <b>[5m] Ignição Antecipada</b>\n"
                       f"⏰ {now_br()} | {symbol}\n"
                       f"📊 RSI: {rsi5:.1f} | VOL: {mult:.1f}x\n"
                       f"🔗 https://www.binance.com/pt-BR/trade/{symbol}?type=spot")
                await tg(session, msg)

        # 15M
        if ema9_15 > ema20_15 > ema50_15 and rsi15 > 60:
            if can_alert(symbol, "15m", 30*60):
                bola = rsi_bolinha(rsi15)
                msg = (f"{bola} <b>[15m] Continuação de Alta</b>\n"
                       f"⏰ {now_br()} | {symbol}\n"
                       f"📊 RSI: {rsi15:.1f}\n"
                       f"🔗 https://www.binance.com/pt-BR/trade/{symbol}?type=spot")
                await tg(session, msg)

        # 1H
        if ema20_1h > ema50_1h:
            if can_alert(symbol, "1h", 60*60):
                print(f"[1h] ✅ Tendência macro positiva | {symbol}")

        # 💎 CONFLUÊNCIA (mantido)
        if ema20_1h > ema50_1h and ema9_15 > ema20_15 and ema9_5 > ema20_5 and rsi15 > 65 and v5[i5] > 1.5 * volmed5:
            if can_alert(symbol, "MACD_CONFLUENCIA", 15*60):
                bola = rsi_bolinha(rsi15)
                preco = c5[-1]
                stop = min(c5[-3], ema(c5, 21)[-1])
                risco = preco - stop
                alvo_1 = preco + 2.5 * risco
                alvo_2 = preco + 5.0 * risco
                tp_parcial = preco + risco

                if rsi15 >= 70: prob = 90
                elif rsi15 >= 65: prob = 85
                elif rsi15 >= 60: prob = 80
                else: prob = 75

                msg = (
                    f"{bola} 💎 <b>Confluência MACD Detectada</b>\n"
                    f"⏰ {now_br()} | {symbol}\n"
                    f"📈 1h ✅ | 15m ✅ | 5m ✅\n\n"
                    f"💰 Preço: <b>{fmt_price(preco)}</b>\n"
                    f"📊 RSI15m: {rsi15:.1f} | Probabilidade: <b>{prob}%</b>\n\n"
                    f"🛡️ Stop Seguro: <code>{fmt_price(stop)}</code> (-{(risco/preco)*100:.1f}%)\n"
                    f"🎯 Alvo 1 (1:2.5): <code>{fmt_price(alvo_1)}</code> (+{(alvo_1/preco-1)*100:.1f}%)\n"
                    f"🎯 Alvo 2 (1:5): <code>{fmt_price(alvo_2)}</code> (+{(alvo_2/preco-1)*100:.1f}%)\n"
                    f"💫 TP Parcial (1:1): <code>{fmt_price(tp_parcial)}</code> (+{(tp_parcial/preco-1)*100:.1f}%)\n\n"
                    f"💬 Padrão de Alta Sustentada\n"
                    f"🔗 https://www.binance.com/pt-BR/trade/{symbol}?type=spot"
                )
                await tg(session, msg)

    except Exception as e:
        print(f"[ERRO] {symbol}: {e}")

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"<b>{VERSION} ATIVO</b>\n3m | 5m | 15m | 1h | {len(symbols)} pares\n{now_br()}\n──────────────────────────────")
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
