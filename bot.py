import asyncio, time, os, json
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
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
NOTIFY_HOURS   = 3
CHECK_INTERVAL = 300
MSK            = timezone(timedelta(hours=3))

# ── Фиксированный порядок серверов ────────────────────────
SERVER_ORDER = [
    "Phoenix", "Tucson", "Scottdale", "Chandler", "Brainburg", "Saint-Rose",
    "Mesa", "Red-Rock", "Yuma", "Surprise", "Prescott", "Glendale",
    "Kingman", "Winslow", "Payson", "Gilbert", "Show Low", "Casa-Grande",
    "Page", "Sun-City", "Queen-Creek", "Sedona", "Holiday", "Wednesday",
    "Yava", "Faraway", "Bumble Bee", "Christmas", "Love", "Mirage",
    "Drake", "Space",
]

# ── Подписчики ────────────────────────────────────────────
subscribers         = set()   # слёты
lottery_subscribers = set()   # лотерея
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
            })
    result.sort(key=lambda x: x["expiryTs"])
    return result

def format_time_msk(ts):
    """Форматирует UTC timestamp в МСК время."""
    dt_msk   = datetime.fromtimestamp(ts, tz=MSK)
    today_msk = datetime.now(tz=MSK).strftime("%d.%m")
    day = dt_msk.strftime("%d.%m")
    hm  = dt_msk.strftime("%H:%M")
    return hm if day == today_msk else f"{hm} {day}"

def format_last_scan(ts):
    """Форматирует время последнего скана в МСК."""
    if not ts:
        return "нет данных"
    dt = datetime.fromtimestamp(ts, tz=MSK)
    return dt.strftime("%d.%m %H:%M МСК")

def prop_type_ru(pt):
    if pt == "house":    return "Дом"
    if pt == "business": return "Бизнес"
    return pt

def build_list_text(props, title="📋 Актуальные слёты"):
    if not props:
        return "✅ Слётов нет или данных пока нет\\."
    lines = [f"*{title}*\n"]
    for p in props:
        bar = "🔴" if p["hoursLeft"] <= 1 else "🟡" if p["hoursLeft"] <= 3 else "🟢"
        lines.append(
            f"{bar} *{p['server']}* — {prop_type_ru(p['propType'])}\n"
            f"    ⏰ Слетит в {format_time_msk(p['expiryTs'])} МСК "
            f"\\(через {p['hoursLeft']}ч\\)"
        )
    return "\n".join(lines)

def get_servers_ordered():
    """Возвращает серверы в фиксированном порядке, только те что есть в БД."""
    ref  = db.reference("properties")
    data = ref.get() or {}
    existing = set(data.keys())
    ordered = [s for s in SERVER_ORDER if s in existing]
    # Добавляем серверы которых нет в SERVER_ORDER (на случай новых)
    for s in existing:
        if s not in ordered:
            ordered.append(s)
    return ordered

def get_last_scan(server):
    """Получает время последнего скана сервера из Firebase."""
    ref  = db.reference(f"properties/{server}")
    data = ref.get() or {}
    if not isinstance(data, dict):
        return None
    scan_times = [v.get("scanTs", 0) for v in data.values() if isinstance(v, dict)]
    return max(scan_times) if scan_times else None

# ── Главное меню (кнопки) ─────────────────────────────────
def main_menu():
    buttons = [
        [InlineKeyboardButton("📋 Все слёты",          callback_data="action_list")],
        [InlineKeyboardButton("⚠️ Ближайшие (3ч)",     callback_data="action_soon")],
        [InlineKeyboardButton("🗺 По серверу",          callback_data="action_servers")],
        [InlineKeyboardButton("🔔 Уведомления слёты",  callback_data="action_notify_menu")],
        [InlineKeyboardButton("🎰 Уведомления лотерея",callback_data="action_lottery_menu")],
    ]
    return InlineKeyboardMarkup(buttons)

# ── Handlers ──────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = "👋 *Arizona Property Tracker*\n\nВыбери действие:"
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu())
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu())

