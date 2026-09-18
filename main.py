import os, re, json, asyncio, sqlite3, logging, base64, mimetypes
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from openai import AsyncOpenAI
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto, MenuButtonCommands
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
DB_PATH = os.getenv("DB_PATH", "beauty_manager.db")
DEFAULT_TZ = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow")

client = AsyncOpenAI(api_key=OPENAI_KEY)
app = FastAPI()
tg_app = Application.builder().token(TOKEN).build()
scheduler_task = None

MAIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("➕ Новый товар"), KeyboardButton("📝 Новый пост")],
    [KeyboardButton("📅 План"), KeyboardButton("⏰ Расписание")],
    [KeyboardButton("📋 Очередь"), KeyboardButton("📢 Канал")],
    [KeyboardButton("📊 Идеи")]
], resize_keyboard=True)

# ---------------- DATABASE ----------------

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        channel_id TEXT,
        timezone TEXT DEFAULT 'Europe/Moscow',
        paused INTEGER DEFAULT 0,
        days TEXT DEFAULT '[0,1,2,3,4,5,6]',
        times TEXT DEFAULT '["10:00","15:00","20:00"]'
    );
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT,
        category TEXT,
        item_type TEXT,
        price TEXT,
        stock TEXT,
        volume TEXT,
        shade TEXT,
        description TEXT,
        photo1 TEXT,
        photo2 TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS queue(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        scheduled_at TEXT NOT NULL,
        caption TEXT,
        photo1 TEXT,
        photo2 TEXT,
        created_at TEXT NOT NULL
    );
    """)
    con.commit(); con.close()


def ensure_user(uid):
    con = db(); con.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
    con.commit(); con.close()


def get_user(uid):
    ensure_user(uid)
    con = db(); row = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone(); con.close()
    return row


def update_user(uid, **fields):
    ensure_user(uid)
    if not fields: return
    con = db()
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*fields.values(), uid))
    con.commit(); con.close()

# ---------------- HELPERS ----------------

def clean_text(text):
    if not text: return ""
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\[[^\]]+\]\([^)]*\)', '', text)
    text = re.sub(r'(?im)^\s*(источники?|sources?)\s*:.*$', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_price_stock(text):
    price = None; stock = None
    m = re.search(r'(?<!\d)(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб(?:\.|лей)?)?', text.lower())
    if m: price = re.sub(r'\s+', '', m.group(1)).replace(',', '.')
    m = re.search(r'(?:в\s*наличии|наличие|остаток)\s*[:\-]?\s*(\d+)\s*(?:шт|штук)?', text.lower())
    if m: stock = m.group(1)
    else:
        m = re.search(r'(\d+)\s*шт', text.lower())
        if m: stock = m.group(1)
    return price, stock


def format_dt(iso, uid):
    tz = ZoneInfo(get_user(uid)["timezone"] or DEFAULT_TZ)
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz).strftime("%d.%m в %H:%M")


def category_hashtag(value):
    value = (value or "#косметика").strip().lower().replace(" ", "_")
    if not value.startswith("#"): value = "#" + value
    return value

# ---------------- OPENAI ----------------

async def ai_json(prompt):
    response = await client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search", "search_context_size": "high"}],
        input=prompt
    )
    raw = response.output_text.strip()
    raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    return json.loads(raw)


async def analyze_product(name_hint, price, stock, item_type_hint, image_paths=None):
    image_paths = image_paths or []
    prompt = f"""
Ты — контент-менеджер Telegram-магазина оригинальной косметики и парфюмерии.
Определи товар по данным пользователя и изображениям и обязательно проверь актуальную информацию через веб-поиск.
Приоритет: официальный сайт бренда → официальный/крупный ритейлер → другие надёжные источники.
Не выдумывай характеристики.
Цена и остаток пользователя всегда важнее найденных в интернете.

Название/подсказка пользователя: {name_hint or 'нет'}
Цена пользователя: {price or 'нет'}
Количество пользователя: {stock or 'нет'}
Явно указанный тип: {item_type_hint or 'нет'}

Верни ТОЛЬКО валидный JSON без markdown:
{{
  "name": "точное название товара",
  "category_hashtag": "один хэштег",
  "item_type": "single" или "set",
  "description": "2–4 коротких предложения на русском",
  "volume": "объём или пусто",
  "shade": "оттенок или пусто",
  "product_image_url": "прямая ссылка на изображение товара без упаковки, если найдена, иначе пусто",
  "packaging_image_url": "прямая ссылка на изображение товара вместе с упаковкой, если найдена, иначе пусто"
}}

