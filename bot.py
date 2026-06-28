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
CHECK_INTERVAL = 60
MSK            = timezone(timedelta(hours=3))
STALE_HOURS    = 8
PAGE_SIZE      = 10
RENDER_URL     = os.environ.get("RENDER_URL", "https://arizona-tracker.onrender.com")
HISTORY_HOURS  = 5    # история слётов за 5 часов
DELETE_AFTER   = 86400  # удалять уведомления через 24 часа

SERVER_ORDER = [
    "Phoenix", "Tucson", "Scottdale", "Chandler", "Brainburg", "Saint-Rose",
    "Mesa", "Red-Rock", "Yuma", "Surprise", "Prescott", "Glendale",
    "Kingman", "Winslow", "Payson", "Gilbert", "Show Low", "Casa-Grande",
    "Page", "Sun-City", "Queen-Creek", "Sedona", "Holiday", "Wednesday",
    "Yava", "Faraway", "Bumble Bee", "Christmas", "Love", "Mirage",
    "Drake", "Space",
]

NOTIFY_OPTIONS  = [60, 50, 40, 30, 20, 10, 5]
LOTTERY_OPTIONS = [10, 5]

# ── Состояние пользователей ───────────────────────────────
user_notify_minutes = {}   # { chat_id: set(minutes) }
lottery_notify_mins = {}   # { chat_id: set(minutes) }
subscribers         = set()
lottery_subscribers = set()
favorite_servers    = {}   # { chat_id: set(server_names) }
notified            = set()
# { chat_id: [(message_id, send_ts)] }
sent_notifications  = defaultdict(list)
all_users           = set()   # все кто писал боту

# ── Firebase helpers ──────────────────────────────────────
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
    """Слёты которые произошли за последние HISTORY_HOURS часов."""
    now      = int(time.time())
    since    = now - HISTORY_HOURS * 3600
    ref      = db.reference("history")
    data     = ref.get() or {}
    result   = []
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

def prop_type_ru(pt):
    if pt == "house":    return "Дом"
    if pt == "business": return "Бизнес"
    return pt

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
    """Возвращает {propType: count} для сервера."""
    now  = int(time.time())
    ref  = db.reference(f"properties/{server}")
    data = ref.get() or {}
    counts = defaultdict(int)
    for v in data.values():
        if not isinstance(v, dict):
            continue
        if v.get("expiryTs", 0) > now:
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

    houses = sum(1 for p in props if p["propType"] == "house")
    bizs   = sum(1 for p in props if p["propType"] == "business")

    # Дедуплицируем для пагинации
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
    if houses: stats.append(f"🏠 {houses}")
    if bizs:   stats.append(f"🏢 {bizs}")
    if stats:  lines.append(" | ".join(stats))
    if total_pages > 1:
        lines.append(f"_Страница {page + 1} из {total_pages}_")
    lines.append("")

    for p in chunk:
        bar     = "🔴" if p["hoursLeft"] <= 1 else "🟡" if p["hoursLeft"] <= 3 else "🟢"
        pd_str  = f" {p['pd']}pd" if p.get("pd") else ""
        cnt     = group[(p["server"], p["expiryTs"], p["propType"])]
        type_ru = prop_type_ru(p["propType"])
        cnt_str = f" ({cnt} {type_ru.lower()}{'а' if 2 <= cnt <= 4 else 'ов' if cnt > 4 else ''})" if cnt > 1 else f" ({type_ru})"
        lines.append(
            f"{bar} *{p['server']}*{cnt_str}{pd_str}\n"
            f"    ⏰ {format_time_msk(p['expiryTs'])} МСК (через {p['hoursLeft']}ч)"
        )
    return "\n".join(lines), total_pages

# ── Клавиатура ────────────────────────────────────────────
def permanent_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 Все слёты"),    KeyboardButton("⚠️ Ближайшие")],
        [KeyboardButton("🗺 По серверу"),   KeyboardButton("⭐️ Избранное")],
        [KeyboardButton("🔔 Уведомления"),  KeyboardButton("🎰 Лотерея")],
        [KeyboardButton("📜 История"),      KeyboardButton("ℹ️ О боте")],
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
    await update.message.reply_text(
        "👋 *Arizona Property Tracker*\n\nИспользуй кнопки внизу экрана.\n\n👨‍💻 Создатель: @hiroto",
        parse_mode="Markdown",
        reply_markup=permanent_keyboard()
    )

# ── Текстовые кнопки ──────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    all_users.add(update.effective_chat.id)
    t = update.message.text
    if t == "📋 Все слёты":     await show_list(update, ctx)
    elif t == "⚠️ Ближайшие":  await show_soon(update, ctx)
    elif t == "🗺 По серверу":  await show_servers(update, ctx)
    elif t == "⭐️ Избранное":  await show_favorites(update, ctx)
    elif t == "🔔 Уведомления": await show_notify_menu(update, ctx)
    elif t == "🎰 Лотерея":     await show_lottery_menu(update, ctx)
    elif t == "📜 История":     await show_history(update, ctx)
    elif t == "ℹ️ О боте":      await show_about(update, ctx)

