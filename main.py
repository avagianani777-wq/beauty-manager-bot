import os
import logging
import base64
import io

from fastapi import FastAPI
from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

TEXT_MODEL = "gpt-5.6-luna"
IMAGE_MODEL = "gpt-image-2"

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
    telegram_app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

openai_client = None

if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )

# =========================
# AI PROMPT
# =========================

SYSTEM_PROMPT = """
Ты — Beauty Manager, AI-помощник владельца Telegram-канала
«Косметика | Парфюм | Москва».

Канал продаёт оригинальную косметику, парфюмерию
и средства ухода.

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

ВАЖНЫЕ ПРАВИЛА:

Не выдумывай характеристики товара.
Не выдумывай состав.
Не выдумывай наличие.
Не выдумывай скидки.
Не выдумывай ноты аромата.

Используй только информацию,
которую сообщил владелец канала.

Если каких-то данных не хватает,
не блокируй создание поста.
Просто не включай неизвестную информацию.

Если пользователь просит пост —
сразу дай готовый текст для Telegram.

Если пользователь просит сторис —
сделай несколько коротких отдельных сторис.

Если пользователь просит идеи Reels/TikTok —
предложи конкретные идеи с сюжетом,
первой фразой, текстом на экране
и призывом к действию.

Если пользователь просит контент-план —
сделай понятный и реалистичный план.

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
            [
                InlineKeyboardButton(
                    "📸 Работа с фото",
                    callback_data="photo",
                )
            ],
        ]
    )


def photo_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✨ Улучшить моё фото",
                    callback_data="improve_photo",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎨 Сделать рекламное фото",
                    callback_data="ad_photo",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Отмена",
                    callback_data="cancel_photo",
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
    context.user_data.clear()

    await update.message.reply_text(
        "Привет! 💗\n\n"
        "Я Beauty Manager — твой AI-помощник "
        "для канала «Косметика | Парфюм | Москва».\n\n"
        "Я могу помогать тебе с товарами, постами, "
        "сторис, Reels/TikTok и фотографиями.\n\n"
        "Выбирай действие ниже или просто напиши мне.",
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
        "📦 товарами\n"
        "✍️ постами\n"
        "📱 сторис\n"
        "🎥 Reels/TikTok\n"
        "📅 контент-планами\n"
        "📸 фотографиями\n\n"
        "Просто напиши, что тебе нужно 💗"
    )

# =========================
# TEXT AI
# =========================

async def ask_ai(
    user_text: str,
    mode: str | None = None,
) -> str:

    if not openai_client:
        return (
            "⚠️ OpenAI пока не подключён.\n\n"
            "Проверь переменную OPENAI_API_KEY "
            "в Render."
        )

    mode_instruction = ""

    if mode == "add":
        mode_instruction = """
Пользователь добавляет товар.
Структурируй информацию о товаре.
"""

    elif mode == "post":
        mode_instruction = """
Пользователь хочет готовый продающий пост
для Telegram.
Сделай текст полностью готовым
к публикации.
"""

    elif mode == "stories":
        mode_instruction = """
Пользователь хочет серию сторис.
Раздели ответ на отдельные короткие сторис.
"""

    elif mode == "reels":
        mode_instruction = """
Пользователь хочет идеи Reels/TikTok.
Предложи несколько конкретных идей.
"""

    elif mode == "plan":
        mode_instruction = """
Пользователь хочет контент-план.
Сделай план на неделю.
"""

    prompt = f"""
{mode_instruction}

Запрос владельца канала:

