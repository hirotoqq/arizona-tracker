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
BOT_TOKEN          = os.environ.get("BOT_TOKEN", "")
CHECK_INTERVAL     = 300
MSK                = timezone(timedelta(hours=3))
STALE_HOURS        = 8     # через сколько часов данные считаются устаревшими
PAGE_SIZE          = 10    # слётов на странице
NOTIFY_HOURS_OPTIONS = [0.5, 1]  # 30 минут, 1 час

# ── Фиксированный порядок серверов ────────────────────────
SERVER_ORDER = [
    "Phoenix", "Tucson", "Scottdale", "Chandler", "Brainburg", "Saint-Rose",
    "Mesa", "Red-Rock", "Yuma", "Surprise", "Prescott", "Glendale",
    "Kingman", "Winslow", "Payson", "Gilbert", "Show Low", "Casa-Grande",
    "Page", "Sun-City", "Queen-Creek", "Sedona", "Holiday", "Wednesday",
    "Yava", "Faraway", "Bumble Bee", "Christmas", "Love", "Mirage",
    "Drake", "Space",
]

# ── Состояние пользователей ───────────────────────────────
# { chat_id: notify_hours }
user_notify_hours   = {}
subscribers         = set()
lottery_subscribers = set()
notified            = set()
lottery_notified    = False

# ── Вспомогательные функции ───────────────────────────────
def get_all_props():
    now  = int(time.time())
    ref  = db.reference("properties")
    data = ref.get() or {}
    result = []
    for srv, entries in data.items():
        if not isinstance(entries, dict):
            continue
        for k, v in entries.items():
            expiry = v.get("expiryTs", 0)
            if expiry <= now:
                continue
            result.append({
                "server":   srv,
                "propType": v.get("propType", "?"),
                "pd":       v.get("pd", 0),
                "expiryTs": expiry,
                "hoursLeft": round((expiry - now) / 3600, 1),
                "scanTs":   v.get("scanTs", 0),
            })
    result.sort(key=lambda x: x["expiryTs"])
    return result

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

def stale_warning(scan_ts):
    if is_stale(scan_ts):
        return "⚠️ _Данные устарели (скан > 8ч назад)_\n"
    return ""

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
        bar = "🔴" if p["hoursLeft"] <= 1 else "🟡" if p["hoursLeft"] <= 3 else "🟢"
        pd_str = f"{p['pd']}pd" if p.get("pd") else ""
        lines.append(
            f"{bar} *{p['server']}* — {prop_type_ru(p['propType'])} {pd_str}\n"
            f"    ⏰ {format_time_msk(p['expiryTs'])} МСК (через {p['hoursLeft']}ч)"
        )
    return "\n".join(lines), total_pages

def get_servers_ordered():
    ref      = db.reference("properties")
    data     = ref.get() or {}
    existing = set(data.keys())
    ordered  = [s for s in SERVER_ORDER if s in existing]
    for s in existing:
        if s not in ordered:
            ordered.append(s)
    return ordered

def get_last_scan(server):
    ref  = db.reference(f"properties/{server}")
    data = ref.get() or {}
    if not isinstance(data, dict):
        return None
    scan_times = [v.get("scanTs", 0) for v in data.values() if isinstance(v, dict)]
    return max(scan_times) if scan_times else None

# ── Постоянная клавиатура (внизу экрана) ─────────────────
def permanent_keyboard():
    keyboard = [
        [KeyboardButton("📋 Все слёты"),        KeyboardButton("⚠️ Ближайшие")],
        [KeyboardButton("🗺 По серверу"),        KeyboardButton("🔔 Уведомления")],
        [KeyboardButton("🎰 Лотерея"),           KeyboardButton("ℹ️ О боте")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

# ── Inline кнопки ─────────────────────────────────────────
def back_to_menu_inline():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="action_menu")]])

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
        "на серверах Arizona RP\\.\n\n"
        "📡 Данные собираются автоматически от игроков с установленным скриптом\\.\n\n"
        "👨‍💻 Создатель: @hirotoqq\n\n"
        "Используй кнопки внизу экрана для навигации\\."
    )
    await update.message.reply_text(
        text.replace("\\.", "."),
        parse_mode="Markdown",
        reply_markup=permanent_keyboard()
    )

# ── Обработка текстовых кнопок ────────────────────────────
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

