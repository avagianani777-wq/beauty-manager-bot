import os
import re
import json
import base64
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow")

# Если ID канала указан в Render — бот использует его.
# Можно оставить пустым и подключить канал через /channel.
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")

DB_PATH = os.getenv("DB_PATH", "beauty_manager.db")

MAX_CAPTION_LENGTH = 1000

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("beauty_manager")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")


client = AsyncOpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()

telegram_app: Optional[Application] = None

# Состояние текущего диалога.
USER_STATE = {}


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            channel_id TEXT,
            timezone TEXT DEFAULT 'Europe/Moscow',
            schedule_enabled INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel_id TEXT NOT NULL,
            content_type TEXT NOT NULL,
            text TEXT,
            photo1 TEXT,
            photo2 TEXT,
            publish_at TEXT,
            status TEXT DEFAULT 'waiting',
            created_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT,
            category TEXT,
            item_type TEXT,
            price TEXT,
            stock TEXT,
            data_json TEXT,
            photo1 TEXT,
            photo2 TEXT,
            created_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            user_id INTEGER PRIMARY KEY,
            times TEXT,
            days TEXT,
            timezone TEXT
        )
    """)

    conn.commit()
    conn.close()


def ensure_user(user_id: int):
    conn = db()

    existing = conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if not existing:
        conn.execute(
            """
            INSERT INTO users
            (user_id, channel_id, timezone, schedule_enabled, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (
                user_id,
                TELEGRAM_CHANNEL_ID,
                BOT_TIMEZONE,
                datetime.utcnow().isoformat(),
            ),
        )

    conn.commit()
    conn.close()


def get_channel_id(user_id: int):
    ensure_user(user_id)

    conn = db()
    row = conn.execute(
        "SELECT channel_id FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    conn.close()

    if row and row["channel_id"]:
        return row["channel_id"]

    return TELEGRAM_CHANNEL_ID


def set_channel_id(user_id: int, channel_id: str):
    ensure_user(user_id)

    conn = db()
    conn.execute(
        "UPDATE users SET channel_id = ? WHERE user_id = ?",
        (str(channel_id), user_id),
    )
    conn.commit()
    conn.close()


# ============================================================
# CATEGORY
# ============================================================

CATEGORY_HASHTAGS = {
    "парфюм": "#парфюм",
    "тональный крем": "#тональный_крем",
    "крем": "#крем",
    "румяна": "#румяна",
    "бронзер": "#бронзер",
    "пудра": "#пудра",
    "тени": "#тени",
    "тушь": "#тушь",
    "помада": "#помада",
    "блеск для губ": "#блеск_для_губ",
    "карандаш": "#карандаш",
    "хайлайтер": "#хайлайтер",
    "консилер": "#консилер",
    "маска": "#маска",
    "сыворотка": "#сыворотка",
    "шампунь": "#шампунь",
    "кондиционер": "#кондиционер",
    "набор": "#набор",
    "комплект": "#комплект",
}


def category_hashtag(category: str) -> str:
    if not category:
        return "#новинка"

    category = category.lower().strip()

    for key, tag in CATEGORY_HASHTAGS.items():
        if key in category:
            return tag

    return "#новинка"


# ============================================================
# TELEGRAM MENU
# ============================================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Добавить товар", callback_data="add_product"),
            InlineKeyboardButton("📝 Создать пост", callback_data="create_post"),
        ],
        [
            InlineKeyboardButton("📅 Расписание", callback_data="schedule"),
            InlineKeyboardButton("📋 Очередь", callback_data="queue"),
        ],
        [
            InlineKeyboardButton("💡 Контент-план", callback_data="content_plan"),
            InlineKeyboardButton("📊 Идеи", callback_data="ideas"),
        ],
        [
            InlineKeyboardButton("🗳️ Опрос", callback_data="poll"),
            InlineKeyboardButton("📢 Канал", callback_data="channel"),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    await update.message.reply_text(
        "Привет! 💗\n\n"
        "Я твой Beauty Manager.\n\n"
        "Ты можешь просто прислать мне фото товара "
        "и написать цену и количество — остальное я сделаю сама:\n\n"
        "📸 распознаю товар\n"
        "🔎 найду информацию\n"
        "📝 напишу описание\n"
        "🖼️ подготовлю фотографии\n"
        "📦 укажу наличие\n"
        "💰 поставлю твою цену\n"
        "✅ отправлю тебе на утверждение\n\n"
        "Без твоего подтверждения ничего не публикуется.",
        reply_markup=main_menu(),
    )


# ============================================================
# OPENAI HELPERS
# ============================================================

async def ask_openai(
    prompt: str,
    images: Optional[list[str]] = None,
    web_search: bool = False,
) -> str:

    content = [
        {
            "type": "input_text",
            "text": prompt,
        }
    ]

    if images:
        for image_data in images:
            content.append({
                "type": "input_image",
                "image_url": image_data,
            })

    request = {
        "model": MODEL,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
    }

    if web_search:
        request["tools"] = [
            {
                "type": "web_search"
            }
        ]

    response = await client.responses.create(**request)

    return response.output_text.strip()


def clean_json(text: str):
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text)
        text = re.sub(r"```$", "", text)
        text = text.strip()

    return text


async def ask_json(
    prompt: str,
    images: Optional[list[str]] = None,
    web_search: bool = False,
):
    text = await ask_openai(
        prompt,
        images=images,
        web_search=web_search,
    )

    text = clean_json(text)

    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)

        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass

    return {}


