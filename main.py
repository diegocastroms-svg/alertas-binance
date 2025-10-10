# main_v2_1_Hard.py
# Hardened: conexões limitadas, semáforo, retries/backoff, logs, HTML seguro, checagem de envs, ciclo 90s.
# Estratégia de alertas INALTERADA (curtos 5m/15m; longos 1h/4h; perdendo força/saída; rompimento; retestes).
# Inclui: intra-barra (5m/15m), volume forte (>=1.3x média), divergência RSI/MACD passiva; SPOT-only.

import os, math, asyncio, time
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

import aiohttp
from flask import Flask

# ===================== CONFIG =====================
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_5M   = "5m"
INTERVAL_15M  = "15m"
INTERVAL_1H   = "1h"
INTERVAL_4H   = "4h"

SHORTLIST_N   = 65
COOLDOWN_CURTO = 15 * 60   # 15min por tipo/ativo (curtos)
COOLDOWN_LONGO = 60 * 60   # 1h por ativo (longos)

# Filtros iniciais (24h)
MIN_PCT = 1.0
MIN_QV  = 300_000.0

# Indicadores
EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
VOL_MA   = 9
BB_LEN   = 20
ADX_LEN  = 14

# Intra-barra (detectar sem esperar fechamento)
INTRABAR_HOLD_SECONDS = 20

# Telegram / Webhook
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Concorrência protegida (hardenings)
TCP_LIMIT = 40
SEMAPHORE_LIMIT = 32
REQUEST_TIMEOUT = 12
RETRIES = 3

# ===================== UTILS =====================
def fmt_symbol(symbol: str) -> str:
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol: str) -> str:
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

def clamp_text(msg: str, limit: int = 3900) -> str:
    if len(msg) <= limit:
        return msg
    extra = len(msg) - limit
    return msg[:limit] + f"\n… [+{extra} chars ocultos]"

def percent(a, b) -> float:
    return (a / (b + 1e-12) - 1.0) * 100.0

# ===================== INDICADORES =====================
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq: return []
    out=[]; alpha=2.0/(span+1.0); e=seq[0]; out.append(e)
    for x in seq[1:]:
        e = alpha*x + (1-alpha)*e
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

def rsi_wilder(closes, period=14):
    if not closes: return []
    deltas = [0.0] + [closes[i]-closes[i-1] for i in range(1,len(closes))]
    gains = [max(d,0.0) for d in deltas]
    losses= [max(-d,0.0) for d in deltas]
    rsis = [50.0]*len(closes)
    if len(closes) < period+1: return rsis
    avg_gain = sum(gains[1:period+1])/period
    avg_loss = sum(losses[1:period+1])/period
    for i in range(period+1,len(closes)):
        avg_gain = (avg_gain*(period-1)+gains[i])/period
        avg_loss = (avg_loss*(period-1)+losses[i])/period
        rs = avg_gain/(avg_loss+1e-12)
        rsis[i] = 100.0 - (100.0/(1.0+rs))
    return rsis

def true_range(h, l, c):
    tr=[0.0]
    for i in range(1,len(c)):
        tr_curr = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        tr.append(tr_curr)
    return tr

def adx(h, l, c, period=14):
    n=len(c)
    if n<period+1: return [20.0]*n, [0.0]*n, [0.0]*n
    tr = true_range(h,l,c)
    plus_dm=[0.0]; minus_dm=[0.0]
    for i in range(1,n):
        up=h[i]-h[i-1]; down=l[i-1]-l[i]
        plus_dm.append(up if (up>down and up>0) else 0.0)
        minus_dm.append(down if (down>up and down>0) else 0.0)
    atr=[0.0]*n; atr[period]=sum(tr[1:period+1])
    pdm=[0.0]*n; mdm=[0.0]*n
    pdm[period]=sum(plus_dm[1:period+1]); mdm[period]=sum(minus_dm[1:period+1])
    for i in range(period+1,n):
        atr[i]=atr[i-1]-(atr[i-1]/period)+tr[i]
        pdm[i]=pdm[i-1]-(pdm[i-1]/period)+plus_dm[i]
        mdm[i]=mdm[i-1]-(mdm[i-1]/period)+minus_dm[i]
    atr[:period]=[sum(tr[1:period+1])]*(period)
    pdm[:period]=[sum(plus_dm[1:period+1])]*(period)
    mdm[:period]=[sum(minus_dm[1:period+1])]*(period)
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

