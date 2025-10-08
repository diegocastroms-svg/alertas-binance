# v11.3_final_structural_signals.py
# Core: 5m | Confirmação: 15m
# Novos alertas paralelos: Minervini (MA200 ascendente) e Turtle (Donchian 20-high)
# Mantém todos os sinais anteriores e formato de mensagens (🇧🇷, HTML, links Binance)

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"         # Core
INTERVAL_CONF = "15m"        # Confirmação
SHORTLIST_N   = 65
COOLDOWN_SEC  = 15 * 60
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

# ----------------- Utils -----------------
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
    # (1) webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    # (2) Telegram
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except:
            pass

def pct_change(new, old):
    return (new / (old + 1e-12) - 1.0) * 100.0

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
    plus_dm  = [0.0]; minus_dm = [0.0]
    for i in range(1, n):
        up_move   = h[i] - h[i-1]
        down_move = l[i-1] - l[i]
        plus_dm.append(  up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append( down_move if (down_move > up_move and down_move > 0) else 0.0)
    # Wilder smoothing
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
    # Preencher inicio com valores iniciais
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

def bb_width_ratio(ma20, bb_up, bb_low, i):
    mid = ma20[i]
    if mid <= 0: return 0.0
    return (bb_up[i] - bb_low[i]) / mid

# ----------------- Binance -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    # remover candle em formação
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
        s = t.get("symbol", "")
        if not s.endswith("USDT"): 
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

# ----------------- Emojis / Mensagens -----------------
def kind_emoji(kind):
    return {
        "MONITORANDO_REVERSAO":"🔍",
        "TENDENCIA_INICIANDO_5M":"⬆️",
        "TENDENCIA_CONFIRMADA_15M":"💎",
        "REVERSAO_FUNDO":"🔄",
        "RETESTE_EMA9":"♻️",
        "RETESTE_MA20":"♻️",
        "PERDENDO_FORCA":"🟠",
        "SAIDA":"🚪",
        "MERCADO_ESTICADO":"⚠️",
        "MINERVINI_200_UP":"💚",
        "TURTLE_BREAKOUT":"📈",
    }.get(kind,"📌")

def build_msg(symbol, kind, price, bullets, rs_tag=""):
    star="⭐"; sym=fmt_symbol(symbol); em=kind_emoji(kind)
    tag = f" | 🏆 RS+" if rs_tag else ""
    return (
        f"{star} {sym} {em} — {kind.replace('_',' ')}{tag}\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"🧠 {bullets}\n"
        f"⏰ {ts_brazil_now()}\n"
        f"{binance_links(symbol)}"
    )

# ----------------- Monitor de estado -----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)     # por (symbol, kind)
        self.rs_24h   = {}                           # força relativa por símbolo vs BTC
        self.btc_pct  = 0.0

    def allowed(self, symbol, kind):
        return time.time() - self.cooldown[(symbol, kind)] >= COOLDOWN_SEC

    def mark(self, symbol, kind):
        self.cooldown[(symbol, kind)] = time.time()

    def set_rs(self, rs_map, btc_pct):
        self.rs_24h = rs_map or {}
        self.btc_pct = btc_pct or 0.0

    def rs_tag(self, symbol):
        pct = self.rs_24h.get(symbol, None)
        if pct is None: return ""
        return "RS+" if (pct - self.btc_pct) > 0.0 else ""

# ----------------- Worker -----------------
async def candle_worker(session, symbol, monitor: Monitor):
    try:
        # Core 5m
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_MAIN, limit=200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, vol_ma, bb_up, bb_low, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        last = len(c) - 1; prev = last - 1

        signals = []
        rs_tag = monitor.rs_tag(symbol)

        # -------- Pré-alerta Wyckoff + Bollinger Squeeze (queda + compressão) --------
        if last >= 48:
            drop_4h = pct_change(c[last], c[last-48])     # ~4h
            bw = bb_width_ratio(ma20, bb_up, bb_low, last)
            if drop_4h <= -8.0 and bw <= 0.08 and 40.0 <= rsi14[last] <= 55.0:
                if not (rsi14[last] >= 70.0 and c[last] > bb_up[last]):
                    signals.append(("MONITORANDO_REVERSAO",
                        f"Queda {drop_4h:.1f}% | Compressão BB | RSI {rsi14[last]:.1f}"))

        # -------- Reversão de fundo (RSI 45->50 + vol + cruzamento 9>20) --------
        if (rsi14[prev] < 45.0 <= rsi14[last] and
            v[last] >= vol_ma[last] * 1.2 and
            ema9[prev] <= ma20[prev] and ema9[last] > ma20[last]):
            signals.append(("REVERSAO_FUNDO",
                f"RSI {rsi14[prev]:.1f}→{rsi14[last]:.1f} | Vol {v[last]/max(1e-9,vol_ma[last]):.1f}x | EMA9 cruzou MA20 ↑"))

        # -------- Tendência iniciando (5m) - 9>20>50 + RSI>50 + Vol>=média --------
        cross_9_20 = (ema9[prev] <= ma20[prev] and ema9[last] > ma20[last])
        if cross_9_20 and ema9[last] > ma50[last] and rsi14[last] > 50.0 and v[last] >= vol_ma[last]:
            adx_note = f" | ADX {adx14[last]:.1f}" if adx14[last] >= 20 else " | ADX baixo"
            signals.append(("TENDENCIA_INICIANDO_5M",
                f"EMA9>MA20/50 | RSI {rsi14[last]:.1f} | Vol≥média{adx_note}"))

        # -------- Retestes (continuação) --------
        # EMA9
        if (ema9[last] > ma20[last] > ma50[last] and
            (l[last] <= ema9[last]) and c[last] >= ema9[last] and
            rsi14[last] >= 55.0 and v[last] >= vol_ma[last]*0.9):
            signals.append(("RETESTE_EMA9",
                f"Reteste na EMA9 + reação | RSI {rsi14[last]:.1f} | Vol ok"))
        # MA20
        if (ema9[last] > ma20[last] > ma50[last] and
            (l[last] <= ma20[last]) and c[last] >= ma20[last] and
            rsi14[last] >= 52.0 and v[last] >= vol_ma[last]*0.9):
            signals.append(("RETESTE_MA20",
                f"Reteste na MA20 + reação | RSI {rsi14[last]:.1f} | Vol ok"))

        # -------- Mercado esticado (BB sup + RSI>=70) --------
        if c[last] > bb_up[last] and rsi14[last] >= 70.0:
            signals.append(("MERCADO_ESTICADO", f"Acima da BB superior | RSI {rsi14[last]:.1f}"))

        # -------- Confirmação 15m (tendência forte com ADX) --------
        # Busca leve do 15m apenas para confirmar o cenário
        try:
            o15,h15,l15,c15,v15 = await get_klines(session, symbol, interval=INTERVAL_CONF, limit=120)
            if len(c15) >= 30:
                ema9_15, ma20_15, ma50_15, ma200_15, rsi15, volma15, bb_up15, bb_low15, adx15, pdi15, mdi15 = compute_indicators(o15,h15,l15,c15,v15)
                last15 = len(c15) - 1
                if ema9_15[last15] > ma20_15[last15] > ma50_15[last15] and adx15[last15] >= 25.0:
                    # Confirmação estrutural de 15m
                    if monitor.allowed(symbol, "TENDENCIA_CONFIRMADA_15M"):
                        msg = build_msg(symbol, "TENDENCIA_CONFIRMADA_15M", c[last],
                                        f"9>20>50 (15m) | ADX {adx15[last15]:.1f} | RSI {rsi15[last15]:.1f}", rs_tag)
                        await send_alert(session, msg)
                        monitor.mark(symbol, "TENDENCIA_CONFIRMADA_15M")
        except Exception as _e:
            # Silencioso para não interromper os demais sinais
            pass

        # -------- Alertas paralelos (independentes) --------
        # Minervini: MA200 ascendente (5 velas)
        slope200 = ma200[last] - ma200[last-5] if last >= 5 else 0.0
        if slope200 > 0.0:
            if monitor.allowed(symbol, "MINERVINI_200_UP"):
                m = build_msg(symbol, "MINERVINI_200_UP", c[last],
                              f"MA200 ascendente (slope {slope200:.6f})", rs_tag)
                await send_alert(session, m)
                monitor.mark(symbol, "MINERVINI_200_UP")

        # Turtle / Donchian 20-high: fechamento acima da máxima de 20 (excl. atual)
        if last >= 21:
            donchian_high = max(h[last-20:last])
            if c[last] > donchian_high:
                if monitor.allowed(symbol, "TURTLE_BREAKOUT"):
                    m = build_msg(symbol, "TURTLE_BREAKOUT", c[last],
                                  f"Rompimento: fechou acima da máxima 20 ({donchian_high:.6f})", rs_tag)
                    await send_alert(session, m)
                    monitor.mark(symbol, "TURTLE_BREAKOUT")

        # -------- Envio do primeiro sinal composto (se houver) --------
        if signals:
            k0, d0 = signals[0]
            if monitor.allowed(symbol, k0):
                txt = build_msg(symbol, k0, c[last], d0 if len(signals)==1 else " | ".join([d for _,d in signals]), rs_tag)
                await send_alert(session, txt)
                monitor.mark(symbol, k0)

    except Exception as e:
        print("Worker error", symbol, e)

# ----------------- Main -----------------
async def main():
    monitor = Monitor()
    async with aiohttp.ClientSession() as session:
        # 24h snapshot para shortlist e força relativa (RS)
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        # Map de %24h para RS (vs BTC)
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

        hello = (f"💻 v11.3 — Core 5m | Confirmação 15m | ADX ativo | "
                 f"Wyckoff+Squeeze | RS (CAN SLIM) | Minervini 200↑ | Turtle 20H | "
                 f"{len(watchlist)} pares SPOT | {ts_brazil_now()}")
        await send_alert(session, hello)
        print(hello)

        while True:
            # workers
            await asyncio.gather(*[candle_worker(session, s, monitor) for s in watchlist])
            await asyncio.sleep(180)
            # refresh shortlist + RS
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
        return "✅ Binance Alerts Bot v11.3 — 5m core, 15m conf, ADX, Wyckoff+Squeeze, RS, Minervini & Turtle 🇧🇷"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
