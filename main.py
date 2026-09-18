import os
import re
import io
import json
import time
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

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
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Можно изменить через Render Environment Variables.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

# Канал можно добавить позже.
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()

# Через сколько секунд после фото начинаем обработку.
# Это позволяет пользователю успеть написать цену вторым сообщением.
PHOTO_WAIT_SECONDS = 1.8

# Сколько секунд ждём вторую фотографию.
SECOND_PHOTO_WAIT_SECONDS = 1.8

DB_PATH = os.getenv("DB_PATH", "beauty_manager.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("beauty_manager")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")


openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

app = FastAPI(title="Beauty Manager Bot")


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    conn = db_connect()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT,
            brand TEXT,
            category TEXT,
            description TEXT,
            volume TEXT,
            price INTEGER,
            stock INTEGER,
            photo1 TEXT,
            photo2 TEXT,
            caption TEXT,
            status TEXT DEFAULT 'draft',
            created_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            posts_per_day INTEGER DEFAULT 1,
            times TEXT DEFAULT '12:00',
            days TEXT DEFAULT '0,1,2,3,4,5,6',
            timezone TEXT DEFAULT 'Europe/Moscow',
            paused INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            user_id INTEGER,
            publish_at TEXT,
            status TEXT DEFAULT 'scheduled',
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# USER STATE
# ============================================================

# В памяти держим только текущий процесс добавления товара.
# Самые важные данные продукта сохраняются в SQLite.
USER_STATE: Dict[int, Dict[str, Any]] = {}

# Задачи ожидания для debounce.
PROCESS_TASKS: Dict[int, asyncio.Task] = {}


def get_state(user_id: int) -> Dict[str, Any]:
    if user_id not in USER_STATE:
        USER_STATE[user_id] = {
            "photos": [],
            "photo_captions": [],
            "text_parts": [],
            "price": None,
            "stock": None,
            "name_hint": None,
            "waiting_for": None,
            "processing": False,
            "last_activity": time.time(),
        }

    return USER_STATE[user_id]


def reset_state(user_id: int):
    USER_STATE[user_id] = {
        "photos": [],
        "photo_captions": [],
        "text_parts": [],
        "price": None,
        "stock": None,
        "name_hint": None,
        "waiting_for": None,
        "processing": False,
        "last_activity": time.time(),
    }


# ============================================================
# PRICE / STOCK PARSER
# ============================================================

PRICE_PATTERNS = [
    r"(?<!\d)(\d{1,3}(?:[\s.,]\d{3})+)\s*(?:₽|руб(?:\.|лей)?|р(?:\.|$))",
    r"(?<!\d)(\d{3,6})\s*(?:₽|руб(?:\.|лей)?|р(?:\.|$))",
    r"(?:цена|стоимость)\s*[:\-]?\s*(\d{3,6})",
]

STOCK_PATTERNS = [
    r"(?:в наличии|наличие)\s*[:\-]?\s*(\d+)",
    r"(\d+)\s*(?:шт|штук)",
]


def parse_price(text: str) -> Optional[int]:
    if not text:
        return None

    text_lower = text.lower().replace("\u00a0", " ")

    for pattern in PRICE_PATTERNS:
        match = re.search(pattern, text_lower, re.IGNORECASE)

        if match:
            raw = match.group(1)

            raw = raw.replace(" ", "")
            raw = raw.replace(".", "")
            raw = raw.replace(",", "")

            try:
                value = int(raw)

                # Защита от случайного распознавания номера оттенка,
                # года и т.п.
                if 100 <= value <= 999999:
                    return value
            except ValueError:
                pass

    return None


def parse_stock(text: str) -> Optional[int]:
    if not text:
        return None

    text_lower = text.lower()

    for pattern in STOCK_PATTERNS:
        match = re.search(pattern, text_lower, re.IGNORECASE)

        if match:
            try:
                return int(match.group(1))
            except ValueError:
                pass

    return None


def remove_price_stock(text: str) -> str:
    if not text:
        return ""

    result = text

    for pattern in PRICE_PATTERNS:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)

    for pattern in STOCK_PATTERNS:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)

    return result.strip(" ,.-\n")


# ============================================================
# CATEGORY
# ============================================================

