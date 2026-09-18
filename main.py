import os
import logging
from fastapi import FastAPI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

if not TOKEN:
    raise RuntimeError('TELEGRAM_BOT_TOKEN is not set')

app = FastAPI()
bot_app = Application.builder().token(TOKEN).build()

WELCOME = '''Привет! 💗 Я Beauty Manager — твой AI-помощник для канала «Косметика | Парфюм | Москва».

Пока это первая рабочая версия. Уже можно проверить связь с ботом, а следующим этапом подключим AI, товары, контент и публикацию после твоего одобрения.'''

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton('📦 Добавить товар', callback_data='add')],
        [InlineKeyboardButton('✍️ Сделать пост', callback_data='post')],
        [InlineKeyboardButton('📱 Сделать сторис', callback_data='stories')],
        [InlineKeyboardButton('🎥 Reels / TikTok', callback_data='reels')],
        [InlineKeyboardButton('📅 План на неделю', callback_data='plan')],
    ]
    await update.message.reply_text(WELCOME, reply_markup=InlineKeyboardMarkup(keyboard))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Просто отправь товар, фото или запрос. Я отвечу 💗')

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Получила 💗\n\n'
        f'Твой запрос: {update.message.text}\n\n'
        'AI-модуль подключим следующим этапом.'
    )

async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or 'без подписи'
    await update.message.reply_text(
        f'Фото получила 📸💗\nПодпись: {caption}\n\n'
        'Сохранение товара и AI-анализ подключим следующим этапом.'
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    texts = {
        'add': '📦 Пришли фото товара + название + цену + количество.',
        'post': '✍️ Пришли товар или тему поста.',
        'stories': '📱 Пришли товар или тему — сделаем серию сторис.',
        'reels': '🎥 Пришли товар или тему — придумаю идеи Reels/TikTok.',
        'plan': '📅 Пришли список товаров — подготовим недельный план.',
    }
    await q.message.reply_text(texts[q.data])

bot_app.add_handler(CommandHandler('start', start))
bot_app.add_handler(CommandHandler('help', help_cmd))
bot_app.add_handler(CallbackQueryHandler(button))
bot_app.add_handler(MessageHandler(filters.PHOTO, photo_message))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

@app.on_event('startup')
async def startup():
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

@app.on_event('shutdown')
async def shutdown():
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()

@app.get('/')
def root():
    return {'status': 'ok', 'service': 'beauty-manager-bot'}
