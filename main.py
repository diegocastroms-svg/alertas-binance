# main_v11_6_spot_only.py
# Base: v11.5_longterm_1h (mantida)
# Alteração única: FILTRO SPOT-ONLY real (via /api/v3/exchangeInfo)
# - Remove completamente pares Futuros/Perp/Alavancados da shortlist
# - Restante do setup permanece 100% igual ao da v11.5:
#     • Core 5m/15m intacto
#     • 🌕 Tendência Longa (15m/1h/4h) com cooldown de 1h e mensagem toda em negrito
#     • "Média 200 Ascendente" (Minervini) REMOVIDO (sem alertas)
#     • Rompimento da Resistência (ex-Turtle) mantido
#     • Retestes EMA9/MA20 com “CONTINUAÇÃO DE ALTA DETECTADA”
#     • Formatação HTML/🇧🇷/Flask/Telegram intactos

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"         # Core curto (inalterado)
INTERVAL_CONF = "15m"        # Confirmação curta (inalterado)
SHORTLIST_N   = 65
COOLDOWN_SEC  = 15 * 60      # cooldown geral por tipo (curto prazo)
COOLDOWN_LONGTERM = 60 * 60  # 1h por ativo para o alerta de tendência longa
MIN_PCT       = 1.0
MIN_QV        = 300_000.0

EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
VOL_MA   = 9
BB_LEN   = 20
ADX_LEN  = 14

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
def fmt_symbol(symbol):
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol):
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now():
    # horário de Brasília (UTC-3), sem nome "Brasília", só a bandeira 🇧🇷
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session, text):
    # (1) webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    # (2) Telegram
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except:
            pass

def pct_change(new, old):
    return (new / (old + 1e-12) - 1.0) * 100.0

# ----------------- Indicadores -----------------
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq: return []
    out = []
    alpha = 2.0 / (span + 1.0)
    e = seq[0]; out.append(e)
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def rolling_std(seq, n):
    out, q = [], deque()
    for x in seq:
        q.append(x)
        if len(q) > n: q.popleft()
        m = sum(q) / len(q)
        var = sum((v - m) ** 2 for v in q) / len(q)
        out.append(math.sqrt(var))
    return out