# ============================================================
# IMAGE PROCESSING
# ============================================================

async def telegram_photo_to_data_url(bot, file_id: str) -> Optional[str]:
    try:
        telegram_file = await bot.get_file(file_id)

        data = await telegram_file.download_as_bytearray()

        encoded = base64.b64encode(data).decode("utf-8")

        return f"data:image/jpeg;base64,{encoded}"

    except Exception as e:
        logger.exception("Cannot download Telegram image: %s", e)
        return None


# ============================================================
# PRODUCT RESEARCH
# ============================================================

PRODUCT_RESEARCH_PROMPT = """
Ты — профессиональный beauty-редактор и товаровед интернет-магазина
оригинальной косметики и парфюмерии.

Тебе будут переданы фотографии товара и, возможно, подсказка пользователя.

Твоя задача:

1. Определи точное название товара.
2. Определи бренд.
3. Определи категорию.
4. Пойми, является ли это:
   - single — один товар;
   - set — набор;
   - bundle — комплект;
   - multiple_separate — несколько отдельных товаров.
5. Если это набор, считай его ОДНИМ товаром.
6. Найди актуальную информацию о товаре через интернет.
7. В первую очередь используй:
   - официальный сайт бренда;
   - официальный магазин бренда;
   - крупные проверенные магазины.
8. Не выдумывай характеристики.

ОЧЕНЬ ВАЖНО:

Для косметики укажи, если информация подтверждается:
- что это за продукт;
- для чего нужен;
- текстуру;
- финиш;
- покрытие;
- тип кожи;
- проблемы/задачи, для которых подходит;
- основные свойства;
- эффект;
- оттенок;
- объём;
- способ применения.

Для парфюма:
- семейство;
- верхние ноты;
- ноты сердца;
- базовые ноты;
- характер аромата;
- для какого времени/сезона подходит, только если это можно обоснованно определить;
- кому может понравиться.

Для наборов:
- считай весь набор одним товаром;
- перечисли содержимое;
- объясни назначение набора;
- не разбивай его на отдельные товары;
- укажи, подходит ли он для подарка, если это подтверждается.

Описание должно быть информативным, но понятным обычному покупателю.

Не используй рекламную ложь.
Не придумывай свойства.
Если характеристика неизвестна — оставь пустой строкой.

Верни ТОЛЬКО JSON следующего формата:

{
  "name": "",
  "brand": "",
  "category": "",
  "item_type": "single|set|bundle|multiple_separate",

  "description": "",

  "volume": "",
  "shade": "",

  "texture": "",
  "finish": "",
  "coverage": "",
  "skin_type": "",
  "skin_concerns": "",
  "benefits": "",
  "how_to_use": "",

  "fragrance_family": "",
  "fragrance_notes": "",
  "fragrance_character": "",
  "best_for": "",

  "set_contents": "",
  "set_description": "",

  "product_image_url": "",
  "packaging_image_url": ""
}
"""


async def research_product(
    image_urls: list[str],
    user_hint: str = "",
):

    prompt = PRODUCT_RESEARCH_PROMPT

    if user_hint:
        prompt += f"""

Дополнительная информация от продавца:

{user_hint}

Используй её как подсказку, но всё равно проверяй информацию.
"""

    return await ask_json(
        prompt,
        images=image_urls,
        web_search=True,
    )


# ============================================================
# IMAGE URL EXTRACTION
# ============================================================

async def extract_images_from_page(url: str) -> list[str]:
    if not url:
        return []

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/130 Safari/537.36"
            )
        }

        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers=headers,
        ) as client_http:

            response = await client_http.get(url)

        if response.status_code >= 400:
            return []

        soup = BeautifulSoup(response.text, "html.parser")

        result = []

        for meta in soup.find_all("meta"):
            prop = meta.get("property", "")
            name = meta.get("name", "")

            if prop in ("og:image", "og:image:url") or name in (
                "twitter:image",
            ):
                content = meta.get("content")

                if content and content.startswith("http"):
                    result.append(content)

        for img in soup.find_all("img"):
            src = img.get("src")

            if src and src.startswith("http"):
                result.append(src)

        unique = []

        for item in result:
            if item not in unique:
                unique.append(item)

        return unique[:10]

    except Exception:
        return []


async def find_better_product_images(data: dict):
    """
    Если OpenAI нашёл страницу/URL картинки — пытаемся
    получить две реальные картинки.
    """

    product_url = data.get("product_image_url", "")
    packaging_url = data.get("packaging_image_url", "")

    images = []

    if product_url and product_url.startswith("http"):
        if any(x in product_url.lower() for x in [".jpg", ".jpeg", ".png", ".webp"]):
            images.append(product_url)
        else:
            page_images = await extract_images_from_page(product_url)

            if page_images:
                images.append(page_images[0])

    if packaging_url and packaging_url.startswith("http"):
        if any(x in packaging_url.lower() for x in [".jpg", ".jpeg", ".png", ".webp"]):
            images.append(packaging_url)
        else:
            page_images = await extract_images_from_page(packaging_url)

            if page_images:
                images.append(page_images[0])

    unique = []

    for img in images:
        if img not in unique:
            unique.append(img)

    return unique[:2]


# ============================================================
# PRODUCT CAPTION
# ============================================================

def shorten(text: str, limit: int):
    if not text:
        return ""

    text = str(text).strip()

    if len(text) <= limit:
        return text

    return text[:limit - 1].rstrip() + "…"


