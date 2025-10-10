# main_v2_2.py
# Base: v2.1 estável
# Alteração ÚNICA nesta versão:
#  - "Silenciamento inteligente" do 5m: depois que (EMA9>MA20>MA50>MA200) no 5m,
#    o 5m PARA de enviar novos alertas e o 15m assume (pré-confirmação, confirmação,
#    retestes, entradas). O 5m reativa automaticamente se a estrutura perder a MA200.

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"

INTERVAL_5M   = "5m"
INTERVAL_15M  = "15m"
INTERVAL_1H   = "1h"
INTERVAL_4H   = "4h"

SHORTLIST_N   = 80            # pares SPOT monitorados
COOLDOWN_SEC  = 15 * 60       # cooldown curto por tipo de alerta
COOLDOWN_LONG = 60 * 60       # cooldown longo (1h) por ativo (para alertas 1h/4h)
MIN_PCT       = 1.0           # variação mínima 24h p/ shortlist
MIN_QV        = 300_000.0     # volume cotado mínimo 24h p/ shortlist

# Indicadores
EMA_FAST      = 9
MA_SLOW       = 20
MA_MED        = 50
MA_LONG       = 200
RSI_LEN       = 14
VOL_MA        = 9
BB_LEN        = 20
ADX_LEN       = 14

# Credenciais
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
def fmt_symbol(symbol):
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def trade_links(symbol):
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session, text):
    # webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    # Telegram
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

# ----------------- Binance -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    # Remove candle atual em formação
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

# Filtro SPOT reforçado
def shortlist_from_24h(tickers, n=400):
    usdt = []
    for t in tickers:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        blocked = (
            "UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD",
            "_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_BTC",
            "_EUR","_TRY","_BRL","_ETH","_BNB","_SOL"
        )
        if any(x in s for x in blocked):
            continue
        pct = float(t.get("priceChangePercent", "0") or 0.0)
        qv  = float(t.get("quoteVolume", "0") or 0.0)
        if abs(pct) >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Mensagens -----------------
def ball_color(kind):
    # Tendência: 🟢 alta, 🟡 lateral, 🔴 queda. Força/saída: 🟠/❌.
    if kind in ("INICIO_5M","PRECONF_5M","PRECONF_15M","CONFIRM_15M","BREAKOUT","ENTRY_SAFE","ENTRY_BOOM","RETESTE"):
        return "🟢"
    if kind in ("LATERAL","WATCH_LATERAL"):
        return "🟡"
    if kind in ("QUEDA","WATCH_DROP"):
        return "🔴"
    if kind == "PERDENDO_FORCA":
        return "🟠"
    if kind == "SAIDA":
        return "❌"
    # Longos (negrito) usam 🌕/🚀, mas mantemos bola verde no topo
    if kind.startswith("LONG_"):
        return "🟢"
    return "🟢"

def arrow(kind):
    return "⬆️" if kind in ("INICIO_5M","PRECONF_5M","PRECONF_15M","CONFIRM_15M","BREAKOUT","ENTRY_SAFE","ENTRY_BOOM","RETESTE") else "⬇️" if kind in ("QUEDA","SAIDA") else "➡️"

def header_line(symbol, kind, title):
    # Topo: Bola + PAR centralizado (visual via linha separada)
    sym = fmt_symbol(symbol)
    return f"{ball_color(kind)} <b>{sym}</b>\n{arrow(kind)} {title}"

def body_block(price, bullets):
    return f"💰 <code>{price:.6f}</code>\n🧠 {bullets}\n⏰ {ts_brazil_now()}"

def links_line(symbol):
    return trade_links(symbol)

# ----------------- Monitor -----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)       # curto
        self.cooldown_long = defaultdict(lambda: 0.0)   # longo 1h/4h
        self.rs_map = {}
        self.btc_pct = 0.0
        self.trend5m_active = defaultdict(lambda: False)  # >>> silenciamento 5m

    def allowed(self, symbol, key, cdsec=COOLDOWN_SEC):
        return time.time() - self.cooldown[(symbol, key)] >= cdsec

    def mark(self, symbol, key):
        self.cooldown[(symbol, key)] = time.time()

    def allowed_long(self, symbol):
        return time.time() - self.cooldown_long[symbol] >= COOLDOWN_LONG

    def mark_long(self, symbol):
        self.cooldown_long[symbol] = time.time()

    def set_rs(self, rs_map, btc_pct):
        self.rs_map = rs_map or {}
        self.btc_pct = btc_pct or 0.0

