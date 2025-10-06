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
# (Opcional) se preferir repassar via seu Flask no Render:
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")  # ex: https://seuapp.onrender.com/webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# -------- Configurações principais --------
INTERVAL      = "5m"            # 1m/3m/5m/15m
SHORTLIST_N   = 40              # quantos pares “quentes” analisar
COOLDOWN_SEC  = 15 * 60         # evita spam: 1 alerta / 15min por símbolo
TOP_FILTERS   = dict(min_quote_vol=300000.0, min_pct=1.0)  # filtro inicial

# Indicadores
EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50

# -------- Utilidades --------
def fmt_symbol(symbol: str) -> str:
    # Ex.: "CELOUSDT" -> "CELO/USDT"
    if symbol.endswith("USDT"):
        return symbol[:-4] + "/USDT"
    return symbol

def binance_pair_link(symbol: str) -> str:
    # Ex.: "CELOUSDT" -> "CELO_USDT" no link
    base = symbol.replace("USDT", "_USDT")
    return f"https://www.binance.com/pt/trade/{base}"

async def send_alert(session: aiohttp.ClientSession, text: str):
    # (1) Opcional: seu webhook Flask
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            async with session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Webhook error:", e)
    # (2) Telegram direto
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

# -------- Regras dos 5 modelos de alta --------
def check_signals(df: pd.DataFrame):
    """Retorna lista de (tipo, explicação) para o último candle."""
    if len(df) < 60: 
        return []

    last, prev = df.iloc[-1], df.iloc[-2]
    out = []

    # 1) 🚀 PUMP Explosivo
    pump = (
        (last["volume"] > (last["vol_ma9"] * 2.0)) and
        (last["rsi14"] > 60) and
        (last["ema9"] > last["ma20"]) and
        (last["close"] > prev["close"] * 1.01)  # +1% no candle
    )
    if pump:
        out.append(("PUMP", f"Vol {last['volume']:.0f} > 2x média | RSI {last['rsi14']:.1f} | EMA9>MA20"))

    # 2) 💥 Rompimento (Breakout)
    breakout = (
        (last["close"] > last["hh20"]) and
        (last["volume"] > last["vol_ma9"] * 1.2) and
        (last["rsi14"] > 55) and
        (last["ema9"] > last["ma20"])
    )
    if breakout:
        out.append(("BREAKOUT", f"Fechou acima da máxima 20 | Vol>média | RSI {last['rsi14']:.1f}"))

    # 3) 📈 Tendência Sustentada
    trend = (
        (df["ema9"].iloc[-3:] > df["ma20"].iloc[-3:]).all() and
        (df["ma20"].iloc[-1] > df["ma50"].iloc[-1]) and
        (55 <= last["rsi14"] <= 70)
    )
    if trend:
        out.append(("TENDÊNCIA", f"EMA9>MA20>MA50 | RSI {last['rsi14']:.1f} (zona saudável)"))

    # 4) 🔄 Reversão de Fundo
    prev_rsi = df["rsi14"].iloc[-3]
    rev = (
        (prev_rsi < 45) and (last["rsi14"] > 50) and              # RSI saindo do fundo
        (df["ema9"].iloc[-2] <= df["ma20"].iloc[-2]) and          # cruzamento recente
        (df["ema9"].iloc[-1]  > df["ma20"].iloc[-1])  and
        (last["close"] > prev["close"]) and
        (last["volume"] >= last["vol_ma9"] * 1.10)
    )
    if rev:
        out.append(("REVERSÃO", f"RSI {prev_rsi:.1f}→{last['rsi14']:.1f} | EMA9 cruzou MA20 | Vol>média"))

    # 5) ♻️ Reteste/Pullback
    touched_ma20 = (df["low"].iloc[-3:] <= df["ma20"].iloc[-3:]).any()
    touched_ema9 = (df["low"].iloc[-3:] <= df["ema9"].iloc[-3:]).any()
    reteste = (
        (df["ma20"].iloc[-1] > df["ma50"].iloc[-1]) and           # tendência de alta vigente
        (touched_ma20 or touched_ema9) and                         # toque recente na média
        (last["close"] > last["ema9"]) and
        (last["rsi14"] > 55) and
        (last["volume"] >= last["vol_ma9"] * 1.00)
    )
    if reteste:
        out.append(("RETESTE", f"Retomada após toque na média | RSI {last['rsi14']:.1f} | Vol>=média"))

    return out

# -------- Coleta de dados --------
async def get_klines(session, symbol: str, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=10) as r:
        r.raise_for_status()
        data = await r.json()
    cols = ["open_time","open","high","low","close","volume","close_time","qav","num_trades","taker_base","taker_quote","ignore"]
    df = pd.DataFrame(data, columns=cols)
    df["open"]   = df["open"].astype(float)
    df["high"]   = df["high"].astype(float)
    df["low"]    = df["low"].astype(float)
    df["close"]  = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df[["open","high","low","close","volume","high","low"]]

async def get_24h(session):
    url = f"https://api.binance.me/api/v3/ticker/24hr"
    async with session.get(url, timeout=10) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=40):
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

# -------- Controle de spam --------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)  # symbol -> last_ts

    def allowed(self, symbol: str) -> bool:
        return time.time() - self.cooldown[symbol] >= COOLDOWN_SEC

    def mark(self, symbol: str):
        self.cooldown[symbol] = time.time()

def kind_emoji(kind: str) -> str:
    return {
        "PUMP": "🚀",
        "BREAKOUT": "💥",
        "TENDÊNCIA": "📈",
        "REVERSÃO": "🔄",
        "RETESTE": "♻️"
    }.get(kind, "📌")

def pick_priority_kind(signals):
    # Prioridade: PUMP > BREAKOUT > REVERSÃO > RETESTE > TENDÊNCIA
    prio = {"PUMP":0, "BREAKOUT":1, "REVERSÃO":2, "RETESTE":3, "TENDÊNCIA":4}
    return sorted(signals, key=lambda x: prio.get(x[0], 9))[0][0] if signals else "SINAL"

async def candle_worker(session, symbol: str, monitor: Monitor):
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
        # Ping de inicialização (confirma no Telegram que subiu)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        await send_alert(session, f"✅ Monitor online [{INTERVAL}] — acompanhando {len(watchlist)} pares | {ts}")

        print("Shortlist inicial:", watchlist[:10], "… total:", len(watchlist))
        while True:
            # roda análise em todos da shortlist
            tasks = [candle_worker(session, s, monitor) for s in watchlist]
            await asyncio.gather(*tasks)

            # reconstroi shortlist a cada ~3 min
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

