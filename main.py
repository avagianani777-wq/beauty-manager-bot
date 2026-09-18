import os
import logging
import base64
import re
import io
import json

import httpx
from fastapi import FastAPI
from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
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
# SETTINGS
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Оставляем твою текущую модель.
TEXT_MODEL = "gpt-5.6-luna"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


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


# ============================================================
# AI SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
Ты — Beauty Manager, AI-помощник владельца
Telegram-магазина «Косметика | Парфюм | Москва».

Магазин продаёт оригинальную косметику,
парфюмерию и средства ухода.

ТВОЯ ОСНОВНАЯ ЗАДАЧА:

Пользователь присылает фотографию товара
и информацию о цене/количестве.

Ты должна:

1. Определить товар по фотографии.

2. Определить, если возможно:
   — бренд;
   — точное название;
   — категорию;
   — оттенок;
   — объём или вес;
   — другие характеристики, которые подтверждаются.

3. Найти информацию о товаре в интернете.

4. В первую очередь использовать:
   — официальный сайт бренда;
   — официальную страницу товара;
   — крупные авторитетные магазины.

5. Не выдумывать характеристики.

6. Не выдумывать состав.

7. Не выдумывать оттенок.

8. Не выдумывать объём.

9. Не выдумывать наличие.

10. НЕ менять цену пользователя.

11. НЕ менять количество товара,
если пользователь его указал.

12. Найти изображения именно этого товара.

13. При поиске изображения отдавать предпочтение:
   — официальному сайту бренда;
   — официальным изображениям продукта;
   — крупным магазинам.

14. Изображение должно максимально точно
соответствовать определённому товару.

15. Не подменять товар похожим продуктом.

16. Если точное изображение найти не удалось,
использовать фотографию пользователя.

ОПИСАНИЕ:

Описание должно быть коротким,
красивым и подходящим для Telegram-магазина.

Не пиши длинную статью.

Формат:

✨ БРЕНД — НАЗВАНИЕ

Короткое описание в 2–4 предложениях.

🤍 Оттенок: ...
🤍 Объём: ...

💰 Цена: ...
📦 В наличии: ...

ВАЖНО:

Цена и количество берутся только из сообщения пользователя.

Цена, найденная в интернете, НЕ должна использоваться
вместо цены пользователя.

После карточки напиши:

«Проверь, всё ли верно 💗»

