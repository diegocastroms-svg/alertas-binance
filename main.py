# main_v11_6_reversao_long.py
# Base: v11_6_reversao.py (INTACTA)
# Acrescimos (apenas):
#  1) PRECONF_LONG_1H      — EMA9 cruza MA20 no 1h, RSI 50–60, Vol > 1.2x
#  2) CONFIRM_LONG_1H      — EMA9>MA20>MA50 no 1h, RSI>55, ADX>25
#  3) PRECONF_4H           — EMA9 cruza MA20 no 4h, RSI>50, Vol > média
#  4) CONFIRM_4H           — EMA9>MA20>MA50 no 4h + confirmação na 2ª vela
#  5) LONG_COMBINED        — 15m/1h/4h: EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25
#  6) ENTRY_SAFE_RETEST    — (15m/1h) Toque EMA9/MA20 + RSI 45–55 + Vol +5%

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config (mantida) -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"         # Core curto
INTERVAL_CONF = "15m"        # Confirmação curta
SHORTLIST_N   = 65           # Mantido como na v11.6_reversao
COOLDOWN_SEC  = 15 * 60      # curto
COOLDOWN_LONGTERM = 60 * 60  # 1h por ativo p/ alertas longos
MIN_PCT       = 1.0
MIN_QV        = 300_000.0

EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
VOL_MA   = 9
BB_LEN   = 20
ADX_LEN  = 14

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils (mantidos) -----------------
def fmt_symbol(symbol):
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol):
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session, text):
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except:
            pass

def pct_change(new, old):
    return (new / (old + 1e-12) - 1.0) * 100.0

# ----------------- Indicadores (mantidos) -----------------
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq: return []
    out = []
    alpha = 2.0 / (span + 1.0)
    e = seq[0]; out.append(e)
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def rolling_std(seq, n):
    out, q = [], deque()
    for x in seq:
        q.append(x)
        if len(q) > n: q.popleft()
        m = sum(q) / len(q)
        var = sum((v - m) ** 2 for v in q) / len(q)
        out.append(math.sqrt(var))
    return out