# ----------------- Workers -----------------
async def worker_5m(session, symbol, mon: Monitor):
    """
    5m:
      - Início após queda+lateral (EMA9 cruza MA20/50)
      - Pré-confirmação 5m (9/20/50 cruzam ACIMA da 200)
      - Retestes / Entradas (se ainda NÃO silenciado)
      - Silenciamento: quando 9>20>50>200 -> mon.trend5m_active[symbol]=True
      - Reativação: se EMA9 voltar a FICAR ABAIXO da MA200 -> False
    """
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_5M, limit=200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        last = len(c)-1; prev = last-1
        price = c[last]

        # Reativação automática se perder MA200 (voltar a analisar 5m)
        if mon.trend5m_active[symbol] and ema9[last] < ma200[last]:
            mon.trend5m_active[symbol] = False  # perdeu estrutura -> 5m volta a falar

        # Se JÁ silenciado, apenas monitora (não manda 5m) até 15m assumir
        if mon.trend5m_active[symbol]:
            return

        # 1) Início 5m: EMA9 cruzou MA20 e MA50 após fundo/lateral
        cond_inicio = (
            ema9[prev] <= ma20[prev] and ema9[prev] <= ma50[prev] and
            ema9[last] >  ma20[last] and ema9[last] >  ma50[last] and
            rsi14[last] >= 48.0
        )
        if cond_inicio and mon.allowed(symbol,"INICIO_5M"):
            msg = (
                f"{header_line(symbol,'INICIO_5M','TENDÊNCIA INICIANDO (5m)')}\n"
                f"{body_block(price,'EMA9 cruzou MA20 e MA50 após queda + lateralização')}\n"
                f"{links_line(symbol)}"
            )
            await send_alert(session, msg)
            mon.mark(symbol,"INICIO_5M")

        # 2) Pré-confirmação 5m: 9/20/50 > 200
        cond_pre5 = (ema9[last] > ma20[last] > ma50[last] > ma200[last])
        if cond_pre5 and mon.allowed(symbol,"PRECONF_5M", cdsec=COOLDOWN_SEC//2):
            msg = (
                f"{header_line(symbol,'PRECONF_5M','PRÉ-CONFIRMAÇÃO (5m)')}\n"
                f"{body_block(price,'Médias 9/20/50 cruzaram acima da MA200 (5m) | RSI %.1f | ADX %.1f' % (rsi14[last], adx14[last]))}\n"
                f"{links_line(symbol)}"
            )
            await send_alert(session, msg)
            mon.mark(symbol,"PRECONF_5M")
            # >>> Ativa silenciamento do 5m
            mon.trend5m_active[symbol] = True

        # 3) (Opcional) Retestes / entradas ainda no 5m ANTES de silenciar — se quiser, mantenha;
        #    Como agora silenciamos assim que PRECONF_5M dispara, só chegarão se PRECONF ainda não ocorreu.

        # Entrada segura 5m (pré-silêncio): toque EMA9/MA20 com RSI 45–55 + vol>média
        touched_ema9 = (l[last] <= ema9[last] and c[last] >= ema9[last])
        touched_ma20 = (l[last] <= ma20[last] and c[last] >= ma20[last])
        if (not mon.trend5m_active[symbol]) and (touched_ema9 or touched_ma20) and 45.0 <= rsi14[last] <= 55.0 and v[last] >= volma[last]*1.05:
            if mon.allowed(symbol,"ENTRY_SAFE"):
                msg = (
                    f"{header_line(symbol,'ENTRY_SAFE','ENTRADA SEGURA (5m)')}\n"
                    f"{body_block(price,'Toque na EMA9/MA20 + RSI moderado + volume acima da média')}\n"
                    f"{links_line(symbol)}"
                )
                await send_alert(session, msg)
                mon.mark(symbol,"ENTRY_SAFE")

        # Explosiva (5m): candle fecha acima da máxima 20 (Donchian)
        if last >= 21 and not mon.trend5m_active[symbol]:
            donch_high = max(h[last-20:last])
            if c[last] > donch_high and mon.allowed(symbol,"BREAKOUT"):
                msg = (
                    f"{header_line(symbol,'BREAKOUT','ROMPIMENTO DA RESISTÊNCIA (5m)')}\n"
                    f"{body_block(price,'Fechou acima da máxima 20 — rompimento confirmado')}\n"
                    f"{links_line(symbol)}"
                )
                await send_alert(session, msg)
                mon.mark(symbol,"BREAKOUT")

    except Exception as e:
        print("erro worker_5m", symbol, e)

async def worker_15m(session, symbol, mon: Monitor):
    """
    15m assume depois que o 5m silencia:
      - Pré-confirmação 15m: EMA9 cruza MA200
      - Confirmação 15m: EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25
      - Retestes / Entradas
    """
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_15M, limit=200)
        if len(c) < 60: return
        ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        last = len(c)-1; prev = last-1
        price = c[last]

        # Só faz sentido focar no 15m quando o 5m já entrou em tendência
        if not mon.trend5m_active[symbol]:
            return

        # Pré-confirmação 15m: EMA9 cruzou para cima da MA200
        if ema9[prev] <= ma200[prev] and ema9[last] > ma200[last] and mon.allowed(symbol,"PRECONF_15M"):
            msg = (
                f"{header_line(symbol,'PRECONF_15M','PRÉ-CONFIRMAÇÃO (15m)')}\n"
                f"{body_block(price,'EMA9 cruzou para cima da MA200 no 15m | RSI %.1f | ADX %.1f' % (rsi14[last], adx14[last]))}\n"
                f"{links_line(symbol)}"
            )
            await send_alert(session, msg)
            mon.mark(symbol,"PRECONF_15M")

        # Confirmação 15m
        if (ema9[last] > ma20[last] > ma50[last] > ma200[last] and rsi14[last] > 55.0 and adx14[last] > 25.0
            and mon.allowed(symbol,"CONFIRM_15M")):
            msg = (
                f"{header_line(symbol,'CONFIRM_15M','TENDÊNCIA CONFIRMADA (15m)')}\n"
                f"{body_block(price,'EMA9>MA20>MA50>MA200 + RSI>55 + ADX>25 (15m)')}\n"
                f"{links_line(symbol)}"
            )
            await send_alert(session, msg)
            mon.mark(symbol,"CONFIRM_15M")

        # Retestes 15m
        touched_ema9 = (l[last] <= ema9[last] and c[last] >= ema9[last])
        if touched_ema9 and ema9[last] > ma20[last] > ma50[last] and mon.allowed(symbol,"RETESTE"):
            msg = (
                f"{header_line(symbol,'RETESTE','RETESTE NA EMA9 (15m)')}\n"
                f"{body_block(price,'Toque na EMA9 e reação — Continuação da alta detectada')}\n"
                f"{links_line(symbol)}"
            )
            await send_alert(session, msg)
            mon.mark(symbol,"RETESTE")

        # Entrada segura 15m
        touched_ma20 = (l[last] <= ma20[last] and c[last] >= ma20[last])
        if (touched_ema9 or touched_ma20) and 45.0 <= rsi14[last] <= 55.0 and v[last] >= volma[last]*1.05:
            if mon.allowed(symbol,"ENTRY_SAFE_15"):
                msg = (
                    f"{header_line(symbol,'ENTRY_SAFE','ENTRADA SEGURA (15m)')}\n"
                    f"{body_block(price,'Toque EMA9/MA20 + RSI 45–55 + volume acima da média')}\n"
                    f"{links_line(symbol)}"
                )
                await send_alert(session, msg)
                mon.mark(symbol,"ENTRY_SAFE_15")

    except Exception as e:
        print("erro worker_15m", symbol, e)

