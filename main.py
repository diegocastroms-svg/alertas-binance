# main.py — V9 OURO FUNDO REAL DINÂMICO
# Detector de fundo baseado em comportamento (30m + 15m)
# Sem RSI fixo, sem queda fixa, volume mínimo 5M

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "V9 OURO FUNDO REAL DINÂMICO ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 5_000_000        # 5M conforme pedido
MIN_VOLAT = 1.5              # variação mínima em 24h (flexível, não extrema)
TOP_N = 80                   # mais moedas na peneira
COOLDOWN = 900               # 15 minutos por par
SCAN_INTERVAL = 30           # segundos entre varreduras
BOOK_DOM = 1.05              # fluxo comprador levemente dominante

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    try:
        await s.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print("Erro Telegram:", e)

def ema(data, p):
    if not data:
        return []
    a = 2 / (p + 1)
    e = data[0]
    out = [e]
    for x in data[1:]:
        e = a * x + (1 - a) * e
        out.append(e)
    return out

def rsi(prices, p=14):
    if len(prices) < p + 1:
        return 50.0
    d = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(x, 0) for x in d[-p:]]
    losses = [abs(min(x, 0)) for x in d[-p:]]
    ag = sum(gains) / p
    al = (sum(losses) / p) or 1e-12
    return 100 - 100 / (1 + ag / al)

def macd_virando(close):
    if len(close) < 26:
        return False
    e12 = ema(close, 12)
    e26 = ema(close, 26)
    macd_series = [a - b for a, b in zip(e12, e26)]
    signal_series = ema(macd_series, 9)
    if len(macd_series) < 2 or len(signal_series) < 2:
        return False
    hist_now = macd_series[-1] - signal_series[-1]
    hist_prev = macd_series[-2] - signal_series[-2]
    return hist_now > hist_prev

def bollinger_width(close, p=20):
    if len(close) < p:
        return 0.0
    sub = close[-p:]
    m = sum(sub) / p
    if m == 0:
        return 0.0
    var = sum((x - m) ** 2 for x in sub) / p
    std = var ** 0.5
    up = m + 2 * std
    dn = m - 2 * std
    return ((up - dn) / m) * 100.0

cooldown_fundo = {}

def can_alert(sym):
    n = time.time()
    last = cooldown_fundo.get(sym, 0)
    if n - last >= COOLDOWN:
        cooldown_fundo[sym] = n
        return True
    return False

