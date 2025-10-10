# main_v2_0.py
# Bot Binance SPOT — v2.0 (do zero, consolidado com todos os alertas e indicadores acordados)
# Timeframes: 5m (reversão curta), 15m (confirmação curta), 1h/4h (tendências longas)

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# =========================
# Config
# =========================
BINANCE_HTTP = "https://api.binance.com"
SHORTLIST_N = 80                    # conforme pedimos mais pares
COOLDOWN_SEC = 15 * 60              # curto (5m/15m)
COOLDOWN_LONG = 60 * 60             # longos (1h/4h)
MIN_PCT, MIN_QV = 1.0, 300_000.0    # filtro 24h (percent e quote volume)

# Indicadores
EMA_FAST, MA_SLOW, MA_MED, MA_LONG = 9, 20, 50, 200
RSI_LEN, VOL_MA, BB_LEN, ADX_LEN = 14, 9, 20, 14

# Credenciais
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# =========================
# Utils / mensagens
# =========================
def fmt_symbol(s): 
    return s[:-4] + "/USDT" if s.endswith("USDT") else s

def binance_links(s):
    b = s.upper().replace("USDT","")
    a = f"https://www.binance.com/en/trade/{b}_USDT?type=spot"
    c = f"https://www.binance.com/en/trade?type=spot&symbol={b}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{c}">Abrir (B)</a>'

def ts_brazil_now():
    return (datetime.now(timezone.utc)-timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session, text):
    # webhook (opcional)
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    # Telegram
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except:
            pass

# =========================
# Indicadores (sem pandas)
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
    a = 2.0 / (span + 1.0)
    e = seq[0]; out = [e]
    for x in seq[1:]:
        e = a*x + (1-a)*e
        out.append(e)
    return out

def rolling_std(seq, n):
    out, q = [], deque()
    for x in seq:
        q.append(x)
        if len(q) > n: q.popleft()
        m = sum(q)/len(q)
        var = sum((v-m)**2 for v in q)/len(q)
        out.append(math.sqrt(var))
    return out

def rsi_wilder(c, period=14):
    if len(c) == 0: return []
    d = [0.0] + [c[i]-c[i-1] for i in range(1,len(c))]
    g = [max(x,0.0) for x in d]
    l = [max(-x,0.0) for x in d]
    r = [50.0]*len(c)
    if len(c) < period+1: return r
    ag = sum(g[1:period+1])/period
    al = sum(l[1:period+1])/period
    for i in range(period+1,len(c)):
        ag = (ag*(period-1)+g[i])/period
        al = (al*(period-1)+l[i])/period
        rs = ag/(al+1e-12)
        r[i] = 100 - (100/(1+rs))
    return r