# (Mantidos) LONGOS 1h/4h — mensagens em negrito
def long_msg(symbol, title, price, lines):
    sym = fmt_symbol(symbol)
    extra = "\n".join(f"<b>{ln}</b>" for ln in lines if ln)
    return (
        f"🌕 <b>{sym} — {title}</b>\n"
        f"<b>💰 Preço:</b> <code>{price:.6f}</code>\n"
        f"{extra}\n"
        f"<b>🕒 {ts_brazil_now()}</b>\n"
        f"<b>{trade_links(symbol)}</b>"
    )

async def worker_long(session, symbol, mon: Monitor):
    try:
        # 1h
        o1,h1,l1,c1,v1 = await get_klines(session, symbol, interval=INTERVAL_1H, limit=120)
        if len(c1) < 60: return
        ema9_1, ma20_1, ma50_1, ma200_1, rsi1, volma1, *_ , adx1, _, _ = compute_indicators(o1,h1,l1,c1,v1)
        last1 = len(c1)-1

        # 4h
        o4,h4,l4,c4,v4 = await get_klines(session, symbol, interval=INTERVAL_4H, limit=120)
        if len(c4) < 60: return
        ema9_4, ma20_4, ma50_4, ma200_4, rsi4, volma4, *_ , adx4, _, _ = compute_indicators(o4,h4,l4,c4,v4)
        last4 = len(c4)-1

        # Pré 1h (1ª vela): EMA9 cruza MA20 com RSI 50–60 + vol crescente
        if (ema9_1[last1-1] <= ma20_1[last1-1] and ema9_1[last1] > ma20_1[last1] and
            50.0 <= rsi1[last1] <= 60.0 and v1[last1] >= volma1[last1]*1.05 and mon.allowed_long(symbol)):
            txt = long_msg(symbol, "PRÉ-CONFIRMAÇÃO (1h)", c1[last1],
                           [f"RSI {rsi1[last1]:.1f} | ADX {adx1[last1]:.1f}",
                            "EMA9 cruzou MA20 (1h)", "Volume acima da média"])
            await send_alert(session, txt)
            mon.mark_long(symbol)
            return

        # Confirmada 1h (2ª vela): EMA9>MA20>MA50 + RSI>55 + ADX>25
        if (ema9_1[last1] > ma20_1[last1] > ma50_1[last1] and rsi1[last1] > 55.0 and adx1[last1] > 25.0
            and mon.allowed_long(symbol)):
            txt = long_msg(symbol, "TENDÊNCIA CONFIRMADA (1h)", c1[last1],
                           [f"RSI {rsi1[last1]:.1f} | ADX {adx1[last1]:.1f}",
                            "EMA9>MA20>MA50 (1h)"])
            await send_alert(session, txt)
            mon.mark_long(symbol)
            return

        # Pré 4h (1ª vela): EMA9 cruza MA20 + RSI>50
        if (ema9_4[last4-1] <= ma20_4[last4-1] and ema9_4[last4] > ma20_4[last4] and
            rsi4[last4] > 50.0 and mon.allowed_long(symbol)):
            txt = long_msg(symbol, "PRÉ-CONFIRMAÇÃO (4h)", c4[last4],
                           [f"RSI {rsi4[last4]:.1f} | ADX {adx4[last4]:.1f}",
                            "EMA9 cruzou MA20 (4h)"])
            await send_alert(session, txt)
            mon.mark_long(symbol)
            return

        # Confirmada 4h (2ª vela): estrutura mantida 2 velas + RSI>55
        if (ema9_4[last4] > ma20_4[last4] > ma50_4[last4] and
            ema9_4[last4-1] > ma20_4[last4-1] > ma50_4[last4-1] and
            rsi4[last4] > 55.0 and mon.allowed_long(symbol)):
            txt = long_msg(symbol, "TENDÊNCIA CONFIRMADA (4h)", c4[last4],
                           [f"RSI {rsi4[last4]:.1f} | ADX {adx4[last4]:.1f}",
                            "Estrutura mantida por 2 velas (4h)"])
            await send_alert(session, txt)
            mon.mark_long(symbol)
            return

    except Exception as e:
        print("erro worker_long", symbol, e)