CATEGORY_HASHTAGS = {
    "парфюм": "#парфюм",
    "парфюмерия": "#парфюм",
    "духи": "#парфюм",
    "аромат": "#парфюм",

    "тональный крем": "#тональный_крем",
    "тон": "#тональный_крем",
    "тональная основа": "#тональный_крем",
    "foundation": "#тональный_крем",

    "консилер": "#консилер",
    "корректор": "#консилер",

    "румяна": "#румяна",
    "blush": "#румяна",

    "бронзер": "#бронзер",
    "бронзатор": "#бронзер",
    "bronzer": "#бронзер",

    "пудра": "#пудра",
    "powder": "#пудра",

    "тени": "#тени",
    "палетка теней": "#тени",
    "eyeshadow": "#тени",

    "тушь": "#тушь",
    "mascara": "#тушь",

    "помада": "#помада",
    "lipstick": "#помада",

    "блеск для губ": "#блеск_для_губ",
    "блеск": "#блеск_для_губ",
    "lip gloss": "#блеск_для_губ",

    "карандаш для губ": "#карандаш_для_губ",
    "карандаш": "#карандаш",

    "крем": "#крем",
    "крем для лица": "#крем_для_лица",

    "сыворотка": "#сыворотка",

    "маска": "#маска",

    "очищение": "#очищение",
    "очищающее средство": "#очищение",

    "шампунь": "#шампунь",
    "кондиционер": "#кондиционер",

    "набор": "#набор",
    "комплект": "#набор",

    "уход": "#уход",
    "косметика": "#косметика",
}


def normalize_category(category: str) -> str:
    if not category:
        return "#косметика"

    clean = category.strip().lower()

    if clean.startswith("#"):
        return clean.replace(" ", "_")

    for key, hashtag in CATEGORY_HASHTAGS.items():
        if key in clean:
            return hashtag

    clean = re.sub(r"[^а-яa-z0-9]+", "_", clean)
    clean = clean.strip("_")

    if not clean:
        return "#косметика"

    return "#" + clean


# ============================================================
# TEXT CLEANING
# ============================================================

def clean_ai_text(text: str) -> str:
    if not text:
        return ""

    text = text.strip()

    # Убираем markdown-ссылки.
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Убираем обычные URL.
    text = re.sub(r"https?://\S+", "", text)

    # Убираем строки с источниками.
    lines = []

    for line in text.splitlines():
        low = line.lower().strip()

        if (
            low.startswith("источник")
            or low.startswith("sources")
            or low.startswith("source")
            or "http://" in low
            or "https://" in low
        ):
            continue

        lines.append(line)

    text = "\n".join(lines)

    # Слишком много пустых строк.
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def limit_caption(text: str, limit: int = 1000) -> str:
    text = clean_ai_text(text)

    if len(text) <= limit:
        return text

    return text[:limit - 3].rstrip() + "..."


# ============================================================
# TELEGRAM PHOTO -> BYTES
# ============================================================

async def telegram_photo_to_bytes(
    bot,
    file_id: str
) -> bytes:

    telegram_file = await bot.get_file(file_id)

    buffer = io.BytesIO()

    await telegram_file.download_to_memory(buffer)

    return buffer.getvalue()


def bytes_to_data_url(image_bytes: bytes) -> str:
    import base64

    encoded = base64.b64encode(image_bytes).decode("utf-8")

    return f"data:image/jpeg;base64,{encoded}"


# ============================================================
# OPENAI PRODUCT RECOGNITION
# ============================================================

PRODUCT_SYSTEM_PROMPT = """
Ты — ассистент магазина оригинальной косметики и парфюмерии.

Твоя задача:
1. Определить товар по фотографии максимально точно.
2. Прочитать надписи на упаковке.
3. Если название бренда/товара видно не полностью — не выдумывать.
4. Использовать веб-поиск только для проверки найденного товара.
5. Если товар найден уверенно, проверить официальную информацию.
6. Не путать оттенок, объём, концентрацию или версию продукта.
7. Не использовать цену из интернета как цену продавца.
8. Не использовать наличие из интернета.
9. Цена и наличие продавца будут переданы отдельно.

Особенно важно:
- сначала анализируй саму фотографию;
- не называй случайный похожий товар;
- если есть сомнение, укажи confidence ниже;
- описание должно быть коротким и пригодным для Telegram.

Верни ТОЛЬКО валидный JSON без markdown.

Формат:

{
  "brand": "...",
  "name": "...",
  "category": "...",
  "volume": "...",
  "shade": "...",
  "item_type": "single|set",
  "description": "...",
  "confidence": 0.0,
  "verified": true,
  "notes": "..."
}

category должна быть обычным названием категории:
например:
"парфюм"
"тональный крем"
"румяна"
"бронзер"
"пудра"
"тени"
"тушь"
"помада"
"крем"
"сыворотка"
"набор"

Не добавляй # к category.
"""


