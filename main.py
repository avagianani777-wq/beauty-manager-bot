import os
import re
import json
import base64
import logging
from io import BytesIO
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from fastapi import FastAPI

from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
    InputFile,
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
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

MODEL = "gpt-5.6-luna"


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()


# ============================================================
# OPENAI
# ============================================================

telegram_app = (
    Application.builder()
    .token(TOKEN)
    .build()
    if TOKEN
    else None
)

client = (
    AsyncOpenAI(api_key=OPENAI_API_KEY)
    if OPENAI_API_KEY
    else None
)


# ============================================================
# СИСТЕМНЫЙ ПРОМПТ
# ============================================================

SYSTEM_PROMPT = """
Ты — AI-ассистент Ани для Telegram-канала
«Косметика | Парфюм | Москва».

Канал продаёт оригинальную косметику, парфюмерию и средства ухода.

Твоя задача — помогать Ани создавать красивый, полезный и продающий
контент, но без ощущения обычного магазина-каталога.

СТИЛЬ КАНАЛА:

- живой;
- красивый;
- современный;
- дружелюбный;
- эстетичный;
- не слишком официальный;
- без агрессивных продаж;
- короткие и понятные тексты;
- русский язык.

ВАЖНО:

Никогда не придумывай характеристики товара.

Если информация о товаре неизвестна — так и скажи.

При работе с товаром используй:
1. фотографию пользователя;
2. информацию пользователя;
3. интернет-поиск, если он доступен.

ПРИОРИТЕТ ИНФОРМАЦИИ:

1. официальный сайт бренда;
2. официальный магазин;
3. крупные надёжные магазины;
4. другие источники — только для дополнительной проверки.

ЦЕНА И НАЛИЧИЕ:

Цена, которую прислала Аня, всегда важнее цены в интернете.

Количество товара, которое прислала Аня, всегда важнее информации
из интернета.

Никогда не меняй цену Ани на найденную в интернете.

Никогда не придумывай количество товара.

------------------------------------------------------------
ТОВАРНЫЕ КАРТОЧКИ
------------------------------------------------------------

Когда Аня присылает фотографию товара, цену и количество, нужно:

1. Определить товар по фотографии.
2. Проверить название.
3. Найти актуальную информацию о товаре.
4. Найти объём / оттенок / вариант, если он виден или подтверждается.
5. Написать короткое описание.
6. Использовать цену и наличие от Ани.

Карточка должна быть короткой, потому что она будет находиться
ПРЯМО ПОД ФОТОГРАФИЕЙ В TELEGRAM.

Формат:

✨ Название товара

Короткое красивое описание в 1–3 предложениях.

📏 Объём: ...
или
🎨 Оттенок: ...

💰 Цена: ...
📦 В наличии: ...

Максимальная длина карточки — примерно 750 символов.

НЕ добавляй:

- ссылки;
- URL;
- источники;
- блок «Источники»;
- «Источник:»;
- маркетплейсы;
- поисковые ссылки;
- технические комментарии;
- объяснения того, откуда взята информация.

Источники используются только для внутренней проверки.

------------------------------------------------------------
ПРАВИЛА КОНТЕНТА
------------------------------------------------------------

Примерное соотношение:

70% — полезный/интересный контент
20% — вовлечение и личная атмосфера
10% — прямые продажи.

Рубрики:

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

Идеи:

- 5 ароматов, которые пахнут дороже своей цены
- Что подарить девушке, если вообще не разбираешься в косметике
- 3 крема для тех, кто не любит липкость
- Как выбрать парфюм в подарок
- 5 красивых средств на туалетный столик
- Что купить до 5000 ₽
- Парфюм на разные случаи
- Выбираем вместе
- Что бы выбрала ты?
- Угадайте цену
- Сегодня приехало

Не используй агрессивные фразы вроде:

«КУПИТЕ СРОЧНО!!!»
«ТОЛЬКО СЕГОДНЯ!!!»

если это не подтверждено реальными условиями.

Не придумывай отзывы.

Не придумывай личный опыт Ани.

Если пишешь от первого лица, используй только реальные сведения,
которые дала Аня.
"""


# ============================================================
# ХРАНЕНИЕ ДАННЫХ
# ============================================================

USERS = {}


def get_user_data(user_id):
    if user_id not in USERS:
        USERS[user_id] = {
            "mode": None,

            "products": [],

            "last_photo": None,
            "last_photo_file_id": None,

            "last_product": None,

            "last_image_url": None,

            "draft": None,

            "pending_poll": None,
        }

    return USERS[user_id]


