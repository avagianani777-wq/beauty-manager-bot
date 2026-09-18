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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================
# НАСТРОЙКИ
# =========================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

MODEL = "gpt-5.6-luna"

app = FastAPI()

telegram_app = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .build()
    if TELEGRAM_TOKEN
    else None
)

openai_client = (
    AsyncOpenAI(api_key=OPENAI_API_KEY)
    if OPENAI_API_KEY
    else None
)


# =========================
# AI-ПРОМПТ
# =========================

SYSTEM_PROMPT = """
Ты — Beauty Manager, AI-помощник владельца Telegram-канала
«Косметика | Парфюм | Москва».

Канал продаёт оригинальную косметику, парфюмерию и средства ухода.
Товары могут быть из США и Европы.

Твоя задача — помогать владельцу канала:
- писать продающие посты;
- создавать сторис;
- придумывать идеи Reels и TikTok;
- составлять контент-планы;
- красиво оформлять информацию о товарах;
- помогать с продвижением канала;
- придумывать опросы и вовлекающий контент.

СТИЛЬ:
- современный;
- красивый;
- женственный;
- живой;
- не слишком официальный;
- без огромного количества эмодзи;
- без дешёвого и агрессивного маркетинга;
- текст должен звучать естественно, будто его написала девушка, которая действительно любит косметику.

ВАЖНО:
Не выдумывай характеристики товара, которых пользователь не сообщил.
Не выдумывай наличие товара.
Не выдумывай скидки.
Не выдумывай состав.
Если информации недостаточно — скажи, каких данных не хватает.

Если пользователь даёт название товара, цену и количество,
используй именно эти данные.

Если пользователь просит пост — сразу дай готовый текст,
который можно опубликовать в Telegram.

Если пользователь просит сторис — сделай последовательность
из нескольких сторис с коротким текстом для каждой.

Если пользователь просит идеи Reels/TikTok —
предлагай конкретные идеи: сюжет, первые секунды, текст на экране
и призыв к действию.

Если пользователь просит контент-план —
делай его понятным и реалистичным для небольшого Telegram-канала.

Не публикуй ничего самостоятельно.
Ты только готовишь материал для владельца.
"""


# =========================
# ГЛАВНОЕ МЕНЮ
# =========================

WELCOME = """
Привет! 💗

Я Beauty Manager — твой AI-помощник для канала
«Косметика | Парфюм | Москва».

Теперь я действительно умею работать с AI ✨

Ты можешь:
📦 добавить товар
✍️ сделать пост
📱 сделать сторис
🎥 придумать Reels/TikTok
📅 составить план на неделю

Просто выбери действие ниже или напиши мне обычным сообщением.
"""


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Добавить товар", callback_data="add")],
        [InlineKeyboardButton("✍️ Сделать пост", callback_data="post")],
        [InlineKeyboardButton("📱 Сделать сторис", callback_data="stories")],
        [InlineKeyboardButton("🎥 Идеи Reels/TikTok", callback_data="reels")],
        [InlineKeyboardButton("📅 План на неделю", callback_data="plan")],
    ])


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = None

    await update.message.reply_text(
        WELCOME,
        reply_markup=main_keyboard()
    )


# =========================
# AI
# =========================

async def ask_ai(user_text: str, mode: str | None = None) -> str:

    if not openai_client:
        return (
            "⚠️ OpenAI пока не подключён.\n\n"
            "Проверь, что в Render добавлена переменная "
            "`OPENAI_API_KEY`."
        )

    mode_instruction = ""

    if mode == "post":
        mode_instruction = """
Пользователь хочет создать готовый продающий пост для Telegram.
Сделай текст полностью готовым к публикации.
"""

    elif mode == "stories":
        mode_instruction = """
Пользователь хочет серию сторис.
Раздели ответ на отдельные сторис.
Каждая сторис должна быть короткой и понятной.
"""

    elif mode == "reels":
        mode_instruction = """
Пользователь хочет идеи для Reels/TikTok.
Дай несколько конкретных идей с сюжетом и текстом на экране.
"""

    elif mode == "plan":
        mode_instruction = """
Пользователь хочет контент-план.
Сделай структурированный план публикаций на неделю.
"""

    elif mode == "add":
        mode_instruction = """
Пользователь добавляет товар.
Помоги структурировать информацию о товаре и предложи,
как лучше использовать его дальше в контенте.
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

    except Exception as e:
        logger.exception("OpenAI error")

        return (
            "Произошла ошибка при обращении к AI 😔\n\n"
            f"Техническая информация: {str(e)[:500]}"
        )


# =========================
# КНОПКИ
# =========================

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    modes = {
        "add": (
            "add",
            "📦 Пришли мне название товара, цену и количество. "
            "Можно также отправить фото."
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
            "📅 Пришли список товаров или просто скажи, "
            "какой контент хочешь продвигать на этой неделе."
        ),
    }

    mode, message = modes.get(
        query.data,
        (None, "Готово 💗")
    )

    context.user_data["mode"] = mode

    await query.message.reply_text(message)


# =========================
# ТЕКСТОВЫЕ СООБЩЕНИЯ
# =========================

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text or ""
    mode = context.user_data.get("mode")

    await update.message.chat.send_action("typing")

    answer = await ask_ai(text, mode)

    context.user_data["mode"] = None

    await update.message.reply_text(answer)


# =========================
# ФОТО
# =========================

async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    caption = update.message.caption or ""
    mode = context.user_data.get("mode")

    text = caption or "Пользователь отправил фотографию товара."

    await update.message.chat.send_action("typing")

    answer = await ask_ai(text, mode)

    context.user_data["mode"] = None

    await update.message.reply_text(
        "📸 Фото получила!\n\n" + answer
    )


# =========================
# HELP
# =========================

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

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
# TELEGRAM SETUP
# =========================

async def setup():

    if not telegram_app:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

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
        MessageHandler(filters.PHOTO, photo_message)
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message
        )
    )

    await telegram_app.initialize()
    await telegram_app.start()


# =========================
# STARTUP / SHUTDOWN
# =========================

@app.on_event("startup")
async def startup():

    await setup()

    await telegram_app.updater.start_polling()


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
        "service": "beauty-manager-bot"
    }


@app.get("/health")
async def health():

    return {
        "status": "healthy"
    }
