import os
import re
import json
import base64
import asyncio
import logging

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
# SETTINGS
# =========================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_KEY = os.environ["OPENAI_API_KEY"]

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

openai = AsyncOpenAI(api_key=OPENAI_KEY)


# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# =========================
# TEMPORARY USER DATA
# =========================

users = {}


# =========================
# PRICE
# =========================

def get_price(text: str):

    if not text:
        return None

    text = text.lower()

    # 17 500
    # 17.500
    # 17,500
    # 17500
    match = re.search(
        r"(?<!\d)(\d{1,3}(?:[\s.,]\d{3})+|\d{3,6})(?!\d)",
        text,
    )

    if not match:
        return None

    number = (
        match.group(1)
        .replace(" ", "")
        .replace(".", "")
        .replace(",", "")
    )

    try:
        price = int(number)

        if 100 <= price <= 999999:
            return price

    except ValueError:
        pass

    return None


# =========================
# AI RECOGNITION
# =========================

async def recognize(image_bytes: bytes):

    image64 = base64.b64encode(image_bytes).decode("utf-8")

    prompt = """
Ты создаёшь карточки товаров для Telegram-магазина оригинальной
косметики и парфюмерии.

Посмотри на фотографию.

Определи:
1. Что это за товар.
2. Бренд и название, если их можно определить.
3. Подходящий русский хэштег категории.
4. Сделай короткое красивое описание.

Верни ТОЛЬКО JSON:

{
  "hashtag": "#парфюм",
  "description": "✨ Бренд Название — короткое красивое описание товара."
}

Возможные хэштеги:

#парфюм
#тональный_крем
#румяна
#бронзер
#пудра
#тени
#тушь
#помада
#блеск
#крем
#сыворотка
#маска
#шампунь
#уход_за_лицом
#уход_за_волосами
#макияж
#уход

Если товар другой — придумай один короткий понятный хэштег.

ВАЖНО:

Не пиши:
- объём;
- оттенок;
- наличие;
- количество;
- цену;
- ссылки;
- источники.

Только хэштег и описание.

Если точное название невозможно определить, не выдумывай его.
"""

    response = await openai.responses.create(
        model=MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt,
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{image64}",
                    },
                ],
            }
        ],
        max_output_tokens=300,
    )

    result = response.output_text.strip()

    logger.info("AI: %s", result)

    # Убираем markdown, если модель его добавила
    result = result.replace("```json", "")
    result = result.replace("```", "")
    result = result.strip()

    try:
        data = json.loads(result)

        hashtag = data.get("hashtag", "#товар")
        description = data.get(
            "description",
            "✨ Товар из ассортимента."
        )

        if not hashtag.startswith("#"):
            hashtag = "#" + hashtag

        return hashtag, description

    except Exception:

        logger.exception("Ошибка JSON")

        return (
            "#товар",
            "✨ Товар из нашего ассортимента."
        )


# =========================
# CARD
# =========================

def make_caption(hashtag, description, price):

    return (
        f"{hashtag}\n\n"
        f"{description}\n\n"
        f"💰 {price:,} ₽"
    )


# =========================
# BUTTONS
# =========================

def buttons():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Одобрить",
                    callback_data="approve"
                ),
                InlineKeyboardButton(
                    "❌ Удалить",
                    callback_data="delete"
                ),
            ]
        ]
    )


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Привет! 🩷\n\n"
        "Отправь фотографию товара.\n"
        "Цена может быть:\n"
        "• в подписи к фото\n"
        "• следующим сообщением\n\n"
        "Например: 17500 ₽"
    )


# =========================
# PHOTO
# =========================

async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        user_id = update.effective_user.id
        message = update.message

        logger.info("PHOTO from %s", user_id)

        telegram_photo = message.photo[-1]

        file = await context.bot.get_file(
            telegram_photo.file_id
        )

        image = await file.download_as_bytearray()

        image = bytes(image)

        price = get_price(
            message.caption or ""
        )

        users[user_id] = {
            "image": image,
            "price": price,
        }

        # Цена уже есть
        if price:

            await message.reply_text(
                "🔎 Распознаю товар..."
            )

            await create_product(
                update,
                context,
                user_id,
            )

        # Цены нет
        else:

            await message.reply_text(
                "📸 Фото получила!\n\n"
                "Теперь отправь цену.\n"
                "Например: 17500 ₽"
            )

    except Exception as e:

        logger.exception("PHOTO ERROR")

        await update.message.reply_text(
            "❌ Ошибка при получении фотографии."
        )


# =========================
# TEXT
# =========================

async def text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id
    message = update.message

    text_value = message.text or ""

    logger.info(
        "TEXT from %s: %s",
        user_id,
        text_value,
    )

    # Есть фотография, ждём цену
    if user_id in users:

        price = get_price(text_value)

        if price:

            users[user_id]["price"] = price

            await message.reply_text(
                "💰 Цена получила!\n"
                "🔎 Распознаю товар..."
            )

            await create_product(
                update,
                context,
                user_id,
            )

            return

    await message.reply_text(
        "Отправь сначала фотографию товара 📸"
    )


# =========================
# CREATE PRODUCT
# =========================

async def create_product(
    update,
    context,
    user_id,
):

    data = users.get(user_id)

    if not data:
        return

    image = data["image"]
    price = data["price"]

    if not price:
        return

    try:

        hashtag, description = await recognize(
            image
        )

        caption = make_caption(
            hashtag,
            description,
            price,
        )

        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=image,
            caption=caption,
            reply_markup=buttons(),
        )

        logger.info(
            "PRODUCT CREATED: %s",
            caption,
        )

        users.pop(user_id, None)

    except Exception as e:

        logger.exception(
            "CREATE PRODUCT ERROR"
        )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "❌ Не удалось создать карточку.\n\n"
                f"{str(e)[:400]}"
            ),
        )


# =========================
# BUTTONS
# =========================

async def callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    if query.data == "approve":

        await query.message.reply_text(
            "✅ Товар одобрен."
        )

    elif query.data == "delete":

        try:
            await query.message.delete()
        except Exception:
            pass


# =========================
# ERROR
# =========================

async def error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "BOT ERROR",
        exc_info=context.error,
    )


# =========================
# MAIN
# =========================

def main():

    logger.info("STARTING BOT")
    logger.info("MODEL: %s", MODEL)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text,
        )
    )

    app.add_handler(
        CallbackQueryHandler(callback)
    )

    app.add_error_handler(error)

    logger.info("BOT IS RUNNING")

    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
