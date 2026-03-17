import os, asyncio, aiohttp, time, math
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V9.1 - MOLA ARMADA (Filtro Blacklist Atualizado)", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 1_000_000 
TOP_N = 80
SCAN_INTERVAL = 30

# Configurações da Estratégia
BB_PERIOD = 20
BB_STD = 2.0
STOCH_PERIOD = 14

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg); return
    try:
        await s.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print("Erro Telegram:", e)

cooldown_mola = {}

def can_alert_mola(sym):
    n = time.time()
    if n - cooldown_mola.get(sym, 0) >= 1200: 
        cooldown_mola[sym] = n
        return True
    return False

def get_sma(data, window):
    if len(data) < window: return 0
    return sum(data[-window:]) / window

def get_bollinger(data, window, std_dev):
    sma = get_sma(data, window)
    variance = sum([(x - sma)**2 for x in data[-window:]]) / window
    stdev = math.sqrt(variance)
    return sma + (std_dev * stdev), sma - (std_dev * stdev), (std_dev * stdev * 2 / sma) * 100

def get_stoch_rsi(data, period):
    if len(data) < period * 2: return 50
    deltas = [data[i+1] - data[i] for i in range(len(data)-1)]
    up = [x if x > 0 else 0 for x in deltas]
    down = [-x if x < 0 else 0 for x in deltas]
    avg_gain = sum(up[-(period):]) / period
    avg_loss = sum(down[-(period):]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def scan_mola_armada(s, sym):
    try:
        async with s.get(f"{BINANCE}/api/v3/klines?symbol={sym}&interval=15m&limit=100", timeout=10) as r:
            k = await r.json() if r.status == 200 else []
        if len(k) < 60: return
        closes = [float(x[4]) for x in k]
        lows = [float(x[3]) for x in k]
        price, ma200 = closes[-1], sum(closes[-100:]) / 100
        upper, lower, width = get_bollinger(closes, BB_PERIOD, BB_STD)
        rsi = get_stoch_rsi(closes, STOCH_PERIOD)
        
        # Lógica Mola Armada: Acima da MA200 + Compressão < 1.6% + Fundo Ascendente
        is_compressed = width < 1.6 
        stopped_dropping = lows[-1] > lower and lows[-2] > lower and (price - lower) / lower * 100 < 0.6
        
        if price > ma200 and is_compressed and stopped_dropping and 35 < rsi < 65 and can_alert_mola(sym):
            nome = sym.replace("USDT", "")
            msg = (
                f"🚀 <b>PROJETO MOLA ARMADA</b>\n\n"
                f"🔥 Moeda: <b>#{nome}</b>\n"
                f"📊 Compressão: <code>{width:.2f}%</code>\n"
                f"💎 Preço: {price:.6f}\n"
                f"📉 StochRSI: {rsi:.1f}\n\n"
                f"🎯 <i>Insight: Preço parou de buscar a banda inferior. Mola pronta!</i>\n"
                f"⏰ Hora: {now_br()} BR"
            )
            await tg(s, msg)
    except: pass

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V9.1 - SENTINELA: MOLA ARMADA (Filtros Estendidos)</b>")
        while True:
            try:
                data_resp = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if data_resp.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL); continue
                data = await data_resp.json()
                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume", 0) or 0) >= MIN_VOL24
                    and not any(x in d["symbol"] for x in [
                        "UP","DOWN","BUSD","FDUSD","USDC","TUSD",
                        "EUR","USDE","USD1","XUSD","TRY","GBP","BRL"
                    ])
                ]
                symbols = sorted(symbols, key=lambda x: next((float(t.get("quoteVolume", 0) or 0) for t in data if t["symbol"] == x), 0), reverse=True)[:TOP_N]
                await asyncio.gather(*[scan_mola_armada(s, sym) for sym in symbols])
            except Exception as e: print("Erro:", e)
            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
asyncio.run(main_loop())