НЕ публикуй ничего самостоятельно.
Публикация возможна только после одобрения владельца.
"""


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🆕 Новый товар",
                callback_data="new_product"
            )
        ],
        [
            InlineKeyboardButton(
                "✍️ Создать пост",
                callback_data="post"
            ),
            InlineKeyboardButton(
                "📱 Сторис",
                callback_data="stories"
            ),
        ],
        [
            InlineKeyboardButton(
                "🎥 Reels/TikTok",
                callback_data="reels"
            ),
            InlineKeyboardButton(
                "📅 План",
                callback_data="plan"
            ),
        ],
    ])


def product_keyboard():

    return InlineKeyboardMarkup([
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
        ],
        [
            InlineKeyboardButton(
                "🔄 Новый товар",
                callback_data="new_product"
            ),
        ],
    ])


# ============================================================
# TELEGRAM MENU
# ============================================================

async def configure_bot_menu():

    if not telegram_app:
        return

    commands = [
        BotCommand(
            "start",
            "Главное меню"
        ),
        BotCommand(
            "new",
            "Новый товар"
        ),
        BotCommand(
            "post",
            "Создать пост"
        ),
        BotCommand(
            "stories",
            "Создать сторис"
        ),
        BotCommand(
            "reels",
            "Идеи Reels/TikTok"
        ),
        BotCommand(
            "plan",
            "План на неделю"
        ),
    ]

    await telegram_app.bot.set_my_commands(
        commands
    )

    await telegram_app.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()

    await update.message.reply_text(
        "Привет! 💗\n\n"
        "Я Beauty Manager — твой AI-помощник "
        "для «Косметика | Парфюм | Москва».\n\n"

        "Просто отправь мне:\n"
        "📸 фото товара\n"
        "💰 цену\n"
        "📦 количество\n\n"

        "Я попробую определить товар, "
        "найти подтверждённую информацию "
        "и подобрать фотографию из интернета.\n\n"

        "После этого покажу тебе готовую карточку.\n\n"

        "Ничего не публикую без твоего одобрения 💗",

        reply_markup=main_keyboard(),
    )


# ============================================================
# NEW PRODUCT
# ============================================================

async def new_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()

    await update.message.reply_text(
        "🆕 Начинаем новый товар.\n\n"

        "Отправь фотографию товара.\n\n"

        "Лучше сразу добавить:\n"
        "💰 цену\n"
        "📦 количество\n\n"

        "Например:\n"
        "«3500 ₽, в наличии 5 шт.»"
    )


# ============================================================
# OTHER COMMANDS
# ============================================================

async def post_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["mode"] = "post"

    await update.message.reply_text(
        "✍️ Напиши, о каком товаре "
        "или теме сделать пост."
    )


async def stories_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["mode"] = "stories"

    await update.message.reply_text(
        "📱 Напиши товар или тему.\n\n"
        "Я подготовлю серию сторис."
    )


async def reels_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["mode"] = "reels"

    await update.message.reply_text(
        "🎥 Напиши товар или тему.\n\n"
        "Я предложу идеи Reels/TikTok."
    )


async def plan_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["mode"] = "plan"

    await update.message.reply_text(
        "📅 Пришли список товаров "
        "или напиши, что сейчас нужно продвигать."
    )


# ============================================================
# OPENAI REQUEST
# ============================================================

async def create_ai_response(
    prompt: str,
    image_bytes: bytes | None = None,
    search_images: bool = False,
):

    if not openai_client:

        raise RuntimeError(
            "OPENAI_API_KEY is not configured"
        )

    content = [
        {
            "type": "input_text",
            "text": prompt,
        }
    ]

    # --------------------------------------------------------
    # ADD USER IMAGE
    # --------------------------------------------------------

    if image_bytes:

        encoded = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        content.append({
            "type": "input_image",
            "image_url": (
                "data:image/jpeg;base64,"
                + encoded
            ),
        })

    tools = []

    # --------------------------------------------------------
    # WEB SEARCH
    # --------------------------------------------------------

    if search_images:

        tools.append({
            "type": "web_search",

            "search_content_types": [
                "text",
                "image",
            ],

            "image_settings": {
                "max_results": 5,
                "caption": True,
            },

            "search_context_size": "high",
        })

    elif search_images is False:

        pass

    kwargs = {
        "model": TEXT_MODEL,
        "instructions": SYSTEM_PROMPT,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
    }

    if tools:

        kwargs["tools"] = tools

        kwargs["include"] = [
            "web_search_call.results"
        ]

    response = await openai_client.responses.create(
        **kwargs
    )

    return response


# ============================================================
# FIND URLS IN OPENAI SEARCH RESULTS
# ============================================================

def collect_urls(value):

    urls = []

    if value is None:
        return urls

    if isinstance(value, str):

        found = re.findall(
            r'https?://[^\s"\'<>]+',
            value
        )

        urls.extend(found)

        return urls

    if isinstance(value, dict):

        for key, item in value.items():

            if key in {
                "url",
                "image_url",
                "source_url",
                "thumbnail_url",
            }:

                if isinstance(item, str):

                    urls.append(item)

            else:

                urls.extend(
                    collect_urls(item)
                )

        return urls

    if isinstance(value, list):

        for item in value:

            urls.extend(
                collect_urls(item)
            )

        return urls

    return urls


def clean_url(url):

    if not url:
        return None

    url = url.strip()

    url = url.rstrip(
        ".,);]}>"
    )

    return url


# ============================================================
# GET IMAGE URLS FROM SEARCH
# ============================================================

def extract_image_urls(response):

    urls = []

    try:

        raw = response.model_dump()

        urls.extend(
            collect_urls(raw)
        )

    except Exception:

        try:

            urls.extend(
                collect_urls(
                    response.output
                )
            )

        except Exception:
            pass

    cleaned = []

    for url in urls:

        url = clean_url(url)

        if not url:
            continue

        lower = url.lower()

        if any(
            ext in lower
            for ext in [
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".gif",
            ]
        ):

            if url not in cleaned:

                cleaned.append(url)

    return cleaned


# ============================================================
# DOWNLOAD IMAGE
# ============================================================

async def download_image(url):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/131 Safari/537.36"
        )
    }

    try:

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20,
            headers=headers,
        ) as client:

            response = await client.get(
                url
            )

            response.raise_for_status()

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

            if len(response.content) < 1000:

                return None

            # Telegram has limits, so don't
            # download absurdly large files.
            if len(response.content) > 15 * 1024 * 1024:

                return None

            return response.content

    except Exception:

        logger.exception(
            "Failed to download image: %s",
            url
        )

        return None


# ============================================================
# SEARCH PRODUCT + IMAGE
# ============================================================

async def search_product(
    image_bytes: bytes,
    user_text: str,
):

    prompt = f"""