async def klines(s, sym, tf, limit=200):
    async with s.get(f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit={limit}", timeout=10) as r:
        return await r.json() if r.status == 200 else []

async def ticker(s, sym):
    async with s.get(f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}", timeout=10) as r:
        return await r.json() if r.status == 200 else None

async def scan_symbol(s, sym):
    try:
        print(f"[{now_br()}] Analizando {sym}...")

        t = await ticker(s, sym)
        if not t:
            return

        vol24 = float(t.get("quoteVolume", 0) or 0.0)
        if vol24 < MIN_VOL24:
            return

        change_24h = float(t.get("priceChangePercent", 0) or 0.0)

        # 30m e 15m
        k30 = await klines(s, sym, "30m", 200)
        k15 = await klines(s, sym, "15m", 200)
        if len(k30) < 60 or len(k15) < 40:
            return

        close30 = [float(x[4]) for x in k30]
        vol30 = [float(x[5]) for x in k30]
        close15 = [float(x[4]) for x in k15]
        vol15 = [float(x[5]) for x in k15]

        # --- 30m: queda perdeu força / transição ---
        # tendência recente: média dos últimos 5 vs anteriores
        recent30 = close30[-5:]
        prev30 = close30[-15:-5]
        if len(prev30) < 5:
            return

        media_recent = sum(recent30) / len(recent30)
        media_prev = sum(prev30) / len(prev30)

        # tendência estava caindo e começou a estabilizar/subir levemente
        tendencia_ok = media_recent >= media_prev * 0.97  # parou de derreter
        abaixo_topo = close30[-1] <= max(close30[-30:]) * 0.97  # ainda bem abaixo do topo

        if not (tendencia_ok and abaixo_topo):
            return

        # volatilidade 30m afunilando
        bw_now = bollinger_width(close30)
        bw_prev = bollinger_width(close30[:-5]) if len(close30) > 25 else bw_now
        volat_ok = bw_now <= bw_prev * 0.9 or bw_now <= 12.0  # bandas mais apertadas

        # volume 30m: pico vendedor já passou (volume atual <= max dos 4 anteriores)
        if len(vol30) >= 5:
            vol30_ok = vol30[-1] <= max(vol30[-5:-1])
        else:
            vol30_ok = True

        # RSI 30m: ainda não esticado (abaixo de 55)
        rsi30 = rsi(close30)
        rsi30_ok = rsi30 <= 55.0

        # --- 15m: micro pivô + EMA + volume ---
        last15 = k15[-1]
        prev1 = k15[-2]
        prev2 = k15[-3]

        o_last = float(last15[1])
        h_last = float(last15[2])
        c_last = float(last15[4])

        h_prev1 = float(prev1[2])
        h_prev2 = float(prev2[2])

        # candle verde rompendo máxima de 2 candles anteriores
        micro_pivo = c_last > o_last and c_last > h_prev1 and c_last > h_prev2

        ema9_15 = ema(close15, 9)
        ema21_15 = ema(close15, 21)
        if len(ema21_15) < 2:
            return
        ema_cross_up = ema9_15[-1] > ema21_15[-1] and ema9_15[-2] <= ema21_15[-2]

        # RSI 15m subindo (não fixo; só precisa estar subindo)
        rsi15_now = rsi(close15)
        rsi15_prev = rsi(close15[:-3]) if len(close15) > 20 else rsi15_now
        rsi15_up = rsi15_now > rsi15_prev

        # volume 15m: aumento em relação à média recente
        if len(vol15) >= 10:
            media_vol15 = sum(vol15[-10:-2]) / 8
        else:
            media_vol15 = sum(vol15[:-1]) / max(len(vol15) - 1, 1)
        vol15_ok = vol15[-1] >= media_vol15 * 1.2

        # MACD 5m virando pra cima (entrada antecipada no curtinho)
        k5 = await klines(s, sym, "5m", 120)
        if len(k5) < 35:
            macd_ok = True  # se não tiver dado, não bloqueia
        else:
            close5 = [float(x[4]) for x in k5]
            macd_ok = macd_virando(close5)

        # fluxo real
        taker_buy = float(t.get("takerBuyQuoteAssetVolume", 0) or 0.0)
        taker_sell = max(vol24 - taker_buy, 0.0)
        fluxo_ok = (taker_buy >= taker_sell * BOOK_DOM) or taker_buy == 0.0

        # combinação geral
        fundo_real_ok = (
            tendencia_ok
            and abaixo_topo
            and volat_ok
            and vol30_ok
            and rsi30_ok
            and micro_pivo
            and ema_cross_up
            and rsi15_up
            and vol15_ok
            and macd_ok
            and fluxo_ok
        )

        if not fundo_real_ok:
            return

        if not can_alert(sym):
            return

        nome = sym.replace("USDT", "")

        msg = (
            f"🟢 <b>V9 OURO FUNDO REAL DINÂMICO</b>\n\n"
            f"{nome}\n\n"
            f"📊 24h: <b>{change_24h:.2f}%</b> | Vol 24h: <b>{vol24:,.0f}</b>\n"
            f"⏱ Timeframe base: <b>30m + 15m</b>\n\n"
            f"30m:\n"
            f"• RSI: <b>{rsi30:.1f}</b>\n"
            f"• Largura Bollinger: <b>{bw_now:.1f}%</b> (antes ~{bw_prev:.1f}%)\n"
            f"• Volume atual vs últimos: <b>{vol30[-1]:,.0f}</b>\n\n"
            f"15m:\n"
            f"• RSI subindo: <b>{rsi15_prev:.1f} → {rsi15_now:.1f}</b>\n"
            f"• EMA9 x EMA21: <b>cruzando pra cima</b>\n"
            f"• Volume: <b>{vol15[-1]:,.0f}</b> vs média ~<b>{media_vol15:,.0f}</b>\n"
            f"• Micro pivô: <b>máxima rompendo 2 candles</b>\n\n"
            f"Fluxo real:\n"
            f"• TakerBuy: <b>{taker_buy:,.0f}</b> vs TakerSell: <b>{taker_sell:,.0f}</b>\n\n"
            f"⏱ {now_br()} BR"
        )

        await tg(s, msg)

    except Exception as e:
        print("Erro scan_symbol:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V9 OURO FUNDO REAL DINÂMICO INICIADO — MIN_VOL24 = 5M</b>")
        while True:
            try:
                resp = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=15)
                if resp.status != 200:
                    print("Erro ao buscar ticker24hr, tentando novamente...")
                    await asyncio.sleep(SCAN_INTERVAL)
                    continue

                data = await resp.json()

                symbols = [
                    d["symbol"]
                    for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume") or 0.0) >= MIN_VOL24
                    and abs(float(d.get("priceChangePercent") or 0.0)) >= MIN_VOLAT
                    and not any(x in d["symbol"] for x in [
                        "UP", "DOWN", "BUSD", "FDUSD", "USDC", "TUSD",
                        "EUR", "USDE", "TRY", "GBP", "BRL", "AUD", "CAD"
                    ])
                ]

                symbols = sorted(
                    symbols,
                    key=lambda x: next(
                        (float(d.get("quoteVolume") or 0.0) for d in data if d["symbol"] == x),
                        0.0
                    ),
                    reverse=True
                )[:TOP_N]

                print(f"\n[{now_br()}] === V9 - Varredura FUNDO REAL ({len(symbols)} moedas) ===")

                tasks = [scan_symbol(s, sym) for sym in symbols]
                await asyncio.gather(*tasks)

                print(f"[{now_br()}] === V9 - Varredura finalizada ===\n")

            except Exception as e:
                print("Erro main_loop:", e)

            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(
    target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))),
    daemon=True
).start()

asyncio.run(main_loop())
```0
