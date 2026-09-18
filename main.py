import os
import re
import json
import base64
import asyncio
import logging
from typing import Optional

from openai import OpenAI

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


# =========================================================
# НАСТРОЙКИ
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")


if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN")

if not OPENAI_API_KEY:
    raise RuntimeError("Не найден OPENAI_API_KEY")


# OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)


# =========================================================
# ЛОГИ
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# ВРЕМЕННОЕ ХРАНИЛИЩЕ
# =========================================================

# Для каждого пользователя запоминаем последнюю фотографию,
# чтобы можно было написать цену следующим сообщением.
pending_products = {}


# =========================================================
# ЦЕНА
# =========================================================

def extract_price(text: str) -> Optional[int]:
    """
    Понимает:
    17500
    17 500
    17.500
    17,500
    17500 ₽
    цена 17500
    цена: 17500
    """

    if not text:
        return None

    text = text.lower().strip()

    # Убираем слово "цена"
    text = re.sub(r"цена\s*[:\-]?\s*", "", text)

    # Ищем число от 3 до 6 цифр с возможными пробелами/точками/запятыми
    match = re.search(
        r"(?<!\d)(\d{1,3}(?:[\s.,]\d{3})+|\d{3,6})(?!\d)",
        text
    )

    if not match:
        return None

    value = match.group(1)

    # Убираем разделители тысяч
    value = value.replace(" ", "")
    value = value.replace(".", "")
    value = value.replace(",", "")

    try:
        price = int(value)

        # Защита от случайного распознавания чего-то вроде года
        if 100 <= price <= 999999:
            return price

    except ValueError:
        pass

    return None


# =========================================================
# РАСПОЗНАВАНИЕ ТОВАРА
# =========================================================

def recognize_product(image_bytes: bytes) -> dict:
    """
    Отправляем фотографию в OpenAI.
    Просим вернуть только:
    - категорию
    - название
    - короткое описание
    """

    image_base64 = base64.b64encode(image_bytes).decode("utf-8")

    image_url = f"data:image/jpeg;base64,{image_base64}"

    prompt = """
Ты помогаешь владельцу Telegram-магазина косметики и парфюмерии.

Посмотри на фотографию товара и определи, что это за товар.

Нужно вернуть ТОЛЬКО JSON:

{
  "hashtag": "#парфюм",
  "description": "✨ Бренд + название товара — короткое красивое описание товара в 1 предложении."
}

Правила:

1. hashtag должен быть ОДНИМ подходящим хэштегом.

Примеры:
парфюм → #парфюм
духи → #парфюм
тональный крем → #тональный_крем
румяна → #румяна
бронзер → #бронзер
пудра → #пудра
тени → #тени
тушь → #тушь
помада → #помада
блеск для губ → #блеск
крем → #крем
сыворотка → #сыворотка
шампунь → #шампунь
маска → #маска
уход за волосами → #уход_за_волосами
уход за лицом → #уход_за_лицом

Если категория другая, создай понятный короткий русский хэштег.

2. В description обязательно постарайся указать БРЕНД и НАЗВАНИЕ товара,
если их можно определить по фотографии.

3. Сделай описание коротким и привлекательным.
Пример:
"✨ HFC Devil's Intrigue — насыщенный и соблазнительный аромат с выразительным характером."

4. НЕ указывай:
- объём;
- оттенок;
- количество;
- цену;
- наличие;
- ссылки;
- источники;
- артикулы.

5. Не выдумывай конкретные характеристики, которых не видно или в которых не уверен.

6. Ответ должен быть только JSON без Markdown.
"""

    response = client.responses.create(
        model=OPENAI_MODEL,
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
                        "image_url": image_url,
                        "detail": "high",
                    },
                ],
            }
        ],
        max_output_tokens=300,
    )

    raw = response.output_text.strip()

    logger.info("Ответ OpenAI: %s", raw)

    # Иногда модель может добавить ```json
    raw = raw.replace("```json", "")
    raw = raw.replace("```", "")
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except Exception:
        logger.exception("Не удалось распарсить JSON OpenAI")

        return {
            "hashtag": "#товар",
            "description": "✨ Товар из представленного ассортимента.",
        }

    hashtag = str(data.get("hashtag", "#товар")).strip()
    description = str(
        data.get(
            "description",
            "✨ Товар из представленного ассортимента."
        )
    ).strip()

    if not hashtag.startswith("#"):
        hashtag = "#" + hashtag

    return {
        "hashtag": hashtag,
        "description": description,
    }


# =========================================================
# СОЗДАНИЕ КАРТОЧКИ
# =========================================================

