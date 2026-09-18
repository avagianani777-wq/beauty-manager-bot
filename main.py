import os
import logging
import base64
import re

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
# AI PROMPT
# ============================================================

SYSTEM_PROMPT = """
Ты — Beauty Manager, персональный AI-помощник владельца
Telegram-магазина «Косметика | Парфюм | Москва».

Магазин продаёт оригинальную косметику, парфюмерию
и средства ухода.

ТВОЯ ЗАДАЧА:

1. Определить товар по фотографии.
2. Если возможно — определить:
   - бренд;
   - точное название;
   - категорию;
   - оттенок;
   - объём/вес;
   - другие данные, которые действительно видны.
3. Если пользователь дал цену — использовать ИМЕННО её.
4. Если пользователь дал количество — использовать ИМЕННО его.
5. Найти актуальную информацию о товаре в интернете.
6. В первую очередь ориентироваться на:
   - официальный сайт бренда;
   - официальные страницы продукта;
   - крупные авторитетные магазины.
7. Не выдумывать характеристики.
8. Если разные сайты противоречат друг другу — сообщить об этом.
9. Не выдавать догадки за факты.
10. Не менять цену пользователя на найденную в интернете.
11. Написать короткое красивое описание товара для Telegram-магазина.
12. Не делать описание слишком длинным.
13. Не использовать агрессивные продажи.
14. Не использовать слишком много эмодзи.

ВАЖНО:

Если товар точно не удалось определить,
напиши, что идентификация требует уточнения.

Если информации недостаточно,
НЕ придумывай её.

Формат готовой карточки:

✨ БРЕНД — НАЗВАНИЕ

Короткое описание в 2–4 предложениях.

🤍 Объём/оттенок: ...
💰 Цена: ...
📦 В наличии: ...

В конце:

Источники:
1. ...
2. ...

После карточки обязательно добавь:

«Проверь, всё ли верно. После твоего одобрения можно подготовить публикацию.»
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


def approval_keyboard():
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
                "🔄 Новый товар",
                callback_data="new_product"
            ),
        ],
    ])


# ============================================================
# MENU
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

    await telegram_app.bot.set_my_commands(commands)

    await telegram_app.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data.clear()

    await update.message.reply_text(
        "Привет! 💗\n\n"
        "Я Beauty Manager — твой AI-помощник "
        "для «Косметика | Парфюм | Москва».\n\n"
        "Теперь можешь просто отправить мне:\n\n"
        "📸 фотографию товара\n"
        "💰 цену\n"
        "📦 количество\n\n"
        "А дальше я сама разберусь с товаром, "
        "найду информацию и подготовлю красивую карточку.\n\n"
        "И главное — ничего не публикую без твоего одобрения.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# NEW PRODUCT
# ============================================================

async def new_product(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data.clear()

    await update.message.reply_text(
        "🆕 Начинаем новый товар.\n\n"
        "Пришли фотографию товара.\n\n"
        "Лучше сразу написать вместе с фото:\n"
        "💰 цену\n"
        "📦 количество\n\n"
        "Например:\n"
        "«3500 ₽, в наличии 2 шт.»"
    )


# ============================================================
# COMMANDS
# ============================================================

async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["mode"] = "post"

    await update.message.reply_text(
        "✍️ Хорошо.\n\n"
        "Пришли товар или напиши тему поста."
    )


async def stories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["mode"] = "stories"

    await update.message.reply_text(
        "📱 Пришли товар или тему.\n\n"
        "Я подготовлю серию сторис."
    )


async def reels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["mode"] = "reels"

    await update.message.reply_text(
        "🎥 Напиши товар или тему.\n\n"
        "Придумаю идеи Reels/TikTok."
    )


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["mode"] = "plan"

    await update.message.reply_text(
        "📅 Пришли список товаров "
        "или напиши, что сейчас нужно продвигать."
    )


# ============================================================
# AI — TEXT
# ============================================================

async def ask_ai(
    text: str,
    image_bytes: bytes | None = None,
    web_search: bool = False,
) -> str:

    if not openai_client:
        return (
            "⚠️ OpenAI пока не подключён.\n\n"
            "Проверь OPENAI_API_KEY в Render."
        )

    content = []

    content.append({
        "type": "input_text",
        "text": text,
    })

    if image_bytes:

        encoded = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        image_data_url = (
            "data:image/jpeg;base64,"
            + encoded
        )

        content.append({
            "type": "input_image",
            "image_url": image_data_url,
        })

    tools = []

    if web_search:
        tools.append({
            "type": "web_search"
        })

    try:

        response = await openai_client.responses.create(

            model=TEXT_MODEL,

            instructions=SYSTEM_PROMPT,

            tools=tools,

            input=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
        )

        return response.output_text

    except Exception as e:

        logger.exception(
            "OpenAI request failed"
        )

        return (
            "Не получилось обработать запрос 😔\n\n"
            "Техническая ошибка.\n"
            "Проверь логи Render."
        )


# ============================================================
# PHOTO
# ============================================================

async def photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    telegram_photo = update.message.photo[-1]

    file = await telegram_photo.get_file()

    image_bytes = bytes(
        await file.download_as_bytearray()
    )

    caption = (
        update.message.caption
        or ""
    )

    # сохраняем фото
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

    await update.message.reply_text(
        "🔎 Рассматриваю товар и ищу информацию о нём...\n\n"
        "Это может занять немного времени."
    )

    user_request = f"""