Категорийный хэштег должен быть в стиле:
#парфюм, #тональный_крем, #крем, #румяна, #бронзер, #пудра, #тени, #тушь,
#помада, #блеск_для_губ, #карандаш, #хайлайтер, #консилер, #маска, #сыворотка,
#шампунь, #кондиционер, #палетка, #набор и т.п.
Если это готовый подарочный/косметический set, ставь item_type=set и считай его ОДНИМ товаром.
В description не добавляй ссылки и источники.
"""
    content = [{"type": "input_text", "text": prompt}]
    for path in image_paths[:2]:
        try:
            data = Path(path).read_bytes()
            mime = mimetypes.guess_type(path)[0] or "image/jpeg"
            b64 = base64.b64encode(data).decode("ascii")
            content.append({"type": "input_image", "image_url": f"data:{mime};base64,{b64}"})
        except Exception:
            logging.exception("image for vision")
    response = await client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search", "search_context_size": "high"}],
        input=[{"role": "user", "content": content}]
    )
    raw = response.output_text.strip()
    raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    return json.loads(raw)


async def download_image(url):
    if not url: return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20,
                                     headers={"User-Agent": "Mozilla/5.0"}) as h:
            r = await h.get(url)
            if r.status_code == 200 and r.headers.get("content-type", "").lower().startswith("image/"):
                ext = ".jpg"
                ct = r.headers.get("content-type", "")
                if "png" in ct: ext = ".png"
                path = Path("/tmp") / f"beauty_{abs(hash(url))}{ext}"
                path.write_bytes(r.content)
                return str(path)
    except Exception:
        logging.exception("image download")
    return None


def card_caption(data, price, stock):
    lines = [category_hashtag(data.get("category_hashtag")), "", f"✨ {data.get('name','Товар')}", "",
             clean_text(data.get("description", ""))]
    if data.get("volume"): lines += ["", f"📏 Объём: {data['volume']}"]
    if data.get("shade"): lines += [f"🎨 Оттенок: {data['shade']}"]
    if price: lines += ["", f"💰 Цена: {price} ₽"]
    if stock: lines += [f"📦 В наличии: {stock} шт."]
    return clean_text("\n".join(lines))[:1000]

# ---------------- SCHEDULING ----------------

def next_slot(uid):
    u = get_user(uid); tz = ZoneInfo(u["timezone"] or DEFAULT_TZ)
    days = json.loads(u["days"]); times = json.loads(u["times"])
    now = datetime.now(tz).replace(second=0, microsecond=0)
    con = db()
    occupied = {r["scheduled_at"] for r in con.execute(
        "SELECT scheduled_at FROM queue WHERE user_id=? AND status='queued'", (uid,)
    ).fetchall()}
    con.close()
    for offset in range(60):
        date = now.date() + timedelta(days=offset)
        if date.weekday() not in days: continue
        for tm in sorted(times):
            hh, mm = map(int, tm.split(":"))
            dt = datetime(date.year, date.month, date.day, hh, mm, tzinfo=tz)
            iso = dt.isoformat()
            if dt > now and iso not in occupied: return iso
    return (now + timedelta(days=1)).isoformat()


def add_queue(uid, kind, caption, photo1=None, photo2=None):
    when = next_slot(uid)
    con = db()
    cur = con.execute("""INSERT INTO queue
        (user_id,kind,status,scheduled_at,caption,photo1,photo2,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (uid, kind, "queued", when, caption, photo1, photo2, datetime.utcnow().isoformat()))
    qid = cur.lastrowid; con.commit(); con.close()
    return qid, when


async def publish_row(row):
    user = get_user(row["user_id"])
    channel = user["channel_id"]
    if not channel: raise RuntimeError("Канал не подключён")
    if row["kind"] == "product" and row["photo1"]:
        media = [InputMediaPhoto(row["photo1"], caption=row["caption"] or "")]
        if row["photo2"]: media.append(InputMediaPhoto(row["photo2"]))
        await tg_app.bot.send_media_group(chat_id=channel, media=media)
    else:
        await tg_app.bot.send_message(chat_id=channel, text=row["caption"] or "")


