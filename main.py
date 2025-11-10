# main.py — V7 OURO CONFLUÊNCIA REAL (Tendência Curta)
# Timeframes: 15m, 30m, 1h
# Fluxo: Rompimento EMA200 → Reteste (EMA50/EMA100/EMA200) → Continuação
# Filtros: RSI, MACD, VolumeStrength, Book (takerBuy vs takerSell), Confluência entre tempos
# Cooldown: 15m=15min, 30m=30min, 1h=60min

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading
from math import inf

app = Flask(__name__)
@app.route("/")
def home():
    return "V7 OURO CONFLUÊNCIA REAL (TENDÊNCIA CURTA) ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ===== PARÂMETROS =====
MIN_VOL24 = 10_000_000  # 10M USDT (moedas mortas/ fracas ficam fora)
TOP_N = 100
COOLDOWN = {"15m": 15*60, "30m": 30*60, "1h": 60*60}  # 1 alerta por candle
RETEST_TOL = 0.003  # 0,3% de tolerância para considerar "toque" de média
VOL_STRENGTH_MIN_BREAK = 120  # % vs média (rompimento)
VOL_STRENGTH_MIN_CONT  = 110  # % vs média (continuação)
BOOK_DOMINANCE_BREAK = 1.20   # takerBuyQuote >= 1.2 * takerSellQuote (romp.)
BOOK_DOMINANCE_CONT  = 1.10   # takerBuy atual >= 1.1 * takerBuy recente (cont.)
RSI_MIN_BREAK = 50
RSI_MIN_RETEST_HOLD = 45
RSI_MIN_CONT = 50

# ===== HELPERS =====
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg); return
    try:
        await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                     timeout=10)
    except Exception as e:
        print("Erro Telegram:", e)

def ema(data, p):
    if not data: return []
    a = 2/(p+1); e = data[0]; out = [e]
    for x in data[1:]:
        e = a*x + (1-a)*e; out.append(e)
    return out

def sma(data, p):
    if len(data) < p: return []
    out = []
    s = sum(data[:p]); out.append(s/p)
    for i in range(p, len(data)):
        s += data[i] - data[i-p]
        out.append(s/p)
    return out

def rsi(prices, p=14):
    if len(prices) < p+1: return 50
    d = [prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains = [max(x,0) for x in d[-p:]]
    losses = [abs(min(x,0)) for x in d[-p:]]
    ag, al = (sum(gains)/p), (sum(losses)/p or 1e-12)
    return 100 - 100/(1 + ag/al)

def macd_vals(close):
    # MACD (12,26,9)
    ema12 = ema(close, 12)
    ema26 = ema(close, 26)
    if not ema12 or not ema26: return 0.0, 0.0, 0.0
    macd_line = ema12[-1] - ema26[-1]
    signal_line = ema(ema12[-26:], 9)[-1] if len(ema12) >= 26 else ema(ema12, 9)[-1]
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def volume_strength(series):
    if len(series) < 21: return 100.0
    ma9 = sum(series[-9:])/9
    ma21 = sum(series[-21:])/21
    base = (ma9 + ma21)/2 or 1e-12
    return (series[-1]/base) * 100

def taker_split_24h(t24):
    vol_quote = float(t24.get("quoteVolume", 0) or 0.0)
    taker_buy_q = float(t24.get("takerBuyQuoteAssetVolume", 0) or 0.0)
    taker_sell_q = max(vol_quote - taker_buy_q, 0.0)
    return vol_quote, taker_buy_q, taker_sell_q

def touched(avg, low, close):
    # preço tocou média (ou ficou muito perto) e não perdeu na varredura
    return (low <= avg*(1+RETEST_TOL)) and (close >= avg*(1-RETEST_TOL))

def is_green_candle(kline):
    o = float(kline[1]); c = float(kline[4])
    return c > o

def broke_prev_high(curr, prev):
    curr_close = float(curr[4]); prev_high = float(prev[2])
    return curr_close > prev_high

# Cooldown por par/timeframe/tipo
cooldown = {"15m": {}, "30m": {}, "1h": {}}
def can_alert(tf, sym, kind):
    key = f"{sym}:{kind}"
    n = time.time()
    last = cooldown[tf].get(key, 0)
    if n - last >= COOLDOWN[tf]:
        cooldown[tf][key] = n
        return True
    return False

# Status por par/timeframe para ciclo (rompimento → reteste → continuação)
state = {
    # ("SYMBOL","15m"): {"broke200": False, "last_taker_buy": 0.0, "last_kline_time": 0}
}

def ensure_state(key):
    if key not in state:
        state[key] = {"broke200": False, "last_taker_buy": 0.0, "watch_retest": False}

# ===== CORE DE VARREDURA =====
async def klines(s, sym, tf, lim=200):
    url = f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit={lim}"
    async with s.get(url, timeout=10) as r:
        return await r.json() if r.status == 200 else []

async def ticker24(s, sym):
    url = f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}"
    async with s.get(url, timeout=10) as r:
        return await r.json() if r.status == 200 else None

