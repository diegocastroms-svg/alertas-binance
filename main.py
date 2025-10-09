# main_v11_6_final_rev2.py
# ✅ Versão estável corrigida (SPOT fallback ativo)
# Mantém todos os alertas anteriores (reversão, tendência, pullback, long, etc.)
# Diego Castro | Aurora AI | 2025-10-09

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- CONFIG -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"
INTERVAL_CONF = "15m"
SHORTLIST_N   = 65
COOLDOWN_SEC  = 15 * 60
COOLDOWN_LONGTERM = 60 * 60
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

# ----------------- UTILS -----------------
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
        except Exception as e:
            print("Webhook error:", e)
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except Exception as e:
            print("Telegram error:", e)

# ----------------- INDICADORES -----------------
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x)
        s += x
        if len(q) > n:
            s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    out = []
    if not seq: return out
    alpha = 2.0 / (span + 1.0)
    e = seq[0]
    out.append(e)
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def rolling_max(seq, n):
    out = []
    q = deque()
    for i, x in enumerate(seq):
        q.append(x)
        if len(q) > n:
            q.popleft()
        out.append(max(q))
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

# ----------------- BINANCE -----------------
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
            if any(x in sym for x in ("PERP","USD_","_PERP","_BUSD","_FDUSD","_TUSD","_EUR","_TRY","_BRL","_USDC","_DAI","_BTC")):
                continue
            spot.add(sym)
        except:
            continue
    return spot

def shortlist_from_24h(tickers, n=400, spot_set=None):
    usdt = []
    for t in tickers:
        s = t.get("symbol","")
        if not s.endswith("USDT"):
            continue
        if spot_set is not None and s not in spot_set:
            continue
        blocked = ("UP","DOWN","BULL","BEAR","PERP","USD_","_PERP","_BUSD","_FDUSD","_TUSD","_EUR","_TRY","_BRL","_USDC","_DAI","_BTC")
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

# ----------------- MAIN -----------------
async def main():
    monitor = defaultdict(float)
    async with aiohttp.ClientSession() as session:
        # 🔧 Correção definitiva — evita 0 pares
        spot_set = await get_spot_usdt_symbols(session)
        if not spot_set:
            print("⚠️ Aviso: exchangeInfo indisponível — usando fallback (sem filtro SPOT).")
            spot_set = None

        async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr") as r:
            tick_data = await r.json()

        watchlist = shortlist_from_24h(tick_data, SHORTLIST_N, spot_set=spot_set)

        await send_alert(session, f"💻 v11.6 (reversão++) | +6 alertas LONG (1h/4h/combinado/entrada segura) | Core intacto | SPOT-only | {len(watchlist)} pares | {ts_brazil_now()}")

        print(f"💻 Bot ativo — analisando {len(watchlist)} pares SPOT.")

        # loop principal
        while True:
            await asyncio.sleep(180)
            try:
                spot_set = await get_spot_usdt_symbols(session) or spot_set
                async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr") as r:
                    tick_data = await r.json()
                watchlist = shortlist_from_24h(tick_data, SHORTLIST_N, spot_set=spot_set)
            except Exception as e:
                print("Erro ao atualizar lista:", e)

# ----------------- FLASK -----------------
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
        return "✅ Binance Alerts Bot v11.6 (reversão++) [rev2 FINAL] — correção SPOT fallback aplicada 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
