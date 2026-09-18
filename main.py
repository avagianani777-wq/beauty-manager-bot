import os
import re
import json
import asyncio
import sqlite3
import logging
import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
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

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Можно оставить пустым.
# Канал можно подключить прямо из бота через кнопку.
ENV_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

# Москва — основной часовой пояс магазина.
DEFAULT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow")

DB_PATH = os.getenv("DB_PATH", "beauty_manager.db")

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing")


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("beauty-manager")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


def db_execute(query, params=(), commit=True):
    cur = db.cursor()
    cur.execute(query, params)
    if commit:
        db.commit()
    return cur


def init_db():
    db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            channel_id TEXT,
            timezone TEXT DEFAULT 'Europe/Moscow',
            schedule_enabled INTEGER DEFAULT 0,
            schedule_days TEXT DEFAULT '[0,1,2,3,4,5,6]',
            schedule_times TEXT DEFAULT '["10:00","15:00","20:00"]'
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            scheduled_at TEXT NOT NULL,
            caption TEXT,
            text_content TEXT,
            photo1 TEXT,
            photo2 TEXT,
            created_at TEXT NOT NULL,
            published_at TEXT
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT,
            item_type TEXT,
            name TEXT,
            description TEXT,
            volume TEXT,
            shade TEXT,
            price TEXT,
            stock TEXT,
            photo1 TEXT,
            photo2 TEXT,
            created_at TEXT NOT NULL
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            content TEXT,
            photo1 TEXT,
            photo2 TEXT,
            created_at TEXT NOT NULL
        )
    """)


init_db()


# ============================================================
# IN-MEMORY USER STATES
# ============================================================

USER_STATE = {}


def state_for(user_id):
    if user_id not in USER_STATE:
        USER_STATE[user_id] = {}
    return USER_STATE[user_id]


# ============================================================
# CATEGORY SYSTEM
# ============================================================

CATEGORY_MAP = {
    "парфюм": "#парфюм",
    "парфюмерия": "#парфюм",
    "духи": "#парфюм",
    "туалетная вода": "#парфюм",
    "парфюмерная вода": "#парфюм",

    "тональный": "#тональный_крем",
    "тональный крем": "#тональный_крем",
    "foundation": "#тональный_крем",

    "крем": "#крем",
    "крем для лица": "#крем",
    "уходовый крем": "#крем",

    "румяна": "#румяна",
    "blush": "#румяна",

    "бронзер": "#бронзер",
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
    "блеск для губ": "#блеск_для_губ",
    "lip gloss": "#блеск_для_губ",

    "карандаш": "#карандаш",
    "карандаш для глаз": "#карандаш",
    "карандаш для губ": "#карандаш",

    "хайлайтер": "#хайлайтер",
    "highlighter": "#хайлайтер",

    "консилер": "#консилер",
    "concealer": "#консилер",

    "маска": "#маска",
    "маска для лица": "#маска",

    "сыворотка": "#сыворотка",
    "serum": "#сыворотка",

    "шампунь": "#шампунь",
    "shampoo": "#шампунь",

    "кондиционер": "#кондиционер",
    "conditioner": "#кондиционер",

    "набор": "#набор",
    "сет": "#набор",
    "комплект": "#набор",
}


def normalize_category(value: str) -> str:
    if not value:
        return "#другое"

    value = value.lower().strip()

    if value.startswith("#"):
        value = value[1:]

    if value in CATEGORY_MAP:
        return CATEGORY_MAP[value]

    for key, tag in CATEGORY_MAP.items():
        if key in value:
            return tag

    value = re.sub(r"[^a-zA-Zа-яА-Я0-9_]+", "_", value)
    value = value.strip("_")

    if not value:
        return "#другое"

    return "#" + value.lower()


# ============================================================
# HELPERS
# ============================================================

def now_iso():
    return datetime.utcnow().isoformat()


def escape_caption(text: str) -> str:
    """
    Убираем ссылки и лишний мусор из текста.
    """
    if not text:
        return ""

    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Убираем строки с источниками.
    text = re.sub(
        r"(?im)^\s*(источники|source|sources|ссылки)\s*:?.*$",
        "",
        text,
    )

    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def truncate_caption(text: str, limit=1000):
    if len(text) <= limit:
        return text

    return text[:limit - 3].rstrip() + "..."


def parse_price_stock(text: str):
    """
    Поддерживает:
    7500 ₽
    7500 руб
    7500
    2 шт
    наличие 2
    в наличии 2
    """

    price = None
    stock = None

    price_match = re.search(
        r"(\d[\d\s]{2,8})\s*(?:₽|руб\.?|рублей)?",
        text,
        re.IGNORECASE,
    )

    if price_match:
        raw = price_match.group(1).replace(" ", "")
        try:
            value = int(raw)
            if value >= 100:
                price = f"{value:,}".replace(",", " ") + " ₽"
        except Exception:
            pass

    stock_match = re.search(
        r"(?:в\s*наличии|наличие|осталось|остаток|шт\.?|штук)"
        r"\s*[:\-]?\s*(\d+)",
        text,
        re.IGNORECASE,
    )

    if stock_match:
        stock = stock_match.group(1)

    # вариант "2 шт"
    if not stock:
        stock_match = re.search(
            r"\b(\d+)\s*(?:шт\.?|штук)\b",
            text,
            re.IGNORECASE,
        )
        if stock_match:
            stock = stock_match.group(1)

    return price, stock


async def download_image(url: str):
    """
    Скачиваем найденную картинку и возвращаем bytes.
    """
    if not url:
        return None

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/125 Safari/537.36"
            )
        }

        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers=headers,
        ) as client:
            r = await client.get(url)

            if r.status_code != 200:
                return None

            content_type = r.headers.get("content-type", "").lower()

            if "image" not in content_type:
                return None

            if len(r.content) < 5_000:
                return None

            return r.content

    except Exception as e:
        logger.warning("Image download failed: %s", e)
        return None


# ============================================================
# OPENAI
# ============================================================

async def ai_request(prompt: str, web=False):
    kwargs = {
        "model": MODEL,
        "input": prompt,
    }

    if web:
        kwargs["tools"] = [
            {
                "type": "web_search",
                "search_context_size": "high",
            }
        ]

    response = await openai_client.responses.create(**kwargs)

    return response.output_text or ""


async def identify_product(product_hint: str):
    prompt = f"""