def build_product_caption(
    data: dict,
    price: str,
    stock: str,
) -> str:

    category = data.get("category", "")
    item_type = data.get("item_type", "single")

    hashtag = category_hashtag(category)

    name = data.get("name", "Товар")
    brand = data.get("brand", "")

    if brand and brand.lower() not in name.lower():
        title = f"{brand} {name}"
    else:
        title = name

    parts = []

    parts.append(hashtag)
    parts.append("")
    parts.append(f"✨ {title}")

    description = data.get("description", "")

    if description:
        parts.append("")
        parts.append(shorten(description, 360))

    # --------------------------------------------------------
    # PERFUME
    # --------------------------------------------------------

    if "парф" in category.lower():

        family = data.get("fragrance_family", "")
        notes = data.get("fragrance_notes", "")
        character = data.get("fragrance_character", "")
        best_for = data.get("best_for", "")

        if family:
            parts.append(f"\n🌸 Семейство: {family}")

        if notes:
            parts.append(f"✨ Ноты: {notes}")

        if character:
            parts.append(f"💫 Характер: {character}")

        if best_for:
            parts.append(f"🕐 Подойдёт: {best_for}")

    # --------------------------------------------------------
    # COSMETICS
    # --------------------------------------------------------

    else:

        texture = data.get("texture", "")
        finish = data.get("finish", "")
        coverage = data.get("coverage", "")
        skin_type = data.get("skin_type", "")
        skin_concerns = data.get("skin_concerns", "")
        benefits = data.get("benefits", "")

        if texture:
            parts.append(f"\n🫧 Текстура: {texture}")

        if finish:
            parts.append(f"✨ Финиш: {finish}")

        if coverage:
            parts.append(f"🎨 Покрытие: {coverage}")

        if skin_type:
            parts.append(f"🌿 Тип кожи: {skin_type}")

        if skin_concerns:
            parts.append(f"💗 Подходит для: {skin_concerns}")

        if benefits:
            parts.append(f"💎 Эффект: {benefits}")

    # --------------------------------------------------------
    # SET
    # --------------------------------------------------------

    if item_type in ("set", "bundle"):

        set_description = data.get("set_description", "")
        set_contents = data.get("set_contents", "")

        if set_description:
            parts.append(f"\n🎁 О наборе: {set_description}")

        if set_contents:
            parts.append(f"📦 В составе: {set_contents}")

    volume = data.get("volume", "")
    shade = data.get("shade", "")

    if volume:
        parts.append(f"\n📦 Объём: {volume}")

    if shade:
        parts.append(f"🎨 Оттенок: {shade}")

    parts.append("")
    parts.append(f"💰 Цена: {price}")
    parts.append(f"📦 В наличии: {stock}")

    caption = "\n".join(parts)

    return shorten(caption, MAX_CAPTION_LENGTH)


# ============================================================
# PRODUCT STATE
# ============================================================

def get_state(user_id: int):
    return USER_STATE.setdefault(user_id, {})


def clear_state(user_id: int):
    USER_STATE[user_id] = {}


def parse_price_stock(text: str):
    price = None
    stock = None

    if not text:
        return price, stock

    price_match = re.search(
        r"(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб|рублей)?",
        text,
        re.I,
    )

    if price_match:
        value = price_match.group(1)
        value = value.replace(" ", "").replace(",", ".")
        price = value

        if "." in price:
            try:
                price = str(float(price)).rstrip("0").rstrip(".")
            except Exception:
                pass

    stock_match = re.search(
        r"(?:в\s*наличии|наличие|шт\.?|штук|количество)\s*[:\-]?\s*(\d+)",
        text,
        re.I,
    )

    if stock_match:
        stock = stock_match.group(1)

    return price, stock


# ============================================================
# PRODUCT CREATION
# ============================================================

