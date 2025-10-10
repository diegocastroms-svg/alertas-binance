# main_v2_3.py
# Base: v2.1 (completa) — preservada
# Ajuste: corrigido atraso (detecção intrabar) só nos cruzamentos de 5m e 15m
# - Pré-confirmada (5m): EMA9 > MA200 — intrabar (2 leituras consecutivas)
# - Pré-confirmada (15m): EMA9 > MA200 — intrabar (2 leituras consecutivas)
# Demais alertas (curtos/longos) inalterados.
# Flask, filtros SPOT, mensagens e cooldowns preservados.

import os, asyncio, time, math
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from collections import defaultdict, deque
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_5M   = "5m"
INTERVAL_15M  = "15m"
INTERVAL_1H   = "1h"
INTERVAL_4H   = "4h"

SHORTLIST_N   = 65
COOLDOWN_SHORT = 15 * 60          # curto prazo
COOLDOWN_LONG  = 60 * 60          # alertas longos (1h/4h/comb.)
COOLDOWN_ENTRY = 10 * 60          # entrada segura / retestes

MIN_PCT   = 1.0
MIN_QV    = 300_000.0

EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
ADX_LEN  = 14
VOL_MA   = 9
BB_LEN   = 20

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
def ts_brazil_now():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

def fmt_symbol(symbol: str) -> str:
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol: str) -> str:
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

async def send_alert(session: aiohttp.ClientSession, text: str):
    # webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    # Telegram
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            await session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10
            )
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
        tr_curr = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        tr.append(tr_curr)
    return tr

def adx(h, l, c, period=14):
    n = len(c)
    if n < period + 1: return [20.0] * n, [0.0]*n, [0.0]*n
    tr = true_range(h, l, c)
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, n):
        up = h[i] - h[i-1]
        down = l[i-1] - l[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    atr = [0.0]*n; atr[period] = sum(tr[1:period+1])
    pdm, mdm = [0.0]*n, [0.0]*n
    pdm[period] = sum(plus_dm[1:period+1]); mdm[period] = sum(minus_dm[1:period+1])
    for i in range(period+1, n):
        atr[i] = atr[i-1] - (atr[i-1]/period) + tr[i]
        pdm[i] = pdm[i-1] - (pdm[i-1]/period) + plus_dm[i]
        mdm[i] = mdm[i-1] - (mdm[i-1]/period) + minus_dm[i]
    plus_di, minus_di = [0.0]*n, [0.0]*n
    for i in range(n):
        plus_di[i]  = 100.0 * (pdm[i] / (atr[i] + 1e-12))
        minus_di[i] = 100.0 * (mdm[i] / (atr[i] + 1e-12))
    dx = [0.0]*n
    for i in range(n):
        dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i] + 1e-12)
    adx_vals = [0.0]*n; adx_vals[period] = sum(dx[1:period+1]) / period
    for i in range(period+1, n):
        adx_vals[i] = (adx_vals[i-1]*(period-1) + dx[i]) / period
    for i in range(period): adx_vals[i] = adx_vals[period]
    return adx_vals, plus_di, minus_di

def bollinger(c, n=20):
    ma = sma(c, n)
    std = rolling_std(c, n)
    up = [ma[i] + 2*std[i] for i in range(len(c))]
    low = [ma[i] - 2*std[i] for i in range(len(c))]
    return ma, up, low

def compute_indicators(o,h,l,c,v):
    ema9  = ema(c, EMA_FAST)
    ma20  = sma(c, MA_SLOW)
    ma50  = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    adx14, pdi, mdi = adx(h, l, c, ADX_LEN)
    bb_ma, bb_up, bb_low = bollinger(c, BB_LEN)
    return ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi, bb_ma, bb_up, bb_low