Ты — AI-менеджер Telegram-магазина оригинальной косметики и парфюмерии.

Определи товар по описанию пользователя.

Описание:
{product_hint}

Нужно вернуть ТОЛЬКО JSON:

{{
  "name": "...",
  "brand": "...",
  "category": "...",
  "item_type": "single|set|bundle|multiple_separate",
  "volume": "...",
  "shade": "...",
  "search_query": "..."
}}

Правила:
- Если несколько средств продаются вместе как один набор — item_type = "set".
- Если это просто несколько отдельных товаров — multiple_separate.
- Не придумывай оттенок или объём.
- Если информации нет, оставляй пустую строку.
- category должна быть короткой: например "парфюм", "румяна",
  "бронзер", "тональный крем", "пудра", "тени", "тушь", "крем".
"""

    raw = await ai_request(prompt, web=False)

    try:
        match = re.search(r"\{.*\}", raw, re.S)

        if match:
            return json.loads(match.group(0))
    except Exception:
        pass

    return {
        "name": product_hint[:100],
        "brand": "",
        "category": "другое",
        "item_type": "single",
        "volume": "",
        "shade": "",
        "search_query": product_hint,
    }


async def research_product(info):
    """
    Ищем актуальную информацию и ссылки на изображения.
    Источники не попадут в пользовательскую карточку.
    """

    prompt = f"""
Найди актуальную информацию о товаре.

Товар:
{json.dumps(info, ensure_ascii=False)}

Используй web search.

Приоритет:
1. официальный сайт бренда;
2. официальный магазин;
3. крупные проверенные магазины.

Нужно вернуть ТОЛЬКО JSON:

{{
  "name": "...",
  "description": "...",
  "volume": "...",
  "shade": "...",
  "category": "...",
  "item_type": "single|set|bundle|multiple_separate",
  "product_image_url": "...",
  "packaging_image_url": "..."
}}

Изображения:
- product_image_url — фотография самого продукта без коробки,
  если такая доступна;
- packaging_image_url — фотография продукта вместе с упаковкой/коробкой,
  если такая доступна.

