# ============================================
# 📁 main_v2_3_dynamic_preconfirm5m.py
# ============================================
# Binance Spot Alerts (v2.3 Dynamic) + ALERTA EXCLUSIVO 5m (pré-confirmação)
# - Varre TODAS SPOT/USDT e seleciona TOP 50 por volume 24h (auto-update 1h)
# - 3 setups em paralelo:
#   1) Pump (5m + check 15m) — formação / entrada segura / saída
#      ➕ Alerta EXCLUSIVO 5m: EMA9>MA20>MA50 com preço < MA200 (pré-confirmação)
#   2) Day (15m) — reteste inteligente
#   3) Swing (1h/4h) — confirmação multi-TF
# - Cooldown 15min por par/módulo; loop 60s; Flask (Render); Telegram
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
TOP_N = 50                           # sempre monitora as 50 com maior volume 24h
COOLDOWN_MIN = 15
COOLDOWN = timedelta(minutes=COOLDOWN_MIN)
TOP_REFRESH_EVERY = timedelta(hours=1)  # atualização automática da lista TOP
ANTI_LIST = ["USD","FDUSD","BUSD","TUSD","USDC","DAI","AEUR","EUR","PYUSD"]

# Cooldowns separados por módulo
cooldown_pump = {}
cooldown_day  = {}
cooldown_swing = {}

# Lista dinâmica de pares TOP
top_pairs_cache = []
next_top_refresh_at = None

# -----------------------------
# 🌐 Flask (Render keep-alive)
# -----------------------------
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "OK — v2.3 dynamic (preconfirm 5m)", 200

# -----------------------------
# ✉️ Telegram
# -----------------------------
async def send_telegram(msg: str, html: bool = True):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_TOKEN/CHAT_ID ausentes — não foi possível enviar:", msg[:80])
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": True}
    if html:
        payload["parse_mode"] = "HTML"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=payload) as r:
            if r.status != 200:
                try:
                    print("⚠️ Falha Telegram:", await r.text())
                except:
                    print("⚠️ Falha Telegram: HTTP", r.status)

# -----------------------------
# 🔗 Link — gráfico direto no app da Binance
# -----------------------------
def binance_chart_link(symbol: str) -> str:
    base = symbol.replace("USDT", "")
    return f"https://www.binance.com/en/trade/{base}_USDT?ref=open_in_app&layout=pro"

def chart_link_line(symbol: str, tf_label: str) -> str:
    return f'🔗 <a href="{binance_chart_link(symbol)}">Ver gráfico {tf_label} no app da Binance</a>'

# -----------------------------
# 🔎 Requests utilitários
# -----------------------------
async def get_json(session, url):
    async with session.get(url) as resp:
        return await resp.json()

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
# 🔍 Scanner dinâmico de TOP 50 SPOT/USDT
# -----------------------------
async def compute_top50(session):
    """
    Varre TODOS os tickers 24h, filtra SPOT/USDT reais e retorna TOP_N por quoteVolume.
    """
    tickers = await get_ticker_24h(session)
    if not isinstance(tickers, list):
        return []
    ranked = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym.replace("USDT", "")
        if any(x in base for x in ANTI_LIST):
            continue
        try:
            qv = float(t.get("quoteVolume", "0") or 0.0)
        except:
            qv = 0.0
        ranked.append((sym, qv))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:TOP_N]]

async def ensure_top_pairs(session, force=False):
    """
    Atualiza a lista TOP a cada 1h (ou quando force=True).
    Envia uma mensagem curta avisando da atualização (sem listar moedas).
    """
    global top_pairs_cache, next_top_refresh_at
    now = datetime.utcnow()
    if force or next_top_refresh_at is None or now >= next_top_refresh_at:
        new_list = await compute_top50(session)
        if new_list and new_list != top_pairs_cache:
            top_pairs_cache = new_list
            await send_telegram("🔄 Lista TOP 50 SPOT atualizada — monitorando novas moedas (baseado em volume 24h).")
        # agenda próxima atualização
        next_top_refresh_at = now + TOP_REFRESH_EVERY
    return top_pairs_cache

# -----------------------------
# 🧠 Regras de classificação (mensagens de entrada/saída)
# -----------------------------
def entry_classification_pump(rsi14_5, vol_ratio):
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
        return "ℹ️ Aguardando confirmação multi-TF", "<b>📌 Saída:</b> abaixo da EMA9(1h) ou RSI(1h)<50"
    if rsi14_1h > 55 and rsi14_4h > 55:
        return "🟢 <b>Entrada segura confirmada</b>", "<b>📌 Saída:</b> abaixo da EMA9(1h) ou RSI(1h)<50"
    if 50 <= rsi14_1h <= 55 and rsi14_4h >= 50:
        return "🟡 <b>Entrada possível</b>", "<b>📌 Saída:</b> abaixo da EMA9(1h) ou RSI(1h)<50"
    return "🟡 <b>Entrada possível (risco maior)</b>", "<b>📌 Saída:</b> abaixo da EMA9(1h) ou RSI(1h)<50"