# ----------------- Binance -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol":symbol, "interval":interval, "limit":limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    # removendo a última vela em formação para cálculos base
    o,h,l,c,v = [],[],[],[],[]
    for k in data[:-1]:
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
        # bloquear tokens alavancados/futuros/perp e fora do SPOT-padrão
        blocked = (
            "UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD",
            "_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_BTC",
            "_EUR","_TRY","_BRL","_ETH","_BNB","_SOL"
        )
        if any(x in s for x in blocked):
            continue
        pct = float(t.get("priceChangePercent","0") or 0.0)
        qv  = float(t.get("quoteVolume","0") or 0.0)
        if abs(pct) >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Mensageria -----------------
def header_pair(symbol, dot=""):
    # “bola” colorida opcional antes do par (ex.: 🟢/🟡/🔴)
    sym = fmt_symbol(symbol)
    return f"{dot} {sym}".strip()

def msg_block(title, price, bullets):
    return f"{title}\n💰 <code>{price:.6f}</code>\n🧠 {bullets}\n⏰ {ts_brazil_now()}\n{links_block}"

def build_msg(symbol, title, price, bullets):
    head = header_pair(symbol)
    return f"{head}\n{msg_block(title, price, bullets)}"

def build_msg_bold(symbol, title, price, bullets):
    head = header_pair(symbol)
    return f"<b>{head} — {title}</b>\n<b>💰 Preço:</b> <code>{price:.6f}</code>\n<b>🧠 {bullets}</b>\n<b>🕒 {ts_brazil_now()}</b>\n<b>{binance_links(symbol)}</b>"

# ----------------- Monitor / Cooldowns -----------------
class Monitor:
    def __init__(self):
        self.cd_short = defaultdict(lambda: 0.0)  # por (symbol, kind)
        self.cd_long  = defaultdict(lambda: 0.0)  # por symbol
        self.intrabar_memory = defaultdict(int)   # contador de leituras consecutivas por (symbol, tf)
        self.rs_24h = {}; self.btc_pct = 0.0

    def allowed(self, symbol, kind, secs=COOLDOWN_SHORT):
        return time.time() - self.cd_short[(symbol, kind)] >= secs
    def mark(self, symbol, kind):
        self.cd_short[(symbol, kind)] = time.time()

    def allowed_long(self, symbol):
        return time.time() - self.cd_long[symbol] >= COOLDOWN_LONG
    def mark_long(self, symbol):
        self.cd_long[symbol] = time.time()

    def touch_intrabar(self, key, ok):
        # ok=True: aumenta contador; ok=False: zera
        if ok:
            self.intrabar_memory[key] += 1
        else:
            self.intrabar_memory[key] = 0
        return self.intrabar_memory[key]

    def set_rs(self, rs_map, btc_pct):
        self.rs_24h = rs_map or {}
        self.btc_pct = btc_pct or 0.0

monitor = Monitor()
links_block = ""  # preenchido runtime por símbolo