async def scheduler_loop():
    while True:
        try:
            con = db()
            rows = con.execute("""SELECT q.*, u.paused, u.channel_id
                FROM queue q JOIN users u ON u.user_id=q.user_id
                WHERE q.status='queued' ORDER BY q.scheduled_at LIMIT 30""").fetchall()
            con.close()
            now = datetime.now(ZoneInfo("UTC"))
            for row in rows:
                if row["paused"] or not row["channel_id"]: continue
                user = get_user(row["user_id"]); tz = ZoneInfo(user["timezone"] or DEFAULT_TZ)
                due = datetime.fromisoformat(row["scheduled_at"])
                if due.tzinfo is None: due = due.replace(tzinfo=tz)
                if due.astimezone(ZoneInfo("UTC")) <= now:
                    try:
                        await publish_row(row)
                        con = db(); con.execute("UPDATE queue SET status='published' WHERE id=?", (row["id"]))
                        con.commit(); con.close()
                        await tg_app.bot.send_message(row["user_id"], f"📢 Опубликовано в канале!\nПубликация #{row['id']}")
                    except Exception as exc:
                        logging.exception("publish failed")
                        con = db(); con.execute("UPDATE queue SET status='error' WHERE id=?", (row["id"]))
                        con.commit(); con.close()
                        await tg_app.bot.send_message(row["user_id"], f"⚠️ Не удалось опубликовать #{row['id']}: {exc}")
        except Exception:
            logging.exception("scheduler loop")
        await asyncio.sleep(30)

# ---------------- KEYBOARDS ----------------

def approval_kb(pid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{pid}"),
         InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{pid}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{pid}")]
    ])


def schedule_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Дни", callback_data="setdays"),
         InlineKeyboardButton("🕒 Время", callback_data="settimes")],
        [InlineKeyboardButton("🌍 Часовой пояс", callback_data="settz"),
         InlineKeyboardButton("⏸/▶️ Пауза", callback_data="togglepause")]
    ])

# ---------------- PRODUCT FLOW ----------------

async def start_product(update, context):
    context.user_data.clear()
    context.user_data["mode"] = "product"
    context.user_data["photos"] = []
    await update.message.reply_text(
        "📸 Пришли первое фото товара.\n\n"
        "Потом можешь прислать второе — с упаковкой. Если второго нет, просто напиши цену и количество."
    )


async def photo_handler(update, context):
    if context.user_data.get("mode") != "product":
        return await update.message.reply_text("Нажми «➕ Новый товар», чтобы добавить товар.")
    photo = update.message.photo[-1].file_id
    photos = context.user_data.setdefault("photos", [])
    if len(photos) < 2: photos.append(photo)
    if len(photos) == 1:
        await update.message.reply_text("Фото №1 получила 💗 Если есть фото с упаковкой — пришли его. Если нет — напиши цену и количество.")
    else:
        await update.message.reply_text("Отлично! Оба фото получила. Теперь напиши цену и количество, например: 7500 ₽, 2 шт.")


