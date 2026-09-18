import os
import json
import base64
import logging
from typing import Optional

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

# =========================================================
# SETTINGS
# =========================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

MODEL = "gpt-5.6-luna"

app = FastAPI()

telegram_app = (
    Application.builder().token(TOKEN).build()
    if TOKEN
    else None
)

client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# =========================================================
# BRAND / CONTENT STRATEGY
# =========================================================

SYSTEM_PROMPT = """
Ты — Beauty Manager, AI-помощник Telegram-канала
«Косметика | Парфюм | Москва».

Твоя задача — помогать владельцу канала развивать его,
создавать контент и продавать оригинальную косметику,
парфюмерию и уходовые средства.

ВАЖНЫЕ ПРАВИЛА:

1. Канал НЕ должен выглядеть как каталог товаров.

2. Основная стратегия контента:
   - 70% — интересный и полезный контент;
   - 20% — вовлечение и ощущение живого автора;
   - 10% — прямые продажи.

3. Не нужно продавать в каждом посте.

4. Стиль:
   - живой;
   - красивый;
   - современный;
   - дружелюбный;
   - женственный;
   - без канцелярита;
   - без чрезмерного количества эмодзи.

5. Не выдумывай личный опыт Ани.
   Если она не сообщила, что сама пользовалась товаром,
   нельзя писать «я пользуюсь», «мой любимый» и т.п.

6. Можно использовать ощущение личного блога:
   «я бы обратила внимание»,
   «давайте выберем вместе»,
   «мне кажется, этот вариант...»
   — но только если это не выдаёт выдуманный опыт за факт.

7. Продажи должны быть мягкими.
   Не использовать:
   «КУПИТЕ СРОЧНО!!!»
   «УСПЕЙТЕ!!!»
   без реальной причины.

8. Если есть ограниченное количество товара,
   можно честно использовать дефицит:
   «осталось 2 шт.»

9. Цена и количество, которые сообщает Аня,
   всегда важнее найденных в интернете цен и остатков.

10. При поиске информации о товаре:
   - сначала официальный сайт бренда;
   - затем крупные/надёжные магазины;
   - проверяй название, оттенок, объём и назначение;
   - не придумывай характеристики.

11. Если информация противоречива,
   укажи неопределённость, а не выдумывай ответ.

12. Посты должны быть достаточно короткими для Telegram.

ПОСТОЯННЫЕ РУБРИКИ:

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

ИДЕИ КОНТЕНТА:

- 5 ароматов, которые пахнут дороже своей цены
- Что подарить девушке, если вообще не разбираешься в косметике
- 3 крема для тех, кто ненавидит липкость
- Как выбрать парфюм в подарок
- Что купить до 5 000 ₽
- Парфюм на разные типы свиданий
- 5 средств, которые красиво смотрятся на туалетном столике
- Выбираем вместе
- Что бы выбрала ты?
- Угадайте цену
- Сегодня приехало
- До/после
- Распаковка
- Сборка заказа
- Отзывы клиентов

ПРИМЕР НЕДЕЛЬНОЙ СТРУКТУРЫ:

Понедельник:
полезный пост + товар

Вторник:
подборка + опрос

Среда:
новинка + короткая история

Четверг:
разбор продукта

Пятница:
вовлечение + продажный пост

Суббота:
lifestyle / эстетика + подборка

Воскресенье:
итоги недели + тизер новинок

Не копируй эту структуру механически.
Подстраивай её под товары и ситуацию.
"""


# =========================================================
# USER DATA
# =========================================================

# Пока храним данные в памяти.
# Позже можно подключить SQLite/PostgreSQL,
# чтобы товары не исчезали после перезапуска Render.

USERS = {}


def user_data(user_id: int):
    if user_id not in USERS:
        USERS[user_id] = {
            "products": [],
            "draft": None,
            "last_photo": None,
            "last_product": None,
        }

    return USERS[user_id]


# =========================================================
# MAIN MENU
# =========================================================

