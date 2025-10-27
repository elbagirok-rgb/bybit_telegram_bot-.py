# ============================================================
#   Arbitrage Bot v2.2 (Bybit ↔ OKX)
#   💰 Минимальный спред, но без убыточных сделок
#   ⚙️ Diagnostic Mode + Telegram управление
# ============================================================

import os, time, threading, logging
import ccxt, telebot
from telebot import types

# === НАСТРОЙКИ =============================================
BYBIT_API_KEY    = "bI2fcFjKVNY4W6oQs9"
BYBIT_API_SECRET = "QYawByd3Gz8BUXWebZpDMYircORbWY7zD2cV"

OKX_API_KEY      = "6fbfe3bc-3a0a-42b9-985a-d681ec369c78"
OKX_API_SECRET   = "1930C87E73EBC8D00B55F962FBCD9D93"
OKX_PASSWORD     = "Movhafvx7."

TELEGRAM_TOKEN   = "7400072123:AAH098YiSanx1R_0MZB9mk6qr2RuxhJvx_k"
TELEGRAM_CHAT_ID = 537054215

# === ПАРАМЕТРЫ =============================================
POLL_INTERVAL = 3.0        # частота обновления (сек)
TRADE_QTY = 0.01           # объём для расчёта прибыли
DRY_RUN = False             # реальная торговля (False = Live)
DIAGNOSTIC_MODE = True      # выводит все спреды в консоль
TAKER_FEE = 0.0006          # комиссия тейкера (0.06%)

# === ПАРЫ ===================================================
WATCH_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "TRX/USDT",
    "DOT/USDT", "MATIC/USDT", "OP/USDT", "ARB/USDT", "SUI/USDT",
    "APT/USDT", "SEI/USDT", "NEAR/USDT", "FIL/USDT", "ATOM/USDT",
    "LTC/USDT", "ETC/USDT", "UNI/USDT", "AAVE/USDT", "RNDR/USDT",
    "INJ/USDT", "XMR/USDT", "EGLD/USDT", "CFX/USDT", "TON/USDT",
    "IMX/USDT", "FTM/USDT", "PEPE/USDT", "XLM/USDT", "HBAR/USDT",
    "MANA/USDT", "SAND/USDT", "THETA/USDT", "GALA/USDT", "CHZ/USDT"
]

# === ЛОГИРОВАНИЕ ===========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("arb_bot")