Очень важно:
- не придумывай URL;
- если подходящего изображения нет, ставь пустую строку;
- не добавляй источники;
- не добавляй markdown;
- описание должно быть коротким и пригодным для Telegram;
- не придумывай свойства, которых нет в источниках.
"""

    raw = await ai_request(prompt, web=True)

    try:
        match = re.search(r"\{.*\}", raw, re.S)

        if match:
            return json.loads(match.group(0))
    except Exception:
        pass

    return {
        "name": info.get("name", ""),
        "description": "",
        "volume": info.get("volume", ""),
        "shade": info.get("shade", ""),
        "category": info.get("category", "другое"),
        "item_type": info.get("item_type", "single"),
        "product_image_url": "",
        "packaging_image_url": "",
    }


# ============================================================
# IMAGE SEARCH FALLBACK
# ============================================================

async def image_from_page(page_url: str):
    """
    Пытаемся достать og:image / twitter:image / JSON-LD / img.
    """

    if not page_url:
        return None

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/125 Safari/537.36"
            )
        }

        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers=headers,
        ) as client:
            r = await client.get(page_url)

        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        for selector in [
            ('meta[property="og:image"]', "content"),
            ('meta[name="twitter:image"]', "content"),
        ]:
            tag = soup.select_one(selector[0])
            if tag and tag.get(selector[1]):
                return urljoin(page_url, tag.get(selector[1]))

        # JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.get_text(strip=True))

                if isinstance(data, dict):
                    image = data.get("image")

                    if isinstance(image, str):
                        return urljoin(page_url, image)

                    if isinstance(image, list) and image:
                        return urljoin(page_url, image[0])

            except Exception:
                continue

        # обычные img
        for img in soup.find_all("img"):
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-original")
            )

            if src and src.startswith(("http://", "https://", "/")):
                return urljoin(page_url, src)

    except Exception as e:
        logger.warning("Page image search failed: %s", e)

    return None


async def get_best_image(url: str):
    """
    Сначала пробуем URL как картинку.
    Если это страница — ищем картинку внутри.
    """

    if not url:
        return None

    image = await download_image(url)

    if image:
        return image

    page_image = await image_from_page(url)

    if page_image:
        return await download_image(page_image)

    return None


# ============================================================
# PRODUCT CAPTION
# ============================================================

def build_product_caption(data):
    category = normalize_category(data.get("category", ""))

    name = data.get("name", "").strip()
    description = escape_caption(data.get("description", ""))

    volume = data.get("volume", "").strip()
    shade = data.get("shade", "").strip()

    price = data.get("price", "").strip()
    stock = data.get("stock", "").strip()

    item_type = data.get("item_type", "single")

    if item_type == "set" and category == "#другое":
        category = "#набор"

    lines = [
        category,
        "",
        f"✨ {name}",
    ]

    if description:
        lines.extend([
            "",
            description,
        ])

    if volume:
        lines.append(f"\n📦 Объём: {volume}")

    if shade:
        lines.append(f"🎨 Оттенок: {shade}")

    if price:
        lines.append(f"\n💰 Цена: {price}")

    if stock:
        lines.append(f"📦 В наличии: {stock} шт.")

    return truncate_caption("\n".join(lines), 1000)


# ============================================================
# MAIN MENU
# ============================================================

def main_menu():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🛍 Новый товар"),
                KeyboardButton("✍️ Новый пост"),
            ],
            [
                KeyboardButton("📅 Контент-план"),
                KeyboardButton("⏰ Расписание"),
            ],
            [
                KeyboardButton("📋 Очередь"),
                KeyboardButton("📊 Идеи продвижения"),
            ],
            [
                KeyboardButton("🎬 Reels / TikTok"),
                KeyboardButton("📱 Stories"),
            ],
            [
                KeyboardButton("📊 Опрос"),
                KeyboardButton("💡 Что опубликовать?"),
            ],
            [
                KeyboardButton("⚙️ Настройки"),
            ],
        ],
        resize_keyboard=True,
    )


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    db_execute(
        """
        INSERT OR IGNORE INTO users
        (user_id, channel_id, timezone)
        VALUES (?, ?, ?)
        """,
        (
            user_id,
            ENV_CHANNEL_ID or None,
            DEFAULT_TIMEZONE,
        ),
    )

    state_for(user_id).clear()

    await update.message.reply_text(
        "Привет! 💗\n\n"
        "Я твой AI-менеджер магазина косметики и парфюмерии.\n\n"
        "Ты можешь прислать мне товар — я сама:\n"
        "• определю, что это;\n"
        "• найду информацию;\n"
        "• определю категорию;\n"
        "• поставлю правильный хэштег;\n"
        "• найду 2 фотографии;\n"
        "• оформлю карточку;\n"
        "• дождусь твоего одобрения;\n"
        "• поставлю товар в очередь на публикацию.\n\n"
        "Ничего в канал без твоего одобрения не уйдёт. ❤️",
        reply_markup=main_menu(),
    )


# ============================================================
# PRODUCT FLOW
# ============================================================

async def new_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    state = state_for(user_id)
    state.clear()
    state["mode"] = "product"
    state["photos"] = []

    await update.message.reply_text(
        "🛍 Отлично!\n\n"
        "Пришли фото товара.\n\n"
        "Можно:\n"
        "• одно фото — я сама найду второе;\n"
        "• сразу два фото — первое товара, второе с упаковкой.\n\n"
        "В подписи можешь сразу написать, например:\n"
        "7500 ₽, 2 шт."
    )


async def handle_product_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = state_for(user_id)

    if state.get("mode") != "product":
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id

    state.setdefault("photos", []).append(file_id)

    caption = update.message.caption or ""

    price, stock = parse_price_stock(caption)

    if price:
        state["price"] = price

    if stock:
        state["stock"] = stock

    if len(state["photos"]) == 1:
        await update.message.reply_text(
            "📸 Первое фото получила.\n\n"
            "Если есть второе фото с упаковкой — пришли его сейчас.\n"
            "Если второго нет, напиши цену и количество, например:\n\n"
            "7500 ₽, 2 шт."
        )

    elif len(state["photos"]) == 2:
        await update.message.reply_text(
            "📸 Получила оба фото!\n\n"
            "Теперь напиши цену и количество.\n\n"
            "Например:\n"
            "7500 ₽, 2 шт."
        )

    else:
        # Не собираем больше двух.
        state["photos"] = state["photos"][:2]

        await update.message.reply_text(
            "У меня уже есть два фото этого товара 💗\n"
            "Теперь напиши цену и количество."
        )


async def handle_product_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = state_for(user_id)

    if state.get("mode") != "product":
        return False

    text = update.message.text.strip()

    price, stock = parse_price_stock(text)

    # Если цены/остатка нет, считаем текст описанием товара.
    if not price:
        state["product_hint"] = text

        await update.message.reply_text(
            "Хорошо 💗 Теперь напиши цену и количество.\n\n"
            "Например:\n"
            "7500 ₽, 2 шт."
        )

        return True

    state["price"] = price

    if stock:
        state["stock"] = stock
    else:
        state["stock"] = "1"

    await create_product_card(update, context)

    return True


async def create_product_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = state_for(user_id)

    photos = state.get("photos", [])

    if not photos:
        await update.message.reply_text(
            "Мне сначала нужно фото товара 📸"
        )
        return

    price = state.get("price", "")
    stock = state.get("stock", "1")
    hint = state.get("product_hint", "")

    await update.message.reply_text(
        "🔎 Ищу товар и проверяю информацию...\n"
        "Это может занять немного времени."
    )

    try:
        info = await identify_product(hint or "Товар на фотографии")

        research = await research_product(info)

        # Если AI не определил название, используем пользовательское.
        name = research.get("name") or info.get("name") or "Товар"

        category = normalize_category(
            research.get("category")
            or info.get("category")
            or "другое"
        )

        item_type = research.get("item_type") or info.get("item_type") or "single"

        data = {
            "name": name,
            "description": research.get("description", ""),
            "volume": research.get("volume") or info.get("volume", ""),
            "shade": research.get("shade") or info.get("shade", ""),
            "category": category,
            "item_type": item_type,
            "price": price,
            "stock": stock,
        }

        # ----------------------------------------------------
        # PHOTO 1
        # ----------------------------------------------------

        photo1 = photos[0]

        # Если пользователь прислал только одно фото,
        # пытаемся найти фотографию товара отдельно.
        if len(photos) == 1:
            online_1 = await get_best_image(
                research.get("product_image_url", "")
            )

            if online_1:
                msg1 = await update.message.reply_photo(
                    photo=online_1,
                )
                photo1 = msg1.photo[-1].file_id

        # ----------------------------------------------------
        # PHOTO 2
        # ----------------------------------------------------

        photo2 = None

        if len(photos) >= 2:
            photo2 = photos[1]

        else:
            online_2 = await get_best_image(
                research.get("packaging_image_url", "")
            )

            if online_2:
                msg2 = await update.message.reply_photo(
                    photo=online_2,
                )
                photo2 = msg2.photo[-1].file_id

        # Если второго фото найти не получилось,
        # используем первое как резерв.
        if not photo2:
            photo2 = photo1

        data["category"] = category
        data["item_type"] = item_type

        caption = build_product_caption(data)

        # ----------------------------------------------------
        # SHOW PREVIEW
        # ----------------------------------------------------

        media = [
            InputMediaPhoto(
                media=photo1,
                caption=caption,
            ),
            InputMediaPhoto(
                media=photo2,
            ),
        ]

        sent = await context.bot.send_media_group(
            chat_id=user_id,
            media=media,
        )

        # Telegram media group не поддерживает inline keyboard
        # прямо на каждом элементе альбома.
        # Поэтому кнопки идут отдельным сообщением.
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Одобрить",
                        callback_data="product_approve",
                    ),
                    InlineKeyboardButton(
                        "✏️ Изменить",
                        callback_data="product_edit",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🖼 Другое фото",
                        callback_data="product_other_photo",
                    ),
                    InlineKeyboardButton(
                        "🔄 Новый товар",
                        callback_data="product_new",
                    ),
                ],
            ]
        )

        state["draft"] = data
        state["draft"]["caption"] = caption
        state["draft"]["photo1"] = photo1
        state["draft"]["photo2"] = photo2

        await update.message.reply_text(
            "Готово! 💗\n\n"
            "Первое фото — товар.\n"
            "Второе — товар с упаковкой.\n\n"
            "Проверь карточку. После «Одобрить» товар попадёт "
            "в очередь публикаций по твоему расписанию.",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.exception("Product creation error")

        await update.message.reply_text(
            "Не получилось собрать карточку 😔\n\n"
            f"Ошибка: {str(e)[:300]}"
        )


# ============================================================
# CALLBACKS
# ============================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    state = state_for(user_id)

    data = query.data

    if data == "product_new":
        state.clear()
        state["mode"] = "product"
        state["photos"] = []

        await query.message.reply_text(
            "🔄 Начинаем новый товар.\n\n"
            "Пришли фото."
        )
        return

    if data == "product_other_photo":
        state["waiting_new_photo"] = True

        await query.message.reply_text(
            "🖼 Пришли новое фото.\n"
            "Я заменю им первое фото карточки."
        )
        return

    if data == "product_edit":
        state["mode"] = "edit_product"

        await query.message.reply_text(
            "✏️ Что изменить?\n\n"
            "Напиши одним сообщением, например:\n\n"
            "цена 7900\n"
            "или\n"
            "оттенок 120\n"
            "или\n"
            "это набор\n"
            "или\n"
            "это #хайлайтер"
        )
        return

    if data == "product_approve":
        draft = state.get("draft")

        if not draft:
            await query.message.reply_text(
                "Черновик уже недоступен. Создай товар заново."
            )
            return

        queue_id = add_product_to_queue(
            user_id,
            draft,
        )

        # Сохраняем товар.
        db_execute(
            """
            INSERT INTO products (
                user_id, category, item_type, name, description,
                volume, shade, price, stock, photo1, photo2, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                draft.get("category"),
                draft.get("item_type"),
                draft.get("name"),
                draft.get("description"),
                draft.get("volume"),
                draft.get("shade"),
                draft.get("price"),
                draft.get("stock"),
                draft.get("photo1"),
                draft.get("photo2"),
                now_iso(),
            ),
        )

        scheduled = db_execute(
            "SELECT scheduled_at FROM queue WHERE id = ?",
            (queue_id,),
            commit=False,
        ).fetchone()

        state.clear()

        when = scheduled["scheduled_at"] if scheduled else ""

        await query.message.reply_text(
            "✅ Одобрено!\n\n"
            "Товар добавлен в очередь.\n"
            f"🕐 Планируемая публикация: {when}\n\n"
            "Самостоятельно в канал я его отправлю только "
            "по активному расписанию.",
            reply_markup=main_menu(),
        )
        return


