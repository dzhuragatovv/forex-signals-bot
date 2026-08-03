import time
import os
from datetime import datetime, timedelta, timezone
import pandas as pd
import yfinance as yf
import ta
import requests
import mplfinance as mpf
import matplotlib.pyplot as plt
from telegram import Bot

# ==================== НАСТРОЙКИ ====================
TELEGRAM_TOKEN = '8996178345:AAElh6CIkk08qqP_90RyiVgxHjWrBiftKso'
CHAT_ID = '8769646368'  # Ваш личный Telegram ID от @userinfobot

SYMBOLS = [
    'EURUSD=X',
    'GBPUSD=X',
    'USDJPY=X',
    'AUDUSD=X',
    'USDCAD=X',
    'EURGBP=X'
]

TIMEFRAME = '5m'
CHECK_INTERVAL = 60
NEWS_BUFFER_MINUTES = 15

bot = Bot(token=TELEGRAM_TOKEN)

# Переменные календаря новостей
high_impact_news = []
last_news_fetch_time = None

# ==================== НОВОСТНОЙ ФИЛЬТР ====================
def fetch_economic_calendar():
    global high_impact_news, last_news_fetch_time
    if last_news_fetch_time and (datetime.now() - last_news_fetch_time).seconds < 7200:
        return

    url = "https://economic-calendar.tradingview.com/events"
    now_utc = datetime.now(timezone.utc)
    from_time = now_utc.replace(hour=0, minute=0, second=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    to_time = now_utc.replace(hour=23, minute=59, second=59).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    params = {'from': from_time, 'to': to_time, 'countries': 'US,EU,GB,JP,AU,CA'}
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            events = response.json().get('result', [])
            news_list = []
            for event in events:
                if event.get('importance') == 1:
                    event_time = datetime.fromisoformat(event['date'].replace('Z', '+00:00'))
                    news_list.append({
                        'currency': get_currency_by_country(event.get('country')),
                        'title': event.get('title'),
                        'time': event_time
                    })
            high_impact_news = news_list
            last_news_fetch_time = datetime.now()
            print(f"[{time.strftime('%H:%M:%S')}] Обновлен календарь новостей ({len(high_impact_news)} событий)")
    except Exception as e:
        print(f"Ошибка получения новостей: {e}")

def get_currency_by_country(country):
    mapping = {'US': 'USD', 'EU': 'EUR', 'GB': 'GBP', 'JP': 'JPY', 'AU': 'AUD', 'CA': 'CAD'}
    return mapping.get(country, '')

def is_news_time(symbol):
    clean_symbol = symbol.replace('=X', '')
    base_curr, quote_curr = clean_symbol[:3], clean_symbol[3:]
    now_utc = datetime.now(timezone.utc)
    buffer = timedelta(minutes=NEWS_BUFFER_MINUTES)
    
    for news in high_impact_news:
        if news['currency'] in (base_curr, quote_curr):
            if (news['time'] - buffer) <= now_utc <= (news['time'] + buffer):
                print(f"⚠️ Сигнал по {clean_symbol} пропущен из-за новости: {news['title']}")
                return True
    return False

# ==================== ТЕХНИЧЕСКИЙ АНАЛИЗ И ГРАФИКИ ====================
def get_forex_klines(symbol, period='1d', interval='5m'):
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval)
    if df.empty:
        return None
    return df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})

def analyze_market(df):
    # 1. RSI (14)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    
    # 2. Bollinger Bands (уменьшаем отклонение с 2.0 до 1.8 для более частых касаний)
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=1.8)
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low'] = bb.bollinger_lband()
    
    # 3. EMA 200
    df['ema200'] = ta.trend.ema_indicator(df['close'], window=200)
    
    # Берем сформировавшуюся свечу (-2)
    last = df.iloc[-2]
    rsi_val, close_price = last['rsi'], last['close']
    bb_h, bb_l, ema200_val = last['bb_high'], last['bb_low'], last['ema200']
    
    # --- Ослабленные условия ---
    # CALL (ВВЕРХ): RSI < 40 (вместо 35) и касание более узкой нижней полосы
    if rsi_val < 40 and close_price <= bb_l:
        return "CALL (ВВЕРХ 🟢)", rsi_val, close_price
        
    # PUT (ВНИЗ): RSI > 60 (вместо 65) и касание более узкой верхней полосы
    elif rsi_val > 60 and close_price >= bb_h:
        return "PUT (ВНИЗ 🔴)", rsi_val, close_price
        
    return None, rsi_val, close_price

