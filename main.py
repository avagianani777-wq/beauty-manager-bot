import os
import logging
import base64
import io

from fastapi import FastAPI
from openai import AsyncOpenAI

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

# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

TEXT_MODEL = "gpt-5.6-luna"
IMAGE_MODEL = "gpt-image-2"

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

# ============================================================
# APP
# ============================================================

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
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
Ты — Beauty Manager, AI-помощник владельца Telegram-канала
«Косметика | Парфюм | Москва».

Канал продаёт оригинальную косметику, парфюмерию
и средства ухода.

Главная задача — помогать владельцу создавать
красивый и продающий контент на основе реальных товаров.

ВАЖНО:

Если пользователь прислал фотографию товара,
ты ОБЯЗАТЕЛЬНО анализируешь само изображение.

Ты можешь распознавать:
— бренд;
— название товара;
— тип продукта;
— объём;
— надписи на упаковке;
— видимые характеристики;
— информацию, которая явно написана на упаковке.

НЕЛЬЗЯ:
— выдумывать характеристики;
— выдумывать состав;
— выдумывать оттенок;
— выдумывать ноты аромата;
— выдумывать наличие;
— выдумывать скидки;
— выдавать предположение за факт.

Если информация плохо читается,
скажи об этом и не придумывай её.

Если пользователь дал цену текстом,
используй эту цену.

Если пользователь дал количество,
используй его.

СТИЛЬ:

Современный, женственный, красивый,
живой и естественный.

Не используй слишком много эмодзи.
Не пиши слишком официально.
Не используй агрессивные продажи.

Посты должны выглядеть как реальные
посты красивого Telegram-магазина косметики,
а не как шаблонный рекламный текст.

Если пользователь просит продающий пост:
— используй информацию с фотографии;
— используй дополнительную информацию пользователя;
— сделай нормальное описание товара;
— укажи цену;
— добавь мягкий призыв к заказу.

Если пользователь просит сторис:
создай несколько коротких сторис.

Если пользователь просит Reels/TikTok:
дай конкретную идею с сюжетом,
первой фразой, текстом на экране
и призывом к действию.

Если пользователь просит описание товара:
сделай красивое описание на основе
только подтверждённой информации.

Ничего самостоятельно не публикуй.
Ты только готовишь материалы для владельца.
"""

# ============================================================
# ГЛАВНАЯ КЛАВИАТУРА
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📦 Добавить товар",
                    callback_data="add",
                )
            ],
            [
                InlineKeyboardButton(
                    "✍️ Сделать пост",
                    callback_data="post",
                )
            ],
            [
                InlineKeyboardButton(
                    "📱 Сделать сторис",
                    callback_data="stories",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎥 Идеи Reels/TikTok",
                    callback_data="reels",
                )
            ],
            [
                InlineKeyboardButton(
                    "📅 План на неделю",
                    callback_data="plan",
                )
            ],
            [
                InlineKeyboardButton(
                    "📸 Работа с фото",
                    callback_data="photo",
                )
            ],
        ]
    )


# ============================================================
# МЕНЮ ПОСЛЕ ФОТО
# ============================================================

def photo_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✨ Улучшить фото",
                    callback_data="improve_photo",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎨 Сделать рекламное фото",
                    callback_data="ad_photo",
                )
            ],
            [
                InlineKeyboardButton(
                    "✍️ Продающий пост",
                    callback_data="photo_post",
                )
            ],
            [
                InlineKeyboardButton(
                    "📱 Серия сторис",
                    callback_data="photo_stories",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎥 Reels/TikTok",
                    callback_data="photo_reels",
                )
            ],
        ]
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data.clear()

    await update.message.reply_text(
        "Привет! 💗\n\n"
        "Я Beauty Manager — твой AI-помощник "
        "для канала «Косметика | Парфюм | Москва».\n\n"
        "Я могу работать с товарами, фотографиями, "
        "постами, сторис и Reels/TikTok.\n\n"
        "Выбирай действие ниже или просто "
        "пришли мне фотографию товара.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# HELP
# ============================================================

async def help_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "Я могу помочь тебе с:\n\n"
        "📦 товарами\n"
        "📸 фотографиями\n"
        "✍️ постами\n"
        "📱 сторис\n"
        "🎥 Reels/TikTok\n"
        "📅 контент-планами\n\n"
        "Просто отправь фото товара "
        "или напиши, что тебе нужно 💗"
    )


# ============================================================
# AI С ФОТОГРАФИЕЙ
# ============================================================

async def ask_ai_with_image(
    image_bytes: bytes,
    user_text: str,
) -> str:

    if not openai_client:

        return (
            "⚠️ OpenAI пока не подключён.\n\n"
            "Проверь переменную OPENAI_API_KEY "
            "в Render."
        )

    try:

        encoded_image = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        image_data_url = (
            "data:image/jpeg;base64,"
            + encoded_image
        )

        response = await openai_client.responses.create(

            model=TEXT_MODEL,

            instructions=SYSTEM_PROMPT,

            input=[
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
                            "detail": "high",
                        },
                    ],
                }
            ],
        )

        return response.output_text

    except Exception as e:

        logger.exception(
            "OpenAI image request failed"
        )

        return (
            "Не получилось проанализировать "
            "фотографию 😔\n\n"
            f"Техническая ошибка: {str(e)}"
        )


# ============================================================
# ОБЫЧНЫЙ AI БЕЗ ФОТО
# ============================================================

async def ask_ai(
    user_text: str,
    mode: str | None = None,
) -> str:

    if not openai_client:

        return (
            "⚠️ OpenAI пока не подключён.\n\n"
            "Проверь переменную OPENAI_API_KEY "
            "в Render."
        )

    mode_instruction = ""

    if mode == "add":

        mode_instruction = """