# ============================================================
# QUEUE / SCHEDULING
# ============================================================

def get_user(user_id):
    return db_execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
        commit=False,
    ).fetchone()


def get_channel_id(user_id):
    row = get_user(user_id)

    if row and row["channel_id"]:
        return row["channel_id"]

    if ENV_CHANNEL_ID:
        return ENV_CHANNEL_ID

    return None


def schedule_config(user_id):
    row = get_user(user_id)

    if not row:
        db_execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, timezone)
            VALUES (?, ?)
            """,
            (user_id, DEFAULT_TIMEZONE),
        )

        row = get_user(user_id)

    try:
        days = json.loads(row["schedule_days"])
    except Exception:
        days = list(range(7))

    try:
        times = json.loads(row["schedule_times"])
    except Exception:
        times = ["10:00", "15:00", "20:00"]

    return {
        "enabled": bool(row["schedule_enabled"]),
        "days": days,
        "times": times,
        "timezone": row["timezone"] or DEFAULT_TIMEZONE,
    }


def parse_schedule_days(value):
    value = value.strip().lower()

    if value in ("каждый день", "ежедневно", "все", "7"):
        return list(range(7))

    result = []

    for part in re.split(r"[,\s]+", value):
        try:
            number = int(part)

            if 1 <= number <= 7:
                # Пн=0 ... Вс=6
                result.append(number - 1)

        except Exception:
            pass

    return sorted(set(result))


def valid_times(value):
    result = []

    for part in re.split(r"[,\s;]+", value.strip()):
        if re.match(r"^\d{1,2}:\d{2}$", part):
            h, m = part.split(":")
            h = int(h)
            m = int(m)

            if 0 <= h <= 23 and 0 <= m <= 59:
                result.append(f"{h:02d}:{m:02d}")

    return sorted(set(result))


def next_schedule_slot(user_id):
    config = schedule_config(user_id)

    tz = ZoneInfo(config["timezone"])

    current = datetime.now(tz).replace(second=0, microsecond=0)

    occupied_rows = db_execute(
        """
        SELECT scheduled_at
        FROM queue
        WHERE user_id = ?
        AND status = 'queued'
        """,
        (user_id,),
        commit=False,
    ).fetchall()

    occupied = {
        row["scheduled_at"]
        for row in occupied_rows
    }

    # Ищем ближайший слот в течение года.
    for day_offset in range(366):
        day = current + timedelta(days=day_offset)

        if day.weekday() not in config["days"]:
            continue

        for time_str in config["times"]:
            h, m = map(int, time_str.split(":"))

            candidate = day.replace(
                hour=h,
                minute=m,
                second=0,
                microsecond=0,
            )

            if candidate <= current:
                continue

            iso = candidate.isoformat()

            if iso not in occupied:
                return iso

    return None


def add_product_to_queue(user_id, draft):
    slot = next_schedule_slot(user_id)

    if not slot:
        raise RuntimeError("Не удалось найти свободный слот.")

    cur = db_execute(
        """
        INSERT INTO queue (
            user_id,
            kind,
            status,
            scheduled_at,
            caption,
            text_content,
            photo1,
            photo2,
            created_at
        )
        VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            "product",
            slot,
            draft.get("caption"),
            None,
            draft.get("photo1"),
            draft.get("photo2"),
            now_iso(),
        ),
    )

    return cur.lastrowid