async def recognize_product(
    image_bytes: bytes,
    user_hint: str = "",
) -> Dict[str, Any]:

    image_data_url = bytes_to_data_url(image_bytes)

    user_text = """
Определи товар на изображении.

Информация от продавца:
%s

Если в этой информации есть цена или наличие, НЕ используй их для определения товара.
""" % (user_hint or "нет дополнительной информации")

    try:
        response = await openai_client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": PRODUCT_SYSTEM_PROMPT,
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": user_text,
                        },
                        {
                            "type": "input_image",
                            "image_url": image_data_url,
                            "detail": "auto",
                        },
                    ],
                },
            ],
            max_output_tokens=700,
        )

        raw = response.output_text.strip()

        # Если модель вдруг обернула JSON в ```json
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        data = json.loads(raw)

        return data

    except Exception as e:
        logger.exception("Product recognition error: %s", e)

        return {
            "brand": "",
            "name": "",
            "category": "косметика",
            "volume": "",
            "shade": "",
            "item_type": "single",
            "description": "",
            "confidence": 0,
            "verified": False,
            "notes": str(e),
        }


# ============================================================
# WEB VERIFICATION
# ============================================================

async def verify_product_with_web(
    product: Dict[str, Any]
) -> Dict[str, Any]:

    name = product.get("name", "")
    brand = product.get("brand", "")

    if not name:
        return product

    query = f"""
Проверь информацию о косметическом/парфюмерном товаре:
{brand} {name}

Нужно проверить:
- точное название;
- бренд;
- категорию;
- объём;
- назначение;
- основные характеристики.

Предпочитай официальный сайт бренда.
Если официального сайта нет, используй крупного надёжного продавца.

Не меняй товар на похожий.
Не указывай цену продавца.
Не указывай наличие.
"""

    try:
        response = await openai_client.responses.create(
            model=OPENAI_MODEL,
            tools=[
                {
                    "type": "web_search",
                    "search_context_size": "low",
                }
            ],
            input=query,
            max_output_tokens=600,
        )

        text = response.output_text.strip()

        # Просим модель аккуратно применить проверку к имеющемуся объекту.
        merge_prompt = f"""
Ниже данные, полученные с фотографии:

{json.dumps(product, ensure_ascii=False)}

Ниже результаты проверки:

{text}

Верни ТОЛЬКО JSON:

{{
  "brand": "...",
  "name": "...",
  "category": "...",
  "volume": "...",
  "shade": "...",
  "description": "...",
  "confidence": 0.0
}}

Если веб-результат не подтверждает товар, сохрани данные фотографии.
Не меняй товар на похожий.
Не добавляй цену или наличие.
"""

        merged = await openai_client.responses.create(
            model=OPENAI_MODEL,
            input=merge_prompt,
            max_output_tokens=500,
        )

        raw = merged.output_text.strip()

        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        verified = json.loads(raw)

        product.update({
            "brand": verified.get("brand") or product.get("brand", ""),
            "name": verified.get("name") or product.get("name", ""),
            "category": verified.get("category") or product.get("category", ""),
            "volume": verified.get("volume") or product.get("volume", ""),
            "shade": verified.get("shade") or product.get("shade", ""),
            "description": verified.get("description") or product.get("description", ""),
            "confidence": verified.get(
                "confidence",
                product.get("confidence", 0)
            ),
        })

    except Exception as e:
        logger.warning("Web verification failed: %s", e)

    return product


# ============================================================
# PRODUCT CAPTION
# ============================================================

