import os, asyncio, time, math
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVALOS = ["5m", "15m"]
LIMIT = 100
COOLDOWN = 15 * 60
TOP_PAIRS = 50  # 50 maiores volumes
cooldowns = {}

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot de alertas Binance ativo!"

# ---------------- FUNÇÕES ----------------
async def pegar_pares_spot(sessao):
    async with sessao.get(f"{BINANCE_HTTP}/api/v3/exchangeInfo") as r:
        data = await r.json()
    return [s["symbol"] for s in data["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"]

async def pegar_klines(sessao, symbol, interval):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={LIMIT}"
    async with sessao.get(url) as r:
        return await r.json()

def calcular_ma(valores, periodo):
    if len(valores) < periodo:
        return None
    return sum(valores[-periodo:]) / periodo

def calcular_rsi(precos, periodo=14):
    if len(precos) < periodo + 1:
        return None
    ganhos = [max(precos[i] - precos[i - 1], 0) for i in range(1, len(precos))]
    perdas = [max(precos[i - 1] - precos[i], 0) for i in range(1, len(precos))]
    ganho_medio = sum(ganhos[-periodo:]) / periodo
    perda_media = sum(perdas[-periodo:]) / periodo
    if perda_media == 0:
        return 100
    rs = ganho_medio / perda_media
    return 100 - (100 / (1 + rs))

async def enviar_alerta(mensagem):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        print("⚠️ TOKEN ou CHAT_ID não configurados.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": mensagem, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as sessao:
        await sessao.post(url, data=payload)

async def analisar_moeda(sessao, symbol, interval):
    global cooldowns
    if symbol in cooldowns and time.time() - cooldowns[symbol] < COOLDOWN:
        return

    klines = await pegar_klines(sessao, symbol, interval)
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    preco_atual = closes[-1]
    ema9 = calcular_ma(closes, 9)
    ma20 = calcular_ma(closes, 20)
    ma50 = calcular_ma(closes, 50)
    ma200 = calcular_ma(closes, 200)
    rsi = calcular_rsi(closes, 14)
    volume = volumes[-1]
    volume_media = calcular_ma(volumes, 20)

    # 🔒 Correção adicionada: evita erro de comparação com NoneType
    if None in (ema9, ma20, ma50, ma200, rsi, volume, volume_media):
        return

    # 🚀 Tendência iniciando (5m)
    if interval == "5m" and ema9 > ma20 > ma50 and rsi > 55 and volume > volume_media * 1.5:
        msg = f"🚀 <b>{symbol}</b> | Tendência iniciando (5m)\n💰 {preco_atual}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        await enviar_alerta(msg)
        cooldowns[symbol] = time.time()

    # ⚡ Entrada explosiva (5m)
    if interval == "5m" and ema9 > ma20 > ma50 and volume > volume_media * 3 and rsi > 60:
        msg = f"⚡ <b>{symbol}</b> | Entrada explosiva (5m)\n💥 Volume 3x acima da média\n💰 {preco_atual}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        await enviar_alerta(msg)
        cooldowns[symbol] = time.time()

    # 🌕 Tendência confirmada (15m)
    if interval == "15m" and ema9 > ma20 > ma50 > ma200 and rsi > 55:
        msg = f"🌕 <b>{symbol}</b> | Tendência confirmada (15m)\n💰 {preco_atual}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        await enviar_alerta(msg)
        cooldowns[symbol] = time.time()

async def main():
    async with aiohttp.ClientSession() as sessao:
        pares = await pegar_pares_spot(sessao)
        print(f"✅ Monitorando {len(pares)} pares SPOT")
        while True:
            for interval in INTERVALOS:
                tasks = [analisar_moeda(sessao, s, interval) for s in pares[:TOP_PAIRS]]
                await asyncio.gather(*tasks)
            await asyncio.sleep(60)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(main())
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