async def build_product(update, context):
    uid = update.effective_user.id
    price = context.user_data.get("price"); stock = context.user_data.get("stock")
    photos = context.user_data.get("photos", [])
    vision_paths = []
    for idx, fid in enumerate(photos[:2]):
        try:
            tg_file = await context.bot.get_file(fid)
            path = f"/tmp/product_{uid}_{idx}.jpg"
            await tg_file.download_to_drive(path)
            vision_paths.append(path)
        except Exception:
            logging.exception("telegram photo download")
    try:
        data = await analyze_product(context.user_data.get("name_hint", ""), price, stock,
                                     context.user_data.get("item_type_hint"), vision_paths)
    except Exception:
        logging.exception("AI product analysis")
        return await update.message.reply_text("Не получилось определить товар. Напиши его название текстом — я попробую ещё раз.")

    p1 = photos[0] if photos else None
    p2 = photos[1] if len(photos) > 1 else None

    # If user gave only one image, try to obtain a second web image. User image always wins as photo 1.
    if not p2:
        p2 = await download_image(data.get("packaging_image_url"))
    if not p1:
        p1 = await download_image(data.get("product_image_url"))
    if not p1 and p2:
        p1, p2 = p2, None
    if not p1:
        return await update.message.reply_text("Мне нужно хотя бы одно фото. Пришли его ещё раз, пожалуйста.")

    # Web images need to be uploaded once so Telegram file_id can be reused later.
    async def upload_local(path):
        msg = await update.message.reply_photo(photo=path)
        fid = msg.photo[-1].file_id
        try: await msg.delete()
        except Exception: pass
        return fid

    if p1.startswith("/tmp/"): p1 = await upload_local(p1)
    if p2 and p2.startswith("/tmp/"): p2 = await upload_local(p2)

    caption = card_caption(data, price, stock)
    con = db()
    cur = con.execute("""INSERT INTO products
        (user_id,name,category,item_type,price,stock,volume,shade,description,photo1,photo2,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uid, data.get("name"), category_hashtag(data.get("category_hashtag")),
         data.get("item_type", "single"), price, stock, data.get("volume", ""), data.get("shade", ""),
         data.get("description", ""), p1, p2, datetime.utcnow().isoformat()))
    pid = cur.lastrowid; con.commit(); con.close()
    context.user_data.clear()

    if p2:
        await update.message.reply_media_group([
            InputMediaPhoto(p1, caption=caption), InputMediaPhoto(p2)
        ])
        await update.message.reply_text("Готово! Проверь карточку 👆", reply_markup=approval_kb(pid))
    else:
        await update.message.reply_photo(p1, caption=caption, reply_markup=approval_kb(pid))

# ---------------- TEXT/POSTS ----------------

async def generate_post(uid, topic):
    response = await client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search", "search_context_size": "high"}],
        input=f"""
Напиши готовый короткий пост для Telegram-канала оригинальной косметики и парфюмерии.
Тема: {topic}
Стиль: живой, красивый, дружелюбный, современный, без канцелярита и без агрессивных продаж.
Не выдумывай личный опыт автора. Не добавляй URL или список источников.
Если уместно, используй 1–2 эмодзи, но не перегружай.
"""
    )
    return clean_text(response.output_text)


async def ideas_text():
    response = await client.responses.create(model=MODEL, input="""
Дай 10 конкретных идей для Telegram-канала оригинальной косметики и парфюмерии.
Баланс: примерно 70% полезное/интересное, 20% вовлечение, 10% прямые продажи.
Без лица автора. Сделай идеи такими, чтобы их хотелось сохранить или переслать.
""")
    return clean_text(response.output_text)


async def plan_text():
    response = await client.responses.create(model=MODEL, input="""
Составь недельный контент-план для Telegram-канала оригинальной косметики и парфюмерии.
На каждый день: 3–7 коротких сообщений/сторис-идей и 1–2 основных поста.
Баланс 70/20/10. Добавь опросы, рубрики и ненавязчивые продажи. Без лица автора.
""")
    return clean_text(response.output_text)

# ---------------- CHANNEL / QUEUE / SETTINGS ----------------

async def connect_channel(update, context):
    origin = getattr(update.message, "forward_origin", None)
    chat = getattr(origin, "chat", None) if origin else None
    if chat and getattr(chat, "type", None) == "channel":
        update_user(update.effective_user.id, channel_id=str(chat.id))
        await update.message.reply_text(
            f"📢 Канал «{chat.title}» подключён!\n\n"
            "Убедись, что бот добавлен в канал администратором и у него есть право публиковать сообщения."
        )
    else:
        await update.message.reply_text("Перешли сюда любой пост из твоего канала. Бот определит канал автоматически.")


async def show_queue(update, context):
    uid = update.effective_user.id
    con = db(); rows = con.execute(
        "SELECT * FROM queue WHERE user_id=? AND status='queued' ORDER BY scheduled_at", (uid,)
    ).fetchall(); con.close()
    if not rows: return await update.message.reply_text("📋 Очередь пуста.")
    lines = ["📋 Очередь публикаций:\n"]
    for row in rows[:30]:
        title = next((x for x in (row["caption"] or "").splitlines() if x.startswith("✨ ")), row["kind"])
        lines.append(f"#{row['id']} — {title}\n🕒 {format_dt(row['scheduled_at'], uid)}")
    await update.message.reply_text("\n\n".join(lines))


async def show_schedule(update, context):
    uid = update.effective_user.id; u = get_user(uid)
    days = json.loads(u["days"]); times = json.loads(u["times"])
    names = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    text = (f"⏰ Расписание\n\nДни: {', '.join(names[d] for d in days)}\n"
            f"Время: {', '.join(times)}\nЧасовой пояс: {u['timezone']}\n"
            f"Статус: {'⏸ на паузе' if u['paused'] else '▶️ активно'}\n\n"
            "Одобренные материалы автоматически ставятся в ближайшее свободное время.")
    await update.message.reply_text(text, reply_markup=schedule_kb())


async def set_schedule_days(update, context, text):
    try:
        vals = sorted(set(int(x.strip()) - 1 for x in text.split(",")))
        if not vals or any(v < 0 or v > 6 for v in vals): raise ValueError
        update_user(update.effective_user.id, days=json.dumps(vals)); context.user_data.pop("mode", None)
        await update.message.reply_text("Готово! 💗", reply_markup=MAIN_KB)
    except Exception:
        await update.message.reply_text("Формат: 1,2,3,4,5,6,7")


async def set_schedule_times(update, context, text):
    try:
        vals=[]
        for item in text.split(","):
            hh, mm = map(int, item.strip().split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59): raise ValueError
            vals.append(f"{hh:02d}:{mm:02d}")
        if not vals: raise ValueError
        update_user(update.effective_user.id, times=json.dumps(sorted(set(vals))))
        context.user_data.pop("mode", None); await update.message.reply_text("Готово! 💗", reply_markup=MAIN_KB)
    except Exception:
        await update.message.reply_text("Формат: 10:00, 15:00, 20:00")


async def set_timezone(update, context, text):
    try: ZoneInfo(text.strip())
    except Exception: return await update.message.reply_text("Например: Europe/Moscow или Europe/Warsaw")
    update_user(update.effective_user.id, timezone=text.strip()); context.user_data.pop("mode", None)
    await update.message.reply_text("Готово! 🌍", reply_markup=MAIN_KB)

# ---------------- CALLBACKS ----------------

async def callback_handler(update, context):
    q = update.callback_query; await q.answer(); uid = q.from_user.id
    action = q.data

    if action.startswith("approve:"):
        pid = int(action.split(":")[1]); con=db()
        row = con.execute("SELECT * FROM products WHERE id=? AND user_id=?", (pid, uid)).fetchone(); con.close()
        if not row: return await q.message.reply_text("Товар уже удалён или недоступен.")
        caption = row["category"] + "\n\n" + f"✨ {row['name']}\n\n" + clean_text(row["description"] or "")
        if row["volume"]: caption += f"\n\n📏 Объём: {row['volume']}"
        if row["shade"]: caption += f"\n🎨 Оттенок: {row['shade']}"
        if row["price"]: caption += f"\n\n💰 Цена: {row['price']} ₽"
        if row["stock"]: caption += f"\n📦 В наличии: {row['stock']} шт."
        qid, when = add_queue(uid, "product", clean_text(caption)[:1000], row["photo1"], row["photo2"])
        await q.message.reply_text(f"✅ Одобрено!\nПоставила в очередь #{qid}.\n🕒 {format_dt(when, uid)}")

    elif action.startswith("cancel:"):
        pid=int(action.split(":")[1]); con=db(); con.execute("DELETE FROM products WHERE id=? AND user_id=?",(pid,uid)); con.commit(); con.close()
        await q.message.reply_text("Удалено. 💗")

    elif action.startswith("edit:"):
        context.user_data["edit_product_id"] = int(action.split(":")[1])
        context.user_data["mode"] = "edit_product"
        await q.message.reply_text("Напиши, что изменить. Например: «это бронзер» или «цена 6900».")

    elif action == "setdays":
        context.user_data["mode"]="schedule_days"
        await q.message.reply_text("Дни: 1=Пн, 2=Вт ... 7=Вс. Например: 1,2,3,4,5")
    elif action == "settimes":
        context.user_data["mode"]="schedule_times"
        await q.message.reply_text("Время через запятую: 10:00, 15:00, 20:00")
    elif action == "settz":
        context.user_data["mode"]="timezone"
        await q.message.reply_text("Напиши часовой пояс: Europe/Moscow или Europe/Warsaw")
    elif action == "togglepause":
        u=get_user(uid); new=0 if u["paused"] else 1; update_user(uid,paused=new)
        await q.message.reply_text("⏸ Расписание на паузе." if new else "▶️ Расписание снова активно.")
    elif action == "postapprove":
        draft = context.user_data.get("draft")
        if not draft: return await q.message.reply_text("Черновик больше не доступен. Создай пост ещё раз.")
        qid,when=add_queue(uid,"post",draft); context.user_data.clear()
        await q.message.reply_text(f"✅ Пост одобрен!\n#{qid} — {format_dt(when,uid)}")
    elif action == "postcancel":
        context.user_data.pop("draft",None); await q.message.reply_text("Черновик отменён.")

# ---------------- MAIN MESSAGE HANDLER ----------------

async def text_handler(update, context):
    uid=update.effective_user.id; text=update.message.text.strip(); mode=context.user_data.get("mode")
    if text=="➕ Новый товар": return await start_product(update,context)
    if text=="📝 Новый пост":
        context.user_data["mode"]="post"; return await update.message.reply_text("Напиши тему поста.")
    if text=="📅 План": return await update.message.reply_text(await plan_text())
    if text=="⏰ Расписание": return await show_schedule(update,context)
    if text=="📋 Очередь": return await show_queue(update,context)
    if text=="📢 Канал": return await connect_channel(update,context)
    if text=="📊 Идеи": return await update.message.reply_text(await ideas_text())

    if mode=="product":
        price,stock=parse_price_stock(text)
        if price:
            context.user_data["price"]=price; context.user_data["stock"]=stock
            return await build_product(update,context)
        context.user_data["name_hint"]=text
        return await update.message.reply_text("Название запомнила 💗 Теперь напиши цену и количество, например: 7500 ₽, 2 шт.")

    if mode=="schedule_days": return await set_schedule_days(update,context,text)
    if mode=="schedule_times": return await set_schedule_times(update,context,text)
    if mode=="timezone": return await set_timezone(update,context,text)

    if mode=="edit_product":
        pid=context.user_data.get("edit_product_id")
        con=db(); row=con.execute("SELECT * FROM products WHERE id=? AND user_id=?",(pid,uid)).fetchone(); con.close()
        if not row: return await update.message.reply_text("Товар не найден.")
        # Simple reliable edit: re-run AI with the user's correction as a new hint.
        context.user_data["mode"]="product"; context.user_data["photos"]= [row["photo1"]] + ([row["photo2"]] if row["photo2"] else [])
        context.user_data["price"]=row["price"]; context.user_data["stock"]=row["stock"]; context.user_data["name_hint"]=text
        return await build_product(update,context)

    if mode=="post":
        draft=await generate_post(uid,text); context.user_data["draft"]=draft; context.user_data["mode"]=None
        return await update.message.reply_text(draft,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Одобрить и в очередь",callback_data="postapprove"),InlineKeyboardButton("❌ Отмена",callback_data="postcancel")]]))

    await update.message.reply_text("Выбери действие в меню 👇",reply_markup=MAIN_KB)

# ---------------- COMMANDS / STARTUP ----------------

async def start(update, context):
    ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "Привет! 💗 Я твой бьюти-менеджер.\n\n"
        "Присылай товар — я определю категорию, проверю информацию, подготовлю карточку с хэштегом, "
        "двумя фото и поставлю её в очередь. Ничего не публикую без твоего одобрения.",
        reply_markup=MAIN_KB
    )


async def id_command(update, context):
    await update.message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


@app.on_event("startup")
async def on_startup():
    global scheduler_task
    init_db()
    await tg_app.initialize(); await tg_app.start()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("new", start_product))
    tg_app.add_handler(CommandHandler("plan", lambda u,c: u.message.reply_text("Используй кнопку 📅 План")))
    tg_app.add_handler(CommandHandler("queue", show_queue))
    tg_app.add_handler(CommandHandler("schedule", show_schedule))
    tg_app.add_handler(CommandHandler("channel", connect_channel))
    tg_app.add_handler(CommandHandler("id", id_command))
    tg_app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    tg_app.add_handler(CallbackQueryHandler(callback_handler))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    await tg_app.bot.set_my_commands([
        ("start","Запустить бота"),("new","Новый товар"),("queue","Очередь"),
        ("schedule","Расписание"),("channel","Подключить канал"),("id","Мой ID")
    ])
    await tg_app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await tg_app.updater.start_polling()
    scheduler_task=asyncio.create_task(scheduler_loop())


@app.on_event("shutdown")
async def on_shutdown():
    global scheduler_task
    if scheduler_task:
        scheduler_task.cancel()
        try: await scheduler_task
        except asyncio.CancelledError: pass
    if tg_app.updater.running: await tg_app.updater.stop()
    if tg_app.running: await tg_app.stop()
    await tg_app.shutdown()


@app.get("/")
async def root(): return {"ok": True, "service": "Beauty Manager Bot"}