{user_text}
"""

    try:
        response = await openai_client.responses.create(
            model=TEXT_MODEL,
            instructions=SYSTEM_PROMPT,
            input=prompt,
        )

        return response.output_text

    except Exception:
        logger.exception(
            "OpenAI text request failed"
        )

        return (
            "Не получилось получить ответ от AI 😔\n\n"
            "Проверь логи Render."
        )

# =========================
# PHOTO EDIT
# =========================

async def edit_product_photo(
    image_bytes: bytes,
    instruction: str,
) -> bytes | None:

    if not openai_client:
        return None

    try:
        image_file = io.BytesIO(image_bytes)
        image_file.name = "product.png"

        result = await openai_client.images.edit(
            model=IMAGE_MODEL,
            image=image_file,
            prompt=instruction,
            size="1024x1024",
        )

        if not result.data:
            return None

        image_data = result.data[0]

        if hasattr(image_data, "b64_json") and image_data.b64_json:
            return base64.b64decode(
                image_data.b64_json
            )

        return None

    except Exception:
        logger.exception(
            "OpenAI image edit failed"
        )

        return None

# =========================
# BUTTONS
# =========================

async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    if query.data == "photo":

        context.user_data["mode"] = "photo"

        await query.message.reply_text(
            "📸 Пришли фотографию товара.\n\n"
            "После этого я предложу варианты оформления.",
        )

        return

    if query.data == "improve_photo":

        context.user_data["photo_action"] = "improve"

        await query.message.reply_text(
            "✨ Хорошо!\n\n"
            "Теперь пришли фотографию товара, "
            "которую нужно улучшить."
        )

        return

    if query.data == "ad_photo":

        context.user_data["photo_action"] = "ad"

        await query.message.reply_text(
            "🎨 Отлично!\n\n"
            "Пришли фотографию товара. "
            "Я попробую превратить её "
            "в аккуратную рекламную композицию."
        )

        return

    if query.data == "cancel_photo":

        context.user_data["photo_action"] = None
        context.user_data["mode"] = None

        await query.message.reply_text(
            "Отменено 💗",
            reply_markup=main_keyboard(),
        )

        return

    modes = {
        "add": (
            "add",
            "📦 Пришли название товара, "
            "цену и количество.\n\n"
            "Можно также отправить фотографию."
        ),
        "post": (
            "post",
            "✍️ Напиши, о каком товаре "
            "или теме сделать пост."
        ),
        "stories": (
            "stories",
            "📱 Напиши, о каком товаре "
            "или теме сделать сторис."
        ),
        "reels": (
            "reels",
            "🎥 Напиши товар или тему — "
            "придумаю идеи Reels/TikTok."
        ),
        "plan": (
            "plan",
            "📅 Пришли список товаров "
            "или напиши, что хочешь "
            "продвигать на этой неделе."
        ),
    }

    mode, message = modes.get(
        query.data,
        (None, "Готово 💗"),
    )

    context.user_data["mode"] = mode

    await query.message.reply_text(message)

# =========================
# PHOTO MESSAGE
# =========================

async def photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    photo_action = context.user_data.get(
        "photo_action"
    )

    mode = context.user_data.get("mode")

    telegram_photo = update.message.photo[-1]

    file = await telegram_photo.get_file()

    image_bytes = await file.download_as_bytearray()

    if photo_action:

        await update.message.chat.send_action(
            "upload_photo"
        )

        if photo_action == "improve":

            instruction = """
Edit this product photo into a clean,
professional cosmetics/perfume product photo.

IMPORTANT:
Keep the actual product, bottle,
packaging, logo, label and proportions
as faithful to the original image as possible.

Improve lighting, composition and background.
Remove distracting clutter.
Create a premium, elegant,
minimal beauty-store aesthetic.

Do not invent another product.
Do not change the brand or product identity.
Do not add fake text or fake claims.

The final image should look suitable
for a Telegram cosmetics and perfume store.
"""

        else:

            instruction = """
Create a polished commercial product image
using the uploaded product photo as the reference.

Keep the exact product identity,
packaging, bottle shape, logo and label
as faithful to the original as possible.

Create an elegant premium beauty
advertising composition with tasteful lighting,
a clean luxurious background and
professional product photography.

Do not replace the product.
Do not invent a different product.
Do not add fake text, prices or claims.
"""

        result = await edit_product_photo(
            bytes(image_bytes),
            instruction,
        )

        context.user_data["photo_action"] = None
        context.user_data["mode"] = None

        if result:

            await update.message.reply_photo(
                photo=io.BytesIO(result),
                caption=(
                    "✨ Готово!\n\n"
                    "Посмотри, нравится ли тебе "
                    "такое оформление."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✨ Сделать ещё вариант",
                                callback_data="photo",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "✅ Оставить этот вариант",
                                callback_data="keep_photo",
                            )
                        ],
                    ]
                ),
            )

        else:

            await update.message.reply_text(
                "Не получилось обработать фото 😔\n\n"
                "Проверь логи Render — там будет "
                "точная причина."
            )

        return

    caption = update.message.caption or ""

    text = caption or (
        "Пользователь отправил фотографию товара."
    )

    await update.message.chat.send_action(
        "typing"
    )

    answer = await ask_ai(
        text,
        mode,
    )

    context.user_data["mode"] = None

    await update.message.reply_text(
        "📸 Фото получила!\n\n" + answer
    )

# =========================
# KEEP PHOTO
# =========================

async def keep_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "Отлично 💗 Фото оставила.\n\n"
        "Следующим этапом подключим "
        "его к карточке товара и посту.",
        reply_markup=main_keyboard(),
    )

# =========================
# TEXT MESSAGE
# =========================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = update.message.text or ""

    mode = context.user_data.get("mode")

    await update.message.chat.send_action(
        "typing"
    )

    answer = await ask_ai(
        text,
        mode,
    )

    context.user_data["mode"] = None

    await update.message.reply_text(
        answer
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
        CallbackQueryHandler(
            keep_photo,
            pattern="^keep_photo$",
        )
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