def main_menu():

    keyboard = [
        [
            InlineKeyboardButton("📦 Новый товар", callback_data="new_product"),
            InlineKeyboardButton("📋 Мои товары", callback_data="products"),
        ],
        [
            InlineKeyboardButton("✍️ Создать пост", callback_data="post"),
            InlineKeyboardButton("📱 Сторис", callback_data="stories"),
        ],
        [
            InlineKeyboardButton("🎥 Reels / TikTok", callback_data="reels"),
            InlineKeyboardButton("🗳️ Создать опрос", callback_data="poll"),
        ],
        [
            InlineKeyboardButton("☀️ План на сегодня", callback_data="today"),
            InlineKeyboardButton("🗓️ План на неделю", callback_data="week"),
        ],
        [
            InlineKeyboardButton("💬 Вовлечение", callback_data="engagement"),
            InlineKeyboardButton("💡 Идеи рубрик", callback_data="rubrics"),
        ],
        [
            InlineKeyboardButton("🚀 Идеи для роста", callback_data="growth"),
            InlineKeyboardButton("🔥 Что публиковать", callback_data="now"),
        ],
        [
            InlineKeyboardButton("📸 Работа с фото", callback_data="photo"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


WELCOME = """
Привет, Ани! 💗

Я — твой Beauty Manager.

Теперь я могу не только делать карточки товаров, но и помогать тебе развивать весь канал:

📦 товары
✍️ посты
📱 сторис
🎥 Reels / TikTok
🗳️ опросы
☀️ план на сегодня
🗓️ план на неделю
💬 вовлечение
🚀 идеи для роста

И главное — я ничего не публикую без твоего одобрения.

Давай работать вместе 🫶🏻
"""


# =========================================================
# OPENAI
# =========================================================

async def ask_ai(
    prompt: str,
    use_web: bool = False,
    image_data: Optional[str] = None,
):

    if not client:
        return "OPENAI_API_KEY не настроен."

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

        logger.exception("OpenAI error")

        return (
            "Не получилось обратиться к AI.\n\n"
            f"Ошибка: {e}"
        )


# =========================================================
# TELEGRAM IMAGE → BASE64
# =========================================================

async def telegram_photo_to_data_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    photo = update.message.photo[-1]

    telegram_file = await context.bot.get_file(photo.file_id)

    data = await telegram_file.download_as_bytearray()

    encoded = base64.b64encode(data).decode("utf-8")

    return f"data:image/jpeg;base64,{encoded}"


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
        "Просто выбери нужный раздел в меню 💗",
        reply_markup=main_menu(),
    )


# =========================================================
# NEW PRODUCT
# =========================================================

async def start_new_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    data = user_data(update.effective_user.id)

    data["mode"] = "new_product"

    await update.callback_query.message.reply_text(
        """
📦 <b>Добавляем новый товар</b>

Пришли мне:

📸 фото товара
💰 цену
📦 количество

Например:

3500 ₽
В наличии 2 шт.

Можно отправить всё одним сообщением вместе с фотографией.
""",
        parse_mode="HTML",
    )


# =========================================================
# PRODUCT PHOTO
# =========================================================

async def product_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    uid = update.effective_user.id
    data = user_data(uid)

    image_data = await telegram_photo_to_data_url(
        update,
        context,
    )

    caption = update.message.caption or ""

    data["last_photo"] = image_data
    data["product_caption"] = caption

    if data.get("mode") != "new_product":

        await update.message.reply_text(
            "Фото получила 📸\n\n"
            "Если хочешь обработать его как товар, нажми «📦 Новый товар».",
            reply_markup=main_menu(),
        )

        return

    await update.message.reply_text(
        "🔎 Рассматриваю товар и ищу информацию о нём...\n\n"
        "Это может занять немного времени."
    )

    prompt = f"""
Проанализируй фотографию товара.

Дополнительная информация от владельца:
{caption or "не указана"}

Определи максимально точно:
1. бренд;
2. название;
3. продукт;
4. оттенок, если есть;
5. объём, если виден или подтверждается;
6. назначение.

Затем через интернет найди актуальную информацию.

Используй официальные источники бренда в первую очередь.

Верни готовую карточку товара.

ВАЖНО:
Цена и количество будут указаны владельцем отдельно.
Не заменяй их найденной в интернете ценой.

Формат:

✨ НАЗВАНИЕ

Короткое описание 2–4 предложения.

🤍 Объём / оттенок:
...

💰 Цена:
...

📦 В наличии:
...

Источники:
...
"""

    result = await ask_ai(
        prompt,
        use_web=True,
        image_data=image_data,
    )

    data["last_product"] = result

    keyboard = [
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

    await update.message.reply_text(
        result,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# PRODUCT APPROVAL
# =========================================================

async def approve_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    uid = update.effective_user.id
    data = user_data(uid)

    product = data.get("last_product")

    if product:
        data["products"].append(product)

    await query.message.reply_text(
        """
✅ Товар сохранён!

Теперь его можно использовать для:

✍️ постов
📱 сторис
🎥 Reels / TikTok
🗳️ опросов
🗓️ контент-плана

Что делаем дальше?
""",
        reply_markup=main_menu(),
    )


async def edit_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    user_data(update.effective_user.id)["mode"] = "edit_product"

    await query.message.reply_text(
        """
✏️ Напиши, что именно изменить.

Например:

«Сделай описание короче»

«Добавь больше информации про оттенок»

«Убери источники»

«Сделай текст более продающим»
"""
    )


async def another_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    user_data(update.effective_user.id)["mode"] = "new_product"

    await query.message.reply_text(
        "🖼️ Хорошо. Пришли другое фото товара."
    )


# =========================================================
# POSTS
# =========================================================

async def create_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    data = user_data(update.effective_user.id)

    products = "\n\n".join(
        data["products"][-5:]
    )

    prompt = f"""
Создай готовый Telegram-пост для канала.

Товары, которые сейчас есть:
{products or "Товары ещё не добавлены."}

Сделай пост живым и полезным.

Не обязательно продавать товар.
Можно сделать:
- полезный совет;
- подборку;
- мини-разбор;
- вовлекающий пост;
- мягкую продажу.

Не пиши длинно.
"""

    result = await ask_ai(prompt)

    data["draft"] = result

    await query.message.reply_text(
        result,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Одобрить",
                    callback_data="approve_draft",
                ),
                InlineKeyboardButton(
                    "✏️ Изменить",
                    callback_data="edit_draft",
                ),
            ],
        ]),
    )


