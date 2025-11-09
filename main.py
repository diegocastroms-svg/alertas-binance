# main.py — V22.4 (3M FORÇA REAL + 15M/30M SINCRONIZADOS)
import os, asyncio, aiohttp, time, math
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V22.4 (3M FORÇA REAL + 15M/30M SINCRONIZADOS) ATIVO", 200

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
        await s.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print("Erro Telegram:", e)

def ema(data, p):
    if not data:
        return []
    a = 2 / (p + 1)
    e = data[0]
    out = [e]
    for x in data[1:]:
        e = a * x + (1 - a) * e
        out.append(e)
    return out

def rsi(prices, p=14):
    if len(prices) < p + 1:
        return 50
    d = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
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

cooldown = {"3m": {}, "15m": {}, "30m": {}}

def can_alert(tf, sym):
    cd = cooldown[tf]
    n = time.time()
    cooldown_time = 120 if tf == "3m" else 300 if tf == "15m" else 900
    if n - cd.get(sym, 0) >= cooldown_time:
        cd[sym] = n
        return True
    return False

async def scan_tf(s, sym, tf):
    try:
        t = await ticker(s, sym)
        if not t:
            return
        p = float(t["lastPrice"])
        vol24 = float(t["quoteVolume"])
        if vol24 < 3_000_000:
            return

        k = await klines(s, sym, tf, 100)
        if len(k) < 50:
            return
        close = [float(x[4]) for x in k]
        vol = [float(x[5]) for x in k]

        ema9_prev = ema(close[:-1], 9)
        ema20_prev = ema(close[:-1], 20)
        if len(ema9_prev) < 2 or len(ema20_prev) < 2:
            return

        alpha9 = 2 / (9 + 1)
        alpha20 = 2 / (20 + 1)
        ema9_atual = ema9_prev[-1] * (1 - alpha9) + close[-1] * alpha9
        ema20_atual = ema20_prev[-1] * (1 - alpha20) + close[-1] * alpha20

        current_rsi = rsi(close)
        if not (35 <= current_rsi <= 88):
            return

        # 🔹 BLOCO ESPECIAL PARA 3M — leitura real de força
        if tf == "3m":
            ma9 = sum(vol[-9:]) / 9
            ma21 = sum(vol[-21:]) / 21
            avg_vol = (ma9 + ma21) / 2
            volume_strength = (vol[-1] / avg_vol) * 100 if avg_vol else 0

            macd_line = ema(close, 12)[-1] - ema(close, 26)[-1]
            signal_line = ema(close, 9)[-1]
            macd_hist = macd_line - signal_line
            momentum_confluence = (current_rsi / 100) * (1 if macd_hist > 0 else 0) * (volume_strength / 100)

            taker_buy = float(t.get("takerBuyQuoteAssetVolume", 0))
            taker_sell = vol24 - taker_buy
            real_money_flow = (taker_buy / (taker_buy + taker_sell) * 100) if (taker_buy + taker_sell) > 0 else 50

            if volume_strength < 120 or momentum_confluence < 0.5 or real_money_flow < 55:
                return

            cruzamento_valido = (
                (ema9_prev[-1] <= ema20_prev[-1] and ema9_atual > ema20_atual * 1.0002)
                or (ema9_prev[-2] <= ema20_prev[-2] and ema9_prev[-1] > ema20_prev[-1])
            )

        # 🔸 BLOCO PARA 15M / 30M — sincronismo imediato
        else:
            cruzamento_valido = (
                (ema9_prev[-1] <= ema20_prev[-1] and ema9_atual > ema20_atual)
                or (ema9_atual > ema20_atual and ema9_prev[-1] <= ema20_prev[-1])
            )

        if not cruzamento_valido:
            return

        if can_alert(tf, sym):
            stop = min(float(x[3]) for x in k[-10:]) * 0.98
            alvo1 = p * 1.025
            alvo2 = p * 1.05
            prob = "90%" if tf == "3m" else "78%" if tf == "15m" else "85%"
            emoji = "🔥" if tf == "3m" else "💪" if tf == "15m" else "🟢"
            color = "🟡" if tf == "3m" else "🔵" if tf == "15m" else "🟢"

            nome = sym.replace("USDT", "")
            msg = (
                f"<b>{emoji} EMA9 CROSS {tf.upper()} {color} (AO VIVO)</b>\n\n"
                f"{nome}\n\n"
                f"Preço: <b>{p:.6f}</b>\n"
                f"RSI: <b>{current_rsi:.1f}</b>\n"
            )

            if tf == "3m":
                msg += (
                    f"Volume força: <b>{volume_strength:.0f}%</b>\n"
                    f"Confluência: <b>{momentum_confluence:.2f}</b>\n"
                    f"Fluxo real: <b>{real_money_flow:.1f}% compradores</b>\n"
                )

            msg += (
                f"Volume 24h: <b>${vol24:,.0f}</b>\n"
                f"Prob: <b>{prob}</b>\n"
                f"Stop: <b>{stop:.6f}</b>\n"
                f"Alvo +2.5%: <b>{alvo1:.6f}</b>\n"
                f"Alvo +5%: <b>{alvo2:.6f}</b>\n"
                f"<i>{now_br()} BR</i>"
            )
            await tg(s, msg)

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V22.4 (3M FORÇA REAL + 15M/30M SINCRONIZADOS) ATIVO</b>")
        while True:
            try:
                data = await (await s.get(f"{BINANCE}/api/v3/ticker/24hr")).json()
                symbols = [
                    d["symbol"]
                    for d in data
                    if (
                        d["symbol"].endswith("USDT")
                        and float(d["quoteVolume"]) > 3_000_000
                        and not any(
                            x in d["symbol"]
                            for x in [
                                "UP","DOWN","BUSD","FDUSD","USDE","USDC","TUSD","CUSD",
                                "AEUR","AUSD","CEUR","EUR","GBP","TRY","AUD","BRL","RUB",
                                "CAD","CHF","JPY","XAUT","BF","BETA"
                            ]
                        )
                    )
                ]
                symbols = sorted(
                    symbols,
                    key=lambda x: next(
                        (float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0
                    ),
                    reverse=True,
                )[:100]

                tasks = []
                for sym in symbols:
                    tasks.append(scan_tf(s, sym, "3m"))
                    tasks.append(scan_tf(s, sym, "15m"))
                    tasks.append(scan_tf(s, sym, "30m"))
                await asyncio.gather(*tasks)
            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(60)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
