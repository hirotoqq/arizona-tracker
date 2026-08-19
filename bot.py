import asyncio, time, os, json, secrets, string
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
        "databaseURL": "https://kotak-88887-default-rtdb.firebaseio.com"
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
MASS_DROP_MIN  = 4
SCRIPT_PATH    = os.path.join(os.path.dirname(__file__), "property_tracker.luac")
KEY_ALPHABET      = string.ascii_uppercase + string.digits
KEY_GROUP_LEN     = 4
KEY_GROUPS        = 3
EXPIRY_WARN_HOURS = 24  # за сколько часов до истечения подписки напоминать продлить
FUNPAY_URL   = os.environ.get("FUNPAY_URL", "https://funpay.com/users/0000000/")  # TODO: подставь свою ссылку на FunPay
PRICE_WEEK   = 60
PRICE_MONTH  = 200

SERVER_ORDER = [
    "Phoenix", "Tucson", "Scottdale", "Chandler", "Brainburg", "Saint-Rose",
    "Mesa", "Red-Rock", "Yuma", "Surprise", "Prescott", "Glendale",
    "Kingman", "Winslow", "Payson", "Gilbert", "Show Low", "Casa-Grande",
    "Page", "Sun-City", "Queen-Creek", "Sedona", "Holiday", "Wednesday",
    "Yava", "Faraway", "Bumble Bee", "Christmas", "Love", "Mirage",
    "Drake", "Space",
]

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

TAXES_TEXT = (
    "💰 *Налоги по серверам*\n\n"
    "```\n"
    "Сервер        Дом  Бизнес\n"
    "-------------------------\n"
    "Phoenix      1000    2100\n"
    "Tucson       1000    2000\n"
    "Scottdale    1000    1500\n"
    "Chandler     500    1300\n"
    "Brainburg    1000    2000\n"
    "Saint-Rose   1000    2500\n"
    "Mesa         1000    2000\n"
    "Red-Rock     1000    2400\n"
    "Yuma          650    1000\n"
    "Surprise     1000    3000\n"
    "Prescott     1000    1000\n"
    "Glendale     1000    2300\n"
    "Kingman       800    2600\n"
    "Winslow      1000    3500\n"
    "Payson        619    1489\n"
    "Gilbert      1000    2700\n"
    "Show Low      619    1489\n"
    "Casa-Grande  1000    2500\n"
    "Page         1000    2000\n"
    "Sun-City      700    1350\n"
    "Queen-Creek  1000    2000\n"
    "Sedona       1000    1500\n"
    "Holiday      1000    2000\n"
    "Wednesday    1000    2200\n"
    "Yava          650    1300\n"
    "Faraway       780    1250\n"
    "Bumble Bee    900    1300\n"
    "Christmas     500    1000\n"
    "Mirage        600    1000\n"
    "Love          900    2100\n"
    "Drake        1000    2000\n"
    "Vice City       1       1\n"
    "Space         900    2000\n"
    "```"
)