# =========================================================
# STORIES
# =========================================================

async def create_stories(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    data = user_data(update.effective_user.id)

    products = "\n".join(
        data["products"][-5:]
    )

    prompt = f"""
Создай серию из 4 Telegram Stories / коротких сообщений
для канала косметики и парфюмерии.

Текущие товары:
{products or "товаров пока нет"}

Структура:

1. зацепка
2. полезная информация
3. вовлечение
4. мягкий переход к товару или действию

Можно использовать опрос или вопрос.
"""

    result = await ask_ai(prompt)

    data["draft"] = result

    await query.message.reply_text(
        result,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Одобрить",
                    callback_data="approve_draft",
                ),
                InlineKeyboardButton(
                    "✏️ Изменить",
                    callback_data="edit_draft",
                ),
            ],
        ]),
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
Придумай 5 идей Reels/TikTok для Telegram-канала
косметики и парфюмерии.

Условия:

- не показывать лицо владельца;
- идеи должны легко сниматься на телефон;
- эстетично;
- с потенциалом пересылок;
- без сложного монтажа.

Для каждой идеи:
1. хук первых 2 секунд;
2. что снять;
3. текст на экране;
4. подпись;
5. CTA.
"""

    result = await ask_ai(prompt)

    await query.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# DAILY PLAN
# =========================================================

async def daily_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    data = user_data(update.effective_user.id)

    products = "\n".join(
        data["products"][-10:]
    )

    prompt = f"""
Составь контент-план НА ОДИН ДЕНЬ
для канала «Косметика | Парфюм | Москва».

Товары:
{products or "товары пока не добавлены"}

