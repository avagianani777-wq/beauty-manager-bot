import os
import re
import json
import base64
import logging
from io import BytesIO
from urllib.parse import urljoin, urlparse

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


# =========================================================
# НАСТРОЙКИ
# =========================================================

logging.basicConfig(
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "",
)

OPENAI_API_KEY = os.environ.get(
    "OPENAI_API_KEY",
    "",
)

CHANNEL_ID = os.environ.get(
    "TELEGRAM_CHANNEL_ID",
    "",
)

MODEL = "gpt-5.6-luna"

app = FastAPI()

telegram_app = (
    Application.builder()
    .token(TOKEN)
    .build()
    if TOKEN
    else None
)

client = (
    AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )
    if OPENAI_API_KEY
    else None
)


# =========================================================
# СТРАТЕГИЯ BEAUTY MANAGER
# =========================================================

SYSTEM_PROMPT = """
Ты — Beauty Manager, AI-помощник Telegram-канала
«Косметика | Парфюм | Москва».

Твоя задача — помогать владельцу канала развивать его,
создавать контент и продавать оригинальную косметику,
парфюмерию и уходовые средства.

=========================================================
ГЛАВНАЯ СТРАТЕГИЯ
=========================================================

Канал НЕ должен выглядеть как обычный каталог товаров.

Соотношение контента:

70% — полезный и интересный контент
20% — вовлечение и личная атмосфера
10% — прямые продажи

Не нужно продавать каждый день.

=========================================================
СТИЛЬ
=========================================================

Пиши:

- живо;
- красиво;
- современно;
- дружелюбно;
- женственно;
- легко;
- без канцелярита;
- без чрезмерного количества эмодзи.

Не используй агрессивные продажи.

Не пиши:
«КУПИТЕ СРОЧНО!!!»
«УСПЕЙТЕ!!!»
если для этого нет реальной причины.

Если товар действительно заканчивается,
можно честно использовать ограниченность:

«осталось 2 шт.»

=========================================================
ВАЖНО ПРО ЛИЧНОСТЬ АНИ
=========================================================

Можно создавать ощущение личного блога:

«давайте выберем вместе»
«я бы обратила внимание»
«как вам такой вариант?»

Но нельзя выдумывать личный опыт.

Нельзя писать:
«я сама пользуюсь этим кремом»
«это мой любимый аромат»

если Аня этого не сообщала.

=========================================================
ИНФОРМАЦИЯ О ТОВАРАХ
=========================================================

При поиске информации:

1. Сначала официальный сайт бренда.
2. Затем крупные и надёжные магазины.
3. Проверяй:
   - название;
   - оттенок;
   - объём;
   - назначение;
   - комплектацию.

Не придумывай характеристики.

Цена Ани всегда важнее цены в интернете.

Количество Ани всегда важнее количества в интернете.

=========================================================
РУБРИКИ
=========================================================

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

=========================================================
ИДЕИ
=========================================================

Можно использовать:

5 ароматов, которые пахнут дороже своей цены

Что подарить девушке, если вообще не разбираешься
в косметике

3 крема для тех, кто ненавидит липкость

Как выбрать парфюм в подарок

Что купить до 5 000 ₽

Парфюм на разные типы свиданий

5 средств, которые красиво смотрятся
на туалетном столике

Выбираем вместе

Что бы выбрала ты?

Угадайте цену

Сегодня приехало

Распаковка

Сборка заказа

Отзывы клиентов

=========================================================
НЕДЕЛЯ
=========================================================

Понедельник:
полезный пост + товар

Вторник:
подборка + опрос

Среда:
новинка + короткое сообщение

Четверг:
разбор продукта

Пятница:
вовлечение + мягкая продажа

Суббота:
lifestyle + подборка

Воскресенье:
итоги недели + тизер новинок

Не копируй эту структуру механически.
Подстраивай её под реальные товары.
"""


# =========================================================
# ДАННЫЕ ПОЛЬЗОВАТЕЛЕЙ
# =========================================================

USERS = {}


def get_user_data(user_id: int):

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


# =========================================================
# ГЛАВНОЕ МЕНЮ
# =========================================================

