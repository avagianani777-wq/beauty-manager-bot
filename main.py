import os
import re
import json
import io
import base64
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fastapi import FastAPI
from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Это реальная модель GPT-5.6 Luna.
OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-5.6-luna"
)

DB_PATH = os.getenv(
    "DB_PATH",
    "beauty_manager.db"
)

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN не найден"
    )

if not OPENAI_API_KEY:
    raise RuntimeError(
        "OPENAI_API_KEY не найден"
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(
    "beauty_manager"
)


# ============================================================
# OPENAI
# ============================================================

openai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY
)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()


# ============================================================
# DATABASE
# ============================================================

def db():
    connection = sqlite3.connect(
        DB_PATH
    )
    connection.row_factory = sqlite3.Row
    return connection


def init_database():

    connection = db()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            brand TEXT,
            name TEXT,
            category TEXT,
            volume TEXT,
            shade TEXT,
            description TEXT,
            price INTEGER,
            stock INTEGER,
            photo1 TEXT,
            photo2 TEXT,
            caption TEXT,
            status TEXT DEFAULT 'draft',
            created_at TEXT
        )
    """)

    connection.commit()
    connection.close()


# ============================================================
# USER STATES
# ============================================================

USER_STATE: Dict[int, Dict[str, Any]] = {}

PROCESS_TASKS: Dict[int, asyncio.Task] = {}


def new_state():

    return {
        "photos": [],
        "texts": [],
        "price": None,
        "stock": None,
        "processing": False,
    }


def get_state(user_id: int):

    if user_id not in USER_STATE:
        USER_STATE[user_id] = new_state()

    return USER_STATE[user_id]


def reset_state(user_id: int):

    USER_STATE[user_id] = new_state()


# ============================================================
# PRICE
# ============================================================

def parse_price(text: str) -> Optional[int]:

    if not text:
        return None

    text = text.lower()
    text = text.replace("\u00a0", " ")

    patterns = [

        # 3 500 ₽
        r"(?<!\d)(\d{1,3}(?:\s\d{3})+)\s*(?:₽|руб|рублей|р\b)",

        # 3500 ₽
        r"(?<!\d)(\d{3,6})\s*(?:₽|руб|рублей|р\b)",

        # цена 3500
        r"(?:цена|стоимость)\s*[:\-]?\s*(\d{3,6})",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if not match:
            continue

        value = match.group(1)

        value = (
            value
            .replace(" ", "")
            .replace(".", "")
            .replace(",", "")
        )

        try:

            value = int(value)

            if 100 <= value <= 999999:
                return value

        except ValueError:
            pass

    return None


# ============================================================
# STOCK
# ============================================================

def parse_stock(text: str) -> Optional[int]:

    if not text:
        return None

    patterns = [

        r"(?:в наличии|наличие)\s*[:\-]?\s*(\d+)",

        r"(\d+)\s*(?:шт|штук)",

    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text.lower()
        )

        if match:

            try:
                return int(
                    match.group(1)
                )
            except ValueError:
                pass

    return None


# ============================================================
# CATEGORY
# ============================================================

CATEGORY_MAP = {

    "парфюм": "#парфюм",
    "парфюмерия": "#парфюм",
    "духи": "#парфюм",
    "аромат": "#парфюм",

    "тональный крем": "#тональный_крем",
    "тональная основа": "#тональный_крем",
    "тон": "#тональный_крем",
    "foundation": "#тональный_крем",

    "консилер": "#консилер",

    "румяна": "#румяна",
    "blush": "#румяна",

    "бронзер": "#бронзер",
    "бронзатор": "#бронзер",
    "bronzer": "#бронзер",

    "пудра": "#пудра",
    "powder": "#пудра",

    "тени": "#тени",
    "eyeshadow": "#тени",

    "тушь": "#тушь",
    "mascara": "#тушь",

    "помада": "#помада",
    "lipstick": "#помада",

    "блеск": "#блеск_для_губ",
    "lip gloss": "#блеск_для_губ",

    "карандаш для губ": "#карандаш_для_губ",

    "крем": "#крем",

    "сыворотка": "#сыворотка",

    "маска": "#маска",

    "шампунь": "#шампунь",

    "кондиционер": "#кондиционер",

    "набор": "#набор",
    "комплект": "#набор",

    "уход": "#уход",
}


def category_hashtag(
    category: str
) -> str:

    if not category:
        return "#косметика"

    category = category.lower().strip()

    if category.startswith("#"):
        return category.replace(
            " ",
            "_"
        )

    for key, hashtag in CATEGORY_MAP.items():

        if key in category:
            return hashtag

    clean = re.sub(
        r"[^а-яa-z0-9]+",
        "_",
        category
    )

    clean = clean.strip("_")

    if not clean:
        return "#косметика"

    return "#" + clean


# ============================================================
# TELEGRAM FILE
# ============================================================

async def download_telegram_photo(
    bot,
    file_id: str
) -> bytes:

    telegram_file = await bot.get_file(
        file_id
    )

    buffer = io.BytesIO()

    await telegram_file.download_to_memory(
        buffer
    )

    return buffer.getvalue()


# ============================================================
# IMAGE -> DATA URL
# ============================================================

def image_to_data_url(
    image_bytes: bytes
) -> str:

    encoded = base64.b64encode(
        image_bytes
    ).decode("utf-8")

    return (
        "data:image/jpeg;base64,"
        + encoded
    )


# ============================================================
# РАСПОЗНАВАНИЕ ТОВАРА
# ============================================================

async def recognize_product(
    image_bytes: bytes,
    additional_text: str = ""
):

    image_url = image_to_data_url(
        image_bytes
    )

    prompt = f"""
