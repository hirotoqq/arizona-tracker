import asyncio, time, os, json
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import firebase_admin
from firebase_admin import credentials, db

# ── Firebase ──────────────────────────────────────────────
if not firebase_admin._apps:
    firebase_json = os.environ.get("FIREBASE_CREDENTIALS")
    if firebase_json:
        cred_dict = json.loads(firebase_json)
        cred = credentials.Certificate(cred_dict)
    else:
        cred = credentials.Certificate("serviceAccount.json")
    firebase_admin.initialize_app(cred, {
        "databaseURL": "https://arizona-property-tracker-default-rtdb.firebaseio.com"
    })

# ── Настройки ─────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
CHECK_INTERVAL   = 300
MSK              = timezone(timedelta(hours=3))
STALE_HOURS      = 8
PAGE_SIZE        = 10

# ── Фиксированный порядок серверов ────────────────────────
SERVER_ORDER = [
    "Phoenix", "Tucson", "Scottdale", "Chandler", "Brainburg", "Saint-Rose",
    "Mesa", "Red-Rock", "Yuma", "Surprise", "Prescott", "Glendale",
    "Kingman", "Winslow", "Payson", "Gilbert", "Show Low", "Casa-Grande",
    "Page", "Sun-City", "Queen-Creek", "Sedona", "Holiday", "Wednesday",
    "Yava", "Faraway", "Bumble Bee", "Christmas", "Love", "Mirage",
    "Drake", "Space",
]

# ── Firebase-ключи для подписок ───────────────────────────
SUBS_REF         = "subscribers"
LOTTERY_SUBS_REF = "lottery_subscribers"

# ── In-memory кэш нотификаций (ключ → expiryTs) ──────────
notified: dict[str, int] = {}
lottery_notified = False

# ── Firebase: подписки ────────────────────────────────────
def fb_get_subscribers() -> dict:
    """Возвращает {chat_id_str: {"hours": float}}"""
    data = db.reference(SUBS_REF).get() or {}
    return data

def fb_subscribe(chat_id: int, hours: float):
    db.reference(f"{SUBS_REF}/{chat_id}").set({"hours": hours})

def fb_unsubscribe(chat_id: int):
    db.reference(f"{SUBS_REF}/{chat_id}").delete()

def fb_set_hours(chat_id: int, hours: float):
    ref = db.reference(f"{SUBS_REF}/{chat_id}")
    data = ref.get()
    if data:
        ref.update({"hours": hours})

def fb_is_subscribed(chat_id: int) -> bool:
    return db.reference(f"{SUBS_REF}/{chat_id}").get() is not None

def fb_get_hours(chat_id: int) -> float:
    data = db.reference(f"{SUBS_REF}/{chat_id}").get()
    if data and "hours" in data:
        return float(data["hours"])
    return 1.0

def fb_get_lottery_subscribers() -> list:
    data = db.reference(LOTTERY_SUBS_REF).get() or {}
    return list(data.keys())

def fb_lottery_subscribe(chat_id: int):
    db.reference(f"{LOTTERY_SUBS_REF}/{chat_id}").set(True)

def fb_lottery_unsubscribe(chat_id: int):
    db.reference(f"{LOTTERY_SUBS_REF}/{chat_id}").delete()

def fb_is_lottery_subscribed(chat_id: int) -> bool:
    return db.reference(f"{LOTTERY_SUBS_REF}/{chat_id}").get() is not None

# ── Получение всех свойств (один запрос) ─────────────────
def get_all_data() -> dict:
    """Возвращает сырые данные из Firebase одним запросом."""
    return db.reference("properties").get() or {}

def parse_props(data: dict) -> list:
    """Парсит сырые данные в список актуальных слётов."""
    now = int(time.time())
    result = []
    for srv, entries in data.items():
        if not isinstance(entries, dict):
            continue
        for k, v in entries.items():
            expiry = v.get("expiryTs", 0)
            if expiry <= now:
                continue
            result.append({
                "server":    srv,
                "propType":  v.get("propType", "?"),
                "pd":        v.get("pd", 0),
                "expiryTs":  expiry,
                "hoursLeft": round((expiry - now) / 3600, 1),
                "scanTs":    v.get("scanTs", 0),
            })
    result.sort(key=lambda x: x["expiryTs"])
    return result