# ============================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================

def main_menu():
    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Новый товар",
                callback_data="new_product"
            ),
            InlineKeyboardButton(
                "📝 Пост",
                callback_data="create_post"
            ),
        ],
        [
            InlineKeyboardButton(
                "📱 Stories",
                callback_data="create_stories"
            ),
            InlineKeyboardButton(
                "🎬 Reels",
                callback_data="create_reels"
            ),
        ],
        [
            InlineKeyboardButton(
                "📅 План на сегодня",
                callback_data="daily_plan"
            ),
            InlineKeyboardButton(
                "📆 План на неделю",
                callback_data="weekly_plan"
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 Опрос",
                callback_data="poll_creator"
            ),
            InlineKeyboardButton(
                "💬 Вовлечение",
                callback_data="engagement"
            ),
        ],
        [
            InlineKeyboardButton(
                "💡 Что выложить?",
                callback_data="what_to_post"
            ),
            InlineKeyboardButton(
                "🚀 Рост канала",
                callback_data="growth"
            ),
        ],
        [
            InlineKeyboardButton(
                "🏷 Рубрики",
                callback_data="rubrics"
            ),
            InlineKeyboardButton(
                "📦 Мои товары",
                callback_data="show_products"
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


WELCOME = """
✨ <b>Привет, Ани!</b>

Я твой AI-ассистент для канала
<b>«Косметика | Парфюм | Москва»</b> 💄

Я могу помочь тебе:

📦 оформлять товары;
📝 писать посты;
📱 придумывать Stories;
🎬 создавать идеи для Reels;
📅 составлять контент-планы;
📊 делать опросы;
💬 придумывать вовлекающий контент;
🚀 помогать с ростом канала.

<b>Главное:</b>

Ты присылаешь мне фото товара + цену + количество.

Я сам:
🔎 определю товар;
🌐 проверю информацию;
🖼 найду подходящее фото;
✍️ оформлю карточку.

А ты решаешь:
<b>публиковать или нет.</b> ❤️
"""


# ============================================================
# OPENAI
# ============================================================

async def ask_ai(prompt, use_web=False, image_data=None):

    if not client:
        return "❌ OPENAI_API_KEY не настроен в Render."

    content = [
        {
            "type": "input_text",
            "text": prompt
        }
    ]

    if image_data:
        content.append(
            {
                "type": "input_image",
                "image_url": image_data
            }
        )

    tools = []

    if use_web:
        tools.append(
            {
                "type": "web_search",
                "search_context_size": "high"
            }
        )

    try:

        response = await client.responses.create(
            model=MODEL,
            instructions=SYSTEM_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": content
                }
            ],
            tools=tools,
        )

        return response.output_text

    except Exception as e:

        logger.exception("OpenAI request failed")

        return (
            "❌ Не получилось обратиться к AI.\n\n"
            f"Ошибка: {e}"
        )


# ============================================================
# TELEGRAM PHOTO → DATA URL
# ============================================================

async def telegram_photo_to_data_url(update, context):

    photo = update.message.photo[-1]

    telegram_file = await context.bot.get_file(
        photo.file_id
    )

    data = await telegram_file.download_as_bytearray()

    encoded = base64.b64encode(data).decode("utf-8")

    return (
        "data:image/jpeg;base64,"
        + encoded
    )


# ============================================================
# ОЧИСТКА ТЕКСТА ТОВАРА
# ============================================================

def clean_product_text(text):

    if not text:
        return ""

    # Убираем markdown-ссылки:
    # [название](https://...)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\)]+)\)",
        r"\1",
        text
    )

    # Убираем обычные URL
    text = re.sub(
        r"https?://\S+",
        "",
        text
    )

    # Убираем markdown **
    text = re.sub(
        r"\*{1,3}",
        "",
        text
    )

    # Убираем блоки источников
    text = re.sub(
        r"(?is)(источники|sources)\s*:?.*$",
        "",
        text
    )

    # Убираем строки с источниками
    lines = []

    for line in text.splitlines():

        lower = line.strip().lower()

        if (
            lower.startswith("источник:")
            or lower.startswith("источники:")
            or lower.startswith("source:")
            or lower.startswith("sources:")
        ):
            continue

        if "http://" in lower or "https://" in lower:
            continue

        lines.append(line)

    text = "\n".join(lines)

    # Убираем лишние пробелы
    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    # Убираем 3+ пустых строки
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


