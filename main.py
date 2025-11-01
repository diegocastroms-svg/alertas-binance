# main.py — V7.0 OURO CONFLUÊNCIA FORTE (MACD VERDE + EXPANSÃO)
# 3m: EMA9 > EMA20 + RSI 40-80 + Volume crescente
# 5m/15m/30m/1h: MACD hist > 0 e hist[-1] > hist[-2] (expansão)
# Top 50 USDT | Liquidez > 20M | >1000 trades
# Cooldown 10 min | Alerta com alvos 1:2.5 e 1:5

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 10 * 60
TOP_N = 50
REQ_TIMEOUT = 10
UPDATE_TOP_INTERVAL = 10  # 5 min
VERSION = "V7.0 - OURO CONFLUÊNCIA FORTE"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | MACD VERDE + EXPANSÃO | 3m EMA9>20 + RSI 40-80", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " BR"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        print(f"[TG] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=REQ_TIMEOUT)
    except Exception as e:
        print(f"[TG ERRO] {e}")

def ema(seq, period):
    if len(seq) < period: return []
    alpha = 2 / (period + 1)
    ema_val = seq[0]
    out = [ema_val]
    for price in seq[1:]:
        ema_val = alpha * price + (1 - alpha) * ema_val
        out.append(ema_val)
    return out

def macd_expansao(hist):
    if len(hist) < 2: return False
    return hist[-1] > 0 and hist[-1] > hist[-2]  # verde e crescendo

def calc_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d for d in deltas[:period] if d > 0]
    losses = [-d for d in deltas[:period] if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 1e-12
    rs = avg_gain / avg_loss
    rsi = [100 - 100/(1+rs)] if rs > 0 else [100]
    for i in range(period, len(deltas)):
        delta = deltas[i]
        gain = delta if delta > 0 else 0
        loss = -delta if delta < 0 else 0
        avg_gain = (avg_gain * (period-1) + gain) / period
        avg_loss = (avg_loss * (period-1) + loss) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi.append(100 - 100/(1+rs))
    return rsi[-1] if rsi else 50.0

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=100):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            if r.status != 200: return []
            return await r.json()
    except:
        return []

async def get_top_usdt_symbols(session):
    try:
        url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            if r.status != 200: return []
            data = await r.json()
        blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","EUR","BRL","PERP","TEST","USDE")
        pares = []
        for d in data:
            s = d["symbol"]
            if not s.endswith("USDT"): continue
            if any(x in s for x in blocked): continue
            qv = float(d.get("quoteVolume", 0))
            trades = int(d.get("count", 0))
            if qv < 20_000_000 or trades < 1000: continue
            pares.append((s, qv))
        pares.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in pares[:TOP_N]]
    except Exception as e:
        print(f"[ERRO TOP] {e}")
        return []

# ---------------- COOLDOWN ----------------
cooldowns = {}
def can_alert(symbol):
    now = time.time()
    last = cooldowns.get(symbol, 0)
    if now - last >= COOLDOWN_SEC:
        cooldowns[symbol] = now
        # limpa antigos
        cutoff = now - 3600
        to_remove = [k for k, v in cooldowns.items() if v < cutoff]
        for k in to_remove: del cooldowns[k]
        return True
    return False

