# main.py — V7.1 OURO CONFLUÊNCIA CURTA (Destravada)
# Timeframes: 15m, 30m, 1h
# Fluxo: Rompimento EMA200 → Reteste (EMA50/EMA100/EMA200) → Continuação
# Filtros: RSI, MACD (corrigido), VolumeStrength, Book (takerBuy vs takerSell)
# Confluência: FLEX (não bloqueia — apenas loga)
# Cooldown: 15m=15min, 30m=30min, 1h=60min
# Debug: prints detalhados para cada motivo de não-alerta

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading
from math import inf

app = Flask(__name__)
@app.route("/")
def home():
    return "V7.1 OURO CONFLUÊNCIA CURTA (Destravada) ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ===== PARÂMETROS (Destravados) =====
MIN_VOL24 = 3_000_000   # volume mínimo 24h
TOP_N = 150             # mais pares escaneados
COOLDOWN = {"15m": 15*60, "30m": 30*60, "1h": 60*60}  # 1 alerta por candle

RETEST_TOL = 0.003      # 0,3% tolerância de "toque" na média
BREAK_TOL = 0.0015      # 0,15% acima da EMA200 para considerar rompimento

VOL_STRENGTH_MIN_BREAK = 120  # % vs base (rompimento)
VOL_STRENGTH_MIN_CONT  = 110  # % vs base (continuação)

BOOK_DOMINANCE_BREAK = 1.10   # takerBuyQuote >= 1.1 * takerSellQuote (rompimento)
BOOK_DOMINANCE_CONT  = 1.10   # takerBuy atual >= 1.1 * takerBuy anterior (continuação)

RSI_MIN_BREAK = 50
RSI_MIN_RETEST_HOLD = 45
RSI_MIN_CONT = 50

# ===== HELPERS =====
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print("\n[TELEGRAM_SIM]\n" + msg + "\n"); return
    try:
        await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                     timeout=10)
    except Exception as e:
        print("Erro Telegram:", e)

def ema_series(data, p):
    if not data: return []
    a = 2/(p+1); e = data[0]; out = [e]
    for x in data[1:]:
        e = a*x + (1-a)*e; out.append(e)
    return out

def ema_last(data, p):
    s = ema_series(data, p)
    return s[-1] if s else 0.0

def sma_series(data, p):
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

def macd_correct(close):
    # MACD padrão 12/26/9 — sinal é EMA do macd_line (não do close!)
    if len(close) < 26: return 0.0, 0.0, 0.0
    ema12 = ema_series(close, 12)
    ema26 = ema_series(close, 26)
    macd_line_series = [a-b for a,b in zip(ema12[-len(ema26):], ema26)]
    signal_series = ema_series(macd_line_series, 9)
    macd_line = macd_line_series[-1]
    signal = signal_series[-1]
    hist = macd_line - signal
    return macd_line, signal, hist

def volume_strength(vol_series):
    n = len(vol_series)
    if n < 21: return 100.0
    ma9  = sum(vol_series[-9:])/9
    ma21 = sum(vol_series[-21:])/21
    base = (ma9 + ma21)/2 or 1e-12
    return (vol_series[-1]/base) * 100

def taker_split_24h(t24):
    vol_quote = float(t24.get("quoteVolume", 0) or 0.0)
    taker_buy_q = float(t24.get("takerBuyQuoteAssetVolume", 0) or 0.0)
    taker_sell_q = max(vol_quote - taker_buy_q, 0.0)
    return vol_quote, taker_buy_q, taker_sell_q

def touched(avg, low, close):
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

# Estado por par/timeframe para ciclo (rompimento → reteste → continuação)
state = {}
def ensure_state(key):
    if key not in state:
        state[key] = {"broke200": False, "last_taker_buy": 0.0, "watch_retest": False}

# Confluência (FLEX — não bloqueia; só loga)
confluence = {}
def set_confluence(sym, tf, macd_ok):
    confluence[(sym, tf)] = macd_ok

def get_confluence(sym, tf):
    return confluence.get((sym, tf), False)

