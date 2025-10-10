# main_v3_0.py
# Bot de alertas — versão 3.0 (do zero, conforme especificação do Diego)
# - Curto prazo (5m/15m): monitor de queda+lateralização, início, pré/confirmada, rompimento, retestes, perdendo força e saída
# - Longo prazo (1h/4h): pré/confirmadas, entrada segura, combinada, perdendo força e saída
# - Cooldown: 15 min (curtos) | 60 min (longos)
# - SPOT-only, Flask ativo, execução assíncrona com ciclo de 60s

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

import aiohttp
from flask import Flask

# =========================
# CONFIG
# =========================
BINANCE_HTTP = "https://api.binance.com"

TF_5M  = "5m"
TF_15M = "15m"
TF_1H  = "1h"
TF_4H  = "4h"

SHORTLIST_N = 80
MIN_PCT_24H = 1.0
MIN_QV_24H  = 300_000.0

# Cooldowns
CD_SHORT = 15 * 60   # 15 minutos (5m/15m)
CD_LONG  = 60 * 60   # 1 hora (1h/4h)

# Indicadores
EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
ADX_LEN  = 14
VOL_MA   = 9
BB_LEN   = 20
DON_N    = 20

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()

# =========================
# UTILS
# =========================
def ts_br():
    # Horário de Brasília (UTC-3) + bandeira
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

def fmt_symbol(symbol):
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def links(symbol):
    base = symbol.replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

async def send_alert(session, html):
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            await session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": html, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10
            )
        except:
            pass

# =========================
# INDICADORES (sem pandas)
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
    out = []
    a = 2.0 / (span + 1.0)
    e = seq[0]
    out.append(e)
    for x in seq[1:]:
        e = a * x + (1 - a) * e
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