GPU_DROP_TEXT = (
    "🎮 *Слёт видеокарт*\n\n"
    "Время слёта видеокарт по МСК:\n\n"
    "🕑 02:00\n"
    "🕔 05:00\n"
    "🕗 08:00\n"
    "🕚 11:00\n"
    "🕑 14:00\n"
    "🕔 17:00\n"
    "🕗 20:00\n"
    "🕚 23:00"
)

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
    now   = datetime.now(tz=MSK)
    delta = now - SEASON_EPOCH
    weeks = int(delta.total_seconds() // (7 * 86400))
    return SEASON_EPOCH + timedelta(weeks=weeks + 1)

NOTIFY_OPTIONS = [60, 50, 40, 30, 20, 10, 5]

user_notify_minutes  = {}
lottery_notify_mins  = {}
subscribers          = set()
lottery_subscribers  = set()
season_subscribers   = set()   # подписчики на смену сезона
notified             = set()
sent_notifications   = defaultdict(list)
all_users            = set()
banned_users = set()   # { chat_id }
subscriptions        = {}   # { chat_id(int): expires_at(int ts) }
expiry_warned        = set()   # { chat_id } — кому уже напомнили о скором истечении

def load_banned():
    ref  = db.reference("banned")
    data = ref.get() or {}
    return set(int(k) for k in data.keys())

def is_banned(chat_id):
    """Проверяем бан напрямую в базе (а не только по кэшу banned_users),
    чтобы разбан/бан с сайта срабатывали мгновенно, а не только после
    следующей периодической синхронизации (раз в 2 минуты)."""
    try:
        return db.reference(f"banned/{int(chat_id)}").get() is not None
    except Exception:
        return int(chat_id) in banned_users

def load_subscriptions():
    ref  = db.reference("subscriptions")
    data = ref.get() or {}
    result = {}
    for k, v in data.items():
        if isinstance(v, dict) and v.get("expires_at"):
            result[int(k)] = int(v["expires_at"])
    return result

SUBSCRIPTION_REQUIRED_DEFAULT = True
_subscription_required = SUBSCRIPTION_REQUIRED_DEFAULT  # кэш настройки с сайта (Настройки → По подписке)

def load_subscription_required():
    val = db.reference("config/subscriptionRequired").get()
    return bool(val) if isinstance(val, bool) else SUBSCRIPTION_REQUIRED_DEFAULT

def subscription_required():
    return _subscription_required

def is_authorized(chat_id):
    if int(chat_id) == ADMIN_ID:
        return True
    if not _subscription_required:
        return True
    exp = subscriptions.get(int(chat_id))
    return exp is not None and exp > int(time.time())

def get_expiry(chat_id):
    return subscriptions.get(int(chat_id))

def generate_key():
    group = lambda: "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_GROUP_LEN))
    return "-".join(group() for _ in range(KEY_GROUPS))

def create_keys(duration_days, count, created_by):
    now  = int(time.time())
    keys = []
    for _ in range(count):
        key = generate_key()
        db.reference(f"access_keys/{key}").set({
            "duration_days": duration_days,
            "created_at":    now,
            "created_by":    created_by,
            "activated":     False,
        })
        keys.append(key)
    return keys

def activate_key(chat_id, raw_key):
    key = raw_key.strip().upper()
    ref = db.reference(f"access_keys/{key}")
    data = ref.get()
    if not data or not isinstance(data, dict):
        return None, "notfound"
    if data.get("activated"):
        return None, "used"

    now      = int(time.time())
    duration = int(data.get("duration_days", 0)) * 86400
    current  = subscriptions.get(int(chat_id), 0)
    base     = max(now, current)
    expires_at = base + duration

    ref.update({
        "activated":     True,
        "activated_by":  chat_id,
        "activated_at":  now,
    })
    db.reference(f"subscriptions/{chat_id}").set({
        "key":          key,
        "expires_at":   expires_at,
        "activated_at": now,
    })
    subscriptions[int(chat_id)] = expires_at
    expiry_warned.discard(int(chat_id))
    return expires_at, "ok"

def format_expiry(ts):
    return datetime.fromtimestamp(ts, tz=MSK).strftime("%d.%m.%Y %H:%M МСК")
    
_props_cache      = []
_props_cache_time = 0
CACHE_TTL         = 60
favorite_servers     = {}      # { chat_id: set(server_names) }
season_notified      = False   # флаг чтобы не слать дважды

def load_users():
    ref  = db.reference("users")
    data = ref.get() or {}
    # Дедупликация — все ID как строки
    return set(str(k) for k in data.keys())

def load_notify_prefs():
    """Раньше subscribers/user_notify_minutes/lottery_*/season_subscribers/
    favorite_servers были ЧИСТО в памяти — при каждом рестарте бота (деплой,
    краш, конфликт getUpdates и т.п.) они обнулялись, и людям приходилось
    заново включать уведомления. Теперь всё это лежит в Firebase под
    notify_prefs/{chat_id} и подтягивается сюда при старте."""
    global subscribers, user_notify_minutes, lottery_subscribers, lottery_notify_mins
    global season_subscribers, favorite_servers
    data = db.reference("notify_prefs").get() or {}
    subs, mins, lot_subs, lot_mins, seas_subs, favs = set(), {}, set(), {}, set(), {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        try:
            chat_id = int(k)
        except (TypeError, ValueError):
            continue
        if v.get("subscribed"):
            subs.add(chat_id)
        m = v.get("minutes")
        if isinstance(m, list) and m:
            mins[chat_id] = set(int(x) for x in m)
        if v.get("lottery_subscribed"):
            lot_subs.add(chat_id)
        lm = v.get("lottery_minutes")
        if isinstance(lm, list) and lm:
            lot_mins[chat_id] = set(int(x) for x in lm)
        if v.get("season_subscribed"):
            seas_subs.add(chat_id)
        fv = v.get("favorites")
        if isinstance(fv, list) and fv:
            favs[chat_id] = set(fv)
    subscribers          = subs
    user_notify_minutes  = mins
    lottery_subscribers  = lot_subs
    lottery_notify_mins  = lot_mins
    season_subscribers   = seas_subs
    favorite_servers     = favs
    return len(subs), len(lot_subs), len(seas_subs), len(favs)

def save_notify_prefs(chat_id):
    """Сохраняет ТЕКУЩЕЕ состояние всех настроек уведомлений одного chat_id
    одним запросом (вызывается после любого toggle) — см. load_notify_prefs."""
    chat_id = int(chat_id)
    payload = {
        "subscribed":         chat_id in subscribers,
        "minutes":            sorted(user_notify_minutes.get(chat_id, set())),
        "lottery_subscribed": chat_id in lottery_subscribers,
        "lottery_minutes":    sorted(lottery_notify_mins.get(chat_id, set())),
        "season_subscribed":  chat_id in season_subscribers,
        "favorites":          sorted(favorite_servers.get(chat_id, set())),
    }
    try:
        db.reference(f"notify_prefs/{chat_id}").set(payload)
    except Exception as e:
        print(f"[notify_prefs] Ошибка сохранения для {chat_id}: {e}")

def save_user(chat_id, user=None):
    now_msk = datetime.now(tz=MSK).strftime("%d.%m.%Y %H:%M МСК")
    data = {
        "active":    True,
        "last_seen": now_msk,
    }
    if user:
        if user.username:
            data["username"] = f"@{user.username}"
        if user.full_name:
            data["name"] = user.full_name
    db.reference(f"users/{chat_id}").set(data)

def _effective_floor(pd, d, l):
    """Настроенный порог (l) подобран по НОРМАЛЬНОЙ последовательности PD
    (например для нестрахованных домов: 7→5→3→слёт, порог=3). Но если у
    конкретного дома PD идёт по другой чётности (8→6→4→2→слёт),
    последовательность физически никогда не попадёт ровно на 3 — реально
    упрётся в 2. Подгоняем порог под чётность конкретного PD, чтобы
    последовательность "pd, pd-d, pd-2d, ..." гарантированно попадала на
    него точно.

    Защита от None: если pd/d/l пришли битыми (например запись в базе без
    нужных полей) — раньше это роняло TypeError прямо посреди обработки
    списка "Слёты"/"Ближайшие", а поскольку в боте нет общего перехватчика
    ошибок, пользователь не получал вообще никакого ответа."""
    if d is None or d <= 0:
        d = 1
    if pd is None:
        pd = l if l is not None else 0
    if l is None:
        l = 0
    return l - ((pd - l) % d)

def compute_current_pd(pd, scan_ts, d_val, drop_at, now=None):
    """PD на текущий момент: убывает на d_val на каждой КРУГЛОЙ границе часа
    (00 минут) с момента скана (1/час для застрахованных, 2/час для
    незастрахованных) — включая 05:00, PD списывается и в этот час тоже.
    Особенность только в том, что сам слёт (объект реально пропадает) в
    05:00 не происходит из-за рестарта сервера — падает он только в 06:00,
    но это уже учитывается отдельно там, где считается время самого слёта
    (см. server.py calc_expiry_from_pdl), а не здесь.

    Раньше списание отсчитывалось от самого scan_ts шагами по 3600 секунд —
    то есть если скан пришёл в 14:35, списания происходили в 15:35, 16:35 и
    т.д., а не в 15:00/16:00 как должно быть по игре. Теперь считаем именно
    круглые часовые границы.

    Защита от битых данных: если pd/d_val в базе оказались None (например
    из-за старой записи без нужных полей) — раньше это роняло TypeError
    прямо посреди обработки списка, и так как в боте нет общего
    перехватчика ошибок, пользователь не получал вообще никакого ответа на
    кнопки вроде "Ближайшие" — одна битая запись "вешала" список для всех."""
    if now is None:
        now = int(time.time())
    if pd is None:
        pd = 0
    if not d_val:
        d_val = 1
    floor_val = _effective_floor(pd, d_val, drop_at) if drop_at is not None else 0
    if not scan_ts:
        return max(pd, floor_val)
    try:
        scan_ts = int(scan_ts)
    except (TypeError, ValueError):
        return max(pd, floor_val)

    scan_dt   = datetime.fromtimestamp(scan_ts, tz=MSK)
    next_hour = scan_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    ts = int(next_hour.timestamp())

    elapsed_hours = 0
    while ts <= now:
        elapsed_hours += 1
        ts += 3600
    current = pd - d_val * elapsed_hours
    return max(current, floor_val)

def get_all_props():
    global _props_cache, _props_cache_time
    now = int(time.time())
    if now - _props_cache_time < CACHE_TTL and _props_cache:
        return [p for p in _props_cache if p["expiryTs"] > now]
    ref  = db.reference("properties")
    data = ref.get() or {}
    result = []
    for srv, entries in data.items():
        if not isinstance(entries, dict):
            continue
        for k, v in entries.items():
            if not isinstance(v, dict):
                continue
            try:
                expiry = v.get("expiryTs", 0) or 0
                if expiry <= now:
                    continue
                insured = v.get("insured")
                insured = insured if isinstance(insured, bool) else True
                d_val   = v.get("d") or (1 if insured else 2)
                drop_at = v.get("dropAt")
                base_pd = v.get("pd") or 0
                current_pd = compute_current_pd(base_pd, v.get("scanTs"), d_val, drop_at, now)
            except Exception as e:
                # Одна битая запись не должна ронять список для всех —
                # пропускаем именно её и едем дальше.
                print(f"[get_all_props] Пропущена битая запись {srv}/{k}: {e}")
                continue
            result.append({
                "server":    srv,
                "propType":  v.get("propType", "?"),
                "pd":        current_pd,
                "basePd":    base_pd,
                "insured":   insured,
                "expiryTs":  expiry,
                "expiryH":   expiry,
                "hoursLeft": round((expiry - now) / 3600, 1),
                "minsLeft":  int((expiry - now) / 60),
                "scanTs":    v.get("scanTs", 0),
                "count":     v.get("count", 1),
                "propId":    v.get("propId"),
                "pos":       v.get("pos"),
            })
    result.sort(key=lambda x: x["expiryTs"])
    _props_cache      = result
    _props_cache_time = now
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

def get_servers_summary():
    """Один запрос к базе вместо N (по 2 на каждый сервер) — именно это
    тормозило 'По серверу', так как раньше get_last_scan/get_server_counts
    делали отдельный запрос в Firebase на каждый сервер в списке."""
    now  = int(time.time())
    data = db.reference("properties").get() or {}
    summary = {}
    for srv, entries in data.items():
        if not isinstance(entries, dict):
            continue
        last_scan = 0
        counts    = defaultdict(int)
        for v in entries.values():
            if not isinstance(v, dict):
                continue
            last_scan = max(last_scan, v.get("scanTs", 0))
            if v.get("expiryTs", 0) > now:
                counts[v.get("propType", "?")] += v.get("count", 1)
        summary[srv] = {"last_scan": last_scan or None, "counts": counts}
    return summary

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
            counts[v.get("propType", "?")] += v.get("count", 1)
    return counts

def fmt_time_left(hours_left, mins_left):
    if hours_left < 1:
        return f"через {mins_left} мин"
    return f"через {hours_left}ч"

TELEGRAM_MAX_LEN = 4096

def _split_text_for_telegram(text, limit=TELEGRAM_MAX_LEN):
    """Режем длинный текст на куски <= limit символов, стараясь резать по границам строк,
    чтобы не сломать markdown-разметку посреди строки."""
    if len(text) <= limit:
        return [text]

    parts  = []
    lines  = text.split("\n")
    chunk  = ""
    for line in lines:
        # если даже одна строка длиннее лимита - режем её жёстко
        while len(line) > limit:
            if chunk:
                parts.append(chunk)
                chunk = ""
            parts.append(line[:limit])
            line = line[limit:]

        candidate = chunk + ("\n" if chunk else "") + line
        if len(candidate) > limit:
            parts.append(chunk)
            chunk = line
        else:
            chunk = candidate

    if chunk:
        parts.append(chunk)

    return parts

async def send_long_text(update, text, parse_mode="Markdown", reply_markup=None):
    """Отправляет/редактирует сообщение, автоматически разбивая на несколько частей,
    если текст превышает лимит Telegram (4096 символов). reply_markup вешается
    только на последнюю часть."""
    parts = _split_text_for_telegram(text)

    if update.message:
        for i, part in enumerate(parts):
            kb = reply_markup if i == len(parts) - 1 else None
            await update.message.reply_text(part, parse_mode=parse_mode, reply_markup=kb)
    else:
        query = update.callback_query
        # первую часть - редактируем существующее сообщение, остальные - новыми сообщениями
        first_kb = reply_markup if len(parts) == 1 else None
        await query.edit_message_text(parts[0], parse_mode=parse_mode, reply_markup=first_kb)
        for i, part in enumerate(parts[1:], start=1):
            kb = reply_markup if i == len(parts) - 1 else None
            await query.message.reply_text(part, parse_mode=parse_mode, reply_markup=kb)

async def send_long_text_query(query, text, parse_mode="Markdown", reply_markup=None):
    """Как send_long_text, но для случаев, где уже есть callback_query и используется
    try/except-фолбэк edit -> reply (например, если исходное сообщение нельзя редактировать)."""
    parts = _split_text_for_telegram(text)
    first_kb = reply_markup if len(parts) == 1 else None
    try:
        await query.edit_message_text(parts[0], parse_mode=parse_mode, reply_markup=first_kb)
    except Exception:
        await query.message.reply_text(parts[0], parse_mode=parse_mode, reply_markup=first_kb)
    for i, part in enumerate(parts[1:], start=1):
        kb = reply_markup if i == len(parts) - 1 else None
        await query.message.reply_text(part, parse_mode=parse_mode, reply_markup=kb)

def build_list_text(props, title="📋 Актуальные слёты", page=0, hide_season=False, no_pages=False):
    if not props:
        return "✅ Слётов нет или данных пока нет.", "", 0

    house_total = sum(p.get("count", 1) for p in props if p["propType"] == "house")
    biz_total   = sum(p.get("count", 1) for p in props if p["propType"] == "business")

    from collections import defaultdict
    by_time = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for p in props:
        by_time[p["expiryTs"]][p["server"]][p["propType"]].append(p)

    time_slots = sorted(by_time.keys())
    blocks = []
    for ts in time_slots:
        for srv in sorted(by_time[ts].keys(), key=lambda s: SERVER_ORDER.index(s) if s in SERVER_ORDER else 999):
            blocks.append((ts, srv))

    if no_pages:
        total_pages  = 1
        chunk_blocks = blocks
    else:
        total_pages  = max(1, (len(blocks) + PAGE_SIZE - 1) // PAGE_SIZE)
        page         = max(0, min(page, total_pages - 1))
        chunk_blocks = blocks[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    chunk_times = defaultdict(list)
    for ts, srv in chunk_blocks:
        chunk_times[ts].append(srv)

    # Заголовок — Markdown
    stats = []
    if house_total: stats.append(f"🏠×{house_total}")
    if biz_total:   stats.append(f"🏢×{biz_total}")
    stats_str  = " ".join(stats) if stats else ""
    header     = f"*{title}*"
    if stats_str:
        header += f"\n{stats_str}"
    if total_pages > 1 and not no_pages:
        header += f"\n_Страница {page + 1} из {total_pages}_"

    # Дерево — код блок
    tree_lines = []
    for ts in sorted(chunk_times.keys()):
        servers_in_time = chunk_times[ts]
        time_str = format_time_msk(ts)
        tree_lines.append(f"└─⚡️ Слёты в {time_str}:")

        for si, srv in enumerate(servers_in_time):
            is_last_srv = si == len(servers_in_time) - 1
            srv_prefix  = "   └─" if is_last_srv else "   ├─"

            if not hide_season:
                _, s_emoji = get_season_by_name(srv)
                season_str = f" {s_emoji}" if s_emoji else ""
            else:
                season_str = ""

            tree_lines.append(f"{srv_prefix}🌐 Сервер {srv.upper()}{season_str}")

            for pt, items in by_time[ts][srv].items():
                pt_ru = "Дома" if pt == "house" else "⭐️ Бизнесы ⭐️"
                ind   = "   │  " if not is_last_srv else "      "
                tree_lines.append(f"{ind}└─📍 {pt_ru}:")

                for ii, item in enumerate(items):
                    is_last_item = ii == len(items) - 1
                    item_ind     = "         "
                    item_prefix  = f"{item_ind}└─" if is_last_item else f"{item_ind}├─"
                    prop_id = item.get("propId")
                    pos     = item.get("pos")
                    pd      = item.get("pd", 0)

                    if prop_id:
                        tree_lines.append(f"{item_prefix}id {prop_id} (PayDay: {pd})")
                    elif pos:
                        tree_lines.append(f"{item_prefix}pos {pos} (PayDay: {pd})")
                    else:
                        tree_lines.append(f"{item_prefix}(PayDay: {pd})")

        tree_lines.append("")

    block = "```\n" + "\n".join(tree_lines) + "\n```"
    return header, block, total_pages

# ── Клавиатура ────────────────────────────────────────────
TAXES_TEXT = (
    "💰 *Налоги по серверам*\n\n"
    "```\n"
    "Сервер       Дом   Бизнес \n"
    "--------------------------\n"
    "Phoenix      1000  2100   \n"
    "Tucson       1000  2000   \n"
    "Scottdale    1000  1500   \n"
    "Chandler     1000  2000   \n"
    "Brainburg    1000  2000   \n"
    "Saint-Rose   1000  2500   \n"
    "Mesa         1000  2000   \n"
    "Red-Rock     1000  2400   \n"
    "Yuma         650   1000   \n"
    "Surprise     1000  3000   \n"
    "Prescott     1000  1000   \n"
    "Glendale     1000  2300   \n"
    "Kingman      800   2600   \n"
    "Winslow      1000  3500   \n"
    "Payson       619   1489   \n"
    "Gilbert      1000  2700   \n"
    "Show Low     619   1489   \n"
    "Casa-Grande  1000  2500   \n"
    "Page         1000  2000   \n"
    "Sun-City     700   1350   \n"
    "Queen-Creek  1000  2000   \n"
    "Sedona       1000  1500   \n"
    "Holiday      1000  2000   \n"
    "Wednesday    1000  2200   \n"
    "Yava         650   1300   \n"
    "Faraway      780   1250   \n"
    "Bumble Bee   900   1300   \n"
    "Christmas    500   1000   \n"
    "Mirage       600   1000   \n"
    "Love         900   2100   \n"
    "Drake        1000  2000   \n"
    "Vice City    1     1      \n"
    "Space        900   2000   \n"
    "```"
)

def permanent_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 Все слёты"),     KeyboardButton("⚠️ Ближайшие")],
        [KeyboardButton("💥 Массовый слёт"), KeyboardButton("🔍 Фильтр")],
        [KeyboardButton("🗺 По серверу"),    KeyboardButton("⭐️ Избранное")],
        [KeyboardButton("🔔 Уведомления"),   KeyboardButton("🎰 Лотерея")],
        [KeyboardButton("📜 История"),       KeyboardButton("🏆 Сезоны")],
        [KeyboardButton("🏡 Дома с поместьями"), KeyboardButton("💰 Налоги")],
        [KeyboardButton("🎮 Слёт видеокарт")],
        [KeyboardButton("👤 Профиль")],
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
FIRST_TIME_TEXT = (
    "🏙 *Arizona Property Tracker*\n\n"
    "*Как это работает:* игроки ставят Lua-скрипт в MoonLoader. При открытии диалога с имуществом "
    "скрипт считывает данные и шлёт их на общий сервер. Бот получает эти данные и показывает "
    "актуальные слёты всем пользователям.\n\n"
    "📡 Чем больше игроков со скриптом — тем точнее данные.\n"
    f"⚠️ Данные устаревают через {STALE_HOURS}ч без нового скана.\n"
    "🕐 Всё время указано по МСК (UTC+3).\n\n"
    "📥 Скрипт для установки прикреплён следующим сообщением.\n\n"
    "👨‍💻 Разработчик: @hirotoqq"
)

def buy_key_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("💳 Купить ключ", callback_data="buy_key")]])

async def show_buy_key(update, ctx):
    text = (
        "💳 *Покупка доступа*\n\n"
        f"🗓 Неделя — *{PRICE_WEEK}₽*\n"
        f"📅 Месяц — *{PRICE_MONTH}₽*\n\n"
        "Оформи заказ на FunPay — сразу после оплаты придёт ключ доступа.\n"
        f"🔗 {FUNPAY_URL}\n\n"
        "Когда получишь ключ, активируй его командой:\n"
        "`/key ТВОЙ-КЛЮЧ`"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_banned(chat_id):
        await update.message.reply_text("⛔️ Вы заблокированы и не можете использовать этого бота.")
        return

    is_first_time = db.reference(f"users/{chat_id}").get() is None
    all_users.add(chat_id)
    save_user(chat_id, update.effective_user)

    if not is_authorized(chat_id):
        await update.message.reply_text(
            "🔑 *Arizona Property Tracker*\n\n"
            "Доступ к боту закрыт по ключу.\n\n"
            "Есть ключ? Активируй его командой:\n`/key ТВОЙ-КЛЮЧ`\n\n"
            "Нет ключа — жми кнопку ниже, чтобы купить.",
            parse_mode="Markdown", reply_markup=buy_key_keyboard()
        )
        return

    if is_first_time:
        await update.message.reply_text(FIRST_TIME_TEXT, parse_mode="Markdown")
        if os.path.exists(SCRIPT_PATH):
            try:
                await update.message.reply_document(
                    document=open(SCRIPT_PATH, "rb"),
                    filename="property_tracker.luac",
                    caption="📥 Положи файл в папку `moonloader` в GTA San Andreas.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    text = (
        "🏙 *Arizona Property Tracker*\n\n"
        "📋 *Все слёты* — полный список актуальных слётов\n"
        "⚠️ *Ближайшие* — слёты в ближайшие 3 часа\n"
        "💥 *Массовый слёт* — серверы где падает 4+ объектов\n"
        "🔍 *Фильтр* — слёты по сезону ловли\n"
        "🗺 *По серверу* — выбрать конкретный сервер\n"
        "⭐️ *Избранное* — слёты только на твоих серверах\n"
        "🏆 *Сезоны* — таблица сезонов на неделю\n"
        "🔔 *Уведомления* — настрой оповещения о слётах\n"
        "🎰 *Лотерея* — напоминание о билетах в 21:10 МСК\n"
        f"📜 *История* — слёты за последние {HISTORY_HOURS}ч\n"
        "🏡 *Дома с поместьями* — картинка и описание по серверу\n"
        "💰 *Налоги* — таблица налогов на дома и бизнесы по серверам\n"
        "🎮 *Слёт видеокарт* — время слёта видеокарт по МСК\n\n"
        "👨‍💻 Разработчик: @hirotoqq"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=permanent_keyboard())

async def cmd_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_banned(chat_id):
        await update.message.reply_text("⛔️ Вы заблокированы и не можете использовать этого бота.")
        return
    all_users.add(chat_id)
    save_user(chat_id, update.effective_user)

    if not ctx.args:
        await update.message.reply_text("Использование:\n`/key ТВОЙ-КЛЮЧ`", parse_mode="Markdown")
        return

    raw_key = " ".join(ctx.args)
    expires_at, status = activate_key(chat_id, raw_key)
    if status == "ok":
        await update.message.reply_text(
            f"✅ Ключ активирован!\nДоступ действует до: *{format_expiry(expires_at)}*",
            parse_mode="Markdown", reply_markup=permanent_keyboard()
        )
    elif status == "used":
        await update.message.reply_text("❌ Этот ключ уже был активирован.")
    else:
        await update.message.reply_text("❌ Неверный ключ. Проверь и попробуй ещё раз:\n`/key ТВОЙ-КЛЮЧ`", parse_mode="Markdown")

async def cmd_delestate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/delestate <Сервер> — убрать сервер из кнопки '🏡 Дома с поместьями'
    (например, если добавили с опечаткой)."""
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    full_text = update.message.text
    idx = full_text.find(" ")
    if idx == -1:
        await update.message.reply_text("Использование:\n/delestate <Сервер>")
        return
    server = full_text[idx+1:].strip()
    if not db.reference(f"estates/{server}").get():
        await update.message.reply_text(f"У сервера {server} и так ничего не настроено.")
        return
    db.reference(f"estates/{server}").delete()
    order = db.reference("estates_order").get() or []
    if isinstance(order, list) and server in order:
        db.reference("estates_order").set([s for s in order if s != server])
    await update.message.reply_text(f"🗑 Убрано: {server}")

async def cmd_estates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/estates — список серверов, уже настроенных в '🏡 Дома с поместьями'."""
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    estates = db.reference("estates").get(shallow=True) or {}
    if not estates:
        await update.message.reply_text("Пока ничего не добавлено.\nДобавить: фото с подписью /setestate <Сервер> <текст>")
        return
    lines = "\n".join(f"• {s}" for s in sorted(estates))
    await update.message.reply_text(
        f"🏡 Настроено ({len(estates)}):\n{lines}\n\n"
        "Добавить/обновить: фото с подписью /setestate <Сервер> <текст>\n"
        "Убрать: /delestate <Сервер>"
    )

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    full_text = update.message.text
    idx = full_text.find(" ")
    if idx == -1:
        await update.message.reply_text("Использование:\n/broadcast Текст сообщения")
        return
    text = full_text[idx+1:]
    sent, failed = 0, 0
    seen = set()
    for chat_id in list(all_users):
        uid = int(chat_id)
        if uid in seen:
            continue
        seen.add(uid)
        try:
            await ctx.bot.send_message(uid, text, parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Отправлено: {sent}\n❌ Не доставлено: {failed}")

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Использование:\n/ban ID причина")
        return
    try:
        uid    = int(args[0])
        reason = " ".join(args[1:]) if len(args) > 1 else "не указана"
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return

    now_msk = datetime.now(tz=MSK).strftime("%d.%m.%Y %H:%M МСК")
    banned_users.add(uid)
    db.reference(f"banned/{uid}").set({
        "reason":  reason,
        "date":    now_msk,
    })

    # Пробуем получить username
    try:
        chat = await ctx.bot.get_chat(uid)
        name = f"@{chat.username}" if chat.username else chat.full_name
    except Exception:
        name = str(uid)

    await update.message.reply_text(
        f"🚫 Пользователь {uid} ({name}) заблокирован.\n"
        f"Причина: {reason}"
    )

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Использование:\n/unban ID")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return

    banned_users.discard(uid)
    db.reference(f"banned/{uid}").delete()
    await update.message.reply_text(f"✅ Пользователь {uid} разблокирован.")

async def cmd_genkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "Использование:\n/genkey ДНЕЙ [КОЛИЧЕСТВО]\n\n"
            "Пример: /genkey 30 5 — 5 ключей на 30 дней"
        )
        return
    try:
        days  = int(args[0])
        count = int(args[1]) if len(args) > 1 else 1
    except ValueError:
        await update.message.reply_text("❌ Неверные параметры.")
        return
    if days <= 0 or count <= 0 or count > 50:
        await update.message.reply_text("❌ Дни > 0, количество от 1 до 50.")
        return

    keys = create_keys(days, count, update.effective_chat.id)
    text = f"🔑 Создано ключей: {count} (на {days} дн.)\n\n" + "\n".join(f"`{k}`" for k in keys)
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_extend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Использование:\n/extend ID ДНЕЙ")
        return
    try:
        uid  = int(args[0])
        days = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ Неверные параметры.")
        return

    now        = int(time.time())
    current    = subscriptions.get(uid, 0)
    base       = max(now, current)
    expires_at = base + days * 86400
    db.reference(f"subscriptions/{uid}").set({
        "key":          "manual_admin",
        "expires_at":   expires_at,
        "activated_at": now,
    })
    subscriptions[uid] = expires_at
    expiry_warned.discard(uid)
    await update.message.reply_text(f"✅ Доступ для {uid} продлён до {format_expiry(expires_at)}")

async def cmd_banlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    ref  = db.reference("banned")
    data = ref.get() or {}
    if not data:
        await update.message.reply_text("✅ Забаненных пользователей нет.")
        return
    lines = ["🚫 *Забаненные пользователи:*\n"]
    for i, (uid, v) in enumerate(data.items(), 1):
        reason = v.get("reason", "не указана") if isinstance(v, dict) else "не указана"
        date   = v.get("date", "неизвестно") if isinstance(v, dict) else "неизвестно"
        lines.append(f"{i}. `{uid}`\n   Причина: {reason}\n   Дата: {date}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Админ настраивает контент для кнопки '🏡 Дома с поместьями': отправляет
    боту ФОТО с подписью вида '/setestate <Сервер> <текст сообщения>'.
    Текст и file_id картинки сохраняются в Firebase (estates/{server}) —
    именно их бот присылает пользователю одним сообщением при выборе
    этого сервера в show_estates_menu / cb_handler (estate_<server>).

    <Сервер> может быть ЛЮБЫМ названием, не обязательно из SERVER_ORDER —
    так админ сам добавляет новые пункты в это меню, список кнопок в
    show_estates_menu строится из того, что реально есть в Firebase."""
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_ID:
        return
    caption = update.message.caption or ""
    if not caption.startswith("/setestate"):
        return
    parts = caption.split(maxsplit=2)
    if len(parts) < 2:
        await update.message.reply_text(
            "Использование (подпись к фото):\n/setestate <Сервер> <текст сообщения>\n\n"
            "Название сервера может быть любым — просто как оно должно называться в кнопке."
        )
        return
    server  = parts[1]
    text    = parts[2] if len(parts) > 2 else ""
    file_id = update.message.photo[-1].file_id  # последний элемент — самое большое разрешение
    db.reference(f"estates/{server}").set({"file_id": file_id, "text": text})
    await update.message.reply_text(f"✅ Сохранено для {server}")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_banned(chat_id):
        await update.message.reply_text("⛔️ Вы заблокированы и не можете использовать этого бота.")
        return
    all_users.add(chat_id)
    save_user(chat_id, update.effective_user)
    t = update.message.text

    if not is_authorized(chat_id):
        expires_at, status = activate_key(chat_id, t)
        if status == "ok":
            await update.message.reply_text(
                f"✅ Ключ активирован!\nДоступ действует до: *{format_expiry(expires_at)}*",
                parse_mode="Markdown", reply_markup=permanent_keyboard()
            )
        elif status == "used":
            await update.message.reply_text("❌ Этот ключ уже был активирован.")
        else:
            await update.message.reply_text(
                "❌ Неверный ключ.\nПришли корректный ключ доступа или активируй командой `/key ТВОЙ-КЛЮЧ`.\n"
                "Нет ключа — жми кнопку ниже, чтобы купить.",
                parse_mode="Markdown", reply_markup=buy_key_keyboard()
            )
        return
    if t == "📋 Все слёты":          await show_list(update, ctx)
    elif t == "⚠️ Ближайшие":        await show_soon(update, ctx)
    elif t == "💥 Массовый слёт":    await show_mass_drop(update, ctx)
    elif t == "🔍 Фильтр":           await show_filter_menu(update, ctx)
    elif t == "🗺 По серверу":        await show_servers(update, ctx)
    elif t == "⭐️ Избранное":        await show_favorites(update, ctx)
    elif t == "👤 Профиль":           await show_profile(update, ctx)
    elif t == "🔔 Уведомления":       await show_notify_menu(update, ctx)
    elif t == "🎰 Лотерея":           await show_lottery_menu(update, ctx)
    elif t == "📜 История":           await show_history(update, ctx)
    elif t == "🏆 Сезоны":            await show_seasons(update, ctx)
    elif t == "🏡 Дома с поместьями": await show_estates_menu(update, ctx)
    elif t == "💰 Налоги":            await update.message.reply_text(TAXES_TEXT, parse_mode="Markdown")
    elif t == "🎮 Слёт видеокарт":    await update.message.reply_text(GPU_DROP_TEXT, parse_mode="Markdown")

# ── Показ списков ─────────────────────────────────────────
async def show_list(update, ctx, page=0):
    props = get_all_props()
    header, block, total = build_list_text(props, page=page)
    btns = _page_buttons(page, total, "list")
    kb   = InlineKeyboardMarkup(btns) if btns else None
    await send_long_text(update, header + "\n" + block, reply_markup=kb)

async def show_soon(update, ctx, page=0):
    props = [p for p in get_all_props() if p["hoursLeft"] <= 3]
    header, block, total = build_list_text(props, "⚠️ Слёты в ближайшие 3 часа", no_pages=True)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄", callback_data="soon_refresh")]])
    await send_long_text(update, header + "\n" + block, reply_markup=kb)

async def show_mass_drop(update, ctx, page=0):
    props = get_all_props()
    # У каждой отдельной записи в базе count всегда = 1 (одна запись = один
    # объект), поэтому раньше фильтр по p["count"] никогда не срабатывал.
    # "Массовый слёт" — это когда на одном сервере в одно и то же время
    # слетает несколько объектов, поэтому считаем это группировкой здесь.
    groups = defaultdict(list)
    for p in props:
        groups[(p["server"], p["expiryTs"])].append(p)

    filtered = []
    for items in groups.values():
        if len(items) >= MASS_DROP_MIN:
            filtered.extend(items)

    header, block, total = build_list_text(filtered, f"💥 Массовые слёты ({MASS_DROP_MIN}+)", page=page)
    btns = _page_buttons(page, total, "mass")
    kb   = InlineKeyboardMarkup(btns) if btns else None
    await send_long_text(update, header + "\n" + block, reply_markup=kb)

async def show_filter_menu(update, ctx):
    buttons = []
    for num, (name, emoji) in SEASON_NAMES.items():
        buttons.append([InlineKeyboardButton(f"{emoji} {name}", callback_data=f"filter_season_{num}")])
    text = "🔍 *Фильтр по сезону*\n\nВыбери сезон:"
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_estates_menu(update, ctx):
    """Кнопка «🏡 Дома с поместьями»: список серверов, по нажатию на сервер
    приходит ОДНО сообщение — картинка + текст, свои для каждого сервера
    (задаются админом командой /setestate, см. handle_photo).

    Список серверов здесь НЕ привязан к SERVER_ORDER — админ может добавить
    вообще любой сервер (даже не из основного списка), просто отправив фото
    с подписью /setestate. Кнопки строятся из того, что реально сохранено
    в Firebase (estates/). Порядок показа — из estates_order (задаётся на
    сайте, раздел «Поместья»); всё, что туда ещё не попало, добавляется
    в конец по алфавиту."""
    estates = db.reference("estates").get(shallow=True) or {}
    order   = db.reference("estates_order").get() or []
    if not isinstance(order, list):
        order = []
    servers = [s for s in order if s in estates]
    servers += sorted(s for s in estates if s not in servers)

    if not servers:
        txt = "🏡 *Дома с поместьями*\n\nПока ничего не добавлено. Обратись к администратору."
        if update.message:
            await update.message.reply_text(txt, parse_mode="Markdown")
        else:
            await update.callback_query.edit_message_text(txt, parse_mode="Markdown")
        return

    buttons, row = [], []
    for s in servers:
        row.append(InlineKeyboardButton(s, callback_data=f"estate_{s}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    txt = "🏡 *Дома с поместьями*\n\nВыбери сервер:"
    kb  = InlineKeyboardMarkup(buttons)
    if update.message:
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb)

async def show_servers(update, ctx):
    summary = get_servers_summary()
    servers = [s for s in SERVER_ORDER if s in summary] + [s for s in summary if s not in SERVER_ORDER]
    if not servers:
        txt = "Данных пока нет."
        if update.message: await update.message.reply_text(txt)
        else: await update.callback_query.edit_message_text(txt)
        return
    buttons, row = [], []
    for s in servers:
        info   = summary[s]
        icon   = "🔴" if is_stale(info["last_scan"]) else "🟢"
        counts = info["counts"]
        parts  = []
        if counts.get("house"):    parts.append(f"🏠×{counts['house']}")
        if counts.get("business"): parts.append(f"🏢×{counts['business']}")
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
        # Нет избранных — показываем выбор
        await show_favorites_edit(update, ctx)
        return

    props = [p for p in get_all_props() if p["server"] in favs]
    header, block, total = build_list_text(props, "⭐️ Избранные серверы", page=page)
    text = header + "\n" + block
    btns = _page_buttons(page, total, "fav")
    btns.append([InlineKeyboardButton("✏️ Изменить", callback_data="fav_edit")])
    kb = InlineKeyboardMarkup(btns)
    await send_long_text(update, text, reply_markup=kb)

async def show_favorites_edit(update, ctx):
    chat_id = update.effective_chat.id
    favs    = favorite_servers.get(chat_id, set())
    servers = SERVER_ORDER
    buttons, row = [], []
    for s in servers:
        mark = "⭐️ " if s in favs else ""
        row.append(InlineKeyboardButton(f"{mark}{s}", callback_data=f"fav_toggle_{s}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton("✅ Готово", callback_data="fav_done")])
    text = "⭐️ *Избранные серверы*\n\nВыбери серверы для отслеживания:"
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_seasons(update, ctx):
    week_idx    = get_current_week_index()
    week_num    = week_idx + 1
    next_change = get_next_season_change()
    next_str    = next_change.strftime("%d.%m в %H:%M МСК")
    lines = [f"🏆 *Сезоны — Неделя {week_num}*", f"_Следующая смена: {next_str}_\n"]
    for i, srv in enumerate(SERVER_ORDER):
        season_name, season_emoji = get_season(i)
        lines.append(f"{str(i+1).zfill(2)} - {season_emoji} {season_name}")

    # Кнопка подписки на смену сезона
    is_sub  = update.effective_chat.id in season_subscribers
    btn     = "🔕 Отписаться от смены" if is_sub else "🔔 Уведомить о смене"
    buttons = [[InlineKeyboardButton(btn, callback_data="season_notify_toggle")]]

    text = "\n".join(lines)
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_profile(update, ctx):
    chat_id    = update.effective_chat.id
    is_sub     = chat_id in subscribers
    is_lot     = chat_id in lottery_subscribers
    is_season  = chat_id in season_subscribers
    selected   = user_notify_minutes.get(chat_id, set())
    lot_sel    = lottery_notify_mins.get(chat_id, set())
    favs       = favorite_servers.get(chat_id, set())
    exp        = get_expiry(chat_id)
    access_str = format_expiry(exp) if exp else "нет"

    notify_str = "Вкл"
    if is_sub and selected:
        notify_str += f" ({', '.join(f'{m}м' for m in sorted(selected))})"
    elif not is_sub:
        notify_str = "Выкл"

    lot_str = "Вкл"
    if is_lot and lot_sel:
        lot_str += f" ({', '.join(f'{m}м' for m in sorted(lot_sel))})"
    elif not is_lot:
        lot_str = "Выкл"

    season_str = "Вкл" if is_season else "Выкл"
    fav_str    = ", ".join(sorted(favs)) if favs else "не выбраны"

    text = (
        f"👤 *Профиль*\n\n"
        f"🔑 Доступ до: {access_str}\n"
        f"🔔 Слёты: {notify_str}\n"
        f"🎰 Лотерея: {lot_str}\n"
        f"🏆 Сезон: {season_str}\n"
        f"⭐️ Избранное: {fav_str}"
    )
    buttons = [
        [InlineKeyboardButton("🔔 Настроить уведомления", callback_data="open_notify")],
        [InlineKeyboardButton("🎰 Настроить лотерею",     callback_data="open_lottery")],
        [InlineKeyboardButton("⭐️ Изменить избранное",    callback_data="fav_edit")],
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

async def show_history(update, ctx, page=0):
    history = get_history()
    header, block, total = build_list_text(history, f"📜 История слётов (последние {HISTORY_HOURS}ч)", page=page)
    btns = _page_buttons(page, total, "hist")
    kb   = InlineKeyboardMarkup(btns) if btns else None
    await send_long_text(update, header + "\n" + block, reply_markup=kb)

# ── Callbacks ─────────────────────────────────────────────
async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.message.chat_id

    if is_banned(chat_id):
        await query.answer("⛔️ Вы заблокированы.", show_alert=True)
        return
    if not is_authorized(chat_id):
        if query.data == "buy_key":
            await query.answer()
            await show_buy_key(update, ctx)
            return
        await query.answer("🔑 Доступ закрыт. Пришли ключ доступа боту.", show_alert=True)
        return

    await query.answer()
    data    = query.data

    if data.startswith("list_page_"):
        await show_list(update, ctx, page=int(data.split("_")[-1]))
    elif data.startswith("soon_page_"):
        await show_soon(update, ctx, page=int(data.split("_")[-1]))
    elif data == "soon_refresh":
        await show_soon(update, ctx)
    elif data.startswith("mass_page_"):
        await show_mass_drop(update, ctx, page=int(data.split("_")[-1]))
    elif data.startswith("hist_page_"):
        await show_history(update, ctx, page=int(data.split("_")[-1]))
    elif data.startswith("fav_page_"):
        await show_favorites(update, ctx, page=int(data.split("_")[-1]))
    elif data == "action_servers":
        await show_servers(update, ctx)

    elif data.startswith("estate_"):
        server = data.replace("estate_", "")
        info   = db.reference(f"estates/{server}").get()
        if not isinstance(info, dict) or not info.get("file_id"):
            await ctx.bot.send_message(chat_id, f"Пока нет данных по серверу {server}.")
        else:
            try:
                await ctx.bot.send_photo(
                    chat_id, photo=info["file_id"],
                    caption=info.get("text") or "",
                    parse_mode="Markdown",
                )
            except Exception:
                # На случай если caption с Markdown-разметкой некорректен —
                # шлём тем же файлом, но без parse_mode, лишь бы дошло.
                await ctx.bot.send_photo(chat_id, photo=info["file_id"], caption=info.get("text") or "")

    elif data == "open_notify":
        await show_notify_menu(update, ctx)
    elif data == "open_lottery":
        await show_lottery_menu(update, ctx)

    elif data == "fav_edit":
        await show_favorites_edit(update, ctx)

    elif data.startswith("fav_toggle_"):
        server = data.replace("fav_toggle_", "")
        favs   = favorite_servers.setdefault(chat_id, set())
        if server in favs: favs.discard(server)
        else: favs.add(server)
        save_notify_prefs(chat_id)
        # Обновляем кнопки
        servers = SERVER_ORDER
        buttons, row = [], []
        for s in servers:
            mark = "⭐️ " if s in favorite_servers.get(chat_id, set()) else ""
            row.append(InlineKeyboardButton(f"{mark}{s}", callback_data=f"fav_toggle_{s}"))
            if len(row) == 2:
                buttons.append(row); row = []
        if row: buttons.append(row)
        buttons.append([InlineKeyboardButton("✅ Готово", callback_data="fav_done")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "fav_done":
        await show_favorites(update, ctx)

    elif data == "season_notify_toggle":
        if chat_id in season_subscribers:
            season_subscribers.discard(chat_id)
            save_notify_prefs(chat_id)
            await query.answer("🔕 Отписался от смены сезона")
        else:
            season_subscribers.add(chat_id)
            save_notify_prefs(chat_id)
            await query.answer("🔔 Подписался на смену сезона")
        await show_seasons(update, ctx)

    elif data.startswith("filter_season_"):
        season_num = int(data.split("_")[-1])
        season_name, season_emoji = SEASON_NAMES[season_num]
        week_idx = get_current_week_index()
        servers_with_season = [SERVER_ORDER[i] for i, s in enumerate(SEASON_TABLES[week_idx]) if s == season_num]
        props = [p for p in get_all_props() if p["server"] in servers_with_season]
        # корректная распаковка из build_list_text (header, block, total)
        header, block, total = build_list_text(props, f"{season_emoji} {season_name}")
        btns = _page_buttons(0, total, f"fseas{season_num}")
        btns.append([InlineKeyboardButton("◀️ К фильтру", callback_data="back_filter")])
        await send_long_text_query(query, header + "\n" + block, reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("fseas"):
        # формат fseas{num}_page_{n}
        try:
            season_num  = int(data[5:data.index("_page_")])
            page        = int(data.split("_")[-1])
        except Exception:
            await query.answer("Ошибка обработки", show_alert=True)
            return
        season_name, season_emoji = SEASON_NAMES[season_num]
        week_idx = get_current_week_index()
        servers_with_season = [SERVER_ORDER[i] for i, s in enumerate(SEASON_TABLES[week_idx]) if s == season_num]
        props = [p for p in get_all_props() if p["server"] in servers_with_season]
        header, block, total = build_list_text(props, f"{season_emoji} {season_name}", page=page)
        btns = _page_buttons(page, total, f"fseas{season_num}")
        btns.append([InlineKeyboardButton("◀️ К фильтру", callback_data="back_filter")])
        await send_long_text_query(query, header + "\n" + block, reply_markup=InlineKeyboardMarkup(btns))

    elif data == "back_filter":
        await show_filter_menu(update, ctx)

    elif data.startswith("srv_"):
        server             = data.replace("srv_", "")
        props              = [p for p in get_all_props() if p["server"] == server]
        last_scan          = get_last_scan(server)
        scan_str           = format_last_scan(last_scan)
        warn               = "⚠️ Данные устарели (скан > 8ч назад)\n\n" if is_stale(last_scan) else ""
        counts             = get_server_counts(server)
        parts              = []
        if counts["house"]:    parts.append(f"🏠×{counts['house']}")
        if counts["business"]: parts.append(f"🏢×{counts['business']}")
        stats_str          = " ".join(parts)
        season_name, s_emoji = get_season_by_name(server)
        # правильная распаковка: header, block, total
        header, block, total = build_list_text(props, f"📋 {server}", page=0, hide_season=True)
        text = warn + header + "\n" + block + f"\n\n🏆 Сезон: {s_emoji} {season_name}\n🕐 _Последний скан: {scan_str}_"
        buttons            = [[InlineKeyboardButton("◀️ К серверам", callback_data="action_servers")]]
        await send_long_text_query(query, text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "action_notify_toggle":
        if chat_id in subscribers: subscribers.discard(chat_id)
        else: subscribers.add(chat_id)
        save_notify_prefs(chat_id)
        await show_notify_menu(update, ctx)

    elif data.startswith("notify_min_"):
        m = int(data.split("_")[-1])
        s = user_notify_minutes.setdefault(chat_id, set())
        if m in s: s.discard(m)
        else: s.add(m)
        save_notify_prefs(chat_id)
        await show_notify_menu(update, ctx)

    elif data == "action_lottery_toggle":
        if chat_id in lottery_subscribers: lottery_subscribers.discard(chat_id)
        else: lottery_subscribers.add(chat_id)
        save_notify_prefs(chat_id)
        await show_lottery_menu(update, ctx)

    elif data.startswith("lottery_min_"):
        m = int(data.split("_")[-1])
        s = lottery_notify_mins.setdefault(chat_id, set())
        if m in s: s.discard(m)
        else: s.add(m)
        save_notify_prefs(chat_id)
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
        for p in props:
            for chat_id in list(subscribers):
                if not is_authorized(chat_id):
                    continue
                selected = user_notify_minutes.get(chat_id, set())
                for mins in selected:
                    if p["minsLeft"] <= mins:
                        key = f"{chat_id}_{p['server']}_{p['propType']}_{p['expiryH']}_{mins}"
                        if key not in notified:
                            notified.add(key)
                            cnt      = p.get("count", 1)
                            emoji    = prop_emoji(p["propType"])
                            _, s_emoji = get_season_by_name(p["server"])
                            text = (
                                f"⚠️ *Скоро слёт!*\n"
                                f"Сервер: *{p['server']}* {s_emoji}\n"
                                f"({emoji}×{cnt}) - {p['pd']}pd\n"
                                f"{format_time_msk(p['expiryTs'])} МСК — через {p['minsLeft']} мин"
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
            if not is_authorized(chat_id):
                continue
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

async def season_notify_loop(app):
    """Уведомление о смене сезона в понедельник в 06:10 МСК."""
    while True:
        await asyncio.sleep(30)
        now_msk = datetime.now(tz=MSK)
        if now_msk.weekday() == 0 and now_msk.hour == 6 and now_msk.minute == 10:
            # Проверяем в Firebase не отправляли ли уже сегодня
            today_key = now_msk.strftime("%Y-%m-%d")
            ref = db.reference(f"season_notified/{today_key}")
            already = ref.get()
            if not already:
                ref.set(True)
                week_idx  = get_current_week_index()
                week_num  = week_idx + 1
                lines = [f"🏆 *Сменился сезон! Неделя {week_num}*\n"]
                for i, srv in enumerate(SERVER_ORDER):
                    season_name, season_emoji = SEASON_NAMES[SEASON_TABLES[week_idx][i]]
                    lines.append(f"{str(i+1).zfill(2)} - {season_emoji} {season_name}")
                text = "\n".join(lines)
                for chat_id in list(season_subscribers):
                    if not is_authorized(chat_id):
                        continue
                    try:
                        await app.bot.send_message(chat_id, text, parse_mode="Markdown")
                    except Exception:
                        pass

async def subscription_expiry_loop(app):
    """Напоминает пользователям за EXPIRY_WARN_HOURS до истечения ключа."""
    while True:
        await asyncio.sleep(1800)
        now = int(time.time())
        for chat_id, exp in list(subscriptions.items()):
            if exp <= now:
                continue
            if chat_id in expiry_warned:
                continue
            if exp - now <= EXPIRY_WARN_HOURS * 3600:
                expiry_warned.add(chat_id)
                try:
                    await app.bot.send_message(
                        chat_id,
                        f"⏳ *Доступ скоро закончится!*\n"
                        f"Действует до: {format_expiry(exp)}\n\n"
                        f"Пришли новый ключ, чтобы продлить доступ.",
                        parse_mode="Markdown"
                    )
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
                db.reference(f"history/{k}").delete()

async def sync_loop():
    """Периодически подтягивает из базы то, что мог поменять админ через
    сайт (продление/уменьшение/отзыв доступа, бан, переключатель 'По
    подписке') — без этого изменения на сайте применялись бы только после
    перезапуска бота."""
    global banned_users, subscriptions, _subscription_required
    while True:
        await asyncio.sleep(120)
        try:
            banned_users  = load_banned()
            subscriptions = load_subscriptions()
            _subscription_required = load_subscription_required()
        except Exception:
            pass

# ── Запуск ───────────────────────────────────────────────
async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    """Раньше в боте не было общего перехватчика ошибок — если обработчик
    кнопки падал с исключением (например из-за битой записи в базе), это
    просто гасилось библиотекой: пользователь не получал вообще НИЧЕГО в
    ответ на нажатие, и по этому симптому было невозможно понять, что
    вообще произошло. Теперь ошибка хотя бы попадёт в лог (видно в Render),
    и пользователь получит понятное сообщение вместо тишины."""
    print(f"[on_error] {ctx.error!r}")
    import traceback
    traceback.print_exception(type(ctx.error), ctx.error, ctx.error.__traceback__)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Что-то пошло не так при обработке запроса. Попробуй ещё раз чуть позже."
            )
        except Exception:
            pass

def main():
    global all_users, banned_users, subscriptions, _subscription_required
    all_users     = load_users()
    banned_users  = load_banned()
    subscriptions = load_subscriptions()
    _subscription_required = load_subscription_required()
    n_sub, n_lot, n_seas, n_fav = load_notify_prefs()
    print(f"Загружено пользователей: {len(all_users)}, активных ключей: {len(subscriptions)}, "
          f"подписчиков на уведомления: {n_sub}, лотерею: {n_lot}, сезон: {n_seas}, с избранным: {n_fav}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("key",       cmd_key))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("delestate", cmd_delestate))
    app.add_handler(CommandHandler("estates",   cmd_estates))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CommandHandler("ban",      cmd_ban))
    app.add_handler(CommandHandler("unban",    cmd_unban))
    app.add_handler(CommandHandler("banlist",  cmd_banlist))
    app.add_handler(CommandHandler("genkey",   cmd_genkey))
    app.add_handler(CommandHandler("extend",   cmd_extend))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(sync_loop())
    loop.create_task(notify_loop(app))
    loop.create_task(lottery_loop(app))
    loop.create_task(season_notify_loop(app))
    loop.create_task(ping_loop())
    loop.create_task(delete_old_notifications(app))
    loop.create_task(cleanup_history())
    loop.create_task(subscription_expiry_loop(app))

    print("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()