def main_menu():

    keyboard = [

        [
            InlineKeyboardButton(
                "📦 Новый товар",
                callback_data="new_product",
            ),
            InlineKeyboardButton(
                "📋 Мои товары",
                callback_data="products",
            ),
        ],

        [
            InlineKeyboardButton(
                "✍️ Создать пост",
                callback_data="post",
            ),
            InlineKeyboardButton(
                "📱 Сторис",
                callback_data="stories",
            ),
        ],

        [
            InlineKeyboardButton(
                "🎥 Reels / TikTok",
                callback_data="reels",
            ),
            InlineKeyboardButton(
                "🗳️ Создать опрос",
                callback_data="poll",
            ),
        ],

        [
            InlineKeyboardButton(
                "☀️ План на сегодня",
                callback_data="today",
            ),
            InlineKeyboardButton(
                "🗓️ План на неделю",
                callback_data="week",
            ),
        ],

        [
            InlineKeyboardButton(
                "💬 Вовлечение",
                callback_data="engagement",
            ),
            InlineKeyboardButton(
                "💡 Идеи рубрик",
                callback_data="rubrics",
            ),
        ],

        [
            InlineKeyboardButton(
                "🚀 Идеи для роста",
                callback_data="growth",
            ),
            InlineKeyboardButton(
                "🔥 Что публиковать",
                callback_data="now",
            ),
        ],

        [
            InlineKeyboardButton(
                "📸 Работа с фото",
                callback_data="photo",
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


WELCOME = """
Привет, Ани! 💗

Я — твой Beauty Manager.

Теперь я могу помогать тебе не только с товарами,
но и с развитием всего канала:

📦 товары
✍️ посты
📱 сторис
🎥 Reels / TikTok
🗳️ опросы
☀️ план на сегодня
🗓️ план на неделю
💬 вовлечение
🚀 идеи для роста

И главное:

✨ я ничего не публикую без твоего одобрения.

Начнём? 🫶🏻
"""


# =========================================================
# OPENAI
# =========================================================

async def ask_ai(
    prompt: str,
    use_web: bool = False,
    image_data: str | None = None,
):

    if not client:

        return (
            "❌ OPENAI_API_KEY не настроен "
            "в Render."
        )

    content = [
        {
            "type": "input_text",
            "text": prompt,
        }
    ]

    if image_data:

        content.append(
            {
                "type": "input_image",
                "image_url": image_data,
            }
        )

    tools = []

    if use_web:

        tools.append(
            {
                "type": "web_search",
                "search_context_size": "high",
            }
        )

    try:

        response = await client.responses.create(
            model=MODEL,
            instructions=SYSTEM_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
            tools=tools,
        )

        return response.output_text

    except Exception as e:

        logger.exception(
            "OpenAI request failed"
        )

        return (
            "❌ Не получилось обратиться "
            "к AI.\n\n"
            f"Ошибка: {e}"
        )


# =========================================================
# TELEGRAM PHOTO → DATA URL
# =========================================================

async def telegram_photo_to_data_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    photo = update.message.photo[-1]

    telegram_file = await context.bot.get_file(
        photo.file_id
    )

    data = await telegram_file.download_as_bytearray()

    encoded = base64.b64encode(
        data
    ).decode("utf-8")

    return (
        "data:image/jpeg;base64,"
        + encoded
    )


# =========================================================
# ИЗВЛЕКАЕМ URL ИЗ ОТВЕТА AI
# =========================================================

def extract_urls(text: str):

    if not text:
        return []

    urls = re.findall(
        r'https?://[^\s<>\[\]\(\)"\']+',
        text,
    )

    cleaned = []

    for url in urls:

        url = url.rstrip(
            ".,;:!?)]}>"
        )

        if url not in cleaned:

            cleaned.append(url)

    return cleaned


# =========================================================
# ПРОВЕРКА IMAGE URL
# =========================================================

async def download_image(
    http: httpx.AsyncClient,
    image_url: str,
):

    try:

        response = await http.get(
            image_url,
            timeout=20,
            follow_redirects=True,
        )

        if response.status_code != 200:
            return None

        content_type = response.headers.get(
            "content-type",
            "",
        ).lower()

        if not content_type.startswith(
            "image/"
        ):

            return None

        data = response.content

        if len(data) < 1000:
            return None

        # Ограничение примерно 15 MB
        if len(data) > 15 * 1024 * 1024:
            return None

        return {
            "bytes": data,
            "content_type": content_type,
            "url": str(response.url),
        }

    except Exception as e:

        logger.warning(
            "Could not download image %s: %s",
            image_url,
            e,
        )

        return None


# =========================================================
# ПОИСК КАРТИНКИ НА СТРАНИЦЕ
# =========================================================

async def find_image_on_page(
    http: httpx.AsyncClient,
    page_url: str,
):

    try:

        response = await http.get(
            page_url,
            timeout=20,
            follow_redirects=True,
        )

        if response.status_code != 200:
            return None

        content_type = response.headers.get(
            "content-type",
            "",
        ).lower()

        # Иногда ссылка из поиска уже является картинкой
        if content_type.startswith("image/"):

            data = response.content

            if (
                len(data) > 1000
                and len(data)
                < 15 * 1024 * 1024
            ):

                return {
                    "bytes": data,
                    "content_type": content_type,
                    "url": str(response.url),
                }

            return None

        if "text/html" not in content_type:
            return None

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        # -------------------------------------------------
        # 1. OG IMAGE
        # -------------------------------------------------

        meta = soup.find(
            "meta",
            attrs={
                "property": "og:image"
            },
        )

        if meta:

            image_url = meta.get(
                "content"
            )

            if image_url:

                image_url = urljoin(
                    str(response.url),
                    image_url,
                )

                result = await download_image(
                    http,
                    image_url,
                )

                if result:
                    return result

        # -------------------------------------------------
        # 2. OG IMAGE SECURE URL
        # -------------------------------------------------

        meta = soup.find(
            "meta",
            attrs={
                "property":
                    "og:image:secure_url"
            },
        )

        if meta:

            image_url = meta.get(
                "content"
            )

            if image_url:

                image_url = urljoin(
                    str(response.url),
                    image_url,
                )

                result = await download_image(
                    http,
                    image_url,
                )

                if result:
                    return result

        # -------------------------------------------------
        # 3. TWITTER IMAGE
        # -------------------------------------------------

        meta = soup.find(
            "meta",
            attrs={
                "name":
                    "twitter:image"
            },
        )

        if meta:

            image_url = meta.get(
                "content"
            )

            if image_url:

                image_url = urljoin(
                    str(response.url),
                    image_url,
                )

                result = await download_image(
                    http,
                    image_url,
                )

                if result:
                    return result

        # -------------------------------------------------
        # 4. LINK IMAGE
        # -------------------------------------------------

        for link in soup.find_all(
            "link"
        ):

            rel = link.get(
                "rel",
                [],
            )

            href = link.get(
                "href"
            )

            if not href:
                continue

            if isinstance(
                rel,
                list,
            ):

                rel_text = " ".join(
                    rel
                ).lower()

            else:

                rel_text = str(
                    rel
                ).lower()

            if (
                "image_src"
                in rel_text
            ):

                image_url = urljoin(
                    str(response.url),
                    href,
                )

                result = await download_image(
                    http,
                    image_url,
                )

                if result:
                    return result

        # -------------------------------------------------
        # 5. JSON-LD PRODUCT IMAGE
        # -------------------------------------------------

        for script in soup.find_all(
            "script",
            type="application/ld+json",
        ):

            try:

                raw = script.string

                if not raw:
                    continue

                data = json.loads(raw)

                objects = []

                if isinstance(
                    data,
                    list,
                ):

                    objects.extend(data)

                elif isinstance(
                    data,
                    dict,
                ):

                    objects.append(data)

                    graph = data.get(
                        "@graph"
                    )

                    if isinstance(
                        graph,
                        list,
                    ):

                        objects.extend(
                            graph
                        )

                for obj in objects:

                    if not isinstance(
                        obj,
                        dict,
                    ):
                        continue

                    image = obj.get(
                        "image"
                    )

                    candidates = []

                    if isinstance(
                        image,
                        str,
                    ):

                        candidates.append(
                            image
                        )

                    elif isinstance(
                        image,
                        list,
                    ):

                        candidates.extend(
                            image
                        )

                    elif isinstance(
                        image,
                        dict,
                    ):

                        if image.get(
                            "url"
                        ):

                            candidates.append(
                                image[
                                    "url"
                                ]
                            )

                    for image_url in candidates:

                        if not isinstance(
                            image_url,
                            str,
                        ):
                            continue

                        image_url = urljoin(
                            str(
                                response.url
                            ),
                            image_url,
                        )

                        result = await download_image(
                            http,
                            image_url,
                        )

                        if result:
                            return result

            except Exception:

                continue

        # -------------------------------------------------
        # 6. IMG TAGS
        # -------------------------------------------------

        image_candidates = []

        for img in soup.find_all(
            "img"
        ):

            for attr in [
                "src",
                "data-src",
                "data-original",
                "data-lazy-src",
                "data-image",
            ]:

                value = img.get(
                    attr
                )

                if value:
                    image_candidates.append(
                        value
                    )

            srcset = img.get(
                "srcset"
            )

            if srcset:

                for item in srcset.split(
                    ","
                ):

                    image_candidates.append(
                        item.strip().split(
                            " "
                        )[0]
                    )

        # Убираем дубли
        image_candidates = list(
            dict.fromkeys(
                image_candidates
            )
        )

        for image_url in image_candidates:

            image_url = urljoin(
                str(response.url),
                image_url,
            )

            # Отбрасываем SVG, пиксели и очевидные иконки
            lowered = image_url.lower()

            if any(
                x in lowered
                for x in [
                    ".svg",
                    "pixel",
                    "sprite",
                    "icon",
                    "logo",
                    "favicon",
                ]
            ):

                continue

            result = await download_image(
                http,
                image_url,
            )

            if result:
                return result

    except Exception as e:

        logger.warning(
            "Could not inspect page %s: %s",
            page_url,
            e,
        )

    return None


# =========================================================
# ГЛАВНЫЙ ПОИСК ФОТОГРАФИИ
# =========================================================

async def find_product_image(
    ai_text: str,
):

    urls = extract_urls(
        ai_text
    )

    if not urls:
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
    }

    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
    ) as http:

        # Сначала пробуем ссылки в том порядке,
        # в котором их дал AI.
        for url in urls:

            result = await find_image_on_page(
                http,
                url,
            )

            if result:

                logger.info(
                    "Product image found: %s",
                    result["url"],
                )

                return result

    return None


# =========================================================
# КНОПКИ ТОВАРА
# =========================================================

def product_keyboard():

    return InlineKeyboardMarkup(
        [

            [
                InlineKeyboardButton(
                    "✅ Одобрить",
                    callback_data="approve_product",
                ),
            ],

            [
                InlineKeyboardButton(
                    "✏️ Изменить",
                    callback_data="edit_product",
                ),

                InlineKeyboardButton(
                    "🖼️ Другое фото",
                    callback_data="another_photo",
                ),
            ],

            [
                InlineKeyboardButton(
                    "🔄 Новый товар",
                    callback_data="new_product",
                ),
            ],

        ]
    )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        WELCOME,
        reply_markup=main_menu(),
    )