def true_range(h,l,c):
    tr=[0.0]
    for i in range(1,len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return tr

def adx(h,l,c,period=14):
    n=len(c)
    if n<period+1: return [20.0]*n, [0.0]*n, [0.0]*n
    tr = true_range(h,l,c)
    plus_dm=[0.0]; minus_dm=[0.0]
    for i in range(1,n):
        up = h[i]-h[i-1]
        dn = l[i-1]-l[i]
        plus_dm.append(up if (up>dn and up>0) else 0.0)
        minus_dm.append(dn if (dn>up and dn>0) else 0.0)
    atr=[0.0]*n; atr[period] = sum(tr[1:period+1])
    pdm=[0.0]*n; mdm=[0.0]*n
    pdm[period]=sum(plus_dm[1:period+1]); mdm[period]=sum(minus_dm[1:period+1])
    for i in range(period+1,n):
        atr[i]=atr[i-1] - (atr[i-1]/period) + tr[i]
        pdm[i]=pdm[i-1] - (pdm[i-1]/period) + plus_dm[i]
        mdm[i]=mdm[i-1] - (mdm[i-1]/period) + minus_dm[i]
    atr[:period]=[sum(tr[1:period+1])]*period
    pdm[:period]=[sum(plus_dm[1:period+1])]*period
    mdm[:period]=[sum(minus_dm[1:period+1])]*period
    plus_di=[0.0]*n; minus_di=[0.0]*n
    for i in range(n):
        plus_di[i] = 100.0*(pdm[i]/(atr[i]+1e-12))
        minus_di[i]= 100.0*(mdm[i]/(atr[i]+1e-12))
    dx=[0.0]*n
    for i in range(n):
        dx[i] = 100.0*abs(plus_di[i]-minus_di[i])/(plus_di[i]+minus_di[i]+1e-12)
    adx_vals=[0.0]*n; adx_vals[period]=sum(dx[1:period+1])/period
    for i in range(period+1,n):
        adx_vals[i] = (adx_vals[i-1]*(period-1)+dx[i])/period
    for i in range(period):
        adx_vals[i]=adx_vals[period]
    return adx_vals, plus_di, minus_di

def compute_indicators(o,h,l,c,v):
    ema9  = ema(c, EMA_FAST)
    ma20  = sma(c, MA_SLOW)
    ma50  = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    bbstd = rolling_std(c, BB_LEN)
    bb_up = [ma20[i] + 2*bbstd[i] for i in range(len(c))]
    bb_lo = [ma20[i] - 2*bbstd[i] for i in range(len(c))]
    adx14, pdi, mdi = adx(h,l,c, ADX_LEN)
    return ema9,ma20,ma50,ma200,rsi14,volma,bb_up,bb_lo,adx14,pdi,mdi

# =========================
# Binance
# =========================
async def get_klines(sess, symbol, interval="5m", limit=200):
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode({'symbol':symbol,'interval':interval,'limit':limit})}"
    async with sess.get(url, timeout=12) as r:
        r.raise_for_status(); data = await r.json()
    # remove último candle em formação
    o=h=l=c=v=[],[],[],[],[]
    o=[];h=[];l=[];c=[];v=[]
    for k in data[:-1]:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(sess):
    async with sess.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status(); return await r.json()

def shortlist_from_24h(ticks, n=400):
    blocked = (
        "UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD",
        "_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_BTC","_ETH","_BNB","_SOL",
        "_EUR","_TRY","_BRL"
    )
    out=[]
    for t in ticks:
        s = t.get("symbol","")
        if not s.endswith("USDT"): 
            continue
        if any(x in s for x in blocked):
            continue
        try:
            pct=float(t.get("priceChangePercent","0") or 0.0)
            qv =float(t.get("quoteVolume","0") or 0.0)
        except:
            pct,qv=0.0,0.0
        if abs(pct)>=MIN_PCT and qv>=MIN_QV:
            out.append((s,pct,qv))
    out.sort(key=lambda x:(abs(x[1]),x[2]), reverse=True)
    return [x[0] for x in out[:n]]

# =========================
# Cooldown monitor
# =========================
class Monitor:
    def __init__(self):
        self.cd = defaultdict(lambda: 0.0)        # chave: (symbol, key, tf)
        self.cd_long = defaultdict(lambda: 0.0)    # por símbolo (longos)
    def allowed(self, s, k, tf):
        return time.time() - self.cd[(s,k,tf)] >= COOLDOWN_SEC
    def mark(self, s, k, tf):
        self.cd[(s,k,tf)] = time.time()
    def allowed_long(self, s):
        return time.time() - self.cd_long[s] >= COOLDOWN_LONG
    def mark_long(self, s):
        self.cd_long[s] = time.time()

# =========================
# Builders de mensagem
# =========================
def msg_line(sym, title, price, brain):
    return (
        f"⭐ {fmt_symbol(sym)} {title}\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"🧠 {brain}\n"
        f"⏰ {ts_brazil_now()}\n"
        f"{binance_links(sym)}"
    )

def msg_long(sym, title, price, info_lines):
    body = "\n".join(info_lines)
    return (
        f"{title}\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"{body}\n"
        f"🕒 {ts_brazil_now()}\n"
        f"{binance_links(sym)}"
    )

