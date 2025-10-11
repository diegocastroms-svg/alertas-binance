# main.py — v1_zero_final (Nova e última chance da Aurora)
# ----------------------------------------------
# Requisitos de ambiente:
# - TELEGRAM_TOKEN : token do bot do Telegram
# - TELEGRAM_CHAT_ID : id do chat (ex.: -100123..., ou id do usuário)
# - PORT : setado automaticamente pelo Render (fallback 5000)
#
# Bibliotecas: aiohttp, flask, python-dotenv (opcional no Render)
# requirements.txt sugerido:
# aiohttp==3.9.5
# Flask==3.0.3
# python-dotenv==1.0.1

import os
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread

# ============== CONFIGURAÇÃO ==============
BINANCE_API = "https://api.binance.com"
INTERVAL_5M = "5m"
INTERVAL_15M = "15m"
KLIMIT = 240  # barras suficientes p/ MAs/RSI estáveis

# Controle de lista Top-50 (atualização 1h)
TOP_N = 50
UPDATE_INTERVAL_SECONDS = 3600  # 1 hora

# Cooldown: 15 min por PAR + por TIPO de alerta
COOLDOWN_SECONDS = 15 * 60

# Limite de análises concorrentes (estabilidade)
MAX_CONCURRENCY = 12

# Filtros: somente SPOT USDT, excluir sintéticos/leveraged/coletores
EXCLUDE_KEYWORDS = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "2L", "2S", "4L", "4S", "5L", "5S")
# Também exclui base assets que terminem com 'USD' (ex.: BFUSDUSDT)
# Também exclui símbolos que terminem com 'USD' (anti-USD)

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Timezone Brasil (sem horário de verão)
BR_TZ = timezone(timedelta(hours=-3))

# Estado
top_pairs = []                # lista atual dos TOP N por volume 24h (USDT/spot)
last_alert_time = {}          # {(symbol, alert_key): timestamp}
last_top_refresh = 0          # timestamp do último refresh de top list

# Flask (healthcheck Render)
app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ============== UTILITÁRIOS ==============

def now_br_str():
    return datetime.now(BR_TZ).strftime("%Y-%m-%d %H:%M")

def ema(values, period):
    k = 2 / (period + 1)
    ema_val = None
    out = []
    for v in values:
        if ema_val is None:
            ema_val = v
        else:
            ema_val = v * k + ema_val * (1 - k)
        out.append(ema_val)
    return out

def sma(values, period):
    out = []
    s = 0.0
    q = []
    for v in values:
        q.append(v)
        s += v
        if len(q) > period:
            s -= q.pop(0)
        if len(q) == period:
            out.append(s / period)
        else:
            out.append(None)
    return out

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(diff, 0))
        losses.append(-min(diff, 0))
    # médias móveis exponenciais simples (sem numpy)
    def rma(seq, p):
        avg = None
        out = []
        alpha = 1 / p
        for x in seq:
            if avg is None:
                avg = x
            else:
                avg = (1 - alpha) * avg + alpha * x
            out.append(avg)
        return out
    ag = rma(gains, period)
    al = rma(losses, period)
    rs = ag[-1] / al[-1] if al[-1] != 0 else float('inf')
    return 100 - (100 / (1 + rs))

def is_leveraged_or_synthetic(base_asset: str, symbol: str) -> bool:
    b = base_asset.upper()
    s = symbol.upper()
    if s.endswith("USD"):     # anti-USD (queremos só USDT)
        return True
    if b.endswith("USD"):     # BFUSD, AUSD, etc.
        return True
    for kw in EXCLUDE_KEYWORDS:
        if kw in s:
            return True
    return False

def binance_spot_chart_link(symbol: str, interval: str) -> str:
    # Link direto pra aba de trade spot; o Telegram abre o app se instalado
    return f"https://www.binance.com/en/trade?symbol={symbol}&type=spot"

