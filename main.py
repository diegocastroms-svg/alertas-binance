# main_curto_v3.3_exaustao15.py
# ✅ Igual à v3.3 anterior
# ✅ Única alteração: volume_ratio < 0,15 (antes era < 0,25)
# ✅ Nenhuma outra linha alterada

importar os, asyncio, aiohttp, matemática, tempo
de data e hora importar data e hora, fuso horário
do frasco importar frasco

BINANCE_HTTP = "https://api.binance.com"
INTERVALOS = ["5m", "15m"]
MIN_PCT = 0,0
MIN_QV = 10000,0
TEMPO DE RECARGA = 15 * 60

TOKEN = os.getenv("TELEGRAM_TOKEN")
ID_DO_CHAT = os.getenv("ID_DO_CHAT")

aplicativo = Flask(__nome__)

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

async def get_klines(sessão, símbolo, intervalo, limite=50):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    assíncrono com session.get(url, timeout=10) como r:
        retornar await r.json()

async def shortlist_from_24h(sessão):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24h"
    assíncrono com session.get(url, timeout=10) como r:
        dados = aguardar r.json()
    filtrado = [d para d em dados se d["símbolo"].endswith("USDT") e todos(x não em d["símbolo"] para x em ["PARA CIMA", "PARA BAIXO", "BUSD", "FDUSD", "TUSD", "USDC", "USD1"])]
    sorted_pairs = sorted(filtrado, chave=lambda x: float(x["quoteVolume"]), reverse=True)
    retornar [d["símbolo"] para d em sorted_pairs[:50]]

def ema(valores, período):
    k = 2 / (período + 1)
    valores_ema = []
    para i, preço em enumerate(valores):
        se i == 0: ema_values.append(preço)
        caso contrário: ema_values.append(preço * k + ema_values[-1] * (1 - k))
    retornar valores_ema

def sma(valores, período):
    retornar [soma(valores[i-período+1:i+1])/período se i+1>=período senão soma(valores[:i+1])/(i+1) para i no intervalo(len(valores))]

def cruzamento_up(a, b): retorna a[-2] < b[-2] e a[-1] > b[-1]
def cruzamento_down(a, b): retorna a[-2] > b[-2] e a[-1] < b[-1]

async def process_symbol(sessão, símbolo):
    tentar:
        k5 = await get_klines(sessão, símbolo, "5m")
        k15 = await get_klines(sessão, símbolo, "15m")
        c5 = [float(k[4]) para k em k5]
        c15 = [float(k[4]) para k em k15]
        v5 = [float(k[5]) para k em k5]
        v15 = [float(k[5]) para k em k15]

        ema9_5, ma20_5, ma50_5, ma200_5 = ema(c5,9), sma(c5,20), sma(c5,50), sma(c5,200)
        ema9_15, ma20_15, ma50_15, ma200_15 = ema(c15,9), sma(c15,20), sma(c15,50), sma(c15,200)

        avg_vol5 = soma(v5[-10:]) / 10
        avg_vol15 = soma(v15[-10:]) / 10
        vol_ratio_5 = v5[-1] / avg_vol5 se avg_vol5 senão 0
        vol_ratio_15 = v15[-1] / avg_vol15 se avg_vol15 senão 0

        # ⚙️ Ajuste ÚNICO: exaustão 15%
        exaustão_5 = vol_ratio_5 < 0,15
        exaustão_15 = vol_ratio_15 < 0,15

        ini_5m = índices_up(ema9_5, ma20_5) ou índices_up(ema9_5, ma50_5)
        pre_5m = índices_up(ma20_5, ma200_5) ou índices_up(ma50_5, ma200_5)
        pre_15m = índices_up(ema9_15, ma200_15)
        conf_15m = índices_up(ma20_15, ma200_15) ou índices_up(ma50_15, ma200_15)

        p = fmt(c5[-1])
        hora = nowbr()

        se ini_5m e não exaustao_5:
            await send_msg(session, f"🟢 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {p}\n🕒 {hora}")
        se pre_5m e não exaustao_5:
            await send_msg(session, f"🟡 {symbol} ⬆️ Tendência pré-confirmada (5m)\n💰 {p}\n🕒 {hora}")
        se pre_15m e não exaustao_15:
            await send_msg(session, f"🟡 {symbol} ⬆️ Tendência pré-confirmada (15m)\n💰 {p}\n🕒 {hora}")
        se conf_15m e não exaustao_15:
            await send_msg(session, f"🚀 {symbol} ⬆️ Tendência confirmada (15m)\n💰 {p}\n🕒 {hora}")

    exceto Exceção como e:
        print(f"Erro {símbolo}:", e)

async def main_loop():
    assíncrono com aiohttp.ClientSession() como sessão:
        símbolos = aguardar shortlist_from_24h(sessão)
        total = len(símbolos)
        await send_msg(session, f"✅ v3.3_exaustao15 intrabar ativo | {total} pares SPOT | cooldown 15m | {nowbr()} 🇧🇷")

        se total == 0:
            print("⚠️ Nenhum encontrado, revise filtros.")
            retornar

        tarefas = [process_symbol(session, s) para s em símbolos]
        aguarde asyncio.gather(*tarefas)

@app.route("/")
def casa():
    return "Binance Alertas v3.3_exaustao15 ativo", 200

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