# ── Показ списков ─────────────────────────────────────────
async def show_list(update, ctx, page=0):
    props = get_all_props()
    text, total = build_list_text(props, page=page)
    btns = _page_buttons(page, total, "list")
    btns.append([InlineKeyboardButton("📤 Поделиться", callback_data="share_all")])
    kb = InlineKeyboardMarkup(btns)
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def show_soon(update, ctx, page=0):
    props = [p for p in get_all_props() if p["hoursLeft"] <= 3]
    text, total = build_list_text(props, "⚠️ Слёты в ближайшие 3 часа", page=page)
    btns = _page_buttons(page, total, "soon")
    btns.append([InlineKeyboardButton("📤 Поделиться", callback_data="share_soon")])
    kb = InlineKeyboardMarkup(btns)
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
        if counts["house"]:    parts.append(f"🏠{counts['house']}")
        if counts["business"]: parts.append(f"🏢{counts['business']}")
        cnt_str = " " + " ".join(parts) if parts else ""
        row.append(InlineKeyboardButton(f"{icon} {s}{cnt_str}", callback_data=f"srv_{s}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    text = "🗺 *Выбери сервер:*\n🟢 свежие  🔴 устаревшие"
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_favorites(update, ctx, page=0):
    chat_id = update.effective_chat.id
    favs    = favorite_servers.get(chat_id, set())

    if not favs:
        servers = get_servers_ordered()
        buttons, row = [], []
        for s in servers:
            row.append(InlineKeyboardButton(f"+ {s}", callback_data=f"fav_add_{s}"))
            if len(row) == 2:
                buttons.append(row); row = []
        if row: buttons.append(row)
        text = "⭐️ *Избранное пусто*\n\nВыбери серверы которые хочешь отслеживать:"
        if update.message:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return

    props = [p for p in get_all_props() if p["server"] in favs]
    text, total = build_list_text(props, "⭐️ Избранные серверы", page=page)
    btns = _page_buttons(page, total, "fav")
    btns.append([InlineKeyboardButton("✏️ Изменить избранное", callback_data="fav_edit")])
    kb = InlineKeyboardMarkup(btns)
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

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

    buttons = [
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
        if update.message:
            await update.message.reply_text(txt, parse_mode="Markdown")
        else:
            await update.callback_query.edit_message_text(txt, parse_mode="Markdown")
        return

    lines = [f"📜 *История слётов (последние {HISTORY_HOURS}ч)*\n"]
    for v in history[:20]:
        lines.append(
            f"🔘 *{v.get('server','?')}* — {prop_type_ru(v.get('propType','?'))}\n"
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
        f"⚠️ Данные считаются устаревшими через {STALE_HOURS} часов без скана.\n\n"
        f"👥 Пользователей бота: *{total_users}*\n\n"
        f"👨‍💻 Создатель: @hiroto",
        parse_mode="Markdown"
    )

# ── Личный кабинет ────────────────────────────────────────
async def show_profile(update, ctx):
    chat_id  = update.effective_chat.id
    is_sub   = chat_id in subscribers
    is_lot   = chat_id in lottery_subscribers
    selected = user_notify_minutes.get(chat_id, set())
    lot_sel  = lottery_notify_mins.get(chat_id, set())
    favs     = favorite_servers.get(chat_id, set())

    notify_str = ", ".join(f"{m}м" for m in sorted(selected)) if selected else "не настроено"
    lot_str    = ", ".join(f"{m}м" for m in sorted(lot_sel)) if lot_sel else "не настроено"
    fav_str    = ", ".join(sorted(favs)) if favs else "не выбраны"

    text = (
        f"👤 *Личный кабинет*\n\n"
        f"🔔 Уведомления слётов: {'✅' if is_sub else '❌'}\n"
        f"⏱ Предупреждать за: {notify_str}\n\n"
        f"🎰 Уведомления лотерея: {'✅' if is_lot else '❌'}\n"
        f"⏱ За: {lot_str}\n\n"
        f"⭐️ Избранные серверы:\n{fav_str}"
    )
    buttons = [[InlineKeyboardButton("⭐️ Изменить избранное", callback_data="fav_edit")]]
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

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

    elif data.startswith("fav_page_"):
        await show_favorites(update, ctx, page=int(data.split("_")[-1]))

    elif data == "action_servers":
        await show_servers(update, ctx)

    elif data.startswith("srv_"):
        server    = data.replace("srv_", "")
        props     = [p for p in get_all_props() if p["server"] == server]
        last_scan = get_last_scan(server)
        scan_str  = format_last_scan(last_scan)
        warn      = "⚠️ Данные устарели (скан > 8ч назад)\n\n" if is_stale(last_scan) else ""
        counts    = get_server_counts(server)
        parts     = []
        if counts["house"]:    parts.append(f"🏠 {counts['house']}")
        if counts["business"]: parts.append(f"🏢 {counts['business']}")
        stats_str = "  ".join(parts)
        text, _   = build_list_text(props, f"📋 {server}  {stats_str}", page=0)
        text      = warn + text + f"\n\n🕐 _Последний скан: {scan_str}_"

        is_fav  = server in favorite_servers.get(chat_id, set())
        fav_btn = "⭐️ Убрать из избранного" if is_fav else "☆ Добавить в избранное"
        buttons = [
            [InlineKeyboardButton(fav_btn, callback_data=f"fav_toggle_{server}")],
            [InlineKeyboardButton("📤 Поделиться", callback_data=f"share_srv_{server}")],
            [InlineKeyboardButton("◀️ К серверам", callback_data="action_servers")],
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    # ── Избранное
    elif data == "fav_edit":
        servers  = get_servers_ordered()
        favs     = favorite_servers.get(chat_id, set())
        buttons, row = [], []
        for s in servers:
            mark = "⭐️ " if s in favs else ""
            row.append(InlineKeyboardButton(f"{mark}{s}", callback_data=f"fav_toggle_{s}"))
            if len(row) == 2:
                buttons.append(row); row = []
        if row: buttons.append(row)
        buttons.append([InlineKeyboardButton("✅ Готово", callback_data="fav_done")])
        await query.edit_message_text(
            "⭐️ *Избранные серверы*\n\nНажми чтобы добавить/убрать:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("fav_toggle_"):
        server = data.replace("fav_toggle_", "")
        favs   = favorite_servers.setdefault(chat_id, set())
        if server in favs: favs.discard(server)
        else: favs.add(server)
        # Обновляем список серверов
        servers  = get_servers_ordered()
        buttons, row = [], []
        for s in servers:
            mark = "⭐️ " if s in favorite_servers.get(chat_id, set()) else ""
            row.append(InlineKeyboardButton(f"{mark}{s}", callback_data=f"fav_toggle_{s}"))
            if len(row) == 2:
                buttons.append(row); row = []
        if row: buttons.append(row)
        buttons.append([InlineKeyboardButton("✅ Готово", callback_data="fav_done")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "fav_add":
        await show_favorites(update, ctx)

    elif data == "fav_done":
        await show_favorites(update, ctx)

    # ── Поделиться
    elif data == "share_all":
        props = get_all_props()
        text, _ = build_list_text(props, "📋 Актуальные слёты Arizona RP")
        await ctx.bot.send_message(chat_id, f"📤 Скопируй и отправь:\n\n{text}", parse_mode="Markdown")

    elif data == "share_soon":
        props = [p for p in get_all_props() if p["hoursLeft"] <= 3]
        text, _ = build_list_text(props, "⚠️ Ближайшие слёты Arizona RP")
        await ctx.bot.send_message(chat_id, f"📤 Скопируй и отправь:\n\n{text}", parse_mode="Markdown")

    elif data.startswith("share_srv_"):
        server = data.replace("share_srv_", "")
        props  = [p for p in get_all_props() if p["server"] == server]
        text, _ = build_list_text(props, f"📋 {server} — Arizona RP")
        await ctx.bot.send_message(chat_id, f"📤 Скопируй и отправь:\n\n{text}", parse_mode="Markdown")

    # ── Уведомления
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

    elif data == "show_profile":
        await show_profile(update, ctx)

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
    """Удаляет уведомления старше 24 часов."""
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        for chat_id, msgs in list(sent_notifications.items()):
            to_delete = [(msg_id, ts) for msg_id, ts in msgs if now - ts > DELETE_AFTER]
            for msg_id, ts in to_delete:
                try:
                    await app.bot.delete_message(chat_id, msg_id)
                except Exception:
                    pass
            sent_notifications[chat_id] = [(m, t) for m, t in msgs if now - t <= DELETE_AFTER]

async def save_history(props_before):
    """Сохраняет в Firebase историю слётов."""
    now        = int(time.time())
    props_now  = {(p["server"], p["propType"], p["expiryTs"]) for p in get_all_props()}
    ref        = db.reference("history")
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

        # Сохраняем историю
        await save_history(prev_props)
        prev_props = props

        for p in props:
            for chat_id in list(subscribers):
                selected = user_notify_minutes.get(chat_id, set())
                for mins in selected:
                    if p["minsLeft"] <= mins:
                        key = f"{chat_id}_{p['server']}_{p['propType']}_{p['expiryTs']}_{mins}"
                        if key not in notified:
                            notified.add(key)
                            cnt = sum(
                                1 for x in props
                                if x["server"] == p["server"]
                                and x["propType"] == p["propType"]
                                and x["expiryTs"] == p["expiryTs"]
                            )
                            cnt_str = f" ({cnt} шт)" if cnt > 1 else ""
                            text = (
                                f"⚠️ *Скоро слёт!*\n"
                                f"Сервер: *{p['server']}*\n"
                                f"Тип: {prop_type_ru(p['propType'])}{cnt_str} {p['pd']}pd\n"
                                f"Слетит в {format_time_msk(p['expiryTs'])} МСК "
                                f"(через {p['minsLeft']} мин)"
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
    """Удаляет историю старше HISTORY_HOURS * 2."""
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
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("profile", lambda u, c: show_profile(u, c)))
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