async def help_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "Выбери нужный раздел 👇",
        reply_markup=main_menu(),
    )


# =========================================================
# НОВЫЙ ТОВАР
# =========================================================

async def start_new_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    data["mode"] = "new_product"

    await query.message.reply_text(
        """
📦 <b>Добавляем новый товар</b>

Пришли мне:

📸 фотографию товара
💰 цену
📦 количество

Например:

3500 ₽
В наличии 5 шт.

Можно написать цену и количество
в подписи к фотографии.
""",
        parse_mode="HTML",
    )


# =========================================================
# ФОТО ТОВАРА
# =========================================================

async def product_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    uid = update.effective_user.id

    data = get_user_data(uid)

    image_data = (
        await telegram_photo_to_data_url(
            update,
            context,
        )
    )

    photo_file_id = (
        update.message.photo[-1].file_id
    )

    caption = (
        update.message.caption
        or ""
    )

    data["last_photo"] = image_data

    data[
        "last_photo_file_id"
    ] = photo_file_id

    data[
        "product_caption"
    ] = caption

    # Если пользователь просто прислал фото
    # без режима товара
    if data.get("mode") != "new_product":

        await update.message.reply_text(
            """
📸 Фото получила!

Чтобы обработать его как товар,
нажми «📦 Новый товар».
""",
            reply_markup=main_menu(),
        )

        return

    # -----------------------------------------------------
    # СООБЩЕНИЕ О ПОИСКЕ
    # -----------------------------------------------------

    await update.message.reply_text(
        """
🔎 <b>Рассматриваю товар и ищу информацию о нём...</b>

Это может занять немного времени.

Сначала определяю товар,
затем проверяю информацию
и пытаюсь найти фотографию товара
из источника.
""",
        parse_mode="HTML",
    )

    # -----------------------------------------------------
    # ПРОМПТ
    # -----------------------------------------------------

    prompt = f"""
Проанализируй фотографию товара.

Дополнительная информация от владельца:

{caption or "не указана"}

Определи максимально точно:

1. бренд;
2. название;
3. тип продукта;
4. оттенок, если есть;
5. объём, если есть;
6. назначение.

Затем обязательно используй web search,
чтобы проверить информацию.

Приоритет источников:

1. официальный сайт бренда;
2. официальный магазин;
3. крупный надёжный ритейлер.

Найди страницу конкретно этого товара.

Не придумывай характеристики.

ВАЖНО:

Цена владельца:
{caption}

Цена владельца должна иметь приоритет
над ценой из интернета.

Количество владельца также имеет приоритет.

Если цена и количество указаны в подписи,
извлеки их и сохрани.

Сделай готовую карточку товара.

Формат:

✨ **НАЗВАНИЕ**

Короткое красивое описание
на 2–4 предложения.

🤍 **Объём / оттенок:**
...

💰 **Цена:**
...

📦 **В наличии:**
...

**Назначение:**
...

В конце укажи только названия источников
и ссылки на страницы.

Не используй ссылки на изображения
как основной источник информации.
"""

    result = await ask_ai(
        prompt,
        use_web=True,
        image_data=image_data,
    )

    # -----------------------------------------------------
    # ИЩЕМ РЕАЛЬНУЮ КАРТИНКУ
    # -----------------------------------------------------

    online_image = (
        await find_product_image(
            result
        )
    )

    data[
        "last_product"
    ] = result

    data[
        "last_image_url"
    ] = (
        online_image["url"]
        if online_image
        else None
    )

    # -----------------------------------------------------
    # ОТПРАВЛЯЕМ КАРТИНКУ
    # -----------------------------------------------------

    image_sent = False

    if online_image:

        try:

            image_bytes = (
                online_image["bytes"]
            )

            bio = BytesIO(
                image_bytes
            )

            bio.name = (
                "product.jpg"
            )

            await update.message.reply_photo(
                photo=InputFile(
                    bio
                ),
                caption=(
                    "🖼️ Фото товара "
                    "найдено автоматически"
                ),
            )

            image_sent = True

        except Exception as e:

            logger.warning(
                "Could not send online image: %s",
                e,
            )

    # -----------------------------------------------------
    # ЕСЛИ КАРТИНКУ НЕ НАШЛИ —
    # ОТПРАВЛЯЕМ ФОТО ПОЛЬЗОВАТЕЛЯ
    # -----------------------------------------------------

    if not image_sent:

        try:

            await update.message.reply_photo(
                photo=photo_file_id,
                caption=(
                    "📸 Использую "
                    "твоё исходное фото"
                ),
            )

        except Exception as e:

            logger.warning(
                "Could not send original photo: %s",
                e,
            )

    # -----------------------------------------------------
    # КАРТОЧКА
    # -----------------------------------------------------

    await update.message.reply_text(
        result,
        reply_markup=product_keyboard(),
    )


