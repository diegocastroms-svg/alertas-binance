# main_short.py — V7.2D (Render Safe)
# TENDÊNCIA CURTA (15m, 30m, 1h) — EMAs, filtros de volume, alertas didáticos (rompimento/reteste/continuação),
# mensagens com Entrada/Stop/Alvo/Probabilidade, nomes sem “USDT” e cooldown anti-spam.
#
# VARS DE AMBIENTE ESPERADAS NO RENDER:
#   TELEGRAM_TOKEN, CHAT_ID
#
# Requisitos: aiohttp, flask

import os, asyncio, aiohttp, time, math, statistics
from datetime import datetime, timezone, timedelta
from flask import Flask

# ---------- CONFIG ----------
BINANCE = "https://api.binance.com"
TF_LIST = ["15m","30m","1h"]                 # timeframes ativos
LOOKBACK = 300                               # candles por símbolo/TF (suficiente p/ EMA200)
COOLDOWN_MIN = 15                            # min p/ repetir o MESMO tipo de alerta do mesmo TF
USDTVOL_MIN = 10_000_000                     # volume mínimo 24h em USDT (10M)
STABLES = {"USDT","USDC","FDUSD","TUSD","DAI","PYUSD","EUR","BUSD"}
EXCLUDE_SUBSTR = ("UPUSDT","DOWNUSDT","BULL","BEAR","3LUSDT","3SUSDT","5LUSDT","5SUSDT")
EMA_FAST = 12; EMA_SLOW = 26; EMA_SIG = 9
# tolerâncias de reteste (proximidade da média)
RET_200_TOL = 0.004    # 0,4%
RET_50_TOL  = 0.006    # 0,6%

# ---------- WEB / APP ----------
app = Flask(__name__)
@app.get("/")
def home():
    return "V7.2D TENDÊNCIA CURTA ON", 200

@app.get("/health")
def health():
    return "OK", 200

# ---------- UTILS ----------
def now_br():
    return datetime.now(timezone(timedelta(hours=-3)))  # BRT fixo

def ema(series, period):
    if len(series) < period: return [None]*len(series)
    k = 2/(period+1)
    out = []
    ema_val = None
    for i, x in enumerate(series):
        if i == period-1:
            ema_val = sum(series[:period])/period
            out.append(ema_val)
        elif i >= period:
            ema_val = x*k + ema_val*(1-k)
            out.append(ema_val)
        else:
            out.append(None)
    return out

def macd(close):
    e12 = ema(close, EMA_FAST)
    e26 = ema(close, EMA_SLOW)
    macd_line = []
    for a,b in zip(e12, e26):
        macd_line.append(None if (a is None or b is None) else a-b)
    # signal
    sig = ema([x if x is not None else 0 for x in macd_line], EMA_SIG)
    hist = []
    for m, s in zip(macd_line, sig):
        hist.append(None if (m is None or s is None) else m - s)
    return macd_line, sig, hist

def rsi(close, period=14):
    if len(close) < period+1: return [None]*len(close)
    gains, losses = [], []
    out = [None]*(period)
    for i in range(1, period+1):
        ch = close[i]-close[i-1]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    avg_gain = sum(gains)/period
    avg_loss = sum(losses)/period
    rs = (avg_gain/(avg_loss if avg_loss!=0 else 1e-9))
    out.append(100 - (100/(1+rs)))
    for i in range(period+1, len(close)):
        ch = close[i]-close[i-1]
        gain = max(ch,0); loss = max(-ch,0)
        avg_gain = (avg_gain*(period-1)+gain)/period
        avg_loss = (avg_loss*(period-1)+loss)/period
        rs = (avg_gain/(avg_loss if avg_loss!=0 else 1e-9))
        out.append(100 - (100/(1+rs)))
    return out

def sma(series, n):
    out = []
    s = 0
    for i,x in enumerate(series):
        s += x
        if i>=n: s -= series[i-n]
        out.append(None if i<n-1 else s/n)
    return out

def pct(a,b):
    if b == 0: return 0
    return (a-b)/b*100

def near(x,y, tol):
    return abs((x-y)/y) <= tol

def fmt(x, digits=6):
    try:
        # formatação numérica curta sem notação científica pra cripto
        return f"{x:.6f}".rstrip('0').rstrip('.')
    except:
        return str(x)

