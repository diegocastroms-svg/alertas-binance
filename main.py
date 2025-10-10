# main_v3_3_final.py
# ✅ Curto (5m/15m) do jeito combinado + Longo (1h/4h) preservado
# - 5m: Iniciando (EMA9 cruza MA20 após queda/lateral) → Pré-conf. (9/20/50 > 200) → silencia
# - 15m: Pré-conf. (EMA9 cruza MA200), Confirmada (9>20>50>200 + RSI>55 + ADX>25), Retestes, Rompimento, Perdendo força, Saída
# - 1h/4h: pré e confirmadas em 2 velas, Entrada segura, Combinada (mantidos)
# - Somente SPOT, cooldown curtos=15min, longos=1h, Flask ativo.

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_5M, INTERVAL_15M, INTERVAL_1H, INTERVAL_4H = "5m","15m","1h","4h"
SHORTLIST_N           = 65
SCAN_INTERVAL_SECONDS = 15          # frequência de varredura
COOLDOWN_SHORT_SEC    = 15 * 60     # 15 min (curto)
COOLDOWN_LONG_SEC     = 60 * 60     # 1 h (longo)
MIN_PCT, MIN_QV       = 1.0, 300_000.0

EMA_FAST, MA_SLOW, MA_MED, MA_LONG = 9, 20, 50, 200
RSI_LEN, VOL_MA, BB_LEN, ADX_LEN   = 14, 9, 20, 14
DONCHIAN_N = 20

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# --------------- Utils / Alert ---------------
def fmt_symbol(symbol: str) -> str:
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol: str) -> str:
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now():
    # horário de Brasília (UTC-3), sem escrever "Brasília"
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session: aiohttp.ClientSession, text: str):
    # (1) webhook (opcional)
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=8)
        except:
            pass
    # (2) Telegram
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=8)
        except:
            pass

# --------------- Indicadores (sem pandas) ---------------
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
    e = seq[0]
    out.append(e)
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
    # (BBs podem ser usados em “mercado esticado” se você quiser reativar futuramente)
    adx14, pdi, mdi = adx(h, l, c, ADX_LEN)
    return ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi

# --------------- Binance ---------------
async def get_klines(session, symbol: str, interval="5m", limit=210):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    # Mantemos o último candle (parcial) para cruzamento intrabar
    o,h,l,c,v=[],[],[],[],[]
    for k in data:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=400):
    # Somente SPOT “padrão” (exclui UP/DOWN/BULL/BEAR/PERP e pares não-USDT normais)
    usdt = []
    blocked = ("UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD",
               "_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_BTC",
               "_EUR","_TRY","_BRL","_ETH","_BNB","_SOL")
    for t in tickers:
        s = t.get("symbol","")
        if not s.endswith("USDT"): 
            continue
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

# --------------- Helpers de Lógica ---------------
def crossed_up(a_prev, a_now, b_prev, b_now):
    # cruza de baixo pra cima
    return a_prev <= b_prev and a_now > b_now

def is_lateralizing(closes, ma20, win=10, band=0.01):
    # lateral se oscila +-1% ao redor da média nos últimos N candles
    if len(closes) < win+1: return False
    seg = closes[-win:]
    m = sum(seg)/len(seg)
    return max(abs(x-m)/m for x in seg) < band

def recent_drawdown(closes, lookback=60, th=-0.08):
    # drawdown relativo do fechamento atual vs max dos últimos N (ex.: -8% ou pior)
    if len(closes) < lookback+1: return True  # permita se não há histórico
    mx = max(closes[-lookback:])
    dd = (closes[-1]/(mx+1e-12))-1.0
    return dd <= th

# --------------- Emojis / Formatação ---------------
def trend_ball(kind):
    # 🟢 alta / 🔴 queda forte / 🟡 lateral
    if kind in ("INICIO_5M","PRECONF_5M","PRECONF_15M","CONFIRM_15M",
                "PRECONF_1H","CONFIRM_1H","PRECONF_4H","CONFIRM_4H",
                "RET_EMA9_15","RET_MA20_15","BREAK_15",
                "ENTRY_SAFE","COMBINED_TREND"):
        return "🟢"
    if kind in ("PERDENDO_FORCA_15","SAIDA_15","PERDENDO_FORCA_1H","SAIDA_1H","PERDENDO_FORCA_4H","SAIDA_4H"):
        return "🟠" if "PERDENDO" in kind else "❌"
    # fallback (lateral/monitorando)
    return "🟡"

