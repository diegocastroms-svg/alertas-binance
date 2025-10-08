import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL = "5m"
SHORTLIST_N = 65                    # ↑ de 40 para 65 pares
COOLDOWN_SEC = 15 * 60
MIN_PCT = 1.0
MIN_QV  = 300_000.0

EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
VOL_MA   = 9
BB_LEN   = 20

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
def fmt_symbol(symbol): 
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol):
    base = symbol.upper().replace("USDT","")
    a=f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b=f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now(): 
    return (datetime.now(timezone.utc)-timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")+" 🇧🇷"

async def send_alert(session,text):
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}",json={"message":text},timeout=10)
        except: 
            pass
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload={"chat_id":CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}
            await session.post(url,data=payload,timeout=10)
        except: 
            pass

# ----------------- Indicadores -----------------
def sma(seq,n):
    out,q,s=[],deque(),0.0
    for x in seq:
        q.append(x);s+=x
        if len(q)>n:s-=q.popleft()
        out.append(s/len(q))
    return out

def ema(seq,span):
    if not seq:return []
    out=[];alpha=2/(span+1);e=seq[0];out.append(e)
    for x in seq[1:]:
        e=alpha*x+(1-alpha)*e;out.append(e)
    return out

def rolling_std(seq,n):
    out,q=[],deque()
    for x in seq:
        q.append(x)
        if len(q)>n:q.popleft()
        m=sum(q)/len(q)
        var=sum((v-m)**2 for v in q)/len(q)
        out.append(math.sqrt(var))
    return out

def rsi_wilder(closes,period=14):
    if len(closes)==0:return []
    deltas=[0.0]+[closes[i]-closes[i-1] for i in range(1,len(closes))]
    gains=[max(d,0.0) for d in deltas]
    losses=[max(-d,0.0) for d in deltas]
    rsis=[50.0]*len(closes)
    if len(closes)<period+1:return rsis
    avg_gain=sum(gains[1:period+1])/period
    avg_loss=sum(losses[1:period+1])/period
    for i in range(period+1,len(closes)):
        avg_gain=(avg_gain*(period-1)+gains[i])/period
        avg_loss=(avg_loss*(period-1)+losses[i])/period
        rs=avg_gain/(avg_loss+1e-12)
        rsis[i]=100-(100/(1+rs))
    return rsis

def compute_indicators(o,h,l,c,v):
    ema9=ema(c,EMA_FAST);ma20=sma(c,MA_SLOW);ma50=sma(c,MA_MED);ma200=sma(c,MA_LONG)
    rsi14=rsi_wilder(c,RSI_LEN);vol_ma=sma(v,VOL_MA)
    bb_std=rolling_std(c,BB_LEN);bb_up=[ma20[i]+2*bb_std[i] for i in range(len(bb_std))]
    bb_low=[ma20[i]-2*bb_std[i] for i in range(len(bb_std))]
    return ema9,ma20,ma50,ma200,rsi14,vol_ma,bb_up,bb_low

# ----------------- Binance -----------------
async def get_klines(session,symbol,interval="5m",limit=200):
    url=f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with session.get(url,timeout=12) as r:
        r.raise_for_status();data=await r.json()
    # remove candle em formação
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1]));h.append(float(k[2]));l.append(float(k[3]))
        c.append(float(k[4]));v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr",timeout=15) as r:
        r.raise_for_status();return await r.json()