async def schedule_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⚙️ Настроить",
                    callback_data="schedule_setup",
                ),
                InlineKeyboardButton(
                    "📋 Очередь",
                    callback_data="schedule_queue",
                ),
            ],
            [
                InlineKeyboardButton(
                    "▶️ Включить",
                    callback_data="schedule_on",
                ),
                InlineKeyboardButton(
                    "⏸ Пауза",
                    callback_data="schedule_off",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📢 Подключить канал",
                    callback_data="connect_channel",
                ),
            ],
        ]
    )

    config = schedule_config(update.effective_user.id)

    status = "🟢 Включено" if config["enabled"] else "⚪ Выключено"

    await update.message.reply_text(
        "⏰ **Расписание публикаций**\n\n"
        f"Статус: {status}\n"
        f"Время: {', '.join(config['times'])}\n"
        f"Дни: {', '.join(str(x + 1) for x in config['days'])}\n"
        f"Часовой пояс: {config['timezone']}\n\n"
        "В очередь попадают только одобренные тобой материалы.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def schedule_setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state_for(user_id)["mode"] = "schedule_times"

    await update.message.reply_text(
        "⚙️ Настройка расписания.\n\n"
        "Шаг 1.\n"
        "Напиши время публикаций через запятую.\n\n"
        "Например:\n"
        "10:00, 15:00, 20:00"
    )


async def handle_schedule_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = state_for(user_id)

    if state.get("mode") != "schedule_times":
        return False

    times = valid_times(update.message.text)

    if not times:
        await update.message.reply_text(
            "Не смогла распознать время 😔\n\n"
            "Напиши, например:\n"
            "10:00, 15:00, 20:00"
        )
        return True

    state["schedule_times"] = times
    state["mode"] = "schedule_days"

    await update.message.reply_text(
        "Отлично! 💗\n\n"
        "Шаг 2.\n"
        "В какие дни публиковать?\n\n"
        "Пн=1, Вт=2, Ср=3, Чт=4, Пт=5, Сб=6, Вс=7.\n\n"
        "Например:\n"
        "1,2,3,4,5,6,7\n\n"
        "Или просто:\n"
        "каждый день"
    )

    return True


async def handle_schedule_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = state_for(user_id)

    if state.get("mode") != "schedule_days":
        return False

    days = parse_schedule_days(update.message.text)

    if not days:
        await update.message.reply_text(
            "Не смогла определить дни 😔"
        )
        return True

    times = state.get(
        "schedule_times",
        ["10:00", "15:00", "20:00"],
    )

    db_execute(
        """
        UPDATE users
        SET schedule_days = ?,
            schedule_times = ?
        WHERE user_id = ?
        """,
        (
            json.dumps(days),
            json.dumps(times),
            user_id,
        ),
    )

    state.clear()

    await update.message.reply_text(
        "✅ Расписание сохранено!\n\n"
        f"Время: {', '.join(times)}\n"
        f"Дни: {', '.join(str(x + 1) for x in days)}\n\n"
        "Теперь одобренные публикации будут автоматически "
        "раскладываться по этим слотам.",
        reply_markup=main_menu(),
    )

    return True


async def queue_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    rows = db_execute(
        """
        SELECT *
        FROM queue
        WHERE user_id = ?
        AND status = 'queued'
        ORDER BY scheduled_at
        LIMIT 20
        """,
        (user_id,),
        commit=False,
    ).fetchall()

    if not rows:
        await update.message.reply_text(
            "📋 Очередь пока пустая."
        )
        return

    lines = ["📋 **Очередь публикаций**\n"]

    for row in rows:
        title = row["caption"] or row["text_content"] or "Публикация"

        # Первая строка карточки.
        first_line = title.splitlines()[0][:80]

        lines.append(
            f"#{row['id']} — {row['scheduled_at']}\n"
            f"{first_line}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


# ============================================================
# CHANNEL CONNECTION
# ============================================================

async def connect_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    state_for(user_id)["mode"] = "connect_channel"

    await update.message.reply_text(
        "📢 Подключение канала.\n\n"
        "Добавь этого бота администратором своего Telegram-канала "
        "с правом публикации сообщений.\n\n"
        "После этого просто перешли мне сюда любой пост "
        "из своего канала.\n\n"
        "Я попробую определить ID канала автоматически."
    )


async def handle_forwarded_channel_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    state = state_for(user_id)

    if state.get("mode") != "connect_channel":
        return False

    message = update.message

    origin = getattr(message, "forward_origin", None)

    channel_id = None
    channel_title = None

    if origin:
        origin_chat = getattr(origin, "chat", None)

        if origin_chat:
            channel_id = origin_chat.id
            channel_title = origin_chat.title

    if not channel_id:
        await update.message.reply_text(
            "Не смогла определить канал из этого сообщения 😔\n\n"
            "Перешли именно пост из канала."
        )
        return True

    db_execute(
        """
        UPDATE users
        SET channel_id = ?
        WHERE user_id = ?
        """,
        (str(channel_id), user_id),
    )

    state.clear()

    await update.message.reply_text(
        "✅ Канал подключён!\n\n"
        f"📢 {channel_title or 'Канал'}\n"
        f"ID: {channel_id}\n\n"
        "Теперь бот сможет публиковать туда "
        "одобренные тобой материалы по расписанию.",
        reply_markup=main_menu(),
    )

    return True


# ============================================================
# CONTENT GENERATION
# ============================================================

async def generate_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    state_for(user_id)["mode"] = "post_prompt"

    await update.message.reply_text(
        "✍️ Что хочешь опубликовать?\n\n"
        "Напиши тему.\n\n"
        "Например:\n"
        "«5 ароматов, которые пахнут дороже своей цены»\n\n"
        "или просто:\n"
        "«пост про уход за кожей»"
    )


async def handle_post_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = state_for(user_id)

    if state.get("mode") != "post_prompt":
        return False

    topic = update.message.text

    await update.message.reply_text(
        "✍️ Пишу пост..."
    )

    prompt = f"""
Ты ведёшь Telegram-канал оригинальной косметики и парфюмерии.

Название канала:
«Косметика | Парфюм | Москва»

Стиль:
живой, женственный, современный, дружелюбный.
Без ощущения сухого интернет-магазина.
Без навязчивых продаж.
Без выдуманных личных историй от лица Ани.

Контент должен быть интересным и сохраняемым.

Тема:
{topic}

Напиши готовый Telegram-пост.
Не используй источники и ссылки.
Не добавляй фразы вроде «подписывайтесь на канал» без необходимости.
"""
    text = escape_caption(await ai_request(prompt, web=True))

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ В очередь",
                    callback_data="post_approve",
                ),
                InlineKeyboardButton(
                    "✏️ Изменить",
                    callback_data="post_edit",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Отмена",
                    callback_data="post_cancel",
                )
            ],
        ]
    )

    state["draft_post"] = text

    await update.message.reply_text(
        text,
        reply_markup=keyboard,
    )

    return True