def build_caption(
    product: Dict[str, Any],
    price: int,
    stock: int,
) -> str:

    brand = product.get("brand", "").strip()
    name = product.get("name", "").strip()

    category = normalize_category(
        product.get("category", "косметика")
    )

    description = clean_ai_text(
        product.get("description", "")
    )

    volume = product.get("volume", "").strip()
    shade = product.get("shade", "").strip()

    title_parts = []

    if brand:
        title_parts.append(brand)

    if name and name.lower() not in brand.lower():
        title_parts.append(name)

    title = " ".join(title_parts).strip()

    if not title:
        title = "Товар"

    lines = [
        category,
        "",
        f"✨ {title}",
    ]

    if description:
        lines.extend([
            "",
            description,
        ])

    extra = []

    if volume:
        extra.append(f"Объём: {volume}")

    if shade:
        extra.append(f"Оттенок: {shade}")

    if extra:
        lines.extend([
            "",
            " • ".join(extra),
        ])

    lines.extend([
        "",
        f"💰 Цена: {price:,} ₽".replace(",", " "),
        f"📦 В наличии: {stock} шт.",
    ])

    return limit_caption("\n".join(lines))


# ============================================================
# APPROVAL KEYBOARD
# ============================================================

def approval_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Одобрить",
                callback_data=f"approve:{product_id}",
            ),
            InlineKeyboardButton(
                "✏️ Изменить",
                callback_data=f"edit:{product_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🖼️ Другое фото",
                callback_data=f"photo:{product_id}",
            ),
            InlineKeyboardButton(
                "❌ Удалить",
                callback_data=f"cancel:{product_id}",
            ),
        ],
    ])


# ============================================================
# SAVE PRODUCT
# ============================================================