def shortlist_from_24h(tickers,n=400):
    usdt=[]
    for t in tickers:
        s=t.get("symbol","")
        if not s.endswith("USDT"):continue
        # SPOT only / sem futuros, perpétuos, alavancados e fiat/stables secundárias
        blocked=("UP","DOWN","BULL","BEAR","PERP","USD_","_PERP","_BUSD","_FDUSD","_TUSD","_EUR","_TRY","_BRL","_USDC","_DAI","_BTC")
        if any(x in s for x in blocked):continue
        pct=float(t.get("priceChangePercent","0") or 0.0)
        qv=float(t.get("quoteVolume","0") or 0.0)
        if abs(pct)>=MIN_PCT and qv>=MIN_QV:usdt.append((s,pct,qv))
    usdt.sort(key=lambda x:(abs(x[1]),x[2]),reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Emojis / Mensagens -----------------
def kind_emoji(kind):
    return {
        "TENDÊNCIA_INICIANDO":"⬆️",
        "TENDÊNCIA_CONFIRMADA":"💎",
        "MERCADO_ESTICADO":"⚠️",
        "PERDENDO_FORÇA":"🟠",
        "SAÍDA":"🚪",
        "PULLBACK":"♻️",          # (reversão real de baixa -> alta)
        "REVERSÃO_LOCAL":"↕️"      # (reteste/toque na média e retomada)
    }.get(kind,"📌")

def build_msg(symbol, kind, price, bullets):
    star="⭐"; sym=fmt_symbol(symbol); em=kind_emoji(kind)
    return (
        f"{star} {sym} {em} — {kind}\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"🧠 {bullets}\n"
        f"⏰ {ts_brazil_now()}\n"
        f"{binance_links(symbol)}"
    )

# ----------------- Monitor: cooldown + estado de tendência -----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)  # por (símbolo, tipo)
        self.trend = {}  # estado por símbolo

    def allowed(self, symbol, kind):
        return time.time() - self.cooldown[(symbol, kind)] >= COOLDOWN_SEC

    def mark(self, symbol, kind):
        self.cooldown[(symbol, kind)] = time.time()

    def set_stage(self, symbol, stage, bar_idx):
        self.trend[symbol] = {"stage": stage, "bar": bar_idx}

    def get_stage(self, symbol):
        return self.trend.get(symbol)

    def clear_stage(self, symbol):
        if symbol in self.trend: del self.trend[symbol]

# ----------------- Regras de estágio / fraqueza -----------------
def is_confirmed_up(ema9, ma20, ma50, ma200, rsi14, v, vol_ma, i):
    return (ema9[i] > ma20[i] > ma50[i] > ma200[i] and
            rsi14[i] >= 60 and
            v[i] >= vol_ma[i] * 1.2)

def is_top_stretched(c, bb_up, rsi14, i):
    return (c[i] > bb_up[i] and rsi14[i] >= 70)

def is_weakening(ema9, ma20, rsi14, v, vol_ma, i):
    if i < 1: return False
    dist_now  = ema9[i] - ma20[i]
    dist_prev = ema9[i-1] - ma20[i-1]
    return (rsi14[i] < rsi14[i-1] and
            v[i] < vol_ma[i] and
            dist_now < dist_prev)

def still_weak(ema9, ma20, rsi14, i):
    return (rsi14[i] < 50) or (ema9[i] < ma20[i])

# ----------------- Worker -----------------
async def candle_worker(session,symbol,monitor):
    try:
        o,h,l,c,v=await get_klines(session,symbol,interval=INTERVAL,limit=200)
        ema9,ma20,ma50,ma200,rsi14,vol_ma,bb_up,bb_low=compute_indicators(o,h,l,c,v)
        last=len(c)-1; prev=last-1
        if last < 1: return

        signals=[]

        # --- TENDÊNCIA INICIANDO (tolerância 1 candle) ---
        cross_now = ema9[last-1] <= ma20[last-1] and ema9[last] > ma20[last]
        crossed_last = ema9[last] > ma20[last] and ema9[last-1] <= ma20[last-1]
        price_above_200 = c[last] > ma200[last]
        if (cross_now or crossed_last) and price_above_200 and rsi14[last] > 50 and v[last] >= vol_ma[last]:
            signals.append(("TENDÊNCIA_INICIANDO", f"EMA9 cruzou MA20 ↑ | RSI {rsi14[last]:.1f} | Vol≥média"))

        # --- PULLBACK (reversão real de baixa -> alta) ---
        # vindo de baixa (ema9<ma20), cruza pra cima + RSI 45→50 + volume 1.2x + candle anterior abaixo da BB baixa
        if (ema9[prev] < ma20[prev] and ema9[last] > ma20[last] and
            rsi14[prev] < 45 <= rsi14[last] and v[last] > vol_ma[last]*1.2 and
            c[prev] < bb_low[prev]):
            signals.append(("PULLBACK", f"EMA9 cruzou MA20 ↑ | RSI {rsi14[prev]:.1f}→{rsi14[last]:.1f} | Vol {v[last]/max(1e-9,vol_ma[last]):.1f}x | Vindo de BB inf"))

        # --- REVERSÃO_LOCAL (reteste na média e retomada na alta) ---
        if (ema9[last] > ma20[last] > ma50[last] and
            (l[last] <= ema9[last] or l[last] <= ma20[last]) and
            c[last] > ema9[last] and
            rsi14[last] > 55 and v[last] >= vol_ma[last]):
            signals.append(("REVERSÃO_LOCAL", f"Reteste na média + retomada | RSI {rsi14[last]:.1f} | Vol≥média"))

        # --- MERCADO ESTICADO (topo provável) ---
        if is_top_stretched(c, bb_up, rsi14, last):
            signals.append(("MERCADO_ESTICADO", f"Acima da BB sup | RSI {rsi14[last]:.1f} — possível topo"))

        # --------- Gerenciamento de ESTÁGIOS (sequência confirmada → topo → fraqueza → saída) ---------
        state = monitor.get_stage(symbol)

        # 1) Ao detectar INICIANDO, grava estágio "iniciando"
        if any(k == "TENDÊNCIA_INICIANDO" for k, _ in signals):
            monitor.set_stage(symbol, "iniciando", last)

        # 2) 2 velas depois de "iniciando" → checar confirmação
        state = monitor.get_stage(symbol)
        if state and state["stage"] == "iniciando":
            bars_passed = last - state["bar"]
            if bars_passed >= 2 and is_confirmed_up(ema9, ma20, ma50, ma200, rsi14, v, vol_ma, last):
                if monitor.allowed(symbol, "TENDÊNCIA_CONFIRMADA"):
                    msg = build_msg(symbol, "TENDÊNCIA_CONFIRMADA", c[last],
                                    f"Médias alinhadas | RSI {rsi14[last]:.1f} | Vol {v[last]/max(1e-9,vol_ma[last]):.1f}x")
                    await send_alert(session, msg)
                    monitor.mark(symbol, "TENDÊNCIA_CONFIRMADA")
                monitor.set_stage(symbol, "confirmada", last)

        # 3) Ao detectar topo (MERCADO_ESTICADO) → estágio "topo"
        if any(k == "MERCADO_ESTICADO" for k, _ in signals):
            monitor.set_stage(symbol, "topo", last)

        # 4) 2 velas após topo, se fraqueza → PERDENDO_FORÇA
        state = monitor.get_stage(symbol)
        if state and state["stage"] == "topo":
            bars_passed = last - state["bar"]
            if bars_passed >= 2 and is_weakening(ema9, ma20, rsi14, v, vol_ma, last):
                if monitor.allowed(symbol, "PERDENDO_FORÇA"):
                    msg = build_msg(symbol, "PERDENDO_FORÇA", c[last],
                                    f"RSI {rsi14[last-1]:.1f}→{rsi14[last]:.1f} | Vol abaixo da média | EMA9 aproximando MA20")
                    await send_alert(session, msg)
                    monitor.mark(symbol, "PERDENDO_FORÇA")
                monitor.set_stage(symbol, "perdendo", last)

        # 5) 2 velas após PERDENDO_FORÇA, se continuar fraco → SAÍDA
        state = monitor.get_stage(symbol)
        if state and state["stage"] == "perdendo":
            bars_passed = last - state["bar"]
            if bars_passed >= 2 and still_weak(ema9, ma20, rsi14, last):
                if monitor.allowed(symbol, "SAÍDA"):
                    msg = build_msg(symbol, "SAÍDA", c[last],
                                    f"Fraqueza persistente | RSI {rsi14[last]:.1f} {'<50' if rsi14[last]<50 else ''} | {'EMA9<MA20' if ema9[last]<ma20[last] else 'pressão vendedora'}")
                    await send_alert(session, msg)
                    monitor.mark(symbol, "SAÍDA")
                monitor.clear_stage(symbol)

        # --------- Envio do 1º sinal do ciclo (com cooldown por tipo) ---------
        if signals:
            k0, d0 = signals[0]
            if monitor.allowed(symbol, k0):
                txt = build_msg(symbol, k0, c[last], d0 if len(signals)==1 else " | ".join([d for _,d in signals]))
                await send_alert(session, txt)
                monitor.mark(symbol, k0)

    except Exception as e:
        print("Worker error",symbol,e)

# ----------------- Main -----------------
async def main():
    monitor=Monitor()
    async with aiohttp.ClientSession() as session:
        tickers=await get_24h(session)
        watchlist=shortlist_from_24h(tickers,SHORTLIST_N)
        await send_alert(session,f"💻 v10.10 — SPOT ONLY | {len(watchlist)} pares | Ciclo completo (início→confirmação→topo→fraqueza→saída) | {ts_brazil_now()}")
        print(f"💻 v10.10 — analisando {len(watchlist)} pares SPOT.")
        while True:
            await asyncio.gather(*[candle_worker(session,s,monitor) for s in watchlist])
            await asyncio.sleep(180)
            try:
                tickers=await get_24h(session)
                watchlist=shortlist_from_24h(tickers,SHORTLIST_N)
            except Exception as e:
                print("Erro atualização:",e)

# ----------------- Execução Flask -----------------
def start_bot():
    try:asyncio.run(main())
    except KeyboardInterrupt:pass

if __name__=="__main__":
    import threading
    threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home():return "✅ Binance Alerts Bot v10.10 — Ciclo de Tendência ativo 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
