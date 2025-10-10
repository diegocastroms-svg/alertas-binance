# main_v2_0.py
# Rollback estável (curtos simples e sem atraso).
# 5m: Início (EMA9 cruza MA20/MA50 após lateralização)
# 15m: Pré (EMA9>MA200) e Confirmada (EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25)
# 15m: Retestes (EMA9/MA20)
# 5m: Rompimento resistência (Donchian-20)
# 1h/4h: Pré e Confirmadas (básicas)
# Flask + SPOT filter + cooldowns

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- CONFIG -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_5M  = "5m"
INTERVAL_15M = "15m"
INTERVAL_1H  = "1h"
INTERVAL_4H  = "4h"

SHORTLIST_N = 80
COOLDOWN_SHORT = 15 * 60   # curtos (5m/15m)
COOLDOWN_LONG  = 60 * 60   # longos (1h/4h)
MIN_PCT = 1.0
MIN_QV  = 300_000.0

EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
ADX_LEN  = 14
VOL_MA   = 9
DON_N    = 20

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID        = os.getenv("CHAT_ID","").strip()

# ----------------- UTILS -----------------
def ts_br():
    return (datetime.now(timezone.utc)-timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

def fmt_symbol(s):
    return s[:-4] + "/USDT" if s.endswith("USDT") else s

def links(s):
    base = s.replace("USDT","")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

async def send_alert(session, html):
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            await session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": html, "parse_mode":"HTML", "disable_web_page_preview": True},
                timeout=10
            )
        except:
            pass

# ----------------- INDICADORES -----------------
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s+=x
        if len(q)>n: s-=q.popleft()
        out.append(s/len(q))
    return out

def ema(seq, span):
    if not seq: return []
    out=[]; a=2/(span+1); e=seq[0]; out.append(e)
    for x in seq[1:]:
        e = a*x + (1-a)*e
        out.append(e)
    return out

def rsi_wilder(c, period=14):
    if len(c)<period+1: return [50.0]*len(c)
    deltas=[0.0]+[c[i]-c[i-1] for i in range(1,len(c))]
    gains=[max(d,0.0) for d in deltas]
    losses=[max(-d,0.0) for d in deltas]
    rsis=[50.0]*len(c)
    avg_gain=sum(gains[1:period+1])/period
    avg_loss=sum(losses[1:period+1])/period
    for i in range(period+1,len(c)):
        avg_gain=(avg_gain*(period-1)+gains[i])/period
        avg_loss=(avg_loss*(period-1)+losses[i])/period
        rs=avg_gain/(avg_loss+1e-12)
        rsis[i]=100.0-(100.0/(1.0+rs))
    return rsis

