import os
import time
import threading
import requests
import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import telebot
from flask import Flask
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from google import genai

# ==================== НАСТРОЙКИ И КОНФИГУРАЦИЯ ====================
# Получаем секреты из переменных окружения
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")

# Часовой пояс Астаны / Алматы (UTC+5)
ASTANA_TZ = ZoneInfo("Asia/Almaty")

# Валютные пары Yahoo Finance (суффикс '=X')
SYMBOLS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "EURGBP=X", "GBPJPY=X","AUDJPY=X","EURJPY=X", "GBPUSD=X"]

NEWS_BUFFER_MINUTES = 30
bot = telebot.TeleBot(BOT_TOKEN)
client = genai.Client(api_key=GEMINI_API_KEY)

# Глобальные переменные
high_impact_news = []
last_news_fetch_time = None
registered_users = set()
last_signals = {symbol: None for symbol in SYMBOLS}

# ==================== FLASK СЕРВЕР (ДЛЯ RENDER WEB SERVICE) ====================
app = Flask('')

@app.route('/')
def home():
    return "⚡ VSA Bot is running smoothly!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# ==================== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ И КОМАНДЫ ====================
def get_active_users():
    users = list(registered_users)
    if CHANNEL_ID and CHANNEL_ID not in users:
        users.append(CHANNEL_ID)
    return users

def set_user_status(user_id, status):
    if status == 0 and user_id in registered_users:
        registered_users.discard(user_id)

@bot.message_handler(commands=['start'])
def start_cmd(message):
    registered_users.add(message.chat.id)
    text = (
        "👋 <b>Добро пожаловать в VSA Analytics Bot!</b>\n\n"
        "Система успешно подключена к котировкам и готова к сканированию рынка.\n\n"
        "⚙️ <b>Параметры анализа:</b>\n"
        "• <b>Стратегия:</b> VSA (Volume Spread Analysis) + Price Action\n"
        "• <b>Таймфреймы:</b> H1 (Глобальный тренд) + M5 (Точка входа)\n"
        "• <b>Индикаторы:</b> Volume Profile (POC/VAH/VAL), EMA 50\n"
        "• <b>Часовой пояс:</b> Астана (UTC+5)\n"
        "• <b>ИИ-Разбор:</b> Gemini AI (анализ убыточных сделок) 🧠\n\n"
        "📌 <i>Сигналы будут поступать автоматически при появлении объёмных аномалий.</i>\n\n"
        "Используйте /help для просмотра руководства по сигналам."
    )
    bot.reply_to(message, text, parse_mode='HTML')

@bot.message_handler(commands=['help'])
def help_cmd(message):
    text = (
        "📖 <b>РУКОВОДСТВО ПО СИГНАЛАМ VSA</b>\n\n"
        "🟢 <b>CALL (Покупка / Вверх):</b>\n"
        "• <b>Stopping Volume:</b> Кульминация продаж. Огромный объем на падении — крупный игрок выкупает предложение.\n"
        "• <b>No Supply:</b> Тест предложения. Маленький спред и мизерный объем — продавцов на рынке нет.\n\n"
        "🔴 <b>PUT (Продажа / Вниз):</b>\n"
        "• <b>Absorption High:</b> Остановка роста. Высокий объем на закрытии внизу свечи — лимитные продажи.\n"
        "• <b>No Demand:</b> Отсутствие спроса. Слабый рост на маленьком объеме — покупатели иссякли.\n\n"
        "🎯 <b>Уровни объема (POC):</b>\n"
        "Желтая пунктирная линия на графике показывает максимальный накопленный объем. Отскоки от POC обладают максимальной точностью."
    )
    bot.reply_to(message, text, parse_mode='HTML')

@bot.message_handler(commands=['status'])
def status_cmd(message):
    text = (
        "🟢 <b>СИСТЕМА АКТИВНА</b>\n\n"
        f"📊 <b>Отслеживаемые пары:</b> {', '.join([s.replace('=X', '') for s in SYMBOLS])}\n"
        f"👥 <b>Подписчиков на сигналы:</b> {len(registered_users)}\n"
        f"🛡️ <b>Новостной фильтр:</b> Работает\n"
        f"🧠 <b>Gemini AI:</b> Готов к анализу"
    )
    bot.reply_to(message, text, parse_mode='HTML')

