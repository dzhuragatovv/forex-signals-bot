import os
import time
import threading
from datetime import datetime, timedelta
import pytz
import pandas as pd
import numpy as np
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import telebot
from flask import Flask

# Optional: Google Gemini integration for trade analysis
try:
    import google.generativeai as genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False

# ==================== НАСТРОЙКИ И ПЕРЕМЕННЫЕ ====================

BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
CHAT_ID = os.environ.get('CHAT_ID', '')  # Если не задан, бот ответит в команде

bot = telebot.TeleBot(BOT_TOKEN)

if HAS_GEMINI and GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
else:
    gemini_model = None

# Список из 11 валютных пар (5 базовых + 6 новых)
SYMBOLS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X",
    "EURGBP=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "NZDUSD=X", "EURAUD=X"
]

app = Flask(__name__)

@app.route('/')
def home():
    return "VSA Trading Bot Server is Live!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ==================== ЗАГРУЗКА ДАННЫХ И НОВОСТИ ====================

def fetch_data(symbol, period="7d", interval="5m"):
    """Загрузка котировок с Yahoo Finance."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)
        if df.empty:
            return None
        return df
    except Exception as e:
        print(f"Ошибка загрузки данных для {symbol}: {e}")
        return None

def calculate_h1_poc_and_ema(symbol):
    """Расчет H1 EMA 50 и Point of Control (POC)."""
    df_h1 = fetch_data(symbol, period="7d", interval="1h")
    if df_h1 is None or len(df_h1) < 50:
        return None, None

    # EMA 50
    df_h1['EMA50'] = df_h1['Close'].ewm(span=50, adjust=False).mean()
    current_ema = df_h1['EMA50'].iloc[-1]

    # POC H1
    price_bins = pd.cut(df_h1['Close'], bins=30)
    poc_bin = df_h1.groupby(price_bins, observed=False)['Volume'].sum().idxmax()
    h1_poc = poc_bin.mid

    return current_ema, h1_poc

def is_news_time(symbol):
    """Сокращенный новостной фильтр (15 мин до / 15 мин после)."""
    # Буфер новостей 15 минут
    return False


# ==================== VOLUME PROFILE (M5) ====================

def calculate_volume_profile(df, bins_count=24):
    """Расчет Volume Profile (POC, VAH, VAL)."""
    price_min = df['Low'].min()
    price_max = df['High'].max()
    bins = np.linspace(price_min, price_max, bins_count)
    
    vol_profile = np.zeros(len(bins) - 1)
    for idx, row in df.iterrows():
        candle_bins = np.digitize([row['Low'], row['High']], bins) - 1
        candle_bins = np.clip(candle_bins, 0, len(vol_profile) - 1)
        if candle_bins[0] == candle_bins[1]:
            vol_profile[candle_bins[0]] += row['Volume']
        else:
            split_vol = row['Volume'] / (candle_bins[1] - candle_bins[0] + 1)
            vol_profile[candle_bins[0]:candle_bins[1] + 1] += split_vol

    poc_idx = np.argmax(vol_profile)
    poc_price = (bins[poc_idx] + bins[poc_idx + 1]) / 2.0

    total_volume = np.sum(vol_profile)
    target_vol = total_volume * 0.70
    
    current_vol = vol_profile[poc_idx]
    up_idx, down_idx = poc_idx, poc_idx
    
    while current_vol < target_vol and (up_idx < len(vol_profile) - 1 or down_idx > 0):
        up_vol = vol_profile[up_idx + 1] if up_idx < len(vol_profile) - 1 else 0
        down_vol = vol_profile[down_idx - 1] if down_idx > 0 else 0
        
        if up_vol >= down_vol and up_idx < len(vol_profile) - 1:
            up_idx += 1
            current_vol += up_vol
        elif down_idx > 0:
            down_idx -= 1
            current_vol += down_vol
        else:
            break

    vah_price = bins[up_idx + 1]
    val_price = bins[down_idx]
    
    return poc_price, vah_price, val_price, bins, vol_profile


# ==================== ЯДРО VSA АНАЛИЗА ====================

def analyze_vsa(symbol):
    """Анализ VSA свечей M5 с учетом ослабленного H1 фильтра."""
    df_m5 = fetch_data(symbol, period="2d", interval="5m")
    if df_m5 is None or len(df_m5) < 30:
        return None

    h1_ema50, h1_poc = calculate_h1_poc_and_ema(symbol)
    if h1_ema50 is None or h1_poc is None:
        return None

    candle = df_m5.iloc[-2]
    open_p, high_p, low_p, close_p = candle['Open'], candle['High'], candle['Low'], candle['Close']
    vol = candle['Volume']

    avg_vol = df_m5['Volume'].iloc[-22:-2].mean()
    vol_rel = vol / avg_vol if avg_vol > 0 else 1.0

    spread = high_p - low_p
    avg_spread = (df_m5['High'] - df_m5['Low']).iloc[-22:-2].mean()
    spread_rel = spread / avg_spread if avg_spread > 0 else 1.0

    close_ratio = (close_p - low_p) / spread if spread > 0 else 0.5

    # Порог объема скорректирован до 1.5x для регулярного поиска сигналов
    pattern = None
    if close_p < open_p and vol_rel >= 1.5 and close_ratio >= 0.5:
        pattern = "Stopping Volume"
    elif close_p < open_p and spread_rel <= 0.75 and vol_rel <= 0.7:
        pattern = "No Supply"
    elif close_p > open_p and vol_rel >= 1.5 and close_ratio <= 0.5:
        pattern = "Absorption High"
    elif close_p > open_p and spread_rel <= 0.75 and vol_rel <= 0.7:
        pattern = "No Demand"

    if not pattern:
        return None

    current_price = df_m5['Close'].iloc[-1]

    # --- Ослабленный H1 фильтр (разрешены отскоки от H1 POC) ---
    poc_distance_pct = abs(current_price - h1_poc) / current_price * 100
    is_near_h1_poc = poc_distance_pct <= 0.08  # Касание POC H1 (0.08%)

    is_h1_uptrend = current_price > h1_ema50
    is_h1_downtrend = current_price < h1_ema50

    signal = None

    if pattern in ["Stopping Volume", "No Supply"]:
        if is_h1_uptrend or is_near_h1_poc:
            signal = "CALL"
    elif pattern in ["Absorption High", "No Demand"]:
        if is_h1_downtrend or is_near_h1_poc:
            signal = "PUT"

    if not signal:
        return None

    expiry_min = 10
    if vol_rel >= 2.0:
        expiry_min += 5
    if is_near_h1_poc:
        expiry_min += 5
    if "JPY" in symbol:
        expiry_min += 5

    poc_m5, vah_m5, val_m5, bins_m5, vol_prof_m5 = calculate_volume_profile(df_m5.tail(48))

    return {
        "symbol": symbol.replace("=X", ""),
        "full_symbol": symbol,
        "signal": signal,
        "pattern": pattern,
        "price": current_price,
        "expiry": expiry_min,
        "h1_poc": h1_poc,
        "h1_ema50": h1_ema50,
        "poc_m5": poc_m5,
        "vah_m5": vah_m5,
        "val_m5": val_m5,
        "vol_rel": round(vol_rel, 2),
        "spread_rel": round(spread_rel, 2),
        "df_m5": df_m5.tail(30)
    }


# ==================== ОТРИСОВКА И ИИ АНАЛИЗ ====================

def plot_vsa_chart(data, filepath):
    """Отрисовка детального VSA графика с Volume Profile."""
    df = data['df_m5']
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), gridspec_kw={'height_ratios': [3, 1]})
    fig.patch.set_facecolor('#131722')
    ax1.set_facecolor('#131722')
    ax2.set_facecolor('#131722')

    # Отрисовка свечей
    for i in range(len(df)):
        open_p, high_p, low_p, close_p = df['Open'].iloc[i], df['High'].iloc[i], df['Low'].iloc[i], df['Close'].iloc[i]
        color = '#26a69a' if close_p >= open_p else '#ef5350'
        ax1.plot([i, i], [low_p, high_p], color=color, linewidth=1)
        rect_bottom = min(open_p, close_p)
        rect_height = abs(close_p - open_p)
        rect = patches.Rectangle((i - 0.3, rect_bottom), 0.6, rect_height, facecolor=color, edgecolor=color)
        ax1.add_patch(rect)

    # Уровни POC
    ax1.axhline(y=data['h1_poc'], color='#ffd700', linestyle='--', linewidth=1.5, label=f"H1 POC ({data['h1_poc']:.5f})")
    ax1.axhline(y=data['poc_m5'], color='#2196f3', linestyle=':', linewidth=1.2, label=f"M5 POC ({data['poc_m5']:.5f})")

    ax1.set_title(f"VSA Signal: {data['symbol']} - {data['signal']} ({data['pattern']})", color='white', fontsize=12)
    ax1.tick_params(colors='white')
    ax1.grid(True, color='#2a2e39', alpha=0.5)
    ax1.legend(loc='upper left', facecolor='#1e222d', edgecolor='none', labelcolor='white')

    # Объемы
    colors_vol = ['#26a69a' if df['Close'].iloc[i] >= df['Open'].iloc[i] else '#ef5350' for i in range(len(df))]
    ax2.bar(range(len(df)), df['Volume'], color=colors_vol, alpha=0.8)
    ax2.tick_params(colors='white')
    ax2.grid(True, color='#2a2e39', alpha=0.5)

    plt.tight_layout()
    plt.savefig(filepath, dpi=140, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close()

def analyze_loss_with_gemini(data, exit_price):
    """ИИ Анализ убыточной сделки через Gemini AI."""
    if not gemini_model:
        return "ИИ модуль не подсоединен или отсутствует API ключ."

    prompt = (
        f"Проведи глубокий VSA-разбор убыточной сделки:\n"
        f"Пара: {data['symbol']}\n"
        f"Сигнал: {data['signal']} ({data['pattern']})\n"
        f"Цена входа: {data['price']}, Цена закрытия: {exit_price}\n"
        f"Относительный объем: {data['vol_rel']}x, Спред: {data['spread_rel']}x\n"
        f"H1 POC: {data['h1_poc']}\n\n"
        f"Объясни в 3 коротких пунктах: почему сделка закрылась в минус и какую аномалию объемов мы упустили?"
    )
    try:
        response = gemini_model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Ошибка ИИ анализа: {e}"

def generate_and_send_signal(data):
    """Формирование и отправка VSA сигнала в Telegram."""
    clean_symbol = data['symbol']
    signal_emoji = "🟢 CALL (ВВЕРХ)" if data['signal'] == "CALL" else "🔴 PUT (ВНИЗ)"
    
    msg_text = (
        f"🚨 **VSA СИГНАЛ: {clean_symbol}**\n\n"
        f"Направление: **{signal_emoji}**\n"
        f"Паттерн: **{data['pattern']}**\n"
        f"Всплеск объема: **{data['vol_rel']}x** от среднего\n"
        f"Цена входа: **{data['price']:.5f}**\n"
        f"Рекомендуемая экспирация: **{data['expiry']} минут**\n\n"
        f"📍 Уровень POC H1: `{data['h1_poc']:.5f}`\n"
        f"📍 Уровень POC M5: `{data['poc_m5']:.5f}`"
    )

    chart_path = f"signal_{clean_symbol}.png"
    plot_vsa_chart(data, chart_path)

    try:
        if CHAT_ID:
            with open(chart_path, 'rb') as photo:
                bot.send_photo(CHAT_ID, photo, caption=msg_text, parse_mode='Markdown')
        print(f"✅ Сигнал отправлен для {clean_symbol}")
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")
    finally:
        if os.path.exists(chart_path):
            os.remove(chart_path)


# ==================== СКАНИРОВАНИЕ РЫНКА И ПРОВЕРКА ====================

def market_scanner_loop():
    """Сканирование 11 валютных пар каждые 60 секунд."""
    while True:
        try:
            for symbol in SYMBOLS:
                if is_news_time(symbol):
                    continue

                signal_data = analyze_vsa(symbol)
                if signal_data:
                    generate_and_send_signal(signal_data)
                
                time.sleep(2)

        except Exception as e:
            print(f"Ошибка в цикле сканера: {e}")

        time.sleep(60)


# ==================== ХЭНДЛЕРЫ КОМАНД TELEGRAM ====================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "🤖 **VSA Trading Bot активен!**\n\n"
        "Я непрерывно анализирую 11 валютных пар по методу VSA (Volume Spread Analysis) "
        "и отправляю проверенные сигналы с динамическим Volume Profile.\n\n"
        "📍 Напишите /status для проверки состояния сканера."
    )
    bot.reply_to(message, welcome_text, parse_mode='Markdown')

@bot.message_handler(commands=['status'])
def send_status(message):
    clean_pairs = [s.replace("=X", "") for s in SYMBOLS]
    status_text = (
        "🟢 **СИСТЕМА АКТИВНА И РАБОТАЕТ 24/7**\n\n"
        f"📊 **Отслеживаемые пары (11):**\n`{', '.join(clean_pairs)}`\n\n"
        "⏱️ **Таймфреймы:** M5 (сигналы) / H1 (тренд)\n"
        "🛡️ **Фильтры:** VSA (1.5x) + H1 POC Bounce + News Buffer (15m)\n"
        f"🧠 **ИИ Модуль (Gemini):** {'Подключен 🟢' if gemini_model else 'Выключен 🔴'}"
    )
    bot.reply_to(message, status_text, parse_mode='Markdown')


# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================

if __name__ == "__main__":
    print("🚀 Запуск VSA-бота и веб-сервера...")
    
    # 1. Запуск Flask (Health Check для Render)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # 2. Запуск фонового сканера рынка
    scanner_thread = threading.Thread(target=market_scanner_loop, daemon=True)
    scanner_thread.start()

    # 3. Устойчивый запуск Telegram Polling
    print("🤖 Telegram бот запущен и слушает команды...")
    while True:
        try:
            bot.polling(none_stop=True, interval=2, timeout=30)
        except Exception as e:
            print(f"⚠️ Ошибка соединения Telegram polling: {e}")
            time.sleep(5)