Соблюдай соотношение:
70% полезного
20% вовлечения
10% продаж.

Предложи:

09:00–11:00
13:00–15:00
17:00–19:00
20:00–22:00

Для каждого времени:
- что публиковать;
- формат;
- готовая идея;
- пример текста;
- нужен ли товар;
- нужен ли опрос.

Не заставляй публиковать 4 продажи.
День должен ощущаться как живой бьюти-канал.
"""

    result = await ask_ai(prompt)

    data["draft"] = result

    await query.message.reply_text(
        result,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Одобрить план",
                    callback_data="approve_draft",
                ),
                InlineKeyboardButton(
                    "✏️ Изменить",
                    callback_data="edit_draft",
                ),
            ],
        ]),
    )


# =========================================================
# WEEK PLAN
# =========================================================

async def weekly_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    data = user_data(update.effective_user.id)

    products = "\n".join(
        data["products"][-15:]
    )

    prompt = f"""
Составь полноценный контент-план на 7 дней
для Telegram-канала «Косметика | Парфюм | Москва».

Товары:
{products or "товары пока не добавлены"}

Главная стратегия:
70% полезное
20% вовлечение
10% продажи.

На каждый день дай:

- основную тему;
- 1 основной пост;
- 2–4 коротких сообщения / Stories;
- идею опроса или вовлечения;
- товарную интеграцию, если уместно;
- идею для Reels/TikTok.

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

Не превращай каждый день в продажу.
"""

    result = await ask_ai(prompt)

    data["draft"] = result

    await query.message.reply_text(
        result,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Одобрить",
                    callback_data="approve_draft",
                ),
                InlineKeyboardButton(
                    "✏️ Изменить",
                    callback_data="edit_draft",
                ),
            ],
        ]),
    )


# =========================================================
# POLL IDEA
# =========================================================

async def poll_creator(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    prompt = """
Придумай один интересный Telegram-опрос
для аудитории канала косметики и парфюмерии.

Он должен быть лёгким и реально стимулировать людей нажать кнопку.

Верни СТРОГО JSON:

{
  "question": "...",
  "options": [
    "...",
    "...",
    "...",
    "..."
  ]
}

Максимум 4 варианта.
"""

    result = await ask_ai(prompt)

    try:

        cleaned = result.strip()

        if "```" in cleaned:
            cleaned = cleaned.replace("```json", "")
            cleaned = cleaned.replace("```", "")

        poll = json.loads(cleaned)

        question = poll["question"]
        options = poll["options"][:4]

        user_data(update.effective_user.id)["pending_poll"] = {
            "question": question,
            "options": options,
        }

        await query.message.reply_text(
            f"""
🗳️ <b>Опрос готов</b>

{question}

""" + "\n".join(
                f"• {x}" for x in options
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Одобрить и создать",
                        callback_data="approve_poll",
                    ),
                    InlineKeyboardButton(
                        "✏️ Другой",
                        callback_data="poll",
                    ),
                ]
            ]),
        )

    except Exception:

        await query.message.reply_text(
            "Не удалось оформить опрос автоматически. Попробуй ещё раз."
        )


async def approve_poll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    data = user_data(update.effective_user.id)

    poll = data.get("pending_poll")

    if not poll:

        await query.message.reply_text(
            "Опрос уже не найден. Создай новый."
        )

        return

    await context.bot.send_poll(
        chat_id=update.effective_chat.id,
        question=poll["question"],
        options=poll["options"],
        is_anonymous=True,
    )

    await query.message.reply_text(
        "✅ Опрос создан."
    )


# =========================================================
# ENGAGEMENT
# =========================================================

async def engagement(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    prompt = """
Придумай 10 идей для вовлечения аудитории
Telegram-канала косметики и парфюмерии.

Форматы:

- «Что бы выбрала ты?»
- «Угадайте цену»
- «Выбираем вместе»
- мини-тест
- вопрос
- реакция
- сравнение двух товаров

