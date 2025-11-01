# backtest_reais_30d.py
# Setup OURO Confluência Curta V6.2 – Backtest com dados reais últimos 30 dias
import asyncio, aiohttp, os
import pandas as pd
from datetime import datetime, timedelta, timezone

BINANCE_HTTP   = "https://api.binance.com"
REQ_TIMEOUT    = 10
TOP_N          = 50
MIN_LIQUIDITY  = 20_000_000
DAYS           = 30
COOLDOWN_SEC   = 15 * 60
R_MULT_TP1     = 2.5
R_MULT_TP2     = 5.0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID        = os.getenv("CHAT_ID","").strip()

def ema(seq, span):
    if not seq: return []
    a = 2.0/(span+1.0)
    e = seq[0]
    out = []
    for x in seq:
        e = a*x + (1-a)*e
        out.append(e)
    return out

def macd(seq, fast=12, slow=26, signal=9):
    if len(seq) < slow + signal + 2:
        n = len(seq)
        return {"macd":[0]*n,"signal":[0]*n,"hist":[0]*n}
    ef = ema(seq, fast)
    es = ema(seq, slow)
    line = [f-s for f,s in zip(ef, es)]
    sig  = ema(line, signal)
    if len(sig) < len(line):
        sig = [sig[0]]*(len(line)-len(sig)) + sig
    hist = [m - s for m,s in zip(line, sig)]
    return {"macd": line, "signal": sig, "hist": hist}

def calc_rsi(seq, period=14):
    n = len(seq)
    if n < period+2:
        return [50.0]*n
    gains = [0.0]*(n-1); losses = [0.0]*(n-1)
    for i in range(1, n):
        d = seq[i] - seq[i-1]
        gains[i-1] = max(d,0.0)
        losses[i-1] = max(-d,0.0)
    rsi = [50.0]*n
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / (avg_loss + 1e-12)
    rsi[period] = 100 - 100/(1+rs)
    for i in range(period+1, n):
        avg_gain = (avg_gain*(period-1) + gains[i-1]) / period
        avg_loss = (avg_loss*(period-1) + losses[i-1]) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi[i] = 100 - 100/(1+rs)
    return rsi

def cruzou_de_baixo(c, p9=9, p20=20):
    if len(c) < p20+2:
        return False
    e9 = ema(c, p9)
    e20 = ema(c, p20)
    return e9[-2] <= e20[-2] and e9[-1] > e20[-1]

def ts_ms(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp()*1000)

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
        if not s.endswith("USDT"):
            continue
        if any(x in s for x in blocked):
            continue
        qv = float(d.get("quoteVolume",0) or 0)
        if qv < MIN_LIQUIDITY:
            continue
        pares.append((s, qv))
    pares.sort(key=lambda x: x[1], reverse=True)
    return pares[:TOP_N]

async def get_klines(session, symbol, interval, limit=1000):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return await fetch_json(session, url)

async def send_excel_to_telegram(session, file_path):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        print("⚠️ Telegram não configurado.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        form = aiohttp.FormData()
        form.add_field("chat_id", CHAT_ID)
        form.add_field("document", open(file_path, "rb"),
                       filename=os.path.basename(file_path),
                       content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        async with session.post(url, data=form) as resp:
            if resp.status == 200:
                print("📤 Relatório enviado pro Telegram com sucesso.")
            else:
                print(f"[ERRO TELEGRAM] status={resp.status}")
    except Exception as e:
        print(f"[ERRO TELEGRAM] {e}")

async def backtest_symbol(session, symbol, qv, start_ms, end_ms):
    k3  = await get_klines(session, symbol, "3m", )
    k5  = await get_klines(session, symbol, "5m", )
    k15 = await get_klines(session, symbol, "15m",)
    k30 = await get_klines(session, symbol, "30m",)
    k1h = await get_klines(session, symbol, "1h", )
    if not (k3 and k5 and k15 and k30 and k1h):
        return None

    c3  = [float(x[4]) for x in k3]
    c5  = [float(x[4]) for x in k5]
    c15 = [float(x[4]) for x in k15]
    c30 = [float(x[4]) for x in k30]
    c1h = [float(x[4]) for x in k1h]

    macd3  = macd(c3)
    macd5  = macd(c5)
    macd15 = macd(c15)
    macd30 = macd(c30)
    macd1h = macd(c1h)

    rsi5 = calc_rsi(c5, 14)[-1] if len(c5)>0 else 50
    cruz  = cruzou_de_baixo(c5)
    hist_ok = (macd3["hist"][-1]>0 and macd5["hist"][-1]>0 and
               macd15["hist"][-1]>0 and macd30["hist"][-1]>0 and macd1h["hist"][-1]>0)

    valido = cruz and hist_ok and (45 <= rsi5 <= 65)
    return {"symbol": symbol, "liq": qv, "alerta": valido}

async def main():
    async with aiohttp.ClientSession() as session:
        pares = await get_top_usdt_symbols(session)
        if not pares:
            print("Sem pares válidos.")
            return
        end   = datetime.utcnow().replace(tzinfo=timezone.utc)
        start = end - timedelta(days=DAYS)
        s_ms  = ts_ms(start); e_ms = ts_ms(end)

        results = []
        for s, qv in pares:
            res = await backtest_symbol(session, s, qv, s_ms, e_ms)
            if res:
                results.append(res)
            await asyncio.sleep(0.2)

        df = pd.DataFrame(results)
        df.to_excel("relatorio_backtest.xlsx", index=False)
        await send_excel_to_telegram(session, "relatorio_backtest.xlsx")

if __name__ == "__main__":
    asyncio.run(main())