# ---------------- SCAN ----------------
async def scan_symbol(session, symbol):
    try:
        # Pegar dados (apenas candles fechados)
        k3  = await get_klines(session, symbol, "3m", 50)
        k5  = await get_klines(session, symbol, "5m", 50)
        k15 = await get_klines(session, symbol, "15m", 50)
        k30 = await get_klines(session, symbol, "30m", 50)
        k1h = await get_klines(session, symbol, "1h", 50)

        if not all([k3, k5, k15, k30, k1h]) or len(k3) < 30: return

        # Preços de fechamento (fechados: até -2)
        close3  = [float(k[4]) for k in k3[:-1]]
        close5  = [float(k[4]) for k in k5[:-1]]
        close15 = [float(k[4]) for k in k15[:-1]]
        close30 = [float(k[4]) for k in k30[:-1]]
        close1h = [float(k[4]) for k in k1h[:-1]]

        volume3 = [float(k[5]) for k in k3[:-1]]

        # --- 3m: EMA9 > EMA20 + RSI + Volume ---
        ema9  = ema(close3, 9)
        ema20 = ema(close3, 20)
        if len(ema9) == 0 or len(ema20) == 0: return
        ema_ok = ema9[-1] > ema20[-1]

        rsi3 = calc_rsi(close3[-30:])
        rsi_ok = 40 <= rsi3 <= 80

        vol_ok = volume3[-1] > volume3[-2] > volume3[-3]  # 3 velas crescentes

        # --- MACD com expansão ---
        macd5  = macd(close5)
        macd15 = macd(close15)
        macd30 = macd(close30)
        macd1h = macd(close1h)

        hist5  = macd5["hist"]
        hist15 = macd15["hist"]
        hist30 = macd30["hist"]
        hist1h = macd1h["hist"]

        macd_ok = all([
            len(h) >= 2 and macd_expansao(h[-2:])  # usa últimos 2 fechados
            for h in [hist5, hist15, hist30, hist1h]
        ])

        # --- CONDIÇÃO FINAL ---
        if ema_ok and rsi_ok and vol_ok and macd_ok and can_alert(symbol):
            preco = close5[-1]  # último fechado
            preco_ant = close5[-2]
            var_5m = (preco - preco_ant) / preco_ant * 100

            # Stop: mínima das últimas 2 velas 5m ou EMA21
            low5 = [float(k[3]) for k in k5[-3:-1]]
            ema21 = ema(close5, 21)
            ema21_val = ema21[-1] if ema21 else preco
            stop = min(min(low5), ema21_val * 0.995)  # margem

            risco = max(preco - stop, preco * 0.001)  # min 0.1%
            alvo1 = preco + 2.5 * risco
            alvo2 = preco + 5.0 * risco

            msg = (
                f"<b>🚀 TENDÊNCIA CURTA FORTE</b>\n"
                f"<code>{symbol}</code>\n"
                f"RSI 3m: <b>{rsi3:.1f}</b> | MACD 5m/15m/30m/1h <b>VERDE + EXPANSÃO</b>\n"
                f"Preço: <b>{preco:.6f}</b> ({var_5m:+.2f}%)\n"
                f"Stop: <b>{stop:.6f}</b>\n"
                f"Alvo 1: <b>{alvo1:.6f}</b> (1:2.5)\n"
                f"Alvo 2: <b>{alvo2:.6f}</b> (1:5)\n"
                f"<i>{now_br()}</i>"
            )
            await tg(session, msg)
            print(f"[SINAL FORTE] {symbol} | RSI={rsi3:.1f} | Preço={preco:.6f}")

    except Exception as e:
        print(f"[ERRO SCAN] {symbol}: {e}")

def macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal: return {"hist": []}
    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line[-len(signal_line):], signal_line)]
    return {"hist": hist}

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        top_symbols = await get_top_usdt_symbols(session)
        if top_symbols:
            await tg(session, f"<b>{VERSION} ATIVO</b>\nConfluência FORTE + MACD em expansão\n{len(top_symbols)} pares monitorados\n{now_br()}")
            print(f"[{now_br()}] Iniciado com {len(top_symbols)} pares")

        cycle = 0
        while True:
            cycle += 1
            if cycle % UPDATE_TOP_INTERVAL == 1:
                top_symbols = await get_top_usdt_symbols(session)
                print(f"[{now_br()}] Top 50 atualizado: {len(top_symbols)} pares")

            print(f"[{now_br()}] Ciclo {cycle} - Varredura iniciada...")
            await asyncio.gather(*[scan_symbol(session, s) for s in top_symbols], return_exceptions=True)
            print(f"[{now_br()}] Varredura concluída. Próximo em 30s...")
            await asyncio.sleep(30)

def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main_loop())
    except KeyboardInterrupt:
        print("Bot parado.")
    except Exception as e:
        print(f"[FATAL] {e}")
        time.sleep(5)
        start_bot()

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
