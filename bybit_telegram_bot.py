# ============================================================
#   Arbitrage Bot v2.6 (Bybit ↔ OKX)
#   💰 Автоарбитраж: открытие+закрытие + умный приоритет пар
#   🧠 Pair Intelligence + 🏆 Топ-5 по ликвидности (24h swap)
#   ⚙️ Telegram управление, DRY_RUN, Diagnostic Mode
# ============================================================
import streamlit as st
st.title("🤖 Bybit Telegram Bot запущен успешно!")
import os, time, threading, logging, random
import ccxt, telebot
from telebot import types

# === НАСТРОЙКИ =============================================
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY", "bI2fcFjKVNY4W6oQs9")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "QYawByd3Gz8BUXWebZpDMYircORbWY7zD2cV")

OKX_API_KEY      = os.getenv("OKX_API_KEY", "6fbfe3bc-3a0a-42b9-985a-d681ec369c78")
OKX_API_SECRET   = os.getenv("OKX_API_SECRET", "1930C87E73EBC8D00B55F962FBCD9D93")
OKX_PASSWORD     = os.getenv("OKX_PASSWORD", "Movhafvx7.")

TELEGRAM_TOKEN   = os.getenv("TG_TOKEN", "7400072123:AAH098YiSanx1R_0MZB9mk6qr2RuxhJvx_k")
TELEGRAM_CHAT_ID = int(os.getenv("TG_CHAT_ID", 537054215))

POLL_INTERVAL = 3.0
TRADE_QTY = 0.01
DRY_RUN = False
DIAGNOSTIC_MODE = True
TAKER_FEE = 0.0006

WATCH_PAIRS = [
    "SUI/USDT", "INJ/USDT", "CFX/USDT", "RNDR/USDT", "OP/USDT",
    "APT/USDT", "SEI/USDT", "IMX/USDT", "AR/USDT",  "PEPE/USDT",
    "GALA/USDT","TON/USDT", "NEAR/USDT","FIL/USDT", "ATOM/USDT"
]

# === ФИЛЬТР ПЕРСПЕКТИВНЫХ ПАР ===============================
PERSISTENT_THRESHOLD = 5      # 5 прибыльных циклов подряд
PERSISTENT_TIMEOUT   = 900    # 15 минут, затем сброс статистики
pair_stats = {}               # {"PAIR": {"hits": int, "last_seen": ts}}

# === ЛОГИ ====================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("arb_bot")

# === БИРЖИ ==================================================
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
bybit.load_markets(); okx.load_markets()

# === TELEGRAM ===============================================
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
trade_enabled = False
monitor_enabled = True
open_trades = []

# === УТИЛИТЫ: поиск своп-символов и объёмов =================
def _find_swap_market(exchange: ccxt.Exchange, base: str, quote: str = "USDT"):
    """
    Находит нужный рынок типа 'swap' по base/quote с учётом особенностей биржи.
    Возвращает dict market или None.
    """
    for mkt in exchange.markets.values():
        try:
            if mkt.get("type") != "swap":
                continue
            if mkt.get("base") == base and str(mkt.get("quote")).startswith(quote):
                return mkt
        except Exception:
            continue
    return None

def _fetch_24h_quote_volume(exchange: ccxt.Exchange, market) -> float:
    """
    Возвращает 24h объём в котируемой валюте (USDT), если есть.
    ccxt для деривативов может заполнять baseVolume/quoteVolume не всегда —
    поэтому аккуратно проверяем поля.
    """
    try:
        t = exchange.fetch_ticker(market["symbol"])
        # Предпочитаем quoteVolume (в USDT). Если нет — оцениваем через baseVolume*last.
        qv = t.get("quoteVolume")
        if qv is not None:
            return float(qv)
        bv = t.get("baseVolume")
        last = t.get("last") or t.get("close")
        if bv is not None and last:
            return float(bv) * float(last)
    except Exception as e:
        log.warning(f"Volume fetch error {exchange.id} {market.get('symbol')}: {e}")
    return 0.0

def _split_pair(pair: str):
    base, quote = pair.split("/")
    return base, quote

# === БАЛАНСЫ ================================================
def get_bybit_free_usdt() -> float:
    try:
        resp = bybit.private_get_v5_account_wallet_balance({"accountType": "UNIFIED"})
        coins = resp.get("result", {}).get("list", [])[0].get("coin", [])
        for c in coins:
            if c.get("coin") == "USDT":
                return float(c.get("availableToWithdraw") or c.get("walletBalance") or c.get("equity") or 0)
    except Exception as e:
        log.warning(f"Bybit balance fetch error: {e}")
    return 0.0

def get_okx_free_usdt() -> float:
    try:
        balances = okx.fetch_balance()
        return float(balances.get("USDT", {}).get("free", 0))
    except Exception as e:
        log.warning(f"OKX balance fetch error: {e}")
    return 0.0

