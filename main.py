# ============================================
# 📁 main_v2_4_preconfirm5m.py
# ============================================
# Atualização única: alerta de PRÉ-CONFIRMAÇÃO no 5m
# Quando EMA9>MA20>MA50 e preço ainda abaixo da MA200
# ============================================

import os
import asyncio
import aiohttp
import threading
from datetime import datetime, timedelta
from statistics import mean
from flask import Flask

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://api.binance.com/api/v3"
COOLDOWN = timedelta(minutes=15)

cooldown_pump = {}
cooldown_day  = {}
cooldown_swing = {}

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot Binance ativo (v2.4 preconfirm 5m)", 200

async def send_telegram(msg, html=True):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": True}
    if html:
        data["parse_mode"] = "HTML"
    async with aiohttp.ClientSession() as s:
        await s.post(url, data=data)

def binance_chart_link(symbol): 
    base = symbol.replace("USDT","")
    return f"https://www.binance.com/en/trade/{base}_USDT?ref=open_in_app&layout=pro"

def chart_link_line(symbol, tf): 
    return f'🔗 <a href="{binance_chart_link(symbol)}">Ver gráfico {tf} no app da Binance</a>'

def ma(seq,p): return mean(seq[-p:]) if len(seq)>=p else None
def ema(seq,p):
    if len(seq)<p: return None
    k=2/(p+1); e=seq[-p]
    for x in seq[-p+1:]: e=x*k+e*(1-k)
    return e
def rsi(seq,p=14):
    if len(seq)<p+1:return None
    g,l=[],[]
    for i in range(-p,0):
        d=seq[i]-seq[i-1]
        (g if d>0 else l).append(abs(d))
    ag=mean(g) if g else 0; al=mean(l) if l else 1e-9
    rs=ag/al
    return 100-(100/(1+rs))

async def get_klines(session,symbol,interval,limit=240):
    url=f"{BASE}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with session.get(url) as r:
        return await r.json()

def losing_strength_msg(tf=""): 
    return f"🔻 Saída recomendada — perdendo força ({tf})" if tf else "🔻 Saída recomendada — perdendo força"

# ============================
# 🚀 Pump detector (5m)
# ============================
async def pump_detector(session,symbol):
    now=datetime.now()
    if symbol in cooldown_pump and now - cooldown_pump[symbol] < COOLDOWN:
        return

    k5=await get_klines(session,symbol,"5m",240)
    if not isinstance(k5,list) or len(k5)<210: return
    c5=[float(x[4]) for x in k5]
    v5=[float(x[5]) for x in k5]
    price=c5[-1]
    ema9_5=ema(c5,9); ma20_5=ma(c5,20); ma50_5=ma(c5,50); ma200_5=ma(c5,200)
    rsi14_5=rsi(c5,14)
    if not all([ema9_5,ma20_5,ma50_5,ma200_5,rsi14_5]): return

    # === NOVO ALERTA: pré-confirmação abaixo da MA200 ===
    if ema9_5>ma20_5>ma50_5 and price<ma200_5:
        msg_pre = (
            f"🟢 {symbol}\n"
            f"Tendência pré-confirmada — EMA9>MA20>MA50 abaixo da MA200 (5m)\n"
            f"💰 Preço: {price:.6f}\n"
            f"{chart_link_line(symbol,'5m')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg_pre)
        cooldown_pump[symbol]=now

    # === Alerta normal (mantido) ===
    if ema9_5>ma20_5 and rsi14_5>50:
        msg=(
            f"🚀 {symbol}\n"
            f"Tendência de alta iniciada (5m)\n"
            f"EMA9>MA20 • RSI={rsi14_5:.1f}\n"
            f"💰 Preço: {price:.6f}\n"
            f"{chart_link_line(symbol,'5m')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_pump[symbol]=now

# ============================
# 🔁 Loop principal
# ============================
async def main_loop():
    await send_telegram("Bot iniciado com sucesso ✅", html=False)
    await asyncio.sleep(1)
    await send_telegram("✅ <b>BOT ATIVO — v2.4 preconfirm5m</b>\n🧠 Novo alerta 5m ativo.")

    symbols=["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT"]
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                tasks=[pump_detector(s,sym) for sym in symbols]
                await asyncio.gather(*tasks)
            await asyncio.sleep(60)
        except Exception as e:
            print("Erro loop:",e)
            await asyncio.sleep(10)

def _start(): asyncio.run(main_loop())
if __name__=="__main__":
    threading.Thread(target=_start,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
