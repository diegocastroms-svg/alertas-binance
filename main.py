import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_5M, INTERVAL_15M = "5m", "15m"
COOLDOWN = 15 * 60  # 15 minutos
TOP_PAIRS_LIMIT = 50

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

app = Flask(__name__)
cooldowns = {}
precos = defaultdict(lambda: deque(maxlen=200))
volumes = defaultdict(lambda: deque(maxlen=200))

# ---------------- FUNÇÕES ----------------
async def enviar_alerta(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    params = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        await session.post(url, params=params)

async def pegar_pares_spot():
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr") as r:
            dados = await r.json()
            filtrados = [d for d in dados if d["symbol"].endswith("USDT")]
            ordenados = sorted(filtrados, key=lambda x: float(x["quoteVolume"]), reverse=True)
            return [d["symbol"] for d in ordenados[:TOP_PAIRS_LIMIT]]

async def pegar_klines(session, symbol, interval):
    try:
        async with session.get(f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit=100") as r:
            return await r.json()
    except:
        return []

def calcular_ma(valores, periodo):
    if len(valores) < periodo:
        return None
    return sum(valores[-periodo:]) / periodo

def calcular_rsi(valores, periodo=14):
    if len(valores) < periodo + 1:
        return 0
    ganhos, perdas = 0, 0
    for i in range(-periodo, -1):
        diff = valores[i + 1] - valores[i]
        if diff > 0:
            ganhos += diff
        else:
            perdas -= diff
    if perdas == 0:
        return 100
    rs = ganhos / perdas
    return 100 - (100 / (1 + rs))

# ---------------- LÓGICA PRINCIPAL ----------------
async def analisar_moeda(session, symbol, interval):
    klines = await pegar_klines(session, symbol, interval)
    if not klines or len(klines) < 50:
        return

    closes = [float(k[4]) for k in klines]
    volumes_lista = [float(k[5]) for k in klines]
    preco_atual = closes[-1]
    volume = volumes_lista[-1]
    volume_media = calcular_ma(volumes_lista, 20)

    ema9 = calcular_ma(closes, 9)
    ma20 = calcular_ma(closes, 20)
    ma50 = calcular_ma(closes, 50)
    ma200 = calcular_ma(closes, 200)
    rsi = calcular_rsi(closes, 14)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if symbol not in cooldowns:
        cooldowns[symbol] = 0

    # --- TENDÊNCIA INICIANDO (5m)
    if interval == "5m" and ema9 > ma20 and rsi > 50 and preco_atual > ema9:
        if time.time() > cooldowns[symbol]:
            msg = f"🚀 {symbol} Tendência iniciando (5m)\n💰 {preco_atual}\n📊 RSI: {rsi:.1f}\n⏱️ {agora}"
            await enviar_alerta(msg)
            cooldowns[symbol] = time.time() + COOLDOWN

    # --- TENDÊNCIA CONFIRMADA (15m)
    if interval == "15m" and ema9 > ma20 > ma50 > ma200 and rsi > 55:
        if time.time() > cooldowns[symbol]:
            msg = f"🚀 {symbol} Tendência confirmada (15m)\n💰 {preco_atual}\n📊 RSI: {rsi:.1f}\n⏱️ {agora}"
            await enviar_alerta(msg)
            cooldowns[symbol] = time.time() + COOLDOWN

    # ---------------------- ALERTA: ENTRADA EXPLOSIVA (5m) ----------------------
    if interval == "5m":
        if (
            ema9 > ma20 > ma50
            and rsi > 50
            and volume > volume_media * 1.2  # confirma aumento de volume
            and preco_atual > ema9  # garante reação de alta
        ):
            msg = (
                f"⚡ {symbol} 🚀 Entrada Explosiva detectada (5m)\n"
                f"💰 {preco_atual}\n"
                f"📊 RSI: {rsi:.1f} | Vol: {volume:.1f}\n"
                f"⏱️ {agora}"
            )
            await enviar_alerta(msg)
            cooldowns[symbol] = time.time() + COOLDOWN


# ---------------- LOOP ----------------
async def main():
    pares = await pegar_pares_spot()
    print(f"✅ v3.3 intrabar ativo | {len(pares)} pares SPOT | cooldown 15m | {datetime.now()}")
    while True:
        async with aiohttp.ClientSession() as session:
            tarefas = []
            for symbol in pares:
                tarefas.append(analisar_moeda(session, symbol, INTERVAL_5M))
                tarefas.append(analisar_moeda(session, symbol, INTERVAL_15M))
            await asyncio.gather(*tarefas)
        await asyncio.sleep(60)

# ---------------- WEB SERVER ----------------
@app.route("/")
def home():
    return "Bot rodando..."

if __name__ == "__main__":
    asyncio.run(main())