# ============================================================
# URLS ИЗ AI-ОТВЕТА
# ============================================================

def extract_urls(text):

    if not text:
        return []

    urls = re.findall(
        r"https?://[^\s\]\)>,]+",
        text
    )

    result = []

    for url in urls:

        url = url.rstrip(
            ".,;:!?\"'"
        )

        if url not in result:
            result.append(url)

    return result


# ============================================================
# СКАЧИВАНИЕ КАРТИНКИ
# ============================================================

async def download_image(http, image_url):

    try:

        response = await http.get(
            image_url,
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "Chrome/120 Safari/537.36"
                )
            }
        )

        if response.status_code != 200:
            return None

        content_type = response.headers.get(
            "content-type",
            ""
        ).lower()

        if (
            "image" not in content_type
            and not image_url.lower().endswith(
                (
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp",
                    ".gif"
                )
            )
        ):
            return None

        data = response.content

        if len(data) < 5000:
            return None

        if len(data) > 15 * 1024 * 1024:
            return None

        return data

    except Exception:

        return None


# ============================================================
# ПОИСК КАРТИНКИ НА СТРАНИЦЕ
# ============================================================

async def find_image_on_page(http, page_url):

    try:

        response = await http.get(
            page_url,
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "Chrome/120 Safari/537.36"
                )
            }
        )

        if response.status_code != 200:
            return None

        content_type = response.headers.get(
            "content-type",
            ""
        ).lower()

        if "text/html" not in content_type:
            return None

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        # 1. OpenGraph
        for selector in [
            ('meta[property="og:image"]'),
            ('meta[property="og:image:url"]'),
            ('meta[name="twitter:image"]'),
        ]:

            tag = soup.select_one(selector)

            if tag:

                image_url = (
                    tag.get("content")
                    or tag.get("value")
                )

                if image_url:

                    image_url = urljoin(
                        page_url,
                        image_url
                    )

                    image = await download_image(
                        http,
                        image_url
                    )

                    if image:
                        return image

        # 2. JSON-LD
        for script in soup.find_all(
            "script",
            type="application/ld+json"
        ):

            try:

                data = json.loads(
                    script.string or script.text
                )

                objects = (
                    data
                    if isinstance(data, list)
                    else [data]
                )

                for obj in objects:

                    if not isinstance(obj, dict):
                        continue

                    image = obj.get("image")

                    if isinstance(image, str):

                        image_url = urljoin(
                            page_url,
                            image
                        )

                        image_data = await download_image(
                            http,
                            image_url
                        )

                        if image_data:
                            return image_data

                    if isinstance(image, list):

                        for item in image:

                            if not isinstance(
                                item,
                                str
                            ):
                                continue

                            image_url = urljoin(
                                page_url,
                                item
                            )

                            image_data = await download_image(
                                http,
                                image_url
                            )

                            if image_data:
                                return image_data

            except Exception:
                continue

        # 3. Обычные img
        for img in soup.find_all("img"):

            image_url = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-original")
            )

            if not image_url:
                continue

            image_url = urljoin(
                page_url,
                image_url
            )

            image_data = await download_image(
                http,
                image_url
            )

            if image_data:
                return image_data

        return None

    except Exception:

        return None


# ============================================================
# ПОИСК ФОТО ТОВАРА
# ============================================================

async def find_product_image(ai_text):

    urls = extract_urls(ai_text)

    if not urls:
        return None

    async with httpx.AsyncClient(
        follow_redirects=True
    ) as http:

        for url in urls:

            # Сначала пробуем сам URL
            image = await download_image(
                http,
                url
            )

            if image:
                return image

            # Затем ищем картинку внутри страницы
            image = await find_image_on_page(
                http,
                url
            )

            if image:
                return image

    return None


# ============================================================
# КНОПКИ ТОВАРА
# ============================================================

