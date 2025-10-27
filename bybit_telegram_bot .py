
import streamlit as st
import asyncio
import aiohttp
import pandas as pd
import time
import os

# ==========================================================
# 🔧 Настройки
# ==========================================================
REFRESH_INTERVAL = 10  # секунд
CSV_LOG_FILE = "arbitrage_log.csv"

EXCHANGES = {
    "Binance": "https://api.binance.com/api/v3/ticker/bookTicker?symbol={}",
    "Bybit": "https://api.bybit.com/v5/market/tickers?category=spot&symbol={}",
    "OKX": "https://www.okx.com/api/v5/market/ticker?instId={}-USDT",
    "KuCoin": "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={}",
    "Bitget": "https://api.bitget.com/api/v2/spot/market/ticker?symbol={}_SPBL"
}

# ==========================================================
# 📡 Асинхронное получение цен
# ==========================================================
async def fetch_price(session, name, url):
    try:
        async with session.get(url, timeout=5) as resp:
            data = await resp.json()
            if name == "Binance":
                return name, float(data["bidPrice"]), float(data["askPrice"])
            elif name == "Bybit":
                t = data["result"]["list"][0]
                return name, float(t["bid1Price"]), float(t["ask1Price"])
            elif name == "OKX":
                t = data["data"][0]
                return name, float(t["bidPx"]), float(t["askPx"])
            elif name == "KuCoin":
                t = data["data"]
                return name, float(t["bestBid"]), float(t["bestAsk"])
            elif name == "Bitget":
                t = data["data"][0]
                return name, float(t["buyOne"]), float(t["sellOne"])
    except Exception:
        return name, None, None

async def get_prices(symbol):
    tasks = []
    async with aiohttp.ClientSession() as session:
        for name, url in EXCHANGES.items():
            if name == "OKX":
                formatted = symbol.replace("USDT", "")
            elif name == "Bitget":
                formatted = symbol.replace("USDT", "USDT")
            else:
                formatted = symbol
            tasks.append(fetch_price(session, name, url.format(formatted)))
        results = await asyncio.gather(*tasks)
    return {name: (bid, ask) for name, bid, ask in results if bid and ask}

# ==========================================================
# 📊 Расчёт спредов
# ==========================================================
def calculate_spreads(prices, symbol):
    rows = []
    exchanges = list(prices.keys())
    for buy_ex in exchanges:
        for sell_ex in exchanges:
            if buy_ex == sell_ex:
                continue
            buy_price = prices[buy_ex][1]  # ask
            sell_price = prices[sell_ex][0]  # bid
            spread = (sell_price - buy_price) / buy_price * 100
            if spread > 0:
                rows.append({
                    "Монета": symbol,
                    "Купить на": buy_ex,
                    "Продать на": sell_ex,
                    "Buy цена": round(buy_price, 2),
                    "Sell цена": round(sell_price, 2),
                    "Спред %": round(spread, 3),
                    "Время": time.strftime('%H:%M:%S')
                })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Спред %", ascending=False)
    return df

# ==========================================================
# 🧾 Логирование в CSV
# ==========================================================
def log_to_csv(df):
    if df.empty:
        return
    file_exists = os.path.isfile(CSV_LOG_FILE)
    df.to_csv(CSV_LOG_FILE, mode='a', index=False, header=not file_exists, encoding='utf-8-sig')

# ==========================================================
# 🌐 Интерфейс Streamlit
# ==========================================================
st.set_page_config(page_title="Arbitrage Monitor", layout="wide")
st.title("💰 Real-Time Crypto Arbitrage Monitor")

# Мультиселект выбора монет
default_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
symbols = st.multiselect(
    "Выбери монеты для мониторинга:",
    options=default_symbols + ["BNBUSDT", "DOGEUSDT", "ADAUSDT", "TONUSDT"],
    default=default_symbols
)

st.write(f"⏱ Обновление каждые {REFRESH_INTERVAL} секунд.")
placeholder = st.empty()

while True:
    start = time.time()
    all_data = []
    for symbol in symbols:
        prices = asyncio.run(get_prices(symbol))
        df_spreads = calculate_spreads(prices, symbol)
        if not df_spreads.empty:
            all_data.append(df_spreads)

    st.markdown(f"**🕒 Последнее обновление:** {time.strftime('%H:%M:%S')}")

    if all_data:
        result = pd.concat(all_data)
        log_to_csv(result)

        # Цветовое выделение спредов
        def color_spread(val):
            if val >= 0.5:
                color = 'background-color: #00FF00; color: black'  # ярко-зелёный
            elif val >= 0.2:
                color = 'background-color: #90EE90; color: black'  # светло-зелёный
            else:
                color = ''
            return color

        styled_df = result.style.applymap(color_spread, subset=["Спред %"])
        st.dataframe(styled_df, use_container_width=True)
    else:
        st.info("Нет положительных спредов на данный момент.")

    time.sleep(REFRESH_INTERVAL)