def save_product(
    user_id: int,
    product: Dict[str, Any],
    price: int,
    stock: int,
    photo1: Optional[str],
    photo2: Optional[str],
    caption: str,
) -> int:

    conn = db_connect()

    cursor = conn.execute(
        """
        INSERT INTO products (
            user_id,
            name,
            brand,
            category,
            description,
            volume,
            price,
            stock,
            photo1,
            photo2,
            caption,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            product.get("name", ""),
            product.get("brand", ""),
            normalize_category(product.get("category", "")),
            product.get("description", ""),
            product.get("volume", ""),
            price,
            stock,
            photo1,
            photo2,
            caption,
            "draft",
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    product_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return product_id


# ============================================================
# PROCESS PRODUCT
# ============================================================

async def process_current_product(
    user_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
):

    state = get_state(user_id)

    if state["processing"]:
        return

    state["processing"] = True

    try:
        # Даём возможность последнему сообщению попасть в state.
        await asyncio.sleep(0.1)

        photos = state["photos"]

        if not photos:
            state["processing"] = False
            return

        price = state.get("price")
        stock = state.get("stock")

        # ----------------------------------------------------
        # PRICE
        # ----------------------------------------------------

        if price is None:
            state["processing"] = False
            state["waiting_for"] = "price"

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "💰 Я получила фотографию, но не нашла цену.\n\n"
                    "Напиши, например:\n"
                    "3500 ₽\n\n"
                    "или:\n"
                    "3500 ₽, в наличии 2 шт."
                ),
            )

            return

        # ----------------------------------------------------
        # STOCK
        # ----------------------------------------------------

        if stock is None:
            stock = 1

        # ----------------------------------------------------
        # SHOW PROCESSING
        # ----------------------------------------------------

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action="typing",
        )

        # ----------------------------------------------------
        # GET FIRST IMAGE
        # ----------------------------------------------------

        first_photo = photos[0]

        image_bytes = await telegram_photo_to_bytes(
            context.bot,
            first_photo,
        )

        # ----------------------------------------------------
        # RECOGNITION
        # ----------------------------------------------------

        user_text = " ".join(
            state.get("text_parts", [])
        ).strip()

        recognition = await recognize_product(
            image_bytes=image_bytes,
            user_hint=user_text,
        )

        # ----------------------------------------------------
        # OPTIONAL WEB CHECK
        # ----------------------------------------------------

        confidence = float(
            recognition.get("confidence", 0) or 0
        )

        if recognition.get("name") and confidence >= 0.55:
            recognition = await verify_product_with_web(
                recognition
            )

        # ----------------------------------------------------
        # IF PRODUCT NOT RECOGNIZED
        # ----------------------------------------------------

        name = recognition.get("name", "").strip()

        if not name:

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "😕 Я не смогла уверенно определить товар по фото.\n\n"
                    "Попробуй прислать фотографию, где хорошо видно "
                    "название бренда и продукта."
                ),
            )

            state["processing"] = False
            return

        # ----------------------------------------------------
        # CATEGORY
        # ----------------------------------------------------

        recognition["category"] = normalize_category(
            recognition.get("category", "")
        )

        # ----------------------------------------------------
        # DESCRIPTION FALLBACK
        # ----------------------------------------------------

        if not recognition.get("description"):
            recognition["description"] = (
                f"{recognition.get('brand', '')} "
                f"{recognition.get('name', '')}"
            ).strip()

        # ----------------------------------------------------
        # PHOTOS
        # ----------------------------------------------------

        photo1 = photos[0]

        photo2 = None

        if len(photos) >= 2:
            photo2 = photos[1]

        # ----------------------------------------------------
        # CAPTION
        # ----------------------------------------------------

        caption = build_caption(
            recognition,
            price,
            stock,
        )

        # ----------------------------------------------------
        # SAVE
        # ----------------------------------------------------

        product_id = save_product(
            user_id=user_id,
            product=recognition,
            price=price,
            stock=stock,
            photo1=photo1,
            photo2=photo2,
            caption=caption,
        )

        # ----------------------------------------------------
        # SEND RESULT
        # ----------------------------------------------------

        keyboard = approval_keyboard(product_id)

        if photo2:
            # Telegram albums позволяют отправить несколько фото.
            # Caption ставим на первое фото.
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
                chat_id=chat_id,
                media=media,
            )

            await context.bot.send_message(
                chat_id=chat_id,
                text="Проверь карточку товара 👆",
                reply_markup=keyboard,
            )

        else:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo1,
                caption=caption,
                reply_markup=keyboard,
            )

        # ----------------------------------------------------
        # RESET
        # ----------------------------------------------------

        reset_state(user_id)

    except Exception as e:

        logger.exception(
            "process_current_product error: %s",
            e,
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Что-то пошло не так при обработке товара.\n\n"
                "Попробуй отправить фото ещё раз."
            ),
        )

        reset_state(user_id)


# ============================================================
# DEBOUNCE
# ============================================================

def schedule_product_processing(
    user_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    delay: float = PHOTO_WAIT_SECONDS,
):

    old_task = PROCESS_TASKS.get(user_id)

    if old_task and not old_task.done():
        old_task.cancel()

    async def delayed():
        try:
            await asyncio.sleep(delay)

            state = get_state(user_id)

            # Если за это время ничего не пришло — обрабатываем.
            await process_current_product(
                user_id,
                chat_id,
                context,
            )

        except asyncio.CancelledError:
            pass

        except Exception:
            logger.exception(
                "Delayed product processing error"
            )

    task = asyncio.create_task(delayed())

    PROCESS_TASKS[user_id] = task


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    reset_state(user_id)

    await update.message.reply_text(
        "Привет! 💕\n\n"
        "Я помогу подготовить карточку товара.\n\n"
        "Просто отправь мне:\n\n"
        "📷 фото товара\n"
        "💰 цену\n"
        "📦 количество, если оно больше 1\n\n"
        "Например:\n"
        "«3500 ₽, в наличии 2 шт.»\n\n"
        "Можно написать цену прямо в подписи к фотографии "
        "или следующим сообщением — я пойму оба варианта."
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "Как работать со мной:\n\n"
        "📷 Отправь фото товара.\n"
        "💰 Напиши цену.\n"
        "📦 При необходимости укажи количество.\n\n"
        "Можно сделать так:\n"
        "Фото + подпись «3500 ₽, в наличии 2 шт.»\n\n"
        "Или:\n"
        "Фото\n"
        "3500 ₽\n\n"
        "Я сама распознаю товар, определю категорию "
        "и подготовлю карточку."
    )


# ============================================================
# PHOTO HANDLER
# ============================================================

async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message or not update.message.photo:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    state = get_state(user_id)

    # Самая большая фотография.
    photo = update.message.photo[-1]

    state["photos"].append(photo.file_id)

    state["last_activity"] = time.time()

    # --------------------------------------------------------
    # ТЕКСТ ПОД ФОТО
    # --------------------------------------------------------

    caption = (
        update.message.caption
        or ""
    ).strip()

    if caption:
        state["photo_captions"].append(caption)
        state["text_parts"].append(caption)

        parsed_price = parse_price(caption)

        if parsed_price is not None:
            state["price"] = parsed_price

        parsed_stock = parse_stock(caption)

        if parsed_stock is not None:
            state["stock"] = parsed_stock

    # --------------------------------------------------------
    # ЕСЛИ ПРИШЛА ВТОРАЯ ФОТОГРАФИЯ
    # --------------------------------------------------------

    if len(state["photos"]) >= 2:

        # Не надо ждать ещё долго.
        schedule_product_processing(
            user_id=user_id,
            chat_id=chat_id,
            context=context,
            delay=0.5,
        )

        return

    # --------------------------------------------------------
    # ЖДЁМ ЦЕНУ / ВТОРУЮ ФОТОГРАФИЮ
    # --------------------------------------------------------

    schedule_product_processing(
        user_id=user_id,
        chat_id=chat_id,
        context=context,
        delay=PHOTO_WAIT_SECONDS,
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = (
        update.message.text
        or ""
    ).strip()

    if not text:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    state = get_state(user_id)

    # --------------------------------------------------------
    # ЕСЛИ ЖДЁМ ЦЕНУ
    # --------------------------------------------------------

    parsed_price = parse_price(text)
    parsed_stock = parse_stock(text)

    # Если пользователь прислал цену.
    if parsed_price is not None:

        state["price"] = parsed_price

        if parsed_stock is not None:
            state["stock"] = parsed_stock

        state["text_parts"].append(text)
        state["last_activity"] = time.time()

        # Если фото уже есть — запускаем обработку.
        if state["photos"]:

            schedule_product_processing(
                user_id=user_id,
                chat_id=chat_id,
                context=context,
                delay=0.4,
            )

            return

    # --------------------------------------------------------
    # ЕСЛИ ПРИСЛАЛИ ТОЛЬКО КОЛИЧЕСТВО
    # --------------------------------------------------------

    if parsed_stock is not None:

        state["stock"] = parsed_stock
        state["text_parts"].append(text)
        state["last_activity"] = time.time()

        if state["photos"]:

            schedule_product_processing(
                user_id=user_id,
                chat_id=chat_id,
                context=context,
                delay=0.4,
            )

            return

    # --------------------------------------------------------
    # ЕСЛИ ЭТО ОБЫЧНЫЙ ТЕКСТ ПОСЛЕ ФОТО
    # --------------------------------------------------------

    if state["photos"]:

        state["text_parts"].append(text)
        state["last_activity"] = time.time()

        # Цена могла быть написана нестандартно.
        # Например: "моя цена 3500"
        if "цена" in text.lower():

            numbers = re.findall(
                r"\b\d{3,6}\b",
                text.replace(" ", ""),
            )

            if numbers:
                try:
                    value = int(numbers[-1])

                    if 100 <= value <= 999999:
                        state["price"] = value
                except ValueError:
                    pass

        if state.get("price") is not None:

            schedule_product_processing(
                user_id=user_id,
                chat_id=chat_id,
                context=context,
                delay=0.5,
            )

            return

        # Если цена пока не найдена — ждём немного.
        schedule_product_processing(
            user_id=user_id,
            chat_id=chat_id,
            context=context,
            delay=PHOTO_WAIT_SECONDS,
        )

        return

    # --------------------------------------------------------
    # ТЕКСТ БЕЗ ФОТО
    # --------------------------------------------------------

    await update.message.reply_text(
        "📷 Сначала отправь мне фотографию товара.\n\n"
        "Цену можешь написать прямо в подписи к фото "
        "или следующим сообщением."
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    data = query.data or ""

    try:
        action, raw_id = data.split(":", 1)
        product_id = int(raw_id)
    except Exception:
        return

    # --------------------------------------------------------
    # APPROVE
    # --------------------------------------------------------

    if action == "approve":

        conn = db_connect()

        product = conn.execute(
            "SELECT * FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()

        if product:
            conn.execute(
                """
                UPDATE products
                SET status = 'approved'
                WHERE id = ?
                """,
                (product_id,),
            )

            conn.commit()

        conn.close()

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "✅ Товар одобрен.\n\n"
            "Теперь его можно поставить в очередь "
            "на публикацию."
        )

        return

    # --------------------------------------------------------
    # CANCEL
    # --------------------------------------------------------

    if action == "cancel":

        conn = db_connect()

        conn.execute(
            """
            UPDATE products
            SET status = 'deleted'
            WHERE id = ?
            """,
            (product_id,),
        )

        conn.commit()
        conn.close()

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "🗑 Товар удалён из черновиков."
        )

        return

    # --------------------------------------------------------
    # EDIT
    # --------------------------------------------------------

    if action == "edit":

        await query.message.reply_text(
            "✏️ Напиши, что именно изменить.\n\n"
            "Например:\n"
            "«Поменяй цену на 4200»\n"
            "«Название должно быть ...»\n"
            "«В наличии 3 шт.»"
        )

        return

    # --------------------------------------------------------
    # OTHER PHOTO
    # --------------------------------------------------------

    if action == "photo":

        await query.message.reply_text(
            "🖼 Пришли новое фото товара.\n\n"
            "Я использую его вместо текущего."
        )

        return


# ============================================================
# SCHEDULING
# ============================================================

def get_user_settings(user_id: int):

    conn = db_connect()

    row = conn.execute(
        """
        SELECT *
        FROM settings
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    if not row:

        conn.execute(
            """
            INSERT INTO settings (
                user_id,
                posts_per_day,
                times,
                days,
                timezone,
                paused
            )
            VALUES (?, 1, '12:00', '0,1,2,3,4,5,6',
                    'Europe/Moscow', 0)
            """,
            (user_id,),
        )

        conn.commit()

        row = conn.execute(
            """
            SELECT *
            FROM settings
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    conn.close()

    return row


async def schedule_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    settings = get_user_settings(user_id)

    await update.message.reply_text(
        "📅 Настройки публикаций\n\n"
        f"Постов в день: {settings['posts_per_day']}\n"
        f"Время: {settings['times']}\n"
        f"Дни недели: {settings['days']}\n"
        f"Часовой пояс: {settings['timezone']}\n\n"
        "Для изменения настроек позже можно будет "
        "использовать отдельное меню."
    )


# ============================================================
# QUEUE
# ============================================================

async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    conn = db_connect()

    rows = conn.execute(
        """
        SELECT
            q.id,
            q.publish_at,
            p.name,
            p.brand
        FROM queue q
        LEFT JOIN products p
            ON p.id = q.product_id
        WHERE q.user_id = ?
          AND q.status = 'scheduled'
        ORDER BY q.publish_at
        LIMIT 20
        """,
        (user_id,),
    ).fetchall()

    conn.close()

    if not rows:
        await update.message.reply_text(
            "📭 Очередь публикаций пока пустая."
        )
        return

    lines = ["📅 Очередь публикаций:\n"]

    for row in rows:

        title = " ".join(
            x for x in [
                row["brand"],
                row["name"],
            ]
            if x
        )

        lines.append(
            f"• {row['publish_at']}\n"
            f"  {title or 'Товар'}"
        )

    await update.message.reply_text(
        "\n\n".join(lines)
    )


# ============================================================
# MENU
# ============================================================

async def setup_menu(application: Application):

    commands = [
        ("start", "Начать"),
        ("help", "Помощь"),
        ("queue", "Очередь публикаций"),
        ("schedule", "Настройки публикаций"),
    ]

    await application.bot.set_my_commands(commands)

    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonCommands()
        )
    except Exception as e:
        logger.warning(
            "Could not set menu button: %s",
            e,
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Unhandled Telegram error: %s",
        context.error,
    )


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

telegram_application: Optional[Application] = None


async def start_bot():

    global telegram_application

    init_database()

    telegram_application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    telegram_application.add_handler(
        CommandHandler("start", start_command)
    )

    telegram_application.add_handler(
        CommandHandler("help", help_command)
    )

    telegram_application.add_handler(
        CommandHandler("queue", queue_command)
    )

    telegram_application.add_handler(
        CommandHandler("schedule", schedule_command)
    )

    telegram_application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    telegram_application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_handler,
        )
    )

    telegram_application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    telegram_application.add_error_handler(
        error_handler
    )

    await telegram_application.initialize()

    await telegram_application.start()

    await telegram_application.updater.start_polling(
        drop_pending_updates=True
    )

    await setup_menu(telegram_application)

    logger.info("Telegram bot started")

    while True:
        await asyncio.sleep(3600)


async def stop_bot():

    global telegram_application

    if telegram_application:

        try:
            await telegram_application.updater.stop()
        except Exception:
            pass

        try:
            await telegram_application.stop()
        except Exception:
            pass

        try:
            await telegram_application.shutdown()
        except Exception:
            pass


# ============================================================
# FASTAPI
# ============================================================

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
        "bot": bool(telegram_application),
    }


# ============================================================
# RENDER STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():

    asyncio.create_task(
        start_bot()
    )

    logger.info(
        "Beauty Manager started"
    )


@app.on_event("shutdown")
async def shutdown_event():

    await stop_bot()

    logger.info(
        "Beauty Manager stopped"
)