Пользователь добавляет товар.
Помоги структурировать информацию.
"""

    elif mode == "post":

        mode_instruction = """
Пользователь хочет готовый продающий пост
для Telegram.
"""

    elif mode == "stories":

        mode_instruction = """
Пользователь хочет серию сторис.
"""

    elif mode == "reels":

        mode_instruction = """
Пользователь хочет идеи Reels/TikTok.
"""

    elif mode == "plan":

        mode_instruction = """
Пользователь хочет контент-план на неделю.
"""

    prompt = f"""
{mode_instruction}

Запрос владельца:

{user_text}
"""

    try:

        response = await openai_client.responses.create(

            model=TEXT_MODEL,

            instructions=SYSTEM_PROMPT,

            input=prompt,
        )

        return response.output_text

    except Exception as e:

        logger.exception(
            "OpenAI text request failed"
        )

        return (
            "Не получилось получить ответ от AI 😔\n\n"
            f"Техническая ошибка: {str(e)}"
        )


# ============================================================
# ОБРАБОТКА ФОТО GPT-IMAGE
# ============================================================

async def edit_product_photo(
    image_bytes: bytes,
    instruction: str,
) -> bytes | None:

    if not openai_client:

        return None

    try:

        image_file = io.BytesIO(image_bytes)

        image_file.name = "product.jpg"

        result = await openai_client.images.edit(

            model=IMAGE_MODEL,

            image=image_file,

            prompt=instruction,

            size="1024x1024",
        )

        if not result.data:

            return None

        image_data = result.data[0]

        if (
            hasattr(image_data, "b64_json")
            and image_data.b64_json
        ):

            return base64.b64decode(
                image_data.b64_json
            )

        return None

    except Exception:

        logger.exception(
            "OpenAI image edit failed"
        )

        return None


# ============================================================
# КНОПКИ
# ============================================================

async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    # --------------------------------------------------------
    # Обычная работа с фото
    # --------------------------------------------------------

    if query.data == "photo":

        context.user_data["waiting_for_photo"] = True

        await query.message.reply_text(
            "📸 Отлично!\n\n"
            "Пришли фотографию товара.\n\n"
            "Я сначала посмотрю, что на ней "
            "написано и что за товар, а затем "
            "ты выберешь, что с ним сделать."
        )

        return

    # --------------------------------------------------------
    # Улучшить фото
    # --------------------------------------------------------

    if query.data == "improve_photo":

        context.user_data["photo_action"] = "improve"

        await query.message.reply_text(
            "✨ Хорошо!\n\n"
            "Пришли фотографию товара, "
            "которую нужно улучшить."
        )

        return

    # --------------------------------------------------------
    # Рекламное фото
    # --------------------------------------------------------

    if query.data == "ad_photo":

        context.user_data["photo_action"] = "ad"

        await query.message.reply_text(
            "🎨 Отлично!\n\n"
            "Пришли фотографию товара.\n\n"
            "Я сделаю из неё аккуратную "
            "рекламную композицию."
        )

        return

    # --------------------------------------------------------
    # Пост по уже загруженному фото
    # --------------------------------------------------------

    if query.data == "photo_post":

        image_bytes = context.user_data.get(
            "product_image"
        )

        if not image_bytes:

            await query.message.reply_text(
                "Сначала пришли фотографию товара 📸"
            )

            return

        context.user_data["photo_action"] = (
            "post"
        )

        await query.message.reply_text(
            "✍️ Хорошо!\n\n"
            "Напиши цену и, если хочешь, "
            "количество товара.\n\n"
            "Остальную информацию я попробую "
            "взять непосредственно с фотографии."
        )

        return

    # --------------------------------------------------------
    # Сторис по фото
    # --------------------------------------------------------

    if query.data == "photo_stories":

        image_bytes = context.user_data.get(
            "product_image"
        )

        if not image_bytes:

            await query.message.reply_text(
                "Сначала пришли фотографию товара 📸"
            )

            return

        context.user_data["photo_action"] = (
            "stories"
        )

        await query.message.reply_text(
            "📱 Хорошо!\n\n"
            "Если есть цена или важная "
            "информация — напиши её.\n\n"
            "Остальное возьму с фотографии."
        )

        return

    # --------------------------------------------------------
    # Reels
    # --------------------------------------------------------

    if query.data == "photo_reels":

        image_bytes = context.user_data.get(
            "product_image"
        )

        if not image_bytes:

            await query.message.reply_text(
                "Сначала пришли фотографию товара 📸"
            )

            return

        context.user_data["photo_action"] = (
            "reels"
        )

        await query.message.reply_text(
            "🎥 Хорошо!\n\n"
            "Напиши цену, если её нужно "
            "использовать в идее."
        )

        return

    # --------------------------------------------------------
    # Остальные режимы
    # --------------------------------------------------------

    modes = {

        "add": (
            "add",
            "📦 Пришли фотографию товара "
            "или название, цену и количество."
        ),

        "post": (
            "post",
            "✍️ Напиши, о каком товаре "
            "или теме сделать пост."
        ),

        "stories": (
            "stories",
            "📱 Напиши, о каком товаре "
            "или теме сделать сторис."
        ),

        "reels": (
            "reels",
            "🎥 Напиши товар или тему — "
            "придумаю идеи Reels/TikTok."
        ),

        "plan": (
            "plan",
            "📅 Пришли список товаров "
            "или напиши, что хочешь "
            "продвигать на этой неделе."
        ),
    }

    if query.data in modes:

        mode, message = modes[
            query.data
        ]

        context.user_data["mode"] = mode

        await query.message.reply_text(
            message
        )

        return

    await query.message.reply_text(
        "Готово 💗",
        reply_markup=main_keyboard(),
    )


# ============================================================
# ФОТОГРАФИЯ ОТ ПОЛЬЗОВАТЕЛЯ
# ============================================================

async def photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    telegram_photo = (
        update.message.photo[-1]
    )

    file = await telegram_photo.get_file()

    image_bytes = bytes(
        await file.download_as_bytearray()
    )

    # Сохраняем фотографию в текущем диалоге
    context.user_data[
        "product_image"
    ] = image_bytes

    caption = (
        update.message.caption or ""
    )

    # --------------------------------------------------------
    # Если пользователь выбрал обработку фото
    # --------------------------------------------------------

    photo_action = (
        context.user_data.get(
            "photo_action"
        )
    )

    if photo_action in [
        "improve",
        "ad",
    ]:

        await update.message.chat.send_action(
            "upload_photo"
        )

        if photo_action == "improve":

            instruction = """
