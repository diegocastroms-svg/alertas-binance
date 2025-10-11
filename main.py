# main_short.py — versão com alerta "mercado caiu e lateralizando"
# Básico, funcional e leve — 5m/15m (SPOT/USDT, top 50 por volume)

import os, time, threading, requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from flask import Flask

# ==========================
# CONFIG
# ==========================
BINANCE = "https://api.binance.com"
INTERVAL_5M, INTERVAL_15M = "5m", "15m"
K_LIMIT = 300
TOP_N, TOP_REFRESH_SEC = 50, 3600
SCAN_SLEEP, COOLDOWN_SEC = 300, 900
MAX_WORKERS = 40
EXCLUDE_KEYWORDS = ("UP","DOWN","BULL","BEAR","2L","2S","3L","3S","4L","4S","5L","5S","1000")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","").strip()
BR_TZ = timezone(timedelta(hours=-3))
cooldowns, current_top, last_top_update = defaultdict(dict), [], 0

app = Flask(__name__)

@app.route("/")
def health(): return "OK",200

# ==========================
# UTILS
# ==========================
def now_br_str(): return datetime.now(BR_TZ).strftime("%Y-%m-%d %H:%M")
def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":True},timeout=10)
    except: pass
def fetch_json(url,p=None,t=10):
    try: r=requests.get(url,params=p,timeout=t);return r.json() if r.status_code==200 else None
    except: return None
def get_klines(s,i,l=K_LIMIT):
    d=fetch_json(f"{BINANCE}/api/v3/klines",{"symbol":s,"interval":i,"limit":l})
    if not d: return [],[]
    return [float(x[4]) for x in d],[float(x[5]) for x in d]
def sma(v,p):
    o,s,q=[],0.0,[]
    for x in v:
        q.append(x);s+=x
        if len(q)>p:s-=q.pop(0)
        o.append((s/p) if len(q)==p else None)
    return o
def ema(v,p):
    o=[];k=2/(p+1);e=None
    for x in v:
        e=x if e is None else x*k+e*(1-k);o.append(e)
    return o
def rsi(v,p=14):
    if len(v)<p+1:return None
    g,l=[],[]
    for i in range(1,len(v)):
        d=v[i]-v[i-1];g.append(max(d,0));l.append(-min(d,0))
    ag=sum(g[:p])/p;al=sum(l[:p])/p
    for i in range(p,len(g)):
        ag=(ag*(p-1)+g[i])/p;al=(al*(p-1)+l[i])/p
    if al==0:return 100.0
    rs=ag/al;return 100-(100/(1+rs))
def cross_up(pa,na,pb,nb): return pa<=pb and na>nb if all(x is not None for x in (pa,na,pb,nb)) else False
def check_cooldown(s,k): 
    n=time.time();l=cooldowns[s].get(k,0)
    if n-l<COOLDOWN_SEC:return True
    cooldowns[s][k]=n;return False
def chart_link(s,i): return f"https://www.binance.com/en/trade?symbol={s}&type=spot"
def fmt_msg(s,e,t,m,p,r,e9,m20,m50,m200,i):
    return (f"{e} <b>{s}</b>\n🧭 <b>{t}</b>\n📊 {m}\n💰 Preço: {p:.6f}\n"
            f"📈 EMA9:{e9:.5f} | MA20:{m20:.5f} | MA50:{m50:.5f}\n🌙 MA200:{m200:.5f}\n"
            f"🧪 RSI:{r:.1f}\n🇧🇷 {now_br_str()}\n🔗 <a href='{chart_link(s,i)}'>Ver gráfico {i}</a>\n━━━━━━━━━━━")
def get_valid_spot_usdt():
    i=fetch_json(f"{BINANCE}/api/v3/exchangeInfo");out=[]
    if not i:return out
    for s in i["symbols"]:
        if s["status"]!="TRADING" or s["quoteAsset"]!="USDT":continue
        sym,base=s["symbol"],s["baseAsset"]
        if any(k in sym for k in EXCLUDE_KEYWORDS):continue
        if sym.endswith("USD") or base.endswith("USD"):continue
        out.append(sym)
    return out
def get_top50():
    v=set(get_valid_spot_usdt());d=fetch_json(f"{BINANCE}/api/v3/ticker/24hr");r=[]
    if not d:return []
    for t in d:
        s=t["symbol"]; 
        if s in v:
            try:r.append((s,float(t["quoteVolume"])));except:pass
    r.sort(key=lambda x:x[1],reverse=True);return [s for s,_ in r[:TOP_N]]

