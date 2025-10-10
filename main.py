# main_v3_1.py
# Curto: 5m/15m | Longo: 1h/4h
# v3.1 — 5m só INÍCIO e PRÉ; 15m/1h/4h: pré/confirm + reteste + rompimento; Entrada Segura em 15m e 1h
import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"

INTERVAL_5M  = "5m"
INTERVAL_15M = "15m"
INTERVAL_1H  = "1h"
INTERVAL_4H  = "4h"

SHORTLIST_N = 65                 # nº de pares SPOT monitorados
SCAN_INTERVAL_SEC = 15           # ciclo de verificação (intrabar)
COOLDOWN_SHORT_SEC = 15 * 60     # 5m e 15m
COOLDOWN_LONG_SEC  = 60 * 60     # 1h e 4h

# Filtros 24h
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
DONCHIAN_N = 20

# Env
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
def fmt_symbol(symbol: str) -> str:
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol: str) -> str:
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session: aiohttp.ClientSession, text: str):
    # Webhook opcional
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

# ----------------- Indicadores -----------------
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
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, n):
        up   = h[i] - h[i-1]
        down = l[i-1] - l[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    atr = [0.0]*n
    atr[period] = sum(tr[1:period+1])
    pdm = [0.0]*n; mdm = [0.0]*n
    pdm[period] = sum(plus_dm[1:period+1]); mdm[period] = sum(minus_dm[1:period+1])
    for i in range(period+1, n):
        atr[i] = atr[i-1] - (atr[i-1]/period) + tr[i]
        pdm[i] = pdm[i-1] - (pdm[i-1]/period) + plus_dm[i]
        mdm[i] = mdm[i-1] - (mdm[i-1]/period) + minus_dm[i]
    atr[:period] = [sum(tr[1:period+1])]*period
    pdm[:period] = [sum(plus_dm[1:period+1])]*period
    mdm[:period] = [sum(minus_dm[1:period+1])]*period
    plus_di = [0.0]*n; minus_di = [0.0]*n
    for i in range(n):
        plus_di[i]  = 100.0 * (pdm[i] / (atr[i] + 1e-12))
        minus_di[i] = 100.0 * (mdm[i] / (atr[i] + 1e-12))
    dx = [0.0]*n
    for i in range(n):
        dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i] + 1e-12)
    adx_vals = [0.0]*n
    adx_vals[period] = sum(dx[1:period+1]) / period
    for i in range(period+1, n):
        adx_vals[i] = (adx_vals[i-1]*(period-1) + dx[i]) / period
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
    bbstd = rolling_std(c, BB_LEN)
    bb_up  = [ma20[i] + 2*bbstd[i] for i in range(len(bbstd))]
    bb_low = [ma20[i] - 2*bbstd[i] for i in range(len(bbstd))]
    adx14, pdi, mdi = adx(h,l,c, ADX_LEN)
    return ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, pdi, mdi

# ----------------- Binance -----------------
async def get_klines(session, symbol, interval="5m", limit=200, include_partial=True):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    if not include_partial and len(data) > 0:
        data = data[:-1]
    o,h,l,c,v = [],[],[],[],[]
    for k in data:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=400):
    usdt = []
    for t in tickers:
        s = t.get("symbol","")
        if not s.endswith("USDT"):
            continue
        # Exclui alavancados, perp e moedas não SPOT padrões
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
            pct, qv = 0.0, 0.0
        if abs(pct) >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, abs(pct), qv))
    usdt.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Emojis / Kinds -----------------
K_SHORT = {
    "INICIO_5M": "🚀",
    "PRECONFIRM_5M": "🌕",
    "PRECONFIRM_15M": "🌕",
    "CONFIRM_15M": "🚀",
    "RETESTE_EMA9_15M": "♻️",
    "RETESTE_MA20_15M": "♻️",
    "BREAKOUT_15M": "📈",
    "ENTRYSAFE_15M": "💚",
}
K_LONG = {
    "PRECONFIRM_1H": "🌕",
    "CONFIRM_1H": "🚀",
    "RETESTE_1H": "♻️",
    "BREAKOUT_1H": "📈",
    "ENTRYSAFE_1H": "💚",
    "PRECONFIRM_4H": "🌕",
    "CONFIRM_4H": "🚀",
    "RETESTE_4H": "♻️",
    "BREAKOUT_4H": "📈",
}