Ты профессиональный ассистент магазина
оригинальной косметики и парфюмерии.

Твоя задача — ПО ФОТОГРАФИИ определить
конкретный товар.

Очень внимательно прочитай:
- название бренда;
- название продукта;
- надписи на упаковке;
- объём;
- оттенок;
- тип продукта.

Если товар можно определить точно —
назови его точно.

НЕ выдумывай товар.

Если на фотографии видно только часть названия,
используй визуальные признаки и известную информацию
о продукте, но не придумывай случайный бренд.

Дополнительный текст от продавца:
{additional_text or "нет"}

Верни ТОЛЬКО JSON.
Никаких пояснений до или после JSON.

Формат:

{{
    "brand": "бренд",
    "name": "полное название товара",
    "category": "категория",
    "volume": "объём",
    "shade": "оттенок или пусто",
    "description": "короткое точное описание товара",
    "confidence": 0.0
}}

confidence:
1.0 = товар определён практически точно
0.8 = высокая уверенность
0.6 = вероятно
0.4 = есть сомнения
0.2 = определить почти невозможно

Категория должна быть простой:
парфюм
тональный крем
консилер
румяна
бронзер
пудра
тени
тушь
помада
блеск для губ
крем
сыворотка
маска
шампунь
кондиционер
набор
косметика
"""

    try:

        response = await openai_client.responses.create(

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

            max_output_tokens=500,

        )

        result = (
            response.output_text
            .strip()
        )

        # Убираем ```json
        result = re.sub(
            r"^```json\s*",
            "",
            result
        )

        result = re.sub(
            r"\s*```$",
            "",
            result
        )

        data = json.loads(
            result
        )

        return data

    except Exception as error:

        logger.exception(
            "OPENAI RECOGNITION ERROR: %s",
            error
        )

        return None


# ============================================================
# CLEAN DESCRIPTION
# ============================================================

def clean_text(text: str) -> str:

    if not text:
        return ""

    text = re.sub(
        r"https?://\S+",
        "",
        text
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


# ============================================================
# CAPTION
# ============================================================

def build_caption(
    product: dict,
    price: int,
    stock: int
):

    hashtag = category_hashtag(
        product.get(
            "category",
            "косметика"
        )
    )

    brand = (
        product.get("brand")
        or ""
    ).strip()

    name = (
        product.get("name")
        or ""
    ).strip()

    volume = (
        product.get("volume")
        or ""
    ).strip()

    shade = (
        product.get("shade")
        or ""
    ).strip()

    description = clean_text(
        product.get(
            "description",
            ""
        )
    )

    if brand and name:

        title = (
            f"{brand} {name}"
        )

    elif name:

        title = name

    elif brand:

        title = brand

    else:

        title = "Товар"

    lines = [

        hashtag,

        "",

        f"✨ {title}",

    ]

    if description:

        lines.extend([
            "",
            description
        ])

    details = []

    if volume:
        details.append(
            f"Объём: {volume}"
        )

    if shade:
        details.append(
            f"Оттенок: {shade}"
        )

    if details:

        lines.extend([
            "",
            " • ".join(details)
        ])

    lines.extend([

        "",

        f"💰 Цена: {price:,} ₽"
        .replace(",", " "),

        f"📦 В наличии: {stock} шт.",

    ])

    caption = "\n".join(
        lines
    )

    # Telegram допускает до 1024 символов
    # в caption фотографии.
    if len(caption) > 1000:

        caption = (
            caption[:997]
            + "..."
        )

    return caption


# ============================================================
# SAVE
# ============================================================

def save_product(
    user_id: int,
    product: dict,
    price: int,
    stock: int,
    photo1: str,
    photo2: Optional[str],
    caption: str
):

    connection = db()

    cursor = connection.execute(
        """
        INSERT INTO products (
            user_id,
            brand,
            name,
            category,
            volume,
            shade,
            description,
            price,
            stock,
            photo1,
            photo2,
            caption,
            status,
            created_at
        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,

        (
            user_id,

            product.get(
                "brand",
                ""
            ),

            product.get(
                "name",
                ""
            ),

            category_hashtag(
                product.get(
                    "category",
                    ""
                )
            ),

            product.get(
                "volume",
                ""
            ),

            product.get(
                "shade",
                ""
            ),

            product.get(
                "description",
                ""
            ),

            price,

            stock,

            photo1,

            photo2,

            caption,

            "draft",

            datetime.now(
                timezone.utc
            ).isoformat(),
        )
    )

    product_id = (
        cursor.lastrowid
    )

    connection.commit()
    connection.close()

    return product_id


