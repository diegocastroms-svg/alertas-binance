# main.py — Scanner de Tendência por Cruzamentos (5m/15m) + Exaustão Vendedora
# Foco: entrar cedo após queda -> exaustão -> cruzamentos -> confirmações

import os, math, time, asyncio, aiohttp
from datetime import datetime, timezone
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE = "https://api.binance.com"
INTERVALS = ["5m", "15m"]
COOLDOWN = 15 * 60
TOP_N = 50                 # Top-50 por volume (quoteVolume)
MIN_QV = 1_0000.0          # filtro mínimo de volume em USDT (baixo pra não podar)
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

app = Flask(__name__)

# ---------------- UTILS ----------------
def nowbr():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def fmt(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".")

async def tg(session, text: str):
    if not TOKEN or not CHAT_ID:
        print("[AVISO] TELEGRAM_TOKEN/CHAT_ID ausentes.")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        await session.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Erro TG:", e)

# ---------------- INDICADORES ----------------
def sma(vals, p):
    out, acc = [], 0.0
    for i,v in enumerate(vals):
        acc += v
        if i >= p: acc -= vals[i-p]
        out.append(acc / p if i+1>=p else acc/(i+1))
    return out

def ema(vals, p):
    k = 2/(p+1)
    out = []
    for i,v in enumerate(vals):
        if i == 0: out.append(v)
        else: out.append(v*k + out[-1]*(1-k))
    return out

def rsi14(closes, period=14):
    if len(closes) < period+1: return [None]*len(closes)
    rsis = [None]*len(closes)
    gains = [0.0]; losses = [0.0]
    for i in range(1,len(closes)):
        ch = closes[i]-closes[i-1]
        gains.append(max(ch,0.0)); losses.append(max(-ch,0.0))
    avg_g = sum(gains[1:period+1])/period
    avg_l = sum(losses[1:period+1])/period
    rs = (avg_g / avg_l) if avg_l != 0 else 9999.0
    rsis[period] = 100 - 100/(1+rs)
    for i in range(period+1,len(closes)):
        avg_g = (avg_g*(period-1)+gains[i])/period
        avg_l = (avg_l*(period-1)+losses[i])/period
        rs = (avg_g/avg_l) if avg_l != 0 else 9999.0
        rsis[i] = 100 - 100/(1+rs)
    return rsis

def adx14(high, low, close, period=14):
    n = len(close)
    if n < period+1: return [None]*n
    tr, plus_dm, minus_dm = [0.0], [0.0], [0.0]
    for i in range(1,n):
        tr.append(max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1])))
        up = high[i]-high[i-1]
        dn = low[i-1]-low[i]
        plus_dm.append(up if (up>dn and up>0) else 0.0)
        minus_dm.append(dn if (dn>up and dn>0) else 0.0)
    # smoothed
    tr14 = [sum(tr[1:period+1])]
    pDM14 = [sum(plus_dm[1:period+1])]
    mDM14 = [sum(minus_dm[1:period+1])]
    for i in range(period+1,n):
        tr14.append(tr14[-1]-tr14[-1]/period + tr[i])
        pDM14.append(pDM14[-1]-pDM14[-1]/period + plus_dm[i])
        mDM14.append(mDM14[-1]-mDM14[-1]/period + minus_dm[i])
    dx = [None]*(period)  # índices até period-1
    adx = [None]*(period*2)
    for i in range(period+1,n):
        ti = i-(period+1)
        pdi = 100*(pDM14[ti]/tr14[ti]) if tr14[ti]!=0 else 0
        mdi = 100*(mDM14[ti]/tr14[ti]) if tr14[ti]!=0 else 0
        dx.append(100*abs(pdi-mdi)/(pdi+mdi) if (pdi+mdi)!=0 else 0)
    if len(dx) < period*2: return [None]*n
    first_adx = sum([d for d in dx[period:] if d is not None][:period])/period
    adx[period*2-1] = first_adx
    for i in range(period*2, n):
        adx.append((adx[-1]*(period-1)+dx[i])/period if adx[-1] is not None and dx[i] is not None else None)
    # alinhamento de tamanho
    while len(adx)<n: adx.append(None)
    return adx

