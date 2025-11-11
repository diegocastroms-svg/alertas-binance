# main.py — V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA (Entrada Antecipada Real + DEBUG FILE)
# Correções: cooldown corrigido e limit=220 para EMA200 real.

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA (Entrada Antecipada Real + DEBUG FILE) ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 10_000_000
TOP_N = 50
COOLDOWN = 900
BOOK_DOM = 1.05
SCAN_INTERVAL = 30

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    try:
        await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                     timeout=10)
    except Exception as e:
        print("Erro Telegram:", e)

def ema(data, p):
    if not data: return []
    a = 2 / (p + 1)
    e = data[0]
    out = [e]
    for x in data[1:]:
        e = a * x + (1 - a) * e
        out.append(e)
    return out

def rsi(prices, p=14):
    if len(prices) < p + 1: return 50
    d = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(x, 0) for x in d[-p:]]
    losses = [abs(min(x, 0)) for x in d[-p:]]
    ag, al = sum(gains)/p, (sum(losses)/p or 1e-12)
    return 100 - 100 / (1 + ag / al)

def macd_virando(close):
    if len(close) < 26: return False, 0.0
    e12 = ema(close, 12)
    e26 = ema(close, 26)
    macd_line = e12[-1] - e26[-1]
    macd_series = [a - b for a, b in zip(e12, e26)]
    signal_series = ema(macd_series, 9)
    hist = macd_line - signal_series[-1]
    if len(macd_series) >= 2 and len(signal_series) >= 2:
        hist_prev = macd_series[-2] - signal_series[-2]
    else:
        hist_prev = hist
    return hist > hist_prev, hist

def vol_strength(vol):
    if len(vol) < 21: return 100.0
    ma9 = sum(vol[-9:]) / 9
    ma21 = sum(vol[-21:]) / 21
    base = (ma9 + ma21) / 2 or 1e-12
    return (vol[-1] / base) * 100.0

cooldown = {}
def ready_to_alert(sym):
    return (time.time() - cooldown.get(sym, 0) >= COOLDOWN)

def mark_alert(sym):
    cooldown[sym] = time.time()

async def klines(s, sym, tf):
    async with s.get(f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit=220", timeout=10) as r:
        return await r.json() if r.status == 200 else []

async def ticker(s, sym):
    async with s.get(f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}", timeout=10) as r:
        return await r.json() if r.status == 200 else None

async def scan_tf(s, sym, tf):
    try:
        t = await ticker(s, sym)
        if not t: return
        vol24 = float(t.get("quoteVolume", 0) or 0)
        if vol24 < MIN_VOL24: return
        k = await klines(s, sym, tf)
        if len(k) < 50: return

        close = [float(x[4]) for x in k]
        low = [float(x[3]) for x in k]
        vol = [float(x[5]) for x in k]

        ema9 = ema(close, 9)[-1] if len(close) >= 9 else 0.0
        ema20 = ema(close, 20)[-1] if len(close) >= 20 else 0.0
        ema200 = ema(close, 200)[-1] if len(close) >= 200 else (ema(close,100)[-1] if len(close)>=100 else 0.0)
        price = close[-1]
        r = rsi(close)
        hist_up, hist_val = macd_virando(close)
        vs = vol_strength(vol)

        taker_buy = float(t.get("takerBuyQuoteAssetVolume", 0) or 0.0)
        taker_sell = max(float(t.get("quoteVolume", 0) or 0.0) - taker_buy, 0.0)
        book_ok = (taker_buy >= taker_sell * BOOK_DOM) or (taker_buy == 0.0)

        nome = sym.replace("USDT", "")
        print(f"[{now_br()}] {tf.upper()} {nome} | Price {price:.6f} | EMA200 {ema200:.6f} | RSI {r:.1f} | Vol {vs:.0f}% | MACD {'↑' if hist_up else '↓'} | takerBuy {taker_buy:,.0f} | takerSell {taker_sell:,.0f} | Book {'OK' if book_ok else 'FRACO'}")

        price_vs_ema200 = price >= ema200 * 0.995 if ema200 else False
        reason_fail = []
        if not price_vs_ema200:
            reason_fail.append("price<EMA200")
        if not hist_up:
            reason_fail.append("MACD_não_virando")
        if not (45 <= r <= 60):
            reason_fail.append("RSI_fora_45-60")
        if not (vs >= 100):
            reason_fail.append("Vol_força<100%")
        if not book_ok:
            reason_fail.append("Book_fraco")
        if not ready_to_alert(sym):
            reason_fail.append("Cooldown")

        if price_vs_ema200 and hist_up and 45 <= r <= 60 and vs >= 100 and book_ok and ready_to_alert(sym):
            mark_alert(sym)
            msg = (
                f"⚡ <b>INÍCIO DE ROMPIMENTO ({tf.upper()})</b>\n\n"
                f"{nome}\n\n"
                f"Preço: <b>{price:.6f}</b>\n"
                f"RSI: <b>{r:.1f}</b> | MACD: <b>virando</b>\n"
                f"Vol força: <b>{vs:.0f}%</b>\n"
                f"Fluxo: <b>{taker_buy:,.0f}</b> vs <b>{taker_sell:,.0f}</b>\n"
                f"⏱ {now_br()} BR"
            )
            await tg(s, msg)
            print(f"[{now_br()}] ALERTA ENVIADO {tf.upper()} {nome} | reason=OK")
        else:
            print(f"[{now_br()}] SEM ALERTA {tf.upper()} {nome} | motivos: {', '.join(reason_fail)}")

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA (DEBUG ON)</b>")
        while True:
            try:
                data_resp = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if data_resp.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL); continue
                data = await data_resp.json()
                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume") or 0) >= MIN_VOL24
                    and not any(x in d["symbol"] for x in ["UP", "DOWN", "BUSD", "FDUSD", "USDC", "TUSD"])
                ]
                symbols = sorted(symbols, key=lambda x: next((float(t.get("quoteVolume") or 0) for t in data if t["symbol"] == x), 0), reverse=True)[:TOP_N]

                print(f"\n[{now_br()}] === Iniciando varredura ({len(symbols)} moedas) ===")
                tasks = [scan_tf(s, sym, tf) for sym in symbols for tf in ["15m", "30m", "1h"]]
                await asyncio.gather(*tasks)
                print(f"[{now_br()}] === Varredura finalizada ===\n")

            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