# ---------- TELEGRAM ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID = os.getenv("CHAT_ID","").strip()

async def send_tele(session, text):
    if not TELEGRAM_TOKEN or not CHAT_ID: 
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        async with session.post(url, json=payload, timeout=20) as r:
            await r.text()
    except:
        pass

# ---------- CACHE DE ALERTAS ----------
_last_alert = {}  # key: (symbol, tf, kind) -> ts

def can_alert(symbol, tf, kind):
    key = (symbol, tf, kind)
    ts = _last_alert.get(key, 0)
    return (time.time() - ts) >= COOLDOWN_MIN*60

def mark_alert(symbol, tf, kind):
    _last_alert[(symbol, tf, kind)] = time.time()

# ---------- BINANCE HELPERS ----------
async def fetch_json(session, url, params=None):
    for _ in range(2):
        try:
            async with session.get(url, params=params, timeout=25) as r:
                return await r.json()
        except:
            await asyncio.sleep(0.8)
    return None

async def get_usdt_symbols(session):
    info = await fetch_json(session, f"{BINANCE}/api/v3/exchangeInfo")
    syms = []
    if not info or "symbols" not in info: return syms
    for s in info["symbols"]:
        sym = s["symbol"]
        q = s.get("quoteAsset","")
        if q=="USDT" and s.get("status")=="TRADING":
            if any(tag in sym for tag in EXCLUDE_SUBSTR): 
                continue
            base = sym.replace("USDT","")
            if base in STABLES: 
                continue
            syms.append(sym)
    return syms

async def get_24h(session, symbols):
    # retorna dict symbol -> quoteVolume (float), lastPrice
    out = {}
    data = await fetch_json(session, f"{BINANCE}/api/v3/ticker/24hr")
    if not data: return out
    m = {d["symbol"]: d for d in data}
    for s in symbols:
        d = m.get(s)
        if d:
            qv = float(d.get("quoteVolume","0"))
            lastp = float(d.get("lastPrice","0"))
            out[s] = (qv, lastp)
    return out

async def get_klines(session, symbol, interval, limit=LOOKBACK):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = await fetch_json(session, f"{BINANCE}/api/v3/klines", params)
    if not data: return None
    # kline: [open time, open, high, low, close, volume, ...]
    out = {
        "open_time":[int(k[0]) for k in data],
        "open":[float(k[1]) for k in data],
        "high":[float(k[2]) for k in data],
        "low":[float(k[3]) for k in data],
        "close":[float(k[4]) for k in data],
        "vol":[float(k[5]) for k in data],
        "close_time":[int(k[6]) for k in data]
    }
    return out