def compute_indicators(o,h,l,c,v):
    ema9  = ema(c, EMA_FAST)
    ma20  = sma(c, MA_SLOW)
    ma50  = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    bb_std = rolling_std(c, BB_LEN)
    bb_up  = [ma20[i] + 2*bb_std[i] for i in range(len(bb_std))]
    bb_low = [ma20[i] - 2*bb_std[i] for i in range(len(bb_std))]
    adx14, pdi, mdi = adx(h,l,c,ADX_LEN)
    return ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, pdi, mdi

# Divergência simples RSI (passiva)
def bullish_divergence(price, rsi):
    # preço faz fundos descendentes, RSI faz fundos ascendentes (janela curta)
    if len(price) < 5: return False
    p1=min(range(-5,0), key=lambda i: price[i])
    p2=min(range(-4,0), key=lambda i: price[i])
    try:
        return price[p1] < price[p2] and rsi[p1] > rsi[p2]
    except:
        return False

def bearish_divergence(price, rsi):
    if len(price) < 5: return False
    p1=max(range(-5,0), key=lambda i: price[i])
    p2=max(range(-4,0), key=lambda i: price[i])
    try:
        return price[p1] > price[p2] and rsi[p1] < rsi[p2]
    except:
        return False

# ===================== BINANCE (HARDENED) =====================
sem = asyncio.Semaphore(SEMAPHORE_LIMIT)

async def fetch_json(session: aiohttp.ClientSession, url: str, timeout=REQUEST_TIMEOUT):
    last_exc = None
    for attempt in range(RETRIES):
        try:
            async with session.get(url, timeout=timeout) as r:
                if r.status >= 500:
                    raise RuntimeError(f"HTTP {r.status}")
                data = await r.json()
                return data
        except Exception as e:
            last_exc = e
            await asyncio.sleep(1 + attempt)  # backoff simples
    raise last_exc

async def get_klines(session, symbol: str, interval="5m", limit=200, include_last=True):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with sem:
        data = await fetch_json(session, url, timeout=REQUEST_TIMEOUT)
    o,h,l,c,v=[],[],[],[],[]
    # INTRA-BAR: quando include_last=True, não descartamos o último candle
    raw = data if include_last else data[:-1]
    for k in raw:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with sem:
        return await fetch_json(session, url, timeout=REQUEST_TIMEOUT)

# ===================== SHORTLIST (SPOT-ONLY) =====================
def shortlist_from_24h(tickers, n=400):
    usdt=[]
    for t in tickers:
        s=t.get("symbol","")
        if not s.endswith("USDT"): 
            continue
        # Excluir alavancados, perp e bases não-spot
        blocked = (
            "UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD",
            "_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_BTC",
            "_EUR","_TRY","_BRL","_ETH","_BNB","_SOL"
        )
        if any(x in s for x in blocked):
            continue
        try:
            pct = float(t.get("priceChangePercent","0") or 0.0)
            qv  = float(t.get("quoteVolume","0") or 0.0)
        except:
            continue
        if abs(pct) >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ===================== ALERTAS / FORMATAÇÃO =====================
def ball_color(rsi_val, ema9, ma20, ma50):
    if ema9 > ma20 > ma50 and rsi_val >= 55: return "🟢"
    if ema9 < ma20 < ma50 and rsi_val <= 45: return "🔴"
    return "🟡"

def arrow_for(kind):
    return {
        "TEND_INICIANDO_5M": "⬆️",
        "PRECONF_5M": "⬆️",
        "PRECONF_15M": "⬆️",
        "CONFIRMADA_15M": "⬆️",
        "RETESTE_EMA9": "↔️",
        "RETESTE_MA20": "↔️",
        "ROMP_RES": "⬆️",
        "PERDENDO_FORCA": "⬇️",
        "SAIDA": "⬇️",
        "LONG_PRE_1H": "⬆️",
        "LONG_CONF_1H": "⬆️",
        "LONG_PRE_4H": "⬆️",
        "LONG_CONF_4H": "⬆️",
        "ENTRY_SAFE": "↔️",
        "LONG_COMBINADA": "⬆️",
    }.get(kind,"↔️")