Улучши эту фотографию товара для
Telegram-магазина косметики и парфюмерии.

ОБЯЗАТЕЛЬНО СОХРАНИ:
— настоящий товар;
— бренд;
— название;
— логотип;
— форму упаковки;
— цвет упаковки;
— пропорции товара;
— существующие надписи.

Не заменяй товар другим товаром.

Не придумывай новый логотип.
Не добавляй ложный текст.
Не добавляй цену.
Не добавляй рекламные заявления.

Улучши:
— освещение;
— резкость;
— фон;
— композицию;
— визуальную чистоту.

Сделай фотографию похожей на
профессиональную предметную съёмку
для премиального магазина косметики.
"""

        else:

            instruction = """
Создай красивую рекламную композицию
на основе этой фотографии товара.

Главный объект — именно тот товар,
который находится на исходной фотографии.

Сохрани максимально точно:
— бренд;
— упаковку;
— форму;
— цвет;
— логотип;
— название;
— пропорции.

Можно создать красивый премиальный
фон и профессиональное освещение.

Стиль:
luxury beauty,
premium cosmetics,
clean editorial product photography.

НЕ заменяй товар.
НЕ придумывай другой продукт.
НЕ добавляй фальшивый текст.
НЕ добавляй цену.
НЕ добавляй ложные характеристики.
"""

        result = await edit_product_photo(
            image_bytes,
            instruction,
        )

        context.user_data[
            "photo_action"
        ] = None

        if result:

            await update.message.reply_photo(

                photo=io.BytesIO(result),

                caption=(
                    "✨ Готово!\n\n"
                    "Вот обработанный вариант.\n"
                    "Если нравится — можем "
                    "использовать его для поста."
                ),

                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✍️ Сделать пост",
                                callback_data="photo_post",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "📱 Сделать сторис",
                                callback_data="photo_stories",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🎨 Ещё вариант",
                                callback_data="ad_photo",
                            )
                        ],
                    ]
                ),
            )

        else:

            await update.message.reply_text(
                "Не получилось обработать фото 😔\n\n"
                "Проверь логи Render — там будет "
                "точная причина."
            )

        return

    # --------------------------------------------------------
    # Если фото отправлено просто так
    # --------------------------------------------------------

    if caption:

        user_text = (
            "Проанализируй фотографию товара.\n\n"
            "Дополнительная информация от владельца:\n"
            + caption
            + "\n\n"
            "Сначала кратко напиши, что ты "
            "распознал на фотографии."
        )

    else:

        user_text = """