# ---------- LÓGICA DE SINAL ----------
def build_signal(symbol, tf, ohlc):
    close = ohlc["close"]; high=ohlc["high"]; low=ohlc["low"]; vol=ohlc["vol"]
    if len(close) < 210: return None

    ema9  = ema(close, 9)
    ema50 = ema(close, 50)
    ema200= ema(close, 200)
    rsi14 = rsi(close, 14)
    m_line, m_sig, m_hist = macd(close)

    i = len(close)-1
    c  = close[i]; p1 = close[i-1]
    e9 = ema9[i]; e50= ema50[i]; e200= ema200[i]
    r  = rsi14[i] if rsi14[i] is not None else 50
    mac = m_line[i] if m_line[i] is not None else 0
    mac_prev = m_line[i-1] if m_line[i-1] is not None else 0

    # Volume Strength simples: vol atual vs SMA9/SMA21
    vma9  = sma(vol, 9)[i]
    vma21 = sma(vol,21)[i]
    vs = 0
    if vma9 and vma21 and vma21>0:
        vs = max(0, (vol[i]/max(vma9,vma21))*100)  # %
    vs_cap = max(1, min(500, vs))                 # clamp para texto

    # CONDIÇÕES
    bullish_struct = (c>e9 and c>e50 and c>e200)
    macd_rising = mac > mac_prev
    rsi_ok = r >= 55

    # 1) Rompimento EMA200 (primeiro fechamento acima com filtros)
    romp_200 = (p1 <= ema200[i-1] and c > e200 and rsi_ok and macd_rising)

    # 2) Reteste EMA200 (mínima toca/região e fecha acima da EMA9)
    ret_200 = (near(low[i], e200, RET_200_TOL) or low[i] <= e200*(1+RET_200_TOL)) and (c>e9 and e50>=e200*0.99)

    # 3) Reteste EMA50 (toque/região + fechamento acima da EMA9)
    ret_50 = (near(low[i], e50, RET_50_TOL) or low[i] <= e50*(1+RET_50_TOL)) and (c>e9 and c>e50 and e50>=e200*0.9)

    # 4) Continuação confirmada (break do topo recente com estrutura alinhada)
    prev_high = max(high[i-3:i]) if i>=3 else high[i-1]
    cont = bullish_struct and c>prev_high and rsi_ok and macd_rising

    kind = None
    if romp_200: kind = "Rompimento EMA200"
    elif ret_200: kind = "Reteste EMA200"
    elif ret_50:  kind = "Reteste EMA50"
    elif cont:    kind = "Continuação confirmada"
    else:
        return None

    # ENTRADA/STOP/ALVO (simples, automáticos e coerentes)
    entrada = c
    swing_low = min(low[i-5:i]) if i>=5 else min(low[i-3:i])
    # stop um pouco abaixo do swing/EMA50 (o que for mais próximo, com margem de 0,35%)
    base_stop = max(swing_low, e50*0.997)
    stop = min(entrada*0.985, base_stop)  # nunca acima da entrada
    # alvo: topo anterior ou RR ~1.6x
    topo_ref = max(high[i-10:i])
    rr_target = entrada + (entrada - stop)*1.6
    alvo = max(rr_target, topo_ref)

    # Probabilidade (heurística leve)
    prob = 50
    prob += min(20, (r-55)*0.8 if r>=55 else (r-50)*0.3)
    prob += 10 if macd_rising and mac>0 else -5
    prob += min(20, (vs_cap-100)/5)  # VS>100% ganha pontos
    prob += 5 if bullish_struct else 0
    prob = int(max(25, min(92, round(prob))))

    # Texto (sem USDT no nome)
    base = symbol.replace("USDT","")
    hdr_emoji = "📈"
    titulo = f"*{hdr_emoji} TENDÊNCIA CURTA ({tf.upper()})*\n*{base}*"
    linhas = [
        f"\n*Preço:* {fmt(c)}",
        f"*RSI:* {fmt(r,1)} | *MACD:* {fmt(mac,3)} | *VS:* {fmt(vs_cap)}%",
        f"*Tipo:* {kind}",
        f"🎯 *Entrada:* {fmt(entrada)}",
        f"🧯 *Stop:* {fmt(stop)}",
        f"🟩 *Alvo:* {fmt(alvo)}",
        f"📊 *Probabilidade:* {prob}%\n{now_br().strftime('%d/%m %H:%M')} BR"
    ]
    msg = titulo + "\n" + "\n".join(linhas)
    return {"kind":kind, "tf":tf, "msg":msg}

# ---------- LOOP PRINCIPAL ----------
async def worker():
    await asyncio.sleep(2)
    async with aiohttp.ClientSession() as session:
        symbols = await get_usdt_symbols(session)
        # filtro volume 24h
        vol24 = await get_24h(session, symbols)
        watch = [s for s in symbols if s in vol24 and vol24[s][0] >= USDTVOL_MIN]
        # loop
        while True:
            try:
                for tf in TF_LIST:
                    # processa em blocos para não estourar rate limit
                    for idx in range(0, len(watch), 40):
                        batch = watch[idx:idx+40]
                        tasks = [get_klines(session, s, tf) for s in batch]
                        kl_list = await asyncio.gather(*tasks)
                        for s, ohlc in zip(batch, kl_list):
                            if not ohlc: 
                                continue
                            sig = build_signal(s, tf, ohlc)
                            if not sig: 
                                continue
                            kind = sig["kind"]
                            if can_alert(s, tf, kind):
                                await send_tele(session, sig["msg"])
                                mark_alert(s, tf, kind)
                        await asyncio.sleep(0.25)
                await asyncio.sleep(15)  # gira rápido mas leve
            except Exception as e:
                # falha resiliente
                await asyncio.sleep(3)

def run_loop():
    loop = asyncio.get_event_loop()
    loop.create_task(worker())
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)

if __name__ == "__main__":
    run_loop()
