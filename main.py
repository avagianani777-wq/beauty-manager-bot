import os
import re
import json
import asyncio
import sqlite3
import logging
import base64
import mimetypes
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
    MenuButtonCommands,
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_KEY = os.environ["OPENAI_API_KEY"]

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

DB_PATH = os.getenv(
    "DB_PATH",
    "beauty_manager.db"
)

DEFAULT_TIMEZONE = os.getenv(
    "DEFAULT_TIMEZONE",
    "Europe/Moscow"
)

client = AsyncOpenAI(
    api_key=OPENAI_KEY
)

app = FastAPI()

telegram_app = (
    Application
    .builder()
    .token(TOKEN)
    .build()
)

scheduler_task = None


# ============================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [
            KeyboardButton("➕ Новый товар"),
            KeyboardButton("📝 Новый пост"),
        ],
        [
            KeyboardButton("📅 Контент-план"),
            KeyboardButton("⏰ Расписание"),
        ],
        [
            KeyboardButton("📋 Очередь"),
            KeyboardButton("📢 Канал"),
        ],
        [
            KeyboardButton("💡 Идеи"),
        ],
    ],
    resize_keyboard=True,
)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    connection = sqlite3.connect(
        DB_PATH,
        timeout=30
    )
    connection.row_factory = sqlite3.Row
    return connection


def init_database():

    connection = get_db()

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,

            channel_id TEXT,

            timezone TEXT
                DEFAULT 'Europe/Moscow',

            paused INTEGER
                DEFAULT 0,

            days TEXT
                DEFAULT '[0,1,2,3,4,5,6]',

            times TEXT
                DEFAULT '["10:00","15:00","20:00"]'
        );


        CREATE TABLE IF NOT EXISTS products (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER NOT NULL,

            name TEXT,

            brand TEXT,

            category TEXT,

            item_type TEXT,

            price TEXT,

            stock TEXT,

            volume TEXT,

            shade TEXT,

            description TEXT,

            photo1 TEXT,

            photo2 TEXT,

            created_at TEXT
        );


        CREATE TABLE IF NOT EXISTS queue (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER NOT NULL,

            kind TEXT NOT NULL,

            status TEXT NOT NULL
                DEFAULT 'queued',

            scheduled_at TEXT NOT NULL,

            caption TEXT,

            photo1 TEXT,

            photo2 TEXT,

            created_at TEXT
        );
        """
    )

    connection.commit()
    connection.close()


def ensure_user(user_id):

    connection = get_db()

    connection.execute(
        """
        INSERT OR IGNORE INTO users(user_id)
        VALUES(?)
        """,
        (user_id,)
    )

    connection.commit()
    connection.close()


def get_user(user_id):

    ensure_user(user_id)

    connection = get_db()

    row = connection.execute(
        """
        SELECT *
        FROM users
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    connection.close()

    return row


