# main.py — V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA (com Reteste Antecipado)
# Timeframes: 15m (Entrada Antecipada + Reteste Antecipado), 30m/1h (Rompimento), 1h (Continuação)
# Filtros: RSI, MACD (12/26/9), VolumeStrength (MA9/MA21), Book (takerBuy vs takerSell)
# Cooldown: 15 minutos para todos os TF
# Volume mínimo: 10M USDT (24h)
# Top N por volume: 50
# Scan: 30s
#
# ***ATUALIZAÇÃO:***
# 1) ALERTAS SUBSTITUÍDOS (textos padronizados): ENTRADA ANTECIPADA (15m), ROMPIMENTO (30m/1h), RETESTE ANTECIPADO (15m), CONTINUAÇÃO (1h)
# 2) LOGS ADICIONADOS (Render): símbolo, TF, RSI, MACD, VolStrength, Book, evento disparado, envio OK
# >>> LÓGICA INALTERADA <<<

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading
from math import inf

app = Flask(__name__)
@app.route("/")
def home():
    return "V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA (Reteste Antecipado) ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ===== PARÂMETROS =====
MIN_VOL24 = 10_000_000
TOP_N = 50
SCAN_INTERVAL = 30  # segundos

# cooldown 15 minutos para todos
COOLDOWN = {"15m": 15*60, "30m": 15*60, "1h": 15*60}

# tolerâncias e limiares
BREAK_TOL = 0.0015        # 0,15% acima da média base p/ romper
RETEST_TOL = 0.005        # 0,5% de aproximação/“toque” da média no reteste

VOL_STRENGTH_MIN_EARLY = 120  # entrada antecipada (15m)
VOL_STRENGTH_MIN_BREAK = 90   # rompimento (30m/1h)
VOL_STRENGTH_MIN_RETEST = 90  # reteste antecipado (15m)
VOL_STRENGTH_MIN_CONT = 85    # continuação (1h)

RSI_MIN_EARLY = 50
RSI_MIN_BREAK = 50
RSI_MIN_RETEST = 50
RSI_MIN_CONT = 55

BOOK_DOMINANCE = 1.10  # takerBuy >= 1.1 * takerSell

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
        print("[OK] Alerta Telegram enviado.")
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

def rsi(prices, p=14):
    if len(prices) < p+1: return 50
    d = [prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains = [max(x,0) for x in d[-p:]]
    losses = [abs(min(x,0)) for x in d[-p:]]
    ag, al = (sum(gains)/p), (sum(losses)/p or 1e-12)
    return 100 - 100/(1 + ag/al)

def macd_12269(close):
    # MACD 12/26/9 padrão
    if len(close) < 26: return 0.0, 0.0, 0.0, False
    e12 = ema_series(close, 12)
    e26 = ema_series(close, 26)
    # alinhar tamanhos
    if len(e12) != len(e26):
        n = min(len(e12), len(e26))
        e12 = e12[-n:]; e26 = e26[-n:]
    macd_line_series = [a-b for a,b in zip(e12, e26)]
    signal_series = ema_series(macd_line_series, 9)
    macd_line = macd_line_series[-1]
    signal = signal_series[-1]
    hist = macd_line - signal
    # hist aumentando?
    hist_prev = macd_line_series[-2] - signal_series[-2] if len(macd_line_series) >= 2 and len(signal_series) >= 2 else hist
    hist_up = hist > hist_prev
    return macd_line, signal, hist, hist_up

def volume_strength(vol_series):
    n = len(vol_series)
    if n < 21:
        base = (sum(vol_series[-n:])/max(n,1)) or 1e-12
        return (vol_series[-1]/base)*100 if vol_series else 100.0, base, vol_series[-1] if vol_series else 0.0
    ma9  = sum(vol_series[-9:])/9
    ma21 = sum(vol_series[-21:])/21
    base = (ma9 + ma21)/2 or 1e-12
    return (vol_series[-1]/base)*100, base, vol_series[-1]

def taker_split_24h(t24):
    vol_quote = float(t24.get("quoteVolume", 0) or 0.0)
    taker_buy_q = float(t24.get("takerBuyQuoteAssetVolume", 0) or 0.0)
    taker_sell_q = max(vol_quote - taker_buy_q, 0.0)
    return vol_quote, taker_buy_q, taker_sell_q

def touched(avg, low, close):
    if avg in (None, 0, inf): return False
    return (low <= avg*(1+RETEST_TOL)) and (close >= avg*(1-RETEST_TOL))

def is_green(k): return float(k[4]) > float(k[1])
def broke_prev_high(curr, prev): return float(curr[4]) > float(prev[2])

# cooldown por tipo
cooldown = {"15m": {}, "30m": {}, "1h": {}}
def can_alert(tf, sym, kind):
    key = f"{sym}:{kind}"
    n = time.time()
    last = cooldown[tf].get(key, 0)
    if n - last >= COOLDOWN[tf]:
        cooldown[tf][key] = n
        return True
    return False

# estado por par/TF
state = {}
def ensure_state(key):
    if key not in state:
        state[key] = {
            "broke_base": False,
            "watch_retest": False,
            "last_taker_buy": 0.0,
            "base_ma": "EMA200",
            "last_break_ts": 0.0
        }

# ===== CORE =====
async def klines(s, sym, tf, lim=220):
    url = f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit={lim}"
    async with s.get(url, timeout=10) as r:
        if r.status != 200:
            return []
        return await r.json()

async def ticker24(s, sym):
    url = f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}"
    async with s.get(url, timeout=10) as r:
        if r.status != 200:
            return None
        return await r.json()