def get_server_scan_ts(data: dict, server: str):
    """Вычисляет последний scanTs для сервера из уже загруженных данных."""
    entries = data.get(server)
    if not isinstance(entries, dict):
        return None
    scan_times = [v.get("scanTs", 0) for v in entries.values() if isinstance(v, dict)]
    return max(scan_times) if scan_times else None

# ── Вспомогательные функции ───────────────────────────────
def format_time_msk(ts):
    dt_msk    = datetime.fromtimestamp(ts, tz=MSK)
    today_msk = datetime.now(tz=MSK).strftime("%d.%m")
    day = dt_msk.strftime("%d.%m")
    hm  = dt_msk.strftime("%H:%M")
    return hm if day == today_msk else f"{hm} {day}"

def format_last_scan(ts):
    if not ts:
        return "нет данных"
    dt = datetime.fromtimestamp(ts, tz=MSK)
    return dt.strftime("%d.%m %H:%M МСК")

def prop_type_ru(pt):
    if pt == "house":    return "Дом"
    if pt == "business": return "Бизнес"
    return pt

def is_stale(scan_ts):
    if not scan_ts:
        return True
    return (time.time() - scan_ts) > STALE_HOURS * 3600

def build_list_text(props, title="📋 Актуальные слёты", page=0):
    if not props:
        return "✅ Слётов нет или данных пока нет.", 0

    total_pages = max(1, (len(props) + PAGE_SIZE - 1) // PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))
    chunk       = props[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [f"*{title}*"]
    if total_pages > 1:
        lines.append(f"_Страница {page + 1} из {total_pages}_")
    lines.append("")

    for p in chunk:
        bar    = "🔴" if p["hoursLeft"] <= 1 else "🟡" if p["hoursLeft"] <= 3 else "🟢"
        pd_str = f"{p['pd']}pd" if p.get("pd") else ""
        lines.append(
            f"{bar} *{p['server']}* — {prop_type_ru(p['propType'])} {pd_str}\n"
            f"    ⏰ {format_time_msk(p['expiryTs'])} МСК (через {p['hoursLeft']}ч)"
        )
    return "\n".join(lines), total_pages

# ── Клавиатуры ────────────────────────────────────────────
def permanent_keyboard():
    keyboard = [
        [KeyboardButton("📋 Все слёты"),   KeyboardButton("⚠️ Ближайшие")],
        [KeyboardButton("🗺 По серверу"),  KeyboardButton("🔔 Уведомления")],
        [KeyboardButton("🎰 Лотерея"),     KeyboardButton("ℹ️ О боте")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

def pagination_buttons(page, total_pages, prefix):
    buttons = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}_page_{page-1}"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}_page_{page+1}"))
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"{prefix}_page_{page}")])
    return buttons

# ── /start ────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Arizona Property Tracker*\n\n"
        "Этот бот помогает отслеживать слёты домов и бизнесов "
        "на серверах Arizona RP.\n\n"
        "📡 Данные собираются автоматически от игроков с установленным скриптом.\n\n"
        "👨‍💻 Создатель: @hirotoqq\n\n"
        "‼️ Правильное время слёта отображается если дом/бизнес застрахован "
        "(без страховки время может быть некорректным).\n\n"
        "Используй кнопки внизу экрана для навигации."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=permanent_keyboard())

# ── Текстовые кнопки ──────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📋 Все слёты":
        await show_list(update, ctx, page=0)
    elif text == "⚠️ Ближайшие":
        await show_soon(update, ctx, page=0)
    elif text == "🗺 По серверу":
        await show_servers(update, ctx)
    elif text == "🔔 Уведомления":
        await show_notify_menu(update, ctx)
    elif text == "🎰 Лотерея":
        await show_lottery_menu(update, ctx)
    elif text == "ℹ️ О боте":
        await show_about(update, ctx)