# === ТОРГОВЫЕ ФУНКЦИИ =======================================
def place_order(exchange, side, symbol, qty):
    if DRY_RUN:
        log.info(f"[DRY] {exchange.id} {side} {qty} {symbol}")
        return {"id": "dry-run"}
    try:
        order = exchange.create_order(symbol, "market", side, qty)
        log.info(f"✅ {exchange.id} {side.upper()} {qty} {symbol} @ market")
        return order
    except Exception as e:
        log.error(f"Order error {exchange.id}: {e}")
        return None

def hedge_open(symbol, buy_ex, sell_ex, buy_price, sell_price, net_profit):
    if not trade_enabled: return
    bbal = get_bybit_free_usdt(); okbal = get_okx_free_usdt()
    min_bal = TRADE_QTY * buy_price * 2
    if bbal < min_bal or okbal < min_bal:
        log.warning("Недостаточно баланса для арбитража")
        return

    msg = (f"🚀 <b>HEDGE OPEN</b>\n{symbol}\n"
           f"{buy_ex.id} BUY @ {buy_price:.4f}\n"
           f"{sell_ex.id} SELL @ {sell_price:.4f}\n"
           f"Net ≈ {net_profit:.3f} USDT")
    bot.send_message(TELEGRAM_CHAT_ID, msg)

    def buy_leg():  place_order(buy_ex, "buy", symbol, TRADE_QTY)
    def sell_leg(): place_order(sell_ex, "sell", symbol, TRADE_QTY)
    tb, ts = threading.Thread(target=buy_leg), threading.Thread(target=sell_leg)
    tb.start(); ts.start(); tb.join(); ts.join()

    open_trades.append({
        "symbol": symbol,
        "buy_ex": buy_ex,
        "sell_ex": sell_ex,
        "entry_spread": sell_price - buy_price
    })

def hedge_close(trade, current_spread):
    symbol, buy_ex, sell_ex = trade["symbol"], trade["buy_ex"], trade["sell_ex"]
    msg = (f"💠 <b>HEDGE CLOSE</b>\n{symbol}\n"
           f"{buy_ex.id} SELL (закрытие лонга)\n"
           f"{sell_ex.id} BUY (закрытие шорта)\n"
           f"Δ {trade['entry_spread']:.4f} → {current_spread:.4f}")
    bot.send_message(TELEGRAM_CHAT_ID, msg)

    def close_buy():  place_order(buy_ex, "sell", symbol, TRADE_QTY)
    def close_sell(): place_order(sell_ex, "buy", symbol, TRADE_QTY)
    tb, ts = threading.Thread(target=close_buy), threading.Thread(target=close_sell)
    tb.start(); ts.start(); tb.join(); ts.join()

# === TELEGRAM МЕНЮ ==========================================
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("💼 Балансы"))
    kb.add(types.KeyboardButton("📈 Перспективные пары"))
    kb.add(types.KeyboardButton("🏆 Топ-5 по ликвидности"))
    kb.add(types.KeyboardButton("🟢 Запустить мониторинг") if not monitor_enabled else types.KeyboardButton("🛑 Остановить мониторинг"))
    kb.add(types.KeyboardButton("▶️ Включить торговлю") if not trade_enabled else types.KeyboardButton("⏸ Выключить торговлю"))
    return kb

@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(TELEGRAM_CHAT_ID, "🤖 Арбитражный бот запущен.", reply_markup=main_menu())

@bot.message_handler(func=lambda m: True)
def handle_buttons(m):
    global trade_enabled, monitor_enabled
    if m.chat.id != TELEGRAM_CHAT_ID: return

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
        bbal = get_bybit_free_usdt(); okbal = get_okx_free_usdt()
        bot.send_message(TELEGRAM_CHAT_ID,
                         f"💰 <b>Балансы:</b>\nBybit {bbal:.2f} USDT\nOKX {okbal:.2f} USDT",
                         reply_markup=main_menu())
    elif "Перспективные пары" in m.text:
        if not pair_stats:
            bot.send_message(TELEGRAM_CHAT_ID, "Нет данных по парам.")
        else:
            sorted_pairs = sorted(pair_stats.items(), key=lambda x: x[1]["hits"], reverse=True)
            msg = "📈 <b>Перспективные пары:</b>\n"
            for s, data in sorted_pairs[:10]:
                msg += f"{s}: {data['hits']} прибыльных циклов\n"
            bot.send_message(TELEGRAM_CHAT_ID, msg, reply_markup=main_menu())
    elif "Топ-5 по ликвидности" in m.text:
        msg = build_top5_liquidity()
        bot.send_message(TELEGRAM_CHAT_ID, msg, reply_markup=main_menu())