def product_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Одобрить",
                callback_data="approve_product"
            ),
            InlineKeyboardButton(
                "✏️ Изменить",
                callback_data="edit_product"
            ),
        ],
        [
            InlineKeyboardButton(
                "🖼️ Другое фото",
                callback_data="another_photo"
            ),
            InlineKeyboardButton(
                "🔄 Новый товар",
                callback_data="new_product"
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# ============================================================
# /start
# ============================================================

async def start(update, context):

    user_id = update.effective_user.id

    get_user_data(user_id)

    await update.message.reply_text(
        WELCOME,
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# ============================================================
# /help
# ============================================================

async def help_cmd(update, context):

    text = """
<b>Как пользоваться ботом</b> 💗

<b>Новый товар</b>

Отправь:

📸 фото товара

и в подписи:

<code>3500 ₽, 2 шт.</code>

Я сам:
🔎 определю товар;
🌐 проверю информацию;
🖼 найду фото;
✍️ сделаю карточку.

После этого ты сможешь:

✅ Одобрить
✏️ Изменить
🖼️ Другое фото
🔄 Новый товар

Также можно использовать меню для создания:

📝 постов
📱 Stories
🎬 Reels
📅 контент-планов
📊 опросов
🚀 идей для роста.
"""

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# ============================================================
# НОВЫЙ ТОВАР
# ============================================================

async def start_new_product(update, context):

    user_id = update.effective_user.id

    data = get_user_data(user_id)

    data["mode"] = "new_product"

    data["last_product"] = None
    data["last_image_url"] = None

    text = """
📦 <b>Новый товар</b>

Пришли мне фотографию товара.

В подписи желательно написать:

<b>Цена + количество</b>

Например:

<code>3500 ₽, в наличии 2 шт.</code>

Я сам определю товар и оформлю карточку. ✨
"""

    if update.callback_query:

        await update.callback_query.answer()

        await update.callback_query.message.reply_text(
            text,
            parse_mode="HTML"
        )

    else:

        await update.message.reply_text(
            text,
            parse_mode="HTML"
        )


# ============================================================
# ОБРАБОТКА ФОТО ТОВАРА
# ============================================================

async def product_photo(update, context):

    user_id = update.effective_user.id

    data = get_user_data(user_id)

    if data.get("mode") != "new_product":

        await update.message.reply_text(
            "Сначала нажми «➕ Новый товар»."
        )

        return

    caption = update.message.caption or ""

    try:

        # Получаем фото
        photo = update.message.photo[-1]

        data["last_photo_file_id"] = photo.file_id

        # Фото → data URL для AI
        image_data = await telegram_photo_to_data_url(
            update,
            context
        )

        await update.message.reply_text(
            "🔎 Определяю товар и проверяю информацию..."
        )

        prompt = f"""
Определи товар на фотографии.

Дополнительная информация от Ани:

{caption}

Сделай короткую карточку товара для Telegram.

Обязательно:

✨ Название товара

Короткое описание в 1–3 предложениях.

Если достоверно известно:
📏 Объём: ...
или
🎨 Оттенок: ...

💰 Цена: {caption}
📦 В наличии: количество из сообщения Ани

ВАЖНО:

- цена Ани имеет приоритет;
- количество Ани имеет приоритет;
- не придумывай объём;
- не придумывай оттенок;
- не придумывай характеристики;
- если нужно, проверь товар в интернете;
- ищи подходящую фотографию товара в интернете;
- ссылки и источники НЕ вставляй в карточку;
- источники нужны только для проверки;
- не добавляй блок «Источники»;
- не добавляй URL;
- не пиши пояснения.

Верни сначала готовую карточку.

После карточки, отдельно, в самом конце ответа,
если нашёл страницы товара, укажи их URL для внутреннего поиска фотографии.
"""

        ai_result = await ask_ai(
            prompt,
            use_web=True,
            image_data=image_data
        )

        if ai_result.startswith("❌"):

            await update.message.reply_text(
                ai_result
            )

            return

        # Ищем фотографию в интернете
        internet_image = await find_product_image(
            ai_result
        )

        # Очищаем текст
        product_caption = clean_product_text(
            ai_result
        )

        # Дополнительная страховка:
        # если AI почему-то оставил слишком длинный текст
        if len(product_caption) > 1000:

            product_caption = (
                product_caption[:997]
                + "..."
            )

        data["last_product"] = product_caption

        data["last_image_url"] = (
            "internet"
            if internet_image
            else None
        )

        # ====================================================
        # ЕСЛИ НАШЛИ ФОТО В ИНТЕРНЕТЕ
        # ====================================================

        if internet_image:

            bio = BytesIO(
                internet_image
            )

            bio.name = "product.jpg"

            await update.message.reply_photo(
                photo=InputFile(
                    bio,
                    filename="product.jpg"
                ),
                caption=product_caption,
                reply_markup=product_keyboard()
            )

        # ====================================================
        # ЕСЛИ ФОТО НЕ НАШЛИ
        # ====================================================

        else:

            await update.message.reply_photo(
                photo=photo.file_id,
                caption=product_caption,
                reply_markup=product_keyboard()
            )

    except Exception as e:

        logger.exception(
            "Product processing error"
        )

        await update.message.reply_text(
            "❌ Не получилось обработать товар.\n\n"
            f"Ошибка: {e}"
        )


# ============================================================
# ОДОБРЕНИЕ ТОВАРА
# ============================================================

async def approve_product(update, context):

    query = update.callback_query

    await query.answer(
        "Товар одобрен ❤️"
    )

    user_id = query.from_user.id

    data = get_user_data(user_id)

    product = data.get("last_product")

    if not product:

        await query.message.reply_text(
            "❌ Не нашла карточку товара."
        )

        return

    data["products"].append(
        product
    )

    await query.message.reply_text(
        """
✅ <b>Товар одобрен!</b>

Карточка сохранена.

Пока я ничего не публикую в канал без твоего отдельного подтверждения. ❤️

Можешь прислать следующий товар.
""",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# ============================================================
# ИЗМЕНИТЬ ТОВАР
# ============================================================

async def edit_product(update, context):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    data = get_user_data(user_id)

    data["mode"] = "edit_product"

    await query.message.reply_text(
        """
✏️ <b>Что изменить?</b>

Напиши мне обычным сообщением, например:

<code>Сделай описание короче</code>

или:

<code>Добавь, что это подходит для сухой кожи</code>

или:

<code>Убери описание и оставь только название, цену и наличие</code>
""",
        parse_mode="HTML"
    )


# ============================================================
# ДРУГОЕ ФОТО
# ============================================================

async def another_photo(update, context):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    data = get_user_data(user_id)

    data["mode"] = "new_product"

    await query.message.reply_text(
        """
🖼️ <b>Хорошо!</b>

Пришли другое фото этого товара.

Я попробую найти и использовать другую фотографию.
""",
        parse_mode="HTML"
    )


# ============================================================
# СОЗДАНИЕ ПОСТА
# ============================================================

async def create_post(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
        user_id = query.from_user.id
    else:
        target = update.message
        user_id = update.effective_user.id

    data = get_user_data(user_id)

    data["mode"] = "create_post"

    await target.reply_text(
        """
📝 <b>Создание поста</b>

Напиши тему поста.

Например:

<code>5 ароматов, которые пахнут дороже своей цены</code>

или:

<code>Хочу пост про уход за сухой кожей</code>
""",
        parse_mode="HTML"
    )


# ============================================================
# STORIES
# ============================================================

async def create_stories(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
        user_id = query.from_user.id
    else:
        target = update.message
        user_id = update.effective_user.id

    data = get_user_data(user_id)

    data["mode"] = "create_stories"

    await target.reply_text(
        """
📱 <b>Stories</b>

Напиши тему или просто скажи:

<code>Сделай Stories на сегодня</code>

Я предложу серию из нескольких Stories
с вовлечением аудитории.
""",
        parse_mode="HTML"
    )


# ============================================================
# REELS
# ============================================================

async def create_reels(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
        user_id = query.from_user.id
    else:
        target = update.message
        user_id = update.effective_user.id

    data = get_user_data(user_id)

    data["mode"] = "create_reels"

    await target.reply_text(
        """
🎬 <b>Reels / TikTok</b>

Напиши:

• товар;
• тему;
• или просто «дай идеи».

Я придумаю несколько коротких
видео-концепций без необходимости показывать лицо.
""",
        parse_mode="HTML"
    )


# ============================================================
# ПЛАН НА ДЕНЬ
# ============================================================

async def daily_plan(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
    else:
        target = update.message

    prompt = """
Составь контент-план для Telegram-канала
«Косметика | Парфюм | Москва» на один день.

Нужно:

3–7 коротких Stories/сообщений
и
1–2 основных поста.

Сохрани баланс:

70% полезное
20% вовлечение
10% продажи.

Не превращай день в каталог товаров.

Для каждого пункта укажи:
время примерно,
формат,
тему,
короткую идею текста.
"""

    result = await ask_ai(prompt)

    await target.reply_text(
        result,
        reply_markup=main_menu()
    )


# ============================================================
# ПЛАН НА НЕДЕЛЮ
# ============================================================

async def weekly_plan(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
    else:
        target = update.message

    prompt = """
Составь подробный, но компактный контент-план
на 7 дней для Telegram-канала
«Косметика | Парфюм | Москва».

Учитывай:

Понедельник:
3 продукта + опрос

Вторник:
новинки / распаковка

Среда:
аромат дня

Четверг:
выбираем вместе

Пятница:
топ-5 продуктов

Суббота:
лайфстайл / атмосфера

Воскресенье:
итоги недели + тизер следующей

Не делай каждый день продажным.
Добавляй идеи для вовлечения.
"""

    result = await ask_ai(prompt)

    await target.reply_text(
        result,
        reply_markup=main_menu()
    )


# ============================================================
# СОЗДАТЬ ОПРОС
# ============================================================

async def poll_creator(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
        user_id = query.from_user.id
    else:
        target = update.message
        user_id = update.effective_user.id

    data = get_user_data(user_id)

    data["mode"] = "poll"

    await target.reply_text(
        """
📊 <b>Создание опроса</b>

Напиши тему.

Например:

<code>Какой аромат вы бы выбрали на осень?</code>

Я сделаю красивый вопрос и варианты ответов.
""",
        parse_mode="HTML"
    )


# ============================================================
# ОДОБРЕНИЕ ОПРОСА
# ============================================================

async def approve_poll(update, context):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    data = get_user_data(user_id)

    poll = data.get("pending_poll")

    if not poll:

        await query.message.reply_text(
            "❌ Опрос не найден."
        )

        return

    try:

        question = poll["question"]

        options = poll["options"]

        await query.message.reply_poll(
            question=question,
            options=options,
            is_anonymous=True
        )

        data["pending_poll"] = None

    except Exception as e:

        await query.message.reply_text(
            f"❌ Не удалось создать опрос:\n{e}"
        )


# ============================================================
# ВОВЛЕЧЕНИЕ
# ============================================================

async def engagement(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
    else:
        target = update.message

    prompt = """
Придумай 10 коротких идей для вовлечения
аудитории Telegram-канала
«Косметика | Парфюм | Москва».

Используй:

опросы,
выборы,
угадайки,
«что бы выбрала ты?»,
«угадайте цену»,
реакции,
короткие вопросы.

Идеи должны быть простыми
и подходить для косметики и парфюмерии.
"""

    result = await ask_ai(prompt)

    await target.reply_text(
        result,
        reply_markup=main_menu()
    )


# ============================================================
# РУБРИКИ
# ============================================================

async def rubrics(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
    else:
        target = update.message

    text = """
🏷 <b>Рубрики канала</b>

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

Можно постепенно добавлять новые рубрики,
если какая-то тема хорошо заходит аудитории.
"""

    await target.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# ============================================================
# РОСТ
# ============================================================

async def growth(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
    else:
        target = update.message

    prompt = """
Дай конкретную стратегию роста Telegram-канала
«Косметика | Парфюм | Москва».

Учитывай:

- Telegram;
- TikTok;
- Instagram Reels;
- небольшие рекламные размещения;
- взаимопиар;
- рекомендации;
- эксклюзивный Telegram-контент.

Не предлагай накрутку ботов.

Сделай план действий на ближайшие 30 дней.
"""

    result = await ask_ai(prompt)

    await target.reply_text(
        result,
        reply_markup=main_menu()
    )


# ============================================================
# ЧТО ВЫЛОЖИТЬ
# ============================================================

async def what_to_post(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
    else:
        target = update.message

    prompt = """
Придумай 15 идей, что можно выложить
сегодня в Telegram-канале
«Косметика | Парфюм | Москва».

Не делай все идеи продажными.

Раздели:

💡 полезное
💬 вовлечение
✨ атмосфера
🛍 продажи
📊 интерактив
"""

    result = await ask_ai(prompt)

    await target.reply_text(
        result,
        reply_markup=main_menu()
    )


# ============================================================
# МОИ ТОВАРЫ
# ============================================================

async def show_products(update, context):

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        target = query.message
        user_id = query.from_user.id
    else:
        target = update.message
        user_id = update.effective_user.id

    data = get_user_data(user_id)

    products = data.get("products", [])

    if not products:

        await target.reply_text(
            """
📦 <b>Товаров пока нет.</b>

Нажми «➕ Новый товар» и отправь
фото + цену + количество.
""",
            parse_mode="HTML",
            reply_markup=main_menu()
        )

        return

    text = "📦 <b>Сохранённые товары</b>\n\n"

    for i, product in enumerate(
        products,
        start=1
    ):

        text += (
            f"<b>{i}.</b>\n"
            f"{product}\n\n"
        )

    await target.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# ============================================================
# РАЗДЕЛ ФОТО
# ============================================================

async def photo_section(update, context):

    await update.message.reply_text(
        """
🖼 <b>Работа с фотографиями</b>

Сейчас основной сценарий:

📸 ты присылаешь фото товара;
🔎 AI определяет товар;
🌐 ищет информацию;
🖼 пытается найти подходящее фото;
✨ создаёт готовую карточку.

Если подходящее фото не найдено,
используется твоё исходное фото.
""",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# ============================================================
# ТЕКСТОВЫЕ СООБЩЕНИЯ
# ============================================================

async def text_message(update, context):

    user_id = update.effective_user.id

    data = get_user_data(user_id)

    text = update.message.text or ""

    mode = data.get("mode")

    # --------------------------------------------------------
    # РЕДАКТИРОВАНИЕ ТОВАРА
    # --------------------------------------------------------

    if mode == "edit_product":

        old_product = data.get("last_product")

        if not old_product:

            await update.message.reply_text(
                "❌ Карточка товара не найдена."
            )

            return

        prompt = f"""
Вот текущая карточка товара:

{old_product}

Аня просит:

{text}

Измени карточку согласно её просьбе.

Верни только готовую карточку.

Не добавляй ссылки.
Не добавляй источники.
Не добавляй комментарии.
"""

        result = await ask_ai(prompt)

        result = clean_product_text(result)

        if len(result) > 1000:
            result = result[:997] + "..."

        data["last_product"] = result

        # Используем последнее фото
        file_id = data.get(
            "last_photo_file_id"
        )

        if file_id:

            await update.message.reply_photo(
                photo=file_id,
                caption=result,
                reply_markup=product_keyboard()
            )

        else:

            await update.message.reply_text(
                result,
                reply_markup=product_keyboard()
            )

        data["mode"] = None

        return

    # --------------------------------------------------------
    # ПОСТ
    # --------------------------------------------------------

    if mode == "create_post":

        prompt = f"""
Создай готовый Telegram-пост
для канала «Косметика | Парфюм | Москва».

Тема:

{text}

Пост должен быть:
живым,
красивым,
коротким,
полезным.

Не используй агрессивные продажи.

Если уместно, добавь один вопрос аудитории.
"""

        result = await ask_ai(prompt)

        data["mode"] = None

        await update.message.reply_text(
            result,
            reply_markup=main_menu()
        )

        return

    # --------------------------------------------------------
    # STORIES
    # --------------------------------------------------------

    if mode == "create_stories":

        prompt = f"""
Создай серию из 5–7 Stories
для Telegram-канала
«Косметика | Парфюм | Москва».

Тема:

{text}

Сделай последовательность:
1 — зацепка
2 — развитие
3 — польза
4 — вовлечение
5 — мягкое завершение

Не требуй показывать лицо.
"""

        result = await ask_ai(prompt)

        data["mode"] = None

        await update.message.reply_text(
            result,
            reply_markup=main_menu()
        )

        return

    # --------------------------------------------------------
    # REELS
    # --------------------------------------------------------

    if mode == "create_reels":

        prompt = f"""
Придумай 5 идей для Reels/TikTok
для канала косметики и парфюмерии.

Запрос Ани:

{text}

Для каждой идеи укажи:

🎬 идея
🎥 что снять
📝 текст на экране
🎵 настроение
📌 CTA

Не нужно показывать лицо.
"""

        result = await ask_ai(prompt)

        data["mode"] = None

        await update.message.reply_text(
            result,
            reply_markup=main_menu()
        )

        return

    # --------------------------------------------------------
    # ОПРОС
    # --------------------------------------------------------

    if mode == "poll":

        prompt = f"""
Создай Telegram-опрос.

Тема:

{text}

Верни строго JSON:

{{
  "question": "...",
  "options": ["...", "...", "...", "..."]
}}

Максимум 4 варианта.

Без ссылок.
"""

        result = await ask_ai(prompt)

        try:

            match = re.search(
                r"\{.*\}",
                result,
                re.DOTALL
            )

            if not match:
                raise ValueError(
                    "JSON не найден"
                )

            poll = json.loads(
                match.group(0)
            )

            question = poll["question"]

            options = poll["options"]

            if not isinstance(
                options,
                list
            ):
                raise ValueError(
                    "Некорректные варианты"
                )

            options = [
                str(x)
                for x in options[:10]
            ]

            data["pending_poll"] = {
                "question": question,
                "options": options
            }

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Одобрить и создать",
                            callback_data="approve_poll"
                        )
                    ]
                ]
            )

            await update.message.reply_text(
                f"""
📊 <b>Готовый опрос</b>

<b>{question}</b>

""" +
                "\n".join(
                    f"• {x}"
                    for x in options
                ),
                parse_mode="HTML",
                reply_markup=keyboard
            )

        except Exception:

            await update.message.reply_text(
                "Не получилось подготовить опрос.\n\n"
                + result
            )

        data["mode"] = None

        return

    # --------------------------------------------------------
    # ОБЫЧНОЕ СООБЩЕНИЕ
    # --------------------------------------------------------

    await update.message.reply_text(
        """
Я готова помочь ❤️

Используй меню ниже или нажми:

➕ <b>Новый товар</b>

и пришли фотографию товара
с ценой и количеством.
""",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# ============================================================
# CALLBACK BUTTONS
# ============================================================

async def button(update, context):

    query = update.callback_query

    data = query.data

    if data == "new_product":
        await start_new_product(
            update,
            context
        )
        return

    if data == "approve_product":
        await approve_product(
            update,
            context
        )
        return

    if data == "edit_product":
        await edit_product(
            update,
            context
        )
        return

    if data == "another_photo":
        await another_photo(
            update,
            context
        )
        return

    if data == "create_post":
        await create_post(
            update,
            context
        )
        return

    if data == "create_stories":
        await create_stories(
            update,
            context
        )
        return

    if data == "create_reels":
        await create_reels(
            update,
            context
        )
        return

    if data == "daily_plan":
        await daily_plan(
            update,
            context
        )
        return

    if data == "weekly_plan":
        await weekly_plan(
            update,
            context
        )
        return

    if data == "poll_creator":
        await poll_creator(
            update,
            context
        )
        return

    if data == "approve_poll":
        await approve_poll(
            update,
            context
        )
        return

    if data == "engagement":
        await engagement(
            update,
            context
        )
        return

    if data == "rubrics":
        await rubrics(
            update,
            context
        )
        return

    if data == "growth":
        await growth(
            update,
            context
        )
        return

    if data == "what_to_post":
        await what_to_post(
            update,
            context
        )
        return

    if data == "show_products":
        await show_products(
            update,
            context
        )
        return

    await query.answer()


# ============================================================
# КОМАНДЫ TELEGRAM
# ============================================================

async def setup_commands(application):

    commands = [
        BotCommand(
            "start",
            "Главное меню"
        ),
        BotCommand(
            "new",
            "Добавить товар"
        ),
        BotCommand(
            "post",
            "Создать пост"
        ),
        BotCommand(
            "today",
            "План на сегодня"
        ),
        BotCommand(
            "week",
            "План на неделю"
        ),
        BotCommand(
            "help",
            "Помощь"
        ),
    ]

    await application.bot.set_my_commands(
        commands
    )

    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )


# ============================================================
# /new
# ============================================================

async def new_command(update, context):

    await start_new_product(
        update,
        context
    )


# ============================================================
# /post
# ============================================================

async def post_command(update, context):

    await create_post(
        update,
        context
    )


# ============================================================
# /today
# ============================================================

async def today_command(update, context):

    await daily_plan(
        update,
        context
    )


# ============================================================
# /week
# ============================================================

async def week_command(update, context):

    await weekly_plan(
        update,
        context
    )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
async def root():

    return {
        "status": "ok",
        "bot": "BeautyManager_Moscow_Bot"
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "healthy"
    }


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

def setup_handlers():

    if not telegram_app:
        return

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "help",
            help_cmd
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "new",
            new_command
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "post",
            post_command
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "today",
            today_command
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "week",
            week_command
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            button
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.PHOTO,
            product_photo
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_message
        )
    )


# ============================================================
# ЗАПУСК TELEGRAM
# ============================================================

async def setup():

    if not telegram_app:
        logger.error(
            "TELEGRAM_BOT_TOKEN не найден."
        )
        return

    setup_handlers()

    await telegram_app.initialize()

    await telegram_app.start()

    if telegram_app.updater:

        await telegram_app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES
        )

    await setup_commands(
        telegram_app
    )

    logger.info(
        "Telegram bot started successfully."
    )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():

    await setup()


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown():

    if not telegram_app:
        return

    try:

        if telegram_app.updater:

            await telegram_app.updater.stop()

        await telegram_app.stop()

        await telegram_app.shutdown()

    except Exception:

        logger.exception(
            "Error while shutting down bot"
        )