Идеи должны быть простыми для реализации.
"""

    result = await ask_ai(prompt)

    await query.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# RUBRICS
# =========================================================

async def rubrics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    prompt = """
Придумай 15 постоянных рубрик
для Telegram-канала косметики и парфюмерии.

Для каждой:
- название;
- о чём;
- как часто использовать;
- пример первой публикации.

Не повторяй банальные рубрики.
"""

    result = await ask_ai(prompt)

    await query.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# GROWTH
# =========================================================

async def growth(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    prompt = """
Придумай практический план роста Telegram-канала
«Косметика | Парфюм | Москва».

Учитывай:

- TikTok;
- Instagram Reels/Stories;
- Telegram;
- взаимные рекомендации;
- небольшие рекламные размещения;
- эксклюзивный контент для Telegram;
- отзывы;
- распаковки;
- подборки.

Главный приоритет:
не просто количество подписчиков,
а активная аудитория.

Дай 15 конкретных действий.
"""

    result = await ask_ai(prompt)

    await query.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# WHAT TO POST NOW
# =========================================================

async def what_to_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    data = user_data(update.effective_user.id)

    products = "\n".join(
        data["products"][-10:]
    )

    prompt = f"""
Представь, что Аня прямо сейчас открыла Telegram
и спрашивает:

«Что мне сегодня опубликовать?»

Товары:
{products or "товаров пока нет"}

Предложи:

1. что опубликовать сейчас;
2. что через несколько часов;
3. что вечером.

Учитывай стратегию:
70% полезного
20% вовлечения
10% продаж.

Не предлагай продажу просто ради продажи.
"""

    result = await ask_ai(prompt)

    await query.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


# =========================================================
# MY PRODUCTS
# =========================================================

async def show_products(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    data = user_data(update.effective_user.id)

    if not data["products"]:

        await query.message.reply_text(
            "📋 Пока товаров нет.\n\n"
            "Нажми «📦 Новый товар» и добавь первый.",
            reply_markup=main_menu(),
        )

        return

    text = "📋 <b>Твои товары:</b>\n\n"

    for i, product in enumerate(
        data["products"],
        1,
    ):

        short = product[:500]

        text += f"<b>{i}.</b>\n{short}\n\n"

    await query.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# =========================================================
# PHOTO SECTION
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

Здесь можно будет:

• улучшить качество;
• сделать фон чище;
• подготовить фото для карточки;
• сделать визуал для поста;
• подготовить идею для рекламного изображения.

Пришли фото и напиши, что нужно изменить.
""",
        parse_mode="HTML",
    )