# ============================================================
# POST CALLBACKS
# ============================================================

async def handle_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    state = state_for(user_id)

    if query.data == "post_cancel":
        state.pop("draft_post", None)

        await query.message.reply_text(
            "❌ Черновик удалён.",
            reply_markup=main_menu(),
        )
        return

    if query.data == "post_edit":
        state["mode"] = "post_edit"

        await query.message.reply_text(
            "✏️ Напиши, что изменить в тексте."
        )
        return

    if query.data == "post_approve":
        text_content = state.get("draft_post")

        if not text_content:
            await query.message.reply_text(
                "Черновик не найден."
            )
            return

        slot = next_schedule_slot(user_id)

        if not slot:
            await query.message.reply_text(
                "Не нашла свободный слот расписания."
            )
            return

        db_execute(
            """
            INSERT INTO queue (
                user_id,
                kind,
                status,
                scheduled_at,
                text_content,
                created_at
            )
            VALUES (?, 'post', 'queued', ?, ?, ?)
            """,
            (
                user_id,
                slot,
                text_content,
                now_iso(),
            ),
        )

        state.clear()

        await query.message.reply_text(
            "✅ Пост одобрен и поставлен в очередь!\n\n"
            f"🕐 Публикация: {slot}",
            reply_markup=main_menu(),
        )


# ============================================================
# EDIT PRODUCT
# ============================================================

async def handle_edit_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = state_for(user_id)

    if state.get("mode") != "edit_product":
        return False

    text = update.message.text.strip()
    draft = state.get("draft")

    if not draft:
        await update.message.reply_text(
            "Черновик товара не найден."
        )
        state.clear()
        return True

    price, stock = parse_price_stock(text)

    if price:
        draft["price"] = price

    if stock:
        draft["stock"] = stock

    lower = text.lower()

    # Категория.
    for key, tag in CATEGORY_MAP.items():
        if key in lower or f"#{key}" in lower:
            draft["category"] = tag
            break

    # Набор.
    if "это набор" in lower or "набор" in lower:
        draft["item_type"] = "set"
        draft["category"] = "#набор"

    # Объём.
    volume_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(мл|ml|г|гр|g)",
        lower,
    )

    if volume_match:
        draft["volume"] = (
            f"{volume_match.group(1)} {volume_match.group(2)}"
        )

    # Оттенок.
    shade_match = re.search(
        r"(?:оттенок|тон|shade)\s*[:\-]?\s*(.+)",
        text,
        re.IGNORECASE,
    )

    if shade_match:
        draft["shade"] = shade_match.group(1).strip()

    draft["caption"] = build_product_caption(draft)

    state["draft"] = draft
    state["mode"] = None

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Одобрить",
                    callback_data="product_approve",
                ),
                InlineKeyboardButton(
                    "✏️ Изменить",
                    callback_data="product_edit",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 Новый товар",
                    callback_data="product_new",
                ),
            ],
        ]
    )

    await update.message.reply_text(
        "Готово, изменила карточку:\n\n"
        + draft["caption"],
        reply_markup=keyboard,
    )

    return True


# ============================================================
# STORIES
# ============================================================

async def generate_stories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 Придумываю Stories..."
    )

    prompt = """
Придумай 7 коротких идей для Telegram Stories/коротких сообщений
для канала оригинальной косметики и парфюмерии.

Стиль:
живой, красивый, женственный.
Без лица владельца.
Без выдуманных личных событий.

Смешай:
- опрос;
- выбор;
- полезный совет;
- продукт;
- интерактив;
- закулисье магазина;
- мягкую продажу.

Формат:
1.
2.
3.
...
"""

    result = await ai_request(prompt)

    await update.message.reply_text(result)


# ============================================================
# REELS / TIKTOK
# ============================================================

async def generate_reels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Придумываю идеи Reels/TikTok..."
    )

    prompt = """
Придумай 10 идей коротких Reels/TikTok для Telegram-магазина
оригинальной косметики и парфюмерии.

Владелица не показывает лицо.

Для каждой идеи:
- hook;
- что показать;
- текст на экране;
- короткий сценарий;
- CTA.

Идеи должны быть реально снимаемыми на телефон:
товары, руки, упаковка, распаковка, полка, красивые кадры,
сравнения, подборки.
"""

    result = await ai_request(prompt)

    await update.message.reply_text(result)


# ============================================================
# CONTENT PLAN
# ============================================================

async def content_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📅 Составляю план на неделю..."
    )

    prompt = """
Составь недельный контент-план для Telegram-канала
«Косметика | Парфюм | Москва».

Стратегия:
70% интересного/полезного,
20% вовлечения и ощущения присутствия автора,
10% прямых продаж.

Не превращай канал в каталог.

На каждый день:
- 1 основной пост;
- 3 идеи коротких сообщений/Stories.

Используй рубрики:
#Анисоветует
#Бьютиразбор
#Ароматдня
#ХочуНеМогу
#Новинки
#Вналичии
#Анивыбирает
#Бьютиопрос
#Находка
#Отзывы

Добавляй мягкие продажи.
"""

    result = await ai_request(prompt)

    await update.message.reply_text(result)


# ============================================================
# POLLS
# ============================================================

async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    state_for(user_id)["mode"] = "poll"

    await update.message.reply_text(
        "📊 Напиши тему опроса.\n\n"
        "Например:\n"
        "«Что выбираете чаще — парфюм или косметику?»"
    )


async def handle_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = state_for(user_id)

    if state.get("mode") != "poll":
        return False

    topic = update.message.text

    prompt = f"""
Придумай Telegram-опрос для магазина косметики и парфюмерии.

Тема:
{topic}

Верни JSON:
{{
  "question": "...",
  "options": ["...", "...", "...", "..."]
}}

Максимум 4 варианта.
"""

    raw = await ai_request(prompt)

    try:
        match = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(match.group(0))

        await update.message.reply_poll(
            question=data["question"],
            options=data["options"],
            is_anonymous=True,
        )

    except Exception:
        await update.message.reply_text(
            "Не получилось создать опрос 😔\n\n"
            + raw[:1000]
        )

    state.clear()

    return True


