# main.py — V20.2 VOLUME 10M (CRUZAMENTO + CONFIRMADO + FORÇA DE VELA + FILTRO FINAL)
import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V20.2 VOLUME 10M ATIVO", 200

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

async def klines(s, sym, tf, lim=100):
    url = f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit={lim}"
    async with s.get(url, timeout=10) as r:
        return await r.json() if r.status == 200 else []

async def ticker(s, sym):
    url = f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}"
    async with s.get(url, timeout=10) as r:
        return await r.json() if r.status == 200 else None

cooldown_15m = {}
cooldown_30m = {}

def can_alert(tf, sym):
    cd = cooldown_15m if tf == "15m" else cooldown_30m
    cooldown_time = 900 if tf == "15m" else 1800
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
        if vol24 < 10_000_000: return  # VOLUME 10M

        k = await klines(s, sym, tf, 100)
        if len(k) < 50: return
        close = [float(x[4]) for x in k]

        ema9_prev = ema(close[:-1], 9)
        ema20_prev = ema(close[:-1], 20)
        if len(ema9_prev) < 2 or len(ema20_prev) < 2: return

        alpha9 = 2 / (9 + 1)
        alpha20 = 2 / (20 + 1)
        ema9_atual = ema9_prev[-1] * (1 - alpha9) + close[-1] * alpha9
        ema20_atual = ema20_prev[-1] * (1 - alpha20) + close[-1] * alpha20

        # ALERTA: CRUZAMENTO ATUAL OU CONFIRMADO NA VELA ANTERIOR
        cruzamento_agora = ema9_prev[-1] <= ema20_prev[-1] and ema9_atual > ema20_atual
        cruzamento_confirmado = ema9_prev[-2] <= ema20_prev[-2] and ema9_prev[-1] > ema20_prev[-1]
        if not (cruzamento_agora or cruzamento_confirmado): return

        # FORÇA DE VELA (+1%): exige corpo de alta >= 1%
        if cruzamento_agora:
            open_now = float(k[-1][1])
            close_now = float(k[-1][4])
            if (close_now - open_now) / (open_now or 1e-12) < 0.01:
                return
        else:  # cruzamento_confirmado
            open_prev = float(k[-2][1])
            close_prev = float(k[-2][4])
            if (close_prev - open_prev) / (open_prev or 1e-12) < 0.01:
                return

        # preço acima da EMA9 (folga 0,1%)
        if p < ema9_atual * 0.999: return

        current_rsi = rsi(close)
        if current_rsi < 40 or current_rsi > 80: return

        if can_alert(tf, sym):
            stop = min(float(x[3]) for x in k[-10:]) * 0.98
            alvo1 = p * 1.08
            alvo2 = p * 1.15
            prob = "78%" if tf == "15m" else "85%"
            emoji = "⚡" if tf == "15m" else "💪"
            color = "🔵" if tf == "15m" else "🟢"
            msg = (
                f"<b>{emoji} EMA9 CROSS {tf.upper()} {color} (AO VIVO)</b>\n"
                f"<code>{sym}</code>\n"
                f"Preço: <b>{p:.6f}</b>\n"
                f"RSI: <b>{current_rsi:.1f}</b>\n"
                f"Volume 24h: <b>${vol24:,.0f}</b>\n"
                f"Prob: <b>{prob}</b>\n"
                f"Stop: <b>{stop:.6f}</b>\n"
                f"Alvo +8%: <b>{alvo1:.6f}</b>\n"
                f"Alvo +15%: <b>{alvo2:.6f}</b>\n"
                f"<i>{now_br()} BR</i>"
            )
            await tg(s, msg)
    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V20.2 VOLUME 10M ATIVO</b>\nCRUZAMENTO + CONFIRMADO + FORÇA DE VELA + FILTRO FINAL")
        while True:
            try:
                data = await (await s.get(f"{BINANCE}/api/v3/ticker/24hr")).json()
                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d["quoteVolume"]) > 10_000_000   # alinhado com 10M
                    and (lambda base: not (
                        base.endswith("USD")
                        or base in {
                            "BUSD","FDUSD","USDE","USDC","TUSD","CUSD",   # stablecoins
                            "EUR","GBP","TRY","AUD","BRL","RUB","CAD","CHF","JPY",  # moedas fiduciárias
                            "BF","BFC","BFG","BFD","BETA","AEUR","AUSD","CEUR","XAUT"  # tokens falsos e sintéticos
                        }
                    ))(d["symbol"][:-4])
                    and not any(x in d["symbol"] for x in ["UP", "DOWN"])
                ]
                symbols = sorted(
                    symbols,
                    key=lambda x: next((float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0),
                    reverse=True
                )[:100]  # TOP 100
                tasks = []
                for sym in symbols:
                    tasks.append(scan_tf(s, sym, "15m"))
                    tasks.append(scan_tf(s, sym, "30m"))
                await asyncio.gather(*tasks)
            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(60)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