# =========================================================
# ОДОБРЕНИЕ ТОВАРА
# =========================================================

async def approve_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    product = data.get(
        "last_product"
    )

    if not product:

        await query.message.reply_text(
            "Не нашла карточку товара."
        )

        return

    data["products"].append(
        {
            "text": product,
            "image_url": data.get(
                "last_image_url"
            ),
            "photo_file_id": data.get(
                "last_photo_file_id"
            ),
        }
    )

    data["mode"] = None

    await query.message.reply_text(
        """
✅ <b>Товар сохранён!</b>

Теперь его можно использовать для:

✍️ постов
📱 сторис
🎥 Reels / TikTok
🗳️ опросов
🗓️ контент-плана

Что делаем дальше?
""",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# =========================================================
# ИЗМЕНЕНИЕ ТОВАРА
# =========================================================

async def edit_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    data["mode"] = (
        "edit_product"
    )

    await query.message.reply_text(
        """
✏️ Напиши, что именно изменить.

Например:

«Сделай описание короче»

«Убери лишнюю информацию»

«Добавь больше информации
про оттенок»

«Сделай текст более продающим»

«Исправь объём»
"""
    )


# =========================================================
# ДРУГОЕ ФОТО
# =========================================================

async def another_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    data["mode"] = (
        "new_product"
    )

    await query.message.reply_text(
        """
🖼️ Хорошо!

Пришли другое фото товара.

Я заново попробую определить
товар и найти подходящее изображение.
"""
    )


# =========================================================
# СОЗДАТЬ ПОСТ
# =========================================================

async def create_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    products = "\n\n".join(
        [
            p["text"]
            for p in data["products"][-10:]
        ]
    )

    prompt = f"""
Создай готовый Telegram-пост
для канала «Косметика | Парфюм | Москва».

Доступные товары:

{products or "Товаров пока нет."}

Соблюдай стратегию:

70% полезное
20% вовлечение
10% продажи.

Пост не обязан быть продажным.

Можно сделать:

- полезный совет;
- подборку;
- бьюти-разбор;
- вопрос;
- сравнение;
- мягкую продажу.

Текст должен быть готов к публикации.
"""

    result = await ask_ai(
        prompt
    )

    data["draft"] = result

    await query.message.reply_text(
        result,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Одобрить",
                        callback_data=(
                            "approve_draft"
                        ),
                    ),
                    InlineKeyboardButton(
                        "✏️ Изменить",
                        callback_data=(
                            "edit_draft"
                        ),
                    ),
                ],
            ]
        ),
    )