async def send_telegram(session: aiohttp.ClientSession, text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("AVISO: TELEGRAM_TOKEN/CHAT_ID não configurados. Mensagem não enviada:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True   # remove prévia de link/banner
    }
    try:
        async with session.post(url, json=payload, timeout=30) as resp:
            _ = await resp.text()
    except Exception as e:
        print("Erro ao enviar Telegram:", e)

def cooldown_ok(symbol: str, key: str) -> bool:
    t = last_alert_time.get((symbol, key), 0)
    return (datetime.utcnow().timestamp() - t) >= COOLDOWN_SECONDS

def set_cooldown(symbol: str, key: str):
    last_alert_time[(symbol, key)] = datetime.utcnow().timestamp()

def fmt_msg(symbol: str, titulo: str, motivo: str, price: float, rsi_val: float,
            ema9: float, ma20: float, ma50: float, ma200: float, interval_label: str):
    # Nome + motivo destacados; horário BR + link por último
    body = (
        f"🚀 <b>{titulo}</b>\n\n"
        f"<b>{symbol}</b>\n"
        f"📊 {motivo}\n"
        f"📈 EMA9: {ema9:.5f} | MA20: {ma20:.5f} | MA50: {ma50:.5f}\n"
        f"🌙 MA200: {ma200:.5f}\n"
        f"💰 Preço: {price:.6f}\n"
        f"📉 RSI: {rsi_val:.1f}\n"
        f"🇧🇷 {now_br_str()}\n"
        f"📎 Ver gráfico {interval_label} no app da Binance"
    )
    return body

# ============== BINANCE ==============

async def fetch_json(session: aiohttp.ClientSession, url: str, params=None):
    for _ in range(2):
        try:
            async with session.get(url, params=params, timeout=20) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as e:
            print("fetch_json erro:", e)
        await asyncio.sleep(0.8)
    return None

async def get_spot_usdt_pairs(session: aiohttp.ClientSession):
    """Retorna lista de símbolos SPOT/USDT válidos, filtrando alavancados/sintéticos."""
    url = f"{BINANCE_API}/api/v3/exchangeInfo"
    data = await fetch_json(session, url, params={"permissions": "SPOT"})
    pairs = []
    if not data or "symbols" not in data:
        return pairs
    for s in data["symbols"]:
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        base = s.get("baseAsset", "")
        symbol = s.get("symbol", "")
        if is_leveraged_or_synthetic(base, symbol):
            continue
        pairs.append(symbol)
    return pairs

async def get_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int):
    url = f"{BINANCE_API}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = await fetch_json(session, url, params=params)
    if not data:
        return None
    closes = [float(x[4]) for x in data]
    vols = [float(x[5]) for x in data]
    return closes, vols

async def get_top_by_volume(session: aiohttp.ClientSession, symbols: list) -> list:
    """Ordena por volume 24h e retorna TOP_N."""
    url = f"{BINANCE_API}/api/v3/ticker/24hr"
    top = []
    # Para eficiência, consulta em lotes
    for s in symbols:
        j = await fetch_json(session, url, params={"symbol": s})
        if j and "volume" in j:
            try:
                vol = float(j["volume"])
                top.append((s, vol))
            except:
                pass
    top.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in top[:TOP_N]]

# ============== ANÁLISE ==============

def cross_up(a_prev, a_now, b_prev, b_now) -> bool:
    """Retorna True se a linha A cruzou pra cima da B neste candle."""
    if a_prev is None or b_prev is None:
        return False
    return a_prev <= b_prev and a_now > b_now