Пользователь прислал фотографию товара.

Дополнительная информация пользователя:

{user_text if user_text else "Нет дополнительной информации."}

Тебе нужно провести полноценный поиск товара.

Сначала определи товар по фотографии.

Затем найди:

1. официальный бренд;
2. точное название;
3. оттенок;
4. объём/вес;
5. подтверждённое описание;
6. официальную или максимально надёжную страницу товара;
7. хорошие изображения именно этого товара.

ОСОБЕННО ВАЖНО:

Мне нужны не только текстовые страницы.

Используй поиск изображений.

Найди 1–5 изображений именно этого товара.

Приоритет:

1. официальный сайт бренда;
2. официальное изображение продукта;
3. крупный авторитетный магазин.

Не выбирай фотографию похожего товара.

Если найдено несколько вариантов,
предпочти изображение, на котором:

— хорошо виден весь продукт;
— хорошо читается упаковка;
— нет лишних людей;
— нет большого количества текста;
— нет водяных знаков, если возможно;
— товар точно соответствует определённому продукту.

Цена пользователя НЕ должна заменяться
ценой из интернета.

Количество пользователя НЕ должно заменяться
данными из интернета.

Подготовь красивую короткую карточку товара.

В ответе сначала дай карточку,
а источники можешь указать после неё.
"""

    response = await create_ai_response(
        prompt=prompt,
        image_bytes=image_bytes,
        search_images=True,
    )

    answer = response.output_text

    image_urls = extract_image_urls(
        response
    )

    logger.info(
        "Found %s image URLs",
        len(image_urls)
    )

    # --------------------------------------------------------
    # DOWNLOAD BEST IMAGE
    # --------------------------------------------------------

    downloaded_image = None

    for url in image_urls[:5]:

        downloaded_image = (
            await download_image(url)
        )

        if downloaded_image:

            logger.info(
                "Downloaded image: %s",
                url
            )

            break

    return answer, downloaded_image


# ============================================================
# PHOTO MESSAGE
# ============================================================

async def photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    telegram_photo = (
        update.message.photo[-1]
    )

    file = await telegram_photo.get_file()

    image_bytes = bytes(
        await file.download_as_bytearray()
    )

    caption = (
        update.message.caption
        or ""
    )

    # --------------------------------------------------------
    # SAVE USER PHOTO
    # --------------------------------------------------------

    context.user_data[
        "product_image"
    ] = image_bytes

    context.user_data[
        "product_caption"
    ] = caption

    context.user_data[
        "mode"
    ] = "product"

    await update.message.chat.send_action(
        "typing"
    )

    status_message = await update.message.reply_text(
        "🔎 Определяю товар...\n\n"
        "🌐 Ищу подтверждённую информацию...\n\n"
        "🖼️ Ищу фотографии именно этого товара..."
    )

    try:

        answer, online_image = (
            await search_product(
                image_bytes,
                caption,
            )
        )

        # ----------------------------------------------------
        # DELETE STATUS MESSAGE
        # ----------------------------------------------------

        try:

            await status_message.delete()

        except Exception:

            pass

        # ----------------------------------------------------
        # ONLINE IMAGE FOUND
        # ----------------------------------------------------

        if online_image:

            context.user_data[
                "online_product_image"
            ] = online_image

            await update.message.reply_photo(
                photo=io.BytesIO(
                    online_image
                ),
                caption=answer,
                reply_markup=product_keyboard(),
            )

        # ----------------------------------------------------
        # FALLBACK TO USER PHOTO
        # ----------------------------------------------------

        else:

            context.user_data[
                "online_product_image"
            ] = None

            await update.message.reply_photo(
                photo=io.BytesIO(
                    image_bytes
                ),
                caption=(
                    "📸 Подходящее изображение "
                    "из поиска не удалось получить.\n\n"
                    + answer
                ),
                reply_markup=product_keyboard(),
            )

    except Exception:

        logger.exception(
            "Product processing failed"
        )

        try:

            await status_message.delete()

        except Exception:

            pass

        await update.message.reply_text(
            "Не получилось полностью обработать товар 😔\n\n"
            "Я сохранила твоё фото.\n"
            "Попробуй ещё раз или нажми «Новый товар»."
        )


# ============================================================
# TEXT MESSAGE
# ============================================================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text or ""

    mode = context.user_data.get(
        "mode"
    )

    # --------------------------------------------------------
    # PRODUCT EDIT
    # --------------------------------------------------------

    if mode == "edit_product":

        old_answer = context.user_data.get(
            "product_answer",
            ""
        )

        image_bytes = context.user_data.get(
            "product_image"
        )

        prompt = f"""