# ============================================================
# ENGAGEMENT / GROWTH
# ============================================================

async def growth_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = """
Дай 15 конкретных идей роста Telegram-канала
оригинальной косметики и парфюмерии.

Основные источники:
- TikTok;
- Instagram Reels;
- Telegram-посевы;
- взаимопиар;
- рекомендации;
- сохранения и пересылки.

Цель:
не просто много подписчиков, а активная аудитория,
которая смотрит, отвечает и покупает.

Не предлагай накрутку.
"""

    result = await ai_request(prompt)

    await update.message.reply_text(result)


async def what_to_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = """
Ты контент-менеджер Telegram-магазина оригинальной косметики
и парфюмерии.

Предложи 10 идей, что можно опубликовать сегодня,
если владелица не хочет делать обычный рекламный пост.

Идеи должны быть:
- интересными;
- сохраняемыми;
- вовлекающими;
- связанными с beauty;
- без лица;
- без выдуманных личных историй.
"""

    result = await ai_request(prompt)

    await update.message.reply_text(result)


# ============================================================
# PUBLISHER
# ============================================================

async def publish_queue():
    """
    Фоновый цикл.

    Публикует только:
    status='queued'
    и scheduled_at <= now.

    То есть неподтверждённые материалы никогда сюда
    не попадают.
    """

    while True:
        try:
            rows = db_execute(
                """
                SELECT *
                FROM queue
                WHERE status = 'queued'
                ORDER BY scheduled_at
                LIMIT 10
                """,
                commit=False,
            ).fetchall()

            for row in rows:
                user_id = row["user_id"]

                config = schedule_config(user_id)

                if not config["enabled"]:
                    continue

                channel_id = get_channel_id(user_id)

                if not channel_id:
                    logger.warning(
                        "No channel connected for user %s",
                        user_id,
                    )
                    continue

                tz = ZoneInfo(config["timezone"])

                try:
                    scheduled = datetime.fromisoformat(
                        row["scheduled_at"]
                    )

                    if scheduled.tzinfo is None:
                        scheduled = scheduled.replace(tzinfo=tz)

                    current = datetime.now(tz)

                    if scheduled > current:
                        continue

                except Exception:
                    continue

                try:
                    if row["kind"] == "product":
                        await publish_product(row, channel_id)

                    elif row["kind"] == "post":
                        await publish_text_post(row, channel_id)

                    db_execute(
                        """
                        UPDATE queue
                        SET status = 'published',
                            published_at = ?
                        WHERE id = ?
                        """,
                        (
                            now_iso(),
                            row["id"],
                        ),
                    )

                    # Сообщаем владельцу.
                    try:
                        await bot_app.bot.send_message(
                            chat_id=user_id,
                            text=(
                                "📢 Опубликовано!\n\n"
                                f"Публикация #{row['id']} "
                                "вышла в канале."
                            ),
                        )
                    except Exception:
                        pass

                except Exception as e:
                    logger.exception(
                        "Publishing failed for queue item %s",
                        row["id"],
                    )

                    # Не удаляем.
                    # Оставляем в очереди для повторной попытки.

        except Exception:
            logger.exception("Queue worker error")

        await asyncio.sleep(30)


async def publish_product(row, channel_id):
    photo1 = row["photo1"]
    photo2 = row["photo2"]
    caption = row["caption"] or ""

    media = [
        InputMediaPhoto(
            media=photo1,
            caption=caption,
        ),
        InputMediaPhoto(
            media=photo2,
        ),
    ]

    await bot_app.bot.send_media_group(
        chat_id=channel_id,
        media=media,
    )


async def publish_text_post(row, channel_id):
    text_content = row["text_content"] or ""

    await bot_app.bot.send_message(
        chat_id=channel_id,
        text=text_content,
    )


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    # --------------------------------------------------------
    # CONNECTED CHANNEL FORWARD
    # --------------------------------------------------------

    if await handle_forwarded_channel_post(update, context):
        return

    # --------------------------------------------------------
    # MAIN BUTTONS
    # --------------------------------------------------------

    if text == "🛍 Новый товар":
        await new_product(update, context)
        return

    if text == "✍️ Новый пост":
        await generate_post(update, context)
        return

    if text == "📅 Контент-план":
        await content_plan(update, context)
        return

    if text == "⏰ Расписание":
        await schedule_menu(update, context)
        return

    if text == "📋 Очередь":
        await queue_text(update, context)
        return

    if text == "📊 Идеи продвижения":
        await growth_ideas(update, context)
        return

    if text == "🎬 Reels / TikTok":
        await generate_reels(update, context)
        return

    if text == "📱 Stories":
        await generate_stories(update, context)
        return

    if text == "📊 Опрос":
        await create_poll(update, context)
        return

    if text == "💡 Что опубликовать?":
        await what_to_post(update, context)
        return

    if text == "⚙️ Настройки":
        await settings(update, context)
        return

    # --------------------------------------------------------
    # STATE ROUTING
    # --------------------------------------------------------

    state = state_for(user_id)

    if state.get("mode") == "product":
        await handle_product_text(update, context)
        return

    if state.get("mode") == "edit_product":
        await handle_edit_product(update, context)
        return

    if state.get("mode") == "post_prompt":
        await handle_post_prompt(update, context)
        return

    if state.get("mode") == "post_edit":
        draft = state.get("draft_post")

        if draft:
            prompt = f"""
Отредактируй этот Telegram-пост.

Исходный текст:
{draft}

Пожелание:
{text}

Верни только готовый текст поста.
Без ссылок и источников.
"""

            new_text = escape_caption(await ai_request(prompt))

            state["draft_post"] = new_text

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ В очередь",
                            callback_data="post_approve",
                        ),
                        InlineKeyboardButton(
                            "✏️ Изменить",
                            callback_data="post_edit",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Отмена",
                            callback_data="post_cancel",
                        )
                    ],
                ]
            )

            state["mode"] = "post_prompt"

            await update.message.reply_text(
                new_text,
                reply_markup=keyboard,
            )

        return

    if state.get("mode") == "schedule_times":
        await handle_schedule_times(update, context)
        return

    if state.get("mode") == "schedule_days":
        await handle_schedule_days(update, context)
        return

    if state.get("mode") == "poll":
        await handle_poll(update, context)
        return

    if state.get("mode") == "connect_channel":
        await update.message.reply_text(
            "Для подключения канала нужно именно "
            "переслать мне пост из канала 📢"
        )
        return

    # --------------------------------------------------------
    # CATEGORY QUICK OVERRIDE
    # --------------------------------------------------------

    draft = state.get("draft")

    if draft and text.startswith("#"):
        draft["category"] = normalize_category(text)
        draft["caption"] = build_product_caption(draft)

        await update.message.reply_text(
            "Категорию изменила 💗\n\n"
            + draft["caption"],
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Одобрить",
                            callback_data="product_approve",
                        ),
                        InlineKeyboardButton(
                            "✏️ Изменить",
                            callback_data="product_edit",
                        ),
                    ]
                ]
            ),
        )


