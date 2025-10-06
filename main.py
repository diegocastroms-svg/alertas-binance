import os, asyncio, time
from urllib.parse import urlencode
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
import pandas as pd
import numpy as np

BINANCE_HTTP = "https://api.binance.com"

# ========== ENV ==========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# -------- Configurações principais --------
INTERVAL      = "5m"           
SHORTLIST_N   = 50              
COOLDOWN_SEC  = 15 * 60         
TOP_FILTERS   = dict(min_quote_vol=300000.0, min_pct=1.0)  

# Indicadores
EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50

# -------- Utilidades --------
def fmt_symbol(symbol: str) -> str:
    if symbol.endswith("USDT"):
        return symbol[:-4] + "/USDT"
    return symbol

def binance_pair_link(symbol: str) -> str:
    # 🔗 Versão original que funcionava antes das 13h30
    base = symbol.replace("USDT", "_USDT")
    return f"https://www.binance.com/pt/trade/{base}"

async def send_alert(session: aiohttp.ClientSession, text: str):
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            async with session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Webhook error:", e)
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
            async with session.post(tg_url, data=payload, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Telegram error:", e)

# -------- Indicadores --------
def rsi(series: pd.Series, period: int = 14):
    delta = series.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=series.index).rolling(period).mean()
    roll_down = pd.Series(down, index=series.index).rolling(period).mean()
    rs = roll_up / (roll_down + 1e-12)
    return 100 - (100 / (1 + rs))

def compute_indicators(df: pd.DataFrame):
    df["ema9"]   = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ma20"]   = df["close"].rolling(MA_SLOW).mean()
    df["ma50"]   = df["close"].rolling(MA_MED).mean()
    df["rsi14"]  = rsi(df["close"], 14)
    df["vol_ma9"]= df["volume"].rolling(9).mean()
    df["hh20"]   = df["high"].rolling(20).max()
    return df

# -------- Regras --------
def check_signals(df: pd.DataFrame):
    if len(df) < 60:
        return []

    last, prev = df.iloc[-1], df.iloc[-2]
    out = []

    # 🚀 PUMP
    if (last["volume"] > (last["vol_ma9"] * 2.0)) and (last["rsi14"] > 60) and (last["ema9"] > last["ma20"]) and (last["close"] > prev["close"] * 1.01):
        out.append(("PUMP", f"Vol {last['volume']:.0f} > 2x média | RSI {last['rsi14']:.1f} | EMA9>MA20"))

    # 💥 Breakout
    if (last["close"] > last["hh20"]) and (last["volume"] > last["vol_ma9"] * 1.2) and (last["rsi14"] > 55) and (last["ema9"] > last["ma20"]):
        out.append(("BREAKOUT", f"Fechou acima da máxima 20 | Vol>média | RSI {last['rsi14']:.1f}"))

    # 📈 Tendência Sustentada
    if (df["ema9"].iloc[-3:] > df["ma20"].iloc[-3:]).all() and (df["ma20"].iloc[-1] > df["ma50"].iloc[-1]) and (55 <= last["rsi14"] <= 70):
        out.append(("TENDÊNCIA", f"EMA9>MA20>MA50 | RSI {last['rsi14']:.1f}"))

    # 🔄 Reversão de Fundo
    prev_rsi = df["rsi14"].iloc[-3]
    if (prev_rsi < 45) and (last["rsi14"] > 50) and (df["ema9"].iloc[-2] <= df["ma20"].iloc[-2]) and (df["ema9"].iloc[-1] > df["ma20"].iloc[-1]) and (last["volume"] >= last["vol_ma9"] * 1.10):
        out.append(("REVERSÃO", f"RSI {prev_rsi:.1f}→{last['rsi14']:.1f} | EMA9 cruzou MA20 | Vol>média"))

    # ♻️ Reteste
    if (df["ma20"].iloc[-1] > df["ma50"].iloc[-1]) and ((df["low"].iloc[-3:] <= df["ma20"].iloc[-3:]).any() or (df["low"].iloc[-3:] <= df["ema9"].iloc[-3:]).any()) and (last["close"] > last["ema9"]) and (last["rsi14"] > 55):
        out.append(("RETESTE", f"Retomada após toque na média | RSI {last['rsi14']:.1f}"))

    return out

# -------- Coleta --------
async def get_klines(session, symbol: str, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=10) as r:
        r.raise_for_status()
        data = await r.json()
    cols = ["open_time","open","high","low","close","volume","close_time","qav","num_trades","taker_base","taker_quote","ignore"]
    df = pd.DataFrame(data, columns=cols)
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df[["open","high","low","close","volume"]]

async def get_24h(session):
    url = "https://api.binance.me/api/v3/ticker/24hr"
    async with session.get(url, timeout=10) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=50):
    usdt = []
    for t in tickers:
        s = t.get("symbol","")
        if not s.endswith("USDT"): 
            continue
        if any(x in s for x in ("UP","DOWN","BULL","BEAR")): 
            continue
        pct = abs(float(t.get("priceChangePercent","0") or 0.0))
        qv  = float(t.get("quoteVolume","0") or 0.0)
        if pct >= TOP_FILTERS["min_pct"] and qv >= TOP_FILTERS["min_quote_vol"]:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# -------- Controle --------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)
    def allowed(self, symbol):
        return time.time() - self.cooldown[symbol] >= COOLDOWN_SEC
    def mark(self, symbol):
        self.cooldown[symbol] = time.time()

def kind_emoji(kind):
    return {"PUMP":"🚀","BREAKOUT":"💥","TENDÊNCIA":"📈","REVERSÃO":"🔄","RETESTE":"♻️"}.get(kind,"📌")

def pick_priority_kind(signals):
    prio = {"PUMP":0,"BREAKOUT":1,"REVERSÃO":2,"RETESTE":3,"TENDÊNCIA":4}
    return sorted(signals, key=lambda x: prio.get(x[0], 9))[0][0] if signals else "SINAL"

async def candle_worker(session, symbol, monitor):
    try:
        df = await get_klines(session, symbol, interval=INTERVAL, limit=200)
        df = compute_indicators(df)
        signals = check_signals(df)
        if signals and monitor.allowed(symbol):
            last_price = df["close"].iloc[-1]
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            first_kind = pick_priority_kind(signals)
            emoji = kind_emoji(first_kind)
            sym_pretty = fmt_symbol(symbol)
            bullets = "\n".join([f"• {kind_emoji(k)} *{k}*: {desc}" for k, desc in signals])
            txt = (
                f"{emoji} *{sym_pretty} — {first_kind} DETECTADO!*\n"
                f"💰 Preço: `{last_price:.6f}`\n"
                f"🧠 Sinal técnico:\n{bullets}\n\n"
                f"⏰ {ts}\n"
                f"🔗 [Abrir na Binance]({binance_pair_link(symbol)})"
            )
            await send_alert(session, txt)
            monitor.mark(symbol)
    except Exception as e:
        print("candle_worker error", symbol, e)

async def main():
    monitor = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        await send_alert(session, f"✅ Monitor online [{INTERVAL}] — acompanhando {len(watchlist)} pares | {ts}")
        print("Shortlist inicial:", watchlist[:10], "… total:", len(watchlist))
        while True:
            await asyncio.gather(*[candle_worker(session, s, monitor) for s in watchlist])
            await asyncio.sleep(180)
            try:
                tickers = await get_24h(session)
                watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
            except Exception as e:
                print("Erro ao atualizar shortlist:", e)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
