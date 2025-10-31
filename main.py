# main.py — LONGSETUP CONFIRMADO V2.3 (CRUZAMENTO 5M)
# Entrada: 1D+4H+1H + CRUZAMENTO EMA9 x EMA20 SÓ NO 5M
# Stop 1h | Alvo 1:3 e 1:5 | Saída MACD 1D < 0

import os, asyncio, aiohttp, time, threading
from datetime import datetime, timedelta
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
TOP_N = 80
REQ_TIMEOUT = 10
COOLDOWN_SEC = 15 * 60  # 15 min

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Scanner ativo (LongSetup V2.3) — Cruzamento 5M + Tendência Longa", 200

@app.route("/health")
def health():
    return "OK", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " BR"

async def tg(session, text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[TG FALHOU] Token ou Chat ID ausente!")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with session.post(url, data=payload, timeout=10) as resp:
            data = await resp.json()
            if resp.status == 200 and data.get("ok"):
                print(f"[TG ENVIADO] {text.split(chr(10))[0]}...")
                return True
            else:
                print(f"[TG ERRO] {resp.status} | {data.get('description', '')}")
                return False
    except Exception as e:
        print(f"[TG EXCEÇÃO] {e}")
        return False

def fmt_price(x: float) -> str:
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"

def sma(seq, n):
    if len(seq) < n: return [0.0] * len(seq)
    from collections import deque
    out, s, q = [], 0.0, deque()
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq: return []
    alpha = 2.0 / (span + 1.0)
    out, e = [seq[0]], seq[0]
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def calc_rsi(seq, period=14):
    if len(seq) < period + 1:
        return [50.0] * len(seq)
    gains, losses = [], []
    for i in range(1, len(seq)):
        diff = seq[i] - seq[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    rsi, avg_gain = [], sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / (avg_loss + 1e-12)
    rsi.append(100 - (100 / (1 + rs)))
    for i in range(period, len(seq) - 1):
        diff = seq[i] - seq[i - 1]
        gain, loss = max(diff, 0), abs(min(diff, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi.append(100 - (100 / (1 + rs)))
    return [50.0] * (len(seq) - len(rsi)) + rsi

def macd_hist(seq):
    if len(seq) < 35: return 0.0
    ema_fast = ema(seq, 12)
    ema_slow = ema(seq, 26)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal = ema(macd_line, 9)
    return macd_line[-1] - signal[-1] if len(signal) > 0 else 0.0

# ---------------- FUNÇÃO DE CRUZAMENTO (SÓ NO 5M) ----------------
def cruzou_de_baixo(c, p9=9, p20=20):
    if len(c) < p20 + 1: return False
    e9 = ema(c, p9)
    e20 = ema(c, p20)
    return e9[-1] > e20[-1] and e9[-2] <= e20[-2]

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=210):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[ERRO KLINE] {symbol} {interval}: {e}")
        return []

async def get_valid_spot_usdt_symbols(session):
    try:
        async with session.get(f"{BINANCE_HTTP}/api/v3/exchangeInfo", timeout=REQ_TIMEOUT) as r:
            info = await r.json()
    except:
        return set()
    valid = set()
    for s in info.get("symbols", []):
        try:
            if s.get("status") != "TRADING": continue
            if s.get("quoteAsset") != "USDT": continue
            name = s.get("symbol", "")
            if any(x in name for x in ("UP","DOWN","BULL","BEAR","PERP","_PERP")): continue
            valid.add(name)
        except: continue
    return valid

async def get_top_usdt_symbols(session):
    valid = await get_valid_spot_usdt_symbols(session)
    try:
        async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=REQ_TIMEOUT) as r:
            data = await r.json()
    except:
        data = []
    blocked = ("BUSD","FDUSD","TUSD","USDC","USDP","USD1","USDE","XUSD","USDX","GUSD","EUR","EURS","CEUR","BRL","TRY","STABLE","TEST")
    pares = []
    for d in data if isinstance(data, list) else []:
        s = d.get("symbol", "")
        if s not in valid or not s.endswith("USDT"): continue
        if any(x in s for x in blocked): continue
        qv = float(d.get("quoteVolume", "0") or 0.0)
        if qv >= 15_000_000:
            pares.append((s, qv))
    pares.sort(key=lambda x: x[1], reverse=True)
    print(f"[INFO] {len(pares[:TOP_N])} pares USDT válidos (TOP {TOP_N})")
    return [s for s, _ in pares[:TOP_N]]

# ---------------- ESTADO DE POSIÇÃO E ALERTA ----------------
POSICOES = {}
LAST_HIT = {}

def allowed(symbol, kind, cd=COOLDOWN_SEC):
    ts = LAST_HIT.get((symbol, kind), 0.0)
    return (time.time() - ts) >= cd

def mark(symbol, kind):
    LAST_HIT[(symbol, kind)] = time.time()

# ---------------- WORKER (COM CRUZAMENTO SÓ NO 5M) ----------------
async def scan_symbol(session, symbol):
    try:
        # === DADOS 1D ===
        k1d = await get_klines(session, symbol, "1d", 200)
        if len(k1d) < 50: return
        c1d = [float(x[4]) for x in k1d]
        v1d = [float(x[5]) for x in k1d]
        close_1d = c1d[-1]
        ema9_1d = ema(c1d, 9)[-1]
        ema21_1d = ema(c1d, 21)[-1]
        ema200_1d = ema(c1d, 200)[-1]
        rsi_1d = calc_rsi(c1d)[-1]
        macd_hist_1d = macd_hist(c1d)
        vol_med_20 = sum(v1d[-20:]) / 20
        vol_atual = v1d[-1]

        # === DADOS 4H ===
        k4h = await get_klines(session, symbol, "4h", 100)
        if len(k4h) < 50: return
        c4h = [float(x[4]) for x in k4h]
        ema9_4h = ema(c4h, 9)[-1]
        ema21_4h = ema(c4h, 21)[-1]
        ema50_4h = ema(c4h, 50)[-1]
        macd_hist_4h = macd_hist(c4h)
        close_4h = c4h[-1]

        # === DADOS 1H ===
        k1h = await get_klines(session, symbol, "1h", 100)
        if len(k1h) < 50: return
        c1h = [float(x[4]) for x in k1h]
        ema9_1h = ema(c1h, 9)[-1]
        ema21_1h = ema(c1h, 21)[-1]
        ema20_1h = ema(c1h, 20)[-1]
        macd_hist_1h = macd_hist(c1h)
        close_1h = c1h[-1]

        # === DADOS 5M (CRUZAMENTO AQUI) ===
        k5m = await get_klines(session, symbol, "5m", 100)
        if len(k5m) < 50: return
        c5m = [float(x[4]) for x in k5m]
        v5m = [float(x[5]) for x in k5m]
        vol_med_5m = sum(v5m[-10:]) / 10

        # === CRUZAMENTO SÓ NO 5M ===
        cruzou_5m = cruzou_de_baixo(c5m, 9, 20)

        # === FILTROS DE FORÇA (EMA9 > EMA20 NOS OUTROS TFs) ===
        tendencia_3m = ema(await get_klines(session, symbol, "3m", 100) and [float(x[4]) for x in await get_klines(session, symbol, "3m", 100)], 9)[-1] > ema(await get_klines(session, symbol, "3m", 100) and [float(x[4]) for x in await get_klines(session, symbol, "3m", 100)], 20)[-1] if len(c5m) > 0 else False
        tendencia_15m = ema(c1h, 9)[-1] > ema(c1h, 20)[-1]  # Aproveita 1h como proxy
        tendencia_30m = ema(c4h, 9)[-1] > ema(c4h, 20)[-1]  # Aproveita 4h
        tendencia_1h = ema9_1h > ema21_1h

        # === VOLUME E MACD NO 5M ===
        volume_forte = v5m[-1] > vol_med_5m * 1.3
        macd_subindo = macd_hist(c5m) > 0 and macd_hist(c5m) > (macd_hist(c5m[:-1]) if len(c5m) > 1 else -999)

        # === CONDIÇÕES TRIPLA CONFIRMAÇÃO (MANTIDAS) ===
        cond_1d = (
            ema9_1d > ema21_1d and
            close_1d > ema200_1d and
            45 <= rsi_1d <= 70 and
            macd_hist_1d > 0 and
            vol_atual > vol_med_20 * 1.2
        )
        conf_4h = sum([ema9_4h > ema21_4h, macd_hist_4h > 0, close_4h > ema50_4h]) >= 2
        conf_1h = sum([ema9_1h > ema21_1h, macd_hist_1h > 0, close_1h > ema20_1h]) >= 2

        # === ENTRADA: CRUZAMENTO 5M + TUDO ALINHADO ===
        if (cruzou_5m and 
            cond_1d and conf_4h and conf_1h and
            volume_forte and macd_subindo and
            tendencia_3m and tendencia_15m and tendencia_30m and tendencia_1h and
            allowed(symbol, "CRUZAMENTO_5M")):

            swing_low = min(float(x[3]) for x in k1h[-5:])
            stop = swing_low * 0.995
            risco = close_1h - stop
            alvo_1 = close_1h + 3 * risco
            alvo_2 = close_1h + 5 * risco

            POSICOES[symbol] = {
                "entry": close_1h,
                "stop": stop,
                "alvo1": alvo_1,
                "alvo2": alvo_2,
                "ativo": True
            }

            msg = (
                f"<b>CRUZAMENTO 5M + TRIPLA!</b>\n"
                f"<b>{symbol}</b>\n"
                f"Preço: ${fmt_price(close_1h)}\n"
                f"Stop: ${fmt_price(stop)}\n"
                f"Alvo 1: ${fmt_price(alvo_1)} (+{((alvo_1/close_1h)-1)*100:.1f}%)\n"
                f"Alvo 2: ${fmt_price(alvo_2)} (+{((alvo_2/close_1h)-1)*100:.1f}%)\n"
                f"{now_br()}\n"
                f"<a href='https://www.binance.com/en/trade/{symbol}'>ABRIR</a>"
            )
            if await tg(session, msg):
                mark(symbol, "CRUZAMENTO_5M")
                print(f"ALERTA CRUZAMENTO 5M: {symbol}")

        # === SAÍDA AUTOMÁTICA (MACD 1D VIRA NEGATIVO) ===
        if symbol in POSICOES and POSICOES[symbol]["ativo"]:
            if macd_hist_1d <= 0:
                entry = POSICOES[symbol]["entry"]
                lucro = ((close_1d - entry) / entry) * 100
                msg = (
                    f"<b>SAÍDA AUTOMÁTICA</b>\n"
                    f"<b>{symbol}</b>\n"
                    f"Entrada: ${fmt_price(entry)}\n"
                    f"Saída: ${fmt_price(close_1d)}\n"
                    f"Lucro: {lucro:+.1f}%\n"
                    f"Motivo: MACD 1D virou negativo\n"
                    f"{now_br()}"
                )
                if await tg(session, msg):
                    POSICOES[symbol]["ativo"] = False

    except Exception as e:
        print(f"[ERRO SCAN] {symbol}: {e}")

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        await tg(session, f"<b>BOT LONGSETUP V2.3 INICIADO (CRUZAMENTO 5M)</b>\n{now_br()}")
        print(f"[{now_br()}] BOT V2.3 INICIADO | Telegram: {'OK' if TELEGRAM_TOKEN and CHAT_ID else 'NOK'}")
        while True:
            start = time.time()
            symbols = await get_top_usdt_symbols(session)
            if not symbols:
                await asyncio.sleep(30)
                continue
            print(f"[{now_br()}] Escaneando {len(symbols)} pares...")
            await asyncio.gather(*[scan_symbol(session, s) for s in symbols])
            elapsed = time.time() - start
            print(f"[{now_br()}] Scan concluído em {elapsed:.1f}s. Próximo em 5 min...")
            await asyncio.sleep(300)

# ---------------- EXECUÇÃO FINAL ----------------
def start_bot():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception as e:
            print(f"[ERRO FATAL] {e}. Reiniciando em 5s...")
            time.sleep(5)

def run_flask_with_thread():
    print("[INFO] Inicializando BOT LONGSETUP V2.3 (CRUZAMENTO 5M)...")
    t = threading.Thread(target=start_bot, daemon=True)
    t.start()
    print("[INFO] Thread principal do BOT iniciada.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT") or 10000))

run_flask_with_thread()
