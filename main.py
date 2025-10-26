# main_hibrido_vflex.py
# ✅ Híbrido (3m + 5m + 15m) com alertas limpos
# ✅ Flex: faixas de RSI e volume configuráveis (não engessado)
# ✅ Novos alertas:
#    🟣 PRIMEIRO MOVIMENTO (3m) — preço fecha acima da MA200 com força (antes do cruzamento da EMA9)
#    🟡 ROMPIMENTO (3m) — EMA9 cruza MA200 de baixo para cima
#    🟠 CONFIRMAÇÃO (5m) — EMA9 cruza MA200
#    🟢 TENDÊNCIA (15m) — alinhamento completo, dispara só quando forma
#    🚀 ACELERAÇÃO REAL (3m) — explosão rápida (pumps com continuidade mínima)
#    ♻️ TENDÊNCIA PÓS-PUMP (3m e 5m)
#    📈 TENDÊNCIA GRADUAL (5m) — alta progressiva sem pump (novo)
# ✅ Filtro moedas mortas (blocklist + volume 24h mínimo)
# ✅ Estrutura original preservada + validações de continuidade

import os, asyncio, aiohttp, time, math, statistics
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 8 * 60          # 8 minutos (pode ajustar abaixo se quiser)
TOP_N = 50
REQ_TIMEOUT = 8

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- AJUSTES DINÂMICOS ----------------
# Faixas flexíveis (intervalos) e filtros — ajuste aqui sem mexer na lógica
RSI_RANGE_REVERSAO = (45, 65)     # Para sinais de início (3m/5m)
RSI_RANGE_CONF     = (55, 70)     # Para confirmação/tendência (5m/15m)
VOL_MULTIPLIER     = 1.2          # Volume atual precisa ser >= VOL_MULTIPLIER * média20
MIN_VOL_24H        = 15_000_000   # Filtro de liquidez mínima em USDT (24h)
# 🔄 Parâmetros de tendência pós-pump (flexíveis)
RSI_RANGE_POSTPUMP = (50, 60)        # faixa de RSI aceitável na retomada
VOL_MULTIPLIER_POSTPUMP = 1.3        # volume atual precisa ser >= 1.3× média5
POSTPUMP_LOOKBACK = 20               # quantas velas olhar pra trás pra detectar o pump anterior

# Moedas mortas / memes a evitar (além dos já bloqueados)
NAME_BLOCKLIST = ("PEPE", "FLOKI", "BONK", "SHIB", "DOGE")

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m + 15m) — flex RSI/Volume | 🇧🇷", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        await session.post(url, data=payload, timeout=REQ_TIMEOUT)
    except:
        pass

def fmt_price(x: float) -> str:
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"

def sma(seq, n):
    out, s = [], 0.0
    from collections import deque
    q = deque()
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s/len(q))
    return out

def ema(seq, span):
    if not seq: return []
    alpha = 2.0/(span+1.0)
    out = [seq[0]]
    e = seq[0]
    for x in seq[1:]:
        e = alpha*x + (1-alpha)*e
        out.append(e)
    return out

def bollinger_bands(seq, n=20, mult=2):
    if len(seq) < n: return [], [], []
    out_mid, out_upper, out_lower = [], [], []
    for i in range(len(seq)):
        window = seq[max(0, i-n+1):i+1]
        m = sum(window)/len(window)
        s = statistics.pstdev(window)
        out_mid.append(m)
        out_upper.append(m + mult*s)
        out_lower.append(m - mult*s)
    return out_upper, out_mid, out_lower

def calc_rsi(seq, period=14):
    if len(seq) < period + 1:
        return [50.0] * len(seq)
    gains, losses = [], []
    for i in range(1, len(seq)):
        diff = seq[i] - seq[i-1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    rsi = []
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / (avg_loss + 1e-12)
    rsi.append(100 - (100 / (1 + rs)))
    for i in range(period, len(seq)-1):
        diff = seq[i] - seq[i-1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period-1) + gain) / period
        avg_loss = (avg_loss * (period-1) + loss) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi.append(100 - (100 / (1 + rs)))
    return [50.0]*(len(seq)-len(rsi)) + rsi

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=210):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
            if isinstance(data, list):
                return data
            return []
    except:
        return []