def crossed_up(a, b):
    return a[-2] is not None and b[-2] is not None and a[-2] < b[-2] and a[-1] >= b[-1]

def all_above_now(*series):
    return all(s[-1] is not None for s in series) and all(series[i][-1] > series[i+1][-1] for i in range(len(series)-1))

# ---------------- EXAUSTÃO VENDEDORA (5m) ----------------
def seller_exhaustion(high, low, open_, close, volume):
    """Critérios:
       - Tendência de queda recente (média dos últimos 10 closes < média dos 10 anteriores).
       - Vela atual bearish OU martelo com grande pavio inferior.
       - Pavio inferior >= 1.5x corpo; volume atual >= 1.8x média(20)."""
    n = len(close)
    if n < 40: return False
    last = n-1
    m10a = sum(close[last-20:last-10])/10
    m10b = sum(close[last-10:last])/10
    downtrend = m10b < m10a
    body = abs(close[-1]-open_[-1])
    range_ = max(high[-1]-low[-1], 1e-9)
    lower_wick = (min(open_[-1], close[-1]) - low[-1])
    wick_ok = lower_wick >= 1.5*body and (lower_wick/range_) >= 0.55
    vol_avg = sum(volume[-20:])/20
    vol_ok = volume[-1] >= 1.8*vol_avg
    return downtrend and wick_ok and vol_ok

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=250):
    url = f"{BINANCE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with session.get(url, timeout=15) as r:
        return await r.json()

async def top50_usdt_symbols(session):
    url = f"{BINANCE}/api/v3/ticker/24hr"
    async with session.get(url, timeout=15) as r:
        data = await r.json()
    rows = []
    for d in data:
        s = d.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(x in s for x in ["UP","DOWN","BUSD","FDUSD","TUSD","USDC","USD1"]): continue
        try:
            qv = float(d["quoteVolume"])
            rows.append((qv, s))
        except: pass
    rows.sort(reverse=True)
    return [s for _,s in rows[:TOP_N]]

# ---------------- ESTADO (de-dup) ----------------
LAST_SENT = {}      # (symbol, tag) -> timestamp
EXHAUST_LAST = {}   # symbol -> timestamp do último sinal de exaustão

def can_send(key, ttl=60*60):  # 1h de proteção por evento
    t = time.time()
    last = LAST_SENT.get(key, 0)
    if t - last >= ttl:
        LAST_SENT[key] = t
        return True
    return False