# ── Показ списков ─────────────────────────────────────────
async def show_list(update, ctx, page=0):
    data  = get_all_data()
    props = parse_props(data)
    text, total = build_list_text(props, page=page)
    buttons = pagination_buttons(page, total, "list")
    kb = InlineKeyboardMarkup(buttons) if buttons else None
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def show_soon(update, ctx, page=0):
    data  = get_all_data()
    props = [p for p in parse_props(data) if p["hoursLeft"] <= 3]
    text, total = build_list_text(props, "⚠️ Слёты в ближайшие 3 часа", page=page)
    buttons = pagination_buttons(page, total, "soon")
    kb = InlineKeyboardMarkup(buttons) if buttons else None
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def show_servers(update, ctx):
    # ОДИН запрос к Firebase вместо N+1
    data = get_all_data()
    if not data:
        msg = "Данных пока нет."
        if update.message:
            await update.message.reply_text(msg)
        else:
            await update.callback_query.edit_message_text(msg)
        return

    # Порядок: сначала из SERVER_ORDER, потом неизвестные
    ordered = [s for s in SERVER_ORDER if s in data]
    for s in data:
        if s not in ordered:
            ordered.append(s)

    buttons = []
    row = []
    for s in ordered:
        scan_ts = get_server_scan_ts(data, s)
        icon    = "🔴" if is_stale(scan_ts) else "🟢"
        row.append(InlineKeyboardButton(f"{icon} {s}", callback_data=f"srv_{s}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    kb   = InlineKeyboardMarkup(buttons)
    text = "🗺 *Выбери сервер:*\n🟢 — данные свежие  🔴 — данные устарели"
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def show_notify_menu(update, ctx):
    chat_id = update.effective_chat.id
    is_sub  = fb_is_subscribed(chat_id)
    hours   = fb_get_hours(chat_id) if is_sub else 1.0
    status  = "✅ Подписан" if is_sub else "❌ Не подписан"
    btn     = "🔕 Отписаться" if is_sub else "🔔 Подписаться"
    buttons = [
        [InlineKeyboardButton(btn, callback_data="action_notify_toggle")],
        [
            InlineKeyboardButton("30 мин" + (" ✓" if hours == 0.5 else ""), callback_data="notify_hours_0.5"),
            InlineKeyboardButton("1 час"  + (" ✓" if hours == 1.0 else ""), callback_data="notify_hours_1.0"),
        ],
    ]
    text = (
        f"🔔 *Уведомления о слётах*\n\n"
        f"Статус: {status}\n"
        f"Предупреждение за: {'30 мин' if hours == 0.5 else '1 час'}"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_lottery_menu(update, ctx):
    chat_id = update.effective_chat.id
    is_sub  = fb_is_lottery_subscribed(chat_id)
    status  = "✅ Подписан" if is_sub else "❌ Не подписан"
    btn     = "🔕 Отписаться" if is_sub else "🔔 Подписаться"
    buttons = [[InlineKeyboardButton(btn, callback_data="action_lottery_toggle")]]
    text = (
        f"🎰 *Уведомления о лотерее*\n\n"
        f"Статус: {status}\n"
        f"Билеты слетают каждый день в 21:10 МСК.\n"
        f"Уведомление приходит в 21:05 МСК."
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_about(update, ctx):
    text = (
        "ℹ️ *Arizona Property Tracker*\n\n"
        "Бот отслеживает слёты домов и бизнесов на серверах Arizona RP.\n\n"
        "📡 Данные собираются автоматически от игроков с Lua скриптом.\n"
        "🕐 Время отображается по МСК (UTC+3).\n"
        "⚠️ Данные считаются устаревшими через 8 часов без скана.\n\n"
        "👨‍💻 Создатель: @hirotoqq"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Inline callbacks ───────────────────────────────────────
async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id

    if data.startswith("list_page_"):
        page = int(data.split("_")[-1])
        await show_list(update, ctx, page=page)

    elif data.startswith("soon_page_"):
        page = int(data.split("_")[-1])
        await show_soon(update, ctx, page=page)

    elif data.startswith("srv_"):
        server = data.replace("srv_", "")
        # Один запрос — берём и props и scan_ts из одних данных
        raw     = get_all_data()
        props   = [p for p in parse_props(raw) if p["server"] == server]
        scan_ts = get_server_scan_ts(raw, server)
        scan_str = format_last_scan(scan_ts)
        warn     = "⚠️ Данные устарели (скан > 8ч назад)\n\n" if is_stale(scan_ts) else ""
        text, _  = build_list_text(props, f"📋 {server}", page=0)
        text     = warn + text + f"\n\n🕐 _Последний скан: {scan_str}_"
        buttons  = [[InlineKeyboardButton("◀️ К серверам", callback_data="action_servers")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "action_servers":
        await show_servers(update, ctx)

    elif data == "action_notify_toggle":
        if fb_is_subscribed(chat_id):
            fb_unsubscribe(chat_id)
        else:
            fb_subscribe(chat_id, 1.0)
        await show_notify_menu(update, ctx)

    elif data.startswith("notify_hours_"):
        hours = float(data.replace("notify_hours_", ""))
        if fb_is_subscribed(chat_id):
            fb_set_hours(chat_id, hours)
        else:
            fb_subscribe(chat_id, hours)
        await show_notify_menu(update, ctx)

    elif data == "action_lottery_toggle":
        if fb_is_lottery_subscribed(chat_id):
            fb_lottery_unsubscribe(chat_id)
        else:
            fb_lottery_subscribe(chat_id)
        await show_lottery_menu(update, ctx)

# ── Фоновые задачи ────────────────────────────────────────
def _clean_notified():
    """Удаляет из notified устаревшие записи (expiryTs уже прошёл)."""
    now = int(time.time())
    expired = [k for k, exp in notified.items() if exp <= now]
    for k in expired:
        del notified[k]

async def notify_loop(app):
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            _clean_notified()
            data  = get_all_data()
            props = parse_props(data)
            subs  = fb_get_subscribers()  # {chat_id_str: {"hours": float}}

            for p in props:
                for chat_id_str, info in subs.items():
                    hours   = float(info.get("hours", 1.0)) if isinstance(info, dict) else 1.0
                    chat_id = int(chat_id_str)
                    if p["hoursLeft"] <= hours:
                        key = f"{chat_id}_{p['server']}_{p['propType']}_{p['expiryTs']}"
                        if key not in notified:
                            notified[key] = p["expiryTs"]
                            text = (
                                f"⚠️ *Скоро слёт!*\n"
                                f"Сервер: *{p['server']}*\n"
                                f"Тип: {prop_type_ru(p['propType'])} {p['pd']}pd\n"
                                f"Слетит в {format_time_msk(p['expiryTs'])} МСК "
                                f"(через {p['hoursLeft']}ч)"
                            )
                            try:
                                await app.bot.send_message(chat_id, text, parse_mode="Markdown")
                            except Exception as e:
                                print(f"[notify] ошибка отправки {chat_id}: {e}")
        except Exception as e:
            print(f"[notify_loop] ошибка: {e}")

async def lottery_loop(app):
    global lottery_notified
    while True:
        await asyncio.sleep(30)
        try:
            now_msk = datetime.now(tz=MSK)
            if now_msk.hour == 21 and now_msk.minute == 5:
                if not lottery_notified:
                    lottery_notified = True
                    subs = fb_get_lottery_subscribers()
                    for chat_id_str in subs:
                        try:
                            await app.bot.send_message(
                                int(chat_id_str),
                                "🎰 *Билеты через 5 минут!*\nЛотерея начнётся в 21:10 МСК.",
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            print(f"[lottery] ошибка отправки {chat_id_str}: {e}")
            else:
                lottery_notified = False
        except Exception as e:
            print(f"[lottery_loop] ошибка: {e}")

# ── post_init: запуск фоновых задач ВНУТРИ event loop бота ─
async def post_init(app):
    asyncio.create_task(notify_loop(app))
    asyncio.create_task(lottery_loop(app))
    print("Фоновые задачи запущены.")

# ── Запуск ────────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()