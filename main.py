# main_breakout_v1_render_hibrido.py
# V5.8 – CRUZAMENTO 5M (FECHADO) + CONFLUÊNCIA DINÂMICA (3m/15m/30m/1h em tempo real)

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 15 * 60
TOP_N = 80
REQ_TIMEOUT = 8
VERSION = "V5.8 - CRUZAMENTO 5M FECHADO + CONFLUÊNCIA DINÂMICA"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | CRUZAMENTO 5m FECHADO + MACD dinâmico (3m/15m/30m/1h) | 50 pares", 200

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

def fmt_price(x: float) -> str:
    return f"{x:.8f}".rstrip("0").rstrip(".") or "0"

def ema(seq, span):
    if not seq: return []
    alpha = 2.0 / (span + 1.0)
    out = [seq[0]]
    e = seq[0]
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def calc_rsi(seq, period=14):
    if len(seq) < period + 1: return [50.0] * len(seq)
    gains, losses = [], []
    for i in range(1, len(seq)):
        diff = seq[i] - seq[i-1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    rsi = []
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / (avg_loss + 1e-12)
    rsi.append(100 - (100 / (1 + rs)))
    for i in range(period, len(seq) - 1):
        diff = seq[i] - seq[i-1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi.append(100 - (100 / (1 + rs)))
    return [50.0] * (len(seq) - len(rsi)) + rsi

def macd(seq, fast=12, slow=26, signal=9):
    if len(seq) < slow + signal + 1:
        n = len(seq)
        return {"macd": [0.0]*n, "signal": [0.0]*n, "hist": [0.0]*n}
    ema_fast = ema(seq, fast)
    ema_slow = ema(seq, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    m = len(macd_line)
    s = len(signal_line)
    if s < m:
        signal_line = [signal_line[0]]*(m - s) + signal_line
    hist = [m_ - s_ for m_, s_ in zip(macd_line, signal_line)]
    return {"macd": macd_line, "signal": signal_line, "hist": hist}

# ---------------- FUNÇÃO DE CRUZAMENTO (SÓ NO 5M FECHADO) ----------------
def cruzou_de_baixo(c, p9=9, p20=20):
    # 'c' deve ser a série FECHADA (sem o último candle em formação)
    if len(c) < p20 + 2:
        return False
    e9 = ema(c, p9)
    e20 = ema(c, p20)
    return e9[-2] <= e20[-2] and e9[-1] > e20[-1]

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=100):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
            return data if isinstance(data, list) and len(data) > 0 else []
    except:
        return []

async def get_top_usdt_symbols(session):
    try:
        url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
        blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","EUR","BRL","PERP","TEST","USDE")
        pares = []
        for d in data:
            s = d.get("symbol", "")
            if not s.endswith("USDT"): continue
            if any(x in s for x in blocked): continue
            qv = float(d.get("quoteVolume", 0) or 0)
            pares.append((s, qv))
        pares.sort(key=lambda x: x[1], reverse=True)
        return pares[:TOP_N]
    except:
        return []

# ---------------- COOLDOWNS ----------------
cooldowns = {}
def can_alert(symbol, tipo, cooldown_sec):
    now = time.time()
    key = f"{symbol}_{tipo}"
    last = cooldowns.get(key, 0)
    if now - last > cooldown_sec:
        cooldowns[key] = now
        return True
    return False

# ---------------- WORKER (5m FECHADO + DEMAIS DINÂMICOS) ----------------
async def scan_symbol(session, symbol, qv):
    try:
        k3  = await get_klines(session, symbol, "3m",  100)
        k5  = await get_klines(session, symbol, "5m",  100)
        k15 = await get_klines(session, symbol, "15m", 100)
        k30 = await get_klines(session, symbol, "30m", 100)
        k1h = await get_klines(session, symbol, "1h",  100)
        if not (len(k3) and len(k5) and len(k15) and len(k30) and len(k1h)): return

        c3  = [float(k[4]) for k in k3]
        c5  = [float(k[4]) for k in k5]
        c15 = [float(k[4]) for k in k15]
        c30 = [float(k[4]) for k in k30]
        c1h = [float(k[4]) for k in k1h]

        v5 = [float(k[5]) for k in k5]
        i3, i5, i15, i30, i1h = len(c3)-1, len(c5)-1, len(c15)-1, len(c30)-1, len(c1h)-1
        volmed5 = sum(v5[-10:])/10 if len(v5) >= 10 else (v5[-1] if v5 else 0.0)

        # === 5m FECHADO para cruzamento e MACD ===
        c5_closed = c5[:-1] if len(c5) > 1 else c5
        cruzou_5m = cruzou_de_baixo(c5_closed, 9, 20)

        macd5_closed = macd(c5_closed)
        h5c = macd5_closed["hist"]
        EPS = 1e-3  # limiar anti-ruído
        h5_green = (len(h5c) >= 1 and h5c[-1] > EPS)  # 5m precisa estar VERDE (fechado)

        # === MACD DINÂMICO para 3m/15m/30m/1h (candle atual) ===
        macd3   = macd(c3)
        macd15  = macd(c15)
        macd30  = macd(c30)
        macd1h  = macd(c1h)

        h3, h15, h30, h1h = macd3["hist"], macd15["hist"], macd30["hist"], macd1h["hist"]

        # 3m e 15m: só precisam estar VERDES (dinâmicos) acima do ruído
        h3_green  = (len(h3)  >= 1 and h3[-1]  > EPS)
        h15_green = (len(h15) >= 1 and h15[-1] > EPS)

        # 30m e 1h: VERDES e CRESCENTES (dinâmicos) com folga > EPS
        h30_ok = (len(h30) >= 2 and h30[-1] > EPS and (h30[-1] - h30[-2]) >= EPS/2)
        h1h_ok = (len(h1h) >= 2 and h1h[-1] > EPS and (h1h[-1] - h1h[-2]) >= EPS/2)

        hist_ok = (h5_green and h3_green and h15_green and h30_ok and h1h_ok)

        # === FILTROS DE SEGURANÇA (dinâmicos) ===
        rsi15 = calc_rsi(c15, 14)[-1] if len(c15) else 50.0
        preco = c5[-1]
        ema20_1h = ema(c1h, 20)[i1h] if len(c1h) > 20 else c1h[-1]
        filtro_forte = (
            preco > ema20_1h and
            45 <= rsi15 <= 68 and
            v5[-1] > volmed5 * 1.1
        )

        # === CONDIÇÃO FINAL ===
        if cruzou_5m and hist_ok and filtro_forte:
            if can_alert(symbol, "CRUZAMENTO_5M", COOLDOWN_SEC):
                l5 = [float(k[3]) for k in k5]
                stop = min(l5[i5-1], ema(c5,21)[i5]) if i5 >= 1 else ema(c5,21)[i5]
                risco = max(preco - stop, 1e-12)
                alvo_1 = preco + 2.5 * risco
                alvo_2 = preco + 5.0 * risco
                tp_parcial = preco + risco

                if qv >= 100_000_000:
                    liq_status = f"Alta (US$ {qv/1_000_000:.1f}M)"
                elif qv >= 20_000_000:
                    liq_status = f"Média (US$ {qv/1_000_000:.1f}M)"
                else:
                    liq_status = f"Baixa (US$ {qv/1_000_000:.1f}M)"

                msg = (
                    f"<b>CRUZAMENTO 5M + CONFLUÊNCIA!</b>\n"
                    f"{symbol}\n"
                    f"MACD: 3m✅ 5m✅ 15m✅ 30m⬆️ 1h⬆️\n"
                    f"RSI15: {rsi15:.1f}\n"
                    f"Liquidez: {liq_status}\n\n"
                    f"Preço: {fmt_price(preco)}\n"
                    f"Stop: {fmt_price(stop)}\n"
                    f"Alvo1: {fmt_price(alvo_1)} (1:2.5)\n"
                    f"Alvo2: {fmt_price(alvo_2)} (1:5)\n"
                    f"Parcial: {fmt_price(tp_parcial)} (1:1)\n"
                    f"{now_br()}"
                )
                await tg(session, msg)

    except Exception as e:
        print(f"[ERRO] {symbol}: {e}")

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        pares = await get_top_usdt_symbols(session)
        await tg(session, f"<b>{VERSION} ATIVO</b>\nCruzamento 5m FECHADO + Confluência dinâmica\n{len(pares)} pares\n{now_br()}\n──────────────────────────────")
        while True:
            try:
                await asyncio.gather(*[scan_symbol(session, s, qv) for s, qv in pares])
                await asyncio.sleep(30)
            except Exception as e:
                await tg(session, f"<b>ERRO NO BOT</b>\n{e}\nReiniciando em 10s...\n{now_br()}")
                print(f"[LOOP ERRO] {e}")
                await asyncio.sleep(10)

def start_bot():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception as e:
            print(f"[LOOP ERRO] {e}")
            time.sleep(5)

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