Владелец магазина хочет изменить
готовую карточку товара.

Предыдущая карточка:

{old_answer}

Запрос владельца:

{text}

Измени только то, что попросил владелец.

Не выдумывай новые характеристики.

Если владелец изменил цену,
используй новую цену.

Верни полностью готовую карточку.
"""

        try:

            response = await create_ai_response(
                prompt=prompt,
                image_bytes=image_bytes,
                search_images=False,
            )

            answer = response.output_text

            context.user_data[
                "product_answer"
            ] = answer

            context.user_data[
                "mode"
            ] = "product"

            online_image = context.user_data.get(
                "online_product_image"
            )

            if online_image:

                await update.message.reply_photo(
                    photo=io.BytesIO(
                        online_image
                    ),
                    caption=answer,
                    reply_markup=product_keyboard(),
                )

            elif image_bytes:

                await update.message.reply_photo(
                    photo=io.BytesIO(
                        image_bytes
                    ),
                    caption=answer,
                    reply_markup=product_keyboard(),
                )

            else:

                await update.message.reply_text(
                    answer,
                    reply_markup=product_keyboard(),
                )

        except Exception:

            logger.exception(
                "Product edit failed"
            )

            await update.message.reply_text(
                "Не получилось изменить карточку 😔"
            )

        return

    # --------------------------------------------------------
    # NORMAL AI COMMANDS
    # --------------------------------------------------------

    if mode == "post":

        prompt = """
Сделай готовый продающий пост
для Telegram-магазина косметики.

Не выдумывай характеристики товара.
"""

    elif mode == "stories":

        prompt = """
Сделай серию коротких сторис
для Telegram-магазина косметики.

Каждая сторис должна быть отдельным экраном.
"""

    elif mode == "reels":

        prompt = """
Предложи конкретные идеи Reels/TikTok
для магазина косметики.

Для каждой идеи дай:
— сюжет;
— первую фразу;
— текст на экране;
— CTA.
"""

    elif mode == "plan":

        prompt = """
Составь реалистичный контент-план
для Telegram-магазина косметики
на 7 дней.
"""

    else:

        prompt = """