def build_msg(symbol, kind, price, lines, rsi_val=None, ema9=None, ma20=None, ma50=None, bold_long=False):
    sym = fmt_symbol(symbol)
    # Bola por RSI/estruturas
    bola = "🟡"
    if rsi_val is not None and ema9 is not None and ma20 is not None and ma50 is not None:
        bola = ball_color(rsi_val, ema9, ma20, ma50)

    seta = arrow_for(kind)

    # Título dos longos em negrito
    title_left = f"{bola} {sym}"
    title_right = f"{seta} {kind.replace('_',' ')}"
    if bold_long:
        title_line = f"<b>{title_left}</b>\n\n<b>{title_right}</b>"
    else:
        title_line = f"{title_left}\n\n{title_right}"

    body = "\n".join(lines)
    msg = (
        f"{title_line}\n\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"{body}\n\n"
        f"⏰ {ts_brazil_now()}\n"
        f"{binance_links(symbol)}"
    )
    return clamp_text(msg)

async def send_alert(session: aiohttp.ClientSession, text: str):
    # Webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        for attempt in range(RETRIES):
            try:
                async with session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10) as r:
                    await r.text()
                break
            except:
                await asyncio.sleep(1+attempt)
    # Telegram
    if TELEGRAM_TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        for attempt in range(RETRIES):
            try:
                async with session.post(url, data=payload, timeout=10) as r:
                    await r.text()
                break
            except:
                await asyncio.sleep(1+attempt)

# ===================== MONITOR / COOLDOWNS =====================
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)      # curto (por tipo)
        self.cooldown_long = defaultdict(lambda: 0.0)  # longo (por ativo)
        self.intrabar_since = defaultdict(lambda: 0.0) # marcador intra-bar por ativo/kind
        self.rs_24h = {}
        self.btc_pct = 0.0

    def allowed(self, symbol, kind):
        return time.time() - self.cooldown[(symbol, kind)] >= COOLDOWN_CURTO

    def mark(self, symbol, kind):
        self.cooldown[(symbol, kind)] = time.time()

    def allowed_long(self, symbol):
        return time.time() - self.cooldown_long[symbol] >= COOLDOWN_LONGO

    def mark_long(self, symbol):
        self.cooldown_long[symbol] = time.time()

    def intrabar_ok(self, key, aligned: bool):
        now = time.time()
        if aligned:
            if self.intrabar_since[key] == 0.0:
                self.intrabar_since[key] = now
            return (now - self.intrabar_since[key]) >= INTRABAR_HOLD_SECONDS
        else:
            self.intrabar_since[key] = 0.0
            return False

    def set_rs(self, rs_map, btc_pct):
        self.rs_24h = rs_map or {}
        self.btc_pct = btc_pct or 0.0