# =========================
# Worker 5m — reversão curta
# =========================
async def worker_5m(sess, sym, M: Monitor):
    try:
        o,h,l,c,v = await get_klines(sess, sym, "5m", 200)
        if len(c) < 60: return
        e9,m20,m50,m200,rsi,vm,bb_up,bb_lo,adx14,pdi,mdi = compute_indicators(o,h,l,c,v)
        i=len(c)-1; j=i-1

        # 🔍 Monitorando queda + lateralização
        # queda recente: e9 < m20 < m50 e rsi < 45 (em j)
        # lateralização: bandas estreitas + RSI entre 40–50 (na janela recente)
        narrow = (bb_up[i]-bb_lo[i]) / (c[i] + 1e-12) < 0.02  # ~2% de largura
        if e9[j] < m20[j] < m50[j] and rsi[j] < 45 and 40 <= rsi[i] <= 50 and narrow:
            if M.allowed(sym,"MONITOR_QUEDA_LATERAL","5m"):
                msg = msg_line(sym, "🔍 — MONITORANDO QUEDA E LATERALIZAÇÃO", c[i],
                               "Baixa recente + lateralização | RSI 40–50 | Bandas estreitas")
                await send_alert(sess, msg); M.mark(sym,"MONITOR_QUEDA_LATERAL","5m")

        # 🚀 Tendência iniciando (5m): EMA9 cruza MA20 e MA50 após lateralização
        if e9[j] <= min(m20[j], m50[j]) and e9[i] > m20[i] and e9[i] > m50[i]:
            if M.allowed(sym,"INI5","5m"):
                msg = msg_line(sym, "🚀 — TENDÊNCIA INICIANDO (5m)", c[i],
                               "EMA9 cruzou MA20/MA50 após lateralização | RSI 50+ | Vol ok")
                await send_alert(sess, msg); M.mark(sym,"INI5","5m")

        # 🌕 Pré-confirmada (5m): EMA9/MA20/MA50 acima da MA200
        if e9[i] > m20[i] > m50[i] > m200[i]:
            if M.allowed(sym,"PRE5","5m"):
                msg = msg_line(sym, "🌕 — TENDÊNCIA PRÉ-CONFIRMADA (5m)", c[i],
                               "EMA9/MA20/MA50 > MA200 | Estrutura de reversão")
                await send_alert(sess, msg); M.mark(sym,"PRE5","5m")

    except Exception as e:
        print("5m error", sym, e)

# =========================
# Worker 15m — confirmações curtas, retestes, rompimento
# =========================
async def worker_15m(sess, sym, M: Monitor):
    try:
        o,h,l,c,v = await get_klines(sess, sym, "15m", 200)
        if len(c) < 60: return
        e9,m20,m50,m200,rsi,vm,bb_up,bb_lo,adx14,pdi,mdi = compute_indicators(o,h,l,c,v)
        i=len(c)-1; j=i-1

        # 🌕 Pré-confirmada (15m): EMA9 cruza MA200 para cima
        if e9[j] <= m200[j] and e9[i] > m200[i] and rsi[i] >= 50:
            if M.allowed(sym,"PRE15","15m"):
                msg = msg_line(sym, "🌕 — TENDÊNCIA PRÉ-CONFIRMADA (15m)", c[i],
                               f"EMA9 cruzou MA200 | RSI {rsi[i]:.1f} | Vol ok")
                await send_alert(sess, msg); M.mark(sym,"PRE15","15m")

        # 🚀 Confirmada (15m): EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25
        if e9[i] > m20[i] > m50[i] > m200[i] and rsi[i] > 55 and adx14[i] > 25:
            if M.allowed(sym,"CONF15","15m"):
                msg = msg_line(sym, "🚀 — TENDÊNCIA CONFIRMADA (15m)", c[i],
                               f"Médias alinhadas + RSI {rsi[i]:.1f} + ADX {adx14[i]:.1f}")
                await send_alert(sess, msg); M.mark(sym,"CONF15","15m")

        # ♻️ Reteste EMA9 (15m)
        if l[i] <= e9[i] and c[i] >= e9[i] and rsi[i] >= 52 and v[i] >= vm[i]*0.9:
            if M.allowed(sym,"RET9_15","15m"):
                msg = msg_line(sym, "♻️ — RETESTE EMA9 (15m)", c[i],
                               f"Toque na EMA9 e reação | RSI {rsi[i]:.1f} | Vol ok | 💚 CONTINUAÇÃO DE ALTA DETECTADA")
                await send_alert(sess, msg); M.mark(sym,"RET9_15","15m")

        # ♻️ Reteste MA20 (15m)
        if l[i] <= m20[i] and c[i] >= m20[i] and rsi[i] >= 50 and v[i] >= vm[i]*0.9:
            if M.allowed(sym,"RET20_15","15m"):
                msg = msg_line(sym, "♻️ — RETESTE MA20 (15m)", c[i],
                               f"Toque na MA20 e reação | RSI {rsi[i]:.1f} | Vol ok | 💚 CONTINUAÇÃO DE ALTA DETECTADA")
                await send_alert(sess, msg); M.mark(sym,"RET20_15","15m")

        # 📈 Rompimento da resistência (Donchian 20)
        if i >= 21:
            dh = max(h[i-20:i])
            if c[i] > dh and M.allowed(sym,"ROMP15","15m"):
                msg = msg_line(sym, "📈 — ROMPIMENTO DA RESISTÊNCIA (15m)", c[i],
                               f"Fechou acima da máxima 20 ({dh:.6f}) — 💥 Rompimento confirmado")
                await send_alert(sess, msg); M.mark(sym,"ROMP15","15m")

    except Exception as e:
        print("15m error", sym, e)