async def create_product_card(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
):

    state = get_state(user_id)

    user_photos = state.get("photos", [])
    product_hint = state.get("product_hint", "")

    price = state.get("price", "")
    stock = state.get("stock", "1")

    if not user_photos:
        return

    await context.bot.send_message(
        chat_id=user_id,
        text="🔎 Определяю товар и ищу актуальную информацию...\n"
             "Это может занять немного времени 💗"
    )

    image_data_urls = []

    for file_id in user_photos[:2]:
        data_url = await telegram_photo_to_data_url(
            context.bot,
            file_id,
        )

        if data_url:
            image_data_urls.append(data_url)

    try:
        data = await research_product(
            image_urls=image_data_urls,
            user_hint=product_hint,
        )

    except Exception as e:
        logger.exception("Product research failed: %s", e)

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "Не получилось найти информацию о товаре 😔\n\n"
                "Попробуй ещё раз и напиши название товара "
                "в подписи к фотографии."
            ),
        )
        return

    if not data:
        await context.bot.send_message(
            chat_id=user_id,
            text="Не удалось распознать товар 😔\n"
                 "Напиши его название вместе с фото и попробуй ещё раз."
        )
        return

    # --------------------------------------------------------
    # USER OVERRIDES
    # --------------------------------------------------------

    if not price:
        await context.bot.send_message(
            chat_id=user_id,
            text="💰 Напиши цену товара, например:\n\n8500 ₽"
        )

        state["awaiting"] = "price"
        state["research_data"] = data

        return

    if not stock:
        stock = "1"

    # --------------------------------------------------------
    # SAVE PRODUCT
    # --------------------------------------------------------

    caption = build_product_caption(
        data,
        price,
        stock,
    )

    item_id = save_product(
        user_id=user_id,
        data=data,
        price=price,
        stock=stock,
        photo1=user_photos[0] if user_photos else "",
        photo2=user_photos[1] if len(user_photos) > 1 else "",
    )

    # --------------------------------------------------------
    # FIND ONLINE IMAGES
    # --------------------------------------------------------

    online_images = await find_better_product_images(data)

    photo1 = user_photos[0] if user_photos else ""
    photo2 = user_photos[1] if len(user_photos) > 1 else ""

    # --------------------------------------------------------
    # SEND TWO PHOTOS
    # --------------------------------------------------------

    sent_as_album = False

    if len(online_images) >= 2:

        try:
            media = [
                InputMediaPhoto(
                    media=online_images[0],
                    caption=caption,
                ),
                InputMediaPhoto(
                    media=online_images[1],
                ),
            ]

            await context.bot.send_media_group(
                chat_id=user_id,
                media=media,
            )

            sent_as_album = True

        except Exception:
            logger.exception("Online album failed")

    if not sent_as_album and photo1 and photo2:

        try:
            media = [
                InputMediaPhoto(
                    media=photo1,
                    caption=caption,
                ),
                InputMediaPhoto(
                    media=photo2,
                ),
            ]

            await context.bot.send_media_group(
                chat_id=user_id,
                media=media,
            )

            sent_as_album = True

        except Exception:
            logger.exception("Telegram photo album failed")

    if not sent_as_album:

        if online_images:

            try:
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=online_images[0],
                    caption=caption,
                )
                sent_as_album = True
            except Exception:
                pass

        if not sent_as_album and photo1:

            await context.bot.send_photo(
                chat_id=user_id,
                photo=photo1,
                caption=caption,
            )

    # --------------------------------------------------------
    # APPROVAL
    # --------------------------------------------------------

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Одобрить",
                callback_data=f"approve_product:{item_id}",
            ),
            InlineKeyboardButton(
                "✏️ Изменить",
                callback_data=f"edit_product:{item_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🖼️ Другое фото",
                callback_data=f"photo_product:{item_id}",
            ),
            InlineKeyboardButton(
                "🔄 Новый товар",
                callback_data="new_product",
            ),
        ],
    ])

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "⬆️ Проверь карточку товара.\n\n"
            "Если всё правильно — нажми «Одобрить».\n"
            "Только после этого товар попадёт в очередь публикации."
        ),
        reply_markup=keyboard,
    )

    clear_state(user_id)


