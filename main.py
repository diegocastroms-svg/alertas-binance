# main_v3_6_corrigido.py
# ✅ Idêntico ao main_v3_6 original
# ✅ Corrige erro de alerta “iniciando” vs “pré-confirmada”
# ✅ Sem alterar nada mais

import os, asyncio, aiohttp, math, time
from datetime import datetime, timezone
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVALOS = ["5m", "15m"]
LIMIT = 50
COOLDOWN = 900  # 15 minutos

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

ULTIMO_ALERTA = {}

# ---------------- FUNÇÕES ----------------
async def enviar_alerta(session, texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML"}
    await session.post(url, data=payload)

async def get_klines(session, symbol, interval):
    try:
        url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit=200"
        async with session.get(url) as resp:
            return await resp.json()
    except:
        return []

def calc_ma(prices, period):
    if len(prices) < period: return None
    return sum(prices[-period:]) / period

def calc_ema(prices, period):
    if len(prices) < period: return None
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = (p * k) + (ema * (1 - k))
    return ema

def calc_rsi(prices, period=14):
    if len(prices) < period + 1: return None
    gains, losses = 0, 0
    for i in range(1, period + 1):
        diff = prices[-i] - prices[-i - 1]
        if diff >= 0: gains += diff
        else: losses -= diff
    if losses == 0: return 100
    rs = gains / losses
    return 100 - (100 / (1 + rs))

# ---------------- LÓGICA ----------------
async def analisar_moeda(session, symbol):
    global ULTIMO_ALERTA
    agora = time.time()
    for intervalo in INTERVALOS:
        klines = await get_klines(session, symbol, intervalo)
        if not klines or isinstance(klines, dict): continue
        closes = [float(k[4]) for k in klines]
        vol = [float(k[5]) for k in klines]

        ema9 = calc_ema(closes, 9)
        ma20 = calc_ma(closes, 20)
        ma50 = calc_ma(closes, 50)
        ma200 = calc_ma(closes, 200)
        rsi = calc_rsi(closes, 14)
        if not all([ema9, ma20, ma50, ma200, rsi]): continue

        preco = closes[-1]
        chave = f"{symbol}-{intervalo}"

        # --- FILTRO cooldown ---
        if chave in ULTIMO_ALERTA and agora - ULTIMO_ALERTA[chave] < COOLDOWN:
            continue

        # --------------- ALERTAS ---------------
        # 🚀 Tendência iniciando (5m)
        if intervalo == "5m" and ema9 > ma20 and ema9 > ma50 and preco < ma200 * 1.02 and not (ema9 > ma20 > ma50 > ma200):
            msg = f"🟢 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {preco:.6f}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await enviar_alerta(session, msg)
            ULTIMO_ALERTA[chave] = agora

        # 🌕 Tendência pré-confirmada (5m)
        if intervalo == "5m" and ema9 > ma20 > ma50 > ma200 and rsi > 55:
            msg = f"🌕 {symbol} ⚡ Tendência pré-confirmada (5m)\n💰 {preco:.6f}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await enviar_alerta(session, msg)
            ULTIMO_ALERTA[chave] = agora

        # 🌕 Tendência pré-confirmada (15m)
        if intervalo == "15m" and ema9 > ma200 and rsi > 55:
            msg = f"🌕 {symbol} ⚡ Tendência pré-confirmada (15m)\n💰 {preco:.6f}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await enviar_alerta(session, msg)
            ULTIMO_ALERTA[chave] = agora

        # 🚀 Tendência confirmada (15m)
        if intervalo == "15m" and ema9 > ma20 > ma50 > ma200 and rsi > 55:
            msg = f"🚀 {symbol} 🔥 Tendência confirmada (15m)\n💰 {preco:.6f}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await enviar_alerta(session, msg)
            ULTIMO_ALERTA[chave] = agora

# ---------------- LOOP ----------------
async def main():
    async with aiohttp.ClientSession() as session:
        await enviar_alerta(session, "✅ v3.6 corrigido | 50 pares SPOT | cooldown 15m")
        url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
        async with session.get(url) as resp:
            data = await resp.json()
        pares = sorted(data, key=lambda x: float(x["quoteVolume"]), reverse=True)
        top50 = [p["symbol"] for p in pares if p["symbol"].endswith("USDT")][:LIMIT]

        while True:
            tarefas = [analisar_moeda(session, s) for s in top50]
            await asyncio.gather(*tarefas)
            await asyncio.sleep(60)

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot rodando com sucesso!"

if __name__ == "__main__":
    asyncio.run(main())