# ============================================================
# PHOTO ROUTER
# ============================================================

async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = state_for(user_id)

    if state.get("waiting_new_photo"):
        photo = update.message.photo[-1]

        state["draft"]["photo1"] = photo.file_id
        state["draft"]["caption"] = build_product_caption(
            state["draft"]
        )

        state["waiting_new_photo"] = False

        await update.message.reply_text(
            "🖼 Фото заменено!\n\n"
            "Ниже обновлённая карточка."
        )

        await context.bot.send_photo(
            chat_id=user_id,
            photo=photo.file_id,
            caption=state["draft"]["caption"],
        )

        await update.message.reply_text(
            "Проверь карточку:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Одобрить",
                            callback_data="product_approve",
                        ),
                        InlineKeyboardButton(
                            "✏️ Изменить",
                            callback_data="product_edit",
                        ),
                    ]
                ]
            ),
        )

        return

    if state.get("mode") == "product":
        await handle_product_photo(update, context)


# ============================================================
# SETTINGS
# ============================================================

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    config = schedule_config(user_id)
    channel_id = get_channel_id(user_id)

    await update.message.reply_text(
        "⚙️ **Настройки**\n\n"
        f"📢 Канал: {channel_id or 'не подключён'}\n"
        f"🌍 Часовой пояс: {config['timezone']}\n"
        f"⏰ Расписание: "
        f"{'включено' if config['enabled'] else 'выключено'}\n\n"
        "Для изменения расписания открой «⏰ Расписание».",
        parse_mode="Markdown",
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data

    if data.startswith("product_"):
        await callbacks(update, context)
        return

    if data.startswith("post_"):
        await handle_post_callback(update, context)
        return

    if data == "schedule_setup":
        await update.callback_query.answer()

        user_id = update.callback_query.from_user.id
        state_for(user_id)["mode"] = "schedule_times"

        await update.callback_query.message.reply_text(
            "⚙️ Шаг 1.\n\n"
            "Напиши время публикаций через запятую.\n\n"
            "Например:\n"
            "10:00, 15:00, 20:00"
        )
        return

    if data == "schedule_queue":
        await update.callback_query.answer()

        fake_update = update
        await queue_text_from_callback(fake_update)
        return

    if data == "schedule_on":
        await update.callback_query.answer()

        user_id = update.callback_query.from_user.id

        db_execute(
            """
            UPDATE users
            SET schedule_enabled = 1
            WHERE user_id = ?
            """,
            (user_id,),
        )

        await update.callback_query.message.reply_text(
            "▶️ Расписание включено!\n\n"
            "Публиковаться будут только материалы, "
            "которые ты одобрила."
        )
        return

    if data == "schedule_off":
        await update.callback_query.answer()

        user_id = update.callback_query.from_user.id

        db_execute(
            """
            UPDATE users
            SET schedule_enabled = 0
            WHERE user_id = ?
            """,
            (user_id,),
        )

        await update.callback_query.message.reply_text(
            "⏸ Расписание поставлено на паузу.\n\n"
            "Очередь сохранится."
        )
        return

    if data == "connect_channel":
        await update.callback_query.answer()

        user_id = update.callback_query.from_user.id
        state_for(user_id)["mode"] = "connect_channel"

        await update.callback_query.message.reply_text(
            "📢 Добавь меня администратором канала "
            "с правом публикации.\n\n"
            "После этого перешли сюда любой пост из канала."
        )
        return


async def queue_text_from_callback(update):
    query = update.callback_query
    user_id = query.from_user.id

    rows = db_execute(
        """
        SELECT *
        FROM queue
        WHERE user_id = ?
        AND status = 'queued'
        ORDER BY scheduled_at
        LIMIT 20
        """,
        (user_id,),
        commit=False,
    ).fetchall()

    if not rows:
        await query.message.reply_text(
            "📋 Очередь пока пустая."
        )
        return

    lines = ["📋 Очередь:\n"]

    for row in rows:
        title = row["caption"] or row["text_content"] or "Публикация"
        first_line = title.splitlines()[0][:80]

        lines.append(
            f"#{row['id']} — {row['scheduled_at']}\n"
            f"{first_line}\n"
        )

    await query.message.reply_text(
        "\n".join(lines)
    )


# ============================================================
# FASTAPI
# ============================================================

@app.get("/")
async def health():
    return {
        "status": "ok",
        "service": "Beauty Manager Bot",
    }


# ============================================================
# TELEGRAM APP
# ============================================================

bot_app = (
    Application.builder()
    .token(BOT_TOKEN)
    .build()
)


bot_app.add_handler(
    CommandHandler("start", start)
)

bot_app.add_handler(
    CommandHandler("schedule", schedule_menu)
)

bot_app.add_handler(
    CommandHandler("queue", queue_text)
)

bot_app.add_handler(
    CallbackQueryHandler(callback_router)
)

bot_app.add_handler(
    MessageHandler(
        filters.PHOTO,
        photo_router,
    )
)

bot_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_router,
    )
)


scheduler_task = None


@app.on_event("startup")
async def startup_event():
    global scheduler_task

    logger.info("Starting Telegram bot...")

    await bot_app.initialize()
    await bot_app.start()

    await bot_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES
    )

    scheduler_task = asyncio.create_task(
        publish_queue()
    )

    logger.info("Beauty Manager Bot started.")


@app.on_event("shutdown")
async def shutdown_event():
    global scheduler_task

    if scheduler_task:
        scheduler_task.cancel()

        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass

    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()

    logger.info("Beauty Manager Bot stopped.")