def save_product(
    user_id: int,
    data: dict,
    price: str,
    stock: str,
    photo1: str,
    photo2: str,
):

    conn = db()

    cursor = conn.execute(
        """
        INSERT INTO products
        (
            user_id,
            name,
            category,
            item_type,
            price,
            stock,
            data_json,
            photo1,
            photo2,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            data.get("name", ""),
            data.get("category", ""),
            data.get("item_type", "single"),
            price,
            stock,
            json.dumps(data, ensure_ascii=False),
            photo1,
            photo2,
            datetime.utcnow().isoformat(),
        ),
    )

    conn.commit()

    item_id = cursor.lastrowid

    conn.close()

    return item_id


# ============================================================
# PHOTO HANDLER
# ============================================================

async def handle_product_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message
    user_id = update.effective_user.id

    ensure_user(user_id)

    state = get_state(user_id)

    if "photos" not in state:
        state["photos"] = []

    # Telegram даёт несколько размеров.
    photo = message.photo[-1]

    state["photos"].append(photo.file_id)

    caption = message.caption or ""

    # Очень важно:
    # сохраняем весь текст подписи как подсказку AI.
    if caption:
        state["product_hint"] = caption

    price, stock = parse_price_stock(caption)

    if price:
        state["price"] = price

    if stock:
        state["stock"] = stock

    if len(state["photos"]) == 1:

        await message.reply_text(
            "📸 Первое фото получила!\n\n"
            "Теперь можешь:\n"
            "• прислать второе фото — например, упаковки;\n"
            "• или сразу написать цену и количество.\n\n"
            "Например:\n"
            "8500 ₽, в наличии 2 шт."
        )

        return

    if len(state["photos"]) >= 2:

        if state.get("price"):
            await create_product_card(
                user_id,
                context,
            )
        else:
            await message.reply_text(
                "Отлично, у меня есть два фото 🩷\n\n"
                "Теперь напиши цену и количество.\n\n"
                "Например:\n"
                "8500 ₽, в наличии 2 шт."
            )


# ============================================================
# TEXT HANDLER
# ============================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message
    user_id = update.effective_user.id
    text = (message.text or "").strip()

    ensure_user(user_id)

    state = get_state(user_id)

    # --------------------------------------------------------
    # WAITING FOR PRICE
    # --------------------------------------------------------

    if state.get("awaiting") == "price":

        price, stock = parse_price_stock(text)

        if not price:
            await message.reply_text(
                "Напиши цену, например: 8500 ₽"
            )
            return

        data = state.get("research_data", {})

        state["price"] = price
        state["stock"] = stock or "1"
        state.pop("awaiting", None)

        caption = build_product_caption(
            data,
            state["price"],
            state["stock"],
        )

        await message.reply_text(
            "Готово 💗\n\n" + caption
        )

        return

    # --------------------------------------------------------
    # SCHEDULE INPUT
    # --------------------------------------------------------

    if state.get("awaiting") == "schedule_times":

        await save_schedule_from_text(
            user_id,
            text,
            message,
        )

        state.pop("awaiting", None)

        return

    # --------------------------------------------------------
    # PRODUCT TEXT WITH PHOTO ALREADY RECEIVED
    # --------------------------------------------------------

    if state.get("photos"):

        price, stock = parse_price_stock(text)

        if price:
            state["price"] = price

        if stock:
            state["stock"] = stock

        # Если пользователь пишет название товара.
        state["product_hint"] = text

        if state.get("price"):

            await create_product_card(
                user_id,
                context,
            )

            return

        await message.reply_text(
            "Поняла 💗\n\n"
            "Напиши цену товара, например:\n"
            "8500 ₽, 2 шт."
        )

        return

    # --------------------------------------------------------
    # GENERAL TEXT COMMANDS
    # --------------------------------------------------------

    lowered = text.lower()

    if lowered in ("меню", "menu"):
        await message.reply_text(
            "Главное меню 💗",
            reply_markup=main_menu(),
        )
        return

    if lowered in ("расписание", "schedule"):
        await show_schedule(user_id, message)
        return

    if lowered in ("очередь", "queue"):
        await show_queue(user_id, message)
        return

    if lowered in ("план", "контент-план"):
        await generate_content_plan(user_id, message)
        return

    # Если пользователь просто прислал текст — предлагаем создать пост.
    await message.reply_text(
        "Я могу превратить это в готовый контент 💗\n\n"
        "Выбери действие:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📝 Сделать пост",
                    callback_data="create_post",
                )
            ],
            [
                InlineKeyboardButton(
                    "💡 Идеи",
                    callback_data="ideas",
                ),
                InlineKeyboardButton(
                    "📅 Контент-план",
                    callback_data="content_plan",
                ),
            ],
        ]),
    )


# ============================================================
# APPROVAL / CALLBACKS
# ============================================================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id
    data = query.data

    ensure_user(user_id)

    # --------------------------------------------------------
    # ADD PRODUCT
    # --------------------------------------------------------

    if data == "add_product":

        clear_state(user_id)

        await query.message.reply_text(
            "📦 Отлично!\n\n"
            "Пришли мне фото товара.\n\n"
            "Лучший вариант — 2 фото:\n"
            "1️⃣ сам товар\n"
            "2️⃣ товар + упаковка\n\n"
            "Можно сразу написать подписью:\n\n"
            "YSL Libre, 8500 ₽, 2 шт."
        )

        return

    # --------------------------------------------------------
    # NEW PRODUCT
    # --------------------------------------------------------

    if data == "new_product":

        clear_state(user_id)

        await query.message.reply_text(
            "🔄 Хорошо!\n\n"
            "Присылай новый товар."
        )

        return

    # --------------------------------------------------------
    # APPROVE PRODUCT
    # --------------------------------------------------------

    if data.startswith("approve_product:"):

        item_id = int(data.split(":")[1])

        product = get_product(item_id, user_id)

        if not product:
            await query.message.reply_text(
                "Товар не найден."
            )
            return

        await approve_product(
            user_id,
            product,
            context,
            query.message,
        )

        return

    # --------------------------------------------------------
    # EDIT PRODUCT
    # --------------------------------------------------------

    if data.startswith("edit_product:"):

        item_id = int(data.split(":")[1])

        await query.message.reply_text(
            "✏️ Напиши, что изменить.\n\n"
            "Например:\n"
            "• изменить цену на 7900\n"
            "• в наличии 3 шт.\n"
            "• убрать информацию про подарок\n"
            "• сделать описание короче"
        )

        state = get_state(user_id)

        state["editing_product_id"] = item_id

        return

    # --------------------------------------------------------
    # OTHER PHOTO
    # --------------------------------------------------------

    if data.startswith("photo_product:"):

        await query.message.reply_text(
            "🖼️ Хорошо!\n\n"
            "Пришли новое фото товара."
        )

        state = get_state(user_id)

        state["replace_photo"] = True

        return

    # --------------------------------------------------------
    # SCHEDULE
    # --------------------------------------------------------

    if data == "schedule":

        await show_schedule(
            user_id,
            query.message,
        )

        return

    # --------------------------------------------------------
    # QUEUE
    # --------------------------------------------------------

    if data == "queue":

        await show_queue(
            user_id,
            query.message,
        )

        return

    # --------------------------------------------------------
    # CONTENT PLAN
    # --------------------------------------------------------

    if data == "content_plan":

        await generate_content_plan(
            user_id,
            query.message,
        )

        return

    # --------------------------------------------------------
    # IDEAS
    # --------------------------------------------------------

    if data == "ideas":

        await generate_ideas(
            user_id,
            query.message,
        )

        return

    # --------------------------------------------------------
    # CREATE POST
    # --------------------------------------------------------

    if data == "create_post":

        await create_post(
            user_id,
            query.message,
        )

        return

    # --------------------------------------------------------
    # POLL
    # --------------------------------------------------------

    if data == "poll":

        await create_poll_idea(
            user_id,
            query.message,
        )

        return

    # --------------------------------------------------------
    # CHANNEL
    # --------------------------------------------------------

    if data == "channel":

        await query.message.reply_text(
            "📢 Подключение канала\n\n"
            "1. Добавь меня администратором канала.\n"
            "2. Дай право публиковать сообщения.\n"
            "3. Затем перешли мне любое сообщение из этого канала.\n\n"
            "Я попробую определить канал автоматически."
        )

        return


# ============================================================
# PRODUCTS DATABASE
# ============================================================

def get_product(item_id: int, user_id: int):

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM products
        WHERE id = ? AND user_id = ?
        """,
        (item_id, user_id),
    ).fetchone()

    conn.close()

    if not row:
        return None

    item = dict(row)

    try:
        item["data"] = json.loads(item["data_json"])
    except Exception:
        item["data"] = {}

    return item