# =========================================================
# СТОРИС
# =========================================================

async def create_stories(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    products = "\n".join(
        [
            p["text"]
            for p in data["products"][-10:]
        ]
    )

    prompt = f"""
Создай серию из 4 коротких
Telegram Stories / сообщений.

Канал:

«Косметика | Парфюм | Москва»

Товары:

{products or "Товаров пока нет."}

Структура:

1. зацепка;
2. полезная информация;
3. вовлечение;
4. мягкий переход к товару
   или действию.

Можно использовать вопрос
или опрос.

Не делай все 4 сообщения продажными.
"""

    result = await ask_ai(
        prompt
    )

    data["draft"] = result

    await query.message.reply_text(
        result,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Одобрить",
                        callback_data=(
                            "approve_draft"
                        ),
                    ),
                    InlineKeyboardButton(
                        "✏️ Изменить",
                        callback_data=(
                            "edit_draft"
                        ),
                    ),
                ]
            ]
        ),
    )


# =========================================================
# REELS / TIKTOK
# =========================================================

async def create_reels(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    prompt = """
Придумай 5 идей Reels/TikTok
для Telegram-канала косметики
и парфюмерии.

Условия:

- не показывать лицо владельца;
- можно снимать только товары;
- легко снять на телефон;
- эстетично;
- с сильным хуком;
- желательно с потенциалом пересылок.

Для каждой:

1. хук первых 2 секунд;
2. что снять;
3. текст на экране;
4. подпись;
5. CTA.
"""

    result = await ask_ai(
        prompt
    )

    await query.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# ПЛАН НА СЕГОДНЯ
# =========================================================

async def daily_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    products = "\n".join(
        [
            p["text"]
            for p in data["products"][-10:]
        ]
    )

    prompt = f"""
Составь контент-план НА ОДИН ДЕНЬ
для Telegram-канала
«Косметика | Парфюм | Москва».

Товары:

{products or "Товаров пока нет."}

Соблюдай:

70% полезного
20% вовлечения
10% продаж.

Предложи публикации:

09:00–11:00
13:00–15:00
17:00–19:00
20:00–22:00

Для каждого времени:

- что публиковать;
- формат;
- идея;
- пример текста;
- нужен ли товар;
- нужен ли опрос.

Не превращай день
в четыре рекламных публикации.
"""

    result = await ask_ai(
        prompt
    )

    data["draft"] = result

    await query.message.reply_text(
        result,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Одобрить план",
                        callback_data=(
                            "approve_draft"
                        ),
                    ),
                    InlineKeyboardButton(
                        "✏️ Изменить",
                        callback_data=(
                            "edit_draft"
                        ),
                    ),
                ]
            ]
        ),
    )