# ==================== ПОЛУЧЕНИЕ КОТИРОВОК YAHOO FINANCE ====================
def get_yahoo_klines(symbol, interval='5m', period='5d'):
    for attempt in range(3):
        try:
            time.sleep(1)  # Задержка перед запросом
            ticker = yf.Ticker(symbol)
            df = ticker.history(interval=interval, period=period)
            
            if df is not None and not df.empty:
                df = df.copy()
                df.rename(columns={
                    'Open': 'open', 'High': 'high', 'Low': 'low', 
                    'Close': 'close', 'Volume': 'volume'
                }, inplace=True)
                
                if df.index.tz is None:
                    df.index = df.index.tz_localize('UTC')
                else:
                    df.index = df.index.tz_convert('UTC')
                    
                if df['volume'].sum() == 0:
                    df['volume'] = ((df['high'] - df['low']) * 100000).astype(int) + 1
                    
                return df[['open', 'high', 'low', 'close', 'volume']]
                
        except Exception as e:
            print(f"⚠️ Попытка {attempt + 1}/3 не удалась для {symbol}: {e}")
            time.sleep(5)
            
    print(f"❌ Не удалось получить данные по {symbol} после 3 попыток.")
    return None

# ==================== ЭКОНОМИЧЕСКИЙ КАЛЕНДАРЬ ====================
def get_currency_by_country(country_code):
    mapping = {'US': 'USD', 'EU': 'EUR', 'GB': 'GBP', 'JP': 'JPY', 'AU': 'AUD', 'CA': 'CAD'}
    return mapping.get(country_code, 'USD')

def fetch_economic_calendar():
    global high_impact_news, last_news_fetch_time
    now_astana = datetime.now(ASTANA_TZ)
    
    if last_news_fetch_time and (now_astana - last_news_fetch_time).seconds < 7200:
        return

    url = "https://economic-calendar.tradingview.com/events"
    from_time = now_astana.replace(hour=0, minute=0, second=0).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    to_time = now_astana.replace(hour=23, minute=59, second=59).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    
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
                    event_time = datetime.fromisoformat(event['date'].replace('Z', '+00:00')).astimezone(ASTANA_TZ)
                    news_list.append({
                        'currency': get_currency_by_country(event.get('country')),
                        'title': event.get('title'),
                        'time': event_time
                    })
            high_impact_news = news_list
            last_news_fetch_time = now_astana
            print(f"[{now_astana.strftime('%H:%M:%S')}] Календарь новостей обновлен ({len(high_impact_news)} событий)")
    except Exception as e:
        print(f"⚠️ Предупреждение календаря: {e}")

def is_news_time(symbol):
    if not high_impact_news: return False
    clean_symbol = symbol[:3]
    now_astana = datetime.now(ASTANA_TZ)
    buffer = timedelta(minutes=NEWS_BUFFER_MINUTES)
    
    for news in high_impact_news:
        if news['currency'] in clean_symbol:
            if (news['time'] - buffer) <= now_astana <= (news['time'] + buffer):
                print(f"⚠️ Сигнал по {symbol} пропущен из-за новости: {news['title']}")
                return True
    return False

# ==================== VOLUME PROFILE (ПРОФИЛЬ ОБЪЕМА) ====================
def calculate_volume_profile(df, bins_count=30):
    if df is None or len(df) == 0: return None, None, None
        
    price_min, price_max = df['low'].min(), df['high'].max()
    if price_min == price_max: return None, None, None
        
    bins = np.linspace(price_min, price_max, bins_count)
    volume_profile = np.zeros(len(bins) - 1)

    for _, row in df.iterrows():
        c_low, c_high, c_vol = row['low'], row['high'], row['volume']
        if c_high == c_low: continue
        for i in range(len(bins) - 1):
            if c_low <= bins[i+1] and c_high >= bins[i]:
                overlap = min(c_high, bins[i+1]) - max(c_low, bins[i])
                volume_profile[i] += c_vol * (overlap / (c_high - c_low))

    poc_index = np.argmax(volume_profile)
    poc_price = (bins[poc_index] + bins[poc_index + 1]) / 2

    total_vol = np.sum(volume_profile)
    target_vol = total_vol * 0.70
    
    current_vol = volume_profile[poc_index]
    min_idx, max_idx = poc_index, poc_index

    while current_vol < target_vol:
        next_min = min_idx - 1 if min_idx > 0 else None
        next_max = max_idx + 1 if max_idx < len(volume_profile) - 1 else None

        if next_min is None and next_max is None: break
        elif next_min is None:
            max_idx = next_max; current_vol += volume_profile[max_idx]
        elif next_max is None:
            min_idx = next_min; current_vol += volume_profile[min_idx]
        else:
            if volume_profile[next_min] >= volume_profile[next_max]:
                min_idx = next_min; current_vol += volume_profile[min_idx]
            else:
                max_idx = next_max; current_vol += volume_profile[max_idx]

    return poc_price, bins[max_idx + 1], bins[min_idx]

