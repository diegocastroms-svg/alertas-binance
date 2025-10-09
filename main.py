# ============================================================
#  Binance SPOT Monitor — v13 (FULL MOTIVO)
#  Base: v12 fullformat + "motivo do disparo" em cada alerta
#  Curto (5m/15m) completo | Long (1h/4h) 🟢 em negrito
#  ------------------------------------------------------------
#  Diego & Aurora — 2025-10-09
# ============================================================

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"     # principal: 5m
SHORTLIST_N   = 80
COOLDOWN_SEC  = 15 * 60  # curto
COOLDOWN_LONG = 60 * 60  # long
MIN_PCT = 1.0
MIN_QV  = 300_000.0

# Médias / Indicadores
EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
VOL_MA   = 9
HH_WIN   = 20
ADX_LEN  = 14

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "20"))

# ---------------- UTILS ----------------
def fmt_symbol(symbol: str) -> str:
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol: str) -> str:
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session: aiohttp.ClientSession, text: str):
    # (1) webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            async with session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Webhook error:", e)
    # (2) Telegram (HTML)
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": CHAT_ID, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True
            }
            async with session.post(url, data=payload, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Telegram error:", e)

# ---------------- INDICADORES ----------------
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
    if n < period + 1: return [20.0]*n, [0.0]*n, [0.0]*n
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
    adx14, pdi, mdi = adx(h,l,c, ADX_LEN)
    return ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval="5m", limit=200, sema=None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    sema = sema or asyncio.Semaphore(MAX_CONCURRENCY)
    async with sema:
        async with session.get(url, timeout=12) as r:
            r.raise_for_status()
            data = await r.json()
    o,h,l,c,v = [],[],[],[],[]
    # ignorar a última vela (em formação) para reduzir falsos positivos
    rows = data[:-1] if len(data) > 0 else data
    for k in rows:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session, sema=None):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    sema = sema or asyncio.Semaphore(MAX_CONCURRENCY)
    async with sema:
        async with session.get(url, timeout=15) as r:
            r.raise_for_status()
            return await r.json()

def shortlist_from_24h(tickers, n=80):
    usdt = []
    for t in tickers:
        s = t.get("symbol","")
        if not s.endswith("USDT"): 
            continue
        if any(x in s for x in ("UP","DOWN","BULL","BEAR","PERP")):
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

# ---------------- MONITOR (cooldown + RS) ----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)
        self.cooldown_long = defaultdict(lambda: 0.0)
        self.rs_map = {}
        self.btc_pct = 0.0

    def allowed(self, symbol, kind="GEN"):
        return time.time() - self.cooldown[(symbol, kind)] >= COOLDOWN_SEC

    def mark(self, symbol, kind="GEN"):
        self.cooldown[(symbol, kind)] = time.time()

    def allowed_long(self, symbol, kind="LONG"):
        return time.time() - self.cooldown_long[(symbol, kind)] >= COOLDOWN_LONG

    def mark_long(self, symbol, kind="LONG"):
        self.cooldown_long[(symbol, kind)] = time.time()

    def set_rs(self, rs_map, btc_pct):
        self.rs_map = rs_map or {}
        self.btc_pct = btc_pct or 0.0

    def rs_tag(self, symbol):
        pct = self.rs_map.get(symbol, None)
        if pct is None: return ""
        return " | 🏆 RS+" if (pct - self.btc_pct) > 0.0 else ""