# =========================================================
# ПЛАН НА НЕДЕЛЮ
# =========================================================

async def weekly_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    products = "\n".join(
        [
            p["text"]
            for p in data["products"][-15:]
        ]
    )

    prompt = f"""
Составь полноценный контент-план
на 7 дней для Telegram-канала
«Косметика | Парфюм | Москва».

Товары:

{products or "Товаров пока нет."}

Главная стратегия:

70% полезное
20% вовлечение
10% продажи.

Для каждого дня:

- основная тема;
- основной пост;
- 2–4 коротких сообщения;
- идея вовлечения;
- идея опроса;
- товарная интеграция,
  если уместна;
- идея Reels/TikTok.

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

Не делай каждый день продажным.
"""

    result = await ask_ai(
        prompt
    )

    data["draft"] = result

    await query.message.reply_text(
        result,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Одобрить план",
                        callback_data=(
                            "approve_draft"
                        ),
                    ),
                    InlineKeyboardButton(
                        "✏️ Изменить",
                        callback_data=(
                            "edit_draft"
                        ),
                    ),
                ]
            ]
        ),
    )


# =========================================================
# ОПРОС
# =========================================================

async def poll_creator(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    prompt = """
Придумай интересный Telegram-опрос
для аудитории канала косметики
и парфюмерии.

Опрос должен быть простым
и мотивировать нажать кнопку.

Верни СТРОГО JSON:

{
  "question": "Вопрос",
  "options": [
    "Вариант 1",
    "Вариант 2",
    "Вариант 3",
    "Вариант 4"
  ]
}

От 2 до 4 вариантов.
"""

    result = await ask_ai(
        prompt
    )

    try:

        cleaned = result.strip()

        if "```" in cleaned:

            cleaned = (
                cleaned
                .replace(
                    "```json",
                    "",
                )
                .replace(
                    "```",
                    "",
                )
                .strip()
            )

        poll = json.loads(
            cleaned
        )

        question = poll[
            "question"
        ]

        options = poll[
            "options"
        ][:4]

        if len(options) < 2:

            raise ValueError(
                "Too few poll options"
            )

        data = get_user_data(
            update.effective_user.id
        )

        data[
            "pending_poll"
        ] = {
            "question": question,
            "options": options,
        }

        preview = (
            f"🗳️ <b>Опрос готов</b>\n\n"
            f"{question}\n\n"
        )

        for option in options:

            preview += (
                f"• {option}\n"
            )

        await query.message.reply_text(
            preview,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Одобрить и создать",
                            callback_data=(
                                "approve_poll"
                            ),
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🔄 Другой опрос",
                            callback_data=(
                                "poll"
                            ),
                        ),
                    ],
                ]
            ),
        )

    except Exception as e:

        logger.warning(
            "Poll parsing failed: %s",
            e,
        )

        await query.message.reply_text(
            """
Не удалось автоматически
оформить опрос.

Нажми «🗳️ Создать опрос»
ещё раз.
""",
            reply_markup=main_menu(),
        )


# =========================================================
# СОЗДАТЬ РЕАЛЬНЫЙ TELEGRAM ОПРОС
# =========================================================

async def approve_poll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    poll = data.get(
        "pending_poll"
    )

    if not poll:

        await query.message.reply_text(
            "Опрос уже не найден."
        )

        return

    await context.bot.send_poll(
        chat_id=(
            update.effective_chat.id
        ),
        question=(
            poll["question"]
        ),
        options=(
            poll["options"]
        ),
        is_anonymous=True,
    )

    data[
        "pending_poll"
    ] = None

    await query.message.reply_text(
        "✅ Опрос создан!",
        reply_markup=main_menu(),
    )


# =========================================================
# ВОВЛЕЧЕНИЕ
# =========================================================