def arrow_for(kind):
    if kind in ("INICIO_5M","PRECONF_5M","PRECONF_15M","CONFIRM_15M",
                "PRECONF_1H","CONFIRM_1H","PRECONF_4H","CONFIRM_4H",
                "RET_EMA9_15","RET_MA20_15","BREAK_15",
                "ENTRY_SAFE","COMBINED_TREND"):
        return "⬆️"
    if "SAIDA" in kind:
        return "⬇️"
    return "↔️"

def title_text(kind):
    titles = {
        "INICIO_5M":      "TENDÊNCIA INICIANDO (5m)",
        "PRECONF_5M":     "TENDÊNCIA PRÉ-CONFIRMADA (5m)",
        "PRECONF_15M":    "TENDÊNCIA PRÉ-CONFIRMADA (15m)",
        "CONFIRM_15M":    "TENDÊNCIA CONFIRMADA (15m)",
        "RET_EMA9_15":    "RETESTE EMA9 (15m) — Continuação de alta",
        "RET_MA20_15":    "RETESTE MA20 (15m) — Continuação de alta",
        "BREAK_15":       "ROMPIMENTO DA RESISTÊNCIA (15m)",
        "PERDENDO_FORCA_15":"PERDENDO FORÇA (15m)",
        "SAIDA_15":       "SAÍDA (15m)",
        # longos (negrito)
        "PRECONF_1H":     "PRÉ-CONFIRMAÇÃO LONGA (1h)",
        "CONFIRM_1H":     "TENDÊNCIA LONGA CONFIRMADA (1h)",
        "PRECONF_4H":     "PRÉ-CONFIRMAÇÃO LONGA (4h)",
        "CONFIRM_4H":     "TENDÊNCIA 4H CONFIRMADA",
        "ENTRY_SAFE":     "ENTRADA SEGURA — Reteste (15m/1h)",
        "COMBINED_TREND": "TENDÊNCIA LONGA COMBINADA (15m+1h+4h)",
    }
    return titles.get(kind, kind.replace("_"," "))

def build_msg(symbol, kind, price, bullets, bold=False):
    # Linha 1: bola + par (destaque)
    sym = fmt_symbol(symbol)
    ball = trend_ball(kind)
    title = title_text(kind)
    arr = arrow_for(kind)
    head = f"{ball} {sym}"  # “par no topo”
    if bold:
        title_line = f"<b>{arr} {title}</b>"
    else:
        title_line = f"{arr} {title}"
    return (
        f"{head}\n"
        f"{title_line}\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"🧠 {bullets}\n"
        f"⏰ {ts_brazil_now()}\n"
        f"{binance_links(symbol)}"
    )

# --------------- Anti-spam / Estado curto ---------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)      # curto (5m/15m) por (symbol, kind)
        self.cooldown_long = defaultdict(lambda: 0.0) # longo (1h/4h) por symbol
        self.stage5m = defaultdict(lambda: 0)         # 0: livre; 1: iniciou 5m; 2: preconf 5m -> silenciar
    def allowed(self, symbol, kind):
        return time.time() - self.cooldown[(symbol, kind)] >= COOLDOWN_SHORT_SEC
    def mark(self, symbol, kind):
        self.cooldown[(symbol, kind)] = time.time()
    def allowed_long(self, symbol):
        return time.time() - self.cooldown_long[symbol] >= COOLDOWN_LONG_SEC
    def mark_long(self, symbol):
        self.cooldown_long[symbol] = time.time()
    def get_stage5m(self, symbol):
        return self.stage5m[symbol]
    def set_stage5m(self, symbol, val):
        self.stage5m[symbol] = val
    def reset_5m_if_lateral(self, symbol, closes, ma20):
        if is_lateralizing(closes, ma20):
            self.stage5m[symbol] = 0