def confluence_allow(tf, sym, cache):
    """Confluência: 15m precisa 30m e 1h verdes; 30m precisa 1h verde; 1h livre."""
    if tf == "1h": return True
    ok_1h = cache.get("1h_macd_pos", False)
    if tf == "30m":
        return ok_1h
    if tf == "15m":
        ok_30 = cache.get("30m_macd_pos", False)
        return ok_30 and ok_1h
    return False

async def scan_tf(s, sym, tf, caches):
    try:
        t24 = await ticker24(s, sym)
        if not t24: return
        p = float(t24["lastPrice"])
        vol24 = float(t24["quoteVolume"])
        if vol24 < MIN_VOL24: return

        k = await klines(s, sym, tf, 100)
        if len(k) < 50: return

        close = [float(x[4]) for x in k]
        low   = [float(x[3]) for x in k]
        vol   = [float(x[5]) for x in k]

        ema50 = ema(close, 50)[-1] if len(close)>=50 else inf
        ema100= ema(close,100)[-1] if len(close)>=100 else inf
        ema200= ema(close,200)[-1] if len(close)>=200 else inf

        r = rsi(close)
        macd_line, signal_line, hist = macd_vals(close)
        macd_pos = macd_line > 0 and hist >= 0

        vs = volume_strength(vol)
        vol_quote, taker_buy_q, taker_sell_q = taker_split_24h(t24)

        # cache para confluência
        if tf == "1h":
            caches["1h_macd_pos"] = macd_pos
        if tf == "30m":
            caches["30m_macd_pos"] = macd_pos

        # estado do ciclo
        key = (sym, tf)
        ensure_state(key)

        # ===== 1) ROMPIMENTO EMA200 (marca ciclo) =====
        broke200_now = (close[-1] > ema200*(1+0.003)) and (r >= RSI_MIN_BREAK) and macd_pos and (vs >= VOL_STRENGTH_MIN_BREAK)
        book_ok_break = (taker_buy_q >= taker_sell_q * BOOK_DOMINANCE_BREAK)
        if broke200_now and book_ok_break:
            state[key]["broke200"] = True
            state[key]["watch_retest"] = True
            state[key]["last_taker_buy"] = taker_buy_q
            # Alerta de rompimento sai em 30m/1h; em 15m só quando tiver confluência
            if tf in ("30m","1h") and confluence_allow(tf, sym, caches):
                if can_alert(tf, sym, "break"):
                    nome = sym.replace("USDT","")
                    msg = (
                        f"<b>⚡ TENDÊNCIA CURTA — ROMPIMENTO EMA200 ({tf.upper()})</b>\n\n"
                        f"{nome}\n\n"
                        f"Entrada sugerida: <b>{p:.6f}</b>\n"
                        f"RSI: <b>{r:.1f}</b> | MACD: <b>positivo</b>\n"
                        f"Vol força: <b>{vs:.0f}%</b>\n"
                        f"💰 Fluxo real: <b>{taker_buy_q:,.0f}</b> compradores vs <b>{taker_sell_q:,.0f}</b> vendedores\n"
                        f"<i>{now_br()} BR</i>"
                    )
                    await tg(s, msg)

        # ===== 2) RETESTE EM EMA50/EMA100/EMA200 (validação do chão) =====
        if state[key]["watch_retest"]:
            # vela anterior como vela de toque (penúltima)
            prev = k[-2]; curr = k[-1]
            prev_low  = float(prev[3]); prev_close = float(prev[4])

            touched50  = touched(ema50,  prev_low,  prev_close)
            touched100 = touched(ema100, prev_low,  prev_close)
            touched200 = touched(ema200, prev_low,  prev_close)
            touched_any = touched50 or touched100 or touched200

            hold_strength = (r >= RSI_MIN_RETEST_HOLD) and (macd_line >= 0)
            vol_pullback_ok = vol[-2] <= (sum(vol[-10:-1])/9 if len(vol)>=11 else vol[-2])

            if touched_any and hold_strength and vol_pullback_ok:
                which = "EMA50" if touched50 else ("EMA100" if touched100 else "EMA200")
                if confluence_allow(tf, sym, caches):
                    if can_alert(tf, sym, "retest"):
                        nome = sym.replace("USDT","")
                        msg = (
                            f"<b>🔁 TENDÊNCIA CURTA — RETESTE VALIDADO ({tf.upper()})</b>\n\n"
                            f"{nome}\n\n"
                            f"Média testada: <b>{which}</b>\n"
                            f"RSI: <b>{r:.1f}</b> | MACD: <b>{'positivo' if macd_pos else 'neutro'}</b>\n"
                            f"Vol do recuo: <b>ok</b>\n"
                            f"<i>{now_br()} BR</i>"
                        )
                        await tg(s, msg)
                # fica aguardando continuação

        # ===== 3) CONTINUAÇÃO (trigger de entrada) =====
        if state[key]["broke200"]:
            prev = k[-2]; curr = k[-1]
            cont_ok = is_green_candle(curr) and broke_prev_high(curr, prev) and (vs >= VOL_STRENGTH_MIN_CONT) and (r >= RSI_MIN_CONT) and macd_pos
            # Book precisa mostrar melhora (takerBuy atual > 1.1x último registro do ciclo)
            book_growth = taker_buy_q >= max(state[key]["last_taker_buy"] * BOOK_DOMINANCE_CONT, state[key]["last_taker_buy"]+1e-9)

            if cont_ok and book_growth and confluence_allow(tf, sym, caches):
                if can_alert(tf, sym, "continue"):
                    nome = sym.replace("USDT","")
                    # Stop = mínima das últimas 10 velas * 0.98
                    stop = min(float(x[3]) for x in k[-10:]) * 0.98
                    alvo1, alvo2 = p*1.025, p*1.05
                    msg = (
                        f"<b>🔥 TENDÊNCIA CURTA — CONTINUAÇÃO CONFIRMADA ({tf.upper()})</b>\n\n"
                        f"{nome}\n\n"
                        f"Entrada: <b>{p:.6f}</b>\n"
                        f"📉 Stop: <b>{stop:.6f}</b>\n"
                        f"🎯 Alvos: <b>{alvo1:.6f}</b> (+2.5%) | <b>{alvo2:.6f}</b> (+5%)\n"
                        f"RSI: <b>{r:.1f}</b> | MACD: <b>positivo</b> | Vol força: <b>{vs:.0f}%</b>\n"
                        f"💰 Fluxo real: <b>{taker_buy_q:,.0f}</b> vs <b>{taker_sell_q:,.0f}</b>\n"
                        f"<i>{now_br()} BR</i>"
                    )
                    await tg(s, msg)
                # reseta ciclo após continuação para evitar múltiplos encadeados
                state[key]["broke200"] = False
                state[key]["watch_retest"] = False
                state[key]["last_taker_buy"] = taker_buy_q

        # ===== CANCELAMENTO AUTOMÁTICO (perda de força) =====
        if state[key]["broke200"]:
            # invalida se perder EMA50 com força ou indicadores virarem
            lost_strength = (r < RSI_MIN_RETEST_HOLD) or (macd_line < 0 and hist < 0)
            below50 = close[-1] < ema50*(1-RETEST_TOL)
            if lost_strength or below50:
                state[key]["broke200"] = False
                state[key]["watch_retest"] = False
                state[key]["last_taker_buy"] = 0.0
                # (silencioso; se quiser avisar fragilidade, dá para enviar aqui)

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>TENDÊNCIA CURTA ATIVA</b>\nOURO Confluência Real: 15m/30m/1h + Book + Reteste 50/100/200")
        while True:
            try:
                data = await (await s.get(f"{BINANCE}/api/v3/ticker/24hr")).json()
                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d["quoteVolume"]) > MIN_VOL24
                    and (lambda base: not (
                        base.endswith("USD") or base in {
                            "BUSD","FDUSD","USDE","USDC","TUSD","CUSD",
                            "EUR","GBP","TRY","AUD","BRL","RUB","CAD","CHF","JPY",
                            "BF","BFC","BFG","BFD","BETA","AEUR","AUSD","CEUR","XAUT"
                        }
                    ))(d["symbol"][:-4])
                    and not any(x in d["symbol"] for x in ["UP","DOWN"])
                ]
                # top N por volume
                symbols = sorted(
                    symbols,
                    key=lambda x: next((float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0),
                    reverse=True
                )[:TOP_N]

                # cache simples para confluência (atualiza a cada laço)
                conf_cache = {"30m_macd_pos": False, "1h_macd_pos": False}
                # primeiro avalia 1h e 30m para preencher confluência
                pre_tasks = []
                for sym in symbols:
                    pre_tasks.append(scan_tf(s, sym, "1h", conf_cache))
                    pre_tasks.append(scan_tf(s, sym, "30m", conf_cache))
                await asyncio.gather(*pre_tasks)

                # depois avalia 15m usando confluência já calculada
                tasks = []
                for sym in symbols:
                    tasks.append(scan_tf(s, sym, "15m", conf_cache))
                await asyncio.gather(*tasks)

            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(60)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