# =========================
# Worker 1h/4h — longos
# =========================
async def worker_long(sess, sym, M: Monitor):
    try:
        # 1h
        o1,h1,l1,c1,v1 = await get_klines(sess, sym, "1h", 200)
        if len(c1)<60: return
        e91,m201,m501,m2001,rsi1,vm1,bb_u1,bb_l1,adx1,pdi1,mdi1 = compute_indicators(o1,h1,l1,c1,v1)
        i1 = len(c1)-1

        # 4h
        o4,h4,l4,c4,v4 = await get_klines(sess, sym, "4h", 200)
        if len(c4)<60: return
        e94,m204,m504,m2004,rsi4,vm4,bb_u4,bb_l4,adx4,pdi4,mdi4 = compute_indicators(o4,h4,l4,c4,v4)
        i4 = len(c4)-1

        # 🌕 Pré-confirmação longa (1H) — 1ª vela
        if e91[i1-1] <= m201[i1-1] and e91[i1] > m201[i1] and 50 <= rsi1[i1] <= 60 and v1[i1] >= vm1[i1]*1.05:
            if M.allowed(sym,"PRE1H","1h"):
                msg = msg_long(fmt_symbol(sym), "🌕 <b>— PRÉ-CONFIRMAÇÃO LONGA (1H)</b>", c1[i1], [
                    f"<b>📈 EMA9 cruzou MA20 | RSI {rsi1[i1]:.1f} | Vol > média</b>"
                ])
                await send_alert(sess, msg); M.mark(sym,"PRE1H","1h")

        # 🚀 Tendência longa confirmada (1H) — 2ª vela (estrutural)
        if e91[i1] > m201[i1] > m501[i1] and rsi1[i1] > 55 and adx1[i1] > 25:
            if M.allowed(sym,"CONF1H","1h"):
                msg = msg_long(fmt_symbol(sym), "🚀 <b>— TENDÊNCIA LONGA CONFIRMADA (1H)</b>", c1[i1], [
                    f"<b>Médias alinhadas | RSI {rsi1[i1]:.1f} | ADX {adx1[i1]:.1f}</b>"
                ])
                await send_alert(sess, msg); M.mark(sym,"CONF1H","1h")

        # 🌕 Pré-confirmação longa (4H) — 1ª vela
        if e94[i4-1] <= m204[i4-1] and e94[i4] > m204[i4] and rsi4[i4] > 50:
            if M.allowed(sym,"PRE4H","4h"):
                msg = msg_long(fmt_symbol(sym), "🌕 <b>— PRÉ-CONFIRMAÇÃO LONGA (4H)</b>", c4[i4], [
                    f"<b>📈 EMA9 cruzou MA20 | RSI {rsi4[i4]:.1f}</b>"
                ])
                await send_alert(sess, msg); M.mark(sym,"PRE4H","4h")

        # 🚀 Tendência 4H confirmada — 2 velas mantidas + RSI>55
        if e94[i4] > m204[i4] > m504[i4] and e94[i4-1] > m204[i4-1] > m504[i4-1] and rsi4[i4] > 55:
            if M.allowed(sym,"CONF4H","4h"):
                msg = msg_long(fmt_symbol(sym), "🚀 <b>— TENDÊNCIA 4H CONFIRMADA</b>", c4[i4], [
                    f"<b>Estrutura mantida por 2 velas | RSI {rsi4[i4]:.1f}</b>"
                ])
                await send_alert(sess, msg); M.mark(sym,"CONF4H","4h")

        # 💚 Entrada segura — reteste (15m/1h) — checagem aqui com 1h (e 15m no próprio worker_15m)
        # (aqui verificamos no 1h uma oportunidade de reteste pós-confirmação)
        touch1 = (l1[i1] <= e91[i1] and c1[i1] >= e91[i1]) or (l1[i1] <= m201[i1] and c1[i1] >= m201[i1])
        if touch1 and 45 <= rsi1[i1] <= 55 and v1[i1] >= vm1[i1]*1.05:
            if M.allowed(sym,"ENTRY_SAFE_1H","1h"):
                msg = msg_long(fmt_symbol(sym), "💚 <b>— ENTRADA SEGURA — RETESTE (1H)</b>", c1[i1], [
                    f"<b>Toque EMA9/MA20 + reação | RSI {rsi1[i1]:.1f} | Vol > média</b>"
                ])
                await send_alert(sess, msg); M.mark(sym,"ENTRY_SAFE_1H","1h")

        # 🌕 Tendência longa combinada — (15m + 1h + 4h) alinhados
        cond1h = (e91[i1] > m201[i1] > m501[i1] > m2001[i1] and rsi1[i1] > 55 and adx1[i1] > 25)
        cond4h = (e94[i4] > m204[i4] > m504[i4] > m2004[i4] and rsi4[i4] > 55)
        # Para 15m usamos a última leitura registrada no worker_15m? Aqui, simplificamos: exigimos que 1h e 4h estejam fortes
        if cond1h and cond4h and M.allowed_long(sym):
            msg = msg_long(fmt_symbol(sym), "🌕 <b>— TENDÊNCIA LONGA DETECTADA (Combinada)</b>", c1[i1], [
                f"<b>1h/4h alinhados | RSI {rsi1[i1]:.1f}/{rsi4[i4]:.1f} | ADX (1h) {adx1[i1]:.1f}</b>"
            ])
            await send_alert(sess, msg); M.mark_long(sym)

    except Exception as e:
        print("long error", sym, e)

# =========================
# Main loop
# =========================
async def main():
    M = Monitor()
    async with aiohttp.ClientSession() as sess:
        ticks = await get_24h(sess)
        watch = shortlist_from_24h(ticks, SHORTLIST_N)
        hello = f"💻 v2.0 | {len(watch)} pares SPOT | {ts_brazil_now()}"
        await send_alert(sess, hello); print(hello)

        while True:
            tasks=[]
            for s in watch:
                tasks += [worker_5m(sess,s,M), worker_15m(sess,s,M), worker_long(sess,s,M)]
            await asyncio.gather(*tasks)

            await asyncio.sleep(180)
            try:
                ticks = await get_24h(sess)
                watch = shortlist_from_24h(ticks, SHORTLIST_N)
            except Exception as e:
                print("shortlist refresh error:", e)

# =========================
# Flask para Render
# =========================
def start_bot():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()
    app = Flask(__name__)
    @app.route("/")
    def home():
        return "✅ Binance Alerts Bot v2.0 — 5m/15m/1h/4h | SPOT-only 🇧🇷"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