Проанализируй эту фотографию товара.

Определи только то, что реально можно
прочитать или увидеть на изображении.

Ответь в формате:

🔎 Я вижу:

Бренд: ...
Название: ...
Тип товара: ...
Объём: ...
Видимая информация: ...

Если что-то невозможно уверенно прочитать,
напиши «не удалось определить».

Ничего не выдумывай.
"""

    await update.message.chat.send_action(
        "typing"
    )

    analysis = await ask_ai_with_image(
        image_bytes,
        user_text,
    )

    await update.message.reply_text(
        "📸 Фото получила и действительно "
        "передала его AI для анализа.\n\n"
        + analysis
        + "\n\n"
        "Что сделать с этим товаром?",
        reply_markup=photo_keyboard(),
    )


# ============================================================
# ТЕКСТ ПОСЛЕ ФОТО
# ============================================================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = update.message.text or ""

    photo_action = (
        context.user_data.get(
            "photo_action"
        )
    )

    product_image = (
        context.user_data.get(
            "product_image"
        )
    )

    # --------------------------------------------------------
    # Пост / сторис / reels после фотографии
    # --------------------------------------------------------

    if (
        photo_action
        in ["post", "stories", "reels"]
        and product_image
    ):

        if photo_action == "post":

            task = """
Используя фотографию товара,
создай готовый продающий пост
для Telegram-магазина.

Обязательно используй информацию,
которую можно прочитать на фотографии.

Дополнительная информация владельца:

{user_text}

Не выдумывай неизвестные характеристики.
Цена, указанная владельцем, является
актуальной ценой товара.
"""

        elif photo_action == "stories":

            task = """
Используя фотографию товара,
создай серию коротких сторис
для Telegram.

Дополнительная информация владельца:

{user_text}

Не выдумывай характеристики.
"""

        else:

            task = """
Используя фотографию товара,
придумай несколько идей Reels/TikTok.

Учитывай именно этот товар.

Дополнительная информация владельца:

{user_text}

Не выдумывай характеристики.
"""

        prompt = task.format(
            user_text=text
        )

        answer = await ask_ai_with_image(
            product_image,
            prompt,
        )

        context.user_data[
            "photo_action"
        ] = None

        await update.message.reply_text(
            answer,
            reply_markup=main_keyboard(),
        )

        return

    # --------------------------------------------------------
    # Обычный текстовый режим
    # --------------------------------------------------------

    mode = context.user_data.get(
        "mode"
    )

    await update.message.chat.send_action(
        "typing"
    )

    answer = await ask_ai(
        text,
        mode,
    )

    context.user_data["mode"] = None

    await update.message.reply_text(
        answer
    )


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
        CallbackQueryHandler(
            button
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_message,
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


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():

    await setup()

    await telegram_app.updater.start_polling()


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown():

    if telegram_app:

        await telegram_app.updater.stop()

        await telegram_app.stop()

        await telegram_app.shutdown()


# ============================================================
# HEALTH
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
    }