# =========================================================
# TEXT MESSAGE
# =========================================================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    uid = update.effective_user.id
    data = user_data(uid)

    text = update.message.text or ""

    mode = data.get("mode")

    # -----------------------------------------------------
    # EDIT PRODUCT
    # -----------------------------------------------------

    if mode == "edit_product":

        old = data.get("last_product", "")

        prompt = f"""
Вот текущая карточка товара:

{old}

Владелец просит изменить её:

{text}

Перепиши карточку с учётом просьбы.
"""

        result = await ask_ai(prompt)

        data["last_product"] = result
        data["mode"] = None

        await update.message.reply_text(
            result,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Одобрить",
                        callback_data="approve_product",
                    ),
                    InlineKeyboardButton(
                        "✏️ Изменить",
                        callback_data="edit_product",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔄 Новый товар",
                        callback_data="new_product",
                    ),
                ],
            ]),
        )

        return

    # -----------------------------------------------------
    # GENERAL AI CHAT
    # -----------------------------------------------------

    result = await ask_ai(
        f"""
Аня написала:

{text}

Ответь как её помощник по развитию
Telegram-канала косметики и парфюмерии.

Дай конкретный и полезный ответ.
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
        await start_new_product(update, context)

    elif action == "products":
        await show_products(update, context)

    elif action == "post":
        await create_post(update, context)

    elif action == "stories":
        await create_stories(update, context)

    elif action == "reels":
        await create_reels(update, context)

    elif action == "today":
        await daily_plan(update, context)

    elif action == "week":
        await weekly_plan(update, context)

    elif action == "poll":
        await poll_creator(update, context)

    elif action == "approve_poll":
        await approve_poll(update, context)

    elif action == "engagement":
        await engagement(update, context)

    elif action == "rubrics":
        await rubrics(update, context)

    elif action == "growth":
        await growth(update, context)

    elif action == "now":
        await what_to_post(update, context)

    elif action == "photo":
        await photo_section(update, context)

    elif action == "approve_product":
        await approve_product(update, context)

    elif action == "edit_product":
        await edit_product(update, context)

    elif action == "another_photo":
        await another_photo(update, context)

    elif action == "approve_draft":

        await query.message.reply_text(
            """
✅ Отлично!

Черновик одобрен.

Пока он остаётся в боте и не публикуется автоматически.

Следующим этапом подключим публикацию
в твой Telegram-канал после подтверждения.
""",
            reply_markup=main_menu(),
        )

    elif action == "edit_draft":

        user_data(
            update.effective_user.id
        )["mode"] = "edit_draft"

        await query.message.reply_text(
            "✏️ Напиши, что изменить в черновике."
        )


# =========================================================
# COMMAND MENU
# =========================================================

async def setup_commands():

    commands = [
        BotCommand("start", "Главное меню"),
        BotCommand("new", "Новый товар"),
        BotCommand("post", "Создать пост"),
        BotCommand("stories", "Создать сторис"),
        BotCommand("poll", "Создать опрос"),
        BotCommand("today", "План на сегодня"),
        BotCommand("week", "План на неделю"),
        BotCommand("growth", "Идеи для роста"),
    ]

    await telegram_app.bot.set_my_commands(commands)

    try:

        await telegram_app.bot.set_chat_menu_button(
            menu_button=MenuButtonCommands()
        )

    except Exception as e:

        logger.warning(
            "Не удалось установить menu button: %s",
            e,
        )


# =========================================================
# COMMANDS
# =========================================================

async def new_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_data(
        update.effective_user.id
    )["mode"] = "new_product"

    await update.message.reply_text(
        "📦 Пришли фото товара + цену + количество."
    )


async def post_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await create_post_from_command(
        update,
        context,
    )


async def create_post_from_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    prompt = """
Создай интересный Telegram-пост
для канала косметики и парфюмерии.

Не делай его прямой рекламой.
Можно использовать полезный совет,
мини-подборку или вовлекающий вопрос.
"""

    result = await ask_ai(prompt)

    await update.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


async def today_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    # Вызываем ту же логику через fake callback
    await update.message.reply_text(
        "☀️ Открываю план на сегодня...",
    )

    prompt = """
Составь план публикаций на сегодня
для канала косметики и парфюмерии.

70% полезное
20% вовлечение
10% продажи.

Дай конкретные темы и примерные часы.
"""

    result = await ask_ai(prompt)

    await update.message.reply_text(
        result,
        reply_markup=main_menu(),
    )


async def week_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    prompt = """
Составь контент-план Telegram-канала
косметики и парфюмерии на 7 дней.

Для каждого дня:
основной пост,
короткие сообщения,
вовлечение,
продажа только там, где уместно.

Используй стратегию 70/20/10.
"""

    result = await ask_ai(prompt)

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
# STARTUP / SHUTDOWN
# =========================================================

async def setup():

    if not telegram_app:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set"
        )

    telegram_app.add_handler(
        CommandHandler("start", start)
    )

    telegram_app.add_handler(
        CommandHandler("help", help_cmd)
    )

    telegram_app.add_handler(
        CommandHandler("new", new_command)
    )

    telegram_app.add_handler(
        CommandHandler("post", post_command)
    )

    telegram_app.add_handler(
        CommandHandler("today", today_command)
    )

    telegram_app.add_handler(
        CommandHandler("week", week_command)
    )

    telegram_app.add_handler(
        CallbackQueryHandler(button)
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.PHOTO,
            product_photo,
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message,
        )
    )

    await telegram_app.initialize()
    await telegram_app.start()

    await setup_commands()


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
