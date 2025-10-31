# backtest.py
# Backtest do setup "OURO CONFLUÊNCIA CURTA"
# - Top 50 pares USDT com liquidez >= 20M
# - Últimos 30 dias
# - Regras: 3m MACD verde; 5m cruzamento EMA9↑EMA20 + MACD verde; 15m/30m/1h MACD verde
#           histograma crescente em todos, RSI(5m) entre 45–65, volume 5m > 1.1x média 10, cooldown 15min
# - Resultado no terminal: sinais, winrate, R médio, ranking por par

import asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone

BINANCE_HTTP   = "https://api.binance.com"
REQ_TIMEOUT    = 12
TOP_N          = 50
MIN_LIQUIDITY  = 20_000_000   # 20M USDT
DAYS           = 30
COOLDOWN_SEC   = 15 * 60
TEST_HORIZON_5M_BARS = 12     # ~1h
R_MULT_TP1     = 2.5
R_MULT_TP2     = 5.0

# ---------------- Utils ----------------
def ema(seq, span):
    if not seq: return []
    a = 2.0 / (span + 1.0)
    out, e = [], seq[0]
    for x in seq:
        e = a * x + (1 - a) * e
        out.append(e)
    return out

def macd(seq, fast=12, slow=26, signal=9):
    n = len(seq)
    if n < slow + signal + 2:
        z = [0.0]*n
        return {"macd": z, "signal": z, "hist": z}
    ef  = ema(seq, fast)
    es  = ema(seq, slow)
    line = [f - s for f, s in zip(ef, es)]
    sig  = ema(line, signal)
    if len(sig) < len(line):
        sig = [sig[0]]*(len(line)-len(sig)) + sig
    hist = [m - s for m, s in zip(line, sig)]
    return {"macd": line, "signal": sig, "hist": hist}

def calc_rsi(seq, period=14):
    n = len(seq)
    if n < period + 2:
        return [50.0]*n
    gains, losses = [0.0]*(n-1), [0.0]*(n-1)
    for i in range(1, n):
        d = seq[i] - seq[i-1]
        gains[i-1]  = max(d, 0.0)
        losses[i-1] = -min(d, 0.0)
    rsi = [50.0]*n
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / (avg_loss + 1e-12)
    rsi[period] = 100 - 100/(1+rs)
    for i in range(period+1, n):
        avg_gain = (avg_gain*(period-1)+gains[i-1]) / period
        avg_loss = (avg_loss*(period-1)+losses[i-1]) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi[i] = 100 - 100/(1+rs)
    return rsi

def cruzou_de_baixo(c, p9=9, p20=20):
    if len(c) < p20 + 2: return False
    e9 = ema(c, p9)
    e20 = ema(c, p20)
    return e9[-2] <= e20[-2] and e9[-1] > e20[-1]

def ts_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

# ---------------- Binance fetch ----------------
async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            return await r.json()
    except:
        return None