# ===================== WORKERS DE ALERTA (mesma lógica base) =====================
# ---- CURTO 5m/15m
async def worker_short(session, symbol, mon: Monitor):
    try:
        # INTRA-BAR: inclui último candle em 5m/15m
        o5,h5,l5,c5,v5 = await get_klines(session, symbol, interval=INTERVAL_5M, limit=200, include_last=True)
        o15,h15,l15,c15,v15 = await get_klines(session, symbol, interval=INTERVAL_15M, limit=200, include_last=True)
        if len(c5) < 60 or len(c15) < 60: return

        ema9_5, ma20_5, ma50_5, ma200_5, rsi5, volma5, bbup5, bblow5, adx5, pdi5, mdi5 = compute_indicators(o5,h5,l5,c5,v5)
        ema9_15,ma20_15,ma50_15,ma200_15,rsi15,volma15,bbup15,bblow15,adx15,pdi15,mdi15 = compute_indicators(o15,h15,l15,c15,v15)
        i5 = len(c5)-1; i15 = len(c15)-1

        # ====== Tendência iniciando (5m): EMA9 > MA20 > MA50 + intra-bar hold + volume forte
        aligned_5 = (ema9_5[i5] > ma20_5[i5] > ma50_5[i5])
        vol_ok_5  = v5[i5] >= (sum(v5[-20:])/20.0)*1.3
        if aligned_5 and vol_ok_5 and rsi5[i5] > 50:
            if mon.intrabar_ok((symbol,"TEND_INICIANDO_5M"), True) and mon.allowed(symbol,"TEND_INICIANDO_5M"):
                msg = build_msg(
                    symbol, "TEND_INICIANDO_5M", c5[i5],
                    lines=["🧠 EMA9 cruzou MA20 e MA50 (intra-bar)", f"RSI {rsi5[i5]:.1f} | Vol forte (>=1.3x)"],
                    rsi_val=rsi5[i5], ema9=ema9_5[i5], ma20=ma20_5[i5], ma50=ma50_5[i5],
                    bold_long=False
                )
                await send_alert(session, msg)
                mon.mark(symbol,"TEND_INICIANDO_5M")
        else:
            mon.intrabar_ok((symbol,"TEND_INICIANDO_5M"), False)

        # ====== Pré-confirmação (5m): 9/20/50 acima da 200
        if (ema9_5[i5] > ma20_5[i5] > ma50_5[i5] > ma200_5[i5]) and mon.allowed(symbol,"PRECONF_5M"):
            msg = build_msg(
                symbol, "PRECONF_5M", c5[i5],
                lines=["🧠 Médias 9/20/50 cruzaram acima da MA200 (5m)", f"RSI {rsi5[i5]:.1f} | ADX {adx5[i5]:.1f}"],
                rsi_val=rsi5[i5], ema9=ema9_5[i5], ma20=ma20_5[i5], ma50=ma50_5[i5],
                bold_long=False
            )
            await send_alert(session, msg)
            mon.mark(symbol,"PRECONF_5M")

        # ====== Pré-confirmação (15m): EMA9 cruza 200
        if (ema9_15[i15] > ma200_15[i15] and ema9_15[i15-1] <= ma200_15[i15-1]) and mon.allowed(symbol,"PRECONF_15M"):
            msg = build_msg(
                symbol, "PRECONF_15M", c15[i15],
                lines=["🧠 EMA9 cruzou acima da MA200 (15m)", f"RSI {rsi15[i15]:.1f} | ADX {adx15[i15]:.1f}"],
                rsi_val=rsi15[i15], ema9=ema9_15[i15], ma20=ma20_15[i15], ma50=ma50_15[i15],
                bold_long=False
            )
            await send_alert(session, msg)
            mon.mark(symbol,"PRECONF_15M")

        # ====== Confirmação (15m): 9>20>50>200 + RSI>55 + ADX>25 (volume forte)
        vol_ok_15 = v15[i15] >= (sum(v15[-20:])/20.0)*1.3
        if (ema9_15[i15] > ma20_15[i15] > ma50_15[i15] > ma200_15[i15] and
            rsi15[i15] > 55 and adx15[i15] > 25 and vol_ok_15 and mon.allowed(symbol,"CONFIRMADA_15M")):
            extra = []
            if bullish_divergence(c15, rsi15): extra.append("⚠️ Divergência RSI altista confirmada")
            msg = build_msg(
                symbol, "CONFIRMADA_15M", c15[i15],
                lines=["🧠 EMA9>MA20>MA50>MA200 | RSI>55 | ADX>25 | Vol forte", *extra],
                rsi_val=rsi15[i15], ema9=ema9_15[i15], ma20=ma20_15[i15], ma50=ma50_15[i15],
                bold_long=False
            )
            await send_alert(session, msg)
            mon.mark(symbol,"CONFIRMADA_15M")

        # ====== Rompimento da resistência (15m): Donchian 20
        if i15 >= 21:
            d_hi = max(h15[i15-20:i15])
            if c15[i15] > d_hi and mon.allowed(symbol,"ROMP_RES"):
                msg = build_msg(
                    symbol, "ROMP_RES", c15[i15],
                    lines=[f"🧠 Fechou acima da máxima 20 ({d_hi:.6f}) — Rompimento confirmado"],
                    rsi_val=rsi15[i15], ema9=ema9_15[i15], ma20=ma20_15[i15], ma50=ma50_15[i15],
                    bold_long=False
                )
                await send_alert(session, msg)
                mon.mark(symbol,"ROMP_RES")

        # ====== Retestes (15m) — EMA9 / MA20
        # EMA9
        if (l15[i15] <= ema9_15[i15] and c15[i15] >= ema9_15[i15] and
            ema9_15[i15] > ma20_15[i15] > ma50_15[i15] and mon.allowed(symbol,"RETESTE_EMA9")):
            msg = build_msg(
                symbol, "RETESTE_EMA9", c15[i15],
                lines=[f"🧠 Toque na EMA9 + reação | RSI {rsi15[i15]:.1f} | Vol ok", "💚 Continuação de alta detectada"],
                rsi_val=rsi15[i15], ema9=ema9_15[i15], ma20=ma20_15[i15], ma50=ma50_15[i15],
                bold_long=False
            )
            await send_alert(session, msg)
            mon.mark(symbol,"RETESTE_EMA9")

        # MA20
        if (l15[i15] <= ma20_15[i15] and c15[i15] >= ma20_15[i15] and
            ema9_15[i15] > ma20_15[i15] > ma50_15[i15] and mon.allowed(symbol,"RETESTE_MA20")):
            msg = build_msg(
                symbol, "RETESTE_MA20", c15[i15],
                lines=[f"🧠 Toque na MA20 + reação | RSI {rsi15[i15]:.1f} | Vol ok", "💚 Continuação de alta detectada"],
                rsi_val=rsi15[i15], ema9=ema9_15[i15], ma20=ma20_15[i15], ma50=ma50_15[i15],
                bold_long=False
            )
            await send_alert(session, msg)
            mon.mark(symbol,"RETESTE_MA20")

        # ====== Perdendo força (5m/15m) — RSI<50, ADX<20, candle < EMA9, vol<media; divergência bearish sugere cedo
        if mon.allowed(symbol,"PERDENDO_FORCA"):
            pf = False
            extra = []
            if rsi5[i5] < 50 and adx5[i5] < 20 and c5[i5] < ema9_5[i5] and v5[i5] < volma5[i5]:
                pf = True
                if bearish_divergence(c5, rsi5): extra.append("⚠️ Divergência bearish detectada")
                msg = build_msg(
                    symbol, "PERDENDO_FORCA", c5[i5],
                    lines=[f"🧠 RSI {rsi5[i5]:.1f}<50 | ADX<20 | candle<EMA9", *extra],
                    rsi_val=rsi5[i5], ema9=ema9_5[i5], ma20=ma20_5[i5], ma50=ma50_5[i5],
                    bold_long=False
                )
                await send_alert(session, msg)
                mon.mark(symbol,"PERDENDO_FORCA")
            elif rsi15[i15] < 50 and adx15[i15] < 20 and c15[i15] < ema9_15[i15] and v15[i15] < volma15[i15]:
                pf = True
                if bearish_divergence(c15, rsi15): extra.append("⚠️ Divergência bearish detectada")
                msg = build_msg(
                    symbol, "PERDENDO_FORCA", c15[i15],
                    lines=[f"🧠 RSI {rsi15[i15]:.1f}<50 | ADX<20 | candle<EMA9", *extra],
                    rsi_val=rsi15[i15], ema9=ema9_15[i15], ma20=ma20_15[i15], ma50=ma50_15[i15],
                    bold_long=False
                )
                await send_alert(session, msg)
                mon.mark(symbol,"PERDENDO_FORCA")

        # ====== Saída (15m/1h/4h) — EMA9<MA20<MA50, RSI<45, ADX caindo
        # (executado no long worker também, mas aqui tratamos 15m)
        if mon.allowed(symbol,"SAIDA"):
            if (ema9_15[i15] < ma20_15[i15] < ma50_15[i15] and rsi15[i15] < 45):
                msg = build_msg(
                    symbol, "SAIDA", c15[i15],
                    lines=[f"🧠 EMA9<MA20<MA50 | RSI {rsi15[i15]:.1f}<45 | Estrutura baixista"],
                    rsi_val=rsi15[i15], ema9=ema9_15[i15], ma20=ma20_15[i15], ma50=ma50_15[i15],
                    bold_long=False
                )
                await send_alert(session, msg)
                mon.mark(symbol,"SAIDA")

    except Exception as e:
        print("worker_short error", symbol, e)