def rsi_wilder(closes, period=14):
    if len(closes) == 0: return []
    deltas = [0.0] + [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    rsis = [50.0] * len(closes)
    if len(closes) < period + 1: return rsis
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    for i in range(period+1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsis[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsis

def true_range(h, l, c):
    tr = [0.0]
    for i in range(1, len(c)):
        tr_curr = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        tr.append(tr_curr)
    return tr

def adx(h, l, c, period=14):
    n = len(c)
    if n < period + 1: return [20.0] * n, [0.0]*n, [0.0]*n
    tr = true_range(h, l, c)
    plus_dm  = [0.0]; minus_dm = [0.0]
    for i in range(1, n):
        up_move   = h[i] - h[i-1]
        down_move = l[i-1] - l[i]
        plus_dm.append(  up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append( down_move if (down_move > up_move and down_move > 0) else 0.0)
    atr = [0.0]*n
    atr[period] = sum(tr[1:period+1])
    pdm = [0.0]*n; mdm = [0.0]*n
    pdm[period] = sum(plus_dm[1:period+1]); mdm[period] = sum(minus_dm[1:period+1])
    for i in range(period+1, n):
        atr[i] = atr[i-1] - (atr[i-1] / period) + tr[i]
        pdm[i] = pdm[i-1] - (pdm[i-1] / period) + plus_dm[i]
        mdm[i] = mdm[i-1] - (mdm[i-1] / period) + minus_dm[i]
    atr[:period] = [sum(tr[1:period+1])]*(period)
    pdm[:period] = [sum(plus_dm[1:period+1])]*(period)
    mdm[:period] = [sum(minus_dm[1:period+1])]*(period)
    plus_di  = [0.0]*n; minus_di = [0.0]*n
    for i in range(n):
        plus_di[i]  = 100.0 * (pdm[i] / (atr[i] + 1e-12))
        minus_di[i] = 100.0 * (mdm[i] / (atr[i] + 1e-12))
    dx = [0.0]*n
    for i in range(n):
        dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i] + 1e-12)
    adx_vals = [0.0]*n
    adx_vals[period] = sum(dx[1:period+1]) / period
    for i in range(period+1, n):
        adx_vals[i] = (adx_vals[i-1] * (period - 1) + dx[i]) / period
    for i in range(period):
        adx_vals[i] = adx_vals[period]
    return adx_vals, plus_di, minus_di

def compute_indicators(o,h,l,c,v):
    ema9  = ema(c, EMA_FAST)
    ma20  = sma(c, MA_SLOW)
    ma50  = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    bb_std = rolling_std(c, BB_LEN)
    bb_up  = [ma20[i] + 2 * bb_std[i] for i in range(len(bb_std))]
    bb_low = [ma20[i] - 2 * bb_std[i] for i in range(len(bb_std))]
    adx14, pdi, mdi = adx(h, l, c, ADX_LEN)
    return ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, pdi, mdi

# ----------------- Binance (mantido) -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:  # remove última vela em formação
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

async def get_spot_usdt_symbols(session):
    url = f"{BINANCE_HTTP}/api/v3/exchangeInfo"
    try:
        async with session.get(url, timeout=15) as r:
            r.raise_for_status()
            data = await r.json()
    except Exception as e:
        print("exchangeInfo error:", e)
        return None
    spot = set()
    blocked_tokens = ("UP","DOWN","BULL","BEAR")
    for s in data.get("symbols", []):
        try:
            sym = s.get("symbol","")
            status = s.get("status","")
            permissions = s.get("permissions", []) or []
            quote = s.get("quoteAsset","")
            if status != "TRADING": 
                continue
            if quote != "USDT":
                continue
            if "SPOT" not in permissions:
                continue
            if any(tok in sym for tok in blocked_tokens):
                continue
            if any(x in sym for x in ("PERP","_PERP","USD_","_BUSD","_FDUSD","_TUSD","_EUR","_TRY","_BRL","_USDC","_DAI","_BTC")):
                continue
            spot.add(sym)
        except:
            continue
    return spot

def shortlist_from_24h(tickers, n=400, spot_set=None):
    usdt = []
    for t in tickers:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        if spot_set is not None and s not in spot_set:
            continue
        blocked = ("UP","DOWN","BULL","BEAR","PERP","USD_","_PERP","_BUSD","_FDUSD","_TUSD","_EUR","_TRY","_BRL","_USDC","_DAI","_BTC")
        if any(x in s for x in blocked):
            continue
        pct = float(t.get("priceChangePercent", "0") or 0.0)
        qv  = float(t.get("quoteVolume", "0") or 0.0)
        if abs(pct) >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Mensagens (mantidas + novos tipos) -----------------
def kind_emoji(kind):
    return {
        "INICIO_5M":"🚀",
        "PRECONF_5M":"🌕",
        "PRECONF_15M":"🌕",
        "CONFIRM_15M":"🚀",
        "RETESTE_EMA9":"♻️",
        "RETESTE_MA20":"♻️",
        "PERDENDO_FORCA":"🟠",
        "SAIDA":"🚪",
        "MERCADO_ESTICADO":"⚠️",
        "TURTLE_BREAKOUT":"📈",
        "LONGTERM_TREND":"🌕",
        # novos
        "PRECONF_LONG_1H":"🌕",
        "CONFIRM_LONG_1H":"🚀",
        "PRECONF_4H":"🌕",
        "CONFIRM_4H":"🚀",
        "LONG_COMBINED":"🌕",
        "ENTRY_SAFE_RETEST":"💚",
    }.get(kind,"📌")

def build_msg(symbol, kind, price, bullets, rs_tag=""):
    star="⭐"; sym=fmt_symbol(symbol); em=kind_emoji(kind)
    tag = f" | 🏆 RS+" if rs_tag else ""
    title_map = {
        "INICIO_5M":"— INÍCIO (5M)",
        "PRECONF_5M":"— PRÉ-CONFIRMAÇÃO (5M)",
        "PRECONF_15M":"— PRÉ-CONFIRMAÇÃO (15M)",
        "CONFIRM_15M":"— CONFIRMAÇÃO (15M)",
        "TURTLE_BREAKOUT":"— ROMPIMENTO DA RESISTÊNCIA",
        # novos
        "PRECONF_LONG_1H":"— PRÉ-CONFIRMAÇÃO LONGA (1H)",
        "CONFIRM_LONG_1H":"— TENDÊNCIA LONGA CONFIRMADA (1H)",
        "PRECONF_4H":"— PRÉ-CONFIRMAÇÃO (4H)",
        "CONFIRM_4H":"— TENDÊNCIA 4H CONFIRMADA",
        "LONG_COMBINED":"— TENDÊNCIA LONGA COMBINADA",
        "ENTRY_SAFE_RETEST":"— ENTRADA SEGURA — RETESTE (15m/1h)",
    }
    header = title_map.get(kind, f"— {kind.replace('_',' ')}")
    return (
        f"{star} {sym} {em} {header}{tag}\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"🧠 {bullets}\n"
        f"⏰ {ts_brazil_now()}\n"
        f"{binance_links(symbol)}"
    )

def build_msg_longterm(symbol, price, rsi_val, adx_val):
    sym = fmt_symbol(symbol)
    return (
        f"🌕 <b>{sym} — TENDÊNCIA LONGA DETECTADA</b>\n"
        f"<b>💰 Preço:</b> <code>{price:.6f}</code>\n"
        f"<b>📈 Estrutura:</b> EMA9>MA20>MA50>MA200 (15m / 1h / 4h)\n"
        f"<b>⚙️ Força:</b> RSI {rsi_val:.1f} | ADX {adx_val:.1f}\n"
        f"<b>🕒 {ts_brazil_now()}</b>\n"
        f"<b>{binance_links(symbol)}</b>\n"
        f"<b>ALTA SUSTENTADA — MOVIMENTO DE VÁRIOS DIAS POSSÍVEL.</b>"
    )

# ----------------- Monitor (mantido) -----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)         # curto
        self.cooldown_long = defaultdict(lambda: 0.0)     # 1h por ativo (longos)
        self.rs_24h = {}
        self.btc_pct = 0.0

    def allowed(self, symbol, kind):
        return time.time() - self.cooldown[(symbol, kind)] >= COOLDOWN_SEC

    def mark(self, symbol, kind):
        self.cooldown[(symbol, kind)] = time.time()

    def allowed_long(self, symbol, kind="LONG_GENERIC"):
        # cooldown separado para alertas longos
        return time.time() - self.cooldown_long[(symbol, kind)] >= COOLDOWN_LONGTERM

    def mark_long(self, symbol, kind="LONG_GENERIC"):
        self.cooldown_long[(symbol, kind)] = time.time()

    def set_rs(self, rs_map, btc_pct):
        self.rs_24h = rs_map or {}
        self.btc_pct = btc_pct or 0.0

    def rs_tag(self, symbol):
        pct = self.rs_24h.get(symbol, None)
        if pct is None: return ""
        return "RS+" if (pct - self.btc_pct) > 0.0 else ""

# ----------------- Workers existentes (mantidos) -----------------
async def candle_worker(session, symbol, monitor: Monitor):
    # (igual ao v11_6_reversao) — com INÍCIO/PRÉ-5m, retestes e rompimento
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_MAIN, limit=200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, vol_ma, bb_up, bb_low, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        last = len(c)-1; prev = last-1
        rs_tag = monitor.rs_tag(symbol)

        # INÍCIO (5m)
        cruzou_ma20 = ema9[prev] <= ma20[prev] and ema9[last] > ma20[last]
        cruzou_ma50 = ema9[prev] <= ma50[prev] and ema9[last] > ma50[last]
        if (cruzou_ma20 and cruzou_ma50 and rsi14[last] >= 45.0 and v[last] >= vol_ma[last]*1.10
            and monitor.allowed(symbol,"INICIO_5M")):
            bullets = f"EMA9 cruzou MA20 e MA50 | RSI {rsi14[last]:.1f} | Vol {v[last]:.0f} (> média)"
            msg = build_msg(symbol,"INICIO_5M",c[last],bullets,rs_tag)
            await send_alert(session,msg); monitor.mark(symbol,"INICIO_5M")

        # PRÉ-CONFIRMAÇÃO (5m)
        todos_acima_now = (ema9[last] > ma200[last] and ma20[last] > ma200[last] and ma50[last] > ma200[last])
        todos_acima_prev = (ema9[prev] > ma200[prev] and ma20[prev] > ma200[prev] and ma50[prev] > ma200[prev])
        if (todos_acima_now and not todos_acima_prev and 50.0 <= rsi14[last] <= 55.5
            and monitor.allowed(symbol,"PRECONF_5M")):
            bullets = f"Médias 9/20/50 cruzaram acima da 200 | RSI {rsi14[last]:.1f}"
            msg = build_msg(symbol,"PRECONF_5M",c[last],bullets,rs_tag)
            await send_alert(session,msg); monitor.mark(symbol,"PRECONF_5M")

        # Reteste EMA9
        if (ema9[last]>ma20[last]>ma50[last] and l[last]<=ema9[last] and c[last]>=ema9[last] and
            rsi14[last]>=55.0 and v[last]>=vol_ma[last]*0.9 and monitor.allowed(symbol,"RETESTE_EMA9")):
            msg = build_msg(symbol,"RETESTE_EMA9",c[last],
                            f"Reteste na EMA9 + reação | RSI {rsi14[last]:.1f} | Vol ok | 💚 CONTINUAÇÃO DE ALTA DETECTADA",
                            rs_tag)
            await send_alert(session,msg); monitor.mark(symbol,"RETESTE_EMA9")

        # Reteste MA20
        if (ema9[last]>ma20[last]>ma50[last] and l[last]<=ma20[last] and c[last]>=ma20[last] and
            rsi14[last]>=52.0 and v[last]>=vol_ma[last]*0.9 and monitor.allowed(symbol,"RETESTE_MA20")):
            msg = build_msg(symbol,"RETESTE_MA20",c[last],
                            f"Reteste na MA20 + reação | RSI {rsi14[last]:.1f} | Vol ok | 💚 CONTINUAÇÃO DE ALTA DETECTADA",
                            rs_tag)
            await send_alert(session,msg); monitor.mark(symbol,"RETESTE_MA20")

        # Rompimento da resistência (Donchian 20)
        if len(h) >= 21:
            donch = max(h[-20:])
            if c[last] > donch and monitor.allowed(symbol,"TURTLE_BREAKOUT"):
                msg = build_msg(symbol,"TURTLE_BREAKOUT",c[last],
                                f"Rompimento: fechou acima da máxima 20 ({donch:.6f}) — 💥 Rompimento confirmado",
                                rs_tag)
                await send_alert(session,msg); monitor.mark(symbol,"TURTLE_BREAKOUT")

    except Exception as e:
        print("worker 5m error",symbol,e)

async def worker_15m(session, symbol, monitor: Monitor):
    # (igual ao v11_6_reversao) — com PRÉ/CONF 15m
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval="15m", limit=200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, vol_ma, bb_up, bb_low, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        last = len(c)-1; prev = last-1
        rs_tag = monitor.rs_tag(symbol)

        # PRÉ-CONFIRMAÇÃO (15m)
        cruzou_9_200 = (ema9[prev] <= ma200[prev] and ema9[last] > ma200[last])
        sloping_up = (ma20[last] > ma20[prev] and ma50[last] > ma50[prev])
        if (cruzou_9_200 and sloping_up and 50.0 <= rsi14[last] <= 55.5 and monitor.allowed(symbol,"PRECONF_15M")):
            bullets = f"EMA9 cruzou MA200 | MA20/50 ascendentes | RSI {rsi14[last]:.1f}"
            msg = build_msg(symbol,"PRECONF_15M",c[last],bullets,rs_tag)
            await send_alert(session,msg); monitor.mark(symbol,"PRECONF_15M")

        # CONFIRMAÇÃO (15m)
        if (ema9[last] > ma20[last] > ma50[last] > ma200[last] and rsi14[last] > 55.0 and adx14[last] > 25.0
            and monitor.allowed(symbol,"CONFIRM_15M")):
            bullets = f"EMA9>MA20>MA50>MA200 | RSI {rsi14[last]:.1f} | ADX {adx14[last]:.1f}"
            msg = build_msg(symbol,"CONFIRM_15M",c[last],bullets,rs_tag)
            await send_alert(session,msg); monitor.mark(symbol,"CONFIRM_15M")

    except Exception as e:
        print("worker 15m error",symbol,e)

# ----------------- Worker LONGO antigo (mantido em negrito) -----------------
async def longterm_worker(session, symbol, monitor: Monitor):
    try:
        # 15m
        o15,h15,l15,c15,v15 = await get_klines(session, symbol, interval="15m", limit=120)
        if len(c15) < 60: return
        ema9_15, ma20_15, ma50_15, ma200_15, rsi15, volma15, bbup15, bblow15, adx15, pdi15, mdi15 = compute_indicators(o15,h15,l15,c15,v15)
        last15 = len(c15)-1
        # 1h
        o1,h1,l1,c1,v1 = await get_klines(session, symbol, interval="1h", limit=120)
        if len(c1) < 60: return
        ema9_1, ma20_1, ma50_1, ma200_1, rsi1, volma1, bbup1, bblow1, adx1, pdi1, mdi1 = compute_indicators(o1,h1,l1,c1,v1)
        last1 = len(c1)-1
        # 4h
        o4,h4,l4,c4,v4 = await get_klines(session, symbol, interval="4h", limit=120)
        if len(c4) < 60: return
        ema9_4, ma20_4, ma50_4, ma200_4, rsi4, volma4, bbup4, bblow4, adx4, pdi4, mdi4 = compute_indicators(o4,h4,l4,c4,v4)
        last4 = len(c4)-1

        cond_15 = (ema9_15[last15] > ma20_15[last15] > ma50_15[last15] > ma200_15[last15] and rsi15[last15] > 55.0 and adx15[last15] > 25.0)
        cond_1  = (ema9_1[last1]   > ma20_1[last1]   > ma50_1[last1]   > ma200_1[last1]   and rsi1[last1]    > 55.0 and adx1[last1]    > 25.0)
        cond_4  = (ema9_4[last4]   > ma20_4[last4]   > ma50_4[last4]   > ma200_4[last4]   and rsi4[last4]    > 55.0 and adx4[last4]    > 25.0)

        if cond_15 and cond_1 and cond_4 and monitor.allowed_long(symbol, "LONGTERM_TREND"):
            last_price = c15[last15]
            rsi_mean = (rsi1[last1] + rsi4[last4]) / 2.0
            adx_mean = (adx1[last1] + adx4[last4]) / 2.0
            txt = build_msg_longterm(symbol, last_price, rsi_mean, adx_mean)
            await send_alert(session, txt)
            monitor.mark_long(symbol, "LONGTERM_TREND")

    except Exception as e:
        print("longterm error", symbol, e)

# ----------------- NOVO Worker: Alertas LONG (1h / 4h / combinado / entrada segura) -----------------
async def long_extensions_worker(session, symbol, monitor: Monitor):
    try:
        # ===== 1H =====
        o1,h1,l1,c1,v1 = await get_klines(session, symbol, interval="1h", limit=200)
        if len(c1) < 60: return
        ema9_1, ma20_1, ma50_1, ma200_1, rsi1, volma1, bbup1, bblow1, adx1, pdi1, mdi1 = compute_indicators(o1,h1,l1,c1,v1)
        last1 = len(c1)-1; prev1 = last1-1

        # 🌕 Pré-confirmação Longa (1H)
        cross_9_20_1h = (ema9_1[prev1] <= ma20_1[prev1] and ema9_1[last1] > ma20_1[last1])
        if (cross_9_20_1h and 50.0 <= rsi1[last1] <= 60.0 and v1[last1] >= volma1[last1]*1.20
            and monitor.allowed_long(symbol, "PRECONF_LONG_1H")):
            bullets = f"EMA9 cruzou MA20 | RSI {rsi1[last1]:.1f} | Vol {v1[last1]:.0f} (>{1.2:.1f}× média)"
            msg = build_msg(symbol, "PRECONF_LONG_1H", c1[last1], bullets, monitor.rs_tag(symbol))
            await send_alert(session, msg); monitor.mark_long(symbol, "PRECONF_LONG_1H")

        # 🚀 Tendência Longa Confirmada (1H)
        if (ema9_1[last1] > ma20_1[last1] > ma50_1[last1] and rsi1[last1] > 55.0 and adx1[last1] > 25.0
            and v1[last1] >= volma1[last1] and monitor.allowed_long(symbol, "CONFIRM_LONG_1H")):
            bullets = f"EMA9>MA20>MA50 | RSI {rsi1[last1]:.1f} | ADX {adx1[last1]:.1f}"
            msg = build_msg(symbol, "CONFIRM_LONG_1H", c1[last1], bullets, monitor.rs_tag(symbol))
            await send_alert(session, msg); monitor.mark_long(symbol, "CONFIRM_LONG_1H")

        # 💚 Entrada Segura — Reteste (15m / 1h) — parte 1H
        # Condições: estrutura altista, toque EMA9/MA20, RSI 45–55, volume +5% vs vela anterior
        touch_ema9_1h = (l1[last1] <= ema9_1[last1] and c1[last1] >= ema9_1[last1])
        touch_ma20_1h = (l1[last1] <= ma20_1[last1] and c1[last1] >= ma20_1[last1])
        if (ema9_1[last1] > ma20_1[last1] > ma50_1[last1] and (touch_ema9_1h or touch_ma20_1h)
            and 45.0 <= rsi1[last1] <= 55.0 and v1[last1] >= v1[prev1]*1.05
            and monitor.allowed_long(symbol, "ENTRY_SAFE_RETEST_1H")):
            ref_line = "EMA9" if touch_ema9_1h else "MA20"
            bullets = f"Reação positiva no reteste {ref_line} | RSI {rsi1[last1]:.1f} | Vol +5%"
            msg = build_msg(symbol, "ENTRY_SAFE_RETEST", c1[last1], bullets, monitor.rs_tag(symbol))
            await send_alert(session, msg); monitor.mark_long(symbol, "ENTRY_SAFE_RETEST_1H")

        # ===== 4H =====
        o4,h4,l4,c4,v4 = await get_klines(session, symbol, interval="4h", limit=200)
        if len(c4) < 60: return
        ema9_4, ma20_4, ma50_4, ma200_4, rsi4, volma4, bbup4, bblow4, adx4, pdi4, mdi4 = compute_indicators(o4,h4,l4,c4,v4)
        last4 = len(c4)-1; prev4 = last4-1; pprev4 = last4-2 if last4>=2 else last4-1

        # 🌕 Pré-confirmação (4H)
        cross_9_20_4h = (ema9_4[prev4] <= ma20_4[prev4] and ema9_4[last4] > ma20_4[last4])
        if (cross_9_20_4h and rsi4[last4] > 50.0 and v4[last4] >= volma4[last4]
            and monitor.allowed_long(symbol, "PRECONF_4H")):
            bullets = f"EMA9 cruzou MA20 | RSI {rsi4[last4]:.1f} | Vol ≥ média"
            msg = build_msg(symbol, "PRECONF_4H", c4[last4], bullets, monitor.rs_tag(symbol))
            await send_alert(session, msg); monitor.mark_long(symbol, "PRECONF_4H")

        # 🚀 Tendência 4H Confirmada — checa se é a 2ª vela após o cruzamento (aproximação)
        # Critério: prev4 já estava com EMA9>MA20 e pprev4 tinha EMA9<=MA20 (cruzamento ocorreu no prev4)
        second_bar_after_cross = (prev4 >= 1 and ema9_4[prev4] > ma20_4[prev4] and ema9_4[pprev4] <= ma20_4[pprev4])
        if (second_bar_after_cross and (ema9_4[last4] > ma20_4[last4] > ma50_4[last4]) and rsi4[last4] > 55.0
            and monitor.allowed_long(symbol, "CONFIRM_4H")):
            bullets = f"EMA9>MA20>MA50 | RSI {rsi4[last4]:.1f} — confirmação na 2ª vela 4H"
            msg = build_msg(symbol, "CONFIRM_4H", c4[last4], bullets, monitor.rs_tag(symbol))
            await send_alert(session, msg); monitor.mark_long(symbol, "CONFIRM_4H")

        # ===== COMBINADO 15m + 1h + 4h =====
        # Reuso de séries mais curtas para o 15m (pega últimas 120)
        o15,h15,l15,c15,v15 = await get_klines(session, symbol, interval="15m", limit=120)
        if len(c15) >= 60:
            ema9_15, ma20_15, ma50_15, ma200_15, rsi15, volma15, bbup15, bblow15, adx15, pdi15, mdi15 = compute_indicators(o15,h15,l15,c15,v15)
            last15 = len(c15)-1
            cond_15 = (ema9_15[last15] > ma20_15[last15] > ma50_15[last15] > ma200_15[last15] and rsi15[last15] > 55.0 and adx15[last15] > 25.0)
            cond_1  = (ema9_1[last1]   > ma20_1[last1]   > ma50_1[last1]   > ma200_1[last1]   and rsi1[last1]    > 55.0 and adx1[last1]    > 25.0)
            cond_4  = (ema9_4[last4]   > ma20_4[last4]   > ma50_4[last4]   > ma200_4[last4]   and rsi4[last4]    > 55.0 and adx4[last4]    > 25.0)
            if (cond_15 and cond_1 and cond_4 and monitor.allowed_long(symbol, "LONG_COMBINED")):
                bullets = "Estrutura completa: EMA9>MA20>MA50>MA200 (15m/1h/4h) | RSI/ADX fortes"
                msg = build_msg(symbol, "LONG_COMBINED", c1[last1], bullets, monitor.rs_tag(symbol))
                await send_alert(session, msg); monitor.mark_long(symbol, "LONG_COMBINED")

        # 💚 Entrada Segura — Reteste (15m) — parte 15m
        if len(c15) >= 3:
            prev15 = len(c15)-2
            touch_ema9_15 = (l15[-1] <= ema9_15[-1] and c15[-1] >= ema9_15[-1])
            touch_ma20_15 = (l15[-1] <= ma20_15[-1] and c15[-1] >= ma20_15[-1])
            if (ema9_15[-1] > ma20_15[-1] > ma50_15[-1] and (touch_ema9_15 or touch_ma20_15)
                and 45.0 <= rsi15[-1] <= 55.0 and v15[-1] >= v15[prev15]*1.05
                and monitor.allowed_long(symbol, "ENTRY_SAFE_RETEST_15M")):
                ref_line = "EMA9" if touch_ema9_15 else "MA20"
                bullets = f"Reação positiva no reteste {ref_line} | RSI {rsi15[-1]:.1f} | Vol +5%"
                msg = build_msg(symbol, "ENTRY_SAFE_RETEST", c15[-1], bullets, monitor.rs_tag(symbol))
                await send_alert(session, msg); monitor.mark_long(symbol, "ENTRY_SAFE_RETEST_15M")

    except Exception as e:
        print("long_extensions error", symbol, e)

# ----------------- Main (mantido e ampliado com novo worker) -----------------
async def main():
    monitor=Monitor()
    async with aiohttp.ClientSession() as session:
        spot_set = await get_spot_usdt_symbols(session)
        if spot_set is None:
            print("⚠️ Aviso: não foi possível obter exchangeInfo; usando filtro por nome como fallback.")

        tickers=await get_24h(session)
        watchlist=shortlist_from_24h(tickers,SHORTLIST_N, spot_set=spot_set)

        # RS (força relativa vs BTC)
        rs_map = {}; btc_pct = 0.0
        for t in tickers:
            s = t.get("symbol","")
            if s == "BTCUSDT":
                try: btc_pct = float(t.get("priceChangePercent","0") or 0.0)
                except: btc_pct = 0.0
            if s.endswith("USDT"):
                try: rs_map[s] = float(t.get("priceChangePercent","0") or 0.0)
                except: rs_map[s] = 0.0
        monitor.set_rs(rs_map, btc_pct)

        hello=f"💻 v11.6 (reversão++) | +6 alertas LONG (1h/4h/combinado/entrada segura) | Core intacto | SPOT-only | {len(watchlist)} pares | {ts_brazil_now()}"
        await send_alert(session,hello)
        print(hello)

        while True:
            tasks = []
            for s in watchlist:
                tasks.append(candle_worker(session, s, monitor))          # 5m
                tasks.append(worker_15m(session, s, monitor))             # 15m
                tasks.append(longterm_worker(session, s, monitor))        # 15m+1h+4h (negrito antigo)
                tasks.append(long_extensions_worker(session, s, monitor)) # NOVOS longos
            await asyncio.gather(*tasks)

            await asyncio.sleep(180)

            try:
                spot_set = await get_spot_usdt_symbols(session) or spot_set
                tickers=await get_24h(session)
                watchlist=shortlist_from_24h(tickers,SHORTLIST_N, spot_set=spot_set)

                rs_map = {}; btc_pct = 0.0
                for t in tickers:
                    s = t.get("symbol","")
                    if s == "BTCUSDT":
                        try: btc_pct = float(t.get("priceChangePercent","0") or 0.0)
                        except: btc_pct = 0.0
                    if s.endswith("USDT"):
                        try: rs_map[s] = float(t.get("priceChangePercent","0") or 0.0)
                        except: rs_map[s] = 0.0
                monitor.set_rs(rs_map, btc_pct)
            except Exception as e:
                print("Erro ao atualizar shortlist/RS/SPOT:", e)

# ----------------- Flask (mantido) -----------------
def start_bot():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

if __name__=="__main__":
    import threading
    threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home():
        return "✅ Binance Alerts Bot v11.6 (reversão++) — Core intacto + 6 alertas LONG (1h/4h/combinado/entrada segura) + SPOT-only 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
