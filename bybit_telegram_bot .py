
# ============================================
#  arb_bot_telegram.py (v1.1 уведомления фикс)
# ============================================

import os, time, math, json, threading, logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
import ccxt, telebot
from telebot import types
from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv("pIR80OjUofQCBMPyJAngEg695l4vQPNDHobxgbWugyIeYs8qzVaqXoXZ6O3pKpwJ", "")
BINANCE_API_SECRET = os.getenv("2KFKdM4zYjoIUJ6NUbghSWFkAZwrN9WutZUskMUxYTWFqaCOLYRXA4iQo6Vbxj3f", "")
BYBIT_API_KEY = os.getenv("bI2fcFjKVNY4W6oQs9", "")
BYBIT_API_SECRET = os.getenv("QYawByd3Gz8BUXWebZpDMYircORbWY7zD2cV", "")
OKX_API_KEY = os.getenv("6fbfe3bc-3a0a-42b9-985a-d681ec369c78", "")
OKX_API_SECRET = os.getenv("1930C87E73EBC8D00B55F962FBCD9D93", "")
OKX_PASSWORD = os.getenv("Movhafvx7.", "")

TELEGRAM_TOKEN = "7400072123:AAH098YiSanx1R_0MZB9mk6qr2RuxhJvx_k"
TELEGRAM_CHAT_ID = 537054215

DRY_RUN = True
POLL_INTERVAL = 1.5
MIN_PROFIT_USD = 0.01
MIN_SPREAD_PCT = 0.0001
MAX_NOTIONAL_PER_TRADE_USD = 50.0

WATCH_SYMBOLS = ["BTC/USDT", "ETH/USDT"]
STATE_FILE = "arb_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("arb_bot")

state_lock = threading.Lock()
trade_enabled = False
last_alerts = {}
state = {"trades": [], "last_opportunity": None}

@dataclass
class Opportunity:
    symbol: str
    buy_ex: str
    sell_ex: str
    ask: float
    bid: float
    qty: float
    notional: float
    net_profit: float
    spread_pct: float

def to_float(x):
    try:
        return float(x)
    except: return 0.0

def round_step(qty, step):
    if step <= 0: return qty
    return math.floor(qty / step) * step

class ExchangeClient:
    def __init__(self, name, api_key, secret, password=None):
        self.name = name.lower()
        if self.name == "binance":
            self.ex = ccxt.binance({"apiKey": api_key, "secret": secret, "enableRateLimit": True})
        elif self.name == "bybit":
            self.ex = ccxt.bybit({"apiKey": api_key, "secret": secret, "enableRateLimit": True})
        elif self.name == "okx":
            self.ex = ccxt.okx({"apiKey": api_key, "secret": secret, "password": password, "enableRateLimit": True})
        else:
            raise ValueError(f"Неизвестная биржа: {name}")
        logger.info(f"[{self.name}] загрузка маркетов...")
        self.ex.load_markets()
        logger.info(f"[{self.name}] загружено {len(self.ex.markets)} инструментов")
    def fetch_order_book(self, symbol, limit=5):
        try: return self.ex.fetch_order_book(symbol, limit=limit)
        except Exception as e:
            logger.debug(f"{self.name}: ошибка стакана {symbol}: {e}"); return {}
    def fees(self):
        f = getattr(self.ex, "fees", {}).get("trading", {})
        return float(f.get("taker", 0.001)), float(f.get("maker", 0.001))

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if trade_enabled: kb.add(types.KeyboardButton("⏸ Выключить торговлю"))
    else: kb.add(types.KeyboardButton("▶️ Включить торговлю"))
    kb.add(types.KeyboardButton("📊 Статус"), types.KeyboardButton("🧾 Последние сделки"))
    return kb

@bot.message_handler(commands=['start'])
def cmd_start(m):
    if m.chat.id != TELEGRAM_CHAT_ID: return
    bot.send_message(TELEGRAM_CHAT_ID, f"🤖 Привет! Мониторинг активен.\nТорговля: {'включена ✅' if trade_enabled else 'выключена ⛔️'}", reply_markup=main_menu())

@bot.message_handler(func=lambda m: True)
def handle(m):
    global trade_enabled
    if m.chat.id != TELEGRAM_CHAT_ID: return
    if m.text.startswith("▶️"): trade_enabled=True; bot.send_message(TELEGRAM_CHAT_ID,"✅ Торговля включена",reply_markup=main_menu())
    elif m.text.startswith("⏸"): trade_enabled=False; bot.send_message(TELEGRAM_CHAT_ID,"⛔️ Торговля выключена",reply_markup=main_menu())
    elif m.text.startswith("📊"): bot.send_message(TELEGRAM_CHAT_ID,f"⚙️ Торговля: {'включена ✅' if trade_enabled else 'выключена ⛔️'}",reply_markup=main_menu())
    elif m.text.startswith("🧾"): bot.send_message(TELEGRAM_CHAT_ID,"Сделок пока нет (режим наблюдения)",reply_markup=main_menu())

def monitor_loop():
    logger.info("Запуск мониторинга (диагностика)")
    binance=ExchangeClient("binance",BINANCE_API_KEY,BINANCE_API_SECRET)
    bybit=ExchangeClient("bybit",BYBIT_API_KEY,BYBIT_API_SECRET)
    okx=ExchangeClient("okx",OKX_API_KEY,OKX_API_SECRET,OKX_PASSWORD)
    exchanges={"binance":binance,"bybit":bybit,"okx":okx}
    while True:
        try:
            bids={}
            for name,ex in exchanges.items():
                ob=ex.fetch_order_book("BTC/USDT")
                if ob and ob.get("bids"): bids[name]=ob["bids"][0][0]
            if bids:
                min_ex=min(bids,key=bids.get); max_ex=max(bids,key=bids.get)
                spread=bids[max_ex]-bids[min_ex]
                if spread>0.0:
                    key=f"BTC/USDT:{min_ex}->{max_ex}"
                    prev_net=last_alerts.get(key,0)
                    if abs(spread-prev_net)>0.001:  # сниженный порог чувствительности
                        last_alerts[key]=spread
                        msg=(f"💰 <b>Арбитражная возможность</b>\nBTC/USDT\n"
                             f"Купить на <b>{min_ex}</b> @ {bids[min_ex]:.2f}\n"
                             f"Продать на <b>{max_ex}</b> @ {bids[max_ex]:.2f}\n"
                             f"Потенц. прибыль: <b>+{spread:.2f} USDT</b>\n"
                             f"Торговля: {'включена ✅' if trade_enabled else 'выключена ⛔️'}")
                        bot.send_message(TELEGRAM_CHAT_ID,msg)
                logger.info(f"📈 BTC/USDT → {', '.join([f'{k}:{v:.2f}' for k,v in bids.items()])} | Спред={spread:.2f} USDT ({min_ex}->{max_ex})")
            else:
                logger.warning("Не удалось получить цены ни с одной биржи")
        except Exception as e:
            logger.warning(f"Ошибка мониторинга: {e}")
        time.sleep(POLL_INTERVAL)

def telegram_loop():
    logger.info("Запуск Telegram-интерфейса")
    bot.infinity_polling(timeout=60,long_polling_timeout=60)

if __name__=="__main__":
    t1=threading.Thread(target=monitor_loop,daemon=True)
    t2=threading.Thread(target=telegram_loop,daemon=True)
    t1.start(); t2.start()
    while True