# ===== CORE =====
async def klines(s, sym, tf, lim=200):
    url = f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit={lim}"
    async with s.get(url, timeout=10) as r:
        if r.status != 200:
            print(f"[{tf}] KLN {sym} HTTP {r.status}")
            return []
        return await r.json()

async def ticker24(s, sym):
    url = f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}"
    async with s.get(url, timeout=10) as r:
        if r.status != 200:
            print(f"[T24] {sym} HTTP {r.status}")
            return None
        return await r.json()

async def scan_tf(s, sym, tf):
    try:
        t24 = await ticker24(s, sym)
        if not t24:
            print(f"[{tf}] {sym} sem ticker24")
            return

        p = float(t24["lastPrice"])
        vol24 = float(t24["quoteVolume"])
        if vol24 < MIN_VOL24:
            print(f"[{tf}] {sym} skip: vol24 {vol24:,.0f} < {MIN_VOL24:,.0f}")
            return

        k = await klines(s, sym, tf, 120)
        if len(k) < 60:
            print(f"[{tf}] {sym} poucas klines ({len(k)})")
            return

        close = [float(x[4]) for x in k]
        low   = [float(x[3]) for x in k]
        vol   = [float(x[5]) for x in k]

        # Médias
        ema50 = ema_last(close, 50) if len(close)>=50 else inf
        ema100= ema_last(close,100) if len(close)>=100 else inf
        ema200= ema_last(close,200) if len(close)>=200 else inf

        # Indicadores
        r = rsi(close)
        macd_line, signal_line, hist = macd_correct(close)
        macd_pos = (macd_line > 0 and hist >= 0)
        vs = volume_strength(vol)
        vol_quote, taker_buy_q, taker_sell_q = taker_split_24h(t24)

        # Confluência (apenas informativa)
        if tf == "1h":
            set_confluence(sym, "1h", macd_pos)
        if tf == "30m":
            set_confluence(sym, "30m", macd_pos)
        log_conf = f"conf30={get_confluence(sym,'30m')} conf1h={get_confluence(sym,'1h')}"

        key = (sym, tf)
        ensure_state(key)

        # ===== 1) ROMPIMENTO EMA200 =====
        broke200_now = (close[-1] > ema200*(1+BREAK_TOL)) and (r >= RSI_MIN_BREAK) and macd_pos and (vs >= VOL_STRENGTH_MIN_BREAK)
        book_ok_break = (taker_buy_q >= taker_sell_q * BOOK_DOMINANCE_BREAK)

        if broke200_now and book_ok_break:
            state[key]["broke200"] = True
            state[key]["watch_retest"] = True
            state[key]["last_taker_buy"] = taker_buy_q

            if can_alert(tf, sym, "break"):
                nome = sym.replace("USDT","")
                msg = (
                    f"<b>⚡ TENDÊNCIA CURTA — ROMPIMENTO EMA200 ({tf.upper()})</b>\n\n"
                    f"{nome}\n\n"
                    f"Entrada sugerida: <b>{p:.6f}</b>\n"
                    f"RSI: <b>{r:.1f}</b> | MACD: <b>positivo</b>\n"
                    f"Vol força: <b>{vs:.0f}%</b>\n"
                    f"💰 Fluxo real: <b>{taker_buy_q:,.0f}</b> vs <b>{taker_sell_q:,.0f}</b>\n"
                    f"<i>{now_br()} BR | {log_conf}</i>"
                )
                await tg(s, msg)
        else:
            print(f"[{tf}] {sym} no-break "
                  f"(price>{ema200*(1+BREAK_TOL):.6f}? {close[-1]>ema200*(1+BREAK_TOL)} | "
                  f"RSI {r:.1f} ok? {r>=RSI_MIN_BREAK} | "
                  f"MACD+? {macd_pos} | "
                  f"Vol% {vs:.0f} >= {VOL_STRENGTH_MIN_BREAK}? {vs>=VOL_STRENGTH_MIN_BREAK} | "
                  f"Book {taker_buy_q:,.0f} >= 1.1*{taker_sell_q:,.0f}? {book_ok_break})")

        # ===== 2) RETESTE =====
        if state[key]["watch_retest"]:
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
                if can_alert(tf, sym, "retest"):
                    nome = sym.replace("USDT","")
                    msg = (
                        f"<b>🔁 TENDÊNCIA CURTA — RETESTE VALIDADO ({tf.upper()})</b>\n\n"
                        f"{nome}\n\n"
                        f"Média testada: <b>{which}</b>\n"
                        f"RSI: <b>{r:.1f}</b> | MACD: <b>{'positivo' if macd_pos else 'neutro'}</b>\n"
                        f"Vol do recuo: <b>ok</b>\n"
                        f"<i>{now_br()} BR | {log_conf}</i>"
                    )
                    await tg(s, msg)
            else:
                print(f"[{tf}] {sym} no-retest "
                      f"(touch={touched_any} [50={touched50} 100={touched100} 200={touched200}] | "
                      f"holdRSI/MACD={hold_strength} (RSI {r:.1f}, macd_line {macd_line:.5f}) | "
                      f"volPull={vol_pullback_ok})")

        # ===== 3) CONTINUAÇÃO =====
        if state[key]["broke200"]:
            prev = k[-2]; curr = k[-1]
            cont_ok = is_green_candle(curr) and broke_prev_high(curr, prev) and (vs >= VOL_STRENGTH_MIN_CONT) and (r >= RSI_MIN_CONT) and (macd_line > 0 and hist >= 0)
            book_growth = taker_buy_q >= max(state[key]["last_taker_buy"] * BOOK_DOMINANCE_CONT, state[key]["last_taker_buy"]+1e-9)

            if cont_ok and book_growth:
                if can_alert(tf, sym, "continue"):
                    nome = sym.replace("USDT","")
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
                        f"<i>{now_br()} BR | {log_conf}</i>"
                    )
                    await tg(s, msg)

                # encerra ciclo para evitar múltiplos encadeados
                state[key]["broke200"] = False
                state[key]["watch_retest"] = False
                state[key]["last_taker_buy"] = taker_buy_q
            else:
                print(f"[{tf}] {sym} no-continue "
                      f"(green={is_green_candle(curr)} | breakPrevHigh={broke_prev_high(curr, prev)} | "
                      f"Vol% {vs:.0f}>= {VOL_STRENGTH_MIN_CONT}? {vs>=VOL_STRENGTH_MIN_CONT} | "
                      f"RSI {r:.1f}>= {RSI_MIN_CONT}? {r>=RSI_MIN_CONT} | "
                      f"MACD+? {(macd_line>0 and hist>=0)} | "
                      f"BookGrowth {taker_buy_q:,.0f} >= 1.1*last({state[key]['last_taker_buy']:,.0f})? {book_growth})")

        # ===== CANCELAMENTO =====
        if state[key]["broke200"]:
            lost_strength = (r < RSI_MIN_RETEST_HOLD) or (macd_line < 0 and hist < 0)
            below50 = close[-1] < ema50*(1-RETEST_TOL)
            if lost_strength or below50:
                print(f"[{tf}] {sym} cancel-cycle (lost={lost_strength} below50={below50})")
                state[key]["broke200"] = False
                state[key]["watch_retest"] = False
                state[key]["last_taker_buy"] = 0.0

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V7.1 ATIVO — TENDÊNCIA CURTA (Destravada)</b>\n15m/30m/1h + Book + Reteste 50/100/200\nConfluência FLEX (não bloqueia)")
        while True:
            try:
                resp = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if resp.status != 200:
                    print(f"[TICKER LIST] HTTP {resp.status}")
                    await asyncio.sleep(60); continue
                data = await resp.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d["quoteVolume"] or 0) > MIN_VOL24
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
                    key=lambda x: next((float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0.0),
                    reverse=True
                )[:TOP_N]

                print(f"[SCAN] {len(symbols)} pares: exemplo {symbols[:10]}")

                tasks = []
                for sym in symbols:
                    # Rodamos 1h, 30m e 15m — confluência é só log
                    tasks.append(scan_tf(s, sym, "1h"))
                    tasks.append(scan_tf(s, sym, "30m"))
                    tasks.append(scan_tf(s, sym, "15m"))
                await asyncio.gather(*tasks)

            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(60)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
