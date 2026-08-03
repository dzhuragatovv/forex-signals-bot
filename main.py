import time
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
import yfinance as yf
import requests
import mplfinance as mpf
from flask import Flask
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext

# ==================== НАСТРОЙКИ ====================
TELEGRAM_TOKEN = '8996178345:AAElh6CIkk08qqP_90RyiVgxHjWrBiftKso'  # Укажите ваш токен от @BotFather

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
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
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
        print(f"⚠️ Предупреждение: Не удалось загрузить новости: {e}")

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

# ==================== РАСЧЕТ VOLUME PROFILE & POC ====================
def calculate_volume_profile(df, num_bins=30):
    """Расчет уровня максимального объема POC и Value Area (VAH, VAL 70%)"""
    min_price = df['low'].min()
    max_price = df['high'].max()
    
    if max_price == min_price:
        return None, None, None

    bins = np.linspace(min_price, max_price, num_bins)
    profile = np.zeros(len(bins) - 1)

    for _, row in df.iterrows():
        # Распределяем объем свечи по бинам в пределах ее диапазона [low, high]
        c_low, c_high, vol = row['low'], row['high'], row['volume']
        if c_high == c_low:
            continue
        idx = np.where((bins[:-1] >= c_low) & (bins[1:] <= c_high))[0]
        if len(idx) > 0:
            profile[idx] += vol / len(idx)
        else:
            mid = (c_low + c_high) / 2
            closest_bin = np.digitize(mid, bins) - 1
            closest_bin = max(0, min(closest_bin, len(profile) - 1))
            profile[closest_bin] += vol

    # Поиск POC (Point of Control)
    poc_idx = np.argmax(profile)
    poc_price = (bins[poc_idx] + bins[poc_idx + 1]) / 2

    # Поиск Value Area (70% объема)
    total_vol = np.sum(profile)
    target_vol = total_vol * 0.70
    
    sorted_indices = np.argsort(profile)[::-1]
    accum_vol = 0
    va_indices = []
    
    for idx in sorted_indices:
        accum_vol += profile[idx]
        va_indices.append(idx)
        if accum_vol >= target_vol:
            break
            
    val_price = (bins[min(va_indices)] + bins[min(va_indices) + 1]) / 2
    vah_price = (bins[max(va_indices)] + bins[max(va_indices) + 1]) / 2

    return poc_price, vah_price, val_price

# ==================== VSA И PRICE ACTION АНАЛИЗ ====================
def get_forex_klines(symbol, period='2d', interval='5m'):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
        
        # Если тиковый объем нулевой (бывает на Forex в yfinance), генерируем спредовый объем
        if df['volume'].sum() == 0:
            df['volume'] = (df['high'] - df['low']) * 100000

        return df
    except Exception as e:
        print(f"Ошибка загрузки котировок {symbol}: {e}")
        return None

