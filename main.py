import time
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
import pandas as pd
import yfinance as yf
import ta
import requests
import mplfinance as mpf
from flask import Flask
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext

# ==================== НАСТРОЙКИ ====================
TELEGRAM_TOKEN = '8996178345:AAElh6CIkk08qqP_90RyiVgxHjWrBiftKso'  # Токен от @BotFather

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
DB_FILE = 'bot_users.db'

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    """Инициализация базы данных пользователей"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_active INTEGER DEFAULT 1,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def add_user(user_id, username):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (user_id, username, is_active)
        VALUES (?, ?, 1)
    ''', (user_id, username))
    conn.commit()
    conn.close()

def set_user_status(user_id, is_active):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET is_active = ? WHERE user_id = ?', (is_active, user_id))
    conn.commit()
    conn.close()

def get_active_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE is_active = 1')
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users

# ==================== НОВОСТНОЙ ФИЛЬТР ====================
high_impact_news = []
last_news_fetch_time = None

def get_currency_by_country(country):
    mapping = {'US': 'USD', 'EU': 'EUR', 'GB': 'GBP', 'JP': 'JPY', 'AU': 'AUD', 'CA': 'CAD'}
    return mapping.get(country, '')

def fetch_economic_calendar():
    global high_impact_news, last_news_fetch_time
    if last_news_fetch_time and (datetime.now() - last_news_fetch_time).seconds < 7200:
        return

    url = "https://economic-calendar.tradingview.com/events"
    now_utc = datetime.now(timezone.utc)
    from_time = now_utc.replace(hour=0, minute=0, second=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    to_time = now_utc.replace(hour=23, minute=59, second=59).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Origin': 'https://www.tradingview.com',
        'Referer': 'https://www.tradingview.com/'
    }
    params = {'from': from_time, 'to': to_time, 'countries': 'US,EU,GB,JP,AU,CA'}
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=5)
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
            print(f"[{time.strftime('%H:%M:%S')}] Календарь новостей обновлен ({len(high_impact_news)} событий)")
    except Exception as e:
        print(f"⚠️ Предупреждение: Не удалось загрузить новости (работаем без фильтра): {e}")

def is_news_time(symbol):
    if not high_impact_news:
        return False

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
def get_forex_klines(symbol, period='2d', interval='5m'):
    """Получение котировок из yfinance"""
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        return df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
    except Exception as e:
        print(f"Ошибка загрузки котировок {symbol}: {e}")
        return None

def analyze_market(df):
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=1.8)
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low'] = bb.bollinger_lband()
    df['ema200'] = ta.trend.ema_indicator(df['close'], window=200)
    
    last = df.iloc[-2]
    rsi_val, close_price = last['rsi'], last['close']
    bb_h, bb_l = last['bb_high'], last['bb_low']
    
    if rsi_val < 40 and close_price <= bb_l:
        return "CALL (ВВЕРХ 🟢)", rsi_val, close_price
    elif rsi_val > 60 and close_price >= bb_h:
        return "PUT (ВНИЗ 🔴)", rsi_val, close_price
        
    return None, rsi_val, close_price

def generate_chart(df, symbol, signal_type):
    df_plot = df.iloc[-40:].copy()
    clean_symbol = symbol.replace('=X', '')
    
    addplots = [
        mpf.make_addplot(df_plot['bb_high'], color='gray', linestyle='--'),
        mpf.make_addplot(df_plot['bb_low'], color='gray', linestyle='--'),
        mpf.make_addplot(df_plot['ema200'], color='orange', width=1.5),
        mpf.make_addplot(df_plot['rsi'], panel=1, color='purple', ylabel='RSI (14)'),
    ]
    
    style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 9})
    file_path = f"chart_{clean_symbol}.png"
    
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

# ==================== МАССОВАЯ РАССЫЛКА ====================
def broadcast_signal(bot, symbol, signal_type, rsi, price, photo_path):
    active_users = get_active_users()
    if not active_users:
        print("Нет активных подписчиков для рассылки.")
        return

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
    
    count = 0
    for user_id in active_users:
        try:
            with open(photo_path, 'rb') as photo:
                bot.send_photo(chat_id=user_id, photo=photo, caption=caption, parse_mode='HTML')
            count += 1
            time.sleep(0.05)
        except Exception as e:
            if "bot was blocked" in str(e).lower():
                set_user_status(user_id, 0)
            print(f"Ошибка отправки пользователю {user_id}: {e}")
            
    print(f"[{time.strftime('%H:%M:%S')}] Сигнал {clean_symbol} отправлен {count}/{len(active_users)} пользователям")
    
    if os.path.exists(photo_path):
        os.remove(photo_path)

# ==================== КОМАНДЫ БОТА И МЕНЮ ====================
def start_command(update: Update, context: CallbackContext):
    user = update.effective_user
    add_user(user.id, user.username)
    
    keyboard = [
        [InlineKeyboardButton("🔔 Включить сигналы", callback_data='enable'),
         InlineKeyboardButton("🔕 Выключить сигналы", callback_data='disable')],
        [InlineKeyboardButton("📊 Статус подписки", callback_data='status')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        f"👋 <b>Добро пожаловать, {user.first_name}!</b>\n\n"
        f"Этот бот сканирует рынок Forex (EURUSD, GBPUSD и др.) на таймфрейме 5 минут "
        f"и присылает сигналы для бинарных опциональных сделок (экспирация 5–10 мин).\n\n"
        f"Все сигналы подкрепляются анализом <b>RSI</b>, <b>Bollinger Bands</b>, <b>EMA 200</b> "
        f"и встроенным <b>новостным фильтром</b>.\n\n"
        f"Используйте кнопки ниже для управления подпиской:"
    )
    update.message.reply_text(welcome_text, parse_mode='HTML', reply_markup=reply_markup)

def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    
    if query.data == 'enable':
        set_user_status(user_id, 1)
        query.edit_message_text("✅ <b>Вы успешно подписались на сигналы!</b> Ожидайте уведомления.", parse_mode='HTML')
    elif query.data == 'disable':
        set_user_status(user_id, 0)
        query.edit_message_text("🔕 <b>Уведомления отключены.</b> Вы можете включить их снова в любое время через /start.", parse_mode='HTML')
    elif query.data == 'status':
        active_users = get_active_users()
        status_str = "АКТИВНА 🟢" if user_id in active_users else "ОТКЛЮЧЕНА 🔴"
        query.edit_message_text(f"📋 <b>Ваш статус:</b> Подписка {status_str}", parse_mode='HTML')

# ==================== ОСНОВНОЙ ЦИКЛ СКАНЕРА ====================
def market_scanner_loop(bot: Bot):
    last_signals = {symbol: None for symbol in SYMBOLS}
    
    while True:
        fetch_economic_calendar()
        
        for symbol in SYMBOLS:
            try:
                df = get_forex_klines(symbol, period='2d', interval=TIMEFRAME)
                if df is None or len(df) < 200:
                    continue
                
                signal, rsi, price = analyze_market(df)
                candle_time = df.index[-2]
                
                if signal and last_signals[symbol] != candle_time:
                    if not is_news_time(symbol):
                        photo_path = generate_chart(df, symbol, signal)
                        broadcast_signal(bot, symbol, signal, rsi, price, photo_path)
                        last_signals[symbol] = candle_time
                    
            except Exception as e:
                print(f"Ошибка анализа {symbol}: {e}")
                
        time.sleep(CHECK_INTERVAL)

# ==================== ФИКТИВНЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER ====================
app = Flask('')

@app.route('/')
def home():
    return "Бот сканера рынка активен и работает 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# ==================== ТОЧКА ВХОДА ====================
if __name__ == '__main__':
    # 1. Инициализация базы данных
    init_db()
    
    # 2. Запуск веб-сервера в фоновом потоке
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    # 3. Запуск Telegram-бота
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CallbackQueryHandler(button_handler))

    updater.start_polling()
    print("Бот и веб-сервер успешно запущены!")

    # 4. Запуск бесконечного цикла сканера рынка
    market_scanner_loop(updater.bot)