def update_user(user_id, **fields):

    ensure_user(user_id)

    if not fields:
        return

    connection = get_db()

    assignments = ", ".join(
        f"{key}=?"
        for key in fields
    )

    values = list(fields.values())
    values.append(user_id)

    connection.execute(
        f"""
        UPDATE users
        SET {assignments}
        WHERE user_id=?
        """,
        values
    )

    connection.commit()
    connection.close()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def clean_text(text):

    if not text:
        return ""

    text = re.sub(
        r"https?://\S+",
        "",
        text
    )

    text = re.sub(
        r"\[[^\]]+\]\([^)]*\)",
        "",
        text
    )

    text = re.sub(
        r"(?im)^\s*(источники?|sources?)\s*:.*$",
        "",
        text
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


def parse_json(text):

    if not text:
        raise ValueError(
            "AI returned empty response"
        )

    text = text.strip()

    text = re.sub(
        r"^```json\s*",
        "",
        text,
        flags=re.I
    )

    text = re.sub(
        r"^```\s*",
        "",
        text
    )

    text = re.sub(
        r"\s*```$",
        "",
        text
    )

    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end > start:

        return json.loads(
            text[start:end + 1]
        )

    raise ValueError(
        "Не удалось разобрать JSON:\n"
        + text[:1000]
    )


def normalize_price(value):

    if value is None:
        return None

    value = str(value)

    value = re.sub(
        r"[^\d,.]",
        "",
        value
    )

    if not value:
        return None

    value = value.replace(
        ",",
        "."
    )

    try:

        number = float(value)

        if number.is_integer():
            return str(int(number))

        return str(number)

    except Exception:

        return None


def parse_price_and_stock(text):

    text_lower = (
        text
        .lower()
        .replace("\xa0", " ")
    )

    price = None
    stock = None

    price_patterns = [

        r"(?:цена|стоимость)"
        r"\s*[:\-]?\s*"
        r"(\d[\d\s]*(?:[.,]\d+)?)",

        r"(\d[\d\s]*(?:[.,]\d+)?)"
        r"\s*(?:₽|руб(?:\.|лей)?)",
    ]

    for pattern in price_patterns:

        match = re.search(
            pattern,
            text_lower
        )

        if match:

            price = normalize_price(
                match.group(1)
            )

            break

    if price is None:

        numbers = re.findall(
            r"\d[\d\s]*",
            text_lower
        )

        if numbers:

            candidate = numbers[0]

            if len(
                re.sub(
                    r"\D",
                    "",
                    candidate
                )
            ) >= 3:

                price = normalize_price(
                    candidate
                )

    stock_patterns = [

        r"(?:в\s*наличии|наличие|остаток)"
        r"\s*[:\-]?\s*(\d+)",

        r"(\d+)\s*(?:шт|штук)\b",
    ]

    for pattern in stock_patterns:

        match = re.search(
            pattern,
            text_lower
        )

        if match:

            stock = match.group(1)

            break

    return price, stock


def normalize_hashtag(category):

    if not category:
        return "#косметика"

    category = str(category).strip().lower()

    category = (
        category
        .replace("#", "")
        .replace(" ", "_")
        .replace("-", "_")
    )

    return "#" + category


# ============================================================
# ОСНОВНОЕ НОВОЕ РАСПОЗНАВАНИЕ
# ============================================================

async def recognize_from_photo(
    image_paths,
    user_hint=""
):

    """
    Первый этап.

    Здесь мы НЕ ищем товар в интернете.

    Мы сначала заставляем AI:
    - рассмотреть фотографию;
    - прочитать надписи;
    - определить бренд;
    - определить название;
    - определить тип товара.
    """

    if not image_paths:

        return {
            "brand": "",
            "product_name": "",
            "category": "",
            "volume": "",
            "shade": "",
            "visible_text": "",
            "confidence": "low",
            "alternatives": []
        }

    prompt = f"""
Ты — специалист по распознаванию косметики,
парфюмерии и средств ухода по фотографии.

ОЧЕНЬ ВАЖНО:

Сейчас НЕ нужно писать рекламный текст.

Твоя единственная задача —
МАКСИМАЛЬНО ТОЧНО понять,
что изображено на фотографии.

Подсказка владельца:
{user_hint or "нет"}

Внимательно изучи:

1. Все надписи на самом товаре.
2. Все надписи на коробке.
3. Логотип бренда.
4. Название продукта.
5. Цифры и объём.
6. Оттенок.
7. Форму упаковки.
8. Характерные элементы дизайна.

Если текст виден —
ОПИРАЙСЯ ПРЕЖДЕ ВСЕГО НА ТЕКСТ.

НЕ определяй бренд только по цвету упаковки.

Если есть сомнение,
укажи несколько возможных вариантов.

Верни ТОЛЬКО JSON:

{{
    "brand": "",
    "product_name": "",
    "category": "",
    "volume": "",
    "shade": "",
    "visible_text": "",
    "confidence": "high",
    "alternatives": []
}}

confidence должен быть:

high
medium
low

Категория должна быть максимально конкретной.

Например:

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
карандаш
хайлайтер
крем
сыворотка
маска
шампунь
кондиционер
палетка
набор

Если точное название неизвестно,
не выдумывай его.
"""

    content = [
        {
            "type": "input_text",
            "text": prompt
        }
    ]

    for path in image_paths[:2]:

        data = Path(path).read_bytes()

        mime_type = (
            mimetypes.guess_type(path)[0]
            or "image/jpeg"
        )

        encoded = base64.b64encode(
            data
        ).decode("utf-8")

        content.append(
            {
                "type": "input_image",
                "image_url":
                    f"data:{mime_type};base64,{encoded}"
            }
        )

    response = await client.responses.create(

        model=MODEL,

        input=[
            {
                "role": "user",
                "content": content
            }
        ]
    )

    result = parse_json(
        response.output_text
    )

    return result


# ============================================================
# ПОВТОРНОЕ РАСПОЗНАВАНИЕ
# ============================================================

async def second_recognition(
    image_paths,
    previous_result
):

    prompt = f"""
Повторно рассмотри фотографию косметики
или парфюмерии.

Первичная попытка распознавания:

{json.dumps(
    previous_result,
    ensure_ascii=False,
    indent=2
)}

Сейчас нужно проверить её.

Особенно внимательно прочитай:

- название бренда;
- название продукта;
- текст на коробке;
- текст на флаконе;
- номер оттенка;
- объём;
- название линейки.

Если первичная версия ошибочна —
исправь её.

НЕ угадывай по общему цвету.

Верни ТОЛЬКО JSON:

{{
    "brand": "",
    "product_name": "",
    "category": "",
    "volume": "",
    "shade": "",
    "visible_text": "",
    "confidence": "high|medium|low",
    "alternatives": []
}}
"""

    content = [
        {
            "type": "input_text",
            "text": prompt
        }
    ]

    for path in image_paths[:2]:

        data = Path(path).read_bytes()

        mime_type = (
            mimetypes.guess_type(path)[0]
            or "image/jpeg"
        )

        encoded = base64.b64encode(
            data
        ).decode("utf-8")

        content.append(
            {
                "type": "input_image",
                "image_url":
                    f"data:{mime_type};base64,{encoded}"
            }
        )

    response = await client.responses.create(

        model=MODEL,

        input=[
            {
                "role": "user",
                "content": content
            }
        ]
    )

    return parse_json(
        response.output_text
    )


# ============================================================
# ПРОВЕРКА ЧЕРЕЗ ИНТЕРНЕТ
# ============================================================

async def verify_product(
    recognized,
    user_hint="",
    price=None,
    stock=None
):

    """
    Второй этап.

    Только после распознавания
    идём искать товар в интернете.
    """

    brand = recognized.get(
        "brand",
        ""
    )

    product_name = recognized.get(
        "product_name",
        ""
    )

    category = recognized.get(
        "category",
        ""
    )

    visible_text = recognized.get(
        "visible_text",
        ""
    )

    alternatives = recognized.get(
        "alternatives",
        []
    )

    prompt = f"""
Ты проверяешь товар для магазина
оригинальной косметики и парфюмерии.

ФОТОГРАФИЯ УЖЕ БЫЛА РАСПОЗНАНА.

Результат распознавания:

Бренд:
{brand}

Название:
{product_name}

Категория:
{category}

Текст на упаковке:
{visible_text}

Другие варианты:
{alternatives}

Подсказка владельца:
{user_hint or "нет"}

Теперь используй WEB SEARCH,
чтобы проверить, существует ли именно такой товар.

Ищи:

1. официальный сайт бренда;
2. официальный магазин;
3. надёжные магазины косметики;
4. только потом другие источники.

ОЧЕНЬ ВАЖНО:

Не заменяй найденный товар похожим товаром.

Если есть несколько похожих товаров,
сопоставь:

- название;
- бренд;
- упаковку;
- оттенок;
- объём.

Цена магазина:
{price or "не указана"}

Количество:
{stock or "не указано"}

Цена и количество НЕ должны заменяться
информацией из интернета.

Нужно вернуть:

{{
    "name": "",
    "brand": "",
    "category_hashtag": "",
    "item_type": "single",
    "description": "",
    "volume": "",
    "shade": "",
    "product_image_url": "",
    "packaging_image_url": "",
    "confidence": "high|medium|low",
    "verification_note": ""
}}

Если несколько товаров на одной фотографии
являются одним готовым набором,
item_type = "set".

Если это обычный один товар,
item_type = "single".

description:
2–4 коротких предложения
на русском языке.

НЕ вставляй ссылки в description.

category_hashtag должен выглядеть так:

#парфюм
#тональный_крем
#румяна
#бронзер
#пудра
#тени
#тушь
#помада
#блеск_для_губ
#хайлайтер
#консилер
#крем
#сыворотка
#маска
#шампунь
#кондиционер
#палетка
#набор
"""

    response = await client.responses.create(

        model=MODEL,

        tools=[
            {
                "type": "web_search",
                "search_context_size": "high"
            }
        ],

        input=prompt
    )

    result = parse_json(
        response.output_text
    )

    # Цена и остаток всегда принадлежат пользователю.
    result["price"] = price
    result["stock"] = stock

    return result


# ============================================================
# ПОЛНЫЙ АНАЛИЗ ТОВАРА
# ============================================================

async def analyze_product(
    image_paths,
    name_hint,
    price,
    stock,
    item_type_hint=None
):

    # ---------- ПЕРВАЯ ПОПЫТКА ----------

    recognized = await recognize_from_photo(
        image_paths,
        name_hint
    )

    logging.info(
        "FIRST RECOGNITION: %s",
        recognized
    )

    # ---------- ЕСЛИ НЕ УВЕРЕН — ВТОРАЯ ----------

    confidence = str(
        recognized.get(
            "confidence",
            "low"
        )
    ).lower()

    product_name = (
        recognized.get(
            "product_name",
            ""
        )
        or ""
    ).strip()

    if (
        confidence == "low"
        or not product_name
        or product_name.lower()
        in {
            "unknown",
            "неизвестно",
            "товар",
            "косметика"
        }
    ):

        logging.info(
            "Running second recognition attempt..."
        )

        second = await second_recognition(
            image_paths,
            recognized
        )

        if second.get("product_name"):

            recognized = second

    # ---------- ПРОВЕРКА В ИНТЕРНЕТЕ ----------

    verified = await verify_product(
        recognized=recognized,
        user_hint=name_hint,
        price=price,
        stock=stock
    )

    if item_type_hint:
        verified["item_type"] = (
            item_type_hint
        )

    logging.info(
        "FINAL PRODUCT: %s",
        verified
    )

    return verified


# ============================================================
# СКАЧИВАНИЕ ФОТО
# ============================================================

async def download_image(url):

    if not url:
        return None

    if not str(url).startswith(
        ("http://", "https://")
    ):
        return None

    try:

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20,
            headers={
                "User-Agent":
                    "Mozilla/5.0"
            }
        ) as http:

            response = await http.get(
                url
            )

            if response.status_code != 200:
                return None

            content_type = (
                response.headers
                .get(
                    "content-type",
                    ""
                )
                .lower()
            )

            if not content_type.startswith(
                "image/"
            ):
                return None

            extension = ".jpg"

            if "png" in content_type:
                extension = ".png"

            elif "webp" in content_type:
                extension = ".webp"

            path = (
                Path("/tmp")
                / f"beauty_{abs(hash(url))}{extension}"
            )

            path.write_bytes(
                response.content
            )

            return str(path)

    except Exception:

        logging.exception(
            "Image download error"
        )

        return None


