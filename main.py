# main_curto_v3.2_limit50.py
# ✅ Corrigido: shortlist limitada às 50 moedas com maior volume
# ✅ Mantido: intrabar ativo, alertas 5m/15m
# ✅ Nenhuma outra linha alterada

importar os, asyncio, aiohttp, matemática, tempo
de data e hora importar data e hora, fuso horário
do frasco importar frasco

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVALOS = ["5m", "15m"]
MIN_PCT = 0,0
MIN_QV = 10000,0
TEMPO DE RECARGA = 15 * 60

TOKEN = os.getenv("TELEGRAM_TOKEN")
ID_DO_CHAT = os.getenv("ID_DO_CHAT")

aplicativo = Flask(__nome__)

# ---------------- UTILITÁRIOS ----------------
async def send_msg(sessão, texto):
    tentar:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        carga útil = {"chat_id": CHAT_ID, "texto": texto, "parse_mode": "HTML"}
        aguardar sessão.post(url, dados=carga útil)
    exceto Exceção como e:
        print("Erro send_msg:", e)

def fmt(num): retornar f"{num:.6f}".rstrip("0").rstrip(".")

def nowbr():
    retornar datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

# ---------------- BINANCE ----------------
async def get_klines(sessão, símbolo, intervalo, limite=50):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    assíncrono com session.get(url, timeout=10) como r:
        retornar await r.json()

async def shortlist_from_24h(sessão):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24h"
    assíncrono com session.get(url, timeout=10) como r:
        dados = aguardar r.json()
    símbolos = []
    para d em dados:
        s = d["símbolo"]
        se não s.endswith("USDT"): continue
        se houver(x em s para x em ["PARA CIMA", "PARA BAIXO", "BUSD", "FDUSD", "TUSD", "USDC", "USD1"]): continue
        tentar:
            qv = float(d["quoteVolume"])
            pct = abs(float(d["preçoAlteraçãoPorcentagem"]))
            se qv > MIN_QV e pct >= MIN_PCT:
                símbolos.append((s, qv))
        exceto:
            continuar
    # 🔹 Mantém apenas as 50 com maior volume
    símbolos = classificados(símbolos, chave=lambda x: x[1], reverso=Verdadeiro)[:50]
    retornar [s para s, _ em símbolos]

def ema(valores, período):
    k = 2 / (período + 1)
    valores_ema = []
    para i, preço em enumerate(valores):
        se i == 0:
            ema_values.append(preço)
        outro:
            ema_values.append(preço * k + ema_values[-1] * (1 - k))
    retornar valores_ema

def sma(valores, período):
    retornar [soma(valores[i-período+1:i+1])/período se i+1>=período senão soma(valores[:i+1])/(i+1) para i no intervalo(len(valores))]

# ---------------- ALERTAS ----------------
def cruzamento_up(a, b): retorna a[-2] < b[-2] e a[-1] > b[-1]
def cruzamento_down(a, b): retorna a[-2] > b[-2] e a[-1] < b[-1]

async def process_symbol(sessão, símbolo):
    tentar:
        k5 = await get_klines(sessão, símbolo, "5m")
        k15 = await get_klines(sessão, símbolo, "15m")
        c5 = [float(k[4]) para k em k5]
        c15 = [float(k[4]) para k em k15]

        ema9_5, ma20_5, ma50_5, ma200_5 = ema(c5,9), sma(c5,20), sma(c5,50), sma(c5,200)
        ema9_15, ma20_15, ma50_15, ma200_15 = ema(c15,9), sma(c15,20), sma(c15,50), sma(c15,200)

        # ---- Cruzamentos ----
        ini_5m = índices_up(ema9_5, ma20_5) ou índices_up(ema9_5, ma50_5)
        pre_5m = índices_up(ma20_5, ma200_5) ou índices_up(ma50_5, ma200_5)
        pre_15m = índices_up(ema9_15, ma200_15)
        conf_15m = índices_up(ma20_15, ma200_15) ou índices_up(ma50_15, ma200_15)

        p = fmt(c5[-1])
        hora = nowbr()

        se ini_5m:
            await send_msg(session, f"🟢 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {p}\n🕒 {hora}")
        se pre_5m:
            await send_msg(session, f"🟡 {symbol} ⬆️ Tendência pré-confirmada (5m)\n💰 {p}\n🕒 {hora}")
        se pre_15m:
            await send_msg(session, f"🟡 {symbol} ⬆️ Tendência pré-confirmada (15m)\n💰 {p}\n🕒 {hora}")
        se conf_15m:
            await send_msg(session, f"🚀 {symbol} ⬆️ Tendência confirmada (15m)\n💰 {p}\n🕒 {hora}")

    exceto Exceção como e:
        print(f"Erro {símbolo}:", e)

# ---------------- LAÇO ----------------
async def main_loop():
    assíncrono com aiohttp.ClientSession() como sessão:
        símbolos = aguardar shortlist_from_24h(sessão)
        total = len(símbolos)
        aguarde send_msg(session, f"✅ v3.2_limit50 intrabar ativo | {total} pares SPOT | cooldown 15m | {nowbr()} 🇧🇷")

        se total == 0:
            print("⚠️ Nenhum encontrado, revise filtros.")
            retornar

        tarefas = [process_symbol(session, s) para s em símbolos]
        aguarde asyncio.gather(*tarefas)

@app.route("/")
def casa():
    return "Binance Alertas v3.2_limit50 ativo", 200

se __nome__ == "__principal__":
    encadeamento de importação

    def corredor():
        enquanto Verdadeiro:
            tentar:
                asyncio.run(loop_principal())
            exceto Exceção como e:
                print("Erro de loop:", e)
            tempo.sleep(TEMPO DE RECARGA)

    encadeamento.Thread(alvo=runner, daemon=True).start()
    app.run(host="0.0.0.0", porta=int(os.getenv("PORTA", 10000)))