# ---------------- PROCESSAMENTO ----------------
async def scan_symbol(session, symbol):
    try:
        k5  = await get_klines(session, symbol, "5m")
        k15 = await get_klines(session, symbol, "15m")
        if not isinstance(k5, list) or not isinstance(k15, list): return

        o5  = [float(k[1]) for k in k5]
        h5  = [float(k[2]) for k in k5]
        l5  = [float(k[3]) for k in k5]
        c5  = [float(k[4]) for k in k5]
        v5  = [float(k[5]) for k in k5]

        o15 = [float(k[1]) for k in k15]
        h15 = [float(k[2]) for k in k15]
        l15 = [float(k[3]) for k in k15]
        c15 = [float(k[4]) for k in k15]
        v15 = [float(k[5]) for k in k15]

        # MAs
        ema9_5   = ema(c5, 9);   ma20_5 = sma(c5,20); ma50_5 = sma(c5,50); ma200_5 = sma(c5,200)
        ema9_15  = ema(c15,9);   ma20_15 = sma(c15,20); ma50_15 = sma(c15,50); ma200_15 = sma(c15,200)

        # RSI/ADX 15m (para confirmação)
        rsi15 = rsi14(c15,14)
        adx15 = adx14(h15,l15,c15,14)

        price = fmt(c5[-1]); hora = nowbr()

        # 1) EXAUSTÃO (5m)
        if seller_exhaustion(h5,l5,o5,c5,v5):
            if can_send((symbol,"exaust5"), ttl=30*60):  # 30 min
                EXHAUST_LAST[symbol] = time.time()
                await tg(session, f"🔻 {symbol} — <b>Exaustão vendedora</b> (5m)\n💰 {price}\n🕒 {hora}")

        # 2) TENDÊNCIA INICIANDO (5m): EMA9 cruza MA20/50 após exaustão/lateralização
        ini = crossed_up(ema9_5, ma20_5) or crossed_up(ema9_5, ma50_5)
        recently_exhausted = (time.time() - EXHAUST_LAST.get(symbol, 0) <= 60*60)  # até 1h após exaustão
        if ini and recently_exhausted and can_send((symbol,"ini5"), ttl=60*60):
            await tg(session, f"🟢 {symbol} ⬆️ <b>Tendência iniciando</b> (5m)\nEMA9 cruzou MA20/50 após exaustão/lateralização\n💰 {price}\n🕒 {hora}")

        # 3) PRÉ-CONFIRMADA (5m): 9/20/50 cruzam acima da 200 (evento)
        pre5_now   = ema9_5[-1] > ma200_5[-1] and ma20_5[-1] > ma200_5[-1] and ma50_5[-1] > ma200_5[-1]
        pre5_prev  = ema9_5[-2] <= ma200_5[-2] or ma20_5[-2] <= ma200_5[-2] or ma50_5[-2] <= ma200_5[-2]
        if pre5_now and pre5_prev and can_send((symbol,"pre5"), ttl=2*60*60):
            await tg(session, f"🟡 {symbol} ⬆️ <b>Tendência pré-confirmada</b> (5m)\nMédias 9/20/50 acima da MA200\n💰 {price}\n🕒 {hora}")

        # 4) PRÉ-CONFIRMADA (15m): EMA9 cruza MA200
        if crossed_up(ema9_15, ma200_15) and can_send((symbol,"pre15"), ttl=2*60*60):
            await tg(session, f"🟡 {symbol} ⬆️ <b>Tendência pré-confirmada</b> (15m)\nEMA9 cruzou MA200\n💰 {price}\n🕒 {hora}")

        # 5) CONFIRMADA (15m): 9>20>50>200 + RSI>55 + ADX>25 (evento quando vira verdadeiro)
        conf_now  = (ema9_15[-1] > ma20_15[-1] > ma50_15[-1] > ma200_15[-1]) and \
                    (rsi15[-1] is not None and rsi15[-1] > 55) and \
                    (adx15[-1] is not None and adx15[-1] > 25)
        conf_prev = not ((ema9_15[-2] > ma20_15[-2] > ma50_15[-2] > ma200_15[-2]) and \
                         (rsi15[-2] is not None and rsi15[-2] > 55) and \
                         (adx15[-2] is not None and adx15[-2] > 25))
        if conf_now and conf_prev and can_send((symbol,"conf15"), ttl=3*60*60):
            await tg(session, f"🚀 {symbol} ⬆️ <b>Tendência confirmada</b> (15m)\nEMA9>MA20>MA50>MA200 + RSI>55 + ADX>25\n💰 {price}\n🕒 {hora}")

    except Exception as e:
        print(f"Erro {symbol}:", e)

# ---------------- LOOP ----------------
async def scanner_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await top50_usdt_symbols(session)
        await tg(session, f"✅ scanner ativo | TOP {len(symbols)} SPOT/USDT | cooldown 15m | {nowbr()} 🇧🇷")
        tasks = [scan_symbol(session, s) for s in symbols]
        await asyncio.gather(*tasks)

@app.route("/")
def home():
    return "scanner ativo", 200

if __name__ == "__main__":
    import threading
    def runner():
        while True:
            try:
                asyncio.run(scanner_loop())
            except Exception as e:
                print("Loop error:", e)
            time.sleep(COOLDOWN)

    threading.Thread(target=runner, daemon=True).start()
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