# ============================================================
# APPROVE PRODUCT
# ============================================================

async def approve_product(
    user_id: int,
    product: dict,
    context: ContextTypes.DEFAULT_TYPE,
    message,
):

    channel_id = get_channel_id(user_id)

    if not channel_id:

        await message.reply_text(
            "⚠️ Сначала подключи Telegram-канал.\n\n"
            "Нажми «📢 Канал» в меню."
        )

        return

    data = product["data"]

    caption = build_product_caption(
        data,
        product["price"],
        product["stock"],
    )

    photo1 = product.get("photo1", "")
    photo2 = product.get("photo2", "")

    publish_at = get_next_publish_time(
        user_id,
    )

    queue_id = add_queue_item(
        user_id=user_id,
        channel_id=channel_id,
        content_type="product",
        text=caption,
        photo1=photo1,
        photo2=photo2,
        publish_at=publish_at,
    )

    await message.reply_text(
        "✅ Товар одобрен!\n\n"
        f"📅 Поставила в очередь на:\n"
        f"{format_dt(publish_at)}\n\n"
        "До этого момента он не будет опубликован."
    )


# ============================================================
# QUEUE
# ============================================================

def add_queue_item(
    user_id: int,
    channel_id: str,
    content_type: str,
    text: str,
    photo1: str = "",
    photo2: str = "",
    publish_at: Optional[datetime] = None,
):

    if publish_at is None:
        publish_at = get_next_publish_time(user_id)

    conn = db()

    cursor = conn.execute(
        """
        INSERT INTO queue
        (
            user_id,
            channel_id,
            content_type,
            text,
            photo1,
            photo2,
            publish_at,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?)
        """,
        (
            user_id,
            str(channel_id),
            content_type,
            text,
            photo1,
            photo2,
            publish_at.isoformat(),
            datetime.utcnow().isoformat(),
        ),
    )

    conn.commit()

    item_id = cursor.lastrowid

    conn.close()

    return item_id


