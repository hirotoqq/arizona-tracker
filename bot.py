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
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
CHECK_INTERVAL = 60    # проверка каждую минуту
MSK            = timezone(timedelta(hours=3))
STALE_HOURS    = 8
PAGE_SIZE      = 10
RENDER_URL     = os.environ.get("RENDER_URL", "https://arizona-tracker.onrender.com")

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
# { chat_id: minutes } — за сколько минут предупреждать о слёте
user_notify_minutes  = {}   # default 60
subscribers          = set()
lottery_subscribers  = set()
lottery_notify_min   = {}   # { chat_id: minutes } — 5 или 10
notified             = set()
lottery_notified_min = {}   # { chat_id: bool }

NOTIFY_OPTIONS = [60, 50, 40, 30, 20, 10, 5]
LOTTERY_OPTIONS = [10, 5]

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
                "server":    srv,
                "propType":  v.get("propType", "?"),
                "pd":        v.get("pd", 0),
                "expiryTs":  expiry,
                "hoursLeft": round((expiry - now) / 3600, 1),
                "minsLeft":  round((expiry - now) / 60),
                "scanTs":    v.get("scanTs", 0),
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

def build_list_text(props, title="📋 Актуальные слёты", page=0):
    if not props:
        return "✅ Слётов нет или данных пока нет.", 0

    # Считаем сколько объектов каждого типа падает на каждом сервере в одно время
    from collections import defaultdict
    srv_expiry_count = defaultdict(lambda: defaultdict(int))
    for p in props:
        srv_expiry_count[(p["server"], p["expiryTs"])][p["propType"]] += 1

    houses = sum(1 for p in props if p["propType"] == "house")
    bizs   = sum(1 for p in props if p["propType"] == "business")

    total_pages = max(1, (len(props) + PAGE_SIZE - 1) // PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))
    chunk       = props[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [f"*{title}*"]
    stats = []
    if houses: stats.append(f"🏠 Домов: {houses}")
    if bizs:   stats.append(f"🏢 Бизнесов: {bizs}")
    if stats:  lines.append(" | ".join(stats))
    if total_pages > 1:
        lines.append(f"_Страница {page + 1} из {total_pages}_")
    lines.append("")

    seen = set()
    for p in chunk:
        key = (p["server"], p["expiryTs"], p["propType"])
        if key in seen:
            continue
        seen.add(key)

        bar   = "🔴" if p["hoursLeft"] <= 1 else "🟡" if p["hoursLeft"] <= 3 else "🟢"
        pd_str = f" {p['pd']}pd" if p.get("pd") else ""
        cnt   = srv_expiry_count[(p["server"], p["expiryTs"])][p["propType"]]
        cnt_str = f" (×{cnt})" if cnt > 1 else ""

        lines.append(
            f"{bar} *{p['server']}*{cnt_str} — {prop_type_ru(p['propType'])}{pd_str}\n"
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

# ── Постоянная клавиатура ─────────────────────────────────
def permanent_keyboard():
    keyboard = [
        [KeyboardButton("📋 Все слёты"),        KeyboardButton("⚠️ Ближайшие")],
        [KeyboardButton("🗺 По серверу"),        KeyboardButton("🔔 Уведомления")],
        [KeyboardButton("🎰 Лотерея"),           KeyboardButton("ℹ️ О боте")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

def back_btn(cb):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=cb)]])

# ── /start ────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Arizona Property Tracker*\n\n"
        "Используй кнопки внизу экрана для навигации.\n\n"
        "👨‍💻 Создатель: @hiroto"
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
    props = get_all_props()
    text, total = build_list_text(props, page=page)
    buttons = _page_buttons(page, total, "list")
    kb = InlineKeyboardMarkup(buttons) if buttons else None
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def show_soon(update, ctx, page=0):
    props = [p for p in get_all_props() if p["hoursLeft"] <= 3]
    text, total = build_list_text(props, "⚠️ Слёты в ближайшие 3 часа", page=page)
    buttons = _page_buttons(page, total, "soon")
    kb = InlineKeyboardMarkup(buttons) if buttons else None
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

def _page_buttons(page, total, prefix):
    buttons = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}_page_{page-1}"))
    row.append(InlineKeyboardButton("🔄", callback_data=f"{prefix}_page_{page}"))
    if page < total - 1:
        row.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}_page_{page+1}"))
    if row:
        buttons.append(row)
    return buttons

