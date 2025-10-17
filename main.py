# main.py — v3.0 (curtos intrabar + Flask) — somente 5m e 15m

import os, asyncio, time, math
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from urllib.parse import urlencode

import aiohttp
from flask import Flask

# =========================
# Config
# =========================
BINANCE_HTTP = "https://api.binance.com"
INTERVALS = ["5m", "15m"]
COOLDOWN_SEC = 15 * 60  # 15 min
SHORTLIST_N = 120
MIN_QV = 250_000.0      # quote volume 24h mínimo
MIN_PCT = 0.0           # sem filtro de pct 24h

EMA_FAST = 9
MA_20 = 20
MA_50 = 50
MA_200 = 200
RSI_LEN = 14
VOL_MA = 9

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# =========================
# Utilitários
# =========================
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

def fmt_symbol(sym):
    return sym[:-4] + "/USDT" if sym.endswith("USDT") else sym

def links(sym):
    base = sym.replace("USDT","")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

async def send_alert(session, text):
    # webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except: pass
    # Telegram
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except: pass

# =========================
# Indicadores simples
# =========================
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq: return []
    out=[]; a = 2.0/(span+1.0); e = seq[0]; out.append(e)
    for x in seq[1:]:
        e = a*x + (1-a)*e
        out.append(e)
    return out

def rsi_wilder(closes, period=14):
    n=len(closes)
    if n==0: return []
    deltas=[0.0]+[closes[i]-closes[i-1] for i in range(1,n)]
    gains=[max(d,0.0) for d in deltas]
    losses=[max(-d,0.0) for d in deltas]
    rsis=[50.0]*n
    if n<period+1: return rsis
    ag=sum(gains[1:period+1])/period
    al=sum(losses[1:period+1])/period
    for i in range(period+1,n):
        ag=(ag*(period-1)+gains[i])/period
        al=(al*(period-1)+losses[i])/period
        rs=ag/(al+1e-12)
        rsis[i]=100.0-(100.0/(1.0+rs))
    return rsis

# =========================
# Binance
# =========================
async def get_klines(session, symbol, interval="5m", limit=200):
    """
    IMPORTANTE: mantém a ÚLTIMA vela (em formação) para alertas intrabar.
    """
    params={"symbol":symbol, "interval":interval, "limit":limit}
    url=f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data=await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in data:  # inclui última vela
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(ticks, n=SHORTLIST_N):
    out=[]
    blocked=("UP","DOWN","BULL","BEAR","PERP","_PERP","_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_EUR","_TRY","_BRL")
    for t in ticks:
        s=t.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(b in s for b in blocked): continue
        qv=float(t.get("quoteVolume","0") or 0.0)
        pct=float(t.get("priceChangePercent","0") or 0.0)
        if qv>=MIN_QV and abs(pct)>=MIN_PCT:
            out.append((s, qv))
    out.sort(key=lambda x: x[1], reverse=True)
    return [s for s,_ in out[:n]]

# =========================
# Mensagens
# =========================
def msg_trend_start(symbol, tf, price):
    sym=fmt_symbol(symbol)
    return (f"🟢 {sym} — Tendência iniciando ({tf})\n"
            f"EMA9 cruzou MA20 e MA50 pra cima\n"
            f"💰 <code>{price:.6f}</code>\n"
            f"⏰ {now_br()}\n{links(symbol)}")

def msg_preconf_5m(symbol, price):
    sym=fmt_symbol(symbol)
    return (f"🟢 {sym} — Tendência pré-confirmada (5m)\n"
            f"Médias 20 e 50 cruzaram acima da MA200\n"
            f"💰 <code>{price:.6f}</code>\n"
            f"⏰ {now_br()}\n{links(symbol)}")

def msg_preconf_15m(symbol, price):
    sym=fmt_symbol(symbol)
    return (f"🟢 {sym} — Tendência pré-confirmada (15m)\n"
            f"EMA9 cruzou acima da MA200\n"
            f"💰 <code>{price:.6f}</code>\n"
            f"⏰ {now_br()}\n{links(symbol)}")

def msg_conf_15m(symbol, price):
    sym=fmt_symbol(symbol)
    return (f"🟢 {sym} — Tendência confirmada (15m)\n"
            f"MA20 e MA50 cruzaram acima da MA200\n"
            f"💰 <code>{price:.6f}</code>\n"
            f"⏰ {now_br()}\n{links(symbol)}")