Пользователь прислал фотографию товара.

Дополнительная информация от пользователя:
{caption if caption else "Не указана"}

Сделай следующее:

1. Определи товар по фотографии.
2. Найди точное название и бренд.
3. Найди подтверждённую информацию о товаре в интернете.
4. Проверь информацию минимум по нескольким источникам,
   если это возможно.
5. Приоритет — официальный сайт бренда.
6. Подготовь короткое описание для магазина.
7. Если пользователь указал цену — используй её.
8. Если указал количество — используй его.
9. Не придумывай отсутствующие данные.

Верни готовую карточку товара.
"""

    answer = await ask_ai(
        user_request,
        image_bytes=image_bytes,
        web_search=True,
    )

    await update.message.reply_text(
        answer,
        reply_markup=product_keyboard(),
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

    if mode == "product":

        image_bytes = context.user_data.get(
            "product_image"
        )

        answer = await ask_ai(
            text,
            image_bytes=image_bytes,
            web_search=True,
        )

    else:

        mode_prompt = ""

        if mode == "post":
            mode_prompt = """
Сделай готовый продающий пост
для Telegram-магазина косметики.
"""

        elif mode == "stories":
            mode_prompt = """
Сделай серию коротких сторис.
Каждая сторис должна быть отдельным экраном.
"""

        elif mode == "reels":
            mode_prompt = """
Предложи конкретные идеи Reels/TikTok.
Для каждой дай:
сюжет, первую фразу, текст на экране
и CTA.
"""

        elif mode == "plan":
            mode_prompt = """
Составь реалистичный контент-план
для Telegram-магазина на 7 дней.
"""

        else:
            mode_prompt = """
Ответь как личный AI-помощник
владельца магазина косметики.
"""

        answer = await ask_ai(
            mode_prompt + "\n\n" + text,
            web_search=False,
        )

    context.user_data["mode"] = None

    await update.message.reply_text(
        answer,
        reply_markup=main_keyboard(),
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

    # --------------------------------------------
    # NEW PRODUCT
    # --------------------------------------------

    if data == "new_product":

        context.user_data.clear()

        await query.message.reply_text(
            "🔄 Начинаем новый товар.\n\n"
            "Пришли фото + цену + количество."
        )

        return

    # --------------------------------------------
    # APPROVE
    # --------------------------------------------

    if data == "approve_product":

        context.user_data[
            "approved"
        ] = True

        await query.message.reply_text(
            "✅ Товар одобрен!\n\n"
            "Карточка сохранена.\n\n"
            "Следующий этап — подключим "
            "подготовку финального рекламного изображения "
            "и публикацию в канал.",
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

    # --------------------------------------------
    # EDIT
    # --------------------------------------------

    if data == "edit_product":

        context.user_data[
            "mode"
        ] = "edit_product"

        await query.message.reply_text(
            "✏️ Напиши, что именно изменить.\n\n"
            "Например:\n"
            "«Сделай описание короче»\n"
            "«Убери эмодзи»\n"
            "«Добавь, что оттенок 02 Auburn»\n"
            "«Измени цену на 3900 ₽»"
        )

        return

    # --------------------------------------------
    # ANOTHER PHOTO
    # --------------------------------------------

    if data == "another_photo":

        context.user_data.pop(
            "product_image",
            None
        )

        await query.message.reply_text(
            "🖼️ Хорошо.\n\n"
            "Пришли новое фото этого товара."
        )

        return

    # --------------------------------------------
    # POST
    # --------------------------------------------

    if data == "post":

        context.user_data[
            "mode"
        ] = "post"

        await query.message.reply_text(
            "✍️ Напиши, о каком товаре "
            "или теме сделать пост."
        )

        return

    # --------------------------------------------
    # STORIES
    # --------------------------------------------

    if data == "stories":

        context.user_data[
            "mode"
        ] = "stories"

        await query.message.reply_text(
            "📱 Напиши товар или тему "
            "для серии сторис."
        )

        return

    # --------------------------------------------
    # REELS
    # --------------------------------------------

    if data == "reels":

        context.user_data[
            "mode"
        ] = "reels"

        await query.message.reply_text(
            "🎥 Напиши товар или тему "
            "для Reels/TikTok."
        )

        return

    # --------------------------------------------
    # PLAN
    # --------------------------------------------

    if data == "plan":

        context.user_data[
            "mode"
        ] = "plan"

        await query.message.reply_text(
            "📅 Пришли список товаров "
            "или напиши, что сейчас нужно продвигать."
        )

        return

    # --------------------------------------------
    # HOME
    # --------------------------------------------

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