def build_caption(product: dict, price: int) -> str:

    hashtag = product["hashtag"]
    description = product["description"]

    caption = (
        f"{hashtag}\n\n"
        f"{description}\n"
        f"💰 {price:,} ₽"
    )

    # Telegram caption ограничен, поэтому дополнительно страхуемся
    if len(caption) > 1000:
        caption = caption[:997] + "..."

    return caption


# =========================================================
# КНОПКИ
# =========================================================

def approval_keyboard() -> InlineKeyboardMarkup:

    keyboard = [
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

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Привет! 🩷\n\n"
        "Отправь мне фотографию товара.\n\n"
        "Можно сразу написать цену в подписи к фотографии:\n"
        "например: 17500 ₽\n\n"
        "Если цены нет — просто отправь её следующим сообщением."
    )


# =========================================================
# ФОТО
# =========================================================

async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.message
    user_id = update.effective_user.id

    logger.info(
        "Получена фотография от пользователя %s",
        user_id
    )

    try:

        # Берём самое большое доступное фото
        photo = message.photo[-1]

        telegram_file = await context.bot.get_file(photo.file_id)

        image_bytes = await telegram_file.download_as_bytearray()

        # Проверяем цену в подписи
        caption = message.caption or ""
        price = extract_price(caption)

        logger.info(
            "Цена из подписи: %s",
            price
        )

        # Сохраняем фото
        pending_products[user_id] = {
            "image_bytes": bytes(image_bytes),
            "price": price,
        }

        # Если цена уже есть — сразу обрабатываем
        if price:

            await message.reply_text(
                "🔎 Распознаю товар..."
            )

            await process_product(
                update,
                context,
                user_id
            )

        else:

            await message.reply_text(
                "📸 Фото получила!\n\n"
                "Теперь отправь цену, например:\n"
                "17500 ₽"
            )

    except Exception as e:

        logger.exception("Ошибка при обработке фотографии")

        await message.reply_text(
            "❌ Не удалось обработать фотографию.\n\n"
            f"Ошибка: {str(e)[:300]}"
        )


# =========================================================
# ТЕКСТ
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.message
    user_id = update.effective_user.id

    text = (message.text or "").strip()

    logger.info(
        "Получено текстовое сообщение от %s: %s",
        user_id,
        text
    )

    # Если есть ожидающее фото
    if user_id in pending_products:

        price = extract_price(text)

        if price:

            pending_products[user_id]["price"] = price

            await message.reply_text(
                "💰 Цена получила!\n"
                "🔎 Определяю товар..."
            )

            await process_product(
                update,
                context,
                user_id
            )

            return

    # Если это не цена
    await message.reply_text(
        "Я жду фотографию товара 📸\n\n"
        "Отправь фото, а затем цену."
    )


# =========================================================
# ОБРАБОТКА ТОВАРА
# =========================================================

async def process_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
):

    product_data = pending_products.get(user_id)

    if not product_data:
        return

    price = product_data.get("price")
    image_bytes = product_data.get("image_bytes")

    if not price or not image_bytes:
        return

    try:

        # OpenAI синхронный клиент запускаем отдельно,
        # чтобы не блокировать Telegram
        product = await asyncio.to_thread(
            recognize_product,
            image_bytes
        )

        caption = build_caption(
            product,
            price
        )

        # Отправляем карточку
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=image_bytes,
            caption=caption,
            reply_markup=approval_keyboard(),
        )

        logger.info(
            "Карточка успешно создана: %s",
            caption
        )

        # Удаляем временные данные
        pending_products.pop(user_id, None)

    except Exception as e:

        logger.exception(
            "Ошибка распознавания товара"
        )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "❌ Не получилось распознать товар.\n\n"
                f"Ошибка: {str(e)[:500]}"
            )
        )


# =========================================================
# КНОПКИ
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    if query.data == "approve":

        await query.message.reply_text(
            "✅ Товар одобрен!"
        )

    elif query.data == "delete":

        try:
            await query.message.delete()
        except Exception:
            pass


# =========================================================
# ОШИБКИ
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "Глобальная ошибка Telegram:",
        exc_info=context.error
    )


# =========================================================
# ЗАПУСК
# =========================================================

def main():

    logger.info("===================================")
    logger.info("Запуск Beauty Manager Bot")
    logger.info("OpenAI model: %s", OPENAI_MODEL)
    logger.info("===================================")

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # /start
    application.add_handler(
        CommandHandler("start", start)
    )

    # Фотографии
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_handler
        )
    )

    # Текстовые сообщения
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    # Кнопки
    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    # Глобальные ошибки
    application.add_error_handler(
        error_handler
    )

    logger.info("Бот запущен. Ожидаю сообщения...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