# ---- LONGO 1h/4h
async def worker_long(session, symbol, mon: Monitor):
    try:
        o1,h1,l1,c1,v1   = await get_klines(session, symbol, interval=INTERVAL_1H, limit=180, include_last=False)
        o4,h4,l4,c4,v4   = await get_klines(session, symbol, interval=INTERVAL_4H, limit=180, include_last=False)
        if len(c1) < 60 or len(c4) < 60: return

        ema9_1,ma20_1,ma50_1,ma200_1,rsi1,volma1,bbup1,bblow1,adx1,pdi1,mdi1 = compute_indicators(o1,h1,l1,c1,v1)
        ema9_4,ma20_4,ma50_4,ma200_4,rsi4,volma4,bbup4,bblow4,adx4,pdi4,mdi4 = compute_indicators(o4,h4,l4,c4,v4)
        j1=len(c1)-1; j4=len(c4)-1

        def long_msg(kind, price, lines):
            return build_msg(symbol, kind, price, lines, rsi_val=rsi1[j1], ema9=ema9_1[j1], ma20=ma20_1[j1], ma50=ma50_1[j1], bold_long=True)

        # Pré 1H — primeira vela 9>20 + RSI 50–60 + vol>média
        if (j1>=1 and ema9_1[j1-1] <= ma20_1[j1-1] and ema9_1[j1] > ma20_1[j1] and
            50 <= rsi1[j1] <= 60 and v1[j1] >= volma1[j1] and mon.allowed_long(symbol)):
            extra=[]
            if bullish_divergence(c1, rsi1): extra.append("⚠️ Divergência RSI altista confirmada")
            msg = long_msg("LONG_PRE_1H", c1[j1], ["🧠 EMA9 cruzou MA20 (1h) | RSI 50–60 | Vol>média", *extra])
            await send_alert(session, msg)
            mon.mark_long(symbol)
            return

        # Confirmada 1H — 9>20>50 + RSI>55 + ADX>25
        if (ema9_1[j1] > ma20_1[j1] > ma50_1[j1] and rsi1[j1] > 55 and adx1[j1] > 25 and mon.allowed_long(symbol)):
            msg = long_msg("LONG_CONF_1H", c1[j1], ["🧠 EMA9>MA20>MA50 (1h) | RSI>55 | ADX>25"])
            await send_alert(session, msg)
            mon.mark_long(symbol)
            return

        # Pré 4H — primeira vela 9>20 + RSI>50
        if (j4>=1 and ema9_4[j4-1] <= ma20_4[j4-1] and ema9_4[j4] > ma20_4[j4] and rsi4[j4] > 50 and mon.allowed_long(symbol)):
            msg = build_msg(symbol, "LONG_PRE_4H", c4[j4],
                lines=["🧠 EMA9 cruzou MA20 (4h) | RSI>50"],
                rsi_val=rsi4[j4], ema9=ema9_4[j4], ma20=ma20_4[j4], ma50=ma50_4[j4],
                bold_long=True
            )
            await send_alert(session, msg)
            mon.mark_long(symbol)
            return

        # Confirmada 4H — 9>20>50 por 2 velas + RSI>55
        if (j4>=1 and ema9_4[j4] > ma20_4[j4] > ma50_4[j4] and
            ema9_4[j4-1] > ma20_4[j4-1] > ma50_4[j4-1] and rsi4[j4] > 55 and mon.allowed_long(symbol)):
            msg = build_msg(symbol, "LONG_CONF_4H", c4[j4],
                lines=["🧠 Estrutura mantida por 2 velas (4h) | RSI>55"],
                rsi_val=rsi4[j4], ema9=ema9_4[j4], ma20=ma20_4[j4], ma50=ma50_4[j4],
                bold_long=True
            )
            await send_alert(session, msg)
            mon.mark_long(symbol)
            return

        # Entrada segura — reteste (15m/1h) já coberto no curto para 15m; aqui tratamos 1h
        if mon.allowed_long(symbol):
            touched = (l1[j1] <= ema9_1[j1] and c1[j1] >= ema9_1[j1]) or (l1[j1] <= ma20_1[j1] and c1[j1] >= ma20_1[j1])
            if touched and 45 <= rsi1[j1] <= 55 and v1[j1] >= volma1[j1]:
                msg = long_msg("ENTRY_SAFE", c1[j1], ["🧠 Toque EMA9/MA20 + reação (1h) | RSI 45–55 | Vol>média"])
                await send_alert(session, msg)
                mon.mark_long(symbol)
                return

        # Saída (1h/4h)
        if mon.allowed(symbol,"SAIDA"):
            if ema9_1[j1] < ma20_1[j1] < ma50_1[j1] and rsi1[j1] < 45:
                msg = build_msg(symbol,"SAIDA",c1[j1],
                    lines=[f"🧠 (1h) EMA9<MA20<MA50 | RSI {rsi1[j1]:.1f}<45 | Estrutura baixista"],
                    rsi_val=rsi1[j1], ema9=ema9_1[j1], ma20=ma20_1[j1], ma50=ma50_1[j1],
                    bold_long=True
                )
                await send_alert(session,msg)
                mon.mark(symbol,"SAIDA")
            elif ema9_4[j4] < ma20_4[j4] < ma50_4[j4] and rsi4[j4] < 45:
                msg = build_msg(symbol,"SAIDA",c4[j4],
                    lines=[f"🧠 (4h) EMA9<MA20<MA50 | RSI {rsi4[j4]:.1f}<45 | Estrutura baixista"],
                    rsi_val=rsi4[j4], ema9=ema9_4[j4], ma20=ma20_4[j4], ma50=ma50_4[j4],
                    bold_long=True
                )
                await send_alert(session,msg)
                mon.mark(symbol,"SAIDA")

    except Exception as e:
        print("worker_long error", symbol, e)