async def upload_image_to_telegram(
    message,
    path
):

    sent = await message.reply_photo(
        photo=path
    )

    file_id = (
        sent.photo[-1].file_id
    )

    try:
        await sent.delete()
    except Exception:
        pass

    return file_id


# ============================================================
# КАРТОЧКА ТОВАРА
# ============================================================

def create_product_caption(
    data,
    price,
    stock
):

    hashtag = normalize_hashtag(
        data.get(
            "category_hashtag"
        )
    )

    name = data.get(
        "name",
        "Товар"
    )

    description = clean_text(
        data.get(
            "description",
            ""
        )
    )

    lines = [

        hashtag,

        "",

        f"✨ {name}",

        "",

        description
    ]

    volume = data.get(
        "volume"
    )

    if volume:
        lines.extend(
            [
                "",
                f"📏 Объём: {volume}"
            ]
        )

    shade = data.get(
        "shade"
    )

    if shade:
        lines.append(
            f"🎨 Оттенок: {shade}"
        )

    if price:

        lines.extend(
            [
                "",
                f"💰 Цена: {price} ₽"
            ]
        )

    if stock:

        lines.append(
            f"📦 В наличии: {stock} шт."
        )

    return clean_text(
        "\n".join(lines)
    )[:1000]


# ============================================================
# КНОПКИ
# ============================================================

def approval_keyboard(
    product_id
):

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Одобрить",
                    callback_data=
                        f"approve:{product_id}"
                ),

                InlineKeyboardButton(
                    "✏️ Изменить",
                    callback_data=
                        f"edit:{product_id}"
                )
            ],

            [
                InlineKeyboardButton(
                    "❌ Отмена",
                    callback_data=
                        f"cancel:{product_id}"
                )
            ]
        ]
    )


# ============================================================
# НОВЫЙ ТОВАР
# ============================================================

async def start_new_product(
    update,
    context
):

    context.user_data.clear()

    context.user_data["mode"] = (
        "product"
    )

    context.user_data["photos"] = []

    await update.message.reply_text(
        "📸 Пришли фото товара.\n\n"

        "Лучше всего отправить:\n"

        "1️⃣ первое фото — сам товар;\n"
        "2️⃣ второе фото — товар + упаковка.\n\n"

        "Если есть только одна фотография — "
        "ничего страшного.\n"

        "Я попробую найти вторую фотографию "
        "самостоятельно."
    )


# ============================================================
# ПОЛУЧЕНИЕ ФОТО
# ============================================================

async def photo_handler(
    update,
    context
):

    if context.user_data.get(
        "mode"
    ) != "product":

        await update.message.reply_text(
            "Сначала нажми «➕ Новый товар»."
        )

        return

    photo = (
        update
        .message
        .photo[-1]
        .file_id
    )

    photos = context.user_data.setdefault(
        "photos",
        []
    )

    if len(photos) < 2:

        photos.append(
            photo
        )

    if len(photos) == 1:

        await update.message.reply_text(
            "Фото №1 получила 💗\n\n"

            "Если есть фото товара "
            "с упаковкой — пришли его сейчас.\n\n"

            "Если второго фото нет — "
            "напиши название товара "
            "или сразу цену."
        )

    else:

        await update.message.reply_text(
            "Отлично! Получила оба фото 💗\n\n"

            "Теперь напиши цену и количество.\n\n"

            "Например:\n"
            "7500 ₽, 2 шт."
        )