SHORT_KINDS = set(K_SHORT.keys())
LONG_KINDS  = set(K_LONG.keys())

def build_msg(symbol, title, price, bullets, bold=False):
    sym = fmt_symbol(symbol)
    head = f"{sym} — {title}"
    if bold:
        head = f"<b>{head}</b>"
    details = " | ".join(bullets) if isinstance(bullets, (list,tuple)) else str(bullets)
    return (
        f"⭐ {head}\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"🧠 {details}\n"
        f"⏰ {ts_brazil_now()}\n"
        f"{binance_links(symbol)}"
    )

# ----------------- Estado / Cooldown -----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)
        # Estado de tendência por símbolo (para 5m silenciar após pré-confirmação)
        self.state_5m = defaultdict(lambda: 0)  # 0=none, 1=iniciada, 2=preconfirmada
    def _cd_for(self, kind: str) -> int:
        return COOLDOWN_LONG_SEC if kind in LONG_KINDS else COOLDOWN_SHORT_SEC
    def allowed(self, symbol: str, kind: str) -> bool:
        return time.time() - self.cooldown[(symbol, kind)] >= self._cd_for(kind)
    def mark(self, symbol: str, kind: str):
        self.cooldown[(symbol, kind)] = time.time()
    def stage5m(self, symbol: str) -> int:
        return self.state_5m[symbol]
    def set_stage5m(self, symbol: str, val: int):
        self.state_5m[symbol] = val
    def reset_if_reversal_5m(self, ema9, ma20, ma50, ma200, idx, symbol: str):
        # Se perder estrutura, permite novo ciclo de início de tendência
        if not (ema9[idx] > ma20[idx] and ema9[idx] > ma50[idx] and ema9[idx] > ma200[idx]):
            self.state_5m[symbol] = 0

# ----------------- Detecções auxiliares -----------------
def crossed_up(a_prev, a_now, b_prev, b_now):
    return a_prev <= b_prev and a_now > b_now

def is_lateralizing(close, ma20, win=12, band=0.008):
    if len(close) < win+1: return False
    seg = close[-win:]
    mseg = sum(seg)/len(seg)
    max_dev = max(abs(x - mseg)/max(1e-9, mseg) for x in seg)
    drift = abs(seg[-1] - seg[0]) / max(1e-9, mseg)
    flat_ma = abs(seg[-1] - seg[0]) / max(1e-9, ma20[-1])
    return (max_dev < band) and (drift < band) and (flat_ma < band)

# ----------------- Workers -----------------
async def worker_5m(session, symbol, mon: Monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_5M, limit=200, include_partial=True)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, *_ = compute_indicators(o,h,l,c,v)
        i = len(c)-1; ip = i-1

        # Reset se perder estrutura
        mon.reset_if_reversal_5m(ema9, ma20, ma50, ma200, i, symbol)

        # 1) Tendência iniciando (5m): EMA9 cruza MA20 e supera MA50 APÓS lateralização
        if mon.stage5m(symbol) == 0:
            if is_lateralizing(c, ma20) and (crossed_up(ema9[ip], ema9[i], ma20[ip], ma20[i]) and ema9[i] > ma50[i]):
                if mon.allowed(symbol, "INICIO_5M"):
                    msg = build_msg(
                        symbol,
                        "TENDÊNCIA INICIANDO (5m)",
                        c[i],
                        [f"EMA9 cruzou MA20 e superou MA50", f"RSI {rsi14[i]:.1f} | Vol {v[i]:.0f}"]
                    )
                    await send_alert(session, msg)
                    mon.mark(symbol, "INICIO_5M")
                    mon.set_stage5m(symbol, 1)

        # 2) Tendência PRÉ-confirmada (5m): 9/20/50 cruzam acima da 200
        if mon.stage5m(symbol) <= 1:
            prev_below = (ema9[ip] <= ma200[ip]) or (ma20[ip] <= ma200[ip]) or (ma50[ip] <= ma200[ip])
            now_above  = (ema9[i]  >  ma200[i]) and (ma20[i]  >  ma200[i]) and (ma50[i]  >  ma200[i])
            if prev_below and now_above:
                if mon.allowed(symbol, "PRECONFIRM_5M"):
                    msg = build_msg(
                        symbol,
                        "TENDÊNCIA PRÉ-CONFIRMADA (5m)",
                        c[i],
                        [f"Médias 9/20/50 cruzaram acima da MA200", f"RSI {rsi14[i]:.1f}"]
                    )
                    await send_alert(session, msg)
                    mon.mark(symbol, "PRECONFIRM_5M")
                    mon.set_stage5m(symbol, 2)
        # 5m silencia após pré-confirmação (sem reteste/rompimento)
    except Exception as e:
        print("worker_5m error", symbol, e)

