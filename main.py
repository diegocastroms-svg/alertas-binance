# backtest.py
# OURO CONFLUÊNCIA CURTA – Backtest + Envio automático para Telegram
# Gera 'Relatório Backtest OURO.xlsx' e envia pro chat configurado via bot Telegram

import asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
import pandas as pd
import os

BINANCE_HTTP   = "https://api.binance.com"
REQ_TIMEOUT    = 12
TOP_N          = 50
MIN_LIQUIDITY  = 20_000_000
DAYS           = 30
COOLDOWN_SEC   = 15 * 60
R_MULT_TP1     = 2.5
R_MULT_TP2     = 5.0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()

# -------------- UTILS --------------
def ema(seq, span):
    if not seq: return []
    a = 2.0 / (span + 1.0)
    e = seq[0]; out=[]
    for x in seq:
        e = a*x + (1-a)*e
        out.append(e)
    return out

def macd(seq, fast=12, slow=26, signal=9):
    if len(seq) < slow + signal + 2:
        n=len(seq)
        return {"macd":[0]*n,"signal":[0]*n,"hist":[0]*n}
    ef, es = ema(seq, fast), ema(seq, slow)
    line=[f-s for f,s in zip(ef,es)]
    sig=ema(line,signal)
    if len(sig)<len(line):
        sig=[sig[0]]*(len(line)-len(sig))+sig
    hist=[m-s for m,s in zip(line,sig)]
    return {"macd":line,"signal":sig,"hist":hist}

def calc_rsi(seq, period=14):
    if len(seq)<period+2:return[50]*len(seq)
    gains=[0]*(len(seq)-1);losses=[0]*(len(seq)-1)
    for i in range(1,len(seq)):
        d=seq[i]-seq[i-1]
        gains[i-1]=max(d,0);losses[i-1]=-min(d,0)
    rsi=[50]*len(seq)
    avg_gain=sum(gains[:period])/period
    avg_loss=sum(losses[:period])/period
    rs=avg_gain/(avg_loss+1e-12)
    rsi[period]=100-100/(1+rs)
    for i in range(period+1,len(seq)):
        avg_gain=(avg_gain*(period-1)+gains[i-1])/period
        avg_loss=(avg_loss*(period-1)+losses[i-1])/period
        rs=avg_gain/(avg_loss+1e-12)
        rsi[i]=100-100/(1+rs)
    return rsi

def cruzou_de_baixo(c,p9=9,p20=20):
    if len(c)<p20+2:return False
    e9=ema(c,p9);e20=ema(c,p20)
    return e9[-2]<=e20[-2] and e9[-1]>e20[-1]

def ts_ms(dt):return int(dt.replace(tzinfo=timezone.utc).timestamp()*1000)

def crec(h,i):return i>=1 and h[i]>0 and h[i]>h[i-1]

# -------------- BINANCE --------------
async def fetch_json(session,url):
    try:
        async with session.get(url,timeout=REQ_TIMEOUT) as r:
            return await r.json()
    except:return None

async def get_top_usdt_symbols(session):
    url=f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    data=await fetch_json(session,url)
    if not isinstance(data,list):return[]
    blocked=("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","EUR","BRL","PERP","TEST","USDE","USD1","BF")
    pares=[]
    for d in data:
        s=d.get("symbol","")
        if not s.endswith("USDT"):continue
        if any(x in s for x in blocked):continue
        qv=float(d.get("quoteVolume",0)or 0)
        if qv<MIN_LIQUIDITY:continue
        pares.append((s,qv))
    pares.sort(key=lambda x:x[1],reverse=True)
    return pares[:TOP_N]

async def get_klines(session,symbol,interval,start_ms=None,end_ms=None,limit=1000):
    base=f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    if start_ms:base+=f"&startTime={start_ms}"
    if end_ms:base+=f"&endTime={end_ms}"
    data=await fetch_json(session,base)
    return data if isinstance(data,list) else[]

def extract_ohlcv(k):
    t=[int(x[0])for x in k]
    o=[float(x[1])for x in k]
    h=[float(x[2])for x in k]
    l=[float(x[3])for x in k]
    c=[float(x[4])for x in k]
    v=[float(x[5])for x in k]
    return t,o,h,l,c,v

# -------------- TELEGRAM --------------
async def send_excel_to_telegram(session, file_path):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        print("⚠️ Telegram não configurado.")
        return
    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(file_path,"rb") as f:
            form={"chat_id":CHAT_ID}
            files={"document":(os.path.basename(file_path),f)}
            await session.post(url,data=form,files=files)
        print("📤 Relatório enviado pro Telegram com sucesso.")
    except Exception as e:
        print(f"[ERRO TELEGRAM] {e}")