# ----------------- Main loop -----------------
async def main_loop():
    mon = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watch   = shortlist_from_24h(tickers, SHORTLIST_N)

        # força relativa simples para uso futuro (mantido)
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
        mon.set_rs(rs_map, btc_pct)

        hello = f"💻 v2.2 | 5m com silenciamento inteligente ➜ 15m assume | Longos 1h/4h preservados | {len(watch)} pares SPOT | {ts_brazil_now()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks = []
            for s in watch:
                tasks.append(worker_5m(session, s, mon))
                tasks.append(worker_15m(session, s, mon))
                tasks.append(worker_long(session, s, mon))
            await asyncio.gather(*tasks)

            # pausa
            await asyncio.sleep(180)

            # refresh shortlist / RS
            try:
                tickers = await get_24h(session)
                watch   = shortlist_from_24h(tickers, SHORTLIST_N)
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
                mon.set_rs(rs_map, btc_pct)
            except Exception as e:
                print("erro refresh", e)

# ----------------- Flask (Render) -----------------
def start_bot():
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()

    app = Flask(__name__)

    @app.route("/")
    def home():
        return "✅ Binance Alerts v2.2 — 5m silenciado após cruzar 200 • 15m assume • Longos 1h/4h • SPOT-only 🇧🇷"

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