async def worker_15m(session, symbol, mon: Monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_15M, limit=200, include_partial=True)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, *_ = compute_indicators(o,h,l,c,v)
        i = len(c)-1; ip = i-1

        # Pré-confirmação (15m): EMA9 cruza MA200
        if crossed_up(ema9[ip], ema9[i], ma200[ip], ma200[i]):
            if mon.allowed(symbol, "PRECONFIRM_15M"):
                msg = build_msg(
                    symbol,
                    "TENDÊNCIA PRÉ-CONFIRMADA (15m)",
                    c[i],
                    [f"EMA9 cruzou MA200", f"RSI {rsi14[i]:.1f} | ADX {adx14[i]:.1f}"]
                )
                await send_alert(session, msg)
                mon.mark(symbol, "PRECONFIRM_15M")

        # Confirmação (15m): 9>20>50>200 + RSI>55 + ADX>25
        prev_ok = not (ema9[ip] > ma20[ip] > ma50[ip] > ma200[ip] and rsi14[ip] > 55 and adx14[ip] > 25)
        now_ok  =      (ema9[i]  > ma20[i]  > ma50[i]  > ma200[i]  and rsi14[i]  > 55 and adx14[i]  > 25)
        if prev_ok and now_ok and mon.allowed(symbol, "CONFIRM_15M"):
            msg = build_msg(
                symbol,
                "TENDÊNCIA CONFIRMADA (15m)",
                c[i],
                [f"EMA9>MA20>MA50>MA200", f"RSI {rsi14[i]:.1f} | ADX {adx14[i]:.1f}"]
            )
            await send_alert(session, msg)
            mon.mark(symbol, "CONFIRM_15M")

        # Reteste (15m): toque e reação (somente com tendência ativa)
        touched_ema9 = (l[i] <= ema9[i] and c[i] >= ema9[i])
        touched_ma20 = (l[i] <= ma20[i] and c[i] >= ma20[i])
        if (ema9[i] > ma20[i] > ma50[i] > ma200[i]):
            if touched_ema9 and mon.allowed(symbol, "RETESTE_EMA9_15M"):
                msg = build_msg(symbol, "RETESTE EMA9 (15m)", c[i],
                                [f"Toque na EMA9 + reação", f"RSI {rsi14[i]:.1f} | Vol {'ok' if v[i]>=volma[i] else 'baixo'}"])
                await send_alert(session, msg)
                mon.mark(symbol, "RETESTE_EMA9_15M")
            if touched_ma20 and mon.allowed(symbol, "RETESTE_MA20_15M"):
                msg = build_msg(symbol, "RETESTE MA20 (15m)", c[i],
                                [f"Toque na MA20 + reação", f"RSI {rsi14[i]:.1f} | Vol {'ok' if v[i]>=volma[i] else 'baixo'}"])
                await send_alert(session, msg)
                mon.mark(symbol, "RETESTE_MA20_15M")

        # Rompimento (15m): fechamento acima da máxima Donchian 20
        if i >= DONCHIAN_N:
            don_high = max(h[i-DONCHIAN_N:i])
            if c[i] > don_high and mon.allowed(symbol, "BREAKOUT_15M"):
                msg = build_msg(symbol, "ROMPIMENTO DA RESISTÊNCIA (15m)", c[i],
                                [f"Fechou acima da máxima {DONCHIAN_N}", "Força compradora ativa"])
                await send_alert(session, msg)
                mon.mark(symbol, "BREAKOUT_15M")

        # Entrada Segura (15m): toque EMA9/MA20 + RSI 45–55 + Vol > média
        def is_entry_safe(idx):
            touched = (l[idx]<=ema9[idx] and c[idx]>=ema9[idx]) or (l[idx]<=ma20[idx] and c[idx]>=ma20[idx])
            return touched and (45.0 <= rsi14[idx] <= 55.0) and (v[idx] >= volma[idx]*1.05)
        if is_entry_safe(i) and mon.allowed(symbol, "ENTRYSAFE_15M"):
            msg = build_msg(symbol, "ENTRADA SEGURA — RETESTE (15m)", c[i],
                            [f"Toque EMA9/MA20 + RSI {rsi14[i]:.1f}", "Volume acima da média"])
            await send_alert(session, msg)
            mon.mark(symbol, "ENTRYSAFE_15M")
    except Exception as e:
        print("worker_15m error", symbol, e)