async def scan_tf(s, sym, tf):
    try:
        t24 = await ticker24(s, sym)
        if not t24: return
        p = float(t24["lastPrice"])
        vol24 = float(t24["quoteVolume"])
        if vol24 < MIN_VOL24: return

        k = await klines(s, sym, tf, 220)
        if len(k) < 60: return

        close = [float(x[4]) for x in k]
        low   = [float(x[3]) for x in k]
        vol   = [float(x[5]) for x in k]

        # médias
        have200 = len(close) >= 200
        ema9  = ema_last(close, 9)   if len(close)>=9   else inf
        ma20  = ema_last(close, 20)  if len(close)>=20  else inf  # usamos EMA20 (preferência por EMA)
        ema50 = ema_last(close, 50)  if len(close)>=50  else inf
        ema100= ema_last(close,100)  if len(close)>=100 else inf
        ema200= ema_last(close,200)  if have200 else None
        base_ma_val = ema200 if have200 else ema100
        base_ma_tag = "EMA200" if have200 else "EMA100(🧩fallback)"

        # indicadores
        r = rsi(close)
        macd_line, signal, hist, hist_up = macd_12269(close)
        macd_pos = (macd_line > 0 and hist >= 0)
        vs, vs_base, vs_now = volume_strength(vol)
        vol_quote, taker_buy_q, taker_sell_q = taker_split_24h(t24)
        book_ok = (taker_buy_q >= taker_sell_q * BOOK_DOMINANCE) or (taker_buy_q == 0.0)  # book=0 não bloqueia
        book_note = "bookOK" if taker_buy_q >= taker_sell_q * BOOK_DOMINANCE else ("book=0 ignorado" if taker_buy_q==0.0 else "book fraco")

        key = (sym, tf)
        ensure_state(key)

        # LOG base por varredura
        print(f"{now_br()} | {sym} | {tf} | RSI {r:.1f} | MACD {'+' if macd_line>0 else '-'} | Vol {vs:.0f}% | Book {taker_buy_q:.0f}/{taker_sell_q:.0f}")

        # util p/ mensagens (não altera lógica)
        def mk_common_lines():
            stop_local = min(float(x[3]) for x in k[-10:]) * 0.98
            alvo1_local, alvo2_local = p*1.025, p*1.05
            return stop_local, alvo1_local, alvo2_local

        # ===================== 15m: ENTRADA ANTECIPADA =====================
        # (LÓGICA ORIGINAL MANTIDA: early_ok + closes_above_fast)
        if tf == "15m":
            early_ok = (r >= RSI_MIN_EARLY) and hist_up and (vs >= VOL_STRENGTH_MIN_EARLY) and book_ok
            closes_above_fast = (close[-1] > ema9) and (close[-1] > ma20)
            if early_ok and closes_above_fast:
                if can_alert(tf, sym, "early"):
                    nome = sym.replace("USDT", "")
                    stop, alvo1, alvo2 = mk_common_lines()
                    msg = (
                        f"<b>⚡ ENTRADA ANTECIPADA (15M)</b>\n\n"
                        f"{nome}\n\n"
                        f"Entrada sugerida: <b>{p:.6f}</b>\n"
                        f"📉 Stop: <b>{stop:.6f}</b>\n"
                        f"🎯 Alvos: <b>{alvo1:.6f}</b> (+2.5%) | <b>{alvo2:.6f}</b> (+5%)\n"
                        f"RSI: <b>{r:.1f}</b> | MACD: <b>melhorando</b> (hist ↑)\n"
                        f"Vol força: <b>{vs:.0f}%</b> (atual {vs_now:,.0f} vs base {vs_base:,.0f})\n"
                        f"💰 Fluxo real: <b>{taker_buy_q:,.0f}</b> vs <b>{taker_sell_q:,.0f}</b> ({book_note})\n"
                        f"<i>{now_br()} BR</i>"
                    )
                    print(f"[ALERTA] {sym} 15m ENTRADA ANTECIPADA"); await tg(s, msg)

        # ===================== 30m/1h: ROMPIMENTO EMA200 =====================
        # (LÓGICA ORIGINAL MANTIDA: above_base + RSI + MACD + Volume + book_ok)
        if tf in ("30m","1h"):
            broke_now = False
            if base_ma_val and base_ma_val != 0:
                above_base = close[-1] > base_ma_val*(1+BREAK_TOL)
                broke_now = above_base and (r >= RSI_MIN_BREAK) and macd_pos and (vs >= VOL_STRENGTH_MIN_BREAK)
            if broke_now and book_ok:
                state[(sym,"15m")]["broke_base"] = True
                state[(sym,"15m")]["watch_retest"] = True
                state[(sym,"15m")]["last_taker_buy"] = taker_buy_q
                state[(sym,"15m")]["base_ma"] = base_ma_tag
                state[(sym,"15m")]["last_break_ts"] = time.time()
                if can_alert(tf, sym, "break"):
                    nome = sym.replace("USDT", "")
                    stop, alvo1, alvo2 = mk_common_lines()
                    msg = (
                        f"<b>💥 ROMPIMENTO CONFIRMADO ({tf.upper()})</b>\n\n"
                        f"{nome}\n\n"
                        f"Base: <b>{base_ma_tag}</b> | Preço acima da base +{BREAK_TOL*100:.2f}%\n"
                        f"Entrada sugerida: <b>{p:.6f}</b>\n"
                        f"📉 Stop: <b>{stop:.6f}</b>\n"
                        f"🎯 Alvos: <b>{alvo1:.6f}</b> (+2.5%) | <b>{alvo2:.6f}</b> (+5%)\n"
                        f"RSI: <b>{r:.1f}</b> | MACD: <b>positivo</b>\n"
                        f"Vol força: <b>{vs:.0f}%</b> (atual {vs_now:,.0f} vs base {vs_base:,.0f})\n"
                        f"💰 Fluxo real: <b>{taker_buy_q:,.0f}</b> vs <b>{taker_sell_q:,.0f}</b> ({book_note})\n"
                        f"<i>{now_br()} BR</i>"
                    )
                    print(f"[ALERTA] {sym} {tf} ROMPIMENTO"); await tg(s, msg)

        # ===================== 15m: RETESTE ANTECIPADO =====================
        # (LÓGICA ORIGINAL MANTIDA: touched EMA100/200 + resume_ok + first_green_back)
        if tf == "15m" and state[key]["watch_retest"]:
            if time.time() - state[key]["last_break_ts"] <= 3*60*60:  # até 3h após rompimento
                prev = k[-2]; curr = k[-1]
                prev_low, prev_close = float(prev[3]), float(prev[4])

                touch100 = touched(ema100, prev_low, prev_close) if ema100 != inf else False
                touch200 = touched(ema200, prev_low, prev_close) if ema200 not in (None, 0) else False
                touched_any = touch100 or touch200

                # virar de volta (início da retomada): RSI sobe >=50, MACD melhora (hist_up), volume retoma (≥90%)
                resume_ok = (r >= RSI_MIN_RETEST) and hist_up and (vs >= VOL_STRENGTH_MIN_RETEST)
                first_green_back = (close[-1] > ema9) and is_green(curr)

                if touched_any and resume_ok and first_green_back:
                    if can_alert(tf, sym, "retest_early"):
                        which = "EMA100" if touch100 else "EMA200"
                        nome = sym.replace("USDT", "")
                        stop, alvo1, alvo2 = mk_common_lines()
                        msg = (
                            f"<b>📘 RETESTE ANTECIPADO (15M)</b>\n\n"
                            f"{nome}\n\n"
                            f"Média testada: <b>{which}</b> | Base do ciclo: <b>{state[key]['base_ma']}</b>\n"
                            f"Entrada sugerida: <b>{p:.6f}</b>\n"
                            f"📉 Stop: <b>{stop:.6f}</b>\n"
                            f"🎯 Alvos: <b>{alvo1:.6f}</b> (+2.5%) | <b>{alvo2:.6f}</b> (+5%)\n"
                            f"RSI: <b>{r:.1f}</b> | MACD: <b>melhorando</b> (hist ↑)\n"
                            f"Vol força: <b>{vs:.0f}%</b>\n"
                            f"<i>{now_br()} BR</i>"
                        )
                        print(f"[ALERTA] {sym} 15m RETESTE ANTECIPADO"); await tg(s, msg)

        # ===================== 1h: CONTINUAÇÃO CONFIRMADA =====================
        # (LÓGICA ORIGINAL MANTIDA: candle verde quebrando topo anterior + vs + RSI + MACD + crescimento de book)
        if tf == "1h" and state.get((sym,"15m"), {}).get("broke_base", False):
            prev = k[-2]; curr = k[-1]
            cont_ok = is_green(curr) and broke_prev_high(curr, prev) \
                      and (vs >= VOL_STRENGTH_MIN_CONT) and (r >= RSI_MIN_CONT) \
                      and macd_pos
            last_tb = state[(sym,"15m")]["last_taker_buy"]
            book_growth = (taker_buy_q >= max(last_tb * BOOK_DOMINANCE, last_tb+1e-9)) or (last_tb == 0.0)
            if cont_ok and book_growth:
                if can_alert(tf, sym, "continue"):
                    nome = sym.replace("USDT", "")
                    stop, alvo1, alvo2 = mk_common_lines()
                    msg = (
                        f"<b>🔥 CONTINUAÇÃO CONFIRMADA (1H)</b>\n\n"
                        f"{nome}\n\n"
                        f"Entrada: <b>{p:.6f}</b>\n"
                        f"📉 Stop: <b>{stop:.6f}</b>\n"
                        f"🎯 Alvos: <b>{alvo1:.6f}</b> (+2.5%) | <b>{alvo2:.6f}</b> (+5%)\n"
                        f"RSI: <b>{r:.1f}</b> | MACD: <b>positivo</b> | Vol força: <b>{vs:.0f}%</b>\n"
                        f"💰 Fluxo real: <b>{taker_buy_q:,.0f}</b> vs <b>{taker_sell_q:,.0f}</b>\n"
                        f"<i>{now_br()} BR</i>"
                    )
                    print(f"[ALERTA] {sym} 1h CONTINUAÇÃO"); await tg(s, msg)
                # encerra ciclo
                state[(sym,"15m")]["broke_base"] = False
                state[(sym,"15m")]["watch_retest"] = False
                state[(sym,"15m")]["last_taker_buy"] = taker_buy_q

        # cancelamento do ciclo (se perder força) — LÓGICA ORIGINAL MANTIDA
        if state.get((sym,"15m"), {}).get("broke_base", False):
            lost_rsi_macd = (r < 45) or (macd_line < 0 and (macd_line - signal) < 0)
            below_fast = close[-1] < ema50*(1-RETEST_TOL) if ema50 not in (None, inf, 0) else False
            if lost_rsi_macd or below_fast:
                state[(sym,"15m")]["broke_base"] = False
                state[(sym,"15m")]["watch_retest"] = False
                state[(sym,"15m")]["last_taker_buy"] = 0.0
                print(f"[CANCEL] {sym} ciclo abortado (força perdida)")

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V7.3 ATIVO — TENDÊNCIA CURTA (Entrada Antecipada + Reteste + Continuação)</b>\n15m/30m/1h | Vol24≥10M | Cooldown 15m | Scan 30s")
        while True:
            try:
                resp = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if resp.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL); continue
                data = await resp.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume") or 0) >= MIN_VOL24
                    and (lambda base: not (
                        base.endswith("USD") or base in {
                            "BUSD","FDUSD","USDE","USDC","TUSD","CUSD",
                            "EUR","GBP","TRY","AUD","BRL","RUB","CAD","CHF","JPY",
                            "BF","BFC","BFG","BFD","BETA","AEUR","AUSD","CEUR","XAUT"
                        }
                    ))(d["symbol"][:-4])
                    and not any(x in d["symbol"] for x in ["UP","DOWN"])
                ]

                # Top N por volume
                symbols = sorted(
                    symbols,
                    key=lambda x: next((float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0.0),
                    reverse=True
                )[:TOP_N]

                print(f"[SCAN] {now_br()} | Símbolos escaneados: {len(symbols)} (Top {TOP_N})")
                tasks = []
                for sym in symbols:
                    # ordem ajuda a preparar ciclo: primeiro 1h/30m, depois 15m
                    tasks.append(scan_tf(s, sym, "1h"))
                    tasks.append(scan_tf(s, sym, "30m"))
                    tasks.append(scan_tf(s, sym, "15m"))
                await asyncio.gather(*tasks)

            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
```0