async def analyze_symbol(session: aiohttp.ClientSession, symbol: str):
    """Analisa 5m e 15m e dispara alertas conforme regras definidas."""
    try:
        # -------- 5m --------
        k5 = await get_klines(session, symbol, INTERVAL_5M, KLIMIT)
        if not k5:
            return
        closes5, vols5 = k5
        price5 = closes5[-1]

        ema9_5 = ema(closes5, 9)
        ma20_5 = sma(closes5, 20)
        ma50_5 = sma(closes5, 50)
        ma200_5 = sma(closes5, 200)
        rsi5 = rsi(closes5, 14)

        # Garantir últimos pontos
        e9_5, m20_5, m50_5, m200_5 = ema9_5[-1], ma20_5[-1], ma50_5[-1], ma200_5[-1]
        e9_5_prev, m20_5_prev = ema9_5[-2], ma20_5[-2]

        # 5m — "Tendência iniciando": ema9 cruza MA20 pra cima, preço acima MA200 (reversão)
        if cross_up(e9_5_prev, e9_5, m20_5_prev, m20_5) and (m200_5 is not None) and (price5 > m200_5):
            key = "start_up_5m"
            if cooldown_ok(symbol, key):
                msg = fmt_msg(
                    symbol,
                    "TENDÊNCIA INICIANDO (5m)",
                    "EMA9 cruzou MA20 pra cima após queda/lateralização",
                    price5, rsi5 if rsi5 else 0.0, e9_5, m20_5, m50_5, m200_5, "5m"
                )
                await send_telegram(session, msg)
                set_cooldown(symbol, key)

        # 5m — "Pré-confirmação": ema9 > ma20 e ma50, mas preço ainda abaixo da MA200
        if (e9_5 is not None and m20_5 is not None and m50_5 is not None and m200_5 is not None):
            if (e9_5 > m20_5 > m50_5) and (price5 < m200_5):
                key = "preconf_5m"
                if cooldown_ok(symbol, key):
                    msg = fmt_msg(
                        symbol,
                        "PRÉ-CONFIRMAÇÃO (5m)",
                        "EMA9>MA20>MA50 com preço abaixo da MA200",
                        price5, rsi5 if rsi5 else 0.0, e9_5, m20_5, m50_5, m200_5, "5m"
                    )
                    await send_telegram(session, msg)
                    set_cooldown(symbol, key)

        # -------- 15m --------
        k15 = await get_klines(session, symbol, INTERVAL_15M, KLIMIT)
        if not k15:
            return
        closes15, vols15 = k15
        price15 = closes15[-1]

        ema9_15 = ema(closes15, 9)
        ma20_15 = sma(closes15, 20)
        ma50_15 = sma(closes15, 50)
        ma200_15 = sma(closes15, 200)
        rsi15 = rsi(closes15, 14)

        e9_15, m20_15, m50_15, m200_15 = ema9_15[-1], ma20_15[-1], ma50_15[-1], ma200_15[-1]
        e9_15_prev, m200_15_prev = ema9_15[-2], ma200_15[-2]

        # 15m — pré-confirmada: EMA9 cruza a MA200
        if cross_up(e9_15_prev, e9_15, m200_15_prev, m200_15):
            key = "preconf_15m"
            if cooldown_ok(symbol, key):
                msg = fmt_msg(
                    symbol,
                    "TENDÊNCIA PRÉ-CONFIRMADA (15m)",
                    "EMA9 cruzou a MA200 pra cima",
                    price15, rsi15 if rsi15 else 0.0, e9_15, m20_15, m50_15, m200_15, "15m"
                )
                await send_telegram(session, msg)
                set_cooldown(symbol, key)

        # 15m — confirmada: MA20 e MA50 acima da MA200
        if (m20_15 is not None and m50_15 is not None and m200_15 is not None):
            if (m20_15 > m50_15) and (m50_15 > m200_15):
                key = "conf_15m"
                if cooldown_ok(symbol, key):
                    msg = fmt_msg(
                        symbol,
                        "TENDÊNCIA CONFIRMADA (15m)",
                        "MA20 e MA50 acima da MA200",
                        price15, rsi15 if rsi15 else 0.0, e9_15, m20_15, m50_15, m200_15, "15m"
                    )
                    await send_telegram(session, msg)
                    set_cooldown(symbol, key)

        # 15m — Reteste confirmado (continuação de alta): preço testa EMA9/MA20, RSI>55 e volume > média
        vol_avg_15 = sum(vols15[-20:]) / 20 if len(vols15) >= 20 else None
        touched = False
        if e9_15 and abs(price15 - e9_15) / price15 < 0.004:
            touched = True
        if m20_15 and abs(price15 - m20_15) / price15 < 0.004:
            touched = True

        if touched and rsi15 and rsi15 > 55 and vol_avg_15 and vols15[-1] > vol_avg_15 and price15 > (m20_15 or 0):
            key = "reteste_ok_15m"
            if cooldown_ok(symbol, key):
                msg = fmt_msg(
                    symbol,
                    "RETESTE CONFIRMADO (15m)",
                    "Preço testou EMA9/MA20 e retomou com força (RSI>55, vol>média)",
                    price15, rsi15, e9_15, m20_15, m50_15, m200_15, "15m"
                )
                await send_telegram(session, msg)
                set_cooldown(symbol, key)

        # 15m — Reteste fraco (perdendo força): preço testa e perde, RSI<50
        if touched and rsi15 and rsi15 < 50 and price15 < (e9_15 or price15):
            key = "reteste_fraco_15m"
            if cooldown_ok(symbol, key):
                msg = fmt_msg(
                    symbol,
                    "RETESTE FRACO (15m)",
                    "Preço testou EMA9/MA20 e perdeu força (RSI<50)",
                    price15, rsi15, e9_15, m20_15, m50_15, m200_15, "15m"
                )
                await send_telegram(session, msg)
                set_cooldown(symbol, key)

    except Exception as e:
        # Protege o loop para nunca parar
        print(f"Erro ao analisar {symbol}:", e)