# --------------- Workers CURTOS ---------------
async def worker_5m(session, symbol, mon: Monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_5M, limit=210)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        i = len(c)-1; ip = i-1

        # Reseta estágio se lateralizar (permite novo ciclo depois de rallies)
        mon.reset_5m_if_lateral(symbol, c, ma20)

        # (a) Tendência iniciando (5m): EMA9 cruza MA20 após queda+lateral
        if mon.get_stage5m(symbol) == 0:
            if recent_drawdown(c, lookback=60, th=-0.08) and is_lateralizing(c, ma20, win=10, band=0.01):
                if crossed_up(ema9[ip], ema9[i], ma20[ip], ma20[i]) and mon.allowed(symbol, "INICIO_5M"):
                    txt = build_msg(symbol, "INICIO_5M", c[i],
                        f"EMA9 cruzou MA20 após queda e lateralização | RSI {rsi14[i]:.1f}")
                    await send_alert(session, txt)
                    mon.mark(symbol, "INICIO_5M")
                    mon.set_stage5m(symbol, 1)

        # (b) Tendência pré-confirmada (5m): 9,20,50 > 200 (cruzaram a MA200)
        if mon.get_stage5m(symbol) <= 1:
            prev_under = (ema9[ip] <= ma200[ip]) or (ma20[ip] <= ma200[ip]) or (ma50[ip] <= ma200[ip])
            now_above  = (ema9[i]  >  ma200[i]) and (ma20[i] >  ma200[i]) and (ma50[i] >  ma200[i])
            if prev_under and now_above and mon.allowed(symbol, "PRECONF_5M"):
                txt = build_msg(symbol, "PRECONF_5M", c[i], "Médias 9/20/50 cruzaram acima da MA200 (5m)")
                await send_alert(session, txt)
                mon.mark(symbol, "PRECONF_5M")
                mon.set_stage5m(symbol, 2)  # silenciar 5m após aqui

    except Exception as e:
        print("worker_5m error", symbol, e)

async def worker_15m(session, symbol, mon: Monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_15M, limit=210)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        i = len(c)-1; ip = i-1

        # 🌕 Pré-confirmada (15m): EMA9 cruza MA200
        if crossed_up(ema9[ip], ema9[i], ma200[ip], ma200[i]) and mon.allowed(symbol, "PRECONF_15M"):
            txt = build_msg(symbol, "PRECONF_15M", c[i],
                f"EMA9 cruzou MA200 (15m) | RSI {rsi14[i]:.1f} | ADX {adx14[i]:.1f}")
            await send_alert(session, txt)
            mon.mark(symbol, "PRECONF_15M")

        # 🚀 Confirmada (15m): 9>20>50>200 + RSI>55 + ADX>25
        prev_ok = not (ema9[ip] > ma20[ip] > ma50[ip] > ma200[ip] and rsi14[ip] > 55.0 and adx14[ip] > 25.0)
        now_ok  =      (ema9[i]  > ma20[i]  > ma50[i]  > ma200[i]  and rsi14[i]  > 55.0 and adx14[i]  > 25.0)
        if prev_ok and now_ok and mon.allowed(symbol, "CONFIRM_15M"):
            txt = build_msg(symbol, "CONFIRM_15M", c[i],
                f"EMA9>MA20>MA50>MA200 | RSI {rsi14[i]:.1f} | ADX {adx14[i]:.1f}")
            await send_alert(session, txt)
            mon.mark(symbol, "CONFIRM_15M")

        # ♻️ Reteste EMA9 (15m): Toque + reação (apenas após estar acima das médias)
        if ema9[i] > ma20[i] > ma50[i] > ma200[i]:
            touched9 = (l[i] <= ema9[i] and c[i] >= ema9[i])
            if touched9 and rsi14[i] >= 52.0 and v[i] >= volma[i]*0.95 and mon.allowed(symbol, "RET_EMA9_15"):
                txt = build_msg(symbol, "RET_EMA9_15", c[i],
                    f"Toque na EMA9 e reação | RSI {rsi14[i]:.1f} | Vol ok")
                await send_alert(session, txt)
                mon.mark(symbol, "RET_EMA9_15")

            touched20 = (l[i] <= ma20[i] and c[i] >= ma20[i])
            if touched20 and rsi14[i] >= 50.0 and v[i] >= volma[i]*0.95 and mon.allowed(symbol, "RET_MA20_15"):
                txt = build_msg(symbol, "RET_MA20_15", c[i],
                    f"Toque na MA20 e reação | RSI {rsi14[i]:.1f} | Vol ok")
                await send_alert(session, txt)
                mon.mark(symbol, "RET_MA20_15")

        # 📈 Rompimento de resistência (15m): fechamento acima da máxima 20
        if i >= DONCHIAN_N:
            don_high = max(h[i-DONCHIAN_N+1:i+1])
            if c[i] > don_high and mon.allowed(symbol, "BREAK_15"):
                txt = build_msg(symbol, "BREAK_15", c[i],
                    f"Fechou acima da máxima {DONCHIAN_N} velas ({don_high:.6f}) — Rompimento confirmado")
                await send_alert(session, txt)
                mon.mark(symbol, "BREAK_15")

        # 🟠 Perdendo força (15m): RSI caindo + ADX caindo
        if i >= 2:
            if (rsi14[i] < rsi14[i-1] < rsi14[i-2]) and (adx14[i] < adx14[i-1]):
                if mon.allowed(symbol, "PERDENDO_FORCA_15"):
                    txt = build_msg(symbol, "PERDENDO_FORCA_15", c[i],
                        f"RSI enfraquecendo | ADX caindo | RSI {rsi14[i]:.1f} | ADX {adx14[i]:.1f}")
                    await send_alert(session, txt)
                    mon.mark(symbol, "PERDENDO_FORCA_15")

        # ❌ Saída (15m): EMA9 cruza MA20 para baixo ou RSI<45
        down_cross = (ema9[ip] >= ma20[ip] and ema9[i] < ma20[i])
        if (down_cross or rsi14[i] < 45.0) and mon.allowed(symbol, "SAIDA_15"):
            reason = "EMA9 cruzou MA20 para baixo" if down_cross else "RSI < 45"
            txt = build_msg(symbol, "SAIDA_15", c[i], reason)
            await send_alert(session, txt)
            mon.mark(symbol, "SAIDA_15")

    except Exception as e:
        print("worker_15m error", symbol, e)