async def show_queue(user_id: int, message):

    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM queue
        WHERE user_id = ?
        AND status = 'waiting'
        ORDER BY publish_at ASC
        LIMIT 20
        """,
        (user_id,),
    ).fetchall()

    conn.close()

    if not rows:

        await message.reply_text(
            "📋 Очередь пока пустая."
        )

        return

    text = "📋 Очередь публикаций:\n\n"

    for i, row in enumerate(rows, 1):

        title = row["text"] or "Без текста"

        title = title.replace("\n", " ")

        text += (
            f"{i}. {format_dt(row['publish_at'])}\n"
            f"{shorten(title, 90)}\n\n"
        )

    await message.reply_text(text)


# ============================================================
# SCHEDULING
# ============================================================

def get_schedule(user_id: int):

    conn = db()

    row = conn.execute(
        "SELECT * FROM schedules WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    conn.close()

    if not row:
        return {
            "times": ["10:00", "15:00", "20:00"],
            "days": [1, 2, 3, 4, 5, 6, 7],
            "timezone": BOT_TIMEZONE,
        }

    return {
        "times": json.loads(row["times"]),
        "days": json.loads(row["days"]),
        "timezone": row["timezone"] or BOT_TIMEZONE,
    }


async def show_schedule(user_id: int, message):

    schedule = get_schedule(user_id)

    days = schedule["days"]

    day_names = {
        1: "Пн",
        2: "Вт",
        3: "Ср",
        4: "Чт",
        5: "Пт",
        6: "Сб",
        7: "Вс",
    }

    days_text = ", ".join(
        day_names.get(day, str(day))
        for day in days
    )

    await message.reply_text(
        "📅 Расписание\n\n"
        f"⏰ Время: {', '.join(schedule['times'])}\n"
        f"📆 Дни: {days_text}\n"
        f"🌍 Часовой пояс: {schedule['timezone']}\n\n"
        "Чтобы изменить расписание, напиши:\n\n"
        "10:00, 15:00, 20:00\n"
        "каждый день\n\n"
        "или, например:\n\n"
        "10:00, 18:00\n"
        "1,2,3,4,5"
    )


async def save_schedule_from_text(
    user_id: int,
    text: str,
    message,
):

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines:
        await message.reply_text(
            "Не смогла разобрать расписание."
        )
        return

    times = re.findall(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
        text,
    )

    formatted_times = [
        f"{int(hour):02d}:{minute}"
        for hour, minute in times
    ]

    if not formatted_times:
        await message.reply_text(
            "Не нашла время.\n"
            "Напиши, например: 10:00, 15:00, 20:00"
        )
        return

    if "каждый день" in text.lower():

        days = [1, 2, 3, 4, 5, 6, 7]

    else:

        numbers = re.findall(
            r"\b([1-7])\b",
            text,
        )

        days = sorted(set(int(x) for x in numbers))

        if not days:
            days = [1, 2, 3, 4, 5, 6, 7]

    conn = db()

    conn.execute(
        """
        INSERT INTO schedules
        (user_id, times, days, timezone)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            times = excluded.times,
            days = excluded.days,
            timezone = excluded.timezone
        """,
        (
            user_id,
            json.dumps(formatted_times),
            json.dumps(days),
            BOT_TIMEZONE,
        ),
    )

    conn.commit()
    conn.close()

    await message.reply_text(
        "✅ Расписание сохранено!\n\n"
        f"⏰ {', '.join(formatted_times)}\n"
        f"📆 Дни: {', '.join(map(str, days))}"
    )


def get_next_publish_time(user_id: int):

    schedule = get_schedule(user_id)

    timezone = ZoneInfo(
        schedule.get("timezone", BOT_TIMEZONE)
    )

    now = datetime.now(timezone)

    candidates = []

    for offset in range(0, 14):

        date = now.date() + timedelta(days=offset)

        weekday = date.isoweekday()

        if weekday not in schedule["days"]:
            continue

        for time_str in schedule["times"]:

            hour, minute = map(
                int,
                time_str.split(":"),
            )

            candidate = datetime(
                date.year,
                date.month,
                date.day,
                hour,
                minute,
                tzinfo=timezone,
            )

            if candidate > now:
                candidates.append(candidate)

    if not candidates:

        return now + timedelta(hours=1)

    return min(candidates)


def format_dt(value):

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except Exception:
            return value

    if value.tzinfo is None:
        value = value.replace(
            tzinfo=ZoneInfo(BOT_TIMEZONE)
        )

    return value.strftime(
        "%d.%m.%Y в %H:%M"
    )


# ============================================================
# PUBLISHER
# ============================================================

async def publish_queue_item(
    item,
    context: ContextTypes.DEFAULT_TYPE,
):

    channel_id = item["channel_id"]

    try:

        # ----------------------------------------------------
        # TWO PHOTOS
        # ----------------------------------------------------

        if item["photo1"] and item["photo2"]:

            media = [
                InputMediaPhoto(
                    media=item["photo1"],
                    caption=item["text"],
                ),
                InputMediaPhoto(
                    media=item["photo2"],
                ),
            ]

            await context.bot.send_media_group(
                chat_id=channel_id,
                media=media,
            )

        # ----------------------------------------------------
        # ONE PHOTO
        # ----------------------------------------------------

        elif item["photo1"]:

            await context.bot.send_photo(
                chat_id=channel_id,
                photo=item["photo1"],
                caption=item["text"],
            )

        # ----------------------------------------------------
        # TEXT
        # ----------------------------------------------------

        else:

            await context.bot.send_message(
                chat_id=channel_id,
                text=item["text"],
            )

        mark_queue_done(item["id"])

        logger.info(
            "Published queue item %s",
            item["id"],
        )

    except Exception as e:

        logger.exception(
            "Publication failed for %s: %s",
            item["id"],
            e,
        )

        mark_queue_error(item["id"])


def mark_queue_done(item_id: int):

    conn = db()

    conn.execute(
        """
        UPDATE queue
        SET status = 'published'
        WHERE id = ?
        """,
        (item_id,),
    )

    conn.commit()
    conn.close()


def mark_queue_error(item_id: int):

    conn = db()

    conn.execute(
        """
        UPDATE queue
        SET status = 'error'
        WHERE id = ?
        """,
        (item_id,),
    )

    conn.commit()
    conn.close()


async def publisher_loop():

    while True:

        try:

            now_utc = datetime.utcnow()

            conn = db()

            rows = conn.execute(
                """
                SELECT *
                FROM queue
                WHERE status = 'waiting'
                AND publish_at <= ?
                ORDER BY publish_at ASC
                LIMIT 10
                """,
                (now_utc.isoformat(),),
            ).fetchall()

            conn.close()

            for row in rows:

                if telegram_app:

                    await publish_queue_item(
                        row,
                        telegram_app,
                    )

        except Exception:

            logger.exception(
                "Publisher loop error"
            )

        await asyncio.sleep(30)


# ============================================================
# CONTENT GENERATION
# ============================================================

async def generate_content_plan(
    user_id: int,
    message,
):

    prompt = """
Ты — контент-стратег Telegram-канала оригинальной косметики
и парфюмерии.

Составь контент-план на 7 дней.

Канал:
- оригинальная косметика;
- оригинальная парфюмерия;
- аудитория преимущественно девушки;
- канал не должен выглядеть каталогом;
- 70% полезного/интересного;
- 20% вовлечения;
- 10% прямых продаж.

Нужно:
- 1–2 основных поста в день;
- идеи для коротких сообщений/историй;
- опросы;
- полезный контент;
- немного товаров;
- мягкие продажи;
- темы, которыми хочется поделиться.

Пиши конкретные идеи, а не общие слова.
"""

    result = await ask_openai(
        prompt,
        web_search=True,
    )

    await message.reply_text(
        "💡 Контент-план на неделю:\n\n" + result
    )


async def generate_ideas(
    user_id: int,
    message,
):

    prompt = """
Придумай 20 конкретных идей для Telegram-канала
оригинальной косметики и парфюмерии.

Не делай канал похожим на магазин-каталог.

Раздели идеи на:
1. Полезное
2. Вовлечение
3. Парфюмерия
4. Косметика
5. Личные/уютные форматы без необходимости показывать лицо
6. Мягкие продажи
7. Идеи для TikTok/Reels