# ----------------- Workers CURTOS -----------------
async def worker_5m(session, symbol):
    """
    CURTO — 5m:
    - Iniciando tendência (após queda+lateral): EMA9 cruza MA20/MA50 (candle fechado)
    - Pré-confirmada (5m): EMA9>MA200 (intrabar corrigido)
    - Retestes 5m foram retirados a seu pedido — ficam no 15m.
    - Rompimento resistência (Donchian 20) opcional no 5m.
    """
    global links_block
    try:
        o,h,l,c,v = await get_klines(session, symbol, INTERVAL_5M, 200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi, bb_ma, bb_up, bb_low = compute_indicators(o,h,l,c,v)
        last=len(c)-1; prev=last-1
        price=c[last]; links_block=binance_links(symbol)

        # 🚀 Tendência iniciando (5m) — cruzamento EMA9 acima de MA20 e MA50 após queda+lateral (fechamento)
        # (mantido como estava, candle fechado para reduzir ruído)
        if (ema9[prev] <= ma20[prev] or ema9[prev] <= ma50[prev]) and (ema9[last] > ma20[last] and ema9[last] > ma50[last]):
            if monitor.allowed(symbol, "INI_5M"):
                txt = (
                    f"{header_pair(symbol,'🟢')}\n"
                    f"⬆️ TENDÊNCIA INICIANDO (5m)\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 EMA9 cruzou acima da MA20 e MA50 após lateralização — RSI {rsi14[last]:.1f}\n"
                    f"⏰ {ts_brazil_now()}\n{links_block}"
                )
                await send_alert(session, txt); monitor.mark(symbol, "INI_5M")

        # 🌕 Pré-confirmada (5m) — EMA9 > MA200 (INTRABAR CORRIGIDO)
        ok_now = (ema9[last] > ma200[last])
        key = (symbol, "ib_5m_pre")
        streak = monitor.touch_intrabar(key, ok_now)
        if streak >= 2:  # 2 leituras consecutivas (~30-45s)
            if monitor.allowed(symbol, "PRECONF_5M"):
                txt = (
                    f"{header_pair(symbol,'🟢')}\n"
                    f"⬆️ TENDÊNCIA PRÉ-CONFIRMADA (5m)\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 EMA9/MA20/MA50 acima da MA200 (5m) | RSI {rsi14[last]:.1f} | ADX {adx14[last]:.1f}\n"
                    f"⏰ {ts_brazil_now()}\n{links_block}"
                )
                await send_alert(session, txt); monitor.mark(symbol, "PRECONF_5M")

        # 📈 Rompimento de resistência (Donchian 20-high em 5m) — opcional (mantido)
        if last >= 21:
            donchian_high = max(h[last-20:last])
            if c[last] > donchian_high and monitor.allowed(symbol, "BREAK_5M"):
                txt = (
                    f"{header_pair(symbol,'🟢')}\n"
                    f"📈 ROMPIMENTO DA RESISTÊNCIA (5m)\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 Fechou acima da máxima 20 ({donchian_high:.6f}) — rompimento confirmado\n"
                    f"⏰ {ts_brazil_now()}\n{links_block}"
                )
                await send_alert(session, txt); monitor.mark(symbol, "BREAK_5M")

    except Exception as e:
        print("worker_5m error", symbol, e)

async def worker_15m(session, symbol):
    """
    CURTO — 15m:
    - Pré-confirmada (15m) — EMA9 cruza MA200 (INTRABAR CORRIGIDO)
    - Confirmada (15m) — EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25 (candle fechado)
    - Retestes (EMA9/MA20) 15m — continuação
    """
    global links_block
    try:
        o,h,l,c,v = await get_klines(session, symbol, INTERVAL_15M, 200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi, bb_ma, bb_up, bb_low = compute_indicators(o,h,l,c,v)
        last=len(c)-1; price=c[last]; links_block=binance_links(symbol)

        # 🌕 Pré-confirmada (15m) — EMA9 > MA200 (INTRABAR CORRIGIDO)
        ok_now = (ema9[last] > ma200[last])
        key = (symbol, "ib_15m_pre")
        streak = monitor.touch_intrabar(key, ok_now)
        if streak >= 2:
            if monitor.allowed(symbol, "PRECONF_15M"):
                txt = (
                    f"{header_pair(symbol,'🟢')}\n"
                    f"⬆️ TENDÊNCIA PRÉ-CONFIRMADA (15m)\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 EMA9 cruzou MA200 — força compradora (15m) | RSI {rsi14[last]:.1f} | ADX {adx14[last]:.1f}\n"
                    f"⏰ {ts_brazil_now()}\n{links_block}"
                )
                await send_alert(session, txt); monitor.mark(symbol, "PRECONF_15M")

        # 🚀 Tendência CONFIRMADA (15m) — candle fechado
        if (ema9[last] > ma20[last] > ma50[last] > ma200[last] and
            rsi14[last] > 55.0 and adx14[last] > 25.0):
            if monitor.allowed(symbol, "CONF_15M"):
                txt = (
                    f"{header_pair(symbol,'🟢')}\n"
                    f"⬆️ TENDÊNCIA CONFIRMADA (15m)\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25 (15m)\n"
                    f"⏰ {ts_brazil_now()}\n{links_block}"
                )
                await send_alert(session, txt); monitor.mark(symbol, "CONF_15M")

        # ♻️ Reteste EMA9 (15m) — continuação de alta
        if (l[last] <= ema9[last] and c[last] >= ema9[last] and
            ema9[last] > ma20[last] > ma50[last] and rsi14[last] >= 55.0 and v[last] >= volma[last]*0.95):
            if monitor.allowed(symbol, "RETESTE_15M_EMA9", COOLDOWN_ENTRY):
                txt = (
                    f"{header_pair(symbol,'🟢')}\n"
                    f"♻️ RETESTE EMA9 (15m)\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 Toque na EMA9 + reação — Continuação da alta\n"
                    f"⏰ {ts_brazil_now()}\n{links_block}"
                )
                await send_alert(session, txt); monitor.mark(symbol, "RETESTE_15M_EMA9")

        # ♻️ Reteste MA20 (15m)
        if (l[last] <= ma20[last] and c[last] >= ma20[last] and
            ema9[last] > ma20[last] > ma50[last] and rsi14[last] >= 52.0 and v[last] >= volma[last]*0.95):
            if monitor.allowed(symbol, "RETESTE_15M_MA20", COOLDOWN_ENTRY):
                txt = (
                    f"{header_pair(symbol,'🟢')}\n"
                    f"♻️ RETESTE MA20 (15m)\n"
                    f"💰 <code>{price:.6f}</code>\n"
                    f"🧠 Toque na MA20 + reação — Correção saudável\n"
                    f"⏰ {ts_brazil_now()}\n{links_block}"
                )
                await send_alert(session, txt); monitor.mark(symbol, "RETESTE_15M_MA20")

    except Exception as e:
        print("worker_15m error", symbol, e)

# ----------------- Workers LONGOS (1h / 4h) -----------------
async def worker_1h(session, symbol):
    """
    LONGO — 1h:
    - Pré-confirmação Longa (1h) — 1ª vela: EMA9 cruza MA20 + RSI 50–60 + vol > média
    - Tendência Longa CONFIRMADA (1h) — 2ª vela: EMA9>MA20>MA50 + RSI>55 + ADX>25
    - Entrada segura — Reteste (1h): toque EMA9/MA20 + RSI 45–55 + vol > média
    """
    global links_block
    try:
        o,h,l,c,v = await get_klines(session, symbol, INTERVAL_1H, 200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi, bb_ma, bb_up, bb_low = compute_indicators(o,h,l,c,v)
        last=len(c)-1; price=c[last]; links_block=binance_links(symbol)

        # 🌕 Pré-confirmação Longa (1h) — 1ª vela
        if (last>=1 and ema9[last-1] <= ma20[last-1] and ema9[last] > ma20[last] and
            50.0 <= rsi14[last] <= 60.0 and v[last] >= volma[last]*1.05):
            if monitor.allowed_long(symbol):
                txt = build_msg_bold(symbol, "PRÉ-CONFIRMAÇÃO LONGA (1h)", price,
                                     f"EMA9 cruzou MA20 | RSI {rsi14[last]:.1f} | Volume > média")
                await send_alert(session, txt); monitor.mark_long(symbol)

        # 🚀 Tendência Longa CONFIRMADA (1h) — 2ª vela (estrutura mantida)
        if (last>=1 and ema9[last] > ma20[last] > ma50[last] and
            ema9[last-1] > ma20[last-1] > ma50[last-1] and
            rsi14[last] > 55.0 and adx14[last] > 25.0):
            if monitor.allowed_long(symbol):
                txt = build_msg_bold(symbol, "TENDÊNCIA LONGA CONFIRMADA (1h)", price,
                                     f"EMA9>MA20>MA50 (2 velas) | RSI {rsi14[last]:.1f} | ADX {adx14[last]:.1f}")
                await send_alert(session, txt); monitor.mark_long(symbol)

        # 💚 Entrada segura — Reteste (1h)
        touched = ((l[last] <= ema9[last] and c[last] >= ema9[last]) or
                   (l[last] <= ma20[last] and c[last] >= ma20[last]))
        if touched and 45.0 <= rsi14[last] <= 55.0 and v[last] >= volma[last]*1.05:
            if monitor.allowed(symbol, "ENTRY_SAFE_1H", COOLDOWN_ENTRY):
                txt = build_msg_bold(symbol, "ENTRADA SEGURA — RETESTE (1h)", price,
                                     f"Toque EMA9/MA20 + RSI moderado + Volume > média")
                await send_alert(session, txt); monitor.mark(symbol, "ENTRY_SAFE_1H")

    except Exception as e:
        print("worker_1h error", symbol, e)

async def worker_4h(session, symbol):
    """
    LONGO — 4h:
    - Pré-confirmação (4h) — 1ª vela: EMA9 cruza MA20 + RSI>50
    - Tendência 4h CONFIRMADA — 2ª vela: EMA9>MA20>MA50 (mantida) + RSI>55
    """
    global links_block
    try:
        o,h,l,c,v = await get_klines(session, symbol, INTERVAL_4H, 200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi, bb_ma, bb_up, bb_low = compute_indicators(o,h,l,c,v)
        last=len(c)-1; price=c[last]; links_block=binance_links(symbol)

        # 🌕 Pré-confirmação (4h) — 1ª vela
        if (last>=1 and ema9[last-1] <= ma20[last-1] and ema9[last] > ma20[last] and rsi14[last] > 50.0):
            if monitor.allowed_long(symbol):
                txt = build_msg_bold(symbol, "PRÉ-CONFIRMAÇÃO (4h)", price,
                                     f"EMA9 cruzou MA20 | RSI {rsi14[last]:.1f}")
                await send_alert(session, txt); monitor.mark_long(symbol)

        # 🚀 Tendência 4h CONFIRMADA — 2ª vela (estrutura mantida)
        if (last>=1 and ema9[last] > ma20[last] > ma50[last] and
            ema9[last-1] > ma20[last-1] > ma50[last-1] and rsi14[last] > 55.0):
            if monitor.allowed_long(symbol):
                txt = build_msg_bold(symbol, "TENDÊNCIA 4h CONFIRMADA", price,
                                     f"Estrutura mantida por 2 velas | RSI {rsi14[last]:.1f}")
                await send_alert(session, txt); monitor.mark_long(symbol)

    except Exception as e:
        print("worker_4h error", symbol, e)

# ----------------- Orquestração / Main -----------------
async def main():
    global links_block
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        # força relativa simples vs BTC (tag RS+ opcional)
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

        hello = f"💻 v2.3 | 5m/15m intrabar corrigido | Longos 1h/4h ativos | {len(watchlist)} pares SPOT | {ts_brazil_now()}"
        await send_alert(session, hello); print(hello)

        while True:
            tasks = []
            for s in watchlist:
                links_block = binance_links(s)
                tasks += [
                    worker_5m(session, s),
                    worker_15m(session, s),
                    worker_1h(session, s),
                    worker_4h(session, s),
                ]
            await asyncio.gather(*tasks)

            # intervalo entre varreduras
            await asyncio.sleep(30)

            # refresh shortlist e força relativa a cada ciclo
            try:
                tickers = await get_24h(session)
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

# ----------------- Flask -----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Binance Alerts Bot v2.3 — (5m/15m intrabar) + (1h/4h longos) 🇧🇷"

if __name__ == "__main__":
    import threading
    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