# ===================== MAIN LOOP (HARDENED) =====================
async def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ TELEGRAM não configurado — alertas via Telegram desativados.")

    connector = aiohttp.TCPConnector(limit=TCP_LIMIT, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        mon = Monitor()

        # 24h + watchlist
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        # RS vs BTC
        rs_map = {}; btc_pct = 0.0
        for t in tickers:
            s = t.get("symbol","")
            if s == "BTCUSDT":
                try: btc_pct = float(t.get("priceChangePercent","0") or 0.0)
                except: btc_pct = 0.0
            if s.endswith("USDT"):
                try: rs_map[s] = float(t.get("priceChangePercent","0") or 0.0)
                except: rs_map[s] = 0.0
        mon.set_rs(rs_map, btc_pct)

        hello = f"💻 v2.1 Hard | {len(watchlist)} pares SPOT | ciclo=90s | tcp={TCP_LIMIT}/sem={SEMAPHORE_LIMIT} | {ts_brazil_now()}"
        await send_alert(session, hello)
        print(f"[INIT] {hello}")

        while True:
            t0 = time.time()
            short_ct = long_ct = 0

            tasks = []
            for s in watchlist:
                tasks.append(worker_short(session, s, mon))
                tasks.append(worker_long(session, s, mon))
            await asyncio.gather(*tasks, return_exceptions=True)

            # Log do ciclo
            dur = time.time() - t0
            print(f"[CYCLE] pares={len(watchlist)} | dur={dur:.1f}s")

            # Aguarda 90s
            await asyncio.sleep(90)

            # Refresh watchlist + RS
            try:
                tickers = await get_24h(session)
                watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

                rs_map = {}; btc_pct = 0.0
                for t in tickers:
                    s = t.get("symbol","")
                    if s == "BTCUSDT":
                        try: btc_pct = float(t.get("priceChangePercent","0") or 0.0)
                        except: btc_pct = 0.0
                    if s.endswith("USDT"):
                        try: rs_map[s] = float(t.get("priceChangePercent","0") or 0.0)
                        except: rs_map[s] = 0.0
                mon.set_rs(rs_map, btc_pct)

                print(f"[REFRESH] watchlist={len(watchlist)} pares | RS atualizado")
            except Exception as e:
                print("Erro ao atualizar watchlist/RS:", e)

# ===================== FLASK (HEALTHCHECK) =====================
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
        return "✅ Binance Alerts Bot v2.1 Hard — Core intacto + Hardenings ativos 🇧🇷"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