# -------------- BACKTEST --------------
async def backtest_symbol(session,symbol,qv,start_ms,end_ms):
    k3=await get_klines(session,symbol,"3m",start_ms,end_ms)
    k5=await get_klines(session,symbol,"5m",start_ms,end_ms)
    k15=await get_klines(session,symbol,"15m",start_ms,end_ms)
    k30=await get_klines(session,symbol,"30m",start_ms,end_ms)
    k1h=await get_klines(session,symbol,"1h",start_ms,end_ms)
    if not (k3 and k5 and k15 and k30 and k1h):
        return {"symbol":symbol,"liq":qv,"signals":0,"wins":0,"loss":0,"winrate":0,"avgR":0,"Rsum":0}

    _,_,_,l5,c5,v5=extract_ohlcv(k5)
    c3=[float(k[4])for k in k3]
    c15=[float(k[4])for k in k15]
    c30=[float(k[4])for k in k30]
    c1h=[float(k[4])for k in k1h]

    macd3,macd5,macd15,macd30,macd1h=[macd(x)for x in[c3,c5,c15,c30,c1h]]
    rsi5=calc_rsi(c5,14)
    ema21_5=ema(c5,21)
    signals=wins=loss=0;sumR=0;last_hit=0
    for i in range(30,len(c5)-15):
        if (k5[i][0]-last_hit)<COOLDOWN_SEC*1000:continue
        if not crec(macd3["hist"],-1):continue
        if not (cruzou_de_baixo(c5[:i+1],9,20)and macd5["hist"][i]>0):continue
        if not (macd15["hist"][i]>0 and macd30["hist"][i]>0 and macd1h["hist"][i]>0):continue
        if not (45<=rsi5[i]<=65):continue
        signals+=1;last_hit=k5[i][0]
        preco=c5[i];stop=min(l5[i],ema21_5[i]);risco=preco-stop
        tp1=preco+R_MULT_TP1*risco;tp2=preco+R_MULT_TP2*risco
        hit=None
        for j in range(i+1,min(i+15,len(c5))):
            if l5[j]<=stop:hit=("SL",-1.0);break
            if c5[j]>=tp2:hit=("TP2",R_MULT_TP2);break
            if c5[j]>=tp1:hit=("TP1",R_MULT_TP1);break
        if hit is None:
            r=(c5[min(i+15,len(c5)-1)]-preco)/risco
            sumR+=r;wins+=r>0;loss+=r<=0
        else:
            sumR+=hit[1];wins+=hit[1]>0;loss+=hit[1]<0
    winrate=wins/signals*100 if signals else 0
    avgR=sumR/signals if signals else 0
    return {"symbol":symbol,"liq":qv,"signals":signals,"wins":wins,"loss":loss,"winrate":winrate,"avgR":avgR,"Rsum":sumR}

# -------------- MAIN --------------
async def main():
    async with aiohttp.ClientSession() as session:
        pares=await get_top_usdt_symbols(session)
        if not pares:
            print("Sem pares válidos.");return
        end=datetime.utcnow().replace(tzinfo=timezone.utc)
        start=end-timedelta(days=DAYS)
        s_ms,e_ms=ts_ms(start),ts_ms(end)
        tasks=[backtest_symbol(session,s,qv,s_ms,e_ms)for s,qv in pares]
        results=await asyncio.gather(*tasks)

        df=pd.DataFrame(results)
        total=df["signals"].sum()
        wins=df["wins"].sum();loss=df["loss"].sum()
        winrate=(wins/total*100)if total else 0
        avgR=(df["Rsum"].sum()/total)if total else 0

        resumo=pd.DataFrame([{
            "Total Pares":len(df),
            "Total Sinais":total,
            "Vitórias":wins,
            "Derrotas":loss,
            "Winrate Global (%)":round(winrate,1),
            "R Médio Global":round(avgR,2),
            "R Total":round(df["Rsum"].sum(),1)
        }])

        with pd.ExcelWriter("Relatório Backtest OURO.xlsx") as writer:
            df.to_excel(writer,index=False,sheet_name="Resultados")
            resumo.to_excel(writer,index=False,sheet_name="Resumo")

        print("✅ Backtest concluído. Enviando relatório pro Telegram...")
        await send_excel_to_telegram(session,"Relatório Backtest OURO.xlsx")

if __name__=="__main__":
    asyncio.run(main())
