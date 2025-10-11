# ============================================
# 📁 main_v2_1_signal_strength.py
# ============================================
# Diego + Aurora — Binance Spot Alerts (v2.1):
# 1) Pump Detector (5m + check 15m) — fases: formação / entrada segura / saída
# 2) Day Trade (15m) — reteste inteligente (entrada possível / segura / saída)
# 3) Swing Trade (1h/4h) — confirmação multi-TF (entrada possível / segura / saída)
# Infra: Flask keep-alive (Render), Telegram, aiohttp, async
# ============================================

import os
import asyncio
import aiohttp
import threading
from datetime import datetime, timedelta
from statistics import mean
from flask import Flask

# -----------------------------
# 🔧 Variáveis de ambiente
# -----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://api.binance.com/api/v3"

# -----------------------------
# ⚙️ Parâmetros gerais
# -----------------------------
TOP_N = 50  # top por volume 24h
COOLDOWN_MIN = 15
COOLDOWN = timedelta(minutes=COOLDOWN_MIN)

# Cooldowns separados por módulo
cooldown_pump = {}
cooldown_day  = {}
cooldown_swing = {}

# -----------------------------
# 🌐 Flask (Render keep-alive)
# -----------------------------
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "OK — multi-setup v2.1 (pump/day/swing)", 200

# -----------------------------
# ✉️ Telegram
# -----------------------------
async def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_TOKEN/CHAT_ID ausentes.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=payload) as r:
            if r.status != 200:
                print("⚠️ Falha Telegram:", await r.text())

# -----------------------------
# 🔎 Requests utilitários
# -----------------------------
async def get_json(session, url):
    async with session.get(url) as resp:
        return await resp.json()

async def get_exchange_info(session):
    return await get_json(session, f"{BASE}/exchangeInfo")

async def get_ticker_24h(session):
    return await get_json(session, f"{BASE}/ticker/24hr")

async def get_klines(session, symbol, interval, limit=240):
    url = f"{BASE}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return await get_json(session, url)

# -----------------------------
# 🧮 Indicadores simples
# -----------------------------
def ma(series, p):
    if len(series) < p: return None
    return mean(series[-p:])

def ema(series, p):
    if len(series) < p: return None
    k = 2/(p+1)
    e = series[-p]
    for x in series[-p+1:]:
        e = x*k + e*(1-k)
    return e

def rsi(series, p=14):
    if len(series) < p+1: return None
    gains, losses = [], []
    for i in range(-p, 0):
        diff = series[i] - series[i-1]
        (gains if diff>0 else losses).append(abs(diff))
    ag = mean(gains) if gains else 0.0
    al = mean(losses) if losses else 1e-9
    rs = ag/al
    return 100 - (100/(1+rs))

# -----------------------------
# 🚫 Filtro SPOT válido (anti-USD universal)
# -----------------------------
async def load_valid_spot(session):
    info = await get_exchange_info(session)
    valid = []
    for s in info["symbols"]:
        sym = s["symbol"]
        base = sym.replace("USDT", "")
        if (
            s.get("isSpotTradingAllowed")
            and s["status"] == "TRADING"
            and sym.endswith("USDT")
            and not any(x in base for x in ["USD","FDUSD","BUSD","TUSD","USDC","DAI","AEUR","EUR","PYUSD"])
        ):
            valid.append(sym)
    # ordenar por volume
    t = await get_ticker_24h(session)
    tmap = {x["symbol"]: float(x.get("quoteVolume","0") or 0) for x in t}
    valid_sorted = [s for s in sorted(valid, key=lambda k: tmap.get(k,0), reverse=True)]
    return valid_sorted[:TOP_N], valid_sorted[TOP_N:]