def rsi_wilder(c, period=14):
    if len(c) < period + 1: return [50.0]*len(c)
    deltas = [0.0] + [c[i]-c[i-1] for i in range(1, len(c))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    rsis = [50.0]*len(c)
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    for i in range(period+1, len(c)):
        avg_gain = (avg_gain*(period-1) + gains[i]) / period
        avg_loss = (avg_loss*(period-1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsis[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsis

def true_range(h, l, c):
    tr = [0.0]
    for i in range(1, len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return tr

def adx(h, l, c, period=14):
    n = len(c)
    if n < period + 1: return [20.0]*n, [0.0]*n, [0.0]*n
    tr = true_range(h, l, c)
    plus_dm  = [0.0]
    minus_dm = [0.0]
    for i in range(1, n):
        up   = h[i] - h[i-1]
        down = l[i-1] - l[i]
        plus_dm.append(up   if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    atr = [0.0]*n
    atr[period] = sum(tr[1:period+1])
    pdm = [0.0]*n; mdm = [0.0]*n
    pdm[period] = sum(plus_dm[1:period+1])
    mdm[period] = sum(minus_dm[1:period+1])
    for i in range(period+1, n):
        atr[i] = atr[i-1] - (atr[i-1]/period) + tr[i]
        pdm[i] = pdm[i-1] - (pdm[i-1]/period) + plus_dm[i]
        mdm[i] = mdm[i-1] - (mdm[i-1]/period) + minus_dm[i]
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
        adx_vals[i] = (adx_vals[i-1]*(period-1) + dx[i]) / period
    for i in range(period):
        adx_vals[i] = adx_vals[period]
    return adx_vals, plus_di, minus_di

def compute_indicators(o, h, l, c, v):
    ema9  = ema(c, EMA_FAST)
    ma20  = sma(c, MA_SLOW)
    ma50  = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    adx14, pdi, mdi = adx(h, l, c, ADX_LEN)
    bb_std = rolling_std(c, BB_LEN)
    bb_up  = [ma20[i] + 2*bb_std[i] for i in range(len(c))]
    bb_low = [ma20[i] - 2*bb_std[i] for i in range(len(c))]
    return ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi, bb_up, bb_low

# =========================
# DATA FETCH
# =========================
async def get_klines(session, symbol, interval="5m", limit=200):
    # Usa o candle em formação (não descarta o último) para reduzir atraso
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    o, h, l, c, v = [], [], [], [], []
    for k in data:  # inclui último candle (em formação)
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o, h, l, c, v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=80):
    usdt = []
    blocked = (
        "UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD",
        "_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_BTC","_EUR","_TRY","_BRL","_ETH","_BNB","_SOL"
    )
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
            continue
        if abs(pct) >= MIN_PCT_24H and qv >= MIN_QV_24H:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# =========================
# MONITOR / COOLDOWNS
# =========================
class Monitor:
    def __init__(self):
        self.cd_short = defaultdict(lambda: 0.0)  # chave: (symbol, kind)
        self.cd_long  = defaultdict(lambda: 0.0)  # chave: symbol
        # memória leve para "queda + lateralização" por ativo
        self.prewatch = defaultdict(lambda: False)

    def allowed_short(self, symbol, kind):
        return time.time() - self.cd_short[(symbol, kind)] >= CD_SHORT
    def mark_short(self, symbol, kind):
        self.cd_short[(symbol, kind)] = time.time()

    def allowed_long(self, symbol):
        return time.time() - self.cd_long[symbol] >= CD_LONG
    def mark_long(self, symbol):
        self.cd_long[symbol] = time.time()

monitor = Monitor()

# =========================
# HELPERS
# =========================
def is_lateralizing(h, l, c, i, window=6, max_band=0.01):
    """Variação menor que ~1% nas últimas `window` velas."""
    if i < window-1: return False
    seg_hi = max(h[i-window+1:i+1])
    seg_lo = min(l[i-window+1:i+1])
    rng = seg_hi - seg_lo
    return (rng / max(c[i], 1e-12)) < max_band

def cross_up(a_prev, a_now, b_prev, b_now):
    return a_prev <= b_prev and a_now > b_now

def touched_and_reacted(low, close, ref):
    return low <= ref and close >= ref

# =========================
# WORKERS — CURTO PRAZO (5m/15m)
# =========================
async def worker_5m(session, s):
    try:
        o, h, l, c, v = await get_klines(session, s, TF_5M, 200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi, bb_up, bb_low = compute_indicators(o,h,l,c,v)
        i = len(c)-1
        price = c[i]

        # 🔍 Monitor: Queda + Lateralização (pré-gatilho do início 5m)
        # Queda recente >= 5% (últimas 30 velas) e RSI < 40, depois lateraliza
        if i >= 30:
            drop = (c[i-30] - c[i]) / max(c[i-30],1e-12) * 100.0
            if drop >= 5.0 and rsi14[i] < 40 and is_lateralizing(h,l,c,i,window=6,max_band=0.010):
                monitor.prewatch[s] = True
        # 🔧 Se lateralização “sumir”, podemos limpar depois de 120 velas
        if i % 120 == 0:
            monitor.prewatch[s] = False

        # 🚀 Tendência iniciando (5m): EMA9 cruza MA20 e MA50 após fundo/lateralização
        if monitor.prewatch[s]:
            cond_cross20 = cross_up(ema9[i-1], ema9[i], ma20[i-1], ma20[i])
            cond_above50 = ema9[i] > ma50[i]
            if cond_cross20 and cond_above50 and rsi14[i] >= 45.0 and v[i] >= volma[i] and monitor.allowed_short(s, "START_5M"):
                msg = (
                    f"🟢 <b>{fmt_symbol(s)}</b>\n"
                    f"⬆️ <b>TENDÊNCIA INICIANDO (5m)</b>\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 EMA9 cruzou MA20 e ficou acima de MA50 após queda+lateralização | RSI {rsi14[i]:.1f}\n"
                    f"⏰ {ts_br()}\n{links(s)}"
                )
                await send_alert(session, msg)
                monitor.mark_short(s, "START_5M")
                # Reseta a flag para evitar repetição
                monitor.prewatch[s] = False

        # 🌕 Tendência pré-confirmada (5m): EMA9, MA20 e MA50 cruzam acima da MA200
        if i >= 1 and monitor.allowed_short(s, "PRECONF_5M"):
            cond_prev_below = (ema9[i-1] <= ma200[i-1]) or (ma20[i-1] <= ma200[i-1]) or (ma50[i-1] <= ma200[i-1])
            cond_now_above  = (ema9[i] > ma200[i] and ma20[i] > ma200[i] and ma50[i] > ma200[i])
            if cond_prev_below and cond_now_above and rsi14[i] >= 50.0:
                msg = (
                    f"🟢 <b>{fmt_symbol(s)}</b>\n"
                    f"⬆️ <b>TENDÊNCIA PRÉ-CONFIRMADA (5m)</b>\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 EMA9/MA20/MA50 cruzaram acima da MA200 | RSI {rsi14[i]:.1f}\n"
                    f"⏰ {ts_br()}\n{links(s)}"
                )
                await send_alert(session, msg)
                monitor.mark_short(s, "PRECONF_5M")

    except Exception as e:
        print("worker_5m error", s, e)

async def worker_15m(session, s):
    try:
        o, h, l, c, v = await get_klines(session, s, TF_15M, 200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi, bb_up, bb_low = compute_indicators(o,h,l,c,v)
        i = len(c)-1
        price = c[i]

        # 🌕 Tendência pré-confirmada (15m): EMA9 cruza MA200 para cima
        if i >= 1 and monitor.allowed_short(s, "PRECONF_15M"):
            if cross_up(ema9[i-1], ema9[i], ma200[i-1], ma200[i]) and rsi14[i] >= 50.0 and v[i] >= volma[i]:
                msg = (
                    f"🟢 <b>{fmt_symbol(s)}</b>\n"
                    f"⬆️ <b>TENDÊNCIA PRÉ-CONFIRMADA (15m)</b>\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 EMA9 cruzou acima da MA200 | RSI {rsi14[i]:.1f}\n"
                    f"⏰ {ts_br()}\n{links(s)}"
                )
                await send_alert(session, msg)
                monitor.mark_short(s, "PRECONF_15M")

        # 🚀 Tendência confirmada (15m): EMA9 > MA20 > MA50 > MA200 + RSI>55 + ADX>25
        if monitor.allowed_short(s, "CONF_15M"):
            if (ema9[i] > ma20[i] > ma50[i] > ma200[i]) and (rsi14[i] > 55.0) and (adx14[i] > 25.0):
                msg = (
                    f"🚀 <b>{fmt_symbol(s)}</b>\n"
                    f"💎 <b>TENDÊNCIA CONFIRMADA (15m)</b>\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 EMA9>MA20>MA50>MA200 | RSI {rsi14[i]:.1f} | ADX {adx14[i]:.1f}\n"
                    f"⏰ {ts_br()}\n{links(s)}"
                )
                await send_alert(session, msg)
                monitor.mark_short(s, "CONF_15M")

        # 📈 Rompimento da resistência (15m): Fechamento acima da máxima das últimas 20 velas + (BB alta e Volume)
        if i >= DON_N and monitor.allowed_short(s, "BREAK_15M"):
            don_hi = max(h[i-DON_N+1:i+1])
            cond_break = c[i] > don_hi
            cond_bb    = c[i] >= bb_up[i]  # preço tocou/rompeu banda superior
            cond_vol   = v[i] >= volma[i] * 1.05
            if cond_break and cond_bb and cond_vol:
                msg = (
                    f"📈 <b>{fmt_symbol(s)}</b>\n"
                    f"📈 <b>ROMPIMENTO DA RESISTÊNCIA (15m)</b>\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 Fechou acima da máxima {DON_N} | BB alta | Vol>média\n"
                    f"⏰ {ts_br()}\n{links(s)}"
                )
                await send_alert(session, msg)
                monitor.mark_short(s, "BREAK_15M")

        # ♻️ Reteste EMA9 (15m): toque e reação
        if monitor.allowed_short(s, "RETEST_9_15M"):
            if touched_and_reacted(l[i], c[i], ema9[i]) and rsi14[i] >= 48.0 and v[i] >= 0.9*volma[i]:
                msg = (
                    f"♻️ <b>{fmt_symbol(s)}</b>\n"
                    f"♻️ <b>RETESTE EMA9 (15m)</b>\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 Toque na EMA9 + reação | RSI {rsi14[i]:.1f}\n"
                    f"⏰ {ts_br()}\n{links(s)}"
                )
                await send_alert(session, msg)
                monitor.mark_short(s, "RETEST_9_15M")

        # ♻️ Reteste MA20 (15m): toque e reação
        if monitor.allowed_short(s, "RETEST_20_15M"):
            if touched_and_reacted(l[i], c[i], ma20[i]) and rsi14[i] >= 48.0 and v[i] >= 0.9*volma[i]:
                msg = (
                    f"♻️ <b>{fmt_symbol(s)}</b>\n"
                    f"♻️ <b>RETESTE MA20 (15m)</b>\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 Toque na MA20 + reação | RSI {rsi14[i]:.1f}\n"
                    f"⏰ {ts_br()}\n{links(s)}"
                )
                await send_alert(session, msg)
                monitor.mark_short(s, "RETEST_20_15M")

        # 🟠 Perdendo força (15m): RSI < 50 e ADX < 25
        if monitor.allowed_short(s, "WEAK_15M"):
            if rsi14[i] < 50.0 and adx14[i] < 25.0:
                msg = (
                    f"🟠 <b>{fmt_symbol(s)}</b>\n"
                    f"🟠 <b>PERDENDO FORÇA (15m)</b>\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 RSI {rsi14[i]:.1f} < 50 e ADX {adx14[i]:.1f} < 25\n"
                    f"⏰ {ts_br()}\n{links(s)}"
                )
                await send_alert(session, msg)
                monitor.mark_short(s, "WEAK_15M")

        # ❌ Saída (15m): EMA9 cruza MA20 para baixo + RSI < 45
        if i >= 1 and monitor.allowed_short(s, "EXIT_15M"):
            if cross_up(ma20[i-1], ma20[i], ema9[i-1], ema9[i]) and rsi14[i] < 45.0:
                # (cross_up(ma20, ema9) = ema9 cruzou PARA BAIXO de ma20)
                msg = (
                    f"❌ <b>{fmt_symbol(s)}</b>\n"
                    f"❌ <b>SAÍDA (15m)</b>\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 EMA9 cruzou para baixo da MA20 | RSI {rsi14[i]:.1f}\n"
                    f"⏰ {ts_br()}\n{links(s)}"
                )
                await send_alert(session, msg)
                monitor.mark_short(s, "EXIT_15M")

    except Exception as e:
        print("worker_15m error", s, e)

# =========================
# WORKERS — LONGO PRAZO (1h/4h)
# =========================
async def worker_long(session, s):
    """Processa 1h e 4h + combinada + entrada segura + perdendo força/saída (1h/4h)"""
    try:
        # 1h
        o1, h1, l1, c1, v1 = await get_klines(session, s, TF_1H, 200)
        if len(c1) < 60: return
        ema9_1, ma20_1, ma50_1, ma200_1, rsi1, volma1, adx1, pdi1, mdi1, bb_up1, bb_low1 = compute_indicators(o1,h1,l1,c1,v1)
        j = len(c1)-1; price1 = c1[j]

        # 4h
        o4, h4, l4, c4, v4 = await get_klines(session, s, TF_4H, 200)
        if len(c4) < 60: return
        ema9_4, ma20_4, ma50_4, ma200_4, rsi4, volma4, adx4, pdi4, mdi4, bb_up4, bb_low4 = compute_indicators(o4,h4,l4,c4,v4)
        k = len(c4)-1; price4 = c4[k]

        # 🌕 Pré-confirmação longa (1H): EMA9 cruza MA20 para cima + RSI 50–60 + Vol>média
        if j >= 1 and monitor.allowed_long(s):
            if cross_up(ema9_1[j-1], ema9_1[j], ma20_1[j-1], ma20_1[j]) and (50.0 <= rsi1[j] <= 60.0) and v1[j] >= volma1[j]*1.05:
                msg = (
                    f"🌕 <b>{fmt_symbol(s)} — PRÉ-CONFIRMAÇÃO LONGA (1h)</b>\n"
                    f"<b>💰</b> <code>{price1:.6f}</code>\n"
                    f"<b>🧠</b> EMA9 cruzou MA20 | RSI {rsi1[j]:.1f} | Vol>média\n"
                    f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
                )
                await send_alert(session, msg)
                monitor.mark_long(s)
                return  # um alerta longo por vez

        # 🚀 Tendência longa confirmada (1H): EMA9>MA20>MA50 + RSI>55 + ADX>25
        if monitor.allowed_long(s):
            if (ema9_1[j] > ma20_1[j] > ma50_1[j]) and rsi1[j] > 55.0 and adx1[j] > 25.0:
                msg = (
                    f"🚀 <b>{fmt_symbol(s)} — TENDÊNCIA LONGA CONFIRMADA (1h)</b>\n"
                    f"<b>💰</b> <code>{price1:.6f}</code>\n"
                    f"<b>🧠</b> EMA9>MA20>MA50 | RSI {rsi1[j]:.1f} | ADX {adx1[j]:.1f}\n"
                    f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
                )
                await send_alert(session, msg)
                monitor.mark_long(s)
                return

        # 🌕 Pré-confirmação longa (4H): EMA9 cruza MA20 + RSI>50
        if k >= 1 and monitor.allowed_long(s):
            if cross_up(ema9_4[k-1], ema9_4[k], ma20_4[k-1], ma20_4[k]) and rsi4[k] > 50.0:
                msg = (
                    f"🌕 <b>{fmt_symbol(s)} — PRÉ-CONFIRMAÇÃO LONGA (4h)</b>\n"
                    f"<b>💰</b> <code>{price4:.6f}</code>\n"
                    f"<b>🧠</b> EMA9 cruzou MA20 | RSI {rsi4[k]:.1f}\n"
                    f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
                )
                await send_alert(session, msg)
                monitor.mark_long(s)
                return

        # 🚀 Tendência 4H confirmada: EMA9>MA20>MA50 por 2 velas + RSI>55
        if monitor.allowed_long(s):
            cond_now = (ema9_4[k] > ma20_4[k] > ma50_4[k])
            cond_prev = (ema9_4[k-1] > ma20_4[k-1] > ma50_4[k-1]) if k >= 1 else False
            if cond_now and cond_prev and rsi4[k] > 55.0:
                msg = (
                    f"🚀 <b>{fmt_symbol(s)} — TENDÊNCIA 4H CONFIRMADA</b>\n"
                    f"<b>💰</b> <code>{price4:.6f}</code>\n"
                    f"<b>🧠</b> EMA9>MA20>MA50 por 2 velas | RSI {rsi4[k]:.1f}\n"
                    f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
                )
                await send_alert(session, msg)
                monitor.mark_long(s)
                return

        # 💚 Entrada segura — Reteste (15m/1h): toque EMA9/MA20 + RSI 45–55 + Vol>média
        # (usa timeframe mais responsivo entre 15m e 1h)
        # 15m fetch aqui para não duplicar chamadas acima — opcionalmente pode vir como param
        o15, h15, l15, c15, v15 = await get_klines(session, s, TF_15M, 120)
        ema9_15, ma20_15, ma50_15, ma200_15, rsi15, volma15, adx15, pdi15, mdi15, bb_up15, bb_low15 = compute_indicators(o15,h15,l15,c15,v15)
        m = len(c15)-1
        ok_15 = (touched_and_reacted(l15[m], c15[m], ema9_15[m]) or touched_and_reacted(l15[m], c15[m], ma20_15[m])) and (45.0 <= rsi15[m] <= 55.0) and (v15[m] >= volma15[m]*1.05)
        ok_1  = (touched_and_reacted(l1[j],  c1[j],  ema9_1[j])   or touched_and_reacted(l1[j],  c1[j],  ma20_1[j]))   and (45.0 <= rsi1[j]  <= 55.0)  and (v1[j]  >= volma1[j]*1.05)

        if (ok_15 or ok_1) and monitor.allowed_long(s):
            tf = "15m" if ok_15 else "1h"
            price_used = c15[m] if ok_15 else c1[j]
            rsi_used   = rsi15[m] if ok_15 else rsi1[j]
            msg = (
                f"💚 <b>{fmt_symbol(s)} — ENTRADA SEGURA — RETESTE ({tf})</b>\n"
                f"<b>💰</b> <code>{price_used:.6f}</code>\n"
                f"<b>🧠</b> Toque EMA9/MA20 + RSI {rsi_used:.1f} (45–55) + Vol>média\n"
                f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
            )
            await send_alert(session, msg)
            monitor.mark_long(s)
            return

        # 🌕 Tendência longa combinada (15m + 1h + 4h): EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25 nos 3 tempos
        cond_15 = (ema9_15[m] > ma20_15[m] > ma50_15[m] > ma200_15[m] and rsi15[m] > 55.0 and adx15[m] > 25.0)
        cond_1  = (ema9_1[j]  > ma20_1[j]  > ma50_1[j]  > ma200_1[j]  and rsi1[j]  > 55.0 and adx1[j]  > 25.0)
        cond_4  = (ema9_4[k]  > ma20_4[k]  > ma50_4[k]  > ma200_4[k]  and rsi4[k]  > 55.0 and adx4[k]  > 25.0)
        if cond_15 and cond_1 and cond_4 and monitor.allowed_long(s):
            msg = (
                f"🌕 <b>{fmt_symbol(s)} — TENDÊNCIA LONGA COMBINADA (15m+1h+4h)</b>\n"
                f"<b>💰</b> <code>{c15[m]:.6f}</code>\n"
                f"<b>🧠</b> EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25 nos 3 tempos\n"
                f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
            )
            await send_alert(session, msg)
            monitor.mark_long(s)
            return

        # 🟠 Perdendo força (1h / 4h): RSI < 50 e ADX < 25
        if monitor.allowed_long(s):
            weak_1 = (rsi1[j] < 50.0 and adx1[j] < 25.0)
            weak_4 = (rsi4[k] < 50.0 and adx4[k] < 25.0)
            if weak_1 or weak_4:
                tf = "1h" if weak_1 else "4h"
                price_used = price1 if weak_1 else price4
                rsi_used   = rsi1[j] if weak_1 else rsi4[k]
                adx_used   = adx1[j] if weak_1 else adx4[k]
                msg = (
                    f"🟠 <b>{fmt_symbol(s)} — PERDENDO FORÇA ({tf})</b>\n"
                    f"<b>💰</b> <code>{price_used:.6f}</code>\n"
                    f"<b>🧠</b> RSI {rsi_used:.1f} < 50 e ADX {adx_used:.1f} < 25\n"
                    f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
                )
                await send_alert(session, msg)
                monitor.mark_long(s)
                return

        # ❌ Saída (1h / 4h): EMA9 cruza MA20 para baixo + RSI < 45
        if monitor.allowed_long(s):
            exit_1 = (j >= 1 and cross_up(ma20_1[j-1], ma20_1[j], ema9_1[j-1], ema9_1[j]) and rsi1[j] < 45.0)
            exit_4 = (k >= 1 and cross_up(ma20_4[k-1], ma20_4[k], ema9_4[k-1], ema9_4[k]) and rsi4[k] < 45.0)
            if exit_1 or exit_4:
                tf = "1h" if exit_1 else "4h"
                price_used = price1 if exit_1 else price4
                rsi_used   = rsi1[j] if exit_1 else rsi4[k]
                msg = (
                    f"❌ <b>{fmt_symbol(s)} — SAÍDA ({tf})</b>\n"
                    f"<b>💰</b> <code>{price_used:.6f}</code>\n"
                    f"<b>🧠</b> EMA9 cruzou para baixo da MA20 | RSI {rsi_used:.1f}\n"
                    f"<b>🕒</b> {ts_br()}\n<b>{links(s)}</b>"
                )
                await send_alert(session, msg)
                monitor.mark_long(s)
                return

    except Exception as e:
        print("worker_long error", s, e)

# =========================
# MAIN LOOP
# =========================
async def main():
    async with aiohttp.ClientSession() as session:
        # shortlist inicial
        tickers = await get_24h(session)
        watch = shortlist_from_24h(tickers, SHORTLIST_N)
        hello = f"💻 v3.0 | Monitorando {len(watch)} pares SPOT | CD curto: 15m | CD longo: 1h | {ts_br()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks = []
            for s in watch:
                # Curto prazo
                tasks.append(worker_5m(session, s))
                tasks.append(worker_15m(session, s))
                # Longo prazo (1h/4h + combinada + entrada segura + weak/exit)
                tasks.append(worker_long(session, s))

                # pequeno respiro para evitar rajadas de requests
                await asyncio.sleep(0.05)

            await asyncio.gather(*tasks, return_exceptions=True)

            # pausa do ciclo (60s) para reduzir atraso
            await asyncio.sleep(60)

            # renova shortlist periodicamente
            try:
                tickers = await get_24h(session)
                watch = shortlist_from_24h(tickers, SHORTLIST_N)
            except Exception as e:
                print("refresh shortlist error:", e)

# =========================
# FLASK (Render keep-alive)
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
        return "✅ Binance Alerts Bot v3.0 — curto (5m/15m) + longo (1h/4h) | SPOT-only | CD curto 15m, longo 1h 🇧🇷"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