async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    chat_id = query.message.chat_id

    # ── Назад в меню
    if data == "action_menu":
        await query.edit_message_text(
            "👋 *Arizona Property Tracker*\n\nВыбери действие:",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )

    # ── Все слёты
    elif data == "action_list":
        props = get_all_props()
        back  = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="action_menu")]])
        await query.edit_message_text(
            build_list_text(props),
            parse_mode="MarkdownV2",
            reply_markup=back
        )

    # ── Ближайшие 3ч
    elif data == "action_soon":
        props = [p for p in get_all_props() if p["hoursLeft"] <= 3]
        back  = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="action_menu")]])
        await query.edit_message_text(
            build_list_text(props, "⚠️ Слёты в ближайшие 3 часа"),
            parse_mode="MarkdownV2",
            reply_markup=back
        )

    # ── Список серверов
    elif data == "action_servers":
        servers = get_servers_ordered()
        if not servers:
            await query.edit_message_text("Данных пока нет\\.", parse_mode="MarkdownV2")
            return
        # По 2 кнопки в ряд
        buttons = []
        row = []
        for i, s in enumerate(servers):
            row.append(InlineKeyboardButton(s, callback_data=f"srv_{s}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="action_menu")])
        await query.edit_message_text(
            "🗺 *Выбери сервер:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    # ── Конкретный сервер
    elif data.startswith("srv_"):
        server    = data.replace("srv_", "")
        props     = [p for p in get_all_props() if p["server"] == server]
        last_scan = get_last_scan(server)
        scan_str  = format_last_scan(last_scan)

        text = build_list_text(props, f"📋 {server}")
        text += f"\n\n🕐 _Последний скан: {scan_str}_"

        back = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К серверам", callback_data="action_servers")]
        ])
        await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=back)

    # ── Меню уведомлений слёты
    elif data == "action_notify_menu":
        is_sub = chat_id in subscribers
        buttons = [
            [InlineKeyboardButton(
                "🔕 Отписаться" if is_sub else "🔔 Подписаться",
                callback_data="action_notify_toggle"
            )],
            [InlineKeyboardButton("◀️ Назад", callback_data="action_menu")],
        ]
        status = "✅ Подписан" if is_sub else "❌ Не подписан"
        await query.edit_message_text(
            f"🔔 *Уведомления о слётах*\n\nСтатус: {status}\n"
            f"Предупреждаю за {NOTIFY_HOURS}ч до слёта\\.",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data == "action_notify_toggle":
        if chat_id in subscribers:
            subscribers.discard(chat_id)
            status = "❌ Не подписан"
            btn    = "🔔 Подписаться"
        else:
            subscribers.add(chat_id)
            status = "✅ Подписан"
            btn    = "🔕 Отписаться"
        buttons = [
            [InlineKeyboardButton(btn, callback_data="action_notify_toggle")],
            [InlineKeyboardButton("◀️ Назад", callback_data="action_menu")],
        ]
        await query.edit_message_text(
            f"🔔 *Уведомления о слётах*\n\nСтатус: {status}\n"
            f"Предупреждаю за {NOTIFY_HOURS}ч до слёта\\.",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    # ── Меню уведомлений лотерея
    elif data == "action_lottery_menu":
        is_sub = chat_id in lottery_subscribers
        buttons = [
            [InlineKeyboardButton(
                "🔕 Отписаться" if is_sub else "🔔 Подписаться",
                callback_data="action_lottery_toggle"
            )],
            [InlineKeyboardButton("◀️ Назад", callback_data="action_menu")],
        ]
        status = "✅ Подписан" if is_sub else "❌ Не подписан"
        await query.edit_message_text(
            f"🎰 *Уведомления о лотерее*\n\nСтатус: {status}\n"
            f"Билеты продаются каждый день в 21:10 МСК\\.\n"
            f"Уведомление приходит в 21:05 МСК\\.",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data == "action_lottery_toggle":
        if chat_id in lottery_subscribers:
            lottery_subscribers.discard(chat_id)
            status = "❌ Не подписан"
            btn    = "🔔 Подписаться"
        else:
            lottery_subscribers.add(chat_id)
            status = "✅ Подписан"
            btn    = "🔕 Отписаться"
        buttons = [
            [InlineKeyboardButton(btn, callback_data="action_lottery_toggle")],
            [InlineKeyboardButton("◀️ Назад", callback_data="action_menu")],
        ]
        await query.edit_message_text(
            f"🎰 *Уведомления о лотерее*\n\nСтатус: {status}\n"
            f"Билеты продаются каждый день в 21:10 МСК\\.\n"
            f"Уведомление приходит в 21:05 МСК\\.",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

# ── Фоновые задачи ────────────────────────────────────────
async def notify_loop(app):
    """Проверяет слёты и шлёт уведомления."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        props = get_all_props()
        for p in props:
            if p["hoursLeft"] <= NOTIFY_HOURS:
                key = f"{p['server']}_{p['propType']}_{p['expiryTs']}"
                if key not in notified:
                    notified.add(key)
                    text = (
                        f"⚠️ *Скоро слёт\\!*\n"
                        f"Сервер: *{p['server']}*\n"
                        f"Тип: {prop_type_ru(p['propType'])}\n"
                        f"Слетит в {format_time_msk(p['expiryTs'])} МСК "
                        f"\\(через {p['hoursLeft']}ч\\)"
                    )
                    for chat_id in subscribers:
                        try:
                            await app.bot.send_message(chat_id, text, parse_mode="MarkdownV2")
                        except Exception:
                            pass

async def lottery_loop(app):
    """Шлёт уведомление о лотерее в 21:05 МСК каждый день."""
    global lottery_notified
    while True:
        await asyncio.sleep(30)
        now_msk = datetime.now(tz=MSK)
        # 21:05 МСК
        if now_msk.hour == 21 and now_msk.minute == 5:
            if not lottery_notified:
                lottery_notified = True
                for chat_id in lottery_subscribers:
                    try:
                        await app.bot.send_message(
                            chat_id,
                            "🎰 *Билеты через 5 минут\\!*\nЛотерея начнётся в 21:10 МСК\\.",
                            parse_mode="MarkdownV2"
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

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(notify_loop(app))
    loop.create_task(lottery_loop(app))

    print("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