async def get_top_usdt_symbols(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    data = await fetch_json(session, url)
    if not isinstance(data, list):
        return []
    blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","EUR","BRL","PERP","TEST","USDE","USD1","BF")
    pares = []
    for d in data:
        s = d.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(x in s for x in blocked): continue
        qv = float(d.get("quoteVolume", 0) or 0)
        if qv < MIN_LIQUIDITY: continue
        pares.append((s, qv))
    pares.sort(key=lambda x: x[1], reverse=True)
    return [s for s,_ in pares[:TOP_N]]

async def get_klines(session, symbol, interval, start_ms=None, end_ms=None, limit=1000):
    base = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    if start_ms is not None: base += f"&startTime={start_ms}"
    if end_ms   is not None: base += f"&endTime={end_ms}"
    data = await fetch_json(session, base)
    return data if isinstance(data, list) else []

# ---------------- Helpers ----------------
def extract_ohlcv(kl):
    # [openTime, open, high, low, close, volume, closeTime, ...]
    t  = [int(k[0]) for k in kl]
    o  = [float(k[1]) for k in kl]
    h  = [float(k[2]) for k in kl]
    l  = [float(k[3]) for k in kl]
    c  = [float(k[4]) for k in kl]
    v  = [float(k[5]) for k in kl]
    ct = [int(k[6]) for k in kl]
    return t, o, h, l, c, v, ct

def map_to_prev_index(times_ref, times_other):
    res = []
    j = 0
    m = len(times_other)
    for t in times_ref:
        while j+1 < m and times_other[j+1] <= t:
            j += 1
        res.append(j if j < m else m-1)
    return res

def crec(h, i):
    return i >= 1 and h[i] > 0 and h[i] > h[i-1]

# ---------------- Backtest ----------------
async def backtest_symbol(session, symbol, start_ms, end_ms):
    k3  = await get_klines(session, symbol, "3m",  start_ms, end_ms, 1000)
    k5  = await get_klines(session, symbol, "5m",  start_ms, end_ms, 1000)
    k15 = await get_klines(session, symbol, "15m", start_ms, end_ms, 1000)
    k30 = await get_klines(session, symbol, "30m", start_ms, end_ms, 1000)
    k1h = await get_klines(session, symbol, "1h",  start_ms, end_ms, 1000)
    if not (k3 and k5 and k15 and k30 and k1h):
        return {"symbol": symbol, "signals": 0, "wins": 0, "loss": 0, "avgR": 0.0, "Rsum": 0.0, "winrate": 0.0}

    t3,o3,h3,l3,c3,v3,ct3   = extract_ohlcv(k3)
    t5,o5,h5,l5,c5,v5,ct5   = extract_ohlcv(k5)
    t15,_,_,_,c15,_,ct15    = extract_ohlcv(k15)
    t30,_,_,_,c30,_,ct30    = extract_ohlcv(k30)
    t1h,_,_,_,c1h,_,ct1h    = extract_ohlcv(k1h)

    macd3   = macd(c3)
    macd5   = macd(c5)
    macd15  = macd(c15)
    macd30  = macd(c30)
    macd1h  = macd(c1h)
    rsi5    = calc_rsi(c5, 14)
    ema21_5 = ema(c5, 21)

    idx3  = map_to_prev_index(ct5, ct3)
    idx15 = map_to_prev_index(ct5, ct15)
    idx30 = map_to_prev_index(ct5, ct30)
    idx1h = map_to_prev_index(ct5, ct1h)

    signals = wins = loss = 0
    sumR = 0.0
    last_hit_ts = 0

    for i in range(50, len(c5)-TEST_HORIZON_5M_BARS-1):
        if (ct5[i] - last_hit_ts) < COOLDOWN_SEC*1000:
            continue

        i3  = idx3[i]; i15 = idx15[i]; i30 = idx30[i]; i1h = idx1h[i]
        h3  = macd3["hist"]; h5 = macd5["hist"]; h15 = macd15["hist"]; h30 = macd30["hist"]; h1h = macd1h["hist"]

        # Histograma verde e crescente em TODOS TFs
        cond_hist = (crec(h3, i3) and crec(h5, i) and crec(h15, i15) and crec(h30, i30) and crec(h1h, i1h))
        if not cond_hist: 
            continue

        # Cruzamento 5m (candle fechado até i)
        if i < 22: 
            continue
        cruzou = cruzou_de_baixo(c5[:i+1], 9, 20)
        if not cruzou: 
            continue

        # RSI 45–65 no 5m
        if not (45 <= rsi5[i] <= 65):
            continue

        # Volume 5m atual > 1.1x média 10
        if i < 10: 
            continue
        vol_med10 = sum(v5[i-9:i+1]) / 10.0
        if not (v5[i] > vol_med10 * 1.1):
            continue

        # Sinal válido
        signals += 1
        last_hit_ts = ct5[i]

        preco = c5[i]
        stop  = min(l5[i], ema21_5[i])
        risco = max(preco - stop, 1e-9)
        tp1   = preco + R_MULT_TP1 * risco
        tp2   = preco + R_MULT_TP2 * risco

        hit = None
        for j in range(i+1, i+1+TEST_HORIZON_5M_BARS):
            if j >= len(c5): break
            if l5[j] <= stop:
                hit = ("SL", -1.0)
                break
            if h5[j] >= tp2:
                hit = ("TP2", R_MULT_TP2)
                break
            if h5[j] >= tp1:
                hit = ("TP1", R_MULT_TP1)
                break

        if hit is None:
            # parcial 1R se tocar em algum momento
            touched_partial = any(h5[j] >= preco + risco for j in range(i+1, min(i+1+TEST_HORIZON_5M_BARS, len(c5))))
            if touched_partial:
                sumR += 1.0
                wins += 1
            else:
                close_end = c5[min(i+TEST_HORIZON_5M_BARS, len(c5)-1)]
                r_real = (close_end - preco) / risco
                sumR += r_real
                if r_real >= 0: wins += 1
                else: loss += 1
        else:
            sumR += hit[1]
            if hit[1] > 0: wins += 1
            else: loss += 1

    winrate = (wins/signals*100.0) if signals else 0.0
    avgR = (sumR/signals) if signals else 0.0
    return {"symbol": symbol, "signals": signals, "wins": wins, "loss": loss, "avgR": avgR, "Rsum": sumR, "winrate": winrate}

# ---------------- Main ----------------
async def main():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        if not symbols:
            print("Falha ao obter pares com liquidez.")
            return
        end   = datetime.utcnow().replace(tzinfo=timezone.utc)
        start = end - timedelta(days=DAYS)
        start_ms, end_ms = ts_ms(start), ts_ms(end)

        tasks = [backtest_symbol(session, s, start_ms, end_ms) for s in symbols]
        results = await asyncio.gather(*tasks)

        total_signals = sum(r["signals"] for r in results)
        total_R       = sum(r["Rsum"]    for r in results)
        wins          = sum(r["wins"]    for r in results)
        loss          = sum(r["loss"]    for r in results)
        winrate       = (wins/total_signals*100.0) if total_signals else 0.0
        avgR_global   = (total_R/total_signals) if total_signals else 0.0

        print("\n===== BACKTEST – OURO CONFLUÊNCIA CURTA =====")
        print(f"Período: últimos {DAYS} dias | Pares testados: {len(symbols)}")
        print(f"Sinais: {total_signals} | Vitórias: {wins} | Derrotas: {loss}")
        print(f"Winrate: {winrate:.1f}% | R médio: {avgR_global:.2f}R | R total: {total_R:.1f}R\n")

        ranked = [r for r in results if r["signals"] >= 5]
        ranked.sort(key=lambda x: (x["winrate"], x["signals"]), reverse=True)
        print("Top 10 símbolos (mín. 5 sinais):")
        for r in ranked[:10]:
            print(f"- {r['symbol']}: {r['signals']} sinais | {r['winrate']:.1f}% | avg {r['avgR']:.2f}R")

if __name__ == "__main__":
    asyncio.run(main())
