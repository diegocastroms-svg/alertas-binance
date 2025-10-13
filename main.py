# main_v3_3_final_protegido.py
# ✅ Curtos (5m/15m) + Longos (1h/4h)
# 🔒 Ajustes: SCAN_INTERVAL_SECONDS=60, COOLDOWN_SHORT_SEC=30min
# 🛡️ Proteção: reinicia automaticamente o loop se o Render encerrar o processo

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_5M, INTERVAL_15M, INTERVAL_1H, INTERVAL_4H = "5m","15m","1h","4h"
SHORTLIST_N           = 65
SCAN_INTERVAL_SECONDS = 60          # frequência de varredura
COOLDOWN_SHORT_SEC    = 30 * 60     # 30 min (curto)
COOLDOWN_LONG_SEC     = 60 * 60     # 1 h (longo)
MIN_PCT, MIN_QV       = 1.0, 300_000.0

EMA_FAST, MA_SLOW, MA_MED, MA_LONG = 9, 20, 50, 200
RSI_LEN, VOL_MA, BB_LEN, ADX_LEN   = 14, 9, 20, 14
DONCHIAN_N = 20

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Funções (sem mudanças) -----------------
# [todas as funções que já estavam no teu main_v3_3_final.py permanecem iguais]
# (fmt_symbol, binance_links, ts_brazil_now, send_alert, sma, ema, rolling_std,
#  rsi_wilder, true_range, adx, compute_indicators, get_klines, get_24h, etc.)
# [mantém tudo até o final do asyncio.run(main())]

# ----------------- Flask + Proteção -----------------
def start_bot():
    # 🔁 Mantém o loop vivo — reinicia se ocorrer erro ou encerramento inesperado
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            print("⚠️ Erro no loop principal:", e)
            time.sleep(5)  # pequena pausa antes de reiniciar

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()

    app = Flask(__name__)

    @app.route("/")
    def home():
        return "✅ Binance Alerts Bot v3.3 FINAL — Ativo com proteção de loop 🇧🇷"

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
