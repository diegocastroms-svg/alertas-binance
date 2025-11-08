# main.py — V22.0 CURTO (5m, 15m, 30m) — CRUZAMENTO UNIFICADO E CONFIRMADO
import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading, statistics

app = Flask(__name__)
@app.route("/")
def home():
    return "V22.0 CURTO ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    try:
        await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
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
    g = [max(x, 0) for x in d[-p:]]
    l = [abs(min(x, 0)) for x in d[-p:]]
    ag, al = sum(g) / p, sum(l) / p or 1e-12
    return 100 - 100 / (1 + ag / al)

async def klines(s, sym, tf, lim=200):
    url = f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit={lim}"
    async with s.get(url, timeout=10) as r:
        return await r.json() if r.status == 200 else []

async def ticker(s, sym):
    url = f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}"
    async with s.get(url, timeout=10) as r:
        return await r.json() if r.status == 200 else None

cooldown_5m, cooldown_15m, cooldown_30m = {}, {}, {}

def can_alert(tf, sym):
    cd = cooldown_5m if tf == "5m" else cooldown_15m if tf == "15m" else cooldown_30m
    cooldown_time = 450 if tf == "5m" else 900 if tf == "15m" else 1800
    n = time.time()
    if n - cd.get(sym, 0) >= cooldown_time:
        cd[sym] = n
        return True
    return False

async def scan_tf(s, sym, tf):
    try:
        t = await ticker(s, sym)
        if not t: return
        p = float(t["lastPrice"])
        vol24 = float(t["quoteVolume"])
        if vol24 < 3_000_000: return

        k = await klines(s, sym, tf, 200)
        if len(k) < 50: return
        close = [float(x[4]) for x in k]

        ema9_vals = ema(close[:-1], 9)
        ema20_vals = ema(close[:-1], 20)
        if len(ema9_vals) < 2 or len(ema20_vals) < 2: return

        ema9 = ema9_vals[-1]
        ema20 = ema20_vals[-1]
        ema9_prev = ema9_vals[-2]
        ema20_prev = ema20_vals[-2]

        # --- CRUZAMENTO UNIFICADO E CONFIRMADO ---
        cruzamento = ema9_prev <= ema20_prev and ema9 > ema20 * 1.002
        if not cruzamento:
            return
        # confirmação de que ainda está acima
        if ema9 <= ema20:
            return
        # ------------------------------------------------

        # --- FILTRO 5M: BOLLINGER + MA200 PROXIMIDADE ---
        if tf == "5m":
            ma20 = sum(close[-20:]) / 20
            std = statistics.pstdev(close[-20:])
            largura = (2 * std) / ma20
            if largura > 0.08:
                return

            ma200 = sum(close[-200:]) / 200
            distancia = abs(p - ma200) / ma200
            if distancia > 0.02:
                return
        # -------------------------------------------------

        current_rsi = rsi(close)
        if current_rsi < 40 or current_rsi > 80: return

        if can_alert(tf, sym):
            stop = min(float(x[3]) for x in k[-10:]) * 0.98
            alvo1, alvo2 = p * 1.025, p * 1.05
            nome = sym[:-4]
            prob = "70%" if tf == "5m" else "78%" if tf == "15m" else "85%"
            emoji = "⚡" if tf == "5m" else "💫" if tf == "15m" else "💪"
            color = "🟣" if tf == "5m" else "🔵" if tf == "15m" else "🟢"

            msg = (
                f"<b>{emoji} TENDÊNCIA CURTA {tf.upper()} {color}</b>\n\n"
                f"<b>{nome}</b>\n\n"
                f"💰 Preço: <b>{p:.6f}</b>\n"
                f"📊 RSI: <b>{current_rsi:.1f}</b>\n"
                f"💵 Volume 24h: <b>${vol24:,.0f}</b>\n"
                f"📉 Stop: <b>{stop:.6f}</b>\n"
                f"🎯 Alvo +2.5%: <b>{alvo1:.6f}</b>\n"
                f"🏁 Alvo +5%: <b>{alvo2:.6f}</b>\n"
                f"📈 Prob: <b>{prob}</b>\n"
                f"<i>{now_br()} BR</i>"
            )
            await tg(s, msg)
    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V22.0 CURTO ATIVO</b>\n5M + 15M + 30M | TENDÊNCIA CURTA | CRUZAMENTO UNIFICADO 0.2% | BOLLINGER ≤8% | MA200 ±2% | NOMES SEM USDT")
        while True:
            try:
                data = await (await s.get(f"{BINANCE}/api/v3/ticker/24hr")).json()
                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d["quoteVolume"]) > 3_000_000
                    and not any(x in d["symbol"] for x in ["UP", "DOWN"])
                ]
                symbols = sorted(
                    symbols,
                    key=lambda x: next((float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0),
                    reverse=True
                )[:100]

                tasks = [scan_tf(s, sym, tf) for sym in symbols for tf in ["5m", "15m", "30m"]]
                await asyncio.gather(*tasks)
            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(60)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 10000)
    app.run(host="0.0.0.0", port=port)