# === ТОП-5 ПО ЛИКВИДНОСТИ (24h swap обеих бирж) ============
def build_top5_liquidity() -> str:
    rows = []
    for pair in WATCH_PAIRS:
        base, quote = _split_pair(pair)
        m_byb = _find_swap_market(bybit, base, quote)
        m_okx = _find_swap_market(okx,  base, quote)

        if not m_byb or not m_okx:
            continue

        v_byb = _fetch_24h_quote_volume(bybit, m_byb)  # в USDT
        v_okx = _fetch_24h_quote_volume(okx,  m_okx)   # в USDT
        v_pair = min(v_byb, v_okx)                     # «бутылочное горлышко»

        rows.append({
            "pair": pair,
            "bybit_vol_usdt": v_byb,
            "okx_vol_usdt": v_okx,
            "effective_usdt": v_pair
        })

    if not rows:
        return "Не удалось собрать объёмы (проверь доступ к API и пары)."

    rows.sort(key=lambda r: r["effective_usdt"], reverse=True)
    top = rows[:5]

    # Формируем красивый текст
    out = ["🏆 <b>ТОП-5 по ликвидности (24h, Perp/Swap)</b>"]
    for i, r in enumerate(top, 1):
        out.append(
            f"{i}. <b>{r['pair']}</b>\n"
            f"   Bybit: {r['bybit_vol_usdt']:,.0f} USDT\n"
            f"   OKX:   {r['okx_vol_usdt']:,.0f} USDT\n"
            f"   🔎 Эффективная (min): {r['effective_usdt']:,.0f} USDT"
        )
    return "\n".join(out)

# === МОНИТОРИНГ ============================================
def monitor_loop():
    log.info("🚀 Запуск мониторинга (Smart Pair Filter Mode)")
    while True:
        try:
            if not monitor_enabled:
                time.sleep(2); continue

            current_time = time.time()
            # чистим устаревшую статистику
            for s in list(pair_stats.keys()):
                if current_time - pair_stats[s]["last_seen"] > PERSISTENT_TIMEOUT:
                    del pair_stats[s]

            for symbol in WATCH_PAIRS:
                # менее перспективные пары — реже сканируем (1 из 3 проходов)
                if symbol not in pair_stats or pair_stats[symbol]["hits"] < PERSISTENT_THRESHOLD:
                    if random.random() > 0.33:
                        continue

                try:
                    ob_bybit = bybit.fetch_order_book(symbol, 5)
                    ob_okx = okx.fetch_order_book(symbol, 5)
                except Exception:
                    continue
                if not ob_bybit or not ob_okx:
                    continue

                bybit_bid = ob_bybit["bids"][0][0] if ob_bybit["bids"] else 0
                bybit_ask = ob_bybit["asks"][0][0] if ob_bybit["asks"] else 0
                okx_bid   = ob_okx["bids"][0][0]   if ob_okx["bids"]   else 0
                okx_ask   = ob_okx["asks"][0][0]   if ob_okx["asks"]   else 0

                scenarios = [
                    ("Bybit", "OKX", bybit_ask, okx_bid),
                    ("OKX", "Bybit", okx_ask, bybit_bid)
                ]

                for buy_ex_name, sell_ex_name, buy_price, sell_price in scenarios:
                    if not buy_price or not sell_price:
                        continue
                    buy_ex  = bybit if buy_ex_name == "Bybit" else okx
                    sell_ex = okx   if sell_ex_name == "OKX"   else bybit

                    spread = sell_price - buy_price
                    if buy_price <= 0:
                        continue

                    gross = spread * TRADE_QTY
                    fee   = (buy_price + sell_price) * TRADE_QTY * TAKER_FEE
                    net   = gross - fee

                    if DIAGNOSTIC_MODE:
                        log.info(f"[{symbol}] {buy_ex_name}->{sell_ex_name} Δ={spread:.4f} | Net={net:.4f}")

                    # статистика «перспективности»
                    if net > 0:
                        pair_stats.setdefault(symbol, {"hits": 0, "last_seen": 0})
                        pair_stats[symbol]["hits"] += 1
                        pair_stats[symbol]["last_seen"] = time.time()

                    # --- Открытие сделки ---
                    if net > 0.001 and trade_enabled:
                        exists = any(t["symbol"] == symbol for t in open_trades)
                        if not exists:
                            hedge_open(symbol, buy_ex, sell_ex, buy_price, sell_price, net)

                    # --- Закрытие сделки ---
                    for t in open_trades[:]:
                        if t["symbol"] == symbol:
                            if spread < t["entry_spread"] * 0.2 or spread <= 0:
                                hedge_close(t, spread)
                                open_trades.remove(t)

            time.sleep(POLL_INTERVAL)
        except Exception as e:
            log.warning(f"Monitor err: {e}")
            time.sleep(POLL_INTERVAL)

# === ЗАПУСК ================================================
def telegram_loop():
    try: bot.delete_webhook(drop_pending_updates=True)
    except: pass
    bot.infinity_polling(timeout=60, long_polling_timeout=60)

if __name__ == "__main__":
    t1 = threading.Thread(target=monitor_loop, daemon=True)
    t2 = threading.Thread(target=telegram_loop, daemon=True)
    t1.start(); t2.start()
    while True: time.sleep(1)
