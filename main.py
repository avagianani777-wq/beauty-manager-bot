import os
import logging

from fastapi import FastAPI
from openai import AsyncOpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# НАСТРОЙКИ
# =========================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

MODEL = "gpt-5.6-luna"

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

# =========================
# APP
# =========================

app = FastAPI()

telegram_app = None

if TELEGRAM_TOKEN:
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

openai_client = None

if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# =========================
# AI PROMPT
# =========================

SYSTEM_PROMPT = """
Ты — Beauty Manager, AI-помощник владельца Telegram-канала
«Косметика | Парфюм | Москва».

Канал продаёт оригинальную косметику, парфюмерию и средства ухода.

Твоя задача:

— писать продающие посты;
— создавать серии сторис;
— придумывать идеи Reels и TikTok;
— составлять контент-планы;
— красиво оформлять информацию о товарах;
— придумывать опросы;
— помогать с продвижением канала.

СТИЛЬ:

Современный, женственный, живой и красивый.
Не слишком официальный.
Не используй слишком много эмодзи.
Не используй агрессивные продажи.
Текст должен звучать естественно, будто его написала девушка,
которая действительно любит косметику.

ВАЖНЫЕ ПРАВИЛА:

Не выдумывай характеристики товара.
Не выдумывай состав.
Не выдумывай наличие.
Не выдумывай скидки.
Не выдумывай происхождение товара.

Используй только информацию, которую сообщил владелец канала.

Если информации недостаточно, укажи, каких данных не хватает.

Если пользователь просит пост —
сразу дай готовый текст для Telegram.

Если пользователь просит сторис —
сделай несколько отдельных коротких сторис.

Если пользователь просит идеи Reels/TikTok —
предлагай конкретные идеи с сюжетом,
первой фразой, текстом на экране и призывом к действию.

Если пользователь просит контент-план —
сделай понятный и реалистичный план для небольшого канала.

Ничего самостоятельно не публикуй.
Ты только готовишь материалы для владельца.
"""

# =========================
# KEYBOARD
# =========================

def main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📦 Добавить товар",
                    callback_data="add",
                )
            ],
            [
                InlineKeyboardButton(
                    "✍️ Сделать пост",
                    callback_data="post",
                )
            ],
            [
                InlineKeyboardButton(
                    "📱 Сделать сторис",
                    callback_data="stories",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎥 Идеи Reels/TikTok",
                    callback_data="reels",
                )
            ],
            [
                InlineKeyboardButton(
                    "📅 План на неделю",
                    callback_data="plan",
                )
            ],
        ]
    )

# =========================
# START
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data["mode"] = None

    await update.message.reply_text(
        "Привет! 💗\n\n"
        "Я Beauty Manager — твой AI-помощник для канала "
        "«Косметика | Парфюм | Москва».\n\n"
        "Теперь я могу помогать тебе создавать контент с помощью AI ✨\n\n"
        "📦 добавлять товары\n"
        "✍️ создавать посты\n"
        "📱 делать сторис\n"
        "🎥 придумывать Reels/TikTok\n"
        "📅 составлять контент-план\n\n"
        "Выбери действие ниже или просто напиши мне сообщение.",
        reply_markup=main_keyboard(),
    )

# =========================
# HELP
# =========================

async def help_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Я могу помочь тебе с:\n\n"
        "✍️ постами\n"
        "📱 сторис\n"
        "🎥 Reels/TikTok\n"
        "📅 контент-планами\n"
        "📦 товарами\n\n"
        "Просто напиши, что тебе нужно 💗"
    )

# =========================
# AI
# =========================

async def ask_ai(
    user_text: str,
    mode: str | None = None,
) -> str:

    if not openai_client:
        return (
            "⚠️ OpenAI пока не подключён.\n\n"
            "Проверь переменную OPENAI_API_KEY "
            "в настройках Render."
        )

    mode_instruction = ""

    if mode == "add":
        mode_instruction = """
Пользователь добавляет товар.
Помоги структурировать информацию о нём.
"""

    elif mode == "post":
        mode_instruction = """
Пользователь хочет готовый продающий пост для Telegram.
Сделай его полностью готовым к публикации.
"""

    elif mode == "stories":
        mode_instruction = """
Пользователь хочет серию сторис.
Раздели ответ на отдельные сторис.
Каждая сторис должна быть короткой.
"""

    elif mode == "reels":
        mode_instruction = """
Пользователь хочет идеи Reels или TikTok.
Предложи несколько конкретных идей.
Для каждой укажи сюжет, первые секунды,
текст на экране и призыв к действию.
"""

    elif mode == "plan":
        mode_instruction = """
Пользователь хочет контент-план.
Сделай структурированный план на неделю.
"""

    prompt = f"""
{mode_instruction}

Запрос владельца канала:

{user_text}
"""

    try:
        response = await openai_client.responses.create(
            model=MODEL,
            instructions=SYSTEM_PROMPT,
            input=prompt,
        )

        return response.output_text

    except Exception:
        logger.exception("OpenAI request failed")

        return (
            "Не получилось получить ответ от AI 😔\n\n"
            "Проверь логи Render — там будет причина ошибки."
        )

# =========================
# BUTTONS
# =========================

async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    modes = {
        "add": (
            "add",
            "📦 Пришли название товара, цену и количество.\n\n"
            "Можно также отправить фотографию товара."
        ),
        "post": (
            "post",
            "✍️ Напиши, о каком товаре или теме сделать пост."
        ),
        "stories": (
            "stories",
            "📱 Напиши, о каком товаре или теме сделать сторис."
        ),
        "reels": (
            "reels",
            "🎥 Напиши товар или тему — придумаю идеи Reels/TikTok."
        ),
        "plan": (
            "plan",
            "📅 Пришли список товаров или напиши, "
            "что хочешь продвигать на этой неделе."
        ),
    }

    mode, message = modes.get(
        query.data,
        (None, "Готово 💗"),
    )

    context.user_data["mode"] = mode

    await query.message.reply_text(message)

# =========================
# TEXT
# =========================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = update.message.text or ""

    mode = context.user_data.get("mode")

    await update.message.chat.send_action("typing")

    answer = await ask_ai(text, mode)

    context.user_data["mode"] = None

    await update.message.reply_text(answer)

# =========================
# PHOTO
# =========================

async def photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    caption = update.message.caption or ""

    mode = context.user_data.get("mode")

    if caption:
        text = caption
    else:
        text = "Пользователь отправил фотографию товара."

    await update.message.chat.send_action("typing")

    answer = await ask_ai(text, mode)

    context.user_data["mode"] = None

    await update.message.reply_text(
        "📸 Фото получила!\n\n" + answer
    )

# =========================
# SETUP
# =========================

async def setup():
    if not telegram_app:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set"
        )

    telegram_app.add_handler(
        CommandHandler("start", start)
    )

    telegram_app.add_handler(
        CommandHandler("help", help_cmd)
    )

    telegram_app.add_handler(
        CallbackQueryHandler(button)
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_message,
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message,
        )
    )

    await telegram_app.initialize()
    await telegram_app.start()

# =========================
# STARTUP
# =========================

@app.on_event("startup")
async def startup():
    await setup()

    await telegram_app.updater.start_polling()

# =========================
# SHUTDOWN
# =========================

@app.on_event("shutdown")
async def shutdown():

    if telegram_app:

        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

# =========================
# HEALTH CHECK
# =========================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "beauty-manager-bot",
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
    }