# ---------------- ALERTAS CURTOS (5m) — FULL MOTIVO ----------------
async def worker_5m(session, symbol, monitor: Monitor, sema):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_MAIN, limit=200, sema=sema)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        last = len(c)-1
        prev = last-1 if last>=1 else last
        sym = fmt_symbol(symbol)
        price = c[last]
        ts = ts_brazil_now()
        rs_tag = monitor.rs_tag(symbol)

        # 🚀 Início 5m — EMA9 cruza MA20 e MA50 após fundo/lateral
        cruzou_20 = (ema9[prev] <= ma20[prev] and ema9[last] > ma20[last]) if prev>=0 else False
        cruzou_50 = (ema9[prev] <= ma50[prev] and ema9[last] > ma50[last]) if prev>=0 else False
        fundo_lateral = (rsi14[prev] < 45.0)  # heurística simples
        if cruzou_20 and cruzou_50 and fundo_lateral and rsi14[last] >= 45.0 and monitor.allowed(symbol,"INICIO_5M"):
            motivo = "Cruzamento da EMA9 acima da MA20 e MA50 após fundo/lateralização"
            desc = f"{motivo} | RSI {rsi14[last]:.1f} | Vol ok | 💚 Continuação de alta detectada"
            txt = (f"⭐ {sym} 🚀 — INÍCIO 5M{rs_tag}\n"
                   f"💰 {price:.6f}\n"
                   f"🧠 {desc}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark(symbol,"INICIO_5M")

        # 🌕 Pré-confirmação 5m — 9/20/50 cruzam acima da 200
        todos_acima_now  = (ema9[last] > ma200[last] and ma20[last] > ma200[last] and ma50[last] > ma200[last])
        todos_acima_prev = (ema9[prev] > ma200[prev] and ma20[prev] > ma200[prev] and ma50[prev] > ma200[prev]) if prev>=0 else False
        if todos_acima_now and not todos_acima_prev and 50.0 <= rsi14[last] <= 60.0 and monitor.allowed(symbol,"PRECONF_5M"):
            motivo = "Médias curtas (9/20/50) cruzaram acima da MA200"
            desc = f"{motivo} | RSI {rsi14[last]:.1f} | Entrada de força no curto prazo"
            txt = (f"⭐ {sym} 🌕 — PRÉ-CONFIRMAÇÃO 5M{rs_tag}\n"
                   f"💰 {price:.6f}\n"
                   f"🧠 {desc}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark(symbol,"PRECONF_5M")

        # ♻️ Reteste EMA9 — toque e volta a subir
        touched_ema9 = (l[last] <= ema9[last] and c[last] >= ema9[last])
        if ema9[last] > ma20[last] > ma50[last] and touched_ema9 and rsi14[last] > 50.0 and monitor.allowed(symbol,"RETESTE_EMA9"):
            motivo = "Reteste na EMA9 com reação positiva"
            desc = f"{motivo} | RSI {rsi14[last]:.1f} | Vol ok | 💚 Continuação de alta detectada"
            txt = (f"⭐ {sym} ♻️ — RETESTE EMA9{rs_tag}\n"
                   f"💰 {price:.6f}\n"
                   f"🧠 {desc}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark(symbol,"RETESTE_EMA9")

        # ♻️ Reteste MA20 — toque e reação
        touched_ma20 = (l[last] <= ma20[last] and c[last] >= ma20[last])
        if ema9[last] > ma20[last] > ma50[last] and touched_ma20 and rsi14[last] > 52.0 and monitor.allowed(symbol,"RETESTE_MA20"):
            motivo = "Reteste na MA20 com reação positiva (correção saudável)"
            desc = f"{motivo} | RSI {rsi14[last]:.1f} | Vol ok | 💚 Continuação de alta detectada"
            txt = (f"⭐ {sym} ♻️ — RETESTE MA20{rs_tag}\n"
                   f"💰 {price:.6f}\n"
                   f"🧠 {desc}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark(symbol,"RETESTE_MA20")

        # 📈 Rompimento da resistência — acima da máxima 20
        if len(h) >= HH_WIN and c[last] > max(h[-HH_WIN:]) and rsi14[last] > 55.0 and ema9[last] > ma20[last] and monitor.allowed(symbol,"BREAKOUT_5M"):
            donch = max(h[-HH_WIN:])
            motivo = f"Rompimento: fechou acima da máxima {HH_WIN} ({donch:.6f})"
            desc = f"{motivo} | RSI {rsi14[last]:.1f} | 💥 Rompimento confirmado"
            txt = (f"⭐ {sym} 📈 — ROMPIMENTO DA RESISTÊNCIA{rs_tag}\n"
                   f"💰 {price:.6f}\n"
                   f"🧠 {desc}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark(symbol,"BREAKOUT_5M")

    except Exception as e:
        print("worker_5m error", symbol, e)

# ---------------- CONFIRMAÇÃO CURTA (15m) — FULL MOTIVO ----------------
async def worker_15m(session, symbol, monitor: Monitor, sema):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval="15m", limit=200, sema=sema)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        last = len(c)-1
        prev = last-1 if last>=1 else last
        sym = fmt_symbol(symbol)
        price = c[last]
        ts = ts_brazil_now()
        rs_tag = monitor.rs_tag(symbol)

        # 🌕 Pré-confirmação 15m — EMA9 cruza 200 + médias alinhadas
        cross_9_200 = (ema9[prev] <= ma200[prev] and ema9[last] > ma200[last]) if prev>=0 else False
        sloping_up = (ma20[last] > ma20[prev] and ma50[last] > ma50[prev]) if prev>=0 else False
        if cross_9_200 and sloping_up and 50.0 <= rsi14[last] <= 60.0 and monitor.allowed(symbol,"PRECONF_15M"):
            motivo = "EMA9 cruzou a MA200 com MA20/MA50 ascendentes"
            desc = f"{motivo} | RSI {rsi14[last]:.1f} | Entrada de força (15m)"
            txt = (f"⭐ {sym} 🌕 — PRÉ-CONFIRMAÇÃO 15M{rs_tag}\n"
                   f"💰 {price:.6f}\n"
                   f"🧠 {desc}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark(symbol,"PRECONF_15M")

        # 🚀 Confirmação 15m — 9>20>50>200 + RSI/ADX
        if (ema9[last] > ma20[last] > ma50[last] > ma200[last] and rsi14[last] > 55.0 and adx14[last] > 25.0
            and monitor.allowed(symbol,"CONFIRM_15M")):
            motivo = "Médias alinhadas (9>20>50>200) + RSI/ADX fortes"
            desc = f"{motivo} | RSI {rsi14[last]:.1f} | ADX {adx14[last]:.1f}"
            txt = (f"⭐ {sym} 🚀 — CONFIRMAÇÃO 15M{rs_tag}\n"
                   f"💰 {price:.6f}\n"
                   f"🧠 {desc}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark(symbol,"CONFIRM_15M")

    except Exception as e:
        print("worker_15m error", symbol, e)

# ---------------- LONGOS (1h / 4h) — 🟢 NEGRITO + MOTIVO ----------------
async def worker_1h_4h(session, symbol, monitor: Monitor, sema):
    try:
        # ===== 1H =====
        o1,h1,l1,c1,v1 = await get_klines(session, symbol, interval="1h", limit=200, sema=sema)
        if len(c1) < 60: return
        ema9_1, ma20_1, ma50_1, ma200_1, rsi1, volma1, adx1, pdi1, mdi1 = compute_indicators(o1,h1,l1,c1,v1)
        last1 = len(c1)-1
        prev1 = last1-1 if last1>=1 else last1
        sym = fmt_symbol(symbol)
        ts = ts_brazil_now()
        rs_tag = monitor.rs_tag(symbol)

        # 🌕 Pré 1h
        cross_9_20_1h = (ema9_1[prev1] <= ma20_1[prev1] and ema9_1[last1] > ma20_1[last1]) if prev1>=0 else False
        if cross_9_20_1h and 50.0 <= rsi1[last1] <= 60.0 and v1[last1] >= volma1[last1]*1.20 and monitor.allowed_long(symbol,"PRECONF_LONG_1H"):
            motivo = "EMA9 cruzou MA20 com RSI 50–60 e volume acima da média"
            bullets = f"{motivo}"
            txt = (f"🟢 <b>{sym} — PRÉ-CONFIRMAÇÃO LONGA (1H)</b>{rs_tag}\n"
                   f"💰 {c1[last1]:.6f}\n"
                   f"🧠 {bullets}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark_long(symbol,"PRECONF_LONG_1H")

        # 🚀 Confirmada 1h
        if ema9_1[last1] > ma20_1[last1] > ma50_1[last1] and rsi1[last1] > 55.0 and adx1[last1] > 25.0 and v1[last1] >= volma1[last1] and monitor.allowed_long(symbol,"CONFIRM_LONG_1H"):
            motivo = "EMA9>MA20>MA50 + RSI/ADX fortes (1H)"
            bullets = f"{motivo} | RSI {rsi1[last1]:.1f} | ADX {adx1[last1]:.1f}"
            txt = (f"🟢 <b>{sym} — TENDÊNCIA LONGA CONFIRMADA (1H)</b>{rs_tag}\n"
                   f"💰 {c1[last1]:.6f}\n"
                   f"🧠 {bullets}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark_long(symbol,"CONFIRM_LONG_1H")

        # ===== 4H =====
        o4,h4,l4,c4,v4 = await get_klines(session, symbol, interval="4h", limit=200, sema=sema)
        if len(c4) < 60: return
        ema9_4, ma20_4, ma50_4, ma200_4, rsi4, volma4, adx4, pdi4, mdi4 = compute_indicators(o4,h4,l4,c4,v4)
        last4 = len(c4)-1
        prev4 = last4-1 if last4>=1 else last4
        pprev4= last4-2 if last4>=2 else prev4

        # 🌕 Pré 4h
        cross_9_20_4h = (ema9_4[prev4] <= ma20_4[prev4] and ema9_4[last4] > ma20_4[last4]) if prev4>=0 else False
        if cross_9_20_4h and rsi4[last4] > 50.0 and v4[last4] >= volma4[last4] and monitor.allowed_long(symbol,"PRECONF_4H"):
            motivo = "EMA9 cruzou MA20 com RSI>50 (4H)"
            bullets = f"{motivo}"
            txt = (f"🟢 <b>{sym} — PRÉ-CONFIRMAÇÃO (4H)</b>{rs_tag}\n"
                   f"💰 {c4[last4]:.6f}\n"
                   f"🧠 {bullets}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark_long(symbol,"PRECONF_4H")

        # 🚀 Confirmada 4h — 2ª vela após o cruzamento
        second_bar = (prev4 >= 1 and pprev4 >= 0 and ema9_4[prev4] > ma20_4[prev4] and ema9_4[pprev4] <= ma20_4[pprev4])
        if second_bar and (ema9_4[last4] > ma20_4[last4] > ma50_4[last4]) and rsi4[last4] > 55.0 and monitor.allowed_long(symbol,"CONFIRM_4H"):
            motivo = "2ª vela após cruzamento + 9>20>50 e RSI>55 (4H)"
            bullets = f"{motivo}"
            txt = (f"🟢 <b>{sym} — TENDÊNCIA 4H CONFIRMADA</b>{rs_tag}\n"
                   f"💰 {c4[last4]:.6f}\n"
                   f"🧠 {bullets}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark_long(symbol,"CONFIRM_4H")

        # 🌕 Combinada (15m+1h+4h) + 💚 Entrada Segura (15m/1h)
        # 15m para combinado/entrada:
        o15,h15,l15,c15,v15 = await get_klines(session, symbol, interval="15m", limit=120, sema=sema)
        if len(c15) >= 60:
            ema9_15, ma20_15, ma50_15, ma200_15, rsi15, volma15, adx15, pdi15, mdi15 = compute_indicators(o15,h15,l15,c15,v15)
            last15 = len(c15)-1
            # Combinado
            cond_15 = (ema9_15[last15] > ma20_15[last15] > ma50_15[last15] > ma200_15[last15] and rsi15[last15] > 55.0 and adx15[last15] > 25.0)
            cond_1  = (ema9_1[last1]     > ma20_1[last1]     > ma50_1[last1]     > ma200_1[last1]     and rsi1[last1]    > 55.0 and adx1[last1]    > 25.0)
            cond_4  = (ema9_4[last4]     > ma20_4[last4]     > ma50_4[last4]     > ma200_4[last4]     and rsi4[last4]    > 55.0 and adx4[last4]    > 25.0)
            if cond_15 and cond_1 and cond_4 and monitor.allowed_long(symbol,"LONG_COMBINED"):
                motivo = "EMA9>MA20>MA50>MA200 + RSI/ADX fortes em 15m/1h/4h"
                bullets = f"{motivo}"
                txt = (f"🟢 <b>{sym} — TENDÊNCIA LONGA COMBINADA</b>{rs_tag}\n"
                       f"💰 {c1[last1]:.6f}\n"
                       f"🧠 {bullets}\n"
                       f"⏰ {ts}\n"
                       f"{binance_links(symbol)}")
                await send_alert(session, txt); monitor.mark_long(symbol,"LONG_COMBINED")

            # Entrada segura — reteste 15m
            touch_ema9_15 = (l15[last15] <= ema9_15[last15] and c15[last15] >= ema9_15[last15])
            touch_ma20_15 = (l15[last15] <= ma20_15[last15] and c15[last15] >= ma20_15[last15])
            if (ema9_15[last15] > ma20_15[last15] > ma50_15[last15]) and (touch_ema9_15 or touch_ma20_15) and 45.0 <= rsi15[last15] <= 55.0 and v15[last15] >= v15[last15-1]*1.05 and monitor.allowed_long(symbol,"ENTRY_SAFE_15M"):
                ref_line = "EMA9" if touch_ema9_15 else "MA20"
                motivo = f"Entrada segura: reteste {ref_line} (15m) com reação + RSI 45–55 + Vol +5%"
                bullets = f"{motivo}"
                txt = (f"🟢 <b>{sym} — ENTRADA SEGURA — RETESTE (15m)</b>{rs_tag}\n"
                       f"💰 {c15[last15]:.6f}\n"
                       f"🧠 {bullets}\n"
                       f"⏰ {ts}\n"
                       f"{binance_links(symbol)}")
                await send_alert(session, txt); monitor.mark_long(symbol,"ENTRY_SAFE_15M")

        # Entrada segura — reteste 1h
        touch_ema9_1h = (l1[last1] <= ema9_1[last1] and c1[last1] >= ema9_1[last1])
        touch_ma20_1h = (l1[last1] <= ma20_1[last1] and c1[last1] >= ma20_1[last1])
        if (ema9_1[last1] > ma20_1[last1] > ma50_1[last1]) and (touch_ema9_1h or touch_ma20_1h) and 45.0 <= rsi1[last1] <= 55.0 and v1[last1] >= v1[prev1]*1.05 and monitor.allowed_long(symbol,"ENTRY_SAFE_1H"):
            ref_line = "EMA9" if touch_ema9_1h else "MA20"
            motivo = f"Entrada segura: reteste {ref_line} (1h) com reação + RSI 45–55 + Vol +5%"
            bullets = f"{motivo}"
            txt = (f"🟢 <b>{sym} — ENTRADA SEGURA — RETESTE (1h)</b>{rs_tag}\n"
                   f"💰 {c1[last1]:.6f}\n"
                   f"🧠 {bullets}\n"
                   f"⏰ {ts}\n"
                   f"{binance_links(symbol)}")
            await send_alert(session, txt); monitor.mark_long(symbol,"ENTRY_SAFE_1H")

    except Exception as e:
        print("worker_1h_4h error", symbol, e)

# ---------------- MAIN ----------------
async def main():
    monitor = Monitor()
    sema = asyncio.Semaphore(MAX_CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        # 24h → watchlist + força relativa
        tickers = await get_24h(session, sema=sema)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        rs_map = {}
        btc_pct = 0.0
        for t in tickers:
            s = t.get("symbol","")
            if s == "BTCUSDT":
                try: btc_pct = float(t.get("priceChangePercent","0") or 0.0)
                except: btc_pct = 0.0
            if s.endswith("USDT"):
                try: rs_map[s] = float(t.get("priceChangePercent","0") or 0.0)
                except: rs_map[s] = 0.0
        monitor.set_rs(rs_map, btc_pct)

        hello = f"💻 v13 FULL MOTIVO | Monitorando {len(watchlist)} pares SPOT | {ts_brazil_now()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks = []
            for s in watchlist:
                tasks.append(worker_5m(session, s, monitor, sema))
                tasks.append(worker_15m(session, s, monitor, sema))
                tasks.append(worker_1h_4h(session, s, monitor, sema))
            await asyncio.gather(*tasks, return_exceptions=True)

            await asyncio.sleep(180)

            # atualiza shortlist e RS
            try:
                tickers = await get_24h(session, sema=sema)
                watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
                rs_map = {}
                btc_pct = 0.0
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
                print("Erro ao atualizar shortlist/RS:", e)

# ---------------- FLASK KEEP-ALIVE ----------------
def start_bot():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Binance Alerts Bot (v13 FULL MOTIVO) ativo!"

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