async def worker_1h(session, symbol, mon: Monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_1H, limit=200, include_partial=True)
        if len(c) < 120: return
        ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, *_ = compute_indicators(o,h,l,c,v)
        i = len(c)-1; ip = i-1

        # Pré-confirmação (1h): EMA9 cruza MA20 + RSI 50–60 + vol > média
        if crossed_up(ema9[ip], ema9[i], ma20[ip], ma20[i]) and 50.0 <= rsi14[i] <= 60.0 and v[i] >= volma[i]*1.05:
            if mon.allowed(symbol, "PRECONFIRM_1H"):
                msg = build_msg(symbol, "PRÉ-CONFIRMAÇÃO LONGA (1h)", c[i],
                                [f"EMA9 cruzou MA20", f"RSI {rsi14[i]:.1f} | Vol acima da média"], bold=True)
                await send_alert(session, msg)
                mon.mark(symbol, "PRECONFIRM_1H")

        # Confirmação (1h): 9>20>50 + RSI>55 + ADX>25 (transição)
        prev_ok = not (ema9[ip] > ma20[ip] > ma50[ip] and rsi14[ip] > 55 and adx14[ip] > 25)
        now_ok  =      (ema9[i]  > ma20[i]  > ma50[i]  and rsi14[i]  > 55 and adx14[i]  > 25)
        if prev_ok and now_ok and mon.allowed(symbol, "CONFIRM_1H"):
            msg = build_msg(symbol, "TENDÊNCIA LONGA CONFIRMADA (1h)", c[i],
                            [f"EMA9>MA20>MA50", f"RSI {rsi14[i]:.1f} | ADX {adx14[i]:.1f}"], bold=True)
            await send_alert(session, msg)
            mon.mark(symbol, "CONFIRM_1H")

        # Reteste (1h)
        touched_ema9 = (l[i] <= ema9[i] and c[i] >= ema9[i])
        touched_ma20 = (l[i] <= ma20[i] and c[i] >= ma20[i])
        if (ema9[i] > ma20[i] > ma50[i]):
            if touched_ema9 and mon.allowed(symbol, "RETESTE_1H"):
                msg = build_msg(symbol, "RETESTE (1h)", c[i],
                                [f"Toque EMA9 + reação", f"RSI {rsi14[i]:.1f} | Vol {'ok' if v[i]>=volma[i] else 'baixo'}"], bold=True)
                await send_alert(session, msg)
                mon.mark(symbol, "RETESTE_1H")
            if touched_ma20 and mon.allowed(symbol, "RETESTE_1H"):
                msg = build_msg(symbol, "RETESTE (1h)", c[i],
                                [f"Toque MA20 + reação", f"RSI {rsi14[i]:.1f} | Vol {'ok' if v[i]>=volma[i] else 'baixo'}"], bold=True)
                await send_alert(session, msg)
                mon.mark(symbol, "RETESTE_1H")

        # Rompimento (1h)
        if i >= DONCHIAN_N:
            don_high = max(h[i-DONCHIAN_N:i])
            if c[i] > don_high and mon.allowed(symbol, "BREAKOUT_1H"):
                msg = build_msg(symbol, "ROMPIMENTO (1h)", c[i],
                                [f"Fechou acima da máxima {DONCHIAN_N}", "Força de continuidade"], bold=True)
                await send_alert(session, msg)
                mon.mark(symbol, "BREAKOUT_1H")

        # Entrada Segura (1h)
        def is_entry_safe(idx):
            touched = (l[idx]<=ema9[idx] and c[idx]>=ema9[idx]) or (l[idx]<=ma20[idx] and c[idx]>=ma20[idx])
            return touched and (45.0 <= rsi14[idx] <= 55.0) and (v[idx] >= volma[idx]*1.05)
        if is_entry_safe(i) and mon.allowed(symbol, "ENTRYSAFE_1H"):
            msg = build_msg(symbol, "ENTRADA SEGURA — RETESTE (1h)", c[i],
                            [f"Toque EMA9/MA20 + RSI {rsi14[i]:.1f}", "Volume acima da média"], bold=True)
            await send_alert(session, msg)
            mon.mark(symbol, "ENTRYSAFE_1H")
    except Exception as e:
        print("worker_1h error", symbol, e)