def generate_chart(df, symbol, signal_type):
    """Генерирует свечной график с индикаторами и стрелкой входа"""
    # Берем последние 40 свечей для наглядного отображения
    df_plot = df.iloc[-40:].copy()
    clean_symbol = symbol.replace('=X', '')
    
    # Дополнительные линии индикаторов для графика
    addplots = [
        mpf.make_addplot(df_plot['bb_high'], color='gray', linestyle='--'),
        mpf.make_addplot(df_plot['bb_low'], color='gray', linestyle='--'),
        mpf.make_addplot(df_plot['ema200'], color='orange', width=1.5),
        mpf.make_addplot(df_plot['rsi'], panel=1, color='purple', ylabel='RSI (14)'),
    ]
    
    # Стиль оформления графика (темная тема)
    style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 9})
    
    file_path = f"chart_{clean_symbol}.png"
    
    # Создаем график и сохраняем его в PNG
    mpf.plot(
        df_plot,
        type='candle',
        addplot=addplots,
        volume=False,
        style=style,
        title=f"\n{clean_symbol} | 5m | {signal_type}",
        savefig=file_path,
        panel_ratios=(3, 1),
        figratio=(12, 7)
    )
    return file_path

def send_signal_with_photo(symbol, signal_type, rsi, price, photo_path):
    """Отправляет сигнал с прикрепленным изображением графика"""
    clean_symbol = symbol.replace('=X', '')
    caption = (
        f"🚨 <b>СИГНАЛ FOREX (БИНАРНЫЕ ОПЦИОНЫ)</b> 🚨\n\n"
        f"<b>Валютная пара:</b> {clean_symbol}\n"
        f"<b>Направление:</b> {signal_type}\n"
        f"<b>Экспирация:</b> 5 - 10 минут\n"
        f"<b>Текущая цена:</b> {price:.5f}\n"
        f"<b>RSI:</b> {rsi:.2f}\n\n"
        f"🛡️ <i>Новостной фильтр: Чисто</i>"
    )
    try:
        with open(photo_path, 'rb') as photo:
            bot.send_photo(
                chat_id=CHAT_ID,
                photo=photo,
                caption=caption,
                parse_mode='HTML'
            )
        print(f"[{time.strftime('%H:%M:%S')}] Сигнал со скриншотом отправлен: {clean_symbol}")
    except Exception as e:
        print(f"Ошибка отправки фото: {e}")
    finally:
        # Удаляем локальный файл скриншота после отправки
        if os.path.exists(photo_path):
            os.remove(photo_path)

# ==================== ОСНОВНОЙ ЦИКЛ ====================
def main():
    print("Бот сканера рынка со скриншотами запущен...")
    
    try:
        bot.send_message(chat_id=CHAT_ID, text="🤖 Бот успешно запущен! Сканирование с генерацией графиков включено.")
    except Exception as e:
        print(f"Ошибка стартовой отправки: {e}")

    last_signals = {symbol: None for symbol in SYMBOLS}
    
    while True:
        fetch_economic_calendar()
        
        for symbol in SYMBOLS:
            try:
                df = get_forex_klines(symbol, period='1d', interval=TIMEFRAME)
                if df is None or len(df) < 200:
                    continue
                
                signal, rsi, price = analyze_market(df)
                candle_time = df.index[-2]
                
                if signal and last_signals[symbol] != candle_time:
                    if not is_news_time(symbol):
                        # Генерируем скриншот графика и отправляем в Telegram
                        photo_path = generate_chart(df, symbol, signal)
                        send_signal_with_photo(symbol, signal, rsi, price, photo_path)
                        last_signals[symbol] = candle_time
                    
            except Exception as e:
                print(f"Ошибка при обработке {symbol}: {e}")
                
        time.sleep(CHECK_INTERVAL)

import threading
from flask import Flask

# Создаем минимальный веб-сервер для "обмана" Render
app = Flask('')

@app.route('/')
def home():
    return "Бот сканера рынка активен и работает 24/7!"

def run_flask():
    # Render передает порт через переменную PORT, по умолчанию 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    init_db()
    
    # 1. Запускаем фиктивный веб-сервер в отдельном фоновом потоке (threading)
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    # 2. Запускаем нашего Telegram-бота и сканер рынка
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CallbackQueryHandler(button_handler))

    updater.start_polling()
    print("Бот и веб-сервер заглушка успешно запущены!")

    # Запуск сканера рынка
    market_scanner_loop(updater.bot)