def losing_strength_msg(tf_label=""):
    if tf_label:
        return f"🔻 Saída recomendada — perdendo força ({tf_label})"
    return "🔻 Saída recomendada — perdendo força"

# -----------------------------
# 🧠 Módulo 1 — Pump (5m + check 15m)
#     ➕ ALERTA EXCLUSIVO de PRÉ-CONFIRMAÇÃO (abaixo da MA200)
# -----------------------------
async def pump_detector(session, symbol):
    now = datetime.now()
    if symbol in cooldown_pump and now - cooldown_pump[symbol] < COOLDOWN:
        return

    k5 = await get_klines(session, symbol, "5m", 240)   # 240 p/ MA200
    if not isinstance(k5, list) or len(k5) < 210: return
    c5 = [float(c[4]) for c in k5]
    v5 = [float(c[5]) for c in k5]
    price = c5[-1]

    ema9_5  = ema(c5,9)
    ma20_5  = ma(c5,20)
    ma50_5  = ma(c5,50)
    ma200_5 = ma(c5,200)
    rsi14_5 = rsi(c5,14)
    vol20_5 = ma(v5,20)
    vol_ratio = (v5[-1]/vol20_5) if vol20_5 else None
    if not all([ema9_5, ma20_5, ma50_5, ma200_5, rsi14_5]): return

    # Checagem 15m (força mínima)
    k15 = await get_klines(session, symbol, "15m", 120)
    if not isinstance(k15, list) or len(k15) < 40: return
    c15 = [float(c[4]) for c in k15]
    rsi14_15 = rsi(c15,14)

    # ✅ NOVO ALERTA EXCLUSIVO: PRÉ-CONFIRMAÇÃO (5m)
    # Condição: EMA9 > MA20 > MA50 e PREÇO ainda abaixo da MA200
    if ema9_5 > ma20_5 > ma50_5 and price < ma200_5:
        msg_pre = (
            f"🟢 <b>[PUMP 5m — PRÉ-CONFIRMAÇÃO]</b> {symbol}\n"
            f"EMA9>MA20>MA50 com <b>preço abaixo da MA200</b>\n"
            f"RSI(5m)={rsi14_5:.1f} • Vol≈{(vol_ratio or 0):.1f}x\n"
            f"💰 Preço: {price:.6f}\n"
            f"🕒 {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"{chart_link_line(symbol, '5m')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg_pre)
        cooldown_pump[symbol] = now

    # 🔹 Alerta principal de Pump (mantido)
    cond_cross = ema9_5 > ma20_5
    last_close5 = float(k5[-2][4])
    losing = (rsi14_5 < 50) or (last_close5 < ema9_5)

    if cond_cross and rsi14_15 and rsi14_15 > 50:
        entry_label, exit_hint = entry_classification_pump(rsi14_5, vol_ratio)
        msg = (
            f"🚀 <b>[PUMP 5m]</b> {symbol}\n"
            f"EMA9>MA20 • RSI(5m)={rsi14_5:.1f} • Vol≈{(vol_ratio or 0):.1f}x\n"
            f"🧪 Confirmação 15m: RSI={rsi14_15:.1f} (>50)\n"
            f"{entry_label}\n"
            f"{exit_hint}\n"
            f"💰 Preço: {price:.6f}\n"
            f"🕒 {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"{chart_link_line(symbol, '5m')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_pump[symbol] = now

        if losing:
            msg2 = (
                f"⚠️ <b>[PUMP 5m]</b> {symbol}\n"
                f"{losing_strength_msg('5m')}\n"
                f"RSI(5m)={rsi14_5:.1f} • Close<EMA9? {'Sim' if last_close5 < ema9_5 else 'Não'}\n"
                f"💰 Preço: {price:.6f}\n"
                f"{chart_link_line(symbol, '5m')}\n"
                f"{'━'*28}"
            )
            await send_telegram(msg2)

# -----------------------------
# 🧠 Módulo 2 — Day (15m Reteste Inteligente)
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
    vol_ratio = (v15[-1]/ma(v15,20)) if ma(v15,20) else None

    trend_up = (ema9_15 > ma20_15 > ma50_15) and (price > ma200_15)
    touch = (abs(price-ema9_15)/ema9_15 < 0.005) or (abs(price-ma20_15)/ma20_15 < 0.005)

    last_close15 = float(k15[-2][4])
    losing = (rsi14_15 < 50) or (last_close15 < ema9_15)

    if trend_up and touch:
        entry_label, exit_hint = entry_classification_day(rsi14_15, vol_ratio)
        msg = (
            f"🟢 <b>[DAY TRADE 15m]</b> {symbol}\n"
            f"Reteste EMA9/MA20 • RSI(15m)={rsi14_15:.1f} • Vol≈{(vol_ratio or 0):.1f}x\n"
            f"{entry_label}\n"
            f"{exit_hint}\n"
            f"💰 Preço: {price:.6f}\n"
            f"🕒 {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"{chart_link_line(symbol, '15m')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_day[symbol] = now

        if losing:
            msg2 = (
                f"⚠️ <b>[DAY TRADE 15m]</b> {symbol}\n"
                f"{losing_strength_msg('15m')}\n"
                f"RSI(15m)={rsi14_15:.1f} • Close<EMA9? {'Sim' if last_close15 < ema9_15 else 'Não'}\n"
                f"💰 Preço: {price:.6f}\n"
                f"{chart_link_line(symbol, '15m')}\n"
                f"{'━'*28}"
            )
            await send_telegram(msg2)

# -----------------------------
# 🧠 Módulo 3 — Swing (1h/4h)
# -----------------------------
async def swing_detector(session, symbol):
    now = datetime.now()
    if symbol in cooldown_swing and now - cooldown_swing[symbol] < COOLDOWN:
        return

    k1h = await get_klines(session, symbol, "1h", 240)
    if not isinstance(k1h, list) or len(k1h) < 200: return
    c1h = [float(c[4]) for c in k1h]
    price = c1h[-1]
    ema9_1h = ema(c1h,9); ma20_1h = ma(c1h,20); ma50_1h = ma(c1h,50); ma200_1h = ma(c1h,200)
    rsi14_1h = rsi(c1h,14)

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

    last_close1h = float(k1h[-2][4])
    losing = (rsi14_1h < 50) or (last_close1h < ema9_1h)

    if trend_1h and confirm_4h and (touch_1h or breakout_1h):
        entry_label, exit_hint = entry_classification_swing(rsi14_1h, rsi14_4h)
        msg = (
            f"🚀 <b>[SWING 1h/4h]</b> {symbol}\n"
            f"<b>Confirmação 1h + 4h</b>\n"
            f"1h: EMA9>MA20>MA50 & Preço>MA200 • RSI={rsi14_1h:.1f}\n"
            f"4h: EMA9>MA20>MA50 • RSI={rsi14_4h:.1f}\n"
            f"{entry_label}\n"
            f"{exit_hint}\n"
            f"💰 Preço: <b>{price:.6f}</b>\n"
            f"🕒 {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"{chart_link_line(symbol, '1h/4h')}\n"
            f"{'━'*28}"
        )
        await send_telegram(msg)
        cooldown_swing[symbol] = now

        if losing:
            msg2 = (
                f"⚠️ <b>[SWING 1h]</b> {symbol}\n"
                f"<b>{losing_strength_msg('1h')}</b>\n"
                f"RSI(1h)={rsi14_1h:.1f} • Close<EMA9? {'Sim' if last_close1h < ema9_1h else 'Não'}\n"
                f"💰 Preço: <b>{price:.6f}</b>\n"
                f"{chart_link_line(symbol, '1h/4h')}\n"
                f"{'━'*28}"
            )
            await send_telegram(msg2)

# -----------------------------
# 🔁 Loop principal
# -----------------------------
async def main_loop():
    print("✅ Bot iniciado no Render v2.3 Dynamic + PreConfirm 5m")
    # Mensagem de ativação (texto simples primeiro, depois HTML)
    await send_telegram("Bot iniciado com sucesso ✅", html=False)
    await asyncio.sleep(1)
    await send_telegram("✅ <b>BOT ATIVO — Multi-Setup v2.3</b>\n🧠 Pump (5m), Day (15m), Swing (1h/4h)\n⏱️ Cooldown: 15 min por par/módulo")

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                # Atualiza/garante TOP 50 dinâmico (avisa ao atualizar)
                pairs = await ensure_top_pairs(session)
                if not pairs:
                    await asyncio.sleep(10)
                    continue

                # Executa os três módulos em paralelo para todos os pares TOP_N
                tasks = []
                for sym in pairs:
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
# 🚀 Execução para Render (Flask + loop paralelo)
# -----------------------------
def _start_bot():
    asyncio.run(main_loop())

if __name__ == "__main__":
    threading.Thread(target=_start_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