# ============================================================
# BUTTONS
# ============================================================

def product_keyboard(
    product_id: int
):

    return InlineKeyboardMarkup([

        [

            InlineKeyboardButton(
                "✅ Одобрить",
                callback_data=(
                    f"approve:{product_id}"
                )
            ),

            InlineKeyboardButton(
                "✏️ Изменить",
                callback_data=(
                    f"edit:{product_id}"
                )
            ),

        ],

        [

            InlineKeyboardButton(
                "🖼 Другое фото",
                callback_data=(
                    f"photo:{product_id}"
                )
            ),

            InlineKeyboardButton(
                "❌ Удалить",
                callback_data=(
                    f"delete:{product_id}"
                )
            ),

        ],

    ])


# ============================================================
# PROCESS PRODUCT
# ============================================================

async def process_product(
    user_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE
):

    state = get_state(
        user_id
    )

    if state["processing"]:
        return

    state["processing"] = True

    try:

        photos = state["photos"]

        if not photos:
            state["processing"] = False
            return

        price = state["price"]

        # ----------------------------------------------------
        # ЦЕНА НЕ НАЙДЕНА
        # ----------------------------------------------------

        if price is None:

            state["processing"] = False

            await context.bot.send_message(

                chat_id=chat_id,

                text=(
                    "💰 Фото получила ❤️\n\n"
                    "Теперь напиши цену товара.\n\n"
                    "Например: 3500 ₽"
                )

            )

            return

        stock = (
            state["stock"]
            or 1
        )

        # ----------------------------------------------------
        # АНИМАЦИЯ
        # ----------------------------------------------------

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action="typing"
        )

        # ----------------------------------------------------
        # СКАЧИВАЕМ ФОТО
        # ----------------------------------------------------

        image_bytes = (
            await download_telegram_photo(
                context.bot,
                photos[0]
            )
        )

        # ----------------------------------------------------
        # РАСПОЗНАЁМ
        # ----------------------------------------------------

        additional_text = "\n".join(
            state["texts"]
        )

        product = (
            await recognize_product(
                image_bytes,
                additional_text
            )
        )

        # ----------------------------------------------------
        # ЕСЛИ НЕ УДАЛОСЬ
        # ----------------------------------------------------

        if not product:

            state["processing"] = False

            await context.bot.send_message(

                chat_id=chat_id,

                text=(
                    "😔 Я не смогла прочитать товар "
                    "на фотографии.\n\n"
                    "Попробуй отправить фото крупнее, "
                    "чтобы было хорошо видно название "
                    "бренда и продукта."
                )

            )

            return

        brand = (
            product.get(
                "brand",
                ""
            )
            or ""
        ).strip()

        name = (
            product.get(
                "name",
                ""
            )
            or ""
        ).strip()

        confidence = float(
            product.get(
                "confidence",
                0
            )
            or 0
        )

        # ----------------------------------------------------
        # ПРОВЕРКА
        # ----------------------------------------------------

        if not brand and not name:

            state["processing"] = False

            await context.bot.send_message(

                chat_id=chat_id,

                text=(
                    "😔 Не получилось определить "
                    "название товара.\n\n"
                    "Пришли, пожалуйста, более чёткое "
                    "фото лицевой стороны упаковки."
                )

            )

            return

        # ----------------------------------------------------
        # ЕСЛИ НИЗКАЯ УВЕРЕННОСТЬ
        # ----------------------------------------------------

        if confidence < 0.45:

            warning = (
                "\n\n⚠️ Уверенность распознавания "
                "невысокая — пожалуйста, проверь "
                "название перед одобрением."
            )

        else:

            warning = ""

        # ----------------------------------------------------
        # CAPTION
        # ----------------------------------------------------

        caption = build_caption(
            product,
            price,
            stock
        )

        # ----------------------------------------------------
        # Сохраняем
        # ----------------------------------------------------

        photo1 = photos[0]

        photo2 = (
            photos[1]
            if len(photos) >= 2
            else None
        )

        product_id = save_product(

            user_id=user_id,

            product=product,

            price=price,

            stock=stock,

            photo1=photo1,

            photo2=photo2,

            caption=caption

        )

        keyboard = product_keyboard(
            product_id
        )

        # ----------------------------------------------------
        # ОТПРАВЛЯЕМ
        # ----------------------------------------------------

        if photo2:

            media = [

                InputMediaPhoto(
                    media=photo1,
                    caption=caption
                ),

                InputMediaPhoto(
                    media=photo2
                ),

            ]

            await context.bot.send_media_group(

                chat_id=chat_id,

                media=media

            )

            await context.bot.send_message(

                chat_id=chat_id,

                text=(
                    "Проверь карточку товара 👆"
                    + warning
                ),

                reply_markup=keyboard

            )

        else:

            await context.bot.send_photo(

                chat_id=chat_id,

                photo=photo1,

                caption=caption,

                reply_markup=keyboard

            )

        # ----------------------------------------------------
        # ОЧИЩАЕМ СОСТОЯНИЕ
        # ----------------------------------------------------

        reset_state(
            user_id
        )

    except Exception as error:

        logger.exception(
            "PROCESS ERROR: %s",
            error
        )

        state["processing"] = False

        await context.bot.send_message(

            chat_id=chat_id,

            text=(
                "⚠️ Произошла ошибка при обработке "
                "товара.\n\n"
                "Попробуй отправить фотографию ещё раз."
            )

        )