# ============== LOOP PRINCIPAL ==============

async def refresh_top_pairs(session: aiohttp.ClientSession):
    """Recarrega top_pairs com os TOP_N por volume entre SPOT USDT válidos."""
    global top_pairs
    symbols = await get_spot_usdt_pairs(session)
    top = await get_top_by_volume(session, symbols)
    top_pairs = top
    print(f"Top {TOP_N} atualizado. Total pares: {len(top_pairs)}")

async def main_loop():
    global last_top_refresh
    async with aiohttp.ClientSession() as session:
        # Mensagem inicial
        await send_telegram(session,
            f"✅ BOT ATIVO — Monitorando SPOT USDT\n"
            f"⏱️ Cooldown: 15 min por par/alerta\n"
            f"🔁 Atualização automática TOP {TOP_N}: a cada 1h\n"
            f"🇧🇷 {now_br_str()}"
        )

        # Primeira carga de TOP
        await refresh_top_pairs(session)
        last_top_refresh = datetime.utcnow().timestamp()
        if top_pairs:
            top5 = ", ".join(top_pairs[:5])
            await send_telegram(session, f"📦 Pares carregados (TOP {TOP_N}): {top5} …")

        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        while True:
            # Atualiza TOP a cada 1h
            if datetime.utcnow().timestamp() - last_top_refresh >= UPDATE_INTERVAL_SECONDS:
                await refresh_top_pairs(session)
                last_top_refresh = datetime.utcnow().timestamp()
                await send_telegram(session, f"🔄 Lista TOP {TOP_N} atualizada automaticamente 🇧🇷")

            # Analisa pares atuais
            tasks = []
            for s in top_pairs:
                async def wrapped(sym=s):
                    async with sem:
                        await analyze_symbol(session, sym)
                tasks.append(asyncio.create_task(wrapped()))
            if tasks:
                await asyncio.gather(*tasks)

            # Espera 5 minutos entre varreduras (compatível com 5m)
            await asyncio.sleep(300)

# ============== START ==============

if __name__ == "__main__":
    # Flask em thread separada (Render healthcheck)
    Thread(target=run_flask, daemon=True).start()

    # Loop principal assíncrono
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