async def engagement(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    prompt = """
Придумай 10 идей для вовлечения
аудитории Telegram-канала косметики
и парфюмерии.

Используй:

- «Что бы выбрала ты?»
- «Угадайте цену»
- «Выбираем вместе»
- мини-тест;
- вопрос;
- реакцию;
- сравнение товаров.

Идеи должны быть простыми
для реализации.
"""

    result = await ask_ai(
        prompt
    )

    await query.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# РУБРИКИ
# =========================================================

async def rubrics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    prompt = """
Придумай 15 постоянных рубрик
для Telegram-канала косметики
и парфюмерии.

Для каждой:

- название;
- о чём;
- частота;
- пример первой публикации.

Не повторяй банальные идеи.
"""

    result = await ask_ai(
        prompt
    )

    await query.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# ИДЕИ ДЛЯ РОСТА
# =========================================================

async def growth(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    prompt = """
Придумай практический план роста
Telegram-канала
«Косметика | Парфюм | Москва».

Учитывай:

- TikTok;
- Instagram Reels;
- Instagram Stories;
- Telegram;
- взаимные рекомендации;
- рекламные размещения;
- эксклюзивный Telegram-контент;
- отзывы;
- распаковки;
- подборки.

Главный приоритет —
активная аудитория.

Дай 15 конкретных действий.
"""

    result = await ask_ai(
        prompt
    )

    await query.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# ЧТО ПУБЛИКОВАТЬ
# =========================================================

async def what_to_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    products = "\n".join(
        [
            p["text"]
            for p in data["products"][-10:]
        ]
    )

    prompt = f"""
Представь, что Аня прямо сейчас
открыла Telegram и спрашивает:

«Что мне сегодня опубликовать?»

Товары:

{products or "Товаров пока нет."}

Предложи:

1. что опубликовать сейчас;
2. что через несколько часов;
3. что вечером.

Учитывай:

70% полезного
20% вовлечения
10% продаж.

Не предлагай продажу
просто ради продажи.
"""

    result = await ask_ai(
        prompt
    )

    await query.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# МОИ ТОВАРЫ
# =========================================================

async def show_products(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = get_user_data(
        update.effective_user.id
    )

    if not data["products"]:

        await query.message.reply_text(
            """
📋 Товаров пока нет.

Нажми «📦 Новый товар»
и добавь первый.
""",
            reply_markup=main_menu(),
        )

        return

    text = (
        "📋 <b>Твои товары:</b>\n\n"
    )

    for i, product in enumerate(
        data["products"],
        1,
    ):

        short = product[
            "text"
        ][:800]

        text += (
            f"<b>{i}.</b>\n"
            f"{short}\n\n"
        )

    await query.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# =========================================================
# ФОТО
# =========================================================

async def photo_section(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        """
📸 <b>Работа с фото</b>

Сейчас здесь можно:

• прислать фото товара;
• улучшить его в следующем этапе;
• подготовить изображение
  для карточки;
• использовать фото
  для публикации.

Пришли фотографию
и напиши, что нужно сделать.
""",
        parse_mode="HTML",
    )


# =========================================================
# РЕДАКТИРОВАНИЕ ТЕКСТА
# =========================================================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    uid = update.effective_user.id

    data = get_user_data(uid)

    text = (
        update.message.text
        or ""
    )

    mode = data.get(
        "mode"
    )

    # -----------------------------------------------------
    # РЕДАКТИРОВАНИЕ ТОВАРА
    # -----------------------------------------------------

    if mode == "edit_product":

        old = data.get(
            "last_product",
            "",
        )

        prompt = f"""
Вот текущая карточка товара:

{old}

Аня просит изменить её:

{text}

Перепиши карточку
с учётом просьбы.

Не меняй подтверждённую
цену или количество,
если Аня прямо не попросила
их изменить.
"""

        result = await ask_ai(
            prompt
        )

        data[
            "last_product"
        ] = result

        data["mode"] = None

        await update.message.reply_text(
            result,
            reply_markup=product_keyboard(),
        )

        return

    # -----------------------------------------------------
    # РЕДАКТИРОВАНИЕ ЧЕРНОВИКА
    # -----------------------------------------------------

    if mode == "edit_draft":

        old = data.get(
            "draft",
            "",
        )

        prompt = f"""
Вот текущий черновик:

{old}

Аня просит:

{text}

Перепиши черновик
с учётом её просьбы.
"""

        result = await ask_ai(
            prompt
        )

        data[
            "draft"
        ] = result

        data["mode"] = None

        await update.message.reply_text(
            result,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Одобрить",
                            callback_data=(
                                "approve_draft"
                            ),
                        ),
                        InlineKeyboardButton(
                            "✏️ Изменить",
                            callback_data=(
                                "edit_draft"
                            ),
                        ),
                    ]
                ]
            ),
        )

        return

    # -----------------------------------------------------
    # ОБЫЧНЫЙ AI-ЗАПРОС
    # -----------------------------------------------------

    result = await ask_ai(
        f"""
Аня написала:

{text}

Ответь как её AI-помощник
по развитию Telegram-канала
косметики и парфюмерии.

Дай конкретный ответ.
"""
    )

    await update.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    action = query.data

    if action == "new_product":

        await start_new_product(
            update,
            context,
        )

    elif action == "products":

        await show_products(
            update,
            context,
        )

    elif action == "post":

        await create_post(
            update,
            context,
        )

    elif action == "stories":

        await create_stories(
            update,
            context,
        )

    elif action == "reels":

        await create_reels(
            update,
            context,
        )

    elif action == "today":

        await daily_plan(
            update,
            context,
        )

    elif action == "week":

        await weekly_plan(
            update,
            context,
        )

    elif action == "poll":

        await poll_creator(
            update,
            context,
        )

    elif action == "approve_poll":

        await approve_poll(
            update,
            context,
        )

    elif action == "engagement":

        await engagement(
            update,
            context,
        )

    elif action == "rubrics":

        await rubrics(
            update,
            context,
        )

    elif action == "growth":

        await growth(
            update,
            context,
        )

    elif action == "now":

        await what_to_post(
            update,
            context,
        )

    elif action == "photo":

        await photo_section(
            update,
            context,
        )

    elif action == "approve_product":

        await approve_product(
            update,
            context,
        )

    elif action == "edit_product":

        await edit_product(
            update,
            context,
        )

    elif action == "another_photo":

        await another_photo(
            update,
            context,
        )

    elif action == "approve_draft":

        await query.message.reply_text(
            """
✅ <b>Черновик одобрен!</b>

Он сохранён как готовый контент.

Автоматически в канал
пока ничего не публикую.
""",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

    elif action == "edit_draft":

        data = get_user_data(
            update.effective_user.id
        )

        data["mode"] = (
            "edit_draft"
        )

        await query.message.reply_text(
            """
✏️ Напиши, что изменить.

Например:

«Сделай короче»

«Добавь больше вовлечения»

«Сделай менее рекламным»

«Добавь опрос»

«Сделай стиль более живым»
"""
        )


# =========================================================
# TELEGRAM КОМАНДЫ
# =========================================================

async def setup_commands():

    commands = [

        BotCommand(
            "start",
            "Главное меню",
        ),

        BotCommand(
            "new",
            "Новый товар",
        ),

        BotCommand(
            "post",
            "Создать пост",
        ),

        BotCommand(
            "stories",
            "Создать сторис",
        ),

        BotCommand(
            "poll",
            "Создать опрос",
        ),

        BotCommand(
            "today",
            "План на сегодня",
        ),

        BotCommand(
            "week",
            "План на неделю",
        ),

        BotCommand(
            "growth",
            "Идеи для роста",
        ),
    ]

    await telegram_app.bot.set_my_commands(
        commands
    )

    try:

        await telegram_app.bot.set_chat_menu_button(
            menu_button=MenuButtonCommands()
        )

    except Exception as e:

        logger.warning(
            "Could not set menu button: %s",
            e,
        )


# =========================================================
# КОМАНДА /NEW
# =========================================================

async def new_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    data = get_user_data(
        update.effective_user.id
    )

    data["mode"] = (
        "new_product"
    )

    await update.message.reply_text(
        """
📦 <b>Новый товар</b>

Пришли фотографию товара,
цену и количество.

Например:

3500 ₽
В наличии 5 шт.
""",
        parse_mode="HTML",
    )


# =========================================================
# КОМАНДА /POST
# =========================================================

async def post_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    prompt = """
Создай готовый Telegram-пост
для канала косметики и парфюмерии.

Сделай его интересным,
полезным и живым.

Не превращай его
в прямую рекламу.
"""

    result = await ask_ai(
        prompt
    )

    await update.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# КОМАНДА /TODAY
# =========================================================

async def today_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    prompt = """
Составь контент-план
на сегодня для канала
косметики и парфюмерии.

70% полезного
20% вовлечения
10% продаж.

Дай конкретные темы
и примерные часы.
"""

    result = await ask_ai(
        prompt
    )

    await update.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# КОМАНДА /WEEK
# =========================================================

async def week_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    prompt = """
Составь контент-план
на 7 дней для канала
косметики и парфюмерии.

70% полезного
20% вовлечения
10% продаж.

Для каждого дня:
основной пост,
короткие сообщения,
вовлечение,
продажа только там,
где она уместна.
"""

    result = await ask_ai(
        prompt
    )

    await update.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# FASTAPI
# =========================================================

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


# =========================================================
# SETUP
# =========================================================

async def setup():

    if not telegram_app:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set"
        )

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "help",
            help_cmd,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "new",
            new_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "post",
            post_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "today",
            today_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "week",
            week_command,
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
            product_photo,
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_message,
        )
    )

    await telegram_app.initialize()

    await telegram_app.start()

    await setup_commands()


# =========================================================
# STARTUP
# =========================================================

@app.on_event(
    "startup"
)
async def startup():

    await setup()

    await telegram_app.updater.start_polling()


# =========================================================
# SHUTDOWN
# =========================================================

@app.on_event(
    "shutdown"
)
async def shutdown():

    if telegram_app:

        await telegram_app.updater.stop()

        await telegram_app.stop()

        await telegram_app.shutdown()