def analyze_vsa(df):
    """VSA анализ свечей и профиля объемов"""
    df = df.copy()
    
    # Расчет метрик VSA
    df['spread'] = df['high'] - df['low']
    df['sma_spread'] = df['spread'].rolling(window=20).mean()
    df['sma_vol'] = df['volume'].rolling(window=20).mean()
    
    df['close_ratio'] = (df['close'] - df['low']) / df['spread'].replace(0, np.nan)
    df['vol_rel'] = df['volume'] / df['sma_vol'].replace(0, np.nan)

    last = df.iloc[-2]
    close, open_p = last['close'], last['open']
    
    poc, vah, val = calculate_volume_profile(df.iloc[-80:])
    
    reasons = []
    signal = None

    # Метрики закрытой свечи
    spread = last['spread']
    sma_spread = last['sma_spread']
    vol_rel = last['vol_rel']
    close_ratio = last['close_ratio']

    # 1. Stopping Volume (Остановка падения - Накопление)
    if close < open_p and vol_rel > 1.8 and close_ratio > 0.5:
        signal = "CALL (ВВЕРХ 🟢)"
        reasons.append(f"**Stopping Volume**: Медвежья свеча с аномальным объемом ({vol_rel:.1f}x от нормы).")
        reasons.append("Крупный игрок встретил продажи лимитными бай-ордерами (откуп).")
        reasons.append(f"Закрытие в верхней части свечи (Ratio: {close_ratio:.2f}).")

    # 2. No Supply (Тест предложения - Нет продавцов)
    elif close < open_p and spread < (0.7 * sma_spread) and vol_rel < 0.65:
        signal = "CALL (ВВЕРХ 🟢)"
        reasons.append(f"**No Supply**: Узкий спред при крайне низком объеме ({vol_rel:.1f}x от нормы).")
        reasons.append("Предложение на рынке иссякло (продавцы отсутствуют).")

    # 3. Absorption / Stopping High (Поглощение на вершине / Остановка роста)
    elif close > open_p and vol_rel > 1.8 and close_ratio < 0.5:
        signal = "PUT (ВНИЗ 🔴)"
        reasons.append(f"**Absorption High**: Бычья свеча с аномальным объемом ({vol_rel:.1f}x от нормы).")
        reasons.append("Крупный продавец вставил лимитный барьер, цена не может расти.")
        reasons.append(f"Закрытие в нижней части свечи (Ratio: {close_ratio:.2f}).")

    # 4. No Demand (Тест спроса - Нет покупателей)
    elif close > open_p and spread < (0.7 * sma_spread) and vol_rel < 0.65:
        signal = "PUT (ВНИЗ 🔴)"
        reasons.append(f"**No Demand**: Рост на очень узком спреде и слабом объеме ({vol_rel:.1f}x от нормы).")
        reasons.append("Покупатели не поддерживают движение вверх.")

    # Добавляем аргумент по Volume Profile POC/VAH/VAL
    if signal and poc:
        if abs(close - poc) / close < 0.0008:
            reasons.append(f"📍 Цена находится прямо на **POC** (Максимальный объем дня: {poc:.5f}).")
        elif abs(close - vah) / close < 0.0008:
            reasons.append(f"📍 Отбой от верхней границы зоны стоимости **VAH** ({vah:.5f}).")
        elif abs(close - val) / close < 0.0008:
            reasons.append(f"📍 Отбой от нижней границы зоны стоимости **VAL** ({val:.5f}).")

    return signal, close, reasons, poc, vah, val

# ==================== ОФОРМЛЕНИЕ ГРАФИКА TRADINGVIEW + VSA ====================
def generate_chart(df, symbol, signal_type, poc, vah, val):
    df_plot = df.iloc[-60:].copy()
    clean_symbol = symbol.replace('=X', '')

    market_colors = mpf.make_marketcolors(
        up='#089981',
        down='#F23645',
        edge={'up': '#089981', 'down': '#F23645'},
        wick={'up': '#089981', 'down': '#F23645'},
        volume={'up': '#089981', 'down': '#F23645'}
    )
    
    tv_style = mpf.make_mpf_style(
        marketcolors=market_colors,
        facecolor='#131722',
        edgecolor='#2A2E39',
        figcolor='#131722',
        gridcolor='#2A2E39',
        gridstyle='--',
        rc={'font.size': 9, 'axes.labelcolor': '#D1D4DC', 'xtick.color': '#D1D4DC', 'ytick.color': '#D1D4DC'}
    )

    file_path = f"chart_{clean_symbol}.png"

    fig, axes = mpf.plot(
        df_plot,
        type='candle',
        volume=True,
        style=tv_style,
        title=f"\n{clean_symbol} | 5m | VSA & Volume Profile",
        savefig=dict(fname=file_path, dpi=150, bbox_inches='tight'),
        returnfig=True,
        panel_ratios=(3, 1),
        figratio=(12, 7)
    )

    ax = axes[0]
    
    # Нанесение линий Профиля Объема (POC, VAH, VAL)
    if poc:
        ax.axhline(y=poc, color='#FFD700', linestyle='-', linewidth=1.5, alpha=0.9, label='POC')
    if vah:
        ax.axhline(y=vah, color='#00BCD4', linestyle='--', linewidth=1.2, alpha=0.7, label='VAH')
    if val:
        ax.axhline(y=val, color='#00BCD4', linestyle='--', linewidth=1.2, alpha=0.7, label='VAL')

    fig.savefig(file_path, dpi=150, bbox_inches='tight')
    return file_path