# ==================== ДИНАМИЧЕСКАЯ ЭКСПИРАЦИЯ ====================
def calculate_expiration(symbol, vol_rel, has_volume_level):
    base_minutes = 10
    if vol_rel > 2.2: base_minutes = 15
    if has_volume_level: base_minutes += 5
    if 'JPY' in symbol: base_minutes += 5
    return max(5, min(base_minutes, 30))

# ==================== АНАЛИЗ СТАРШЕГО ТАЙМФРЕЙМА (H1) ====================
def analyze_h1_timeframe(symbol):
    df_h1 = get_yahoo_klines(symbol, interval='60m', period='7d')
    if df_h1 is None or len(df_h1) < 30:
        return "NEUTRAL", [], None, None, None

    df_h1['ema50'] = df_h1['close'].ewm(span=50, adjust=False).mean()
    last_close = df_h1['close'].iloc[-1]
    last_ema50 = df_h1['ema50'].iloc[-1]

    trend = "BULLISH" if last_close > last_ema50 else "BEARISH"
    trend_str = "Восходящий 🟢" if trend == "BULLISH" else "Нисходящий 🔴"

    poc_h1, vah_h1, val_h1 = calculate_volume_profile(df_h1.iloc[-80:])

    h1_notes = [
        f"• **Глобальный тренд H1**: {trend_str} (Цена {'выше' if last_close > last_ema50 else 'ниже'} EMA 50)."
    ]
    if poc_h1:
        h1_notes.append(f"• **Исторический POC H1**: <code>{poc_h1:.5f}</code> (Ключевой объем).")

    return trend, h1_notes, poc_h1, vah_h1, val_h1

# ==================== VSA И ТЕХНИЧЕСКИЙ АНАЛИЗ (M5) ====================
def analyze_vsa(df, symbol):
    df = df.copy()
    h1_trend, h1_reasons, poc_h1, vah_h1, val_h1 = analyze_h1_timeframe(symbol)

    df['spread'] = df['high'] - df['low']
    df['sma_spread'] = df['spread'].rolling(window=20).mean()
    df['sma_vol'] = df['volume'].rolling(window=20).mean()
    df['close_ratio'] = (df['close'] - df['low']) / df['spread'].replace(0, np.nan)
    df['vol_rel'] = df['volume'] / df['sma_vol'].replace(0, np.nan)

    last = df.iloc[-2]
    close, open_p = last['close'], last['open']

    poc_m5, vah_m5, val_m5 = calculate_volume_profile(df.iloc[-80:])

    reasons = []
    signal = None

    spread, sma_spread = last['spread'], last['sma_spread']
    vol_rel, close_ratio = last['vol_rel'], last['close_ratio']
    has_volume_level = False

    candle_body = abs(close - open_p)
    lower_wick = min(close, open_p) - last['low']
    upper_wick = last['high'] - max(close, open_p)

    # VSA Паттерны
    if close < open_p and vol_rel > 1.8 and close_ratio > 0.5:
        signal = "CALL (ВВЕРХ 🟢)"
        reasons.append(f"**Stopping Volume (M5)**: Кульминация продаж, объем ({vol_rel:.1f}x).")
        if lower_wick > candle_body: reasons.append("• **Свечной анализ**: Длинный нижний фитиль — лимитный выкуп.")

    elif close < open_p and spread < (0.7 * sma_spread) and vol_rel < 0.65:
        signal = "CALL (ВВЕРХ 🟢)"
        reasons.append(f"**No Supply (M5)**: Тест предложения, мизерный объем ({vol_rel:.1f}x).")

    elif close > open_p and vol_rel > 1.8 and close_ratio < 0.5:
        signal = "PUT (ВНИЗ 🔴)"
        reasons.append(f"**Absorption High (M5)**: Остановка роста лимитными продажами ({vol_rel:.1f}x).")
        if upper_wick > candle_body: reasons.append("• **Свечной анализ**: Длинная верхняя тень — отторжение цен.")

    elif close > open_p and spread < (0.7 * sma_spread) and vol_rel < 0.65:
        signal = "PUT (ВНИЗ 🔴)"
        reasons.append(f"**No Demand (M5)**: Безобъемный слабый рост ({vol_rel:.1f}x).")

    # Фильтр по тренду H1
    if signal:
        if "BULLISH" in h1_trend and "CALL" not in signal: return None, close, reasons, poc_m5, vah_m5, val_m5, 5
        elif "BEARISH" in h1_trend and "PUT" not in signal: return None, close, reasons, poc_m5, vah_m5, val_m5, 5

        reasons.insert(0, "📈 **АНАЛИЗ СТАРШЕГО ТАЙМФРЕЙМА (H1):**")
        for idx, h1_note in enumerate(h1_reasons): reasons.insert(1 + idx, h1_note)
        reasons.append("\n📉 **АНАЛИЗ МЛАДШЕГО ТАЙМФРЕЙМА (M5):**")

    if signal:
        if poc_h1 and abs(close - poc_h1) / close < 0.001:
            reasons.append(f"• 🎯 Реакция от **исторического POC H1** ({poc_h1:.5f}).")
            has_volume_level = True
        elif poc_m5 and abs(close - poc_m5) / close < 0.0008:
            reasons.append(f"• 📍 Отскок от локального **POC M5** ({poc_m5:.5f}).")
            has_volume_level = True

    exp_minutes = calculate_expiration(symbol, vol_rel, has_volume_level)
    return signal, close, reasons, poc_m5, vah_m5, val_m5, exp_minutes