def rsi_wilder(closes, period=14):
    if len(closes) == 0: return []
    deltas = [0.0] + [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    rsis = [50.0] * len(closes)
    if len(closes) < period + 1: return rsis
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    for i in range(period+1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsis[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsis

def true_range(h, l, c):
    tr = [0.0]
    for i in range(1, len(c)):
        tr_curr = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        tr.append(tr_curr)
    return tr

def adx(h, l, c, period=14):
    n = len(c)
    if n < period + 1: return [20.0] * n, [0.0]*n, [0.0]*n
    tr = true_range(h, l, c)
    plus_dm  = [0.0]; minus_dm = [0.0]
    for i in range(1, n):
        up_move   = h[i] - h[i-1]
        down_move = l[i-1] - l[i]
        plus_dm.append(  up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append( down_move if (down_move > up_move and down_move > 0) else 0.0)
    atr = [0.0]*n
    atr[period] = sum(tr[1:period+1])
    pdm = [0.0]*n; mdm = [0.0]*n
    pdm[period] = sum(plus_dm[1:period+1]); mdm[period] = sum(minus_dm[1:period+1])
    for i in range(period+1, n):
        atr[i] = atr[i-1] - (atr[i-1] / period) + tr[i]
        pdm[i] = pdm[i-1] - (pdm[i-1] / period) + plus_dm[i]
        mdm[i] = mdm[i-1] - (mdm[i-1] / period) + minus_dm[i]
    atr[:period] = [sum(tr[1:period+1])]*(period)
    pdm[:period] = [sum(plus_dm[1:period+1])]*(period)
    mdm[:period] = [sum(minus_dm[1:period+1])]*(period)
    plus_di  = [0.0]*n; minus_di = [0.0]*n
    for i in range(n):
        plus_di[i]  = 100.0 * (pdm[i] / (atr[i] + 1e-12))
        minus_di[i] = 100.0 * (mdm[i] / (atr[i] + 1e-12))
    dx = [0.0]*n
    for i in range(n):
        dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i] + 1e-12)
    adx_vals = [0.0]*n
    adx_vals[period] = sum(dx[1:period+1]) / period
    for i in range(period+1, n):
        adx_vals[i] = (adx_vals[i-1] * (period - 1) + dx[i]) / period
    for i in range(period):
        adx_vals[i] = adx_vals[period]
    return adx_vals, plus_di, minus_di

def compute_indicators(o,h,l,c,v):
    ema9  = ema(c, EMA_FAST)
    ma20  = sma(c, MA_SLOW)
    ma50  = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    bb_std = rolling_std(c, BB_LEN)
    bb_up  = [ma20[i] + 2 * bb_std[i] for i in range(len(bb_std))]
    bb_low = [ma20[i] - 2 * bb_std[i] for i in range(len(bb_std))]
    adx14, pdi, mdi = adx(h, l, c, ADX_LEN)
    return ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, pdi, mdi

# ----------------- Binance: dados -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    # Remove o último candle em formação
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

# 🔒 NEW: obter apenas símbolos SPOT reais via exchangeInfo
async def get_spot_usdt_symbols(session):
    """
    Retorna um set com todos os símbolos USDT que são realmente SPOT (permissions contém 'SPOT')
    e estão com status TRADING. Exclui alavancados/derivativos por nome e por ausência de permissão SPOT.
    """
    url = f"{BINANCE_HTTP}/api/v3/exchangeInfo"
    try:
        async with session.get(url, timeout=15) as r:
            r.raise_for_status()
            data = await r.json()
    except Exception as e:
        print("exchangeInfo error:", e)
        return None  # fallback será tratado

    spot = set()
    blocked_tokens = ("UP","DOWN","BULL","BEAR")
    for s in data.get("symbols", []):
        try:
            sym = s.get("symbol","")
            status = s.get("status","")
            permissions = s.get("permissions", []) or []
            quote = s.get("quoteAsset","")
            # critérios SPOT reais:
            if status != "TRADING": 
                continue
            if quote != "USDT":
                continue
            if "SPOT" not in permissions:
                continue
            # excluir por nome tokens alavancados/estranhos
            if any(tok in sym for tok in blocked_tokens):
                continue
            # excluir pares com sufixos/formatos fora do padrão spot
            if any(x in sym for x in ("PERP","_PERP","USD_","_BUSD","_FDUSD","_TUSD","_EUR","_TRY","_BRL","_USDC","_DAI","_BTC")):
                continue
            spot.add(sym)
        except:
            continue
    return spot

def shortlist_from_24h(tickers, n=400, spot_set=None):
    """
    Monta shortlist com base no 24h, aplicando filtros MIN_PCT/MIN_QV e (se disponível) o spot_set real.
    """
    usdt = []
    for t in tickers:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        # se temos spot_set, só aceitaremos se estiver na lista SPOT real
        if spot_set is not None and s not in spot_set:
            continue
        # fallback adicional de nome (caso spot_set esteja None por erro)
        blocked = ("UP","DOWN","BULL","BEAR","PERP","USD_","_PERP","_BUSD","_FDUSD","_TUSD","_EUR","_TRY","_BRL","_USDC","_DAI","_BTC")
        if any(x in s for x in blocked):
            continue
        pct = float(t.get("priceChangePercent", "0") or 0.0)
        qv  = float(t.get("quoteVolume", "0") or 0.0)
        if abs(pct) >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Emojis / Mensagens -----------------
def kind_emoji(kind):
    return {
        "MONITORANDO_REVERSAO":"🔍",
        "TENDENCIA_INICIANDO_5M":"⬆️",
        "TENDENCIA_CONFIRMADA_15M":"💎",
        "REVERSAO_FUNDO":"🔄",
        "RETESTE_EMA9":"♻️",
        "RETESTE_MA20":"♻️",
        "PERDENDO_FORCA":"🟠",
        "SAIDA":"🚪",
        "MERCADO_ESTICADO":"⚠️",
        "TURTLE_BREAKOUT":"📈",
        "LONGTERM_TREND":"🌕",
    }.get(kind,"📌")

def build_msg(symbol, kind, price, bullets, rs_tag=""):
    star="⭐"; sym=fmt_symbol(symbol); em=kind_emoji(kind)
    tag = f" | 🏆 RS+" if rs_tag else ""
    # renomeio dinâmico de títulos
    if "TURTLE_BREAKOUT" in kind:
        header="📈 — ROMPIMENTO DA RESISTÊNCIA"
    else:
        header=f"{em} — {kind.replace('_',' ')}"
    return (
        f"{star} {sym} {header}{tag}\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"🧠 {bullets}\n"
        f"⏰ {ts_brazil_now()}\n"
        f"{binance_links(symbol)}"
    )

def build_msg_longterm(symbol, price, rsi_val, adx_val):
    # Mensagem inteira em negrito, conforme pedido
    sym = fmt_symbol(symbol)
    return (
        f"🌕 <b>{sym} — TENDÊNCIA LONGA DETECTADA</b>\n"
        f"<b>💰 Preço:</b> <code>{price:.6f}</code>\n"
        f"<b>📈 Estrutura:</b> EMA9>MA20>MA50>MA200 (15m / 1h / 4h)\n"
        f"<b>⚙️ Força:</b> RSI {rsi_val:.1f} | ADX {adx_val:.1f}\n"
        f"<b>🕒 {ts_brazil_now()}</b>\n"
        f"<b>{binance_links(symbol)}</b>\n"
        f"<b>ALTA SUSTENTADA — MOVIMENTO DE VÁRIOS DIAS POSSÍVEL.</b>"
    )

# ----------------- Monitor -----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)         # curto prazo
        self.cooldown_long = defaultdict(lambda: 0.0)     # longo prazo (1h por ativo)
        self.rs_24h = {}
        self.btc_pct = 0.0

    def allowed(self, symbol, kind):
        return time.time() - self.cooldown[(symbol, kind)] >= COOLDOWN_SEC

    def mark(self, symbol, kind):
        self.cooldown[(symbol, kind)] = time.time()

    def allowed_long(self, symbol):
        return time.time() - self.cooldown_long[symbol] >= COOLDOWN_LONGTERM

    def mark_long(self, symbol):
        self.cooldown_long[symbol] = time.time()

    def set_rs(self, rs_map, btc_pct):
        self.rs_24h = rs_map or {}
        self.btc_pct = btc_pct or 0.0

    def rs_tag(self, symbol):
        pct = self.rs_24h.get(symbol, None)
        if pct is None: return ""
        return "RS+" if (pct - self.btc_pct) > 0.0 else ""

# ----------------- Worker curto (inalterado, exceto sem Minervini) -----------------
async def candle_worker(session, symbol, monitor: Monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_MAIN, limit=200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, vol_ma, bb_up, bb_low, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        last = len(c)-1
        signals=[]
        rs_tag = monitor.rs_tag(symbol)

        # Reteste EMA9 (com continuação de alta)
        if (ema9[last]>ma20[last]>ma50[last] and l[last]<=ema9[last] and c[last]>=ema9[last] and
            rsi14[last]>=55.0 and v[last]>=vol_ma[last]*0.9):
            signals.append(("RETESTE_EMA9",
                f"Reteste na EMA9 + reação | RSI {rsi14[last]:.1f} | Vol ok | 💚 CONTINUAÇÃO DE ALTA DETECTADA"))

        # Reteste MA20 (com continuação de alta)
        if (ema9[last]>ma20[last]>ma50[last] and l[last]<=ma20[last] and c[last]>=ma20[last] and
            rsi14[last]>=52.0 and v[last]>=vol_ma[last]*0.9):
            signals.append(("RETESTE_MA20",
                f"Reteste na MA20 + reação | RSI {rsi14[last]:.1f} | Vol ok | 💚 CONTINUAÇÃO DE ALTA DETECTADA"))

        # Rompimento da resistência (Donchian 20-high)
        if len(h) >= 21:
            donchian_high=max(h[-20:])
            if c[last]>donchian_high and monitor.allowed(symbol,"TURTLE_BREAKOUT"):
                msg=build_msg(symbol,"TURTLE_BREAKOUT",c[last],
                              f"Rompimento: fechou acima da máxima 20 ({donchian_high:.6f}) — 💥 Rompimento confirmado",
                              rs_tag)
                await send_alert(session,msg)
                monitor.mark(symbol,"TURTLE_BREAKOUT")

        # Envio do primeiro sinal composto (se houver)
        if signals:
            k0,d0=signals[0]
            if monitor.allowed(symbol,k0):
                msg=build_msg(symbol,k0,c[last],d0,rs_tag)
                await send_alert(session,msg)
                monitor.mark(symbol,k0)

    except Exception as e:
        print("worker error",symbol,e)

# ----------------- Worker LONGO (novo desde 11.5) -----------------
async def longterm_worker(session, symbol, monitor: Monitor):
    """
    Monitora 15m + 1h + 4h para detectar tendência longa:
    - EMA9 > MA20 > MA50 > MA200 nos três tempos
    - RSI > 55
    - ADX > 25
    Cooldown: 1h por ativo
    """
    try:
        # 15m
        o15,h15,l15,c15,v15 = await get_klines(session, symbol, interval="15m", limit=120)
        if len(c15) < 60: return
        i15 = compute_indicators(o15,h15,l15,c15,v15)
        ema9_15, ma20_15, ma50_15, ma200_15, rsi15, volma15, bbup15, bblow15, adx15, pdi15, mdi15 = i15
        last15 = len(c15)-1

        # 1h
        o1,h1,l1,c1,v1 = await get_klines(session, symbol, interval="1h", limit=120)
        if len(c1) < 60: return
        i1 = compute_indicators(o1,h1,l1,c1,v1)
        ema9_1, ma20_1, ma50_1, ma200_1, rsi1, volma1, bbup1, bblow1, adx1, pdi1, mdi1 = i1
        last1 = len(c1)-1

        # 4h
        o4,h4,l4,c4,v4 = await get_klines(session, symbol, interval="4h", limit=120)
        if len(c4) < 60: return
        i4 = compute_indicators(o4,h4,l4,c4,v4)
        ema9_4, ma20_4, ma50_4, ma200_4, rsi4, volma4, bbup4, bblow4, adx4, pdi4, mdi4 = i4
        last4 = len(c4)-1

        # Condições de estrutura (nos 3 tempos)
        cond_15 = (ema9_15[last15] > ma20_15[last15] > ma50_15[last15] > ma200_15[last15] and
                   rsi15[last15] > 55.0 and adx15[last15] > 25.0)
        cond_1  = (ema9_1[last1]   > ma20_1[last1]   > ma50_1[last1]   > ma200_1[last1]   and
                   rsi1[last1]    > 55.0 and adx1[last1]    > 25.0)
        cond_4  = (ema9_4[last4]   > ma20_4[last4]   > ma50_4[last4]   > ma200_4[last4]   and
                   rsi4[last4]    > 55.0 and adx4[last4]    > 25.0)

        if cond_15 and cond_1 and cond_4 and monitor.allowed_long(symbol):
            last_price = c15[last15]  # preço mais responsivo (15m)
            rsi_mean = (rsi1[last1] + rsi4[last4]) / 2.0
            adx_mean = (adx1[last1] + adx4[last4]) / 2.0
            txt = build_msg_longterm(symbol, last_price, rsi_mean, adx_mean)
            await send_alert(session, txt)
            monitor.mark_long(symbol)

    except Exception as e:
        print("longterm error", symbol, e)

# ----------------- Main -----------------
async def main():
    monitor=Monitor()
    async with aiohttp.ClientSession() as session:
        # NEW: obter set de símbolos SPOT reais
        spot_set = await get_spot_usdt_symbols(session)
        if spot_set is None:
            print("⚠️ Aviso: não foi possível obter exchangeInfo; usando filtro por nome como fallback.")

        tickers=await get_24h(session)
        watchlist=shortlist_from_24h(tickers,SHORTLIST_N, spot_set=spot_set)

        # RS (força relativa vs BTC) para tag 🏆 RS+
        rs_map = {}
        btc_pct = 0.0
        for t in tickers:
            s = t.get("symbol","")
            if s == "BTCUSDT":
                try: btc_pct = float(t.get("priceChangePercent","0") or 0.0)
                except: btc_pct = 0.0
            if s.endswith("USDT"):
                try: rs_map[s] = float(t.get("priceChangePercent","0") or 0.0)
                except: rs_map[s] = 0.0
        monitor.set_rs(rs_map, btc_pct)

        hello=f"💻 v11.6 | Core 5m/15m intacto + LongTerm(15m/1h/4h, cooldown 1h) + SPOT-only | {len(watchlist)} pares | {ts_brazil_now()}"
        await send_alert(session,hello)
        print(hello)

        while True:
            # Workers em paralelo: curto + longo
            tasks = []
            for s in watchlist:
                tasks.append(candle_worker(session, s, monitor))
                tasks.append(longterm_worker(session, s, monitor))
            await asyncio.gather(*tasks)

            # pausa entre ciclos
            await asyncio.sleep(180)

            # refresh shortlist + RS + SPOT a cada ciclo
            try:
                spot_set = await get_spot_usdt_symbols(session) or spot_set
                tickers=await get_24h(session)
                watchlist=shortlist_from_24h(tickers,SHORTLIST_N, spot_set=spot_set)

                rs_map = {}
                btc_pct = 0.0
                for t in tickers:
                    s = t.get("symbol","")
                    if s == "BTCUSDT":
                        try: btc_pct = float(t.get("priceChangePercent","0") or 0.0)
                        except: btc_pct = 0.0
                    if s.endswith("USDT"):
                        try: rs_map[s] = float(t.get("priceChangePercent","0") or 0.0)
                        except: rs_map[s] = 0.0
                monitor.set_rs(rs_map, btc_pct)
            except Exception as e:
                print("Erro ao atualizar shortlist/RS/SPOT:", e)

# ----------------- Flask -----------------
def start_bot():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

if __name__=="__main__":
    import threading
    threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home():
        return "✅ Binance Alerts Bot v11.6 — Core (5m/15m) + LongTerm (15m/1h/4h, 1h) + SPOT-only 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