def true_range(h,l,c):
    tr=[0.0]
    for i in range(1,len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return tr

def adx(h,l,c,period=14):
    n=len(c)
    if n<period+1: return [20.0]*n, [0.0]*n, [0.0]*n
    tr=true_range(h,l,c)
    plus_dm=[0.0]; minus_dm=[0.0]
    for i in range(1,n):
        up=h[i]-h[i-1]; dn=l[i-1]-l[i]
        plus_dm.append(up if (up>dn and up>0) else 0.0)
        minus_dm.append(dn if (dn>up and dn>0) else 0.0)
    atr=[0.0]*n; atr[period]=sum(tr[1:period+1])
    pdm=[0.0]*n; mdm=[0.0]*n
    pdm[period]=sum(plus_dm[1:period+1]); mdm[period]=sum(minus_dm[1:period+1])
    for i in range(period+1,n):
        atr[i]=atr[i-1]-(atr[i-1]/period)+tr[i]
        pdm[i]=pdm[i-1]-(pdm[i-1]/period)+plus_dm[i]
        mdm[i]=mdm[i-1]-(mdm[i-1]/period)+minus_dm[i]
    plus_di=[0.0]*n; minus_di=[0.0]*n
    for i in range(n):
        plus_di[i]=100.0*(pdm[i]/(atr[i]+1e-12))
        minus_di[i]=100.0*(mdm[i]/(atr[i]+1e-12))
    dx=[0.0]*n
    for i in range(n):
        dx[i]=100.0*abs(plus_di[i]-minus_di[i])/(plus_di[i]+minus_di[i]+1e-12)
    adx_vals=[0.0]*n; adx_vals[period]=sum(dx[1:period+1])/period
    for i in range(period+1,n):
        adx_vals[i]=(adx_vals[i-1]*(period-1)+dx[i])/period
    for i in range(period):
        adx_vals[i]=adx_vals[period]
    return adx_vals, plus_di, minus_di

def compute_all(o,h,l,c,v):
    ema9=ema(c,EMA_FAST); ma20=sma(c,MA_SLOW); ma50=sma(c,MA_MED); ma200=sma(c,MA_LONG)
    rsi14=rsi_wilder(c,RSI_LEN); vol_ma=sma(v,VOL_MA)
    adx14, pdi, mdi = adx(h,l,c,ADX_LEN)
    return ema9, ma20, ma50, ma200, rsi14, vol_ma, adx14, pdi, mdi

# ----------------- DATA -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    url=f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status(); data=await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:  # remove candle em formação
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status(); return await r.json()

def shortlist_from_24h(tickers, n=80):
    usdt=[]
    for t in tickers:
        s=t.get("symbol","")
        if not s.endswith("USDT"): continue
        # SPOT-only (remove UP/DOWN alavancados e “perp-like”)
        blocked=("UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD",
                 "_BUSD","_FDUSD","_TUSD","_USDC","_BTC","_EUR","_TRY","_BRL")
        if any(x in s for x in blocked): continue
        try:
            pct=float(t.get("priceChangePercent","0") or 0.0)
            qv =float(t.get("quoteVolume","0") or 0.0)
        except:
            continue
        if abs(pct)>=MIN_PCT and qv>=MIN_QV:
            usdt.append((s,pct,qv))
    usdt.sort(key=lambda x:(abs(x[1]),x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- MONITOR -----------------
class Monitor:
    def __init__(self):
        self.cd = defaultdict(lambda: 0.0)       # curto (5m/15m) por tipo
        self.cd_long = defaultdict(lambda: 0.0)  # longo (1h/4h) por símbolo

    def allowed(self, key, secs=COOLDOWN_SHORT):
        return time.time() - self.cd[key] >= secs
    def mark(self, key):
        self.cd[key] = time.time()

    def allowed_long(self, sym):
        return time.time() - self.cd_long[sym] >= COOLDOWN_LONG
    def mark_long(self, sym):
        self.cd_long[sym] = time.time()

monitor = Monitor()

# ----------------- WORKERS (CURTOS) -----------------
async def worker_5m(session, s):
    try:
        o,h,l,c,v = await get_klines(session, s, INTERVAL_5M, 200)
        if len(c)<60: return
        ema9, ma20, ma50, ma200, rsi14, vol_ma, adx14, pdi, mdi = compute_all(o,h,l,c,v)
        i=len(c)-1; price=c[i]

        # 🔹 Lateralização simples: range estreito nas últimas 10 velas
        rng = max(h[i-9:i+1]) - min(l[i-9:i+1]) if i>=9 else 0.0
        lat_ok = rng/max(1e-12, c[i]) < 0.01  # ~1%

        # 🚀 Tendência Iniciando (5m): EMA9 cruza acima de MA20 e MA50 após lateralização
        if (lat_ok and ema9[i-1] < ma20[i-1] and ema9[i] > ma20[i] and ema9[i] > ma50[i] and
            rsi14[i] >= 45.0 and v[i] >= vol_ma[i] and monitor.allowed((s,"START_5M"))):
            msg = (
                f"🟢 {fmt_symbol(s)} ⬆️ <b>TENDÊNCIA INICIANDO (5m)</b>\n"
                f"💰 <code>{price:.6f}</code>\n"
                f"🧠 EMA9 cruzou acima de MA20 e MA50 após lateralização | RSI {rsi14[i]:.1f}\n"
                f"⏰ {ts_br()}\n{links(s)}"
            )
            await send_alert(session, msg); monitor.mark((s,"START_5M"))

        # 📈 Rompimento de resistência (Donchian-20)
        if i>=DON_N and monitor.allowed((s,"BREAK_5M")):
            dh = max(h[i-DON_N+1:i+1])
            if c[i] > dh:
                msg = (
                    f"📈 {fmt_symbol(s)} — <b>ROMPIMENTO DA RESISTÊNCIA (5m)</b>\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 Fechou acima da máxima {DON_N}\n"
                    f"⏰ {ts_br()}\n{links(s)}"
                )
                await send_alert(session, msg); monitor.mark((s,"BREAK_5M"))

    except Exception as e:
        print("worker_5m error", s, e)

async def worker_15m(session, s):
    try:
        o,h,l,c,v = await get_klines(session, s, INTERVAL_15M, 200)
        if len(c)<60: return
        ema9, ma20, ma50, ma200, rsi14, vol_ma, adx14, pdi, mdi = compute_all(o,h,l,c,v)
        i=len(c)-1; price=c[i]

        # 🌕 Pré-confirmada (15m): EMA9 cruza acima da MA200
        if (ema9[i-1] <= ma200[i-1] and ema9[i] > ma200[i] and rsi14[i] >= 50.0 and
            v[i] >= vol_ma[i] and monitor.allowed((s,"PRECONF_15"))):
            msg = (
                f"🟢 {fmt_symbol(s)} ⬆️ <b>TENDÊNCIA PRÉ-CONFIRMADA (15m)</b>\n"
                f"💰 <code>{price:.6f}</code>\n"
                f"🧠 EMA9 cruzou acima da MA200 | RSI {rsi14[i]:.1f}\n"
                f"⏰ {ts_br()}\n{links(s)}"
            )
            await send_alert(session, msg); monitor.mark((s,"PRECONF_15"))

        # 💎 Confirmada (15m): EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25
        if (ema9[i] > ma20[i] > ma50[i] > ma200[i] and rsi14[i] > 55.0 and adx14[i] > 25.0 and
            monitor.allowed((s,"CONF_15"))):
            msg = (
                f"💎 {fmt_symbol(s)} — <b>TENDÊNCIA CONFIRMADA (15m)</b>\n"
                f"💰 <code>{price:.6f}</code>\n"
                f"🧠 EMA9>MA20>MA50>MA200 | RSI {rsi14[i]:.1f} | ADX {adx14[i]:.1f}\n"
                f"⏰ {ts_br()}\n{links(s)}"
            )
            await send_alert(session, msg); monitor.mark((s,"CONF_15"))

        # ♻️ Reteste EMA9 (15m)
        if (l[i] <= ema9[i] and c[i] >= ema9[i] and ema9[i] > ma20[i] > ma50[i] and
            monitor.allowed((s,"RETEST_9_15"))):
            msg = (
                f"♻️ {fmt_symbol(s)} — <b>RETESTE EMA9 (15m)</b>\n"
                f"💰 <code>{price:.6f}</code>\n"
                f"🧠 Toque na EMA9 + reação\n"
                f"⏰ {ts_br()}\n{links(s)}"
            )
            await send_alert(session, msg); monitor.mark((s,"RETEST_9_15"))

        # ♻️ Reteste MA20 (15m)
        if (l[i] <= ma20[i] and c[i] >= ma20[i] and ma20[i] > ma50[i] and
            monitor.allowed((s,"RETEST_20_15"))):
            msg = (
                f"♻️ {fmt_symbol(s)} — <b>RETESTE MA20 (15m)</b>\n"
                f"💰 <code>{price:.6f}</code>\n"
                f"🧠 Toque na MA20 + reação\n"
                f"⏰ {ts_br()}\n{links(s)}"
            )
            await send_alert(session, msg); monitor.mark((s,"RETEST_20_15"))

    except Exception as e:
        print("worker_15m error", s, e)

# ----------------- WORKERS (LONGOS) -----------------
async def worker_1h(session, s):
    try:
        o,h,l,c,v = await get_klines(session, s, INTERVAL_1H, 200)
        if len(c)<60: return
        ema9, ma20, ma50, ma200, rsi14, vol_ma, adx14, pdi, mdi = compute_all(o,h,l,c,v)
        i=len(c)-1; price=c[i]

        # 🌕 Pré-confirmação (1h): EMA9 > MA20 + RSI>50
        if (ema9[i-1] <= ma20[i-1] and ema9[i] > ma20[i] and rsi14[i] > 50.0 and
            monitor.allowed_long(s)):
            msg = (
                f"🌕 <b>{fmt_symbol(s)} — PRÉ-CONFIRMAÇÃO (1h)</b>\n"
                f"<b>💰</b> <code>{price:.6f}</code>\n"
                f"<b>🧠</b> EMA9 cruzou MA20 | RSI {rsi14[i]:.1f}\n"
                f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
            )
            await send_alert(session, msg); monitor.mark_long(s)

        # 🚀 Confirmada (1h): EMA9>MA20>MA50 + RSI>55 + ADX>25
        if (ema9[i] > ma20[i] > ma50[i] and rsi14[i] > 55.0 and adx14[i] > 25.0 and
            monitor.allowed_long(s)):
            msg = (
                f"🚀 <b>{fmt_symbol(s)} — TENDÊNCIA LONGA CONFIRMADA (1h)</b>\n"
                f"<b>💰</b> <code>{price:.6f}</code>\n"
                f"<b>🧠</b> EMA9>MA20>MA50 | RSI {rsi14[i]:.1f} | ADX {adx14[i]:.1f}\n"
                f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
            )
            await send_alert(session, msg); monitor.mark_long(s)

    except Exception as e:
        print("worker_1h error", s, e)

async def worker_4h(session, s):
    try:
        o,h,l,c,v = await get_klines(session, s, INTERVAL_4H, 200)
        if len(c)<60: return
        ema9, ma20, ma50, ma200, rsi14, vol_ma, adx14, pdi, mdi = compute_all(o,h,l,c,v)
        i=len(c)-1; price=c[i]

        # 🌕 Pré-confirmação (4h): EMA9 > MA20 + RSI>50
        if (ema9[i-1] <= ma20[i-1] and ema9[i] > ma20[i] and rsi14[i] > 50.0 and
            monitor.allowed_long(s)):
            msg = (
                f"🌕 <b>{fmt_symbol(s)} — PRÉ-CONFIRMAÇÃO (4h)</b>\n"
                f"<b>💰</b> <code>{price:.6f}</code>\n"
                f"<b>🧠</b> EMA9 cruzou MA20 | RSI {rsi14[i]:.1f}\n"
                f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
            )
            await send_alert(session, msg); monitor.mark_long(s)

        # 🚀 Confirmada (4h): EMA9>MA20>MA50 + RSI>55 + ADX>25
        if (ema9[i] > ma20[i] > ma50[i] and rsi14[i] > 55.0 and adx14[i] > 25.0 and
            monitor.allowed_long(s)):
            msg = (
                f"🚀 <b>{fmt_symbol(s)} — TENDÊNCIA LONGA CONFIRMADA (4h)</b>\n"
                f"<b>💰</b> <code>{price:.6f}</code>\n"
                f"<b>🧠</b> EMA9>MA20>MA50 | RSI {rsi14[i]:.1f} | ADX {adx14[i]:.1f}\n"
                f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
            )
            await send_alert(session, msg); monitor.mark_long(s)

    except Exception as e:
        print("worker_4h error", s, e)

# ----------------- MAIN -----------------
async def main():
    async with aiohttp.ClientSession() as session:
        ticks = await get_24h(session)
        watch = shortlist_from_24h(ticks, SHORTLIST_N)
        hello = f"💻 v2.0 | Monitorando {len(watch)} pares SPOT | {ts_br()}"
        await send_alert(session, hello); print(hello)

        while True:
            tasks=[]
            for s in watch:
                tasks += [
                    worker_5m(session, s),
                    worker_15m(session, s),
                    worker_1h(session, s),
                    worker_4h(session, s),
                ]
            await asyncio.gather(*tasks)
            await asyncio.sleep(180)

# ----------------- FLASK -----------------
def start_bot():
    asyncio.run(main())

if __name__=="__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home():
        return "✅ Binance Alerts Bot v2.0 — curto estável (5m/15m) + longos básicos 🇧🇷"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