# ==================== ИИ-РАЗБОР ОШИБКИ (GEMINI) ====================
def ask_ai_about_loss(symbol, signal_type, entry_price, exit_price, reasons, df_recent):
    try:
        candles_summary = ""
        for idx, row in df_recent.iterrows():
            candles_summary += f"- Time: {idx.strftime('%H:%M')}, O: {row['open']:.5f}, H: {row['high']:.5f}, L: {row['low']:.5f}, C: {row['close']:.5f}, Vol: {int(row['volume'])}\n"

        prompt = (
            f"Ты — опытный VSA и Price Action трейдер.\n"
            f"Зафиксирован МИНУС по сигналу торгового бота на binary options.\n\n"
            f"📌 Данные сделки:\n"
            f"- Актив: {symbol}\n"
            f"- Направление сигнала: {signal_type}\n"
            f"- Цена входа: {entry_price:.5f}\n"
            f"- Цена закрытия: {exit_price:.5f}\n"
            f"- Логика входа: {', '.join(reasons)}\n\n"
            f"📊 Последние свечи (M5):\n{candles_summary}\n"
            f"Сделай краткий разбор (3-4 предложения):\n"
            f"1. Почему сигнал оказался неверным?\n"
            f"2. Какую анатомию свечи или аномалию объемов алгоритм мог упустить?\n"
            f"Пиши четко, профессионально и с понятными трейдеру выводами."
        )

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"⚠️ Ошибка обращения к Gemini AI: {e}")
        return None