# --------------- Workers LONGOS (mantidos) ---------------
async def worker_1h(session, symbol, mon: Monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_1H, limit=210)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        i = len(c)-1; ip = i-1

        # 🌕 Pré-confirmação longa (1h): EMA9>MA20 + RSI 50–60 + volume acima da média (1ª vela)
        if crossed_up(ema9[ip], ema9[i], ma20[ip], ma20[i]) and 50.0 <= rsi14[i] <= 60.0 and v[i] >= volma[i]*1.05:
            if mon.allowed_long(symbol):
                txt = build_msg(symbol, "PRECONF_1H", c[i],
                    "EMA9 cruzou MA20 (1h) + RSI 50–60 + Volume > média", bold=True)
                await send_alert(session, txt)
                mon.mark_long(symbol)
                return

        # 🚀 Tendência longa confirmada (1h): 9>20>50 + RSI>55 + ADX>25 (2ª vela)
        prev_ok = not (ema9[ip] > ma20[ip] > ma50[ip] and rsi14[ip] > 55.0 and adx14[ip] > 25.0)
        now_ok  =      (ema9[i]  > ma20[i]  > ma50[i]  and rsi14[i]  > 55.0 and adx14[i]  > 25.0)
        if prev_ok and now_ok and mon.allowed_long(symbol):
            txt = build_msg(symbol, "CONFIRM_1H", c[i],
                "EMA9>MA20>MA50 + RSI>55 + ADX>25 (1h)", bold=True)
            await send_alert(session, txt)
            mon.mark_long(symbol)
            return

    except Exception as e:
        print("worker_1h error", symbol, e)

async def worker_4h(session, symbol, mon: Monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_4H, limit=210)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        i = len(c)-1; ip = i-1

        # 🌕 Pré-confirmação (4h): EMA9>MA20 + RSI>50 (1ª vela)
        if crossed_up(ema9[ip], ema9[i], ma20[ip], ma20[i]) and rsi14[i] > 50.0:
            if mon.allowed_long(symbol):
                txt = build_msg(symbol, "PRECONF_4H", c[i], "EMA9 cruzou MA20 (4h) + RSI>50", bold=True)
                await send_alert(session, txt)
                mon.mark_long(symbol)
                return

        # 🚀 Tendência 4H confirmada: 9>20>50 por 2 velas + RSI>55 (2ª vela)
        if (ema9[i] > ma20[i] > ma50[i]) and (ema9[ip] > ma20[ip] > ma50[ip]) and rsi14[i] > 55.0:
            if mon.allowed_long(symbol):
                txt = build_msg(symbol, "CONFIRM_4H", c[i], "Estrutura 9>20>50 mantida por 2 velas + RSI>55", bold=True)
                await send_alert(session, txt)
                mon.mark_long(symbol)
                return

    except Exception as e:
        print("worker_4h error", symbol, e)