# ==========================
# ANÁLISE
# ==========================
def analyze_symbol(sym):
    try:
        # ---------- 5m ----------
        c5,v5=get_klines(sym,INTERVAL_5M,K_LIMIT)
        if len(c5)<210:return
        ema9,ma20,ma50,ma200=ema(c5,9),sma(c5,20),sma(c5,50),sma(c5,200)
        r=rsi(c5,14);p=c5[-1]
        e9,m20,m50,m200v=ema9[-1],ma20[-1],ma50[-1],ma200[-1]
        e9p,m20p=ema9[-2],ma20[-2]

        # (5m) MERCADO CAIU + LATERALIZANDO
        if ma20[-10] and ma20[-1] and ma20[-1]<ma20[-10]:
            ultimos=c5[-20:]
            if max(ultimos)-min(ultimos) < 0.01*sum(ultimos)/len(ultimos):
                key="queda_lat_5m"
                if not check_cooldown(sym,key):
                    send_telegram(f"🔻 <b>{sym}</b>\n💬 Mercado em queda, lateralizando — monitorando possível alta\n🇧🇷 {now_br_str()}\n🔗 <a href='{chart_link(sym,'5m')}'>Ver gráfico 5m</a>\n━━━━━━━━━━━")

        # (5m) TENDÊNCIA INICIANDO
        if cross_up(e9p,e9,m20p,m20) and e9<m200v:
            k="inicio_5m"
            if not check_cooldown(sym,k):
                send_telegram(fmt_msg(sym,"🟢","TENDÊNCIA INICIANDO (5m)",
                    "EMA9 cruzou MA20 p/ cima (abaixo da MA200)",p,r or 0,e9,m20,m50,m200v,"5m"))

        # ---------- 15m ----------
        c15,v15=get_klines(sym,INTERVAL_15M,K_LIMIT)
        if len(c15)<210:return
        e9m,ma20m,ma50m,ma200m=ema(c15,9),sma(c15,20),sma(c15,50),sma(c15,200)
        r15=rsi(c15,14);p15=c15[-1]
        e9,m20,m50,m200v=e9m[-1],ma20m[-1],ma50m[-1],ma200m[-1]
        e9p,ma200p=e9m[-2],ma200m[-2]

        # (15m) PRÉ-CONFIRMADA
        if cross_up(e9p,e9,ma200p,ma200v):
            k="preconf_15m"
            if not check_cooldown(sym,k):
                send_telegram(fmt_msg(sym,"🔵","TENDÊNCIA PRÉ-CONFIRMADA (15m)",
                    "EMA9 cruzou a MA200 p/ cima",p15,r15 or 0,e9,m20,m50,m200v,"15m"))

        # (15m) RETESTES
        vol_avg=sum(v15[-20:])/20 if len(v15)>=20 else None
        touch9=abs(p15-e9)/p15<0.006 if e9 else False
        touch20=abs(p15-m20)/p15<0.006 if m20 else False
        if (touch9 or touch20) and r15 and r15>55 and vol_avg and v15[-1]>vol_avg and p15>m20:
            k="reteste_ok_15m"
            if not check_cooldown(sym,k):
                send_telegram(fmt_msg(sym,"🟢","RETESTE CONFIRMADO (15m)",
                    "Preço testou EMA9/MA20 e retomou com força (RSI>55, vol>média)",
                    p15,r15,e9,m20,m50,m200v,"15m"))
        if (touch9 or touch20) and r15 and r15<50 and p15<(e9 or p15):
            k="reteste_fraco_15m"
            if not check_cooldown(sym,k):
                send_telegram(fmt_msg(sym,"🟠","RETESTE FRACO (15m)",
                    "Preço testou EMA9/MA20 e perdeu força — possível queda",
                    p15,r15,e9,m20,m50,m200v,"15m"))
    except Exception as e: print(f"Erro {sym}:",e)

# ==========================
# LOOP PRINCIPAL
# ==========================
def refresh_top():
    global current_top,last_top_update
    t=get_top50()
    if t: current_top,last_top_update=t,time.time();send_telegram(f"🔄 TOP {TOP_N} atualizado ({len(t)} pares)")

def worker(s): analyze_symbol(s);time.sleep(0.1)

def main_loop():
    send_telegram(f"✅ BOT CURTO ATIVO — SPOT/USDT\n⏱️ Cooldown 15 min\n🇧🇷 {now_br_str()}")
    refresh_top()
    if current_top: send_telegram(f"📦 Top 5: {', '.join(current_top[:5])}")
    while True:
        if time.time()-last_top_update>=TOP_REFRESH_SEC or not current_top: refresh_top()
        if current_top:
            th=[]
            for s in current_top: th.append(threading.Thread(target=worker,args=(s,),daemon=True))
            for i in range(0,len(th),MAX_WORKERS):
                b=th[i:i+MAX_WORKERS]
                [t.start() for t in b];[t.join() for t in b]
        time.sleep(SCAN_SLEEP)

def start_flask(): app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False,use_reloader=False)

if __name__=="__main__":
    threading.Thread(target=start_flask,daemon=True).start()
    main_loop()