# ==================== ГЕНЕРАЦИЯ ГРАФИКА ====================
def generate_chart(df, symbol, poc, vah, val):
    plt.style.use('dark_background')
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), gridspec_kw={'height_ratios': [3, 1]}, sharex=True)
    
    fig.patch.set_facecolor('#131722')
    ax1.set_facecolor('#1e222d')
    ax2.set_facecolor('#1e222d')
    
    df_plot = df.iloc[-50:].copy()

    for i in range(len(df_plot)):
        color = '#00E676' if df_plot['close'].iloc[i] >= df_plot['open'].iloc[i] else '#FF5252'
        ax1.plot([i, i], [df_plot['low'].iloc[i], df_plot['high'].iloc[i]], color=color, linewidth=1.2)
        ax1.plot([i, i], [df_plot['open'].iloc[i], df_plot['close'].iloc[i]], color=color, linewidth=4.5)
        ax2.bar(i, df_plot['volume'].iloc[i], color=color, alpha=0.75, width=0.6)

    clean_name = symbol.replace('=X', '')
    
    if poc: ax1.axhline(poc, color='#FFD700', linestyle='--', linewidth=1.8, label=f'🎯 POC (Макс. объём): {poc:.5f}')
    if vah: ax1.axhline(vah, color='#00E676', linestyle=':', linewidth=1.2, label=f'🟢 VAH (Верх зоны): {vah:.5f}')
    if val: ax1.axhline(val, color='#FF5252', linestyle=':', linewidth=1.2, label=f'🔴 VAL (Низ зоны): {val:.5f}')

    ax1.set_title(f"⚡ VSA POWER SCANNER | {clean_name} [M5] ⚡", fontsize=15, color='#FFFFFF', fontweight='bold', pad=12)
    ax1.grid(True, color='#2a2e39', linestyle='--', alpha=0.4)
    ax1.legend(loc='upper left', facecolor='#1e222d', edgecolor='#363c4e', labelcolor='white', fontsize=10)
    ax2.grid(True, color='#2a2e39', linestyle='--', alpha=0.4)
    ax2.set_ylabel("Volume", color='#848e9c', fontsize=10)

    plt.tight_layout()
    chart_path = f"chart_{clean_name}.png"
    plt.savefig(chart_path, dpi=160, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close()
    return chart_path

# ==================== ФОНОВАЯ ПРОВЕРКА РЕЗУЛЬТАТА ====================
def check_trade_result(bot, chat_ids, symbol, signal_type, entry_price, exp_minutes, reasons):
    sleep_seconds = (exp_minutes * 60) + 15
    time.sleep(sleep_seconds)
    
    try:
        df = get_yahoo_klines(symbol, interval='5m', period='1d')
        if df is None or len(df) == 0: return
            
        exit_price = df.iloc[-1]['close']
        
        is_call = "CALL" in signal_type
        is_win = (exit_price > entry_price) if is_call else (exit_price < entry_price)
        
        multiplier = 1000 if 'JPY' in symbol else 100000
        diff_pips = (exit_price - entry_price) * multiplier if is_call else (entry_price - exit_price) * multiplier
        clean_name = symbol.replace('=X', '')

        if is_win:
            result_header = "🎉💰 <b>ОТЛИЧНЫЙ ПРОФИТ! СДЕЛАНО!</b> 💰🎉"
            status_text = f"🏆 <b>ИТОГ: УВЕРЕННЫЙ ПЛЮС (+{diff_pips:.1f} п.)</b> 🟢"
            footer_note = "🔥 Отличная отработка объёмов! Продолжаем в том же духе!"
        else:
            result_header = "😅📉 <b>УПС... РЫНОК ОКАЗАЛСЯ СИЛЬНЕЕ</b> 📉"
            status_text = f"❌ <b>ИТОГ: МИНУС ({diff_pips:.1f} п.)</b> 🔴"
            footer_note = "🧠 <i>Отправляю запрос в Gemini AI для разбора ошибки...</i>"
        
        result_text = (
            f"{result_header}\n\n"
            f"📊 <b>Пара:</b> #{clean_name}\n"
            f"🎬 <b>Направление:</b> {signal_type}\n"
            f"🏁 <b>Цена входа:</b> <code>{entry_price:.5f}</code>\n"
            f"🏁 <b>Цена выхода:</b> <code>{exit_price:.5f}</code>\n\n"
            f"{status_text}\n\n"
            f"{footer_note}"
        )
        
        for user_id in chat_ids:
            try:
                bot.send_message(chat_id=user_id, text=result_text, parse_mode='HTML')
                time.sleep(0.05)
            except Exception as e:
                if "bot was blocked" in str(e).lower(): set_user_status(user_id, 0)

        # Разбор ИИ при минусе
        if not is_win:
            ai_analysis = ask_ai_about_loss(clean_name, signal_type, entry_price, exit_price, reasons, df.tail(5))
            if ai_analysis:
                ai_message = (
                    f"🧠 <b>РАЗБОР ОШИБКИ ОТ GEMINI AI</b> 🤖\n\n"
                    f"{ai_analysis}\n\n"
                    f"💡 <i>Учитывай эту анатомию свечи в следующих сделках!</i>"
                )
                for user_id in chat_ids:
                    try:
                        bot.send_message(chat_id=user_id, text=ai_message, parse_mode='HTML')
                        time.sleep(0.05)
                    except Exception as e:
                        pass
                    
    except Exception as e:
        print(f"⚠️ Ошибка проверки результата для {symbol}: {e}")

# ==================== РАССЫЛКА СИГНАЛОВ ====================
def broadcast_signal(bot, symbol, signal_type, price, photo_path, candle_time, reasons, exp_minutes):
    active_users = get_active_users()
    if not active_users: return

    if candle_time.tzinfo is None:
        candle_time = candle_time.replace(tzinfo=timezone.utc)
    
    candle_astana = candle_time.astimezone(ASTANA_TZ)
    exp_time = (candle_astana + timedelta(minutes=exp_minutes + 5)).strftime('%H:%M (Астана)')
    
    clean_name = symbol.replace('=X', '')
    reasons_formatted = "\n".join([r if r.startswith('\n') or r.startswith('📈') or r.startswith('📉') else f"  • {r}" for r in reasons])

    if "CALL" in signal_type:
        header_emoji = "🚀🔥 <b>СИГНАЛ НА ПОКУПКУ (CALL)!</b> 🔥🚀"
        action_badge = "🟢 <b>ВХОД ВВЕРХ (CALL)</b>"
    else:
        header_emoji = "💥📉 <b>СИГНАЛ НА ПРОДАЖУ (PUT)!</b> 📉💥"
        action_badge = "🔴 <b>ВХОД ВНИЗ (PUT)</b>"

    caption = (
        f"{header_emoji}\n\n"
        f"💎 <b>Актив:</b> <code>#{clean_name}</code>\n"
        f"🎯 <b>Действие:</b> {action_badge}\n"
        f"📍 <b>Точка входа:</b> <code>{price:.5f}</code>\n"
        f"⏳ <b>Время экспирации:</b> <b>{exp_minutes} МИНУТ</b> (до <code>{exp_time}</code>)\n\n"
        f"🧠 <b>ГЛУБОКАЯ АНАЛИТИКА:</b>\n{reasons_formatted}\n\n"
        f"⚡ <i>Следите за рисками и открывайте сделку строго на новой свече!</i> 🔥"
    )
    
    for user_id in active_users:
        try:
            with open(photo_path, 'rb') as photo:
                bot.send_photo(chat_id=user_id, photo=photo, caption=caption, parse_mode='HTML')
            time.sleep(0.05)
        except Exception as e:
            if "bot was blocked" in str(e).lower(): set_user_status(user_id, 0)
            
    if os.path.exists(photo_path): os.remove(photo_path)

    thread = threading.Thread(
        target=check_trade_result,
        args=(bot, active_users, symbol, signal_type, price, exp_minutes, reasons)
    )
    thread.daemon = True
    thread.start()

# ==================== ЦИКЛ СКАНИРОВАНИЯ РЫНКА ====================
def market_scanner_loop():
    while True:
        try:
            fetch_economic_calendar()

            for symbol in SYMBOLS:
                df = get_yahoo_klines(symbol, interval='5m', period='2d')
                if df is None or len(df) < 30:
                    continue

                signal, price, reasons, poc, vah, val, exp_minutes = analyze_vsa(df, symbol)
                candle_time = df.index[-2]

                if signal and last_signals[symbol] != candle_time:
                    if not is_news_time(symbol):
                        photo_path = generate_chart(df, symbol, poc, vah, val)
                        broadcast_signal(bot, symbol, signal, price, photo_path, candle_time, reasons, exp_minutes)
                        last_signals[symbol] = candle_time

        except Exception as e:
            print(f"⚠️ Ошибка сканера рынка: {e}")

        time.sleep(60)

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================
if __name__ == "__main__":
    print("🚀 Запуск VSA-бота и веб-сервера...")
    
    # 1. Запуск Flask на фоновом потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # 2. Запуск сканера рынка на фоновом потоке
    scanner_thread = threading.Thread(target=market_scanner_loop, daemon=True)
    scanner_thread.start()

    # 3. Запуск опроса Telegram API с защитой от сбоев в основном потоке
    print("🤖 Telegram бот запущен и слушает команды...")
    while True:
        try:
            bot.polling(none_stop=True, interval=1, timeout=60)
        except Exception as e:
            print(f"⚠️ Сбой поллинга Telegram: {e}")
            time.sleep(5)