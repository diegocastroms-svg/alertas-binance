# -*- coding: utf-8 -*-
# v3.final — intrabar + volume, limite TOP50, USDT spot, keep-alive Flask
# ⚠️ Variáveis de ambiente necessárias:
# TELEGRAM_TOKEN, CHAT_ID, PORT (opcional no Render)

import os, asyncio, aiohttp, time, math
from datetime import datetime, timezone
from collections import defaultdict
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVALS = ("5m", "15m")
TOP_N = 50                 # << limite de 50 pares por maior volume
COOLDOWN_LOOP = 15 * 60    # heartbeat do bot
HEARTBEAT_TAG = "v3.final"

# filtros de lista 24h (somente USDT spot; evita tokens alavancados/stable)
EXCLUDE_TOKENS = ("UP", "DOWN", "BUSD", "FDUSD", "TUSD", "USDC", "USD1")

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

app = Flask(__name__)

# -------------- Utils & Telegram --------------
def now_br():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def fmt(n):
    try:
        return f"{float(n):.6f}".rstrip("0").rstrip(".")
    except:
        return str(n)

async def tg_send(session, text):
    if not TOKEN or not CHAT_ID:
        print("! TELEGRAM_TOKEN/CHAT_ID ausentes")
        return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
        await session.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Erro tg_send:", e)

# -------------- Indicadores --------------
def sma(vals, p):
    out = []
    acc = 0.0
    for i, v in enumerate(vals):
        acc += v
        if i >= p: acc -= vals[i - p]
        out.append(acc / p if i + 1 >= p else acc / (i + 1))
    return out

def ema(vals, p):
    k = 2.0 / (p + 1.0)
    out = []
    for i, v in enumerate(vals):
        if i == 0: out.append(v)
        else:      out.append(v * k + out[-1] * (1 - k))
    return out

def rsi(vals, p=14):
    gains, losses = 0.0, 0.0
    out = [50.0]
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i-1]
        gains = (gains*(p-1) + max(ch, 0)) / p
        losses = (losses*(p-1) + max(-ch, 0)) / p
        rs = gains / losses if losses != 0 else 999.0
        out.append(100 - (100/(1+rs)))
    return out

def crossed_up(a, b):
    return len(a) > 1 and len(b) > 1 and a[-2] < b[-2] and a[-1] >= b[-1]

# -------------- Binance --------------
async def http_json(session, url):
    async with session.get(url, timeout=15) as r:
        return await r.json()

async def klines(session, symbol, interval, limit=210):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return await http_json(session, url)

async def shortlist_top50_usdt(session):
    # pega 24h e mantém somente USDT spot, ordenando por quoteVolume desc
    data = await http_json(session, f"{BINANCE_HTTP}/api/v3/ticker/24hr")
    rows = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"): continue
        if any(x in s for x in EXCLUDE_TOKENS): continue
        try:
            qv = float(d.get("quoteVolume", "0"))
            rows.append((qv, s))
        except:
            continue
    rows.sort(reverse=True)
    return [s for _, s in rows[:TOP_N]]

# -------------- Lógica de Alertas --------------
_last_sent = defaultdict(dict)  # _last_sent[symbol][key] = ts

def should_send(symbol, key, ttl=60*60):
    """Evita duplicar alerta do mesmo tipo por símbolo por 'ttl' segundos."""
    now = time.time()
    last = _last_sent[symbol].get(key, 0)
    if now - last >= ttl:
        _last_sent[symbol][key] = now
        return True
    return False

