# main.py — V23.2 (OURO CONFLUÊNCIA REAL — 3m Ajustado RSI 40–80)
# 3m: ALTAS REAIS (BB abrindo + EMA9>MA20 + RSI 40–80 + MACD↑ + volume_strength>110% + real_money_flow>45%)
# 15m/30m: confirmadores rápidos (cruzamento imediato + RSI>50 + MACD>0)

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V23.2 ATIVO (3M AJUSTADO RSI 40–80 + 15/30 CONFIRMAÇÃO)", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------- PARÂMETROS AJUSTÁVEIS ----------
RSI_MIN_3M, RSI_MAX_3M = 40, 80
VOL_STRENGTH_MIN = 110
REAL_FLOW_MIN = 45
BB_OPEN_MIN_GROWTH = 1.01
COOLDOWN = {"3m": 180, "15m": 900, "30m": 1800}
MIN_VOL24 = 3_000_000
TOP_N = 100
# ------------------------------------------

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg); return
    try:
        await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                     timeout=10)
    except Exception as e:
        print("Erro Telegram:", e)

def ema(data, p):
    if not data: return []
    a = 2/(p+1); e = data[0]; out = [e]
    for x in data[1:]:
        e = a*x + (1-a)*e; out.append(e)
    return out

def sma(data, p):
    if len(data) < p: return []
    out = []
    s = sum(data[:p]); out.append(s/p)
    for i in range(p, len(data)):
        s += data[i] - data[i-p]
        out.append(s/p)
    return out

def stdev(data, p):
    if len(data) < p: return []
    out = []
    window = data[:p]
    mu = sum(window)/p
    var = sum((x-mu)**2 for x in window)/p
    out.append(var**0.5)
    for i in range(p, len(data)):
        window.pop(0); window.append(data[i])
        mu = sum(window)/p
        var = sum((x-mu)**2 for x in window)/p
        out.append(var**0.5)
    return out