# ============================================================
# DELAYED PROCESSING
# ============================================================

def schedule_processing(
    user_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    delay: float = 1.5
):

    old_task = (
        PROCESS_TASKS.get(
            user_id
        )
    )

    if old_task and not old_task.done():

        old_task.cancel()

    async def delayed():

        try:

            await asyncio.sleep(
                delay
            )

            await process_product(
                user_id,
                chat_id,
                context
            )

        except asyncio.CancelledError:

            pass

        except Exception:

            logger.exception(
                "DELAYED ERROR"
            )

    PROCESS_TASKS[user_id] = (
        asyncio.create_task(
            delayed()
        )
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = (
        update.effective_user.id
    )

    reset_state(
        user_id
    )

    await update.message.reply_text(

        "Привет! 💕\n\n"

        "Я помогу подготовить карточку товара.\n\n"

        "Просто отправь мне фотографию товара "
        "и цену.\n\n"

        "Можно прямо так:\n"
        "📷 фото\n"
        "3500 ₽\n\n"

        "Или ещё удобнее:\n"
        "📷 фото с подписью "
        "«3500 ₽, в наличии 2 шт.»\n\n"

        "Я определю бренд, название, категорию "
        "и подготовлю карточку для твоего канала."
    )


# ============================================================
# PHOTO
# ============================================================

async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.photo:
        return

    user_id = (
        update.effective_user.id
    )

    chat_id = (
        update.effective_chat.id
    )

    state = get_state(
        user_id
    )

    # Берём самое большое фото.
    photo = (
        update.message.photo[-1]
    )

    state["photos"].append(
        photo.file_id
    )

    # --------------------------------------------------------
    # ПОДПИСЬ К ФОТО
    # --------------------------------------------------------

    caption = (
        update.message.caption
        or ""
    ).strip()

    if caption:

        state["texts"].append(
            caption
        )

        price = parse_price(
            caption
        )

        if price is not None:
            state["price"] = price

        stock = parse_stock(
            caption
        )

        if stock is not None:
            state["stock"] = stock

    # --------------------------------------------------------
    # ВТОРАЯ ФОТОГРАФИЯ
    # --------------------------------------------------------

    if len(state["photos"]) >= 2:

        schedule_processing(
            user_id,
            chat_id,
            context,
            delay=0.4
        )

        return

    # --------------------------------------------------------
    # ПЕРВАЯ ФОТОГРАФИЯ
    # --------------------------------------------------------

    schedule_processing(
        user_id,
        chat_id,
        context,
        delay=1.5
    )


# ============================================================
# TEXT
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    text = (
        update.message.text
        or ""
    ).strip()

    if not text:
        return

    user_id = (
        update.effective_user.id
    )

    chat_id = (
        update.effective_chat.id
    )

    state = get_state(
        user_id
    )

    # --------------------------------------------------------
    # ЦЕНА
    # --------------------------------------------------------

    price = parse_price(
        text
    )

    stock = parse_stock(
        text
    )

    if price is not None:

        state["price"] = price

        if stock is not None:
            state["stock"] = stock

        state["texts"].append(
            text
        )

        # Если фото уже есть,
        # обрабатываем практически сразу.
        if state["photos"]:

            schedule_processing(
                user_id,
                chat_id,
                context,
                delay=0.3
            )

            return

    # --------------------------------------------------------
    # ТОЛЬКО КОЛИЧЕСТВО
    # --------------------------------------------------------

    if stock is not None:

        state["stock"] = stock

        state["texts"].append(
            text
        )

        if state["photos"]:

            schedule_processing(
                user_id,
                chat_id,
                context,
                delay=0.3
            )

            return

    # --------------------------------------------------------
    # ТЕКСТ ПОСЛЕ ФОТО
    # --------------------------------------------------------

    if state["photos"]:

        state["texts"].append(
            text
        )

        # Иногда человек пишет:
        # "моя цена 3500"
        if state["price"] is None:

            numbers = re.findall(
                r"\d{3,6}",
                text.replace(
                    " ",
                    ""
                )
            )

            if numbers:

                try:

                    possible_price = int(
                        numbers[-1]
                    )

                    if (
                        100
                        <= possible_price
                        <= 999999
                    ):

                        state["price"] = (
                            possible_price
                        )

                except ValueError:
                    pass

        if state["price"] is not None:

            schedule_processing(
                user_id,
                chat_id,
                context,
                delay=0.3
            )

        return

    # --------------------------------------------------------
    # БЕЗ ФОТО
    # --------------------------------------------------------

    await update.message.reply_text(

        "📷 Сначала пришли фотографию товара.\n\n"
        "Цену можно написать сразу после фото."
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = (
        update.callback_query
    )

    await query.answer()

    data = (
        query.data
        or ""
    )

    try:

        action, raw_id = (
            data.split(":", 1)
        )

        product_id = int(
            raw_id
        )

    except Exception:

        return

    # --------------------------------------------------------
    # APPROVE
    # --------------------------------------------------------

    if action == "approve":

        connection = db()

        connection.execute(
            """
            UPDATE products
            SET status = 'approved'
            WHERE id = ?
            """,
            (product_id,)
        )

        connection.commit()
        connection.close()

        try:
            await query.edit_message_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        await query.message.reply_text(

            "✅ Товар одобрен!\n\n"
            "Он сохранён как готовый товар. "
            "Публикацию можно будет поставить "
            "в очередь."
        )

        return

    # --------------------------------------------------------
    # DELETE
    # --------------------------------------------------------

    if action == "delete":

        connection = db()

        connection.execute(
            """
            UPDATE products
            SET status = 'deleted'
            WHERE id = ?
            """,
            (product_id,)
        )

        connection.commit()
        connection.close()

        try:
            await query.edit_message_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        await query.message.reply_text(
            "🗑 Товар удалён."
        )

        return

    # --------------------------------------------------------
    # EDIT
    # --------------------------------------------------------

    if action == "edit":

        await query.message.reply_text(

            "✏️ Напиши, что изменить.\n\n"

            "Например:\n"
            "«Поставь цену 4500 ₽»\n"
            "«В наличии 3 шт.»\n"
            "«Название — ...»"
        )

        return

    # --------------------------------------------------------
    # PHOTO
    # --------------------------------------------------------

    if action == "photo":

        await query.message.reply_text(

            "🖼 Хорошо!\n\n"
            "Пришли новое фото товара."
        )

        return


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "Как пользоваться ботом:\n\n"

        "📷 Отправь фото.\n"
        "💰 Напиши цену.\n"
        "📦 При необходимости напиши количество.\n\n"

        "Можно одним сообщением:\n"
        "фото + «3500 ₽, в наличии 2 шт.»\n\n"

        "Или двумя:\n"
        "фото\n"
        "3500 ₽"
    )


# ============================================================
# MENU
# ============================================================

async def set_menu(
    application
):

    commands = [

        ("start", "Начать"),

        ("help", "Помощь"),

    ]

    await application.bot.set_my_commands(
        commands
    )

    try:

        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonCommands()
        )

    except Exception as error:

        logger.warning(
            "Menu error: %s",
            error
        )


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context
):

    logger.exception(
        "Telegram error: %s",
        context.error
    )


# ============================================================
# TELEGRAM
# ============================================================

telegram_app = None


async def run_bot():

    global telegram_app

    init_database()

    telegram_app = (
        Application
        .builder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .build()
    )

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_handler
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler
        )
    )

    telegram_app.add_error_handler(
        error_handler
    )

    await telegram_app.initialize()

    await telegram_app.start()

    await telegram_app.updater.start_polling(
        drop_pending_updates=True
    )

    await set_menu(
        telegram_app
    )

    logger.info(
        "BOT STARTED SUCCESSFULLY"
    )

    while True:

        await asyncio.sleep(
            3600
        )


# ============================================================
# FASTAPI STARTUP
# ============================================================

@app.on_event("startup")
async def startup():

    asyncio.create_task(
        run_bot()
    )


@app.get("/")
async def root():

    return {
        "status": "ok",
        "bot": "running"
    }


@app.get("/health")
async def health():

    return {
        "status": "healthy"
    }
