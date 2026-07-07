import asyncio, time, os, json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
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

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ADMIN_ID       = 1180660765
CHECK_INTERVAL = 60
MSK            = timezone(timedelta(hours=3))
STALE_HOURS    = 8
PAGE_SIZE      = 10
RENDER_URL     = os.environ.get("RENDER_URL", "https://arizona-tracker.onrender.com")
HISTORY_HOURS  = 5
DELETE_AFTER   = 86400

SERVER_ORDER = [
    "Phoenix", "Tucson", "Scottdale", "Chandler", "Brainburg", "Saint-Rose",
    "Mesa", "Red-Rock", "Yuma", "Surprise", "Prescott", "Glendale",
    "Kingman", "Winslow", "Payson", "Gilbert", "Show Low", "Casa-Grande",
    "Page", "Sun-City", "Queen-Creek", "Sedona", "Holiday", "Wednesday",
    "Yava", "Faraway", "Bumble Bee", "Christmas", "Love", "Mirage",
    "Drake", "Space",
]

# ── Сезоны ────────────────────────────────────────────────
SEASON_NAMES = {
    1: ("По инфе",     "📱"),
    2: ("Скорострелы", "⌨️"),
    3: ("Автогонки",   "🚗"),
    4: ("По новому",   "✈️"),
    5: ("Мотогонки",   "🏍"),
}

SEASON_TABLES = [
    [1,3,4,3,1,1,5,4,1,4,3,4,2,5,5,2,2,2,3,5,2,3,1,4,4,4,2,4,4,4,4,2],
    [2,4,5,4,2,2,1,5,2,5,4,5,3,1,1,3,3,3,4,1,3,4,2,5,5,5,3,5,5,5,5,3],
    [3,5,1,5,3,3,2,1,3,1,5,1,4,2,2,4,4,4,5,2,4,5,3,1,1,1,4,1,1,1,1,4],
    [4,1,2,1,4,4,3,2,4,2,1,2,5,3,3,5,5,5,1,3,5,1,4,2,2,2,5,2,2,2,2,5],
    [5,2,3,2,5,5,4,3,5,3,2,3,1,4,4,1,1,1,2,4,1,2,5,3,3,3,1,3,3,3,3,1],
]

# Неделя 3 началась 6 июля 2026 в 06:05 МСК → неделя 1 = 22 июня 2026
SEASON_EPOCH = datetime(2026, 6, 22, 6, 5, 0, tzinfo=MSK)

def get_current_week_index():
    now   = datetime.now(tz=MSK)
    delta = now - SEASON_EPOCH
    weeks = int(delta.total_seconds() // (7 * 86400))
    return weeks % 5

def get_season(server_index):
    week_idx   = get_current_week_index()
    season_num = SEASON_TABLES[week_idx][server_index]
    return SEASON_NAMES[season_num]

def get_season_by_name(server_name):
    if server_name in SERVER_ORDER:
        return get_season(SERVER_ORDER.index(server_name))
    return ("", "")

def get_next_season_change():
    now     = datetime.now(tz=MSK)
    delta   = now - SEASON_EPOCH
    weeks   = int(delta.total_seconds() // (7 * 86400))
    return SEASON_EPOCH + timedelta(weeks=weeks + 1)

NOTIFY_OPTIONS = [60, 50, 40, 30, 20, 10, 5]

user_notify_minutes = {}
lottery_notify_mins = {}
subscribers         = set()
lottery_subscribers = set()
notified            = set()
sent_notifications  = defaultdict(list)
all_users           = set()

# ── Firebase helpers ──────────────────────────────────────
def load_users():
    ref  = db.reference("users")
    data = ref.get() or {}
    return set(str(k) for k in data.keys())

def save_user(chat_id):
    db.reference(f"users/{chat_id}").set(True)

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
                "minsLeft":  int((expiry - now) / 60),
                "scanTs":    v.get("scanTs", 0),
            })
    result.sort(key=lambda x: x["expiryTs"])
    return result