# ============================================================
# СОЗДАНИЕ ТОВАРА
# ============================================================

async def process_product(
    update,
    context
):

    user_id = (
        update
        .effective_user
        .id
    )

    photos = context.user_data.get(
        "photos",
        []
    )

    price = context.user_data.get(
        "price"
    )

    stock = context.user_data.get(
        "stock"
    )

    name_hint = context.user_data.get(
        "name_hint",
        ""
    )

    item_type_hint = context.user_data.get(
        "item_type_hint"
    )

    if not photos:

        await update.message.reply_text(
            "Я не получила фото 😔\n\n"
            "Пришли фотографию товара ещё раз."
        )

        return

    image_paths = []

    for index, file_id in enumerate(
        photos[:2]
    ):

        try:

            telegram_file = (
                await context
                .bot
                .get_file(file_id)
            )

            path = (
                f"/tmp/"
                f"product_{user_id}_{index}.jpg"
            )

            await telegram_file.download_to_drive(
                path
            )

            image_paths.append(
                path
            )

        except Exception:

            logging.exception(
                "Cannot download Telegram photo"
            )

    if not image_paths:

        await update.message.reply_text(
            "Не получилось прочитать фотографию 😔\n"
            "Попробуй отправить её ещё раз."
        )

        return

    await update.message.reply_text(
        "🔎 Сейчас я:\n\n"
        "1. читаю надписи на фотографии;\n"
        "2. определяю бренд и товар;\n"
        "3. проверяю его через интернет;\n"
        "4. определяю категорию;\n"
        "5. собираю готовую карточку.\n\n"
        "Дай мне несколько секунд 💗"
    )

    try:

        data = await analyze_product(
            image_paths=image_paths,
            name_hint=name_hint,
            price=price,
            stock=stock,
            item_type_hint=item_type_hint
        )

    except Exception as error:

        logging.exception(
            "PRODUCT ANALYSIS FAILED"
        )

        await update.message.reply_text(
            "😔 Я не смогла надёжно "
            "распознать товар.\n\n"

            "Попробуй написать его название "
            "текстом прямо следующим сообщением.\n\n"

            "Например:\n"
            "Rare Beauty Soft Pinch Liquid Blush Happy\n\n"

            "Я возьму название как подсказку "
            "и проверю товар через интернет."
        )

        context.user_data["waiting_for_name"] = True

        return

    # ========================================================
    # ФОТО №1
    # ========================================================

    photo1 = (
        photos[0]
        if photos
        else None
    )

    # ========================================================
    # ФОТО №2
    # ========================================================

    photo2 = None

    if len(photos) >= 2:

        photo2 = photos[1]

    else:

        packaging_url = data.get(
            "packaging_image_url"
        )

        downloaded = await download_image(
            packaging_url
        )

        if downloaded:

            try:

                photo2 = (
                    await upload_image_to_telegram(
                        update.message,
                        downloaded
                    )
                )

            except Exception:

                logging.exception(
                    "Could not upload packaging image"
                )

    # ========================================================
    # ЕСЛИ ПЕРВОГО ФОТО НЕТ
    # ========================================================

    if not photo1:

        product_url = data.get(
            "product_image_url"
        )

        downloaded = await download_image(
            product_url
        )

        if downloaded:

            photo1 = (
                await upload_image_to_telegram(
                    update.message,
                    downloaded
                )
            )

    if not photo1:

        await update.message.reply_text(
            "Товар я распознала, "
            "но фотографию не смогла получить.\n\n"
            "Пришли фото ещё раз."
        )

        return

    # ========================================================
    # СОЗДАЁМ КАРТОЧКУ
    # ========================================================

    caption = create_product_caption(
        data,
        price,
        stock
    )

    connection = get_db()

    cursor = connection.execute(
        """
        INSERT INTO products(

            user_id,
            name,
            brand,
            category,
            item_type,
            price,
            stock,
            volume,
            shade,
            description,
            photo1,
            photo2,
            created_at

        )

        VALUES(
            ?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,

        (

            user_id,

            data.get(
                "name",
                "Товар"
            ),

            data.get(
                "brand",
                ""
            ),

            normalize_hashtag(
                data.get(
                    "category_hashtag"
                )
            ),

            data.get(
                "item_type",
                "single"
            ),

            price,

            stock,

            data.get(
                "volume",
                ""
            ),

            data.get(
                "shade",
                ""
            ),

            data.get(
                "description",
                ""
            ),

            photo1,

            photo2,

            datetime.utcnow().isoformat()
        )
    )

    product_id = cursor.lastrowid

    connection.commit()
    connection.close()

    context.user_data.clear()

    # ========================================================
    # ОТПРАВЛЯЕМ ДВЕ ФОТОГРАФИИ
    # ========================================================

    if photo2:

        await update.message.reply_media_group(
            [
                InputMediaPhoto(
                    media=photo1,
                    caption=caption
                ),

                InputMediaPhoto(
                    media=photo2
                )
            ]
        )

        await update.message.reply_text(
            "✨ Карточка готова!\n\n"
            "Проверь товар и нажми кнопку:",
            reply_markup=
                approval_keyboard(
                    product_id
                )
        )

    else:

        await update.message.reply_photo(
            photo=photo1,
            caption=caption,
            reply_markup=
                approval_keyboard(
                    product_id
                )
        )


# ============================================================
# РАСПИСАНИЕ
# ============================================================

def get_next_slot(
    user_id
):

    user = get_user(
        user_id
    )

    timezone = ZoneInfo(
        user["timezone"]
        or DEFAULT_TIMEZONE
    )

    days = json.loads(
        user["days"]
    )

    times = json.loads(
        user["times"]
    )

    now = datetime.now(
        timezone
    ).replace(
        second=0,
        microsecond=0
    )

    connection = get_db()

    occupied_rows = connection.execute(
        """
        SELECT scheduled_at
        FROM queue

        WHERE
            user_id=?
            AND status='queued'
        """,
        (user_id,)
    ).fetchall()

    connection.close()

    occupied = {
        row["scheduled_at"]
        for row in occupied_rows
    }

    for offset in range(90):

        date = (
            now.date()
            + timedelta(
                days=offset
            )
        )

        if date.weekday() not in days:
            continue

        for time_string in sorted(
            times
        ):

            hour, minute = map(
                int,
                time_string.split(":")
            )

            candidate = datetime(
                date.year,
                date.month,
                date.day,
                hour,
                minute,
                tzinfo=timezone
            )

            iso = candidate.isoformat()

            if (
                candidate > now
                and iso not in occupied
            ):

                return iso

    return (
        now
        + timedelta(
            days=1
        )
    ).isoformat()


def format_schedule_time(
    iso,
    user_id
):

    timezone = ZoneInfo(
        get_user(user_id)[
            "timezone"
        ]
        or DEFAULT_TIMEZONE
    )

    dt = datetime.fromisoformat(
        iso
    )

    if dt.tzinfo is None:

        dt = dt.replace(
            tzinfo=timezone
        )

    return dt.astimezone(
        timezone
    ).strftime(
        "%d.%m.%Y в %H:%M"
    )


def add_to_queue(
    user_id,
    kind,
    caption,
    photo1=None,
    photo2=None
):

    scheduled_at = get_next_slot(
        user_id
    )

    connection = get_db()

    cursor = connection.execute(
        """
        INSERT INTO queue(

            user_id,
            kind,
            status,
            scheduled_at,
            caption,
            photo1,
            photo2,
            created_at

        )

        VALUES(
            ?,?,?,?,?,?,?,?
        )
        """,

        (

            user_id,

            kind,

            "queued",

            scheduled_at,

            caption,

            photo1,

            photo2,

            datetime.utcnow().isoformat()
        )
    )

    queue_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return (
        queue_id,
        scheduled_at
    )


# ============================================================
# ПУБЛИКАЦИЯ
# ============================================================

async def publish_queue_item(
    row
):

    user = get_user(
        row["user_id"]
    )

    channel_id = user[
        "channel_id"
    ]

    if not channel_id:

        raise RuntimeError(
            "Telegram-канал не подключён."
        )

    if (
        row["kind"] == "product"
        and row["photo1"]
    ):

        media = [
            InputMediaPhoto(
                media=row["photo1"],
                caption=row["caption"]
            )
        ]

        if row["photo2"]:

            media.append(
                InputMediaPhoto(
                    media=row["photo2"]
                )
            )

        await telegram_app.bot.send_media_group(
            chat_id=channel_id,
            media=media
        )

    else:

        await telegram_app.bot.send_message(
            chat_id=channel_id,
            text=row["caption"]
        )


async def scheduler():

    while True:

        try:

            connection = get_db()

            rows = connection.execute(
                """
                SELECT
                    queue.*,
                    users.paused,
                    users.channel_id

                FROM queue

                JOIN users
                    ON users.user_id =
                       queue.user_id

                WHERE queue.status='queued'

                ORDER BY queue.scheduled_at

                LIMIT 50
                """
            ).fetchall()

            connection.close()

            now = datetime.now(
                ZoneInfo("UTC")
            )

            for row in rows:

                if row["paused"]:
                    continue

                if not row["channel_id"]:
                    continue

                scheduled = datetime.fromisoformat(
                    row["scheduled_at"]
                )

                if scheduled.tzinfo is None:

                    scheduled = scheduled.replace(
                        tzinfo=ZoneInfo(
                            get_user(
                                row["user_id"]
                            )["timezone"]
                        )
                    )

                if (
                    scheduled.astimezone(
                        ZoneInfo("UTC")
                    )
                    <= now
                ):

                    try:

                        await publish_queue_item(
                            row
                        )

                        connection = get_db()

                        connection.execute(
                            """
                            UPDATE queue

                            SET status='published'

                            WHERE id=?
                            """,
                            (row["id"],)
                        )

                        connection.commit()
                        connection.close()

                        await telegram_app.bot.send_message(

                            row["user_id"],

                            "📢 Пост опубликован!\n\n"
                            f"Номер публикации: "
                            f"#{row['id']}"
                        )

                    except Exception as error:

                        logging.exception(
                            "Publishing error"
                        )

                        connection = get_db()

                        connection.execute(
                            """
                            UPDATE queue

                            SET status='error'

                            WHERE id=?
                            """,
                            (row["id"],)
                        )

                        connection.commit()
                        connection.close()

                        await telegram_app.bot.send_message(

                            row["user_id"],

                            "⚠️ Не удалось "
                            "опубликовать пост.\n\n"
                            f"Ошибка: {error}"
                        )

        except Exception:

            logging.exception(
                "Scheduler error"
            )

        await asyncio.sleep(
            30
        )


# ============================================================
# ПОСТЫ
# ============================================================

async def generate_post(
    topic
):

    response = await client.responses.create(

        model=MODEL,

        input=f"""
Напиши готовый пост для Telegram-канала
оригинальной косметики и парфюмерии.

Тема:
{topic}

Стиль:

- живой;
- женственный;
- современный;
- дружелюбный;
- короткий;
- без канцелярита;
- без агрессивных продаж.

Не выдумывай личный опыт автора.

Не добавляй ссылки.

Не добавляй источники.

Не перегружай эмодзи.
"""
    )

    return clean_text(
        response.output_text
    )


async def generate_ideas():

    response = await client.responses.create(

        model=MODEL,

        input="""
Придумай 10 интересных идей
для Telegram-канала
оригинальной косметики и парфюмерии.

Баланс:

70% полезное/интересное
20% вовлечение
10% продажи.

Без лица автора.

Идеи должны быть конкретными,
сохраняемыми и пересылаемыми.
"""
    )

    return clean_text(
        response.output_text
    )


async def generate_plan():

    response = await client.responses.create(

        model=MODEL,

        input="""
Создай недельный контент-план
для Telegram-канала оригинальной
косметики и парфюмерии.

На каждый день:

- полезный контент;
- вовлечение;
- товарные посты;
- идеи для коротких сообщений.

Баланс:

70% полезное
20% вовлечение
10% продажи.

Без лица автора.
"""
    )

    return clean_text(
        response.output_text
    )


# ============================================================
# КАНАЛ
# ============================================================

async def connect_channel(
    update,
    context
):

    origin = getattr(
        update.message,
        "forward_origin",
        None
    )

    channel = getattr(
        origin,
        "chat",
        None
    )

    if (
        channel
        and getattr(
            channel,
            "type",
            None
        ) == "channel"
    ):

        update_user(

            update
            .effective_user
            .id,

            channel_id=str(
                channel.id
            )
        )

        await update.message.reply_text(

            f"📢 Канал "
            f"«{channel.title}» подключён!\n\n"

            "Теперь убедись, что бот является "
            "администратором канала и имеет "
            "право публиковать сообщения."
        )

    else:

        await update.message.reply_text(

            "📢 Чтобы подключить канал:\n\n"

            "1. Добавь бота администратором "
            "в свой канал.\n\n"

            "2. Перешли сюда любой пост "
            "из этого канала.\n\n"

            "Я сама определю ID канала."
        )


# ============================================================
# ОЧЕРЕДЬ
# ============================================================

async def show_queue(
    update,
    context
):

    user_id = (
        update
        .effective_user
        .id
    )

    connection = get_db()

    rows = connection.execute(
        """
        SELECT *

        FROM queue

        WHERE
            user_id=?
            AND status='queued'

        ORDER BY scheduled_at
        """,
        (user_id,)
    ).fetchall()

    connection.close()

    if not rows:

        await update.message.reply_text(
            "📋 Очередь пока пустая."
        )

        return

    result = [
        "📋 Твоя очередь:\n"
    ]

    for row in rows[:30]:

        title = next(
            (
                line
                for line in
                (row["caption"] or "")
                .splitlines()

                if line.startswith(
                    "✨ "
                )
            ),
            row["kind"]
        )

        result.append(

            f"#{row['id']} — {title}\n"
            f"🕒 "
            f"{format_schedule_time("
                f"row['scheduled_at'], "
                f"user_id"
            )}"
        )

    await update.message.reply_text(
        "\n\n".join(result)
    )


# ============================================================
# РАСПИСАНИЕ
# ============================================================

def schedule_keyboard():

    return InlineKeyboardMarkup(

        [
            [
                InlineKeyboardButton(
                    "📅 Дни",
                    callback_data="days"
                ),

                InlineKeyboardButton(
                    "🕒 Время",
                    callback_data="times"
                )
            ],

            [
                InlineKeyboardButton(
                    "🌍 Часовой пояс",
                    callback_data="timezone"
                ),

                InlineKeyboardButton(
                    "⏸/▶️ Пауза",
                    callback_data="pause"
                )
            ]
        ]
    )


async def show_schedule(
    update,
    context
):

    user_id = (
        update
        .effective_user
        .id
    )

    user = get_user(
        user_id
    )

    days = json.loads(
        user["days"]
    )

    times = json.loads(
        user["times"]
    )

    names = [
        "Пн",
        "Вт",
        "Ср",
        "Чт",
        "Пт",
        "Сб",
        "Вс"
    ]

    text = (

        "⏰ РАСПИСАНИЕ\n\n"

        "📅 Дни: "
        + ", ".join(
            names[d]
            for d in days
        )

        + "\n"

        "🕒 Время: "
        + ", ".join(
            times
        )

        + "\n"

        "🌍 Часовой пояс: "
        + user["timezone"]

        + "\n"

        "Статус: "
        + (
            "⏸ пауза"
            if user["paused"]
            else
            "▶️ активно"
        )

        + "\n\n"

        "После одобрения товара "
        "он автоматически попадёт "
        "в ближайшее свободное время."
    )

    await update.message.reply_text(
        text,
        reply_markup=
            schedule_keyboard()
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    action = query.data

    # --------------------------------------------------------
    # ОДОБРЕНИЕ ТОВАРА
    # --------------------------------------------------------

    if action.startswith(
        "approve:"
    ):

        product_id = int(
            action.split(":")[1]
        )

        connection = get_db()

        product = connection.execute(

            """
            SELECT *

            FROM products

            WHERE
                id=?
                AND user_id=?
            """,

            (
                product_id,
                user_id
            )
        ).fetchone()

        connection.close()

        if not product:

            await query.message.reply_text(
                "Товар уже недоступен."
            )

            return

        caption = (
            product["category"]
            + "\n\n"
            + f"✨ {product['name']}"
            + "\n\n"
            + clean_text(
                product["description"]
                or ""
            )
        )

        if product["volume"]:

            caption += (
                "\n\n"
                f"📏 Объём: "
                f"{product['volume']}"
            )

        if product["shade"]:

            caption += (
                "\n"
                f"🎨 Оттенок: "
                f"{product['shade']}"
            )

        if product["price"]:

            caption += (
                "\n\n"
                f"💰 Цена: "
                f"{product['price']} ₽"
            )

        if product["stock"]:

            caption += (
                "\n"
                f"📦 В наличии: "
                f"{product['stock']} шт."
            )

        caption = clean_text(
            caption
        )[:1000]

        queue_id, scheduled_at = (
            add_to_queue(

                user_id,

                "product",

                caption,

                product["photo1"],

                product["photo2"]
            )
        )

        await query.message.reply_text(

            "✅ Одобрено!\n\n"

            f"📋 В очереди: "
            f"#{queue_id}\n"

            "🕒 Публикация: "
            + format_schedule_time(
                scheduled_at,
                user_id
            )
        )

        return

    # --------------------------------------------------------
    # ОТМЕНА
    # --------------------------------------------------------

    if action.startswith(
        "cancel:"
    ):

        product_id = int(
            action.split(":")[1]
        )

        connection = get_db()

        connection.execute(

            """
            DELETE FROM products

            WHERE
                id=?
                AND user_id=?
            """,

            (
                product_id,
                user_id
            )
        )

        connection.commit()
        connection.close()

        await query.message.reply_text(
            "❌ Товар отменён."
        )

        return

    # --------------------------------------------------------
    # ИЗМЕНЕНИЕ
    # --------------------------------------------------------

    if action.startswith(
        "edit:"
    ):

        product_id = int(
            action.split(":")[1]
        )

        context.user_data[
            "editing_product"
        ] = product_id

        await query.message.reply_text(

            "✏️ Напиши, что нужно изменить.\n\n"

            "Например:\n\n"

            "«Это бронзер»\n"
            "«Это хайлайтер»\n"
            "«Цена 6900»\n"
            "«Оттенок Happy»\n"
            "«Название — ...»"
        )

        return

    # --------------------------------------------------------
    # ДНИ
    # --------------------------------------------------------

    if action == "days":

        context.user_data[
            "schedule_mode"
        ] = "days"

        await query.message.reply_text(

            "📅 Выбери дни цифрами:\n\n"

            "1 — Пн\n"
            "2 — Вт\n"
            "3 — Ср\n"
            "4 — Чт\n"
            "5 — Пт\n"
            "6 — Сб\n"
            "7 — Вс\n\n"

            "Например:\n"
            "1,2,3,4,5"
        )

        return

    # --------------------------------------------------------
    # ВРЕМЯ
    # --------------------------------------------------------

    if action == "times":

        context.user_data[
            "schedule_mode"
        ] = "times"

        await query.message.reply_text(

            "🕒 Напиши время через запятую.\n\n"

            "Например:\n"
            "10:00, 15:00, 20:00"
        )

        return

    # --------------------------------------------------------
    # ЧАСОВОЙ ПОЯС
    # --------------------------------------------------------

    if action == "timezone":

        context.user_data[
            "schedule_mode"
        ] = "timezone"

        await query.message.reply_text(

            "🌍 Напиши часовой пояс.\n\n"

            "Например:\n"
            "Europe/Moscow\n\n"
            "или:\n"
            "Europe/Warsaw"
        )

        return

    # --------------------------------------------------------
    # ПАУЗА
    # --------------------------------------------------------

    if action == "pause":

        user = get_user(
            user_id
        )

        new_value = (
            0
            if user["paused"]
            else 1
        )

        update_user(
            user_id,
            paused=new_value
        )

        await query.message.reply_text(

            "⏸ Расписание поставлено "
            "на паузу."
            if new_value

            else

            "▶️ Расписание снова "
            "активно."
        )

        return


# ============================================================
# ТЕКСТОВЫЕ СООБЩЕНИЯ
# ============================================================

async def text_handler(
    update,
    context
):

    user_id = (
        update
        .effective_user
        .id
    )

    text = (
        update
        .message
        .text
        .strip()
    )

    # ========================================================
    # КНОПКИ
    # ========================================================

    if text == "➕ Новый товар":

        return await start_new_product(
            update,
            context
        )

    if text == "📝 Новый пост":

        context.user_data[
            "mode"
        ] = "post"

        await update.message.reply_text(
            "Напиши тему поста."
        )

        return

    if text == "📅 Контент-план":

        await update.message.reply_text(
            await generate_plan()
        )

        return

    if text == "⏰ Расписание":

        return await show_schedule(
            update,
            context
        )

    if text == "📋 Очередь":

        return await show_queue(
            update,
            context
        )

    if text == "📢 Канал":

        return await connect_channel(
            update,
            context
        )

    if text == "💡 Идеи":

        await update.message.reply_text(
            await generate_ideas()
        )

        return

    # ========================================================
    # РЕДАКТИРОВАНИЕ ТОВАРА
    # ========================================================

    editing_product = (
        context.user_data.get(
            "editing_product"
        )
    )

    if editing_product:

        connection = get_db()

        product = connection.execute(

            """
            SELECT *

            FROM products

            WHERE
                id=?
                AND user_id=?
            """,

            (
                editing_product,
                user_id
            )
        ).fetchone()

        connection.close()

        if not product:

            await update.message.reply_text(
                "Товар не найден."
            )

            return

        context.user_data.clear()

        context.user_data[
            "mode"
        ] = "product"

        context.user_data[
            "photos"
        ] = [
            product["photo1"]
        ]

        if product["photo2"]:

            context.user_data[
                "photos"
            ].append(
                product["photo2"]
            )

        context.user_data[
            "price"
        ] = product["price"]

        context.user_data[
            "stock"
        ] = product["stock"]

        context.user_data[
            "name_hint"
        ] = text

        await process_product(
            update,
            context
        )

        return

    # ========================================================
    # РАСПИСАНИЕ
    # ========================================================

    schedule_mode = (
        context.user_data.get(
            "schedule_mode"
        )
    )

    if schedule_mode == "days":

        try:

            values = sorted(
                set(
                    int(x.strip()) - 1
                    for x in text.split(",")
                )
            )

            if (
                not values
                or any(
                    value < 0
                    or value > 6
                    for value in values
                )
            ):

                raise ValueError

            update_user(
                user_id,
                days=json.dumps(
                    values
                )
            )

            context.user_data.pop(
                "schedule_mode",
                None
            )

            await update.message.reply_text(
                "✅ Дни сохранены.",
                reply_markup=
                    MAIN_MENU
            )

        except Exception:

            await update.message.reply_text(
                "Напиши, например:\n"
                "1,2,3,4,5"
            )

        return

    if schedule_mode == "times":

        try:

            times = []

            for item in text.split(","):

                hour, minute = map(
                    int,
                    item.strip().split(":")
                )

                if not (
                    0 <= hour <= 23
                    and
                    0 <= minute <= 59
                ):

                    raise ValueError

                times.append(
                    f"{hour:02d}:{minute:02d}"
                )

            update_user(
                user_id,
                times=json.dumps(
                    sorted(
                        set(times)
                    )
                )
            )

            context.user_data.pop(
                "schedule_mode",
                None
            )

            await update.message.reply_text(
                "✅ Время сохранено.",
                reply_markup=
                    MAIN_MENU
            )

        except Exception:

            await update.message.reply_text(
                "Напиши, например:\n"
                "10:00, 15:00, 20:00"
            )

        return

    if schedule_mode == "timezone":

        try:

            ZoneInfo(
                text
            )

            update_user(
                user_id,
                timezone=text
            )

            context.user_data.pop(
                "schedule_mode",
                None
            )

            await update.message.reply_text(
                "✅ Часовой пояс сохранён.",
                reply_markup=
                    MAIN_MENU
            )

        except Exception:

            await update.message.reply_text(
                "Например:\n"
                "Europe/Moscow\n\n"
                "или:\n"
                "Europe/Warsaw"
            )

        return

    # ========================================================
    # ТОВАР
    # ========================================================

    if context.user_data.get(
        "mode"
    ) == "product":

        price, stock = (
            parse_price_and_stock(
                text
            )
        )

        if price:

            context.user_data[
                "price"
            ] = price

            if stock:

                context.user_data[
                    "stock"
                ] = stock

            # Если пользователь одновременно
            # написал название:
            remaining = re.sub(

                r"(?:цена|стоимость)"
                r"\s*[:\-]?\s*"
                r"\d[\d\s]*(?:[.,]\d+)?"
                r"\s*(?:₽|руб(?:\.|лей)?)?",

                "",

                text,

                flags=re.I
            )

            remaining = re.sub(

                r"(?:в\s*наличии|наличие|остаток)"
                r"\s*[:\-]?\s*\d+"
                r"\s*(?:шт|штук)?",

                "",

                remaining,

                flags=re.I
            )

            remaining = re.sub(

                r"\d+\s*(?:шт|штук)",

                "",

                remaining,

                flags=re.I
            )

            remaining = remaining.strip(
                " ,.-"
            )

            if remaining:

                context.user_data[
                    "name_hint"
                ] = remaining

            return await process_product(
                update,
                context
            )

        else:

            context.user_data[
                "name_hint"
            ] = text

            await update.message.reply_text(

                "Название получила 💗\n\n"

                "Теперь напиши цену и количество.\n\n"

                "Например:\n"
                "7500 ₽, 2 шт."
            )

            return

    # ========================================================
    # ПОСТ
    # ========================================================

    if context.user_data.get(
        "mode"
    ) == "post":

        draft = await generate_post(
            text
        )

        context.user_data[
            "draft"
        ] = draft

        context.user_data.pop(
            "mode",
            None
        )

        keyboard = InlineKeyboardMarkup(

            [
                [
                    InlineKeyboardButton(
                        "✅ В очередь",
                        callback_data=
                            "post_approve"
                    ),

                    InlineKeyboardButton(
                        "❌ Отмена",
                        callback_data=
                            "post_cancel"
                    )
                ]
            ]
        )

        await update.message.reply_text(
            draft,
            reply_markup=keyboard
        )

        return

    # ========================================================
    # НЕИЗВЕСТНАЯ КОМАНДА
    # ========================================================

    await update.message.reply_text(

        "Выбери действие 👇",

        reply_markup=
            MAIN_MENU
    )


# ============================================================
# CALLBACK ДЛЯ ПОСТОВ
# ============================================================

async def post_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    if query.data == "post_approve":

        draft = context.user_data.get(
            "draft"
        )

        if not draft:

            await query.message.reply_text(
                "Черновик уже недоступен."
            )

            return

        queue_id, scheduled_at = (
            add_to_queue(
                user_id,
                "post",
                draft
            )
        )

        context.user_data.clear()

        await query.message.reply_text(

            "✅ Пост добавлен в очередь!\n\n"

            f"📋 #{queue_id}\n"

            "🕒 "
            + format_schedule_time(
                scheduled_at,
                user_id
            )
        )

    elif query.data == "post_cancel":

        context.user_data.pop(
            "draft",
            None
        )

        await query.message.reply_text(
            "❌ Пост отменён."
        )


# ============================================================
# START
# ============================================================

async def start(
    update,
    context
):

    ensure_user(
        update
        .effective_user
        .id
    )

    await update.message.reply_text(

        "Привет! 💗\n\n"

        "Я твой бьюти-менеджер.\n\n"

        "Теперь при добавлении товара я работаю "
        "по схеме:\n\n"

        "📸 фотография\n"
        "↓\n"
        "👁 распознавание\n"
        "↓\n"
        "🔎 проверка в интернете\n"
        "↓\n"
        "🏷 категория + хэштег\n"
        "↓\n"
        "📝 описание\n"
        "↓\n"
        "📦 готовая карточка\n\n"

        "И самое главное — "
        "без твоего одобрения ничего "
        "не публикую. ❤️",

        reply_markup=
            MAIN_MENU
    )


async def my_id(
    update,
    context
):

    await update.message.reply_text(

        "Твой Telegram ID:\n"
        f"{update.effective_user.id}"
    )


# ============================================================
# FASTAPI STARTUP
# ============================================================

@app.on_event(
    "startup"
)
async def startup():

    global scheduler_task

    init_database()

    await telegram_app.initialize()

    await telegram_app.start()

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "new",
            start_new_product
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "queue",
            show_queue
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "schedule",
            show_schedule
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "channel",
            connect_channel
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "id",
            my_id
        )
    )

    # --------------------------------------------------------
    # ФОТО
    # --------------------------------------------------------

    telegram_app.add_handler(

        MessageHandler(
            filters.PHOTO,
            photo_handler
        )
    )

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    telegram_app.add_handler(

        CallbackQueryHandler(
            post_callback,
            pattern=r"^post_"
        )
    )

    telegram_app.add_handler(

        CallbackQueryHandler(
            callback_handler,
            pattern=r"^(approve|edit|cancel|days|times|timezone|pause):?"
        )
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    telegram_app.add_handler(

        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler
        )
    )

    # --------------------------------------------------------
    # MENU
    # --------------------------------------------------------

    await telegram_app.bot.set_my_commands(

        [
            (
                "start",
                "Запустить бота"
            ),

            (
                "new",
                "Новый товар"
            ),

            (
                "queue",
                "Очередь"
            ),

            (
                "schedule",
                "Расписание"
            ),

            (
                "channel",
                "Подключить канал"
            ),

            (
                "id",
                "Мой ID"
            )
        ]
    )

    await telegram_app.bot.set_chat_menu_button(
        menu_button=
            MenuButtonCommands()
    )

    # --------------------------------------------------------
    # POLLING
    # --------------------------------------------------------

    await telegram_app.updater.start_polling()

    # --------------------------------------------------------
    # SCHEDULER
    # --------------------------------------------------------

    scheduler_task = asyncio.create_task(
        scheduler()
    )

    logging.info(
        "Beauty Manager Bot started successfully."
    )


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event(
    "shutdown"
)
async def shutdown():

    global scheduler_task

    if scheduler_task:

        scheduler_task.cancel()

        try:

            await scheduler_task

        except asyncio.CancelledError:

            pass

    if telegram_app.updater.running:

        await telegram_app.updater.stop()

    if telegram_app.running:

        await telegram_app.stop()

    await telegram_app.shutdown()


# ============================================================
# RENDER HEALTH CHECK
# ============================================================

@app.get("/")
async def health():

    return {
        "ok": True,
        "service":
            "Beauty Manager Bot",
        "recognition":
            "vision + web verification"
    }