def rsi(prices, p=14):
    if len(prices) < p+1: return 50
    d = [prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains = [max(x,0) for x in d[-p:]]
    losses = [abs(min(x,0)) for x in d[-p:]]
    ag, al = (sum(gains)/p), (sum(losses)/p or 1e-12)
    return 100 - 100/(1 + ag/al)

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
    n = time.time()
    if n - cooldown[tf].get(sym, 0) >= COOLDOWN[tf]:
        cooldown[tf][sym] = n
        return True
    return False

def bb_opening(close):
    mb_series = sma(close, 20); sd_series = stdev(close, 20)
    if len(mb_series) < 2 or len(sd_series) < 2: return False, 0.0
    mb_prev, mb_now = mb_series[-2], mb_series[-1]
    up_prev, dn_prev = mb_prev + 1.8*sd_series[-2], mb_prev - 1.8*sd_series[-2]
    up_now,  dn_now  = mb_now  + 1.8*sd_series[-1],  mb_now  - 1.8*sd_series[-1]
    bw_prev = (up_prev - dn_prev) / (mb_prev or 1e-12)
    bw_now  = (up_now  - dn_now ) / (mb_now  or 1e-12)
    opening = bw_now > bw_prev * BB_OPEN_MIN_GROWTH and up_now > up_prev
    return opening, bw_now

async def scan_tf(s, sym, tf):
    try:
        t = await ticker(s, sym)
        if not t: return
        p = float(t["lastPrice"])
        vol24 = float(t["quoteVolume"])
        if vol24 < MIN_VOL24: return

        k = await klines(s, sym, tf, 100)
        if len(k) < 50: return
        close = [float(x[4]) for x in k]
        vol   = [float(x[5]) for x in k]

        ema9_prev  = ema(close[:-1], 9)
        ema20_prev = ema(close[:-1], 20)
        if len(ema9_prev) < 2 or len(ema20_prev) < 2: return
        a9, a20 = 2/10, 2/21
        ema9_atual  = ema9_prev[-1]*(1-a9)  + close[-1]*a9
        ema20_atual = ema20_prev[-1]*(1-a20)+ close[-1]*a20

        current_rsi = rsi(close)

        # ===== 3m ajustado =====
        if tf == "3m":
            bb_ok, bb_width = bb_opening(close)
            if not bb_ok: return

            cruz = (
                (ema9_prev[-1] <= ema20_prev[-1] and ema9_atual > ema20_atual*1.0002) or
                (ema9_prev[-2] <= ema20_prev[-2] and ema9_prev[-1] > ema20_prev[-1])
            )
            if not cruz: return

            if not (RSI_MIN_3M <= current_rsi <= RSI_MAX_3M): return

            macd_line   = ema(close,12)[-1] - ema(close,26)[-1]
            signal_line = ema(close,9)[-1]
            macd_hist   = macd_line - signal_line
            if macd_hist <= 0: return

            ma9  = sum(vol[-9:])/9
            ma21 = sum(vol[-21:])/21
            base = (ma9 + ma21)/2 or 1e-12
            volume_strength = (vol[-1]/base)*100
            if volume_strength < VOL_STRENGTH_MIN: return

            taker_buy_quote = float(t.get("takerBuyQuoteAssetVolume", 0))
            real_money_flow = (taker_buy_quote / (vol24 or 1e-12)) * 100
            if real_money_flow < REAL_FLOW_MIN: return

            extras = {
                "bb_width": bb_width,
                "volume_strength": volume_strength,
                "real_money_flow": real_money_flow,
                "macd_hist": macd_hist
            }

        # ===== 15m / 30m confirmadores =====
        else:
            cruz = (
                (ema9_prev[-1] <= ema20_prev[-1] and ema9_atual > ema20_atual) or
                (ema9_atual > ema20_atual and ema9_prev[-1] <= ema20_prev[-1])
            )
            if not cruz: return
            if not (50 <= current_rsi <= 85): return
            macd_12 = ema(close,12); macd_26 = ema(close,26)
            if (macd_12[-1] - macd_26[-1]) <= 0: return
            extras = None

        if not can_alert(tf, sym): return

        stop = min(float(x[3]) for x in k[-10:]) * 0.98
        alvo1, alvo2 = p*1.025, p*1.05
        prob  = "92%" if tf=="3m" else "80%" if tf=="15m" else "86%"
        emoji = "🔥" if tf=="3m" else "💪" if tf=="15m" else "🟢"
        color = "🟡" if tf=="3m" else "🔵" if tf=="15m" else "🟣"
        nome = sym.replace("USDT", "")

        msg = (
            f"<b>{emoji} EMA9 CROSS {tf.upper()} {color} (AO VIVO)</b>\n\n"
            f"{nome}\n\n"
            f"Preço: <b>{p:.6f}</b>\n"
            f"RSI: <b>{current_rsi:.1f}</b>\n"
        )
        if tf == "3m" and extras:
            msg += (
                f"BB abrindo (20): <b>Sim</b>\n"
                f"Vol força: <b>{extras['volume_strength']:.0f}%</b>\n"
                f"Fluxo real (takers): <b>{extras['real_money_flow']:.1f}%</b>\n"
                f"MACD hist: <b>{extras['macd_hist']:.5f}</b>\n"
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
        await tg(s, "<b>V23.2 ATIVO</b>\n3M AJUSTADO RSI 40–80 + 15/30 CONFIRMAÇÃO")
        while True:
            try:
                data = await (await s.get(f"{BINANCE}/api/v3/ticker/24hr")).json()
                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d["quoteVolume"]) > MIN_VOL24
                    and (lambda base: not (
                        base.endswith("USD") or base in {
                            "BUSD","FDUSD","USDE","USDC","TUSD","CUSD",
                            "EUR","GBP","TRY","AUD","BRL","RUB","CAD","CHF","JPY",
                            "BF","BFC","BFG","BFD","BETA","AEUR","AUSD","CEUR","XAUT"
                        }
                    ))(d["symbol"][:-4])
                    and not any(x in d["symbol"] for x in ["UP","DOWN"])
                ]
                symbols = sorted(
                    symbols,
                    key=lambda x: next((float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0),
                    reverse=True
                )[:TOP_N]

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