def get_history():
    now   = int(time.time())
    since = now - HISTORY_HOURS * 3600
    ref   = db.reference("history")
    data  = ref.get() or {}
    result = []
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        expired_at = v.get("expiryTs", 0)
        if since <= expired_at <= now:
            result.append(v)
    result.sort(key=lambda x: x.get("expiryTs", 0), reverse=True)
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
    return datetime.fromtimestamp(ts, tz=MSK).strftime("%d.%m %H:%M МСК")

def prop_emoji(pt):
    if pt == "house":    return "🏠"
    if pt == "business": return "🏢"
    return "❓"

def is_stale(scan_ts):
    return not scan_ts or (time.time() - scan_ts) > STALE_HOURS * 3600

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
    times = [v.get("scanTs", 0) for v in data.values() if isinstance(v, dict)]
    return max(times) if times else None

def get_server_counts(server):
    now    = int(time.time())
    ref    = db.reference(f"properties/{server}")
    data   = ref.get() or {}
    counts = defaultdict(int)
    for v in data.values():
        if isinstance(v, dict) and v.get("expiryTs", 0) > now:
            counts[v.get("propType", "?")] += 1
    return counts

# ── Форматирование списка ─────────────────────────────────
def build_list_text(props, title="📋 Актуальные слёты", page=0):
    if not props:
        return "✅ Слётов нет или данных пока нет.", 0

    # Группируем: (server, expiryTs, propType) -> count
    group = defaultdict(int)
    for p in props:
        group[(p["server"], p["expiryTs"], p["propType"])] += 1

    # Считаем уникальные события слёта (не записи в БД)
    house_events = set((p["server"], p["expiryTs"]) for p in props if p["propType"] == "house")
    biz_events   = set((p["server"], p["expiryTs"]) for p in props if p["propType"] == "business")
    house_total  = sum(group[(srv, ts, "house")] for srv, ts in house_events)
    biz_total    = sum(group[(srv, ts, "business")] for srv, ts in biz_events)

    seen_keys = set()
    unique = []
    for p in props:
        k = (p["server"], p["expiryTs"], p["propType"])
        if k not in seen_keys:
            seen_keys.add(k)
            unique.append(p)

    total_pages = max(1, (len(unique) + PAGE_SIZE - 1) // PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))
    chunk       = unique[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [f"*{title}*"]
    stats = []
    if house_total: stats.append(f"🏠×{house_total}")
    if biz_total:   stats.append(f"🏢×{biz_total}")
    if stats: lines.append(" ".join(stats))
    if total_pages > 1:
        lines.append(f"_Страница {page + 1} из {total_pages}_")
    lines.append("")

    for p in chunk:
        bar    = "🔴" if p["hoursLeft"] <= 1 else "🟡" if p["hoursLeft"] <= 3 else "🟢"
        pd_str = f" {p['pd']}pd" if p.get("pd") else ""
        cnt    = group[(p["server"], p["expiryTs"], p["propType"])]
        emoji  = prop_emoji(p["propType"])
        cnt_str = f"{emoji}×{cnt}" if cnt > 1 else emoji
        _, s_emoji = get_season_by_name(p["server"])
        season_str = f" ({s_emoji})" if s_emoji else ""
        lines.append(
            f"{bar} *{p['server']}*{season_str} {cnt_str}{pd_str}\n"
            f"    ⏰ {format_time_msk(p['expiryTs'])} МСК (через {p['hoursLeft']}ч)"
        )
    return "\n".join(lines), total_pages

# ── Клавиатура ────────────────────────────────────────────
def permanent_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 Все слёты"),    KeyboardButton("⚠️ Ближайшие")],
        [KeyboardButton("🗺 По серверу"),   KeyboardButton("👤 Профиль")],
        [KeyboardButton("🔔 Уведомления"),  KeyboardButton("🎰 Лотерея")],
        [KeyboardButton("📜 История"),      KeyboardButton("🏆 Сезоны")],
        [KeyboardButton("ℹ️ О боте")],
    ], resize_keyboard=True, is_persistent=True)