async def show_list(update, ctx, page=0):
    props = get_all_props()
    text, total = build_list_text(props, page=page)
    buttons = pagination_buttons(page, total, "list")
    msg = update.message or update.callback_query.message
    kb  = InlineKeyboardMarkup(buttons) if buttons else None
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def show_soon(update, ctx, page=0):
    props = [p for p in get_all_props() if p["hoursLeft"] <= 3]
    text, total = build_list_text(props, "⚠️ Слёты в ближайшие 3 часа", page=page)
    buttons = pagination_buttons(page, total, "soon")
    kb = InlineKeyboardMarkup(buttons) if buttons else None
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def show_servers(update, ctx):
    servers = get_servers_ordered()
    if not servers:
        msg = "Данных пока нет."
        if update.message:
            await update.message.reply_text(msg)
        else:
            await update.callback_query.edit_message_text(msg)
        return
    buttons = []
    row = []
    for s in servers:
        scan_ts = get_last_scan(s)
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
    is_sub  = chat_id in subscribers
    hours   = user_notify_hours.get(chat_id, 1)
    status  = "✅ Подписан" if is_sub else "❌ Не подписан"
    btn     = "🔕 Отписаться" if is_sub else "🔔 Подписаться"
    buttons = [
        [InlineKeyboardButton(btn, callback_data="action_notify_toggle")],
        [
            InlineKeyboardButton("30 мин" + (" ✓" if hours == 0.5 else ""), callback_data="notify_hours_0.5"),
            InlineKeyboardButton("1 час"  + (" ✓" if hours == 1.0 else ""), callback_data="notify_hours_1.0"),
        ],
    ]
    text = f"🔔 *Уведомления о слётах*\n\nСтатус: {status}\nПредупреждение за: {'30 мин' if hours == 0.5 else '1 час'}"
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_lottery_menu(update, ctx):
    chat_id = update.effective_chat.id
    is_sub  = chat_id in lottery_subscribers
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

    # Пагинация списка
    if data.startswith("list_page_"):
        page = int(data.split("_")[-1])
        await show_list(update, ctx, page=page)

    elif data.startswith("soon_page_"):
        page = int(data.split("_")[-1])
        await show_soon(update, ctx, page=page)

    # Сервер
    elif data.startswith("srv_"):
        server    = data.replace("srv_", "")
        props     = [p for p in get_all_props() if p["server"] == server]
        last_scan = get_last_scan(server)
        scan_str  = format_last_scan(last_scan)
        warn      = "⚠️ Данные устарели (скан > 8ч назад)\n\n" if is_stale(last_scan) else ""
        text, total = build_list_text(props, f"📋 {server}", page=0)
        text = warn + text + f"\n\n🕐 _Последний скан: {scan_str}_"
        buttons = [[InlineKeyboardButton("◀️ К серверам", callback_data="action_servers")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "action_servers":
        await show_servers(update, ctx)

    # Уведомления слёты
    elif data == "action_notify_toggle":
        if chat_id in subscribers:
            subscribers.discard(chat_id)
        else:
            subscribers.add(chat_id)
        await show_notify_menu(update, ctx)

    elif data.startswith("notify_hours_"):
        hours = float(data.replace("notify_hours_", ""))
        user_notify_hours[chat_id] = hours
        await show_notify_menu(update, ctx)

    # Уведомления лотерея
    elif data == "action_lottery_toggle":
        if chat_id in lottery_subscribers:
            lottery_subscribers.discard(chat_id)
        else:
            lottery_subscribers.add(chat_id)
        await show_lottery_menu(update, ctx)

# ── Фоновые задачи ────────────────────────────────────────
async def notify_loop(app):
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        props = get_all_props()
        now   = time.time()
        for p in props:
            for chat_id in list(subscribers):
                hours = user_notify_hours.get(chat_id, 1.0)
                if p["hoursLeft"] <= hours:
                    key = f"{chat_id}_{p['server']}_{p['propType']}_{p['expiryTs']}"
                    if key not in notified:
                        notified.add(key)
                        text = (
                            f"⚠️ *Скоро слёт!*\n"
                            f"Сервер: *{p['server']}*\n"
                            f"Тип: {prop_type_ru(p['propType'])} {p['pd']}pd\n"
                            f"Слетит в {format_time_msk(p['expiryTs'])} МСК "
                            f"(через {p['hoursLeft']}ч)"
                        )
                        try:
                            await app.bot.send_message(chat_id, text, parse_mode="Markdown")
                        except Exception:
                            pass

async def lottery_loop(app):
    global lottery_notified
    while True:
        await asyncio.sleep(30)
        now_msk = datetime.now(tz=MSK)
        if now_msk.hour == 21 and now_msk.minute == 5:
            if not lottery_notified:
                lottery_notified = True
                for chat_id in lottery_subscribers:
                    try:
                        await app.bot.send_message(
                            chat_id,
                            "🎰 *Билеты через 5 минут!*\nЛотерея начнётся в 21:10 МСК.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
        else:
            lottery_notified = False

# ── Запуск ────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(notify_loop(app))
    loop.create_task(lottery_loop(app))

    print("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()