# 💚 Entrada segura — Reteste (15m/1h)
async def worker_entry_safe(session, symbol, mon: Monitor):
    try:
        # 15m
        o15,h15,l15,c15,v15 = await get_klines(session, symbol, interval=INTERVAL_15M, limit=120)
        ema9_15, ma20_15, ma50_15, ma200_15, rsi15, volma15, adx15, _, _ = compute_indicators(o15,h15,l15,c15,v15)
        i15 = len(c15)-1

        # 1h
        o1,h1,l1,c1,v1 = await get_klines(session, symbol, interval=INTERVAL_1H, limit=120)
        ema9_1, ma20_1, ma50_1, ma200_1, rsi1, volma1, adx1, _, _ = compute_indicators(o1,h1,l1,c1,v1)
        i1 = len(c1)-1

        def ok_reteste(low, close, ema, ma, rsi, vol, volma, idx):
            touched = (low[idx] <= ema[idx] and close[idx] >= ema[idx]) or \
                      (low[idx] <= ma[idx]  and close[idx] >= ma[idx])
            return touched and (45.0 <= rsi[idx] <= 55.0) and (vol[idx] >= volma[idx]*1.05)

        # 15m prioridade, depois 1h
        if ok_reteste(l15, c15, ema9_15, ma20_15, rsi15, v15, volma15, i15) and mon.allowed_long(symbol):
            txt = build_msg(symbol, "ENTRY_SAFE", c15[i15], "Entrada segura (15m): Toque EMA9/MA20 + RSI 45–55 + Vol > média", bold=True)
            await send_alert(session, txt); mon.mark_long(symbol); return

        if ok_reteste(l1, c1, ema9_1, ma20_1, rsi1, v1, volma1, i1) and mon.allowed_long(symbol):
            txt = build_msg(symbol, "ENTRY_SAFE", c1[i1], "Entrada segura (1h): Toque EMA9/MA20 + RSI 45–55 + Vol > média", bold=True)
            await send_alert(session, txt); mon.mark_long(symbol); return

    except Exception as e:
        print("worker_entry_safe error", symbol, e)

# 🌕 Tendência longa combinada (15m+1h+4h)
async def worker_combined(session, symbol, mon: Monitor):
    try:
        # 15m
        o15,h15,l15,c15,v15 = await get_klines(session, symbol, interval=INTERVAL_15M, limit=120)
        e9_15,m20_15,m50_15,m200_15,r15,vm15,ad15,_,_ = compute_indicators(o15,h15,l15,c15,v15)
        i15=len(c15)-1
        # 1h
        o1,h1,l1,c1,v1 = await get_klines(session, symbol, interval=INTERVAL_1H, limit=120)
        e9_1,m20_1,m50_1,m200_1,r1,vm1,ad1,_,_ = compute_indicators(o1,h1,l1,c1,v1)
        i1=len(c1)-1
        # 4h
        o4,h4,l4,c4,v4 = await get_klines(session, symbol, interval=INTERVAL_4H, limit=120)
        e9_4,m20_4,m50_4,m200_4,r4,vm4,ad4,_,_ = compute_indicators(o4,h4,l4,c4,v4)
        i4=len(c4)-1

        ok15 = (e9_15[i15]>m20_15[i15]>m50_15[i15]>m200_15[i15] and r15[i15]>55 and ad15[i15]>25)
        ok1  = (e9_1[i1]  >m20_1[i1]  >m50_1[i1]  >m200_1[i1]  and r1[i1] >55 and ad1[i1] >25)
        ok4  = (e9_4[i4]  >m20_4[i4]  >m50_4[i4]  >m200_4[i4]  and r4[i4] >55 and ad4[i4] >25)

        if ok15 and ok1 and ok4 and mon.allowed_long(symbol):
            txt = build_msg(symbol, "COMBINED_TREND", c15[i15],
                "EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25 (15m,1h,4h)", bold=True)
            await send_alert(session, txt)
            mon.mark_long(symbol)

    except Exception as e:
        print("worker_combined error", symbol, e)

# --------------- Main -----------------
async def main():
    mon = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        hello = f"💻 v3.3 FINAL | {len(watchlist)} pares SPOT | {ts_brazil_now()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks = []
            for s in watchlist:
                # Curtos
                tasks.append(worker_5m(session, s, mon))
                tasks.append(worker_15m(session, s, mon))
                # Longos
                tasks.append(worker_1h(session, s, mon))
                tasks.append(worker_4h(session, s, mon))
                tasks.append(worker_entry_safe(session, s, mon))
                tasks.append(worker_combined(session, s, mon))
            await asyncio.gather(*tasks, return_exceptions=True)

            # espera entre ciclos
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

            # atualiza shortlist periodicamente
            try:
                tickers = await get_24h(session)
                watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
            except Exception as e:
                print("Erro ao atualizar shortlist:", e)

# --------------- Flask (Render) -----------------
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
        return "✅ Binance Alerts Bot v3.3 FINAL — Curtos (5m/15m) + Longos (1h/4h) prontos 🇧🇷"

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