async def get_top_usdt_symbols(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=REQ_TIMEOUT) as r:
        data = await r.json()
    blocked = (
        "UP","DOWN","BULL","BEAR",
        "BUSD","FDUSD","TUSD","USDC","USDP","USD1","USDE","XUSD","USDX","GUSD","BFUSD",
        "EUR","EURS","CEUR","BRL","TRY",
        "PERP","_PERP","STABLE","TEST"
    )
    pares = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        if any(x in s for x in blocked):
            continue
        if any(x in s for x in NAME_BLOCKLIST):
            continue
        try:
            qv = float(d.get("quoteVolume", "0") or 0.0)
        except:
            qv = 0.0
        if qv < float(MIN_VOL_24H):
            continue
        pares.append((s, qv))
    pares.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pares[:TOP_N]]

# ---------------- ALERT STATE ----------------
LAST_HIT = {}

def allowed(symbol, kind):
    ts = LAST_HIT.get((symbol, kind), 0.0)
    return (time.time() - ts) >= COOLDOWN_SEC

def mark(symbol, kind):
    LAST_HIT[(symbol, kind)] = time.time()

# ---------------- FUNÇÃO PÓS-PUMP ----------------
async def postpump_alert(session, symbol, closes, vols, rsi, ema9, ema20, ema50, ma200, timeframe_tag, index):
    """Detecta retomada de tendência após pump forte"""
    try:
        if len(closes) < POSTPUMP_LOOKBACK:
            return

        # Detecta pump anterior (RSI >70 + volume 2× média)
        recent_rsi = rsi[-POSTPUMP_LOOKBACK:]
        recent_vol = vols[-POSTPUMP_LOOKBACK:]
        pump_before = any(r > 70 for r in recent_rsi[:-5]) and any(
            v >= 2 * (sum(recent_vol[-10:]) / 10 + 1e-12) for v in recent_vol[:-5]
        )
        if not pump_before:
            return

        # Condições atuais de retomada
        rsi_now = rsi[-1]
        vol_now = vols[-1]
        mean5 = sum(vols[-6:-1]) / 5.0
        volume_ok = vol_now >= VOL_MULTIPLIER_POSTPUMP * (mean5 + 1e-12)
        rsi_ok = RSI_RANGE_POSTPUMP[0] <= rsi_now <= RSI_RANGE_POSTPUMP[1]
        alignment = ema9[index] > ema20[index] > ema50[index] > ma200[index]

        # Se tudo alinhado, dispara alerta
        if rsi_ok and volume_ok and alignment and allowed(symbol, f"POSTPUMP_{timeframe_tag}"):
            msg = (f"♻️ {symbol} — TENDÊNCIA PÓS-PUMP ({timeframe_tag})\n"
                   f"• Correção concluída e retomada confirmada\n"
                   f"• RSI:{rsi_now:.1f} • Vol ≥ {VOL_MULTIPLIER_POSTPUMP:.1f}×MA5\n"
                   f"• MA9>MA20>MA50>MA200\n"
                   f"💰 {fmt_price(closes[index])}\n🕒 {now_br()}\n──────────────────────────────")
            await tg(session, msg)
            mark(symbol, f"POSTPUMP_{timeframe_tag}")
    except:
        return

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # -------- 3m --------
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            v3 = [float(k[5]) for k in k3]
            ema9_3  = ema(c3, 9)
            ema20_3 = ema(c3, 20)
            ma200_3 = sma(c3, 200)
            rsi3 = calc_rsi(c3, 14)
            vma20_3 = sum(v3[-20:]) / 20.0
            i3 = len(c3)-1

            # ⚙️ FILTRO DE CONTEXTO (evita lateralizações fracas abaixo da 200)
            if len(c3) > 30:
                recent_20 = c3[-20:]
                mean_price = sum(recent_20)/20
                dev = statistics.pstdev(recent_20)
                lateral = (dev / mean_price) < 0.003  # ~0,3%
                below_ma200 = all(p < ma200_3[-1] for p in recent_20)
                rising_rsi = (rsi3[-1] > rsi3[-5]) if len(rsi3) >= 5 else False
                volume_ok = (v3[-1] > VOL_MULTIPLIER * vma20_3)
                if below_ma200 and lateral and not (rising_rsi and volume_ok):
                    return  # ignora falso respiro

            rsi_ok_3_rev  = (RSI_RANGE_REVERSAO[0] <= rsi3[-1] <= RSI_RANGE_REVERSAO[1])
            rsi_ok_3_conf = (RSI_RANGE_CONF[0]     <= rsi3[-1] <= RSI_RANGE_CONF[1])
            vol_ok_3      = (v3[-1] >= VOL_MULTIPLIER * (vma20_3 + 1e-12))

            # 🟣 PRIMEIRO MOVIMENTO (3m)
            first_move_3m = (ema9_3[i3] < ma200_3[i3]) and (c3[i3] > ma200_3[i3]) and rsi_ok_3_rev and vol_ok_3
            if first_move_3m and allowed(symbol, "FIRST_3M"):
                msg = (f"🟣 {symbol} — PRIMEIRO MOVIMENTO (3m)\n"
                       f"• Preço FECHOU acima da MA200 com força (antes do cruzamento da EMA9)\n"
                       f"• RSI:{rsi3[-1]:.1f} dentro da faixa {RSI_RANGE_REVERSAO[0]}–{RSI_RANGE_REVERSAO[1]} • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n"
                       f"💰 {fmt_price(c3[i3])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "FIRST_3M")

            # 🟡 ROMPIMENTO (3m)
            cross_9_200_3m = (ema9_3[i3-1] <= ma200_3[i3-1]) and (ema9_3[i3] > ma200_3[i3]) and (rsi_ok_3_rev or rsi_ok_3_conf) and vol_ok_3
            if cross_9_200_3m and allowed(symbol, "ROMP_3M"):
                msg = (f"🟡 {symbol} — ROMPIMENTO EMA9×MA200 (3m)\n"
                       f"• EMA9 cruzou MA200 de baixo para cima\n"
                       f"• RSI:{rsi3[-1]:.1f} (faixa aceita) • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n"
                       f"💰 {fmt_price(c3[i3])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "ROMP_3M")

                # 🔍 VALIDAÇÃO DE CONTINUIDADE (3–5 velas)
                if len(c3) > 205:
                    next_prices = [float(k[4]) for k in k3[-5:]]
                    next_vols   = [float(k[5]) for k in k3[-5:]]
                    next_rsi    = rsi3[-5:]
                    price_gain = (next_prices[-1] - next_prices[0]) / next_prices[0]
                    vol_trend  = next_vols[-1] >= 0.8 * max(next_vols)
                    rsi_trend  = next_rsi[-1] > next_rsi[0]
                    if price_gain > 0.008 and vol_trend and rsi_trend and allowed(symbol, "CONT_3M"):
                        msg = (f"🟢 {symbol} — CONTINUIDADE DETECTADA (3m)\n"
                               f"• Preço subiu {price_gain*100:.1f}% nas últimas 5 velas\n"
                               f"• RSI subindo e volume mantido\n"
                               f"💰 {fmt_price(c3[i3])}\n🕒 {now_br()}\n──────────────────────────────")
                        await tg(session, msg)
                        mark(symbol, "CONT_3M")

            # 🚀 ACELERAÇÃO REAL (3m) — pumps com continuidade mínima
            if len(c3) >= 210:
                # volume explosivo em 2 velas: vela anterior >=2×média5 e atual >=80% da anterior
                mean5 = (sum(v3[-6:-1]) / 5.0) if len(v3) >= 6 else vma20_3
                vol_prev = v3[-2]
                vol_now  = v3[-1]
                vol_explosive = (vol_prev >= 2.0 * (mean5 + 1e-12)) and (vol_now >= 0.8 * vol_prev)

                # salto de RSI ≥ 10 pts em até 3 velas
                rsi_jump = False
                if len(rsi3) >= 3:
                    rsi_jump = (rsi3[-1] - rsi3[-3]) >= 10.0

                # médias curtas coladas (aceleração real)
                ema_close = abs(ema9_3[i3] - ema20_3[i3]) / max(c3[i3], 1e-12) < 0.003  # 0,3%

                # fechamento perto da máxima (candle forte)
                h = float(k3[-1][2]); l = float(k3[-1][3]); close = c3[i3]
                rng = max(h - l, 1e-12)
                close_near_high = (h - close) / rng <= 0.20  # top 20% do range

                if vol_explosive and rsi_jump and ema_close and close_near_high and allowed(symbol, "ACCEL_3M"):
                    msg = (f"🚀 {symbol} — ACELERAÇÃO REAL (3m)\n"
                           f"• Volume explosivo (≥2× média5 e continuidade)\n"
                           f"• RSI +10 pts em ≤3 velas • EMA9≈EMA20 (aceleração)\n"
                           f"💰 {fmt_price(close)}\n🕒 {now_br()}\n──────────────────────────────")
                    await tg(session, msg)
                    mark(symbol, "ACCEL_3M")

            # ♻️ Verifica tendência pós-pump (3m)
            await postpump_alert(session, symbol, c3, v3, rsi3, ema9_3, ema(c3,20), sma(c3,50), sma(c3,200), "3M", i3)

        # -------- 5m --------
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) >= 210:
            c5 = [float(k[4]) for k in k5]
            v5 = [float(k[5]) for k in k5]
            ema9_5  = ema(c5, 9)
            ema20_5 = ema(c5, 20)              # (adição para o alerta gradual)
            ma50_5  = sma(c5, 50)              # (adição para o alerta gradual)
            ma200_5 = sma(c5, 200)
            rsi5 = calc_rsi(c5, 14)
            vma20_5 = sum(v5[-20:]) / 20.0
            i5 = len(c5)-1

            rsi_ok_5_rev  = (RSI_RANGE_REVERSAO[0] <= rsi5[-1] <= RSI_RANGE_REVERSAO[1])
            rsi_ok_5_conf = (RSI_RANGE_CONF[0]     <= rsi5[-1] <= RSI_RANGE_CONF[1])
            vol_ok_5      = (v5[-1] >= VOL_MULTIPLIER * (vma20_5 + 1e-12))

            # 🟠 CONFIRMAÇÃO (5m)
            cross_9_200_5m = (ema9_5[i5-1] <= ma200_5[i5-1]) and (ema9_5[i5] > ma200_5[i5]) and (rsi_ok_5_rev or rsi_ok_5_conf) and vol_ok_5
            if cross_9_200_5m and allowed(symbol, "CONF_5M"):
                msg = (f"🟠 {symbol} — CONFIRMAÇÃO (5m)\n"
                       f"• EMA9 cruzou MA200 de baixo para cima\n"
                       f"• RSI:{rsi5[-1]:.1f} (faixa aceita) • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n"
                       f"💰 {fmt_price(c5[i5])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "CONF_5M")

                # 🔍 CONFIRMAÇÃO REAL (5m) — pullback + sustentação
                if len(c5) > 205:
                    next_prices = [float(k[4]) for k in k5[-5:]]
                    next_vols   = [float(k[5]) for k in k5[-5:]]
                    next_rsi    = rsi5[-5:]
                    price_pullback = min(next_prices) > ma200_5[i5] * 0.995  # não perdeu 0,5% da MA200
                    price_gain = (next_prices[-1] - next_prices[0]) / next_prices[0]
                    vol_trend  = next_vols[-1] >= 0.8 * max(next_vols)
                    rsi_hold   = all(r > 55 for r in next_rsi[-3:])
                    if price_pullback and price_gain > 0.008 and vol_trend and rsi_hold and allowed(symbol, "REAL_5M"):
                        msg = (f"🟢 {symbol} — CONFIRMAÇÃO REAL (5m)\n"
                               f"• Preço manteve acima da MA200 (sem devolução)\n"
                               f"• RSI >55 e volume estável\n"
                               f"💰 {fmt_price(c5[i5])}\n🕒 {now_br()}\n──────────────────────────────")
                        await tg(session, msg)
                        mark(symbol, "REAL_5M")

            # 📈 TENDÊNCIA GRADUAL (5m) — alta progressiva sem pump (novo)
            if (
                52 <= rsi5[-1] <= 68
                and ema9_5[i5] > ema20_5[i5] > ma50_5[i5] > ma200_5[i5]
                and v5[-1] > 1.05 * (vma20_5 + 1e-12)
                and len(c5) >= 31 and (c5[-1] - c5[-30]) / max(c5[-30], 1e-12) >= 0.02
                and allowed(symbol, "STEADY_5M")
            ):
                msg = (f"📈 {symbol} — TENDÊNCIA GRADUAL (5m)\n"
                       f"• Alta constante e progressiva (sem pump)\n"
                       f"• RSI:{rsi5[-1]:.1f} • Vol ≥ 1.05×MA20\n"
                       f"• MA9>MA20>MA50>MA200\n"
                       f"💰 {fmt_price(c5[-1])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "STEADY_5M")

            # ♻️ Verifica tendência pós-pump (5m)
            await postpump_alert(session, symbol, c5, v5, rsi5, ema9_5, ema20_5, ma50_5, ma200_5, "5M", i5)

        # -------- 15m --------
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) >= 210:
            c15 = [float(k[4]) for k in k15]
            v15 = [float(k[5]) for k in k15]
            ema9_15  = ema(c15, 9)
            ema20_15 = ema(c15, 20)
            ma50_15  = sma(c15, 50)
            ma200_15 = sma(c15, 200)
            rsi15 = calc_rsi(c15, 14)
            vma20_15 = sum(v15[-20:]) / 20.0
            j = len(c15)-1

            rsi_ok_15 = (RSI_RANGE_CONF[0] <= rsi15[-1] <= RSI_RANGE_CONF[1])
            vol_ok_15 = (v15[-1] >= VOL_MULTIPLIER * (vma20_15 + 1e-12))

            aligned_prev = (ema9_15[j-1] > ema20_15[j-1] > ma50_15[j-1] > ma200_15[j-1])
            aligned_now  = (ema9_15[j]   > ema20_15[j]   > ma50_15[j]   > ma200_15[j])
            formed_now_15m = (not aligned_prev) and aligned_now and rsi_ok_15 and vol_ok_15

            if formed_now_15m and allowed(symbol, "TEND_15M"):
                msg = (f"🟢 {symbol} — TENDÊNCIA CONSOLIDADA (15m)\n"
                       f"• EMA9>EMA20>MA50>MA200 • RSI:{rsi15[-1]:.1f} (faixa) • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n"
                       f"💰 {fmt_price(c15[j])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "TEND_15M")

                # 💚 CONFIRMAÇÃO REAL (15m) — tendência sustentada
                if len(c15) > 205:
                    next_prices = [float(k[4]) for k in k15[-5:]]
                    next_vols   = [float(k[5]) for k in k15[-5:]]
                    next_rsi    = rsi15[-5:]
                    price_above_ma50 = all(p > ma50_15[j] for p in next_prices)
                    steady_vol = (min(next_vols) > 0.7 * max(next_vols))
                    strong_rsi = all(r > 60 for r in next_rsi[-3:])
                    if price_above_ma50 and steady_vol and strong_rsi and allowed(symbol, "REAL_15M"):
                        msg = (f"💚 {symbol} — TENDÊNCIA CONFIRMADA (15m)\n"
                               f"• Mantém acima da MA50, RSI>60 e volume constante\n"
                               f"💰 {fmt_price(c15[j])}\n🕒 {now_br()}\n──────────────────────────────")
                        await tg(session, msg)
                        mark(symbol, "REAL_15M")

    except:
        return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"✅ Scanner ativo | {len(symbols)} pares | cooldown {COOLDOWN_SEC//60}m | {now_br()} (UTC-3)\n──────────────────────────────")
        if not symbols: return
        while True:
            tasks = [scan_symbol(session, s) for s in symbols]
            await asyncio.gather(*tasks)
            await asyncio.sleep(10)

# ---------------- RUN ----------------
def start_bot():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception:
            time.sleep(5)

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