# ==================== МАССОВАЯ РАССЫЛКА ====================
def broadcast_signal(bot, symbol, signal_type, price, photo_path, candle_time, reasons):
    active_users = get_active_users()
    if not active_users:
        return

    clean_symbol = symbol.replace('=X', '')
    exp_time_5m = (candle_time + timedelta(minutes=10)).strftime('%H:%M UTC')
    
    reasons_formatted = "\n".join([f"• {r}" for r in reasons])

    caption = (
        f"🚨 <b>СИГНАЛ VSA & VOLUME PROFILE</b> 🚨\n\n"
        f"<b>Пара:</b> #{clean_symbol}\n"
        f"<b>Направление:</b> {signal_type}\n"
        f"<b>Цена входа:</b> {price:.5f}\n"
        f"⏱ <b>Время экспирации:</b> до <code>{exp_time_5m}</code> (на 5 минут)\n\n"
        f"📊 <b>VSA & Объемный анализ:</b>\n"
        f"{reasons_formatted}\n\n"
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
            
    print(f"[{time.strftime('%H:%M:%S')}] VSA-Сигнал {clean_symbol} отправлен {count}/{len(active_users)} пользователям")
    
    if os.path.exists(photo_path):
        os.remove(photo_path)

# ==================== КОМАНДЫ БОТА И МЕНЮ ====================
def get_main_keyboard(is_active=True):
    if is_active:
        button_status = InlineKeyboardButton("🛑 Остановить сигналы", callback_data='disable')
    else:
        button_status = InlineKeyboardButton("▶️ Включить сигналы", callback_data='enable')
        
    keyboard = [
        [button_status],
        [InlineKeyboardButton("📊 Статус подписки", callback_data='status')]
    ]
    return InlineKeyboardMarkup(keyboard)

def start_command(update: Update, context: CallbackContext):
    user = update.effective_user
    add_user(user.id, user.username)
    
    welcome_text = (
        f"👋 <b>Добро пожаловать, {user.first_name}!</b>\n\n"
        f"Этот бот сканирует рынок Forex по методике <b>VSA (Volume Spread Analysis)</b> "
        f"и <b>Профилю Объема (POC / VAH / VAL)</b>.\n\n"
        f"Бот находит действия «крупных игроков» (накопления, поглощения, тесты) "
        f"и присылает сигналы с точным анализом и временем экспирации.\n\n"
        f"Управляйте подпиской кнопками ниже:"
    )
    update.message.reply_text(welcome_text, parse_mode='HTML', reply_markup=get_main_keyboard(True))

def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    
    if query.data == 'enable':
        set_user_status(user_id, 1)
        query.edit_message_text("✅ <b>VSA-сигналы включены!</b>", parse_mode='HTML', reply_markup=get_main_keyboard(True))
    elif query.data == 'disable':
        set_user_status(user_id, 0)
        query.edit_message_text("🛑 <b>Бот остановлен.</b>", parse_mode='HTML', reply_markup=get_main_keyboard(False))
    elif query.data == 'status':
        active_users = get_active_users()
        is_active = user_id in active_users
        status_str = "АКТИВНА 🟢" if is_active else "ОСТАНОВЛЕНА 🛑"
        query.edit_message_text(f"📋 <b>Ваш статус:</b> {status_str}", parse_mode='HTML', reply_markup=get_main_keyboard(is_active))

# ==================== ОСНОВНОЙ ЦИКЛ СКАНЕРА ====================
def market_scanner_loop(bot: Bot):
    last_signals = {symbol: None for symbol in SYMBOLS}
    
    while True:
        fetch_economic_calendar()
        
        for symbol in SYMBOLS:
            try:
                df = get_forex_klines(symbol, period='2d', interval=TIMEFRAME)
                if df is None or len(df) < 60:
                    continue
                
                signal, price, reasons, poc, vah, val = analyze_vsa(df)
                candle_time = df.index[-2]
                
                if signal and last_signals[symbol] != candle_time:
                    if not is_news_time(symbol):
                        photo_path = generate_chart(df, symbol, signal, poc, vah, val)
                        broadcast_signal(bot, symbol, signal, price, photo_path, candle_time, reasons)
                        last_signals[symbol] = candle_time
                    
            except Exception as e:
                print(f"Ошибка анализа {symbol}: {e}")
                
        time.sleep(CHECK_INTERVAL)

# ==================== ВЕБ-СЕРВЕР ДЛЯ RENDER ====================
app = Flask('')

@app.route('/')
def home():
    return "Бот сканера VSA и Volume Profile активен 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# ==================== ТОЧКА ВХОДА ====================
if __name__ == '__main__':
    init_db()
    
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CallbackQueryHandler(button_handler))

    updater.start_polling()
    print("Бот VSA & Volume Profile успешно запущен!")

    market_scanner_loop(updater.bot)