# -----------------------------
# 🧠 Regras compartilhadas de classificação
# -----------------------------
def entry_classification_pump(rsi14_5, vol_ratio):
    # Pump em formação / Entrada segura / Evitar topo
    if rsi14_5 is None or vol_ratio is None:
        return "ℹ️ Aguardando dados", "📌 Saída: abaixo da EMA9 ou RSI<50"
    if rsi14_5 > 70:
        return "🔴 Evitar entrada — possível topo", "📌 Saída: abaixo da EMA9 ou RSI<50"
    if 50 <= rsi14_5 <= 65 and vol_ratio >= 3.0:
        return "🟢 Entrada segura confirmada", "📌 Saída: abaixo da EMA9 ou RSI<50"
    if 48 <= rsi14_5 < 55 and vol_ratio >= 1.5:
        return "🧩 Pump em formação — monitorar", "📌 Saída: abaixo da EMA9 ou RSI<50"
    return "🟡 Entrada possível (requer confirmação)", "📌 Saída: abaixo da EMA9 ou RSI<50"

def entry_classification_day(rsi14_15, vol_ratio):
    if rsi14_15 is None:
        return "ℹ️ Aguardando confirmação", "📌 Saída: abaixo da EMA9 ou RSI<50"
    if rsi14_15 > 70:
        return "🔴 Evitar entrada — possível topo", "📌 Saída: abaixo da EMA9 ou RSI<50"
    if rsi14_15 > 55 and (vol_ratio is None or vol_ratio >= 1.5):
        return "🟢 Entrada segura confirmada", "📌 Saída: abaixo da EMA9 ou RSI<50"
    if 50 <= rsi14_15 <= 55:
        return "🟡 Entrada possível (requer confirmação)", "📌 Saída: abaixo da EMA9 ou RSI<50"
    return "🟡 Entrada possível (risco maior)", "📌 Saída: abaixo da EMA9 ou RSI<50"

def entry_classification_swing(rsi14_1h, rsi14_4h):
    if rsi14_1h is None or rsi14_4h is None:
        return "ℹ️ Aguardando confirmação multi-TF", "📌 Saída: abaixo da EMA9(1h) ou RSI(1h)<50"
    if rsi14_1h > 55 and rsi14_4h > 55:
        return "🟢 <b>Entrada segura confirmada</b>", "<b>📌 Saída:</b> abaixo da EMA9(1h) ou RSI(1h)<50"
    if 50 <= rsi14_1h <= 55 and rsi14_4h >= 50:
        return "🟡 <b>Entrada possível</b>", "<b>📌 Saída:</b> abaixo da EMA9(1h) ou RSI(1h)<50"
    return "🟡 <b>Entrada possível (risco maior)</b>", "<b>📌 Saída:</b> abaixo da EMA9(1h) ou RSI(1h)<50"

def losing_strength_msg(tf_label=""):
    # Mensagem padrão de perda de força (para qualquer módulo)
    if tf_label:
        return f"🔻 Saída recomendada — perdendo força ({tf_label})"
    return "🔻 Saída recomendada — perdendo força"