# =========================
# Cooldown
# =========================
class Cool:
    def __init__(self): self.ts = defaultdict(lambda: 0.0)
    def allow(self, key): return time.time() - self.ts[key] >= COOLDOWN_SEC
    def mark(self, key): self.ts[key] = time.time()

cool = Cool()

# =========================
# Detecções de cruzamento
# =========================
def crossed_up(a_prev, a_now, b_prev, b_now):
    return a_prev <= b_prev and a_now > b_now

async def worker_tf(session, symbol, interval, drop_map):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=interval, limit=200)
        if len(c) < MA_200+5: return
        last = len(c)-1; prev = last-1

        ema9  = ema(c, EMA_FAST)
        ma20  = sma(c, MA_20)
        ma50  = sma(c, MA_50)
        ma200 = sma(c, MA_200)
        rsi14 = rsi_wilder(c, RSI_LEN)
        volma = sma(v, VOL_MA)

        price = c[last]

        # ---------------- 5m
        if interval == "5m":
            # Filtro "queda/lateralização": queda recente ou variação 24h negativa
            dropped = drop_map.get(symbol, False)

            # (1) Tendência iniciando (5m): EMA9 cruza MA20 e MA50 para cima (intrabar)
            a1 = crossed_up(ema9[prev], ema9[last], ma20[prev], ma20[last])
            a2 = crossed_up(ema9[prev], ema9[last], ma50[prev], ma50[last])
            if dropped and a1 and a2 and cool.allow((symbol,"START_5M")):
                await send_alert(session, msg_trend_start(symbol, "5m", price))
                cool.mark((symbol,"START_5M"))

            # (2) Pré-confirmada (5m): MA20 e MA50 cruzam acima da MA200
            b1 = crossed_up(ma20[prev], ma20[last], ma200[prev], ma200[last])
            b2 = crossed_up(ma50[prev], ma50[last], ma200[prev], ma200[last])
            if b1 and b2 and cool.allow((symbol,"PRE_5M")):
                await send_alert(session, msg_preconf_5m(symbol, price))
                cool.mark((symbol,"PRE_5M"))

        # ---------------- 15m
        elif interval == "15m":
            # (3) Pré-confirmada (15m): EMA9 cruza acima da MA200
            c1 = crossed_up(ema9[prev], ema9[last], ma200[prev], ma200[last])
            if c1 and cool.allow((symbol,"PRE_15M")):
                await send_alert(session, msg_preconf_15m(symbol, price))
                cool.mark((symbol,"PRE_15M"))

            # (4) Confirmada (15m): MA20 e MA50 cruzam acima da MA200
            d1 = crossed_up(ma20[prev], ma20[last], ma200[prev], ma200[last])
            d2 = crossed_up(ma50[prev], ma50[last], ma200[prev], ma200[last])
            if d1 and d2 and cool.allow((symbol,"CONF_15M")):
                await send_alert(session, msg_conf_15m(symbol, price))
                cool.mark((symbol,"CONF_15M"))

    except Exception as e:
        print("worker", symbol, interval, "err:", e)

async def main_loop():
    async with aiohttp.ClientSession() as session:
        # shortlist
        ticks = await get_24h(session)
        watch = shortlist_from_24h(ticks, SHORTLIST_N)

        # mapa de “queda/lateralização” simples via pct 24h negativo
        drop_map = {}
        for t in ticks:
            s=t.get("symbol","")
            if s in watch:
                try:
                    drop_map[s] = float(t.get("priceChangePercent","0") or 0.0) < 0.0
                except:
                    drop_map[s] = False

        hello = f"💻 v3.0 — cruzamentos intrabar 5m/15m | {len(watch)} pares SPOT | {now_br()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks=[]
            for s in watch:
                for tf in INTERVALS:
                    tasks.append(worker_tf(session, s, tf, drop_map))
            await asyncio.gather(*tasks)

            # refresh a cada 60s
            await asyncio.sleep(60)
            try:
                ticks = await get_24h(session)
                watch = shortlist_from_24h(ticks, SHORTLIST_N)
                drop_map = {}
                for t in ticks:
                    s=t.get("symbol","")
                    if s in watch:
                        try:
                            drop_map[s] = float(t.get("priceChangePercent","0") or 0.0) < 0.0
                        except:
                            drop_map[s] = False
            except Exception as e:
                print("refresh err:", e)

# =========================
# Bootstrap + Flask (Render web service)
# =========================
def start_bot():
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass

app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot de cruzamentos v3.0 ativo (5m/15m intrabar) — usando Flask para manter serviço no Render."

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