Ответь как AI-помощник
владельца магазина косметики.
"""

    try:

        response = await create_ai_response(
            prompt=prompt + "\n\n" + text,
            search_images=False,
        )

        answer = response.output_text

        context.user_data[
            "mode"
        ] = None

        await update.message.reply_text(
            answer,
            reply_markup=main_keyboard(),
        )

    except Exception:

        logger.exception(
            "Text request failed"
        )

        await update.message.reply_text(
            "Не получилось получить ответ от AI 😔"
        )


# ============================================================
# BUTTONS
# ============================================================

async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    data = query.data

    # --------------------------------------------------------
    # NEW PRODUCT
    # --------------------------------------------------------

    if data == "new_product":

        context.user_data.clear()

        await query.message.reply_text(
            "🔄 Новый товар.\n\n"
            "Пришли фото + цену + количество."
        )

        return

    # --------------------------------------------------------
    # APPROVE
    # --------------------------------------------------------

    if data == "approve_product":

        context.user_data[
            "approved"
        ] = True

        await query.message.reply_text(
            "✅ Товар одобрен! 💗\n\n"

            "Карточка сохранена.\n\n"

            "Публикацию пока не выполняю автоматически.\n"
            "Следующим этапом подключим "
            "финальное оформление и публикацию "
            "в канал.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 Новый товар",
                        callback_data="new_product"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Главное меню",
                        callback_data="home"
                    )
                ],
            ]),
        )

        return

    # --------------------------------------------------------
    # EDIT
    # --------------------------------------------------------

    if data == "edit_product":

        context.user_data[
            "mode"
        ] = "edit_product"

        await query.message.reply_text(
            "✏️ Напиши, что изменить.\n\n"

            "Например:\n"
            "«Сделай описание короче»\n"
            "«Убери эмодзи»\n"
            "«Измени цену на 3900 ₽»\n"
            "«Добавь, что в наличии 3 шт.»"
        )

        return

    # --------------------------------------------------------
    # ANOTHER PHOTO
    # --------------------------------------------------------

    if data == "another_photo":

        context.user_data.pop(
            "online_product_image",
            None
        )

        await query.message.reply_text(
            "🖼️ Хорошо.\n\n"
            "Пришли новое фото этого товара."
        )

        return

    # --------------------------------------------------------
    # POST
    # --------------------------------------------------------

    if data == "post":

        context.user_data[
            "mode"
        ] = "post"

        await query.message.reply_text(
            "✍️ Напиши, о каком товаре "
            "или теме сделать пост."
        )

        return

    # --------------------------------------------------------
    # STORIES
    # --------------------------------------------------------

    if data == "stories":

        context.user_data[
            "mode"
        ] = "stories"

        await query.message.reply_text(
            "📱 Напиши товар или тему "
            "для сторис."
        )

        return

    # --------------------------------------------------------
    # REELS
    # --------------------------------------------------------

    if data == "reels":

        context.user_data[
            "mode"
        ] = "reels"

        await query.message.reply_text(
            "🎥 Напиши товар или тему "
            "для Reels/TikTok."
        )

        return

    # --------------------------------------------------------
    # PLAN
    # --------------------------------------------------------

    if data == "plan":

        context.user_data[
            "mode"
        ] = "plan"

        await query.message.reply_text(
            "📅 Пришли список товаров "
            "или напиши, что продвигать."
        )

        return

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    if data == "home":

        context.user_data.clear()

        await query.message.reply_text(
            "🏠 Главное меню",
            reply_markup=main_keyboard(),
        )

        return


# ============================================================
# SETUP
# ============================================================

async def setup():

    if not telegram_app:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set"
        )

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "new",
            new_product
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
            "stories",
            stories_command
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "reels",
            reels_command
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "plan",
            plan_command
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
            photo_message
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_message
        )
    )

    await telegram_app.initialize()

    await telegram_app.start()

    await configure_bot_menu()


# ============================================================
# FASTAPI
# ============================================================

@app.on_event("startup")
async def startup():

    await setup()

    await telegram_app.updater.start_polling()


@app.on_event("shutdown")
async def shutdown():

    if telegram_app:

        await telegram_app.updater.stop()

        await telegram_app.stop()

        await telegram_app.shutdown()


@app.get("/")
async def root():

    return {
        "status": "ok",
        "service": "beauty-manager-bot",
    }


@app.get("/health")
async def health():

    return {
        "status": "healthy"
        }