async def process_symbol(session, symbol):
    try:
        k5  = await klines(session, symbol, "5m")
        k15 = await klines(session, symbol, "15m")
        if not (isinstance(k5, list) and isinstance(k15, list) and len(k5) > 0 and len(k15) > 0):
            return

        # closes e volumes (klines: [.., close, volume, .., quoteAssetVolume, ..])
        c5   = [float(x[4]) for x in k5]
        v5_q = [float(x[7]) for x in k5]    # volume em moeda de cotação
        c15  = [float(x[4]) for x in k15]

        # MAs/EMAs
        ema9_5  = ema(c5, 9)
        ma20_5  = sma(c5, 20)
        ma50_5  = sma(c5, 50)
        ma200_5 = sma(c5, 200)

        ema9_15  = ema(c15, 9)
        ma20_15  = sma(c15, 20)
        ma50_15  = sma(c15, 50)
        ma200_15 = sma(c15, 200)

        # Volume médio (qv) para gatilho de antecipação intrabar
        v5_ma20 = sma(v5_q, 20)
        vol_boost = v5_q[-1] > 1.3 * v5_ma20[-1]  # 30% acima do médio

        # (Opcional) leitura de exaustão/lateralização via RSI — só como gatilho, sem bloquear cruzamento
        rsi5 = rsi(c5, 14)
        exaustao_ok = rsi5[-2] < 45 and rsi5[-1] > rsi5[-2]

        # -------- CONDIÇÕES PRINCIPAIS (apenas cruzamentos, como você pediu) --------
        iniciar_5m  = crossed_up(ema9_5, ma20_5) or crossed_up(ema9_5, ma50_5)

        pre_5m      = (crossed_up(ma20_5, ma200_5) or crossed_up(ma50_5, ma200_5)) \
                      and (ema9_5[-1] >= ma20_5[-1] and ema9_5[-1] >= ma50_5[-1])

        pre_15m     = crossed_up(ema9_15, ma200_15)
        conf_15m    = (ema9_15[-1] > ma20_15[-1] > ma50_15[-1] > ma200_15[-1])

        # -------- GATILHO INTRABAR (adianta o "Tendência iniciando (5m)") --------
        # Só dispara se houve cruzamento da EMA9 com MA20/50 na vela corrente
        # e houver sinal de fluxo (volume acima da média) OU exaustão aliviando.
        iniciar_5m_intrabar = iniciar_5m and (vol_boost or exaustao_ok)

        price = fmt(c5[-1])
        hora  = now_br()

        # Envio das mensagens (sem duplicar por 60 min/sinal)
        if iniciar_5m_intrabar and should_send(symbol, "ini5"):
            await tg_send(session, f"🟢 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {price}\n🕒 {hora}")

        if pre_5m and should_send(symbol, "pre5"):
            await tg_send(session, f"🟡 {symbol} ⬆️ Tendência pré-confirmada (5m)\n💰 {price}\n🕒 {hora}")

        if pre_15m and should_send(symbol, "pre15"):
            await tg_send(session, f"🟡 {symbol} ⬆️ Tendência pré-confirmada (15m)\n💰 {price}\n🕒 {hora}")

        if conf_15m and should_send(symbol, "conf15"):
            await tg_send(session, f"🚀 {symbol} ⬆️ Tendência confirmada (15m)\n💰 {price}\n🕒 {hora}")

    except Exception as e:
        print(f"Erro {symbol}:", e)

# -------------- Loop Principal --------------
async def scan_once():
    async with aiohttp.ClientSession() as session:
        symbols = await shortlist_top50_usdt(session)
        await tg_send(session, f"✅ {HEARTBEAT_TAG} intrabar ativo | {len(symbols)} pares SPOT | cooldown 15m | {now_br()} 🇧🇷")
        if not symbols:
            print("! Nenhum par após filtro")
            return
        await asyncio.gather(*(process_symbol(session, s) for s in symbols))

def background_runner():
    while True:
        try:
            asyncio.run(scan_once())
        except Exception as e:
            print("Loop error:", e)
        time.sleep(COOLDOWN_LOOP)

# -------------- Flask keep-alive --------------
@app.route("/")
def home():
    return f"Binance Alertas {HEARTBEAT_TAG} ativo", 200

if __name__ == "__main__":
    import threading
    threading.Thread(target=background_runner, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
