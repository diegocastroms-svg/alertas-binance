import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL = "5m"
CONFIRM_INTERVAL = "15m"
SHORTLIST_N = 40
COOLDOWN_SEC = 15 * 60
MIN_PCT = 1.0
MIN_QV  = 300_000.0

# Médias e parâmetros
EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
VOL_MA   = 9
HH_WIN   = 20

# MACD
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

# ADX
ADX_LEN = 14

# Reversão/queda (rebote)
DROP_PCT_TRIGGER = -10.0   # queda <= -10% em 24h -> alerta QUEDA_FORTE + monitorar rebote
REBOTE_MODE_PCT  = -8.0    # abaixo disso CONTINUAÇÃO_ALTA vira REBOTE_TECNICO
RSI_REBOUND_MIN  = 40.0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
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
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            async with session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Webhook error:", e)
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            async with session.post(url, data=payload, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Telegram error:", e)

# ----------------- Indicadores -----------------
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    out = []
    if not seq: return out
    alpha = 2.0 / (span + 1.0)
    e = seq[0]; out.append(e)
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def rolling_max(seq, n):
    out, q = [], deque()
    for x in seq:
        q.append(x)
        if len(q) > n: q.popleft()
        out.append(max(q))
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

def macd(close, fast=12, slow=26, signal=9):
    if not close: return [], [], []
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = ema(dif, signal) if dif else []
    hist = [(d - e) * 2 for d, e in zip(dif, dea)] if dea else []
    return dif, dea, hist

def adx(high, low, close, period=14):
    n = len(close)
    if n < period + 2: return [0.0] * n
    tr  = [0.0] * n; pdm = [0.0] * n; ndm = [0.0] * n
    for i in range(1, n):
        up = high[i] - high[i-1]; down = low[i-1] - low[i]
        pdm[i] = up if (up > down and up > 0) else 0.0
        ndm[i] = down if (down > up and down > 0) else 0.0
        tr[i]  = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
    atr=[0.0]*n; pdi=[0.0]*n; ndi=[0.0]*n; dx=[0.0]*n
    atr[period] = sum(tr[1:period+1]); spdm = sum(pdm[1:period+1]); sndm = sum(ndm[1:period+1])
    for i in range(period+1, n):
        atr[i]  = atr[i-1] - (atr[i-1] / period) + tr[i]
        spdm    = spdm - (spdm / period) + pdm[i]
        sndm    = sndm - (sndm / period) + ndm[i]
        pdi[i]  = 100.0 * (spdm / (atr[i] + 1e-12))
        ndi[i]  = 100.0 * (sndm / (atr[i] + 1e-12))
        dx[i]   = 100.0 * abs(pdi[i] - ndi[i]) / (pdi[i] + ndi[i] + 1e-12)
    return ema(dx, period)

def obv(close, volume):
    out = [0.0]
    for i in range(1, len(close)):
        if close[i] > close[i-1]: out.append(out[-1] + volume[i])
        elif close[i] < close[i-1]: out.append(out[-1] - volume[i])
        else: out.append(out[-1])
    return out

def compute_indicators(open_, high, low, close, volume):
    ema9   = ema(close, EMA_FAST)
    ma20   = sma(close, MA_SLOW)
    ma50   = sma(close, MA_MED)
    ma200  = sma(close, MA_LONG)
    rsi14  = rsi_wilder(close, RSI_LEN)
    vol_ma = sma(volume, VOL_MA)
    vol_sd = rolling_std(volume, VOL_MA)
    hh20   = rolling_max(high, HH_WIN)
    res20  = rolling_max(high, 20)
    res50  = rolling_max(high, 50)
    dif, dea, hist = macd(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    adx_vals = adx(high, low, close, ADX_LEN)
    obv_vals = obv(close, volume)
    bb_std  = rolling_std(close, 20)
    bb_mid  = ma20
    bb_up   = [bb_mid[i] + 2*bb_std[i] for i in range(len(close))]
    bb_low  = [bb_mid[i] - 2*bb_std[i] for i in range(len(close))]
    return (ema9, ma20, ma50, ma200, rsi14, vol_ma, vol_sd, hh20, res20, res50,
            dif, dea, hist, adx_vals, obv_vals, bb_mid, bb_up, bb_low)

# ----------------- Mensagens / Emojis -----------------
def kind_emoji(kind: str) -> str:
    return {
        "TENDÊNCIA_INICIANDO":"🌅","TENDÊNCIA_REAL":"💎","TENDÊNCIA_CONFIRMADA":"💎",
        "REVERSÃO":"🔄","REVERSÃO_FORTE":"🧲","REVERSÃO_BB_ALTA":"🧲",
        "EXAUSTAO_BB_ALTA":"⚠️","EXAUSTAO_BB_BAIXA":"⚠️",
        "CONTINUAÇÃO_ALTA":"🔁","REBOTE_TECNICO":"🎯",
        "PUMP":"🚀","BREAKOUT":"💥","TENDÊNCIA":"📈",
        "RESISTÊNCIA_CURTA":"🧱","RESISTÊNCIA_LONGA":"🏗️",
        "SUPORTE_200":"🟨","ROMPIMENTO_200":"🟩",
        "QUEDA_FORTE":"🧊",
        "PERDA_FORÇA":"⚠️","SAÍDA_TÉCNICA":"🔻","SAÍDA_CONFIRMADA":"❌",
        "MERCADO_ESTICADO":"⛔️"
    }.get(kind, "📌")

def arrow_for_kind(kind: str) -> str:
    ups = {"TENDÊNCIA_INICIANDO","TENDÊNCIA_REAL","TENDÊNCIA_CONFIRMADA",
           "BREAKOUT","PUMP","CONTINUAÇÃO_ALTA","REBOTE_TECNICO","ROMPIMENTO_200","REVERSÃO_BB_ALTA","TENDÊNCIA"}
    downs = {"SAÍDA_TÉCNICA","SAÍDA_CONFIRMADA","PERDA_FORÇA","EXAUSTAO_BB_ALTA","EXAUSTAO_BB_BAIXA","MERCADO_ESTICADO","QUEDA_FORTE"}
    if kind in ups: return "⬆️"
    if kind in downs: return "⬇️"
    return "➜"

def build_msg(symbol: str, kind: str, price: float, bullets: str) -> str:
    star = "⭐"
    sym_pretty = fmt_symbol(symbol)
    arrow = arrow_for_kind(kind)
    title = f"{star} {sym_pretty} {arrow} — {kind.replace('_',' ')}"
    ts = ts_brazil_now()
    return (
        f"{title}\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"🧠 {bullets}\n"
        f"⏰ {ts}\n"
        f"{binance_links(symbol)}"
    )

def pick_priority_kind(signals):
    prio = {
        "TENDÊNCIA_INICIANDO":0,"TENDÊNCIA_REAL":1,"TENDÊNCIA_CONFIRMADA":1,"PUMP":2,"BREAKOUT":3,
        "REVERSÃO_BB_ALTA":4,"REVERSÃO_FORTE":5,"REBOTE_TECNICO":6,"REVERSÃO":7,"CONTINUAÇÃO_ALTA":8,
        "ROMPIMENTO_200":9,"RESISTÊNCIA_CURTA":10,"RESISTÊNCIA_LONGA":11,"SUPORTE_200":12,"QUEDA_FORTE":13,
        "MERCADO_ESTICADO":14,"PERDA_FORÇA":15,"EXAUSTAO_BB_ALTA":16,"EXAUSTAO_BB_BAIXA":16,
        "SAÍDA_TÉCNICA":17,"SAÍDA_CONFIRMADA":18,"TENDÊNCIA":19
    }
    return sorted(signals, key=lambda x: prio.get(x[0], 99))[0][0] if signals else "SINAL"

# ----------------- Regras -----------------
def check_signals(symbol, open_, close, high, low, volume,
                  ema9, ma20, ma50, ma200, rsi14, vol_ma, vol_sd, hh20, res20, res50,
                  dif, dea, hist, adx_vals, obv_vals, bb_mid, bb_up, bb_low,
                  ema9_15=None, ma20_15=None, drop24h_pct=None):
    n = len(close)
    if n < 60: return []
    last, prev = n - 1, n - 2
    out = []

    price_above_200 = close[last] > ma200[last]
    cross_9_20_up   = (ema9[last-1] <= ma20[last-1] and ema9[last] > ma20[last])
    cross_9_20_dn   = (ema9[last-1] >= ma20[last-1] and ema9[last] < ma20[last])
    macd_up         = (len(adx_vals)>0 and len(dea)>1 and dif[last] > dea[last] and dif[prev] <= dea[prev])
    macd_dn         = (len(adx_vals)>0 and len(dea)>1 and dif[last] < dea[last] and dif[prev] >= dea[prev])
    adx_val         = adx_vals[last] if len(adx_vals)>last else 0.0
    adx_ok          = adx_val >= 25.0
    adx_rising      = (len(adx_vals)>last and last>=2 and adx_vals[last] > adx_vals[last-1] > adx_vals[last-2])
    obv_up          = (len(obv_vals)>5 and obv_vals[last] > obv_vals[max(0,last-5)])
    near200         = (abs(close[last] - ma200[last]) / (ma200[last] + 1e-12) < 0.005) or (low[last] <= ma200[last] <= high[last])

    no_top_div   = not (close[last] > close[last-2] and rsi14[last] < rsi14[last-2])
    candle_verde = close[last] > open_[last]
    vol_inteligente = volume[last] >= (vol_ma[last] + vol_sd[last])

    conf15_ok = None
    if ema9_15 is not None and ma20_15 is not None and len(ema9_15) and len(ma20_15):
        conf15_ok = (ema9_15[-1] > ma20_15[-1])

    # ---------- Queda 24h / Rebote ----------
    if drop24h_pct is not None:
        if drop24h_pct <= DROP_PCT_TRIGGER:
            out.append(("QUEDA_FORTE", f"Queda {drop24h_pct:.1f}% nas 24h — monitorando possível reversão"))
        if (drop24h_pct <= DROP_PCT_TRIGGER
            and rsi14[prev] < 35 and rsi14[last] >= RSI_REBOUND_MIN
            and volume[last] > vol_ma[last] * 1.3
            and candle_verde):
            out.append(("REVERSÃO_FORTE", f"RSI {rsi14[prev]:.1f}→{rsi14[last]:.1f} | Vol>1.3×média | Candle de reversão"))

    # ---------- Bollinger: Reversão / Continuação / Esticado ----------
    if (close[prev] < bb_low[prev] and close[last] > bb_mid[last]
        and ema9[last] > ma20[last] and rsi14[last] >= 50):
        out.append(("REVERSÃO_BB_ALTA", f"Saída da BB inferior + fechamento acima da média | RSI {rsi14[last]:.1f}"))

    touched_ma20 = any(low[i] <= ma20[i] for i in range(max(0, last-2), last+1))
    touched_ema9 = any(low[i] <= ema9[i] for i in range(max(0, last-2), last+1))
    if (price_above_200 and (touched_ma20 or touched_ema9) and close[last] > ema9[last]
        and volume[last] >= vol_ma[last] * 1.00
        and rsi14[last] >= 55
        and close[last] > bb_low[last] and close[last] < bb_up[last]*0.98):
        if (drop24h_pct is not None and drop24h_pct <= REBOTE_MODE_PCT):
            out.append(("REBOTE_TECNICO", f"Queda {drop24h_pct:.1f}%/24h | Toque na média + reação | RSI {rsi14[last]:.1f} | BB dentro"))
        else:
            out.append(("CONTINUAÇÃO_ALTA", f"Toque na média + reação | RSI {rsi14[last]:.1f} | Vol>=média | BB dentro"))

    if (close[last] >= bb_up[last] and rsi14[last] >= 70 and adx_ok):
        out.append(("MERCADO_ESTICADO", f"Acima da BB superior | RSI {rsi14[last]:.1f} | ADX {adx_val:.1f} — possível correção"))

    # ---------- Tendência ----------
    if cross_9_20_up and price_above_200 and rsi14[last] >= 50 and candle_verde and no_top_div:
        notas = []
        if conf15_ok is False: notas.append("Aguardando confirmação no 15m — início antecipado.")
        if not adx_ok:         notas.append("Aguardando confirmação do ADX — tendência em formação.")
        nota_txt = (" | " + " / ".join(notas)) if notas else ""
        out.append(("TENDÊNCIA_INICIANDO", f"EMA9>MA20 | RSI {rsi14[last]:.1f}{nota_txt}"))

    medias_alinhadas_5m = (ema9[last] > ma20[last] > ma50[last] > ma200[last])
    rsi_ok = 55 <= rsi14[last] <= 70
    vol_ok = volume[last] >= vol_ma[last] * 1.1
    confirmado = (conf15_ok is True) or adx_ok
    if price_above_200 and medias_alinhadas_5m and confirmado and adx_rising and macd_up and obv_up and rsi_ok and (vol_ok or vol_inteligente):
        label_conf = "15m" if (conf15_ok is True and not adx_ok) else ("ADX" if (adx_ok and not conf15_ok) else "15m + ADX")
        out.append(("TENDÊNCIA_REAL", f"Confirmação: {label_conf} | Médias 5m alinhadas | ADX {adx_val:.1f}↑ | MACD | OBV↑ | RSI {rsi14[last]:.1f}"))

    # ---------- Clássicos ----------
    if (price_above_200
        and volume[last] > (vol_ma[last] * 2.0)
        and rsi14[last] > 60
        and ema9[last] > ma20[last]
        and close[last] > close[prev] * 1.01):
        out.append(("PUMP", f"Vol {volume[last]:.0f} > 2× média | RSI {rsi14[last]:.1f} | EMA9>MA20 | >MA200"))

    if (price_above_200
        and close[last] > hh20[last]
        and volume[last] > vol_ma[last] * 1.2
        and rsi14[last] > 55
        and ema9[last] > ma20[last]):
        out.append(("BREAKOUT", f"Rompimento HH20 | Vol>média | RSI {rsi14[last]:.1f} | >MA200"))

    if (price_above_200
        and ema9[last-2] > ma20[last-2] and ema9[last-1] > ma20[last-1] and ema9[last] > ma20[last]
        and ma20[last] > ma50[last]
        and 55 <= rsi14[last] <= 70):
        out.append(("TENDÊNCIA", f"EMA9>MA20>MA50 | RSI {rsi14[last]:.1f} | >MA200"))

    prev_rsi = rsi14[last-3] if last >= 3 else 50.0
    if (price_above_200
        and prev_rsi < 45 and rsi14[last] > 50
        and ema9[last-1] <= ma20[last-1] and ema9[last] > ma20[last]
        and close[last] > close[prev]
        and volume[last] >= vol_ma[last] * 1.10):
        out.append(("REVERSÃO", f"RSI {prev_rsi:.1f}→{rsi14[last]:.1f} | EMA9 cruzou MA20 | Vol>média | >MA200"))

    # Resistências & MA200
    if close[last] >= res20[last]:
        out.append(("RESISTÊNCIA_CURTA", f"Fechamento acima da resistência 20 ({res20[last]:.4f})"))
    if close[last] >= res50[last]:
        out.append(("RESISTÊNCIA_LONGA", f"Fechamento acima da resistência 50 ({res50[last]:.4f})"))

    if near200:
        out.append(("SUPORTE_200", f"Preço encostando na MA200 ({ma200[last]:.4f})"))
    if close[prev] < ma200[prev] and close[last] > ma200[last] and volume[last] > vol_ma[last] * 1.1:
        out.append(("ROMPIMENTO_200", "Cruzou MA200 ↑ | Vol>média"))

    # Saídas / fraqueza
    if rsi14[prev] > 55 and rsi14[last] < 50 and ema9[last] >= ma20[last]:
        out.append(("PERDA_FORÇA", f"RSI {rsi14[prev]:.1f}→{rsi14[last]:.1f} — momentum caindo"))
    if cross_9_20_dn:
        out.append(("SAÍDA_TÉCNICA", "EMA9 cruzou MA20 ↓ — tendência enfraquecendo"))
    if macd_dn:
        out.append(("SAÍDA_CONFIRMADA", "MACD DIF cruzou DEA ↓ — reversão provável"))

    return out

# ----------------- Binance -----------------
async def get_klines(session, symbol: str, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    now_ms = int(time.time() * 1000)
    if data and now_ms < int(data[-1][6]):  # descarta vela ainda aberta
        data = data[:-1]
    open_, high, low, close, volume = [], [], [], [], []
    for k in data:
        open_.append(float(k[1])); high.append(float(k[2])); low.append(float(k[3]))
        close.append(float(k[4])); volume.append(float(k[5]))
    return open_, high, low, close, volume

async def get_24h(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=400):
    usdt = []
    for t in tickers:
        s = t.get("symbol","")
        if not s.endswith("USDT"): 
            continue
        if any(x in s for x in ("UP","DOWN","BULL","BEAR")):
            continue
        pct = float(t.get("priceChangePercent","0") or 0.0)
        qv  = float(t.get("quoteVolume","0") or 0.0)
        if abs(pct) >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Anti-spam -----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)
    def allowed(self, symbol: str) -> bool:
        return time.time() - self.cooldown[symbol] >= COOLDOWN_SEC
    def mark(self, symbol: str):
        self.cooldown[symbol] = time.time()

# ----------------- Worker -----------------
async def candle_worker(session, symbol: str, monitor: Monitor, drop_map):
    try:
        open_, high, low, close, volume = await get_klines(session, symbol, interval=INTERVAL, limit=200)
        (ema9, ma20, ma50, ma200, rsi14, vol_ma, vol_sd, hh20, res20, res50,
         dif, dea, hist, adx_vals, obv_vals, bb_mid, bb_up, bb_low) = compute_indicators(open_, high, low, close, volume)

        open15, high15, low15, close15, vol15 = await get_klines(session, symbol, interval=CONFIRM_INTERVAL, limit=200)
        ema9_15 = ema(close15, EMA_FAST) if close15 else []
        ma20_15 = sma(close15, MA_SLOW) if close15 else []

        drop24 = drop_map.get(symbol)
        signals = check_signals(symbol, open_, close, high, low, volume,
                                ema9, ma20, ma50, ma200, rsi14, vol_ma, vol_sd, hh20, res20, res50,
                                dif, dea, hist, adx_vals, obv_vals, bb_mid, bb_up, bb_low,
                                ema9_15=ema9_15, ma20_15=ma20_15, drop24h_pct=drop24)

        if signals and monitor.allowed(symbol):
            last_price = close[-1]
            first_kind = pick_priority_kind(signals)
            bullets = " | ".join([f"{kind_emoji(k)} {k}: {desc}" for k, desc in signals])
            txt = build_msg(symbol, first_kind, last_price, bullets)
            await send_alert(session, txt)
            monitor.mark(symbol)
    except Exception as e:
        print("candle_worker error", symbol, e)

# ----------------- Main loop -----------------
async def main():
    monitor = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        drop_map = {}
        for t in tickers:
            s = t.get("symbol","")
            if s in watchlist:
                try: drop_map[s] = float(t.get("priceChangePercent","0") or 0.0)
                except: drop_map[s] = None

        await send_alert(session, f"<b>💻 v10.3 PRO Bollinger PLUS</b> — {len(watchlist)} pares (5m + 15m/ADX + BB + 🇧🇷) | {ts_brazil_now()}")
        print(f"💻 v10.3 PRO Bollinger PLUS — analisando {len(watchlist)} pares.")

        while True:
            await asyncio.gather(*[candle_worker(session, s, monitor, drop_map) for s in watchlist])
            await asyncio.sleep(120)
            try:
                tickers = await get_24h(session)
                watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
                drop_map = {}
                for t in tickers:
                    s = t.get("symbol","")
                    if s in watchlist:
                        try: drop_map[s] = float(t.get("priceChangePercent","0") or 0.0)
                        except: drop_map[s] = None
            except Exception as e:
                print("Erro ao atualizar shortlist:", e)

# ----------------- Exec paralela Flask + bot -----------------
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
        return "✅ Binance Alerts Bot — v10.3 PRO Bollinger PLUS (BB + 🇧🇷 + QUEDA FORTE) ativo!"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