async def show_servers(update, ctx):
    servers = get_servers_ordered()
    if not servers:
        txt = "Данных пока нет."
        if update.message:
            await update.message.reply_text(txt)
        else:
            await update.callback_query.edit_message_text(txt)
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
    text = "🗺 *Выбери сервер:*\n🟢 свежие данные  🔴 устаревшие"
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_notify_menu(update, ctx):
    chat_id = update.effective_chat.id
    is_sub  = chat_id in subscribers
    mins    = user_notify_minutes.get(chat_id, 60)
    status  = "✅ Подписан" if is_sub else "❌ Не подписан"
    btn     = "🔕 Отписаться" if is_sub else "🔔 Подписаться"

    # Кнопки выбора времени
    time_buttons = []
    row = []
    for m in NOTIFY_OPTIONS:
        label = f"{m} мин" + (" ✓" if m == mins else "")
        row.append(InlineKeyboardButton(label, callback_data=f"notify_min_{m}"))
        if len(row) == 4:
            time_buttons.append(row)
            row = []
    if row:
        time_buttons.append(row)

    buttons = [
        [InlineKeyboardButton(btn, callback_data="action_notify_toggle")],
        *time_buttons,
    ]
    text = (
        f"🔔 *Уведомления о слётах*\n\n"
        f"Статус: {status}\n"
        f"Предупреждение за: *{mins} мин*"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_lottery_menu(update, ctx):
    chat_id = update.effective_chat.id
    is_sub  = chat_id in lottery_subscribers
    mins    = lottery_notify_min.get(chat_id, 5)
    status  = "✅ Подписан" if is_sub else "❌ Не подписан"
    btn     = "🔕 Отписаться" if is_sub else "🔔 Подписаться"
    buttons = [
        [InlineKeyboardButton(btn, callback_data="action_lottery_toggle")],
        [
            InlineKeyboardButton("10 мин" + (" ✓" if mins == 10 else ""), callback_data="lottery_min_10"),
            InlineKeyboardButton("5 мин"  + (" ✓" if mins == 5  else ""), callback_data="lottery_min_5"),
        ],
    ]
    text = (
        f"🎰 *Уведомления о лотерее*\n\n"
        f"Статус: {status}\n"
        f"Билеты в 21:10 МСК\n"
        f"Уведомление за: *{mins} мин*"
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
        "👨‍💻 Создатель: @hiroto"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Callbacks ─────────────────────────────────────────────
async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id

    if data.startswith("list_page_"):
        await show_list(update, ctx, page=int(data.split("_")[-1]))

    elif data.startswith("soon_page_"):
        await show_soon(update, ctx, page=int(data.split("_")[-1]))

    elif data == "action_servers":
        await show_servers(update, ctx)

    elif data.startswith("srv_"):
        server    = data.replace("srv_", "")
        props     = [p for p in get_all_props() if p["server"] == server]
        last_scan = get_last_scan(server)
        scan_str  = format_last_scan(last_scan)
        warn      = "⚠️ Данные устарели (скан > 8ч назад)\n\n" if is_stale(last_scan) else ""

        # Считаем дома и бизнесы для сервера
        houses = sum(1 for p in props if p["propType"] == "house")
        bizs   = sum(1 for p in props if p["propType"] == "business")
        stats  = []
        if houses: stats.append(f"🏠 {houses}")
        if bizs:   stats.append(f"🏢 {bizs}")
        stats_str = "  ".join(stats)

        text, _ = build_list_text(props, f"📋 {server}  {stats_str}", page=0)
        text = warn + text + f"\n\n🕐 _Последний скан: {scan_str}_"
        buttons = [[InlineKeyboardButton("◀️ К серверам", callback_data="action_servers")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "action_notify_toggle":
        if chat_id in subscribers:
            subscribers.discard(chat_id)
        else:
            subscribers.add(chat_id)
        await show_notify_menu(update, ctx)

    elif data.startswith("notify_min_"):
        user_notify_minutes[chat_id] = int(data.split("_")[-1])
        await show_notify_menu(update, ctx)

    elif data == "action_lottery_toggle":
        if chat_id in lottery_subscribers:
            lottery_subscribers.discard(chat_id)
        else:
            lottery_subscribers.add(chat_id)
        await show_lottery_menu(update, ctx)

    elif data.startswith("lottery_min_"):
        lottery_notify_min[chat_id] = int(data.split("_")[-1])
        await show_lottery_menu(update, ctx)

# ── Фоновые задачи ────────────────────────────────────────
async def ping_loop():
    """Пингует сам себя чтобы Render не засыпал."""
    import httpx
    await asyncio.sleep(60)
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await client.get(f"{RENDER_URL}/ping", timeout=10)
        except Exception:
            pass
        await asyncio.sleep(600)  # каждые 10 минут

async def notify_loop(app):
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        props = get_all_props()
        for p in props:
            for chat_id in list(subscribers):
                mins = user_notify_minutes.get(chat_id, 60)
                if p["minsLeft"] <= mins:
                    key = f"{chat_id}_{p['server']}_{p['propType']}_{p['expiryTs']}"
                    if key not in notified:
                        notified.add(key)
                        text = (
                            f"⚠️ *Скоро слёт!*\n"
                            f"Сервер: *{p['server']}*\n"
                            f"Тип: {prop_type_ru(p['propType'])} {p['pd']}pd\n"
                            f"Слетит в {format_time_msk(p['expiryTs'])} МСК "
                            f"(через {p['minsLeft']} мин)"
                        )
                        try:
                            await app.bot.send_message(chat_id, text, parse_mode="Markdown")
                        except Exception:
                            pass

async def lottery_loop(app):
    while True:
        await asyncio.sleep(30)
        now_msk = datetime.now(tz=MSK)
        for chat_id in list(lottery_subscribers):
            mins = lottery_notify_min.get(chat_id, 5)
            # Время уведомления: 21:10 - mins
            notify_hour   = 21
            notify_minute = 10 - mins
            if notify_minute < 0:
                notify_hour   -= 1
                notify_minute += 60

            if now_msk.hour == notify_hour and now_msk.minute == notify_minute:
                already = lottery_notified_min.get(chat_id, False)
                if not already:
                    lottery_notified_min[chat_id] = True
                    try:
                        await app.bot.send_message(
                            chat_id,
                            f"🎰 *Билеты через {mins} минут!*\nЛотерея начнётся в 21:10 МСК.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
            else:
                lottery_notified_min[chat_id] = False

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
    loop.create_task(ping_loop())

    print("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()