# === ИНИЦИАЛИЗАЦИЯ БИРЖ ===================================
bybit = ccxt.bybit({
    "apiKey": BYBIT_API_KEY,
    "secret": BYBIT_API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

okx = ccxt.okx({
    "apiKey": OKX_API_KEY,
    "secret": OKX_API_SECRET,
    "password": OKX_PASSWORD,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

bybit.load_markets()
okx.load_markets()

# === TELEGRAM ==============================================
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
trade_enabled = False
monitor_enabled = True

# === БАЛАНСЫ ===============================================
def get_bybit_free_usdt() -> float:
    try:
        resp = bybit.private_get_v5_account_wallet_balance({"accountType": "UNIFIED"})
        coins = resp.get("result", {}).get("list", [])[0].get("coin", [])
        for c in coins:
            if c.get("coin") == "USDT":
                val = c.get("availableToWithdraw") or c.get("walletBalance") or c.get("equity") or 0
                return float(val)
        return 0.0
    except Exception as e:
        log.warning(f"Bybit balance fetch error: {e}")
        return 0.0

def get_okx_free_usdt() -> float:
    try:
        balances = okx.fetch_balance()
        usdt = balances.get("USDT", {})
        return float(usdt.get("free", 0))
    except Exception as e:
        log.warning(f"OKX balance fetch error: {e}")
        return 0.0

# === TELEGRAM МЕНЮ =========================================
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("💼 Балансы"))
    kb.add(types.KeyboardButton("🟢 Запустить мониторинг") if not monitor_enabled else types.KeyboardButton("🛑 Остановить мониторинг"))
    kb.add(types.KeyboardButton("▶️ Включить торговлю") if not trade_enabled else types.KeyboardButton("⏸ Выключить торговлю"))
    return kb

@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(
        TELEGRAM_CHAT_ID,
        "🤖 Арбитражный бот запущен.\nМониторинг активен ✅",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda m: True)
def handle_buttons(m):
    global trade_enabled, monitor_enabled
    if m.chat.id != TELEGRAM_CHAT_ID:
        return

    if "Включить торговлю" in m.text:
        trade_enabled = True
        bot.send_message(TELEGRAM_CHAT_ID, "✅ Торговля включена", reply_markup=main_menu())
    elif "Выключить торговлю" in m.text:
        trade_enabled = False
        bot.send_message(TELEGRAM_CHAT_ID, "⛔️ Торговля выключена", reply_markup=main_menu())
    elif "Запустить мониторинг" in m.text:
        monitor_enabled = True
        bot.send_message(TELEGRAM_CHAT_ID, "🟢 Мониторинг запущен", reply_markup=main_menu())
    elif "Остановить мониторинг" in m.text:
        monitor_enabled = False
        bot.send_message(TELEGRAM_CHAT_ID, "🛑 Мониторинг остановлен", reply_markup=main_menu())
    elif "Балансы" in m.text:
        bbal = get_bybit_free_usdt()
        okbal = get_okx_free_usdt()
        msg = f"💰 <b>Балансы:</b>\nBybit: {bbal:.2f} USDT\nOKX: {okbal:.2f} USDT"
        bot.send_message(TELEGRAM_CHAT_ID, msg, reply_markup=main_menu())

# === МОНИТОРИНГ ============================================
def monitor_loop():
    log.info("🚀 Запуск мониторинга (Smart Spread Mode)")
    while True:
        try:
            if not monitor_enabled:
                time.sleep(2)
                continue

            profitable = []

            for symbol in WATCH_PAIRS:
                try:
                    ob_bybit = bybit.fetch_order_book(symbol, limit=5)
                    ob_okx = okx.fetch_order_book(symbol, limit=5)
                except Exception:
                    continue

                if not ob_bybit or not ob_okx:
                    continue

                bybit_bid = ob_bybit["bids"][0][0] if ob_bybit["bids"] else 0
                bybit_ask = ob_bybit["asks"][0][0] if ob_bybit["asks"] else 0
                okx_bid = ob_okx["bids"][0][0] if ob_okx["bids"] else 0
                okx_ask = ob_okx["asks"][0][0] if ob_okx["asks"] else 0

                scenarios = [
                    ("Bybit", "OKX", bybit_ask, okx_bid),
                    ("OKX", "Bybit", okx_ask, bybit_bid)
                ]

                for buy_ex, sell_ex, buy_price, sell_price in scenarios:
                    if not buy_price or not sell_price:
                        continue
                    spread = sell_price - buy_price
                    spread_pct = spread / buy_price if buy_price > 0 else 0

                    gross_profit = spread * TRADE_QTY
                    fee_cost = (buy_price * TRADE_QTY * TAKER_FEE) + (sell_price * TRADE_QTY * TAKER_FEE)
                    net_profit_after_fee = gross_profit - fee_cost

                    if DIAGNOSTIC_MODE:
                        log.info(f"[{symbol}] {buy_ex}->{sell_ex} Δ={spread:.4f} | Gross={gross_profit:.4f} | "
                                 f"Fees={fee_cost:.4f} | Net={net_profit_after_fee:.4f}")

                    # пропускаем убыточные сделки
                    if net_profit_after_fee <= 0:
                        continue

                    profitable.append((symbol, buy_ex, sell_ex, buy_price, sell_price, spread, spread_pct, net_profit_after_fee))

            # === Telegram уведомление ===
            if profitable:
                profitable.sort(key=lambda x: x[7], reverse=True)
                top = profitable[:10]

                text = "📈 <b>Топ арбитражных возможностей:</b>\n"
                for sym, b, s, bp, sp, spr, sprpct, net in top:
                    text += (
                        f"\n<b>{sym}</b>  {b} → {s}\n"
                        f"Buy @ {bp:.4f} / Sell @ {sp:.4f}\n"
                        f"Δ={spr:.4f} ({sprpct*100:.3f}%) | 💵 ≈ {net:.3f} USDT\n"
                    )

                text += f"\nТорговля: {'✅' if trade_enabled else '⛔️'}"
                bot.send_message(TELEGRAM_CHAT_ID, text)

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            log.warning(f"Monitor err: {e}")
            time.sleep(POLL_INTERVAL)

# === ЗАПУСК ================================================
def telegram_loop():
    log.info("Запуск Telegram-бота")
    try:
        bot.delete_webhook(drop_pending_updates=True)
    except:
        pass
    bot.infinity_polling(timeout=60, long_polling_timeout=60)

if __name__ == "__main__":
    t1 = threading.Thread(target=monitor_loop, daemon=True)
    t2 = threading.Thread(target=telegram_loop, daemon=True)
    t1.start(); t2.start()
    while True:
        time.sleep(1)