# -----------------------------
# 🧠 Módulo 1 — Pump Detector (5m + check 15m)
# -----------------------------
async def pump_detector(session, symbol):
    now = datetime.now()
    if symbol in cooldown_pump and now - cooldown_pump[symbol] < COOLDOWN:
        return
    # 5m: gatilho
    k5 = await get_klines(session, symbol, "5m", 120)
    if not isinstance(k5, list) or len(k5) < 40: return
    c5 = [float(c[4]) for c in k5]
    v5 = [float(c[5]) for c in k5]
    price = c5[-1]
    ema9_5 = ema(c5,9); ma20_5 = ma(c5,20)
    rsi14_5 = rsi(c5,14)
    vol20_5 = ma(v5,20)
    vol_ratio = (v5[-1]/vol20_5) if vol20_5 else None
    if not all([ema9_5, ma20_5, rsi14_5]):
        return

    cond_cross = ema9_5 > ma20_5
    # Check 15m de força mínima
    k15 = await get_klines(session, symbol, "15m", 120)
    if not isinstance(k15, list) or len(k15) < 40: return
    c15 = [float(c[4]) for c in k15]
    rsi14_15 = rsi(c15,14)

    # Perda de força (alerta de saída)
    last_open5 = float(k5[-2][1]); last_close5 = float(k5[-2][4])
    ema9_close = ema9_5
    losing = (rsi14_5 < 50) or (last_close5 < ema9_close)

    if cond_cross and rsi14_15 and rsi14_15 > 50:
        entry_label, exit_hint = entry_classification_pump(rsi14_5, vol_ratio)
        msg = (
            f"🚀 PUMP — {symbol}\n"
            f"EMA9>MA20 • RSI(5m)={rsi14_5:.1f} • Vol≈{vol_ratio:.1f}x\n"
            f"🧪 Confirmação 15m: RSI={rsi14_15:.1f} (>50)\n"
            f"{entry_label}\n"
            f"{exit_hint}\n"
            f"💰 Preço: {price:.6f}\n"
            f"🕒 {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_pump[symbol] = now

        if losing:
            msg2 = (
                f"🚨 PUMP — {symbol}\n"
                f"{losing_strength_msg('5m')}\n"
                f"RSI(5m)={rsi14_5:.1f} • Close<EMA9? {'Sim' if last_close5 < ema9_close else 'Não'}\n"
                f"💰 Preço: {price:.6f}\n"
                f"{'━'*28}"
            )
            await send_telegram(msg2)

# -----------------------------
# 🧠 Módulo 2 — Day Trade (15m Reteste Inteligente)
# -----------------------------
async def daytrade_retest(session, symbol):
    now = datetime.now()
    if symbol in cooldown_day and now - cooldown_day[symbol] < COOLDOWN:
        return
    k15 = await get_klines(session, symbol, "15m", 200)
    if not isinstance(k15, list) or len(k15) < 120: return
    c15 = [float(c[4]) for c in k15]
    v15 = [float(c[5]) for c in k15]
    price = c15[-1]
    ema9_15 = ema(c15,9); ma20_15 = ma(c15,20); ma50_15 = ma(c15,50); ma200_15 = ma(c15,200)
    rsi14_15 = rsi(c15,14)
    if not all([ema9_15, ma20_15, ma50_15, ma200_15, rsi14_15]): return
    vol_ratio = (v15[-1] / ma(v15,20)) if ma(v15,20) else None

    trend_up = (ema9_15 > ma20_15 > ma50_15) and (price > ma200_15)
    touch = (abs(price-ema9_15)/ema9_15 < 0.005) or (abs(price-ma20_15)/ma20_15 < 0.005)

    # Perda de força (saída)
    last_open15 = float(k15[-2][1]); last_close15 = float(k15[-2][4])
    losing = (rsi14_15 < 50) or (last_close15 < ema9_15)

    if trend_up and touch:
        entry_label, exit_hint = entry_classification_day(rsi14_15, vol_ratio)
        msg = (
            f"🟢 <b>[DAY TRADE]</b> {symbol}\n"
            f"Reteste EMA9/MA20 • RSI(15m)={rsi14_15:.1f} • Vol≈{(vol_ratio or 1):.1f}x\n"
            f"{entry_label}\n"
            f"{exit_hint}\n"
            f"💰 Preço: {price:.6f}\n"
            f"🕒 {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_day[symbol] = now

        if losing:
            msg2 = (
                f"⚠️ <b>[DAY TRADE]</b> {symbol}\n"
                f"{losing_strength_msg('15m')}\n"
                f"RSI(15m)={rsi14_15:.1f} • Close<EMA9? {'Sim' if last_close15 < ema9_15 else 'Não'}\n"
                f"💰 Preço: {price:.6f}\n"
                f"{'━'*28}"
            )
            await send_telegram(msg2)

# -----------------------------
# 🧠 Módulo 3 — Swing (1h/4h Confirmação)
# -----------------------------
async def swing_detector(session, symbol):
    now = datetime.now()
    if symbol in cooldown_swing and now - cooldown_swing[symbol] < COOLDOWN:
        return

    # 1h base
    k1h = await get_klines(session, symbol, "1h", 240)
    if not isinstance(k1h, list) or len(k1h) < 200: return
    c1h = [float(c[4]) for c in k1h]
    price = c1h[-1]
    ema9_1h = ema(c1h,9); ma20_1h = ma(c1h,20); ma50_1h = ma(c1h,50); ma200_1h = ma(c1h,200)
    rsi14_1h = rsi(c1h,14)

    # 4h confirmação estrutural
    k4h = await get_klines(session, symbol, "4h", 240)
    if not isinstance(k4h, list) or len(k4h) < 200: return
    c4h = [float(c[4]) for c in k4h]
    ema9_4h = ema(c4h,9); ma20_4h = ma(c4h,20); ma50_4h = ma(c4h,50); ma200_4h = ma(c4h,200)
    rsi14_4h = rsi(c4h,14)

    if not all([ema9_1h, ma20_1h, ma50_1h, ma200_1h, rsi14_1h, ema9_4h, ma20_4h, ma50_4h, ma200_4h, rsi14_4h]):
        return

    trend_1h = (ema9_1h > ma20_1h > ma50_1h) and (price > ma200_1h) and (rsi14_1h and rsi14_1h > 50)
    confirm_4h = (ema9_4h > ma20_4h > ma50_4h) and (rsi14_4h and rsi14_4h > 50)

    touch_1h = (abs(price-ema9_1h)/ema9_1h < 0.006) or (abs(price-ma20_1h)/ma20_1h < 0.006)
    breakout_1h = price > (ma50_1h or price)

    # Perda de força (saída)
    last_open1h = float(k1h[-2][1]); last_close1h = float(k1h[-2][4])
    losing = (rsi14_1h < 50) or (last_close1h < ema9_1h)

    if trend_1h and confirm_4h and (touch_1h or breakout_1h):
        entry_label, exit_hint = entry_classification_swing(rsi14_1h, rsi14_4h)
        msg = (
            f"🚀 <b>[SWING TRADE]</b> {symbol}\n"
            f"<b>Confirmação 1h + 4h</b>\n"
            f"1h: EMA9>MA20>MA50 & Preço>MA200 • RSI={rsi14_1h:.1f}\n"
            f"4h: EMA9>MA20>MA50 • RSI={rsi14_4h:.1f}\n"
            f"{entry_label}\n"
            f"{exit_hint}\n"
            f"💰 Preço: <b>{price:.6f}</b>\n"
            f"🕒 {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_swing[symbol] = now

        if losing:
            msg2 = (
                f"⚠️ <b>[SWING TRADE]</b> {symbol}\n"
                f"<b>{losing_strength_msg('1h')}</b>\n"
                f"RSI(1h)={rsi14_1h:.1f} • Close<EMA9? {'Sim' if last_close1h < ema9_1h else 'Não'}\n"
                f"💰 Preço: <b>{price:.6f}</b>\n"
                f"{'━'*28}"
            )
            await send_telegram(msg2)

# -----------------------------
# 🔁 Loop principal
# -----------------------------
async def main_loop():
    await send_telegram("✅ <b>BOT ATIVO — Multi-Setup v2.1</b>\n🧠 Pump (5m), Day (15m), Swing (1h/4h)\n⏱️ Cooldown: 15 min por par/módulo")
    async with aiohttp.ClientSession() as session:
        top_pairs, _ = await load_valid_spot(session)
        await send_telegram(f"💹 Pares carregados (TOP {len(top_pairs)}): {', '.join(top_pairs[:10])} ...")

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                top_pairs, _ = await load_valid_spot(session)
                tasks = []
                for sym in top_pairs:
                    tasks += [
                        pump_detector(session, sym),
                        daytrade_retest(session, sym),
                        swing_detector(session, sym),
                    ]
                await asyncio.gather(*tasks)
            await asyncio.sleep(60)  # roda a cada 1 min
        except Exception as e:
            print("❌ Erro no loop:", e)
            await asyncio.sleep(10)

# -----------------------------
# 🚀 Inicialização (Render)
# -----------------------------
def _start_bot():
    asyncio.run(main_loop())

threading.Thread(target=_start_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