Идеи должны быть современными, живыми и такими,
которые хочется сохранить или переслать подруге.
"""

    result = await ask_openai(
        prompt,
        web_search=True,
    )

    await message.reply_text(
        "💡 Идеи для канала:\n\n" + result
    )


async def create_post(
    user_id: int,
    message,
):

    prompt = """
Создай один интересный Telegram-пост для канала
оригинальной косметики и парфюмерии.

Не рекламируй конкретный товар, если он не указан.

Тон:
- живой;
- женственный;
- дружелюбный;
- не слишком официальный;
- без ощущения магазина;
- без фальшивого личного опыта.

Тема:
«5 ароматов, которые пахнут дороже своей цены»

Не выдумывай факты о конкретных ароматах.
Если приводишь продукты, используй только подтверждаемую информацию.
"""

    result = await ask_openai(
        prompt,
        web_search=True,
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Одобрить",
                callback_data="approve_text_post",
            ),
            InlineKeyboardButton(
                "✏️ Изменить",
                callback_data="edit_text_post",
            ),
        ]
    ])

    await message.reply_text(
        result,
        reply_markup=keyboard,
    )


async def create_poll_idea(
    user_id: int,
    message,
):

    prompt = """
Придумай 5 коротких Telegram-опросов для канала
косметики и парфюмерии.

Опросы должны быть простыми, интересными и провоцировать
ответить, а не просто посмотреть.

Формат:

Вопрос
1. Вариант
2. Вариант
3. Вариант
4. Вариант
"""

    result = await ask_openai(prompt)

    await message.reply_text(
        "🗳️ Идеи для опросов:\n\n" + result
    )


# ============================================================
# CHANNEL CONNECTION
# ============================================================

async def handle_forwarded_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message

    if not message:
        return

    origin = getattr(
        message,
        "forward_origin",
        None,
    )

    if not origin:
        return

    channel = getattr(
        origin,
        "chat",
        None,
    )

    if not channel:
        return

    channel_id = channel.id

    user_id = update.effective_user.id

    set_channel_id(
        user_id,
        str(channel_id),
    )

    await message.reply_text(
        f"✅ Канал подключён!\n\n"
        f"Теперь одобренные публикации смогут "
        f"отправляться в этот канал.\n\n"
        f"ID: {channel_id}"
    )


# ============================================================
# EDIT PRODUCT TEXT
# ============================================================

async def handle_editing_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    state = get_state(user_id)

    item_id = state.get(
        "editing_product_id"
    )

    if not item_id:
        return False

    product = get_product(
        item_id,
        user_id,
    )

    if not product:
        return False

    old_caption = build_product_caption(
        product["data"],
        product["price"],
        product["stock"],
    )

    prompt = f"""
Отредактируй карточку товара.

Текущая карточка:

{old_caption}

Пожелание продавца:

{update.message.text}

Сохрани точность информации.
Не придумывай новые характеристики.

Верни только готовую карточку товара.
"""

    result = await ask_openai(prompt)

    await update.message.reply_text(
        result,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Одобрить",
                    callback_data=f"approve_product:{item_id}",
                )
            ]
        ]),
    )

    state.pop(
        "editing_product_id",
        None,
    )

    return True


# ============================================================
# COMMANDS
# ============================================================

async def cmd_menu(update: Update, context):
    await update.message.reply_text(
        "Меню 💗",
        reply_markup=main_menu(),
    )


async def cmd_schedule(update: Update, context):
    await show_schedule(
        update.effective_user.id,
        update.message,
    )


async def cmd_queue(update: Update, context):
    await show_queue(
        update.effective_user.id,
        update.message,
    )


async def cmd_channel(update: Update, context):

    await update.message.reply_text(
        "📢 Чтобы подключить канал:\n\n"
        "1. Добавь бота администратором.\n"
        "2. Дай право публиковать сообщения.\n"
        "3. Перешли мне любое сообщение из канала."
    )


# ============================================================
# FASTAPI
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "bot": "Beauty Manager",
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
    }


# ============================================================
# TELEGRAM STARTUP
# ============================================================

async def start_bot():

    global telegram_app

    init_db()

    telegram_app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Commands
    telegram_app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "menu",
            cmd_menu,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "schedule",
            cmd_schedule,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "queue",
            cmd_queue,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "channel",
            cmd_channel,
        )
    )

    # Callback buttons
    telegram_app.add_handler(
        CallbackQueryHandler(
            callbacks,
        )
    )

    # Forwarded channel posts
    telegram_app.add_handler(
        MessageHandler(
            filters.FORWARDED,
            handle_forwarded_message,
        ),
        group=0,
    )

    # Photos
    telegram_app.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_product_photo,
        ),
        group=1,
    )

    # Text
    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        ),
        group=2,
    )

    await telegram_app.initialize()
    await telegram_app.start()

    await telegram_app.updater.start_polling()

    logger.info(
        "Beauty Manager Telegram bot started"
    )

    asyncio.create_task(
        publisher_loop()
    )


async def stop_bot():

    global telegram_app

    if telegram_app:

        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

        telegram_app = None


# ============================================================
# FASTAPI LIFESPAN
# ============================================================

@app.on_event("startup")
async def startup_event():

    await start_bot()


@app.on_event("shutdown")
async def shutdown_event():

    await stop_bot()


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(
            os.getenv("PORT", "8000")
        ),
)