def _page_buttons(page, total, prefix):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}_page_{page-1}"))
    row.append(InlineKeyboardButton("🔄", callback_data=f"{prefix}_page_{page}"))
    if page < total - 1:
        row.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}_page_{page+1}"))
    return [row] if row else []

# ── /start ────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    all_users.add(update.effective_chat.id)
    save_user(update.effective_chat.id)
    await update.message.reply_text(
        "🏙 *Arizona Property Tracker*\n\n"
        "Следи за слётами домов и бизнесов на всех серверах Arizona RP — "
        "в реальном времени, без лишних слов.\n\n"
        "📡 Данные поступают от игроков с установленным скриптом.\n"
        "🔔 Настрой уведомления и не пропусти нужный объект.\n\n"
        "👨‍💻 @hiroto",
        parse_mode="Markdown",
        reply_markup=permanent_keyboard()
    )

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    text = " ".join(ctx.args)
    if not text:
        await update.message.reply_text("Использование:\n/broadcast Текст")
        return
    sent, failed = 0, 0
    for chat_id in list(all_users):
        try:
            await ctx.bot.send_message(int(chat_id), text, parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Отправлено: {sent}\n❌ Не доставлено: {failed}")

# ── Текстовые кнопки ──────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    all_users.add(update.effective_chat.id)
    save_user(update.effective_chat.id)
    t = update.message.text
    if t == "📋 Все слёты":     await show_list(update, ctx)
    elif t == "⚠️ Ближайшие":  await show_soon(update, ctx)
    elif t == "🗺 По серверу":  await show_servers(update, ctx)
    elif t == "👤 Профиль":     await show_profile(update, ctx)
    elif t == "🔔 Уведомления": await show_notify_menu(update, ctx)
    elif t == "🎰 Лотерея":     await show_lottery_menu(update, ctx)
    elif t == "📜 История":     await show_history(update, ctx)
    elif t == "🏆 Сезоны":      await show_seasons(update, ctx)
    elif t == "ℹ️ О боте":      await show_about(update, ctx)

async def show_list(update, ctx, page=0):
    props = get_all_props()
    text, total = build_list_text(props, page=page)
    btns = _page_buttons(page, total, "list")
    kb   = InlineKeyboardMarkup(btns) if btns else None
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def show_soon(update, ctx, page=0):
    props = [p for p in get_all_props() if p["hoursLeft"] <= 3]
    text, total = build_list_text(props, "⚠️ Слёты в ближайшие 3 часа", page=page)
    btns = _page_buttons(page, total, "soon")
    kb   = InlineKeyboardMarkup(btns) if btns else None
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def show_servers(update, ctx):
    servers = get_servers_ordered()
    if not servers:
        txt = "Данных пока нет."
        if update.message: await update.message.reply_text(txt)
        else: await update.callback_query.edit_message_text(txt)
        return
    buttons, row = [], []
    for s in servers:
        icon   = "🔴" if is_stale(get_last_scan(s)) else "🟢"
        counts = get_server_counts(s)
        parts  = []
        if counts["house"]:    parts.append(f"🏠×{counts['house']}")
        if counts["business"]: parts.append(f"🏢×{counts['business']}")
        cnt_str    = " " + " ".join(parts) if parts else ""
        _, s_emoji = get_season_by_name(s)
        row.append(InlineKeyboardButton(f"{icon} {s}{cnt_str} {s_emoji}", callback_data=f"srv_{s}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    text = "🗺 *Выбери сервер:*\n🟢 свежие  🔴 устаревшие"
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_seasons(update, ctx):
    week_idx    = get_current_week_index()
    week_num    = week_idx + 1
    next_change = get_next_season_change()
    next_str    = next_change.strftime("%d.%m в %H:%M МСК")

    lines = [
        f"🏆 *Сезоны — Неделя {week_num}*",
        f"_Следующая смена: {next_str}_\n",
    ]
    for i, srv in enumerate(SERVER_ORDER):
        season_name, season_emoji = get_season(i)
        num = str(i + 1).zfill(2)
        lines.append(f"{num} - {season_emoji} {season_name}")

    text = "\n".join(lines)
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")

async def show_profile(update, ctx):
    chat_id    = update.effective_chat.id
    is_sub     = chat_id in subscribers
    is_lot     = chat_id in lottery_subscribers
    selected   = user_notify_minutes.get(chat_id, set())
    lot_sel    = lottery_notify_mins.get(chat_id, set())
    notify_str = ", ".join(f"{m}м" for m in sorted(selected)) if selected else "не настроено"
    lot_str    = ", ".join(f"{m}м" for m in sorted(lot_sel)) if lot_sel else "не настроено"
    text = (
        f"👤 *Профиль*\n\n"
        f"🔔 Уведомления слётов: {'✅ Вкл' if is_sub else '❌ Выкл'}\n"
        f"⏱ Предупреждать за: {notify_str}\n\n"
        f"🎰 Уведомления лотерея: {'✅ Вкл' if is_lot else '❌ Выкл'}\n"
        f"⏱ За: {lot_str}"
    )
    buttons = [
        [InlineKeyboardButton("🔔 Настроить уведомления", callback_data="open_notify")],
        [InlineKeyboardButton("🎰 Настроить лотерею",     callback_data="open_lottery")],
    ]
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_notify_menu(update, ctx):
    chat_id  = update.effective_chat.id
    is_sub   = chat_id in subscribers
    selected = user_notify_minutes.get(chat_id, set())
    status   = "✅ Подписан" if is_sub else "❌ Не подписан"
    btn_text = "🔕 Отписаться" if is_sub else "🔔 Подписаться"
    sel_str  = ", ".join(f"{m}м" for m in sorted(selected)) if selected else "не выбрано"
    time_buttons, row = [], []
    for m in NOTIFY_OPTIONS:
        mark = "✓ " if m in selected else ""
        row.append(InlineKeyboardButton(f"{mark}{m}м", callback_data=f"notify_min_{m}"))
        if len(row) == 4:
            time_buttons.append(row); row = []
    if row: time_buttons.append(row)
    buttons = [
        [InlineKeyboardButton(btn_text, callback_data="action_notify_toggle")],
        *time_buttons,
    ]
    text = f"🔔 *Уведомления о слётах*\n\nСтатус: {status}\nПредупреждать за: *{sel_str}*"
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_lottery_menu(update, ctx):
    chat_id  = update.effective_chat.id
    is_sub   = chat_id in lottery_subscribers
    selected = lottery_notify_mins.get(chat_id, set())
    status   = "✅ Подписан" if is_sub else "❌ Не подписан"
    btn_text = "🔕 Отписаться" if is_sub else "🔔 Подписаться"
    sel_str  = ", ".join(f"{m}м" for m in sorted(selected)) if selected else "не выбрано"
    buttons  = [
        [InlineKeyboardButton(btn_text, callback_data="action_lottery_toggle")],
        [
            InlineKeyboardButton(("✓ " if 10 in selected else "") + "10м", callback_data="lottery_min_10"),
            InlineKeyboardButton(("✓ " if 5  in selected else "") + "5м",  callback_data="lottery_min_5"),
        ],
    ]
    text = (
        f"🎰 *Уведомления о лотерее*\n\n"
        f"Статус: {status}\n"
        f"Билеты в 21:10 МСК\n"
        f"Уведомлять за: *{sel_str}*"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_history(update, ctx):
    history = get_history()
    if not history:
        txt = f"📜 *История слётов*\n\nЗа последние {HISTORY_HOURS} часов слётов не было."
        if update.message: await update.message.reply_text(txt, parse_mode="Markdown")
        else: await update.callback_query.edit_message_text(txt, parse_mode="Markdown")
        return
    lines = [f"📜 *История слётов (последние {HISTORY_HOURS}ч)*\n"]
    for v in history[:20]:
        emoji = prop_emoji(v.get("propType", "?"))
        lines.append(
            f"🔘 *{v.get('server','?')}* {emoji}\n"
            f"    🕐 {format_time_msk(v.get('expiryTs', 0))} МСК"
        )
    text = "\n".join(lines)
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")

async def show_about(update, ctx):
    total_users = len(all_users)
    await update.message.reply_text(
        f"ℹ️ *Arizona Property Tracker*\n\n"
        f"Бот отслеживает слёты домов и бизнесов на серверах Arizona RP.\n\n"
        f"📡 Данные собираются автоматически от игроков с Lua скриптом.\n"
        f"🕐 Время отображается по МСК (UTC+3).\n"
        f"⚠️ Данные устаревают через {STALE_HOURS}ч без скана.\n\n"
        f"👥 Пользователей: *{total_users}*\n\n"
        f"👨‍💻 Создатель: @hiroto",
        parse_mode="Markdown"
    )

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
    elif data == "open_notify":
        await show_notify_menu(update, ctx)
    elif data == "open_lottery":
        await show_lottery_menu(update, ctx)

    elif data.startswith("srv_"):
        server    = data.replace("srv_", "")
        props     = [p for p in get_all_props() if p["server"] == server]
        last_scan = get_last_scan(server)
        scan_str  = format_last_scan(last_scan)
        warn      = "⚠️ Данные устарели (скан > 8ч назад)\n\n" if is_stale(last_scan) else ""
        counts    = get_server_counts(server)
        parts     = []
        if counts["house"]:    parts.append(f"🏠×{counts['house']}")
        if counts["business"]: parts.append(f"🏢×{counts['business']}")
        stats_str          = " ".join(parts)
        season_name, s_emoji = get_season_by_name(server)
        season_str         = f"{s_emoji} {season_name}" if season_name else ""
        text, _            = build_list_text(props, f"📋 {server}  {stats_str}", page=0)
        text               = warn + text + f"\n\n🏆 Сезон: {season_str}\n🕐 _Последний скан: {scan_str}_"
        buttons            = [[InlineKeyboardButton("◀️ К серверам", callback_data="action_servers")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "action_notify_toggle":
        if chat_id in subscribers: subscribers.discard(chat_id)
        else: subscribers.add(chat_id)
        await show_notify_menu(update, ctx)

    elif data.startswith("notify_min_"):
        m = int(data.split("_")[-1])
        s = user_notify_minutes.setdefault(chat_id, set())
        if m in s: s.discard(m)
        else: s.add(m)
        await show_notify_menu(update, ctx)

    elif data == "action_lottery_toggle":
        if chat_id in lottery_subscribers: lottery_subscribers.discard(chat_id)
        else: lottery_subscribers.add(chat_id)
        await show_lottery_menu(update, ctx)

    elif data.startswith("lottery_min_"):
        m = int(data.split("_")[-1])
        s = lottery_notify_mins.setdefault(chat_id, set())
        if m in s: s.discard(m)
        else: s.add(m)
        await show_lottery_menu(update, ctx)

# ── Фоновые задачи ────────────────────────────────────────
async def ping_loop():
    import httpx
    await asyncio.sleep(60)
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await client.get(f"{RENDER_URL}/ping", timeout=10)
        except Exception:
            pass
        await asyncio.sleep(600)

async def delete_old_notifications(app):
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        for chat_id, msgs in list(sent_notifications.items()):
            for msg_id, ts in msgs:
                if now - ts > DELETE_AFTER:
                    try:
                        await app.bot.delete_message(chat_id, msg_id)
                    except Exception:
                        pass
            sent_notifications[chat_id] = [(m, t) for m, t in msgs if now - t <= DELETE_AFTER]

async def save_history(props_before):
    now       = int(time.time())
    props_now = {(p["server"], p["propType"], p["expiryTs"]) for p in get_all_props()}
    ref       = db.reference("history")
    for p in props_before:
        key = (p["server"], p["propType"], p["expiryTs"])
        if key not in props_now and p["expiryTs"] <= now:
            hist_key = f"{p['server']}_{p['propType']}_{p['expiryTs']}"
            ref.child(hist_key).set({
                "server":   p["server"],
                "propType": p["propType"],
                "expiryTs": p["expiryTs"],
                "pd":       p.get("pd", 0),
            })

async def notify_loop(app):
    prev_props = get_all_props()
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        props = get_all_props()
        await save_history(prev_props)
        prev_props = props

        group = defaultdict(int)
        for p in props:
            group[(p["server"], p["expiryTs"], p["propType"])] += 1

        for p in props:
            for chat_id in list(subscribers):
                selected = user_notify_minutes.get(chat_id, set())
                for mins in selected:
                    if p["minsLeft"] <= mins:
                        key = f"{chat_id}_{p['server']}_{p['propType']}_{p['expiryTs']}_{mins}"
                        if key not in notified:
                            notified.add(key)
                            cnt        = group[(p["server"], p["expiryTs"], p["propType"])]
                            emoji      = prop_emoji(p["propType"])
                            cnt_str    = f"{emoji}×{cnt}" if cnt > 1 else emoji
                            _, s_emoji = get_season_by_name(p["server"])
                            text = (
                                f"⚠️ *Скоро слёт!*\n"
                                f"Сервер: *{p['server']}* {s_emoji}\n"
                                f"{cnt_str} {p['pd']}pd — {format_time_msk(p['expiryTs'])} МСК\n"
                                f"Через {p['minsLeft']} мин"
                            )
                            try:
                                msg = await app.bot.send_message(chat_id, text, parse_mode="Markdown")
                                sent_notifications[chat_id].append((msg.message_id, time.time()))
                            except Exception:
                                pass

async def lottery_loop(app):
    while True:
        await asyncio.sleep(30)
        now_msk = datetime.now(tz=MSK)
        for chat_id in list(lottery_subscribers):
            selected = lottery_notify_mins.get(chat_id, set())
            for mins in selected:
                notify_hour   = 21
                notify_minute = 10 - mins
                if notify_minute < 0:
                    notify_hour  -= 1
                    notify_minute += 60
                if now_msk.hour == notify_hour and now_msk.minute == notify_minute:
                    key = f"lottery_{chat_id}_{mins}_{now_msk.strftime('%d.%m')}"
                    if key not in notified:
                        notified.add(key)
                        try:
                            msg = await app.bot.send_message(
                                chat_id,
                                f"🎰 *Билеты через {mins} минут!*\nЛотерея начнётся в 21:10 МСК.",
                                parse_mode="Markdown"
                            )
                            sent_notifications[chat_id].append((msg.message_id, time.time()))
                        except Exception:
                            pass

async def cleanup_history():
    while True:
        await asyncio.sleep(3600)
        now   = int(time.time())
        since = now - HISTORY_HOURS * 2 * 3600
        ref   = db.reference("history")
        data  = ref.get() or {}
        for k, v in data.items():
            if isinstance(v, dict) and v.get("expiryTs", 0) < since:
                ref.child(k).delete()

# ── Запуск ────────────────────────────────────────────────
def main():
    global all_users
    all_users = load_users()
    print(f"Загружено пользователей: {len(all_users)}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(notify_loop(app))
    loop.create_task(lottery_loop(app))
    loop.create_task(ping_loop())
    loop.create_task(delete_old_notifications(app))
    loop.create_task(cleanup_history())

    print("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()