async def worker_4h(session, symbol, mon: Monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_4H, limit=200, include_partial=True)
        if len(c) < 120: return
        ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, *_ = compute_indicators(o,h,l,c,v)
        i = len(c)-1; ip = i-1

        # Pré-confirmação (4h): EMA9 cruza MA20 + RSI>50
        if crossed_up(ema9[ip], ema9[i], ma20[ip], ma20[i]) and rsi14[i] > 50.0:
            if mon.allowed(symbol, "PRECONFIRM_4H"):
                msg = build_msg(symbol, "PRÉ-CONFIRMAÇÃO LONGA (4h)", c[i],
                                [f"EMA9 cruzou MA20", f"RSI {rsi14[i]:.1f}"], bold=True)
                await send_alert(session, msg)
                mon.mark(symbol, "PRECONFIRM_4H")

        # Confirmação (4h): 9>20>50 por 2 velas + RSI>55
        if (i >= 1 and
            ema9[i] > ma20[i] > ma50[i] and ema9[ip] > ma20[ip] > ma50[ip] and
            rsi14[i] > 55.0 and mon.allowed(symbol, "CONFIRM_4H")):
            msg = build_msg(symbol, "TENDÊNCIA 4H CONFIRMADA", c[i],
                            [f"Estrutura mantida por 2 velas", f"RSI {rsi14[i]:.1f}"], bold=True)
            await send_alert(session, msg)
            mon.mark(symbol, "CONFIRM_4H")

        # Reteste (4h)
        touched_ema9 = (l[i] <= ema9[i] and c[i] >= ema9[i])
        touched_ma20 = (l[i] <= ma20[i] and c[i] >= ma20[i])
        if (ema9[i] > ma20[i] > ma50[i]):
            if touched_ema9 and mon.allowed(symbol, "RETESTE_4H"):
                msg = build_msg(symbol, "RETESTE (4h)", c[i],
                                [f"Toque EMA9 + reação", f"RSI {rsi14[i]:.1f} | Vol {'ok' if v[i]>=volma[i] else 'baixo'}"], bold=True)
                await send_alert(session, msg)
                mon.mark(symbol, "RETESTE_4H")
            if touched_ma20 and mon.allowed(symbol, "RETESTE_4H"):
                msg = build_msg(symbol, "RETESTE (4h)", c[i],
                                [f"Toque MA20 + reação", f"RSI {rsi14[i]:.1f} | Vol {'ok' if v[i]>=volma[i] else 'baixo'}"], bold=True)
                await send_alert(session, msg)
                mon.mark(symbol, "RETESTE_4H")

        # Rompimento (4h)
        if i >= DONCHIAN_N:
            don_high = max(h[i-DONCHIAN_N:i])
            if c[i] > don_high and mon.allowed(symbol, "BREAKOUT_4H"):
                msg = build_msg(symbol, "ROMPIMENTO (4h)", c[i],
                                [f"Fechou acima da máxima {DONCHIAN_N}", "Força institucional"], bold=True)
                await send_alert(session, msg)
                mon.mark(symbol, "BREAKOUT_4H")
    except Exception as e:
        print("worker_4h error", symbol, e)

# ----------------- Main -----------------
async def main():
    mon = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        hello = f"💻 v3.1 | 5m(início/pré) • 15m/1h/4h (pré/confirm + reteste + rompimento) • Entrada Segura(15m/1h) | {len(watchlist)} pares SPOT | {ts_brazil_now()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks = []
            for s in watchlist:
                tasks += [
                    worker_5m(session, s, mon),
                    worker_15m(session, s, mon),
                    worker_1h(session, s, mon),
                    worker_4h(session, s, mon)
                ]
            await asyncio.gather(*tasks, return_exceptions=True)

            await asyncio.sleep(SCAN_INTERVAL_SEC)

            # Atualiza shortlist periodicamente
            try:
                tickers = await get_24h(session)
                watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
            except Exception as e:
                print("Erro ao atualizar shortlist:", e)

# ----------------- Flask / Runner -----------------
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
        return "✅ Binance Alerts Bot v3.1 — 5m início/pré • 15m/1h/4h confirmações + retestes/rompimentos • Entrada Segura (15m/1h) 🇧🇷"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
