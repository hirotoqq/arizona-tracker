# server.py
from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string
import firebase_admin
from firebase_admin import credentials, db
import os, time, json, threading, secrets, string
from functools import wraps
from datetime import timedelta
import requests
import datetime as _dt
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

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

SECRET_KEY = os.environ.get("SECRET_KEY", "")
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)
app.config["SESSION_COOKIE_SECURE"]      = True
app.config["SESSION_COOKIE_SAMESITE"]    = "Lax"

VALID_SERVERS = {
    "Phoenix","Tucson","Scottdale","Chandler","Brainburg","Saint-Rose",
    "Mesa","Red-Rock","Yuma","Surprise","Prescott","Glendale",
    "Kingman","Winslow","Payson","Gilbert","Show Low","Casa-Grande",
    "Page","Sun-City","Queen-Creek","Sedona","Holiday","Wednesday",
    "Yava","Faraway","Bumble Bee","Christmas","Love","Mirage","Drake","Space",
}

SERVER_ORDER = [
    "Phoenix", "Tucson", "Scottdale", "Chandler", "Brainburg", "Saint-Rose",
    "Mesa", "Red-Rock", "Yuma", "Surprise", "Prescott", "Glendale",
    "Kingman", "Winslow", "Payson", "Gilbert", "Show Low", "Casa-Grande",
    "Page", "Sun-City", "Queen-Creek", "Sedona", "Holiday", "Wednesday",
    "Yava", "Faraway", "Bumble Bee", "Christmas", "Love", "Mirage",
    "Drake", "Space",
]

def server_label(srv):
    return srv

# ============================================================
#   Расчёт времени слёта (как в property_tracker.lua)
# ============================================================
MSK_OFFSET = 3 * 3600
MSK_TZ = _dt.timezone(_dt.timedelta(hours=3))

def format_msk(ts, fmt="%d.%m.%Y %H:%M"):
    try:
        return _dt.datetime.fromtimestamp(int(ts), tz=MSK_TZ).strftime(fmt)
    except Exception:
        return ""

DEFAULT_DROP_AT_INSURED   = 2
DEFAULT_DROP_AT_UNINSURED = 3
D_INSURED   = 1   # застрахованный дом теряет 1 PD в час
D_UNINSURED = 2   # незастрахованный дом теряет 2 PD в час (максимум возможного распада)
DEFAULT_D = D_INSURED
HISTORY_RETENTION_SECONDS = 48 * 3600  # хранить историю сканов домов без ID 48ч (раньше было 30 дней при том что показывались только последние 24ч — лишние данные тормозили загрузку страницы)
DETECT_REQUIRED_STREAK = 2  # для домов без ID: сколько раз подряд должно совпасть автоопределение, прежде чем статус реально применится

# Порог слёта (L) по каждому серверу для ДОМОВ — отдельно застрахованные/нет.
# Для бизнесов отдельной статистики нет, поэтому по умолчанию используются
# общие значения DEFAULT_DROP_AT_*, а точные пороги задаются в Настройках.
HOUSE_DROP_AT_INSURED = {
    "Phoenix": 2, "Tucson": 2, "Scottdale": 2, "Chandler": 2,
    "Brainburg": 2, "Saint-Rose": 2, "Mesa": 2, "Red-Rock": 2,
    "Yuma": 2, "Surprise": 2, "Prescott": 2, "Glendale": 2,
    "Kingman": 2, "Winslow": 2, "Payson": 1, "Gilbert": 2,
    "Show Low": 1, "Casa-Grande": 2, "Page": 2, "Sun-City": 1,
    "Queen-Creek": 2, "Sedona": 2, "Holiday": 2, "Wednesday": 2,
    "Yava": 2, "Faraway": 1, "Bumble Bee": 2, "Christmas": 2,
    "Love": 1, "Mirage": 1, "Drake": 2, "Space": 1,
}

HOUSE_DROP_AT_UNINSURED = {
    "Phoenix": 3, "Tucson": 3, "Scottdale": 3, "Chandler": 2,
    "Brainburg": 3, "Saint-Rose": 3, "Mesa": 2, "Red-Rock": 3,
    "Yuma": 3, "Surprise": 3, "Prescott": 3, "Glendale": 3,
    "Kingman": 3, "Winslow": 3, "Payson": 1, "Gilbert": 3,
    "Show Low": 2, "Casa-Grande": 2, "Page": 3, "Sun-City": 1,
    "Queen-Creek": 3, "Sedona": 3, "Holiday": 2, "Wednesday": 2,
    "Yava": 3, "Faraway": 2, "Bumble Bee": 3, "Christmas": 3,
    "Love": 2, "Mirage": 1, "Drake": 2, "Space": 2,
}

def _insurance_key(insured):
    return "insured" if insured else "uninsured"

def _default_drop_at(server, prop_type, insured):
    if prop_type == "house":
        table = HOUSE_DROP_AT_INSURED if insured else HOUSE_DROP_AT_UNINSURED
        return table.get(server, DEFAULT_DROP_AT_INSURED if insured else DEFAULT_DROP_AT_UNINSURED)
    return DEFAULT_DROP_AT_INSURED if insured else DEFAULT_DROP_AT_UNINSURED

def get_drop_at(server, prop_type, insured):
    key = _insurance_key(insured)
    try:
        cfg = db.reference(f"config/dropAt/{server}/{prop_type}/{key}").get()
    except Exception:
        cfg = None
    if isinstance(cfg, (int, float)):
        return int(cfg)
    return _default_drop_at(server, prop_type, insured)

def get_drop_at_cached(cfg_map, server, prop_type, insured):
    """Как get_drop_at, но без похода в Firebase — использует уже загруженный
    целиком словарь config/dropAt. Нужно, чтобы не делать по одному запросу
    к базе на каждую строку таблицы (именно это тормозило вкладку 'Слёты')."""
    key = _insurance_key(insured)
    val = None
    if isinstance(cfg_map, dict):
        srv_cfg = cfg_map.get(server)
        if isinstance(srv_cfg, dict):
            type_cfg = srv_cfg.get(prop_type)
            if isinstance(type_cfg, dict):
                val = type_cfg.get(key)
    if isinstance(val, (int, float)):
        return int(val)
    return _default_drop_at(server, prop_type, insured)

def set_drop_at(server, prop_type, insured_key, value):
    db.reference(f"config/dropAt/{server}/{prop_type}/{insured_key}").set(int(value))

def infer_insured(v):
    """Определяет застрахован ли дом по записи: явное поле 'insured', иначе
    по старому полю 'd' (для записей, созданных до этого обновления)."""
    ins = v.get("insured")
    if isinstance(ins, bool):
        return ins
    d = v.get("d")
    if isinstance(d, (int, float)) and d >= 2:
        return False
    return True

def compute_current_pd(pd, scan_ts, d_val, drop_at, now=None):
    """PD на текущий момент: убывает на d_val на каждой КРУГЛОЙ границе часа
    (00 минут МСК) с момента скана — включая 05:00, PD списывается и в этот
    час тоже. Особенность только в том, что сам слёт (объект реально
    пропадает) в 05:00 не происходит из-за рестарта сервера — это
    учитывается отдельно в _add_valid_hours при расчёте времени "Слёт",
    здесь же просто честный подсчёт текущего PD.

    Раньше списание отсчитывалось от самого scan_ts шагами по 3600 секунд —
    то есть если скан пришёл в 14:35, списания происходили в 15:35, 16:35 и
    т.д., а не в 15:00/16:00 как должно быть по игре. Теперь считаем именно
    круглые часовые границы (синхронно с той же логикой в bot.py)."""
    if now is None:
        now = int(time.time())
    floor_val = drop_at if drop_at is not None else 0
    if not scan_ts:
        return max(pd, floor_val)
    scan_ts = int(scan_ts)

    scan_msk = scan_ts + MSK_OFFSET
    ts = ((scan_msk // 3600) + 1) * 3600 - MSK_OFFSET  # ближайшая круглая граница часа МСК после скана

    elapsed_hours = 0
    while ts <= now:
        elapsed_hours += 1
        ts += 3600
    current = pd - d_val * elapsed_hours
    return max(current, floor_val)

def _add_valid_hours(start_ts, n):
    """Момент времени, когда PD дойдёт до порога слёта — n часовых списаний
    (КАЖДЫЙ час, включая 05:00 — PD там тоже списывается) вперёд от
    start_ts. Единственное исключение: если результат попадает ровно на
    05:00 МСК, реального слёта в этот час не происходит (сервер уходит на
    рестарт) — объект пропадает из списка только в 06:00, поэтому в этом
    случае момент сдвигается на час вперёд. Сам PD при этом продолжает
    считаться по обычным часам (см. compute_current_pd) — сдвигается
    только момент фактического "слёта"."""
    if n <= 0:
        return start_ts
    start_msk = start_ts + MSK_OFFSET
    ts = ((start_msk // 3600) + 1) * 3600 - MSK_OFFSET  # ближайшая круглая граница часа МСК после start_ts
    ts += (n - 1) * 3600  # n-я по счёту круглая граница часа

    hour_msk = (ts + MSK_OFFSET) // 3600 % 24
    if hour_msk == 5:
        ts += 3600  # слёта в 05:00 не бывает — реально в 06:00
    return ts

def calc_expiry_ts(pd, drop_at, now=None):
    if now is None:
        now = int(time.time())
    hours_left = max(pd - drop_at, 0)
    return _add_valid_hours(now, hours_left)

def calc_expiry_from_pdl(pd, d, l, now=None):
    if now is None:
        now = int(time.time())
    if d <= 0:
        d = 1
    diff = pd - l
    hours_left = max(-(-diff // d), 0) if diff > 0 else 0  # ceil(diff / d), сколько ровно часов нужно, без лишнего +1
    return _add_valid_hours(now, hours_left)

KEY_ALPHABET  = string.ascii_uppercase + string.digits
KEY_GROUP_LEN = 4
KEY_GROUPS    = 3

def generate_access_key():
    group = lambda: "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_GROUP_LEN))
    return "-".join(group() for _ in range(KEY_GROUPS))

def tg_send(chat_id, text):
    if not BOT_TOKEN:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        return r.ok
    except Exception:
        return False

def auto_cleanup():
    while True:
        time.sleep(3600)
        try:
            now   = int(time.time())
            limit = now - 48 * 3600
            ref   = db.reference("properties")
            data  = ref.get() or {}
            for srv, entries in data.items():
                if not isinstance(entries, dict):
                    continue
                to_delete = [
                    k for k, v in entries.items()
                    if isinstance(v, dict) and v.get("expiryTs", 0) < limit
                ]
                for k in to_delete:
                    db.reference(f"properties/{srv}/{k}").delete()
        except Exception as e:
            print(f"Cleanup error: {e}")

t = threading.Thread(target=auto_cleanup, daemon=True)
t.start()

@app.route("/update", methods=["POST"])
def update():
    secret = request.headers.get("X-Secret-Key", "")
    if SECRET_KEY and secret != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 403

    raw = request.get_data(as_text=False)
    try:
        data = json.loads(raw.decode('utf-8', errors='replace'))
    except Exception:
        return jsonify({"error": "parse error"}), 400

    if not data:
        return jsonify({"error": "no data"}), 400

    server  = data.get("server")
    entries = data.get("entries", [])

    if not server or not entries:
        return jsonify({"error": "missing fields"}), 400

    if server not in VALID_SERVERS:
        return jsonify({"error": "invalid server"}), 400

    now = int(time.time())
    ref = db.reference(f"properties/{server}")

    existing       = ref.get() or {}
    incoming_types = set(e["propType"] for e in entries)

    kept = {
        k: v for k, v in existing.items()
        if v.get("propType") not in incoming_types
        and v.get("expiryTs", 0) > now
    }

    # "Скан" = часовое окно (15:00, 16:00...). Держим только последние 3
    # таких окна — старые целиком удаляются. Если в каком-то окне
    # просканировали не все сервера — в нём и останутся только они.
    hour_bucket = (now // 3600) * 3600
    sessions_index = db.reference("sessions_index").get() or []
    if not isinstance(sessions_index, list):
        sessions_index = []
    if hour_bucket not in sessions_index:
        sessions_index.append(hour_bucket)
        sessions_index.sort()
        while len(sessions_index) > 3:
            old_bucket = sessions_index.pop(0)
            db.reference(f"sessions/{old_bucket}").delete()
        db.reference("sessions_index").set(sessions_index)
    session_writes = {}
    history_writes = {}

    excluded_ids = set(db.reference(f"exceptions/{server}").get(shallow=True) or {})

    # Замороженные объекты (Настройки → Замороженные): на некоторых
    # серверах есть дома/бизнесы, у которых PD зафиксирован и никогда не
    # падает. У них обычно нет отдельного ID, поэтому их нельзя исключить
    # по ID — вместо этого задаётся "на сервере X застряло N домов/бизнесов
    # с PD=Y", и мы пропускаем первые N встреченных объектов такого типа с
    # таким PD за скан, оставляя лишние (это реальные, не замороженные
    # объекты, просто оказавшиеся на том же PD в моменте).
    frozen_rules = db.reference(f"config/frozenHouses/{server}").get() or {}
    frozen_budget = {}
    if isinstance(frozen_rules, dict):
        for rule in frozen_rules.values():
            if isinstance(rule, dict):
                try:
                    r_pd, r_count = int(rule.get("pd")), int(rule.get("count"))
                except (TypeError, ValueError):
                    continue
                # Старые правила (созданы до поддержки бизнесов) не хранят
                # propType — считаем их домами, как было изначально.
                r_type = rule.get("propType") or "house"
                budget_key = (r_type, r_pd)
                frozen_budget[budget_key] = frozen_budget.get(budget_key, 0) + r_count
    frozen_consumed = {}

    # Для домов БЕЗ ID мы вынуждены опознавать "тот же дом между сканами"
    # по позиции в списке — а позиция ненадёжна: если хоть один дом в
    # списке слетел/появился, все позиции после него сдвигаются, и
    # "house_pos_3" в новом скане может оказаться уже совсем другим домом.
    # Раньше это иногда приводило к неверному автоопределению страховки.
    # Фикс: сравниваем и доверяем позиции только если размер списка домов
    # без ID на этом сервере не изменился между сканами — если изменился,
    # считаем идентичность неизвестной и не переносим ни статус страховки,
    # ни автоопределение со старой записи.
    pos_house_count_in_batch = sum(
        1 for x in entries if x.get("propType") == "house" and not x.get("propId")
    )

    written = 0
    for e in entries:
        pd = e.get("pd", 0)
        if pd > 65 or pd <= 0:
            continue
        if e.get("propType") not in ("house", "business"):
            continue

        budget_key = (e.get("propType"), pd)
        if budget_key in frozen_budget:
            consumed = frozen_consumed.get(budget_key, 0)
            if consumed < frozen_budget[budget_key]:
                frozen_consumed[budget_key] = consumed + 1
                continue  # замороженный объект — не добавляем

        prop_id = e.get("propId")
        pos     = e.get("pos")

        # Исключённые ID (Настройки → Исключения) никогда не попадают в слёты
        if prop_id and str(prop_id) in excluded_ids:
            continue

        # Ключ записи должен быть стабильным между сканами одного и того же
        # дома — иначе каждый скан плодит новую запись вместо обновления
        # старой. Для домов с ID — ключ по ID. Для домов без ID — по позиции
        # в списке (раньше ключ включал время слёта, из-за чего записи не
        # обновлялись, а копились дублями).
        if prop_id:
            key = f"{e['propType']}_{prop_id}"
        else:
            key = f"{e['propType']}_pos_{pos or 0}"

        # ------------------------------------------------------------
        # Автоопределение "застрахован / не застрахован": сравниваем новый
        # скан с предыдущим сканом ТОГО ЖЕ дома (по ID, либо по стабильной
        # позиции — см. ключ выше). Если PD упал ровно на 1×часы —
        # застрахован, ровно на 2×часы — не застрахован.
        #
        # Для домов С ID identity надёжна (ID не меняется) — доверяем
        # результату сразу. Для домов БЕЗ ID позиция может "уехать" на
        # другой физический дом даже при стабильном размере списка, поэтому
        # статус применяется только после DETECT_REQUIRED_STREAK
        # совпадающих определений подряд — одно случайное совпадение дельты
        # больше не может сразу поменять статус.
        # ------------------------------------------------------------
        old_rec = existing.get(key)

        # Для позиционных (без ID) домов доверяем old_rec, только если
        # список домов без ID на сервере не поменялся в размере — иначе
        # позиция могла "уехать" на другой физический дом.
        identity_stable = True
        if not prop_id and isinstance(old_rec, dict):
            old_batch_size = old_rec.get("posBatchSize")
            if old_batch_size is not None and old_batch_size != pos_house_count_in_batch:
                identity_stable = False

        prev_insured = old_rec.get("insured") if isinstance(old_rec, dict) and identity_stable else None
        prev_insured = prev_insured if isinstance(prev_insured, bool) else None

        client_insured = e.get("insured")
        client_insured = client_insured if isinstance(client_insured, bool) else None

        pairwise_detected = None  # результат ОДНОГО сравнения пары сканов
        if isinstance(old_rec, dict) and identity_stable:
            old_pd      = old_rec.get("pd")
            old_scan_ts = old_rec.get("scanTs")
            if isinstance(old_pd, (int, float)) and old_scan_ts:
                elapsed_hours = round((now - int(old_scan_ts)) / 3600)
                delta = int(old_pd) - pd
                if elapsed_hours >= 1 and delta > 0:
                    expected_insured   = elapsed_hours * D_INSURED
                    expected_uninsured = elapsed_hours * D_UNINSURED
                    if delta == expected_insured:
                        pairwise_detected = True
                    elif delta == expected_uninsured:
                        pairwise_detected = False
                    # Иначе (не совпадает ровно ни с одним из вариантов, либо
                    # физически невозможно > 2 PD/час) — не засчитываем.

        prev_candidate = old_rec.get("detectCandidate") if isinstance(old_rec, dict) and identity_stable else None
        prev_streak    = old_rec.get("detectStreak", 0) if isinstance(old_rec, dict) and identity_stable else 0
        if not isinstance(prev_streak, int):
            prev_streak = 0

        if prop_id:
            # ID надёжен — доверяем одиночному сравнению сразу
            new_candidate = pairwise_detected
            new_streak    = 1 if pairwise_detected is not None else 0
            detected_insured = pairwise_detected
        else:
            if pairwise_detected is None:
                new_candidate = prev_candidate
                new_streak    = prev_streak
            elif pairwise_detected == prev_candidate:
                new_candidate = prev_candidate
                new_streak    = prev_streak + 1
            else:
                new_candidate = pairwise_detected
                new_streak    = 1
            detected_insured = new_candidate if new_streak >= DETECT_REQUIRED_STREAK else None

        if client_insured is not None:
            insured = client_insured
        elif detected_insured is not None:
            insured = detected_insured
        elif prev_insured is not None:
            insured = prev_insured
        else:
            insured = True  # новый дом, доказательств пока нет — по умолчанию застрахован

        d_val   = D_INSURED if insured else D_UNINSURED
        drop_at = get_drop_at(server, e["propType"], insured)
        expiry_h = calc_expiry_from_pdl(pd, d_val, drop_at, now)
        if expiry_h <= now:
            continue

        kept[key] = {
            "server":   server,
            "propType": e["propType"],
            "pd":       pd,
            "expiryTs": expiry_h,
            "scanTs":   now,
            "propId":   prop_id,
            "pos":      pos,
            "insured":  insured,
            "dropAt":   drop_at,
            "d":        d_val,
            "count":    1,
            "posBatchSize": pos_house_count_in_batch if (not prop_id and e["propType"] == "house") else None,
            "detectCandidate": new_candidate,
            "detectStreak": new_streak,
        }
        written += 1

        # Снимок для "Риелторки" → режим "Снэпшоты" (последние 3 часовых
        # окна, все сервера разом — как раунд обхода серверов).
        session_writes[key] = {
            "propType": e["propType"], "pd": pd, "insured": insured,
            "propId": prop_id, "pos": pos, "scanTs": now,
        }

        # Для "Риелторки" → режим "История": для домов без ID отдельно
        # копим историю сканов по каждой позиции (48ч) — чтобы можно было
        # сравнить любые два произвольных скана, а не только соседние
        # часовые окна.
        #
        # Раньше здесь на КАЖДУЮ запись без ID синхронно делался ещё и
        # .get() ВСЕЙ истории по ключу + цикл .delete() старых записей —
        # при скане из 14+ домов это было 14+ полных чтений/удалений
        # подряд одно за другим. Из-за этого /update иногда не укладывался
        # в 5-секундный таймаут клиента и просто "зависал" без ответа —
        # клиент не понимал, дошло ли вообще что-то до сервера, а
        # "История" из-за этого выглядела так, будто ничего не
        # сохраняется. Очистка старых записей и так уже происходит при
        # каждом открытии "Истории" (см. _render_history_mode) — здесь
        # оставляем только лёгкую запись, без чтения и удаления.
        if not prop_id:
            history_writes[f"{key}/{now}"] = {"pd": pd}

    ref.set(kept)
    if session_writes:
        db.reference(f"sessions/{hour_bucket}/{server}").update(session_writes)
    if history_writes:
        # Один запрос с составными ("a/b") ключами вместо запроса на
        # каждую позицию по отдельности — Firebase Admin SDK трактует
        # такие ключи как multi-path patch update.
        db.reference(f"history/{server}").update(history_writes)
    return jsonify({"ok": True, "written": written})



@app.route("/list", methods=["GET"])
def list_props():
    now           = int(time.time())
    server_filter = request.args.get("server")
    hours_max     = request.args.get("hours")
    ref           = db.reference("properties")
    data          = ref.get() or {}
    result        = []
    for srv, entries in data.items():
        if server_filter and srv != server_filter:
            continue
        if not isinstance(entries, dict):
            continue
        for k, v in entries.items():
            expiry = v.get("expiryTs", 0)
            if expiry <= now:
                continue
            hours_left = (expiry - now) / 3600
            if hours_max and hours_left > float(hours_max):
                continue
            result.append({
                "server":    srv,
                "propType":  v.get("propType"),
                "pd":        v.get("pd"),
                "expiryTs":  expiry,
                "hoursLeft": round(hours_left, 1),
            })
    result.sort(key=lambda x: x["expiryTs"])
    return jsonify(result)

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"ok": True})

@app.route("/time", methods=["GET"])
def get_time():
    return jsonify({"utc": int(time.time())})

# ADMIN PANEL
def login_required(f):
    """Пускает любого залогиненного — и админа, и редактора."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper

def admin_only(f):
    """Пускает только главного админа — редакторам сюда нельзя."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        if session.get("role") != "admin":
            flash_msg("err", "Недостаточно прав для этого раздела")
            return redirect(url_for("admin_properties"))
        return f(*args, **kwargs)
    return wrapper

def current_role():
    return session.get("role", "admin")

def is_admin_role():
    return current_role() == "admin"

def allowed_servers_for_current_user():
    """Множество серверов, которые текущий пользователь может видеть/редактировать.
    Для админа — все, для редактора — только назначенные ему."""
    if is_admin_role():
        return set(VALID_SERVERS)
    return set(session.get("editor_servers", []))

def find_editor_by_username(username):
    data = db.reference("editors").get() or {}
    for eid, e in data.items():
        if isinstance(e, dict) and e.get("username", "").lower() == username.strip().lower():
            e = dict(e)
            e["id"] = eid
            return e
    return None

BASE_CSS = """
<style>
:root{
  --bg:#0d0f14; --surface:#161923; --surface2:#1e2230; --border:#2a2f3f;
  --text:#e6e8ef; --muted:#8b91a5; --accent:#6ee7d4; --accent2:#a78bfa;
  --danger:#f47174; --ok:#6ee7d4;
}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;margin:0}
a{color:var(--accent);text-decoration:none}
.nav{display:flex;gap:4px;background:var(--surface);padding:12px 20px;border-bottom:1px solid var(--border);flex-wrap:wrap;align-items:center}
.nav .brand{font-weight:700;margin-right:24px;color:var(--text)}
.nav a{padding:8px 14px;border-radius:8px;color:var(--muted);font-size:14px}
.nav a.active,.nav a:hover{background:var(--surface2);color:var(--text)}
.nav .spacer{flex:1}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px}
h1{font-size:22px;margin:0 0 20px}
h2{font-size:16px;color:var(--muted);font-weight:600;margin:28px 0 12px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px}
.stat .n{font-size:26px;font-weight:700;color:var(--accent)}
.stat .l{color:var(--muted);font-size:13px;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border)}
td{padding:8px 10px;border-bottom:1px solid var(--border)}
tr:hover td{background:var(--surface2)}
input,select,button,textarea{
  background:var(--surface2);border:1px solid var(--border);color:var(--text);
  padding:9px 12px;border-radius:8px;font-size:14px;font-family:inherit
}
input[type=checkbox],input[type=radio]{
  width:16px;height:16px;padding:0;background:none;border:1px solid var(--border);
  accent-color:var(--accent);border-radius:4px;vertical-align:middle
}
textarea{width:100%;min-height:120px;resize:vertical}
button{cursor:pointer;background:var(--accent);color:#0d0f14;font-weight:600;border:none}
button:hover{opacity:.88}
button.danger{background:var(--danger)}
button.ghost{background:transparent;border:1px solid var(--border);color:var(--text)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.pill.ok{background:rgba(110,231,212,.15);color:var(--accent)}
.pill.bad{background:rgba(244,113,116,.15);color:var(--danger)}
.pill.muted{background:rgba(139,145,165,.15);color:var(--muted)}
.flash{padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px}
.flash.ok{background:rgba(110,231,212,.12);color:var(--accent);border:1px solid rgba(110,231,212,.3)}
.flash.err{background:rgba(244,113,116,.12);color:var(--danger);border:1px solid rgba(244,113,116,.3)}
.login-box{max-width:340px;margin:120px auto;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:32px}
.login-box input{width:100%;margin-bottom:12px}
.login-box button{width:100%}
.mono{font-family:'Consolas','Courier New',monospace}
form.inline{display:inline}
#load-bar{position:fixed;top:0;left:0;height:2px;width:0;background:var(--accent);z-index:9999;transition:width .2s ease,opacity .3s ease;opacity:0}
#load-bar.active{opacity:1}
#page-content{animation:pageFadeIn .22s ease}
@keyframes pageFadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
</style>
"""

NAV_SCRIPT = """
<div id="load-bar"></div>
<script>
(function(){
  var bar = document.getElementById('load-bar');
  var content = document.getElementById('page-content');
  var barTimer = null;

  function showBar(){
    clearTimeout(barTimer);
    bar.style.width = '0%';
    bar.classList.add('active');
    requestAnimationFrame(function(){ bar.style.width = '70%'; });
  }
  function hideBar(){
    bar.style.width = '100%';
    barTimer = setTimeout(function(){
      bar.classList.remove('active');
      bar.style.width = '0%';
    }, 200);
  }

  function execScripts(container){
    var scripts = container.querySelectorAll('script');
    scripts.forEach(function(old){
      var s = document.createElement('script');
      for (var i=0;i<old.attributes.length;i++){
        s.setAttribute(old.attributes[i].name, old.attributes[i].value);
      }
      s.textContent = old.textContent;
      old.parentNode.replaceChild(s, old);
    });
  }

  function swap(html, url){
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var newContent = doc.getElementById('page-content');
    if (!newContent){ window.location.href = url; return; }

    var newTitle = doc.querySelector('title');
    if (newTitle) document.title = newTitle.textContent;

    content.innerHTML = newContent.innerHTML;
    content.style.animation = 'none';
    void content.offsetWidth;
    content.style.animation = '';

    var newNav = doc.querySelector('.nav');
    if (newNav){
      document.querySelectorAll('.nav a').forEach(function(a){ a.classList.remove('active'); });
      var activeA = newNav.querySelector('a.active');
      if (activeA){
        var href = activeA.getAttribute('href');
        document.querySelectorAll('.nav a').forEach(function(a){
          if (a.getAttribute('href') === href) a.classList.add('active');
        });
      }
    }
    execScripts(content);
    window.scrollTo(0,0);
  }

  function go(url, opts, push){
    push = push !== false;
    showBar();
    fetch(url, Object.assign({headers:{'X-Requested-With':'fetch'}}, opts||{}))
      .then(function(r){
        var finalUrl = r.url || url;
        return r.text().then(function(html){ return {html:html, url:finalUrl, ok:r.ok}; });
      })
      .then(function(res){
        hideBar();
        if (!res.ok){ window.location.href = res.url; return; }
        swap(res.html, res.url);
        if (push) history.pushState({url:res.url}, '', res.url);
      })
      .catch(function(){ hideBar(); window.location.href = url; });
  }

  document.addEventListener('click', function(e){
    var a = e.target.closest('a[href]');
    if (!a) return;
    if (a.hasAttribute('download') || a.target === '_blank') return;
    var href = a.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('mailto:') || href.startsWith('tel:') || href.startsWith('javascript:')) return;
    var url;
    try { url = new URL(href, window.location.href); } catch(err) { return; }
    if (url.origin !== window.location.origin) return;
    e.preventDefault();
    go(url.href);
  });

  document.addEventListener('submit', function(e){
    if (e.defaultPrevented) return;
    var form = e.target;
    if (!form || form.tagName !== 'FORM') return;
    var method = (form.getAttribute('method') || 'GET').toUpperCase();
    var action = form.getAttribute('action') || window.location.href;
    var url;
    try { url = new URL(action, window.location.href); } catch(err) { return; }
    if (url.origin !== window.location.origin) return;
    e.preventDefault();
    if (method === 'GET'){
      url.search = new URLSearchParams(new FormData(form)).toString();
      go(url.href);
    } else {
      go(url.href, {method:'POST', body:new FormData(form)});
    }
  });

  window.addEventListener('popstate', function(){
    go(window.location.href, {}, false);
  });
})();
</script>
"""

def render_page(title, active, body):
    if current_role() == "editor":
        nav_items = [("properties", "Слёты"), ("expired", "Истёкшие"), ("realtor", "Риелторка")]
    else:
        nav_items = [
            ("dashboard", "Дашборд"), ("keys", "Ключи"), ("users", "Пользователи"),
            ("properties", "Слёты"), ("expired", "Истёкшие"), ("settings", "Настройки"),
            ("editors", "Редакторы"), ("realtor", "Риелторка"), ("estates", "Поместья"), ("broadcast", "Рассылка"),
        ]
    nav = "".join(
        f'<a href="{url_for("admin_"+ep)}" class="{"active" if active==ep else ""}">{label}</a>'
        for ep, label in nav_items
    )
    flashes = ""
    for kind, msg in session.pop("_flashes", []):
        flashes += f'<div class="flash {kind}">{msg}</div>'
    if current_role() == "editor":
        who = f'<span style="color:var(--muted);font-size:13px;margin-right:14px">{session.get("editor_username","")} · редактор</span>'
    else:
        who = ""
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Arizona Tracker Admin</title>{BASE_CSS}</head><body>
<div class="nav">
  <span class="brand">🏙 Arizona Tracker</span>
  {nav}
  <span class="spacer"></span>
  {who}
  <a href="{url_for('admin_logout')}">Выйти</a>
</div>
<div class="wrap" id="page-content"><div id="flashes">{flashes}</div>{body}</div>
{NAV_SCRIPT}
</body></html>"""

def flash_msg(kind, msg):
    flashes = session.get("_flashes", [])
    flashes.append((kind, msg))
    session["_flashes"] = flashes

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")

        is_super_admin = (
            ADMIN_PASSWORD and
            secrets.compare_digest(u, ADMIN_USERNAME) and
            secrets.compare_digest(p, ADMIN_PASSWORD)
        )

        if is_super_admin:
            session.clear()
            session.permanent = True
            session["admin_logged_in"] = True
            session["role"] = "admin"
            return redirect(url_for("admin_dashboard"))

        editor = find_editor_by_username(u) if u else None
        if editor and editor.get("active", True) and editor.get("password_hash") and check_password_hash(editor["password_hash"], p):
            session.clear()
            session.permanent = True
            session["admin_logged_in"] = True
            session["role"] = "editor"
            session["editor_id"] = editor["id"]
            session["editor_username"] = editor.get("username", "")
            session["editor_servers"] = [s for s in editor.get("servers", []) if s in VALID_SERVERS]
            return redirect(url_for("admin_properties"))

        error = '<div class="flash err">Неверный логин или пароль</div>'
    else:
        error = ""
    body = f"""
    <div class="login-box">
      <h1>🔑 Вход в админку</h1>
      {error}
      <form method="post">
        <input name="username" placeholder="Логин" autofocus>
        <input name="password" type="password" placeholder="Пароль">
        <button type="submit">Войти</button>
      </form>
    </div>"""
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — Arizona Tracker Admin</title>{BASE_CSS}</head><body>{body}</body></html>"""

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/admin")
@login_required
def admin_root():
    if current_role() == "editor":
        return redirect(url_for("admin_properties"))
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/dashboard")
@admin_only
def admin_dashboard():
    now          = int(time.time())
    users_data   = db.reference("users").get() or {}
    banned_data  = db.reference("banned").get() or {}
    subs_data    = db.reference("subscriptions").get() or {}
    keys_data    = db.reference("access_keys").get() or {}
    props_data   = db.reference("properties").get() or {}

    active_subs  = sum(1 for v in subs_data.values() if isinstance(v, dict) and v.get("expires_at", 0) > now)
    unused_keys  = sum(1 for v in keys_data.values() if isinstance(v, dict) and not v.get("activated"))
    prop_count   = sum(len(v) for v in props_data.values() if isinstance(v, dict))

    stats = [
        (len(users_data), "Пользователей"),
        (active_subs, "Активных подписок"),
        (len(banned_data), "Забанено"),
        (unused_keys, "Неиспользованных ключей"),
        (prop_count, "Записей о слётах"),
    ]
    grid = "".join(f'<div class="stat"><div class="n">{n}</div><div class="l">{l}</div></div>' for n, l in stats)
    body = f"<h1>Дашборд</h1><div class='grid'>{grid}</div>"
    return render_page("Дашборд", "dashboard", body)

@app.route("/admin/keys")
@admin_only
def admin_keys():
    now  = int(time.time())
    data = db.reference("access_keys").get() or {}

    # Автоочистка: если ключ был активирован и выданный им доступ уже
    # полностью истёк — ключ больше не нужен, удаляем его из базы.
    for key, v in list(data.items()):
        if not isinstance(v, dict):
            continue
        if v.get("activated"):
            activated_at = v.get("activated_at") or v.get("created_at", 0)
            duration     = int(v.get("duration_days", 0)) * 86400
            if activated_at + duration < now:
                db.reference(f"access_keys/{key}").delete()
                del data[key]

    rows = ""
    for key, v in sorted(data.items(), key=lambda kv: kv[1].get("created_at", 0) if isinstance(kv[1], dict) else 0, reverse=True):
        if not isinstance(v, dict):
            continue
        activated = v.get("activated")
        status    = '<span class="pill ok">использован</span>' if activated else '<span class="pill muted">свободен</span>'
        used_by   = v.get("activated_by", "—")
        created   = format_msk(v.get("created_at", 0), "%d.%m.%Y %H:%M")
        rows += (
            f"<tr><td class='mono'>{key}</td><td>{v.get('duration_days','?')} дн.</td>"
            f"<td>{status}</td><td>{used_by}</td><td>{created}</td>"
            f"<td><form class='inline' method='post' action='{url_for('admin_key_revoke', key=key)}' "
            f"onsubmit=\"return confirm('Удалить ключ {key}?')\">"
            f"<button class='danger' type='submit'>Удалить</button></form></td></tr>"
        )

    body = f"""
    <h1>Ключи доступа</h1>
    <div class="card">
      <h2 style="margin-top:0">Сгенерировать ключи</h2>
      <form method="post" action="{url_for('admin_key_generate')}" class="row">
        <input type="number" name="days" placeholder="Дней" min="1" required style="width:100px">
        <input type="number" name="count" placeholder="Количество" min="1" max="50" value="1" style="width:130px">
        <button type="submit">Сгенерировать</button>
      </form>
    </div>
    <p style="color:var(--muted);font-size:13px">
      Использованные ключи удаляются автоматически, как только выданный ими доступ истекает.
      Управление доступом (продлить/уменьшить/забрать) — во вкладке «Пользователи».
    </p>
    <table>
      <tr><th>Ключ</th><th>Срок</th><th>Статус</th><th>Активировал</th><th>Создан</th><th></th></tr>
      {rows or "<tr><td colspan='6'>Ключей пока нет</td></tr>"}
    </table>"""
    return render_page("Ключи", "keys", body)

@app.route("/admin/keys/generate", methods=["POST"])
@admin_only
def admin_key_generate():
    try:
        days  = int(request.form.get("days", 0))
        count = int(request.form.get("count", 1))
    except ValueError:
        flash_msg("err", "Неверные параметры")
        return redirect(url_for("admin_keys"))
    if days <= 0 or count <= 0 or count > 50:
        flash_msg("err", "Дни > 0, количество от 1 до 50")
        return redirect(url_for("admin_keys"))

    now = int(time.time())
    for _ in range(count):
        key = generate_access_key()
        db.reference(f"access_keys/{key}").set({
            "duration_days": days, "created_at": now,
            "created_by": "admin_panel", "activated": False,
        })
    flash_msg("ok", f"Создано ключей: {count} (на {days} дн.)")
    return redirect(url_for("admin_keys"))

@app.route("/admin/keys/<key>/revoke", methods=["POST"])
@admin_only
def admin_key_revoke(key):
    ref  = db.reference(f"access_keys/{key}")
    data = ref.get()
    if data and not data.get("activated"):
        ref.delete()
        flash_msg("ok", f"Ключ {key} удалён")
    else:
        flash_msg("err", "Ключ уже использован или не найден")
    return redirect(url_for("admin_keys"))

@app.route("/admin/users")
@admin_only
def admin_users():
    now         = int(time.time())
    users_data  = db.reference("users").get() or {}
    subs_data   = db.reference("subscriptions").get() or {}
    banned_data = db.reference("banned").get() or {}
    q = request.args.get("q", "").strip().lower()

    all_ids = set(users_data.keys()) | set(subs_data.keys()) | set(str(k) for k in banned_data.keys())
    rows = ""
    for uid in sorted(all_ids, key=lambda x: str(x)):
        u    = users_data.get(uid, {}) if isinstance(users_data.get(uid), dict) else {}
        sub  = subs_data.get(uid, {}) if isinstance(subs_data.get(uid), dict) else {}
        is_banned = str(uid) in {str(k) for k in banned_data.keys()}
        uname = u.get("username", "")
        name  = u.get("name", "")

        if q and q not in str(uid) and q not in uname.lower() and q not in name.lower():
            continue

        exp = sub.get("expires_at")
        if exp and exp > now:
            access = f'<span class="pill ok">до {format_msk(exp, "%d.%m.%Y %H:%M")}</span>'
        elif exp:
            access = '<span class="pill bad">истёк</span>'
        else:
            access = '<span class="pill muted">нет</span>'

        key_used = sub.get("key") if isinstance(sub, dict) else None
        key_html = f'<span class="mono" style="font-size:12px">{key_used}</span>' if key_used else '<span style="color:var(--muted)">—</span>'

        ban_pill = '<span class="pill bad">забанен</span>' if is_banned else ""
        ban_btn  = (
            f'<form class="inline" method="post" action="{url_for("admin_user_unban", uid=uid)}">'
            f'<button class="ghost" type="submit" style="padding:4px 7px;font-size:12px;white-space:nowrap">Разбанить</button></form>'
            if is_banned else
            f'<form class="inline" method="post" action="{url_for("admin_user_ban", uid=uid)}" '
            f'onsubmit="return prompt(\'Причина бана:\')!=null">'
            f'<input type="hidden" name="reason" value="через админку">'
            f'<button class="danger" type="submit" style="padding:4px 7px;font-size:12px;white-space:nowrap">Забанить</button></form>'
        )

        uname_clean = uname.lstrip("@")
        uname_html = f'<a href="https://t.me/{uname_clean}" target="_blank" style="color:var(--accent)">{uname}</a>' if uname_clean else '<span style="color:var(--muted)">—</span>'

        rows += f"""<tr>
          <td class='mono' style="max-width:100px;overflow-wrap:break-word">{uid}</td>
          <td style="max-width:130px;overflow-wrap:break-word">{uname_html}</td>
          <td style="max-width:140px;overflow-wrap:break-word">{name}</td>
          <td style="max-width:120px;overflow-wrap:break-word">{key_html}</td>
          <td style="white-space:nowrap">{access} {ban_pill}</td>
          <td style="white-space:nowrap">{u.get('last_seen','—')}</td>
          <td>
            <div style="display:flex;flex-wrap:wrap;gap:4px;max-width:230px">
              <form class="inline" method="post" action="{url_for('admin_user_extend', uid=uid)}" style="display:flex;gap:3px">
                <input type="number" name="days" min="1" placeholder="дн" style="width:46px;padding:4px 5px;font-size:12px">
                <button type="submit" class="ghost" style="padding:4px 6px;font-size:12px;white-space:nowrap">Продлить</button>
              </form>
              <form class="inline" method="post" action="{url_for('admin_user_reduce', uid=uid)}" style="display:flex;gap:3px">
                <input type="number" name="days" min="1" placeholder="дн" style="width:46px;padding:4px 5px;font-size:12px">
                <button type="submit" class="ghost" style="padding:4px 6px;font-size:12px;white-space:nowrap">Уменьшить</button>
              </form>
              <form class="inline" method="post" action="{url_for('admin_user_revoke', uid=uid)}" onsubmit="return confirm('Забрать доступ у {uid}?')">
                <button type="submit" class="danger" style="padding:4px 6px;font-size:12px;white-space:nowrap">Забрать</button>
              </form>
              {ban_btn}
            </div>
          </td>
        </tr>"""

    body = f"""
    <h1>Пользователи</h1>
    <div class="card">
      <form method="get" class="row">
        <input type="text" name="q" placeholder="Поиск по ID / нику / имени" value="{q}">
        <button type="submit">Найти</button>
      </form>
    </div>
    <table style="table-layout:fixed">
      <tr><th>ID</th><th>Ник</th><th>Имя</th><th>Ключ</th><th>Доступ</th><th>Был(а)</th><th>Действия</th></tr>
      {rows or "<tr><td colspan='7'>Никого не найдено</td></tr>"}
    </table>"""
    return render_page("Пользователи", "users", body)

@app.route("/admin/users/<uid>/extend", methods=["POST"])
@admin_only
def admin_user_extend(uid):
    try:
        days = int(request.form.get("days", 0))
    except ValueError:
        days = 0
    if days <= 0:
        flash_msg("err", "Укажи число дней больше нуля")
        return redirect(url_for("admin_keys"))

    now     = int(time.time())
    sub     = db.reference(f"subscriptions/{uid}").get() or {}
    current = sub.get("expires_at", 0) if isinstance(sub, dict) else 0
    base    = max(now, current)
    expires_at = base + days * 86400
    db.reference(f"subscriptions/{uid}").set({
        "key": "admin_panel", "expires_at": expires_at, "activated_at": now,
    })
    tg_send(int(uid), f"⏳ Твой доступ продлён администратором до {format_msk(expires_at, '%d.%m.%Y %H:%M')} МСК.")
    flash_msg("ok", f"Доступ для {uid} продлён на {days} дн.")
    return redirect(url_for("admin_keys"))

@app.route("/admin/users/<uid>/reduce", methods=["POST"])
@admin_only
def admin_user_reduce(uid):
    try:
        days = int(request.form.get("days", 0))
    except ValueError:
        days = 0
    if days <= 0:
        flash_msg("err", "Укажи число дней больше нуля")
        return redirect(url_for("admin_keys"))

    now = int(time.time())
    sub = db.reference(f"subscriptions/{uid}").get() or {}
    current = sub.get("expires_at", 0) if isinstance(sub, dict) else 0
    new_expiry = current - days * 86400

    if new_expiry <= now:
        db.reference(f"subscriptions/{uid}").delete()
        tg_send(int(uid), "⛔️ Твой доступ к боту был отозван администратором.")
        flash_msg("ok", f"Доступ для {uid} уменьшен и полностью истёк")
    else:
        db.reference(f"subscriptions/{uid}").update({"expires_at": new_expiry})
        tg_send(int(uid), f"⏳ Твой доступ уменьшен администратором. Действует до {format_msk(new_expiry, '%d.%m.%Y %H:%M')} МСК.")
        flash_msg("ok", f"Доступ для {uid} уменьшен на {days} дн.")
    return redirect(url_for("admin_keys"))

@app.route("/admin/users/<uid>/revoke", methods=["POST"])
@admin_only
def admin_user_revoke(uid):
    db.reference(f"subscriptions/{uid}").delete()
    tg_send(int(uid), "⛔️ Твой доступ к боту был отозван администратором.")
    flash_msg("ok", f"Доступ для {uid} отозван")
    return redirect(url_for("admin_keys"))

@app.route("/admin/users/<uid>/ban", methods=["POST"])
@admin_only
def admin_user_ban(uid):
    reason  = request.form.get("reason", "через админку")
    now_str = format_msk(time.time(), "%d.%m.%Y %H:%M") + " МСК"
    db.reference(f"banned/{uid}").set({"reason": reason, "date": now_str})
    flash_msg("ok", f"Пользователь {uid} забанен")
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<uid>/unban", methods=["POST"])
@admin_only
def admin_user_unban(uid):
    db.reference(f"banned/{uid}").delete()
    flash_msg("ok", f"Пользователь {uid} разбанен")
    return redirect(url_for("admin_users"))

# Properties functions
def _collect_property_entries(srv_filter, allowed=None):
    """Собирает все записи properties из Firebase, опционально фильтруя по серверу
    и по множеству разрешённых серверов (для роли редактора).
    Возвращает список кортежей (server, key, value_dict).
    """
    data = db.reference("properties").get() or {}
    all_entries = []
    for srv, entries in data.items():
        if not isinstance(entries, dict):
            continue
        if srv_filter and srv != srv_filter:
            continue
        if allowed is not None and srv not in allowed:
            continue
        for k, v in entries.items():
            if not isinstance(v, dict):
                continue
            all_entries.append((srv, k, v))
    return all_entries

def _render_property_rows(entries, back_endpoint="admin_properties", dropat_cfg=None, allowed=None):
    now = int(time.time())
    if dropat_cfg is None:
        dropat_cfg = {}
    rows = ""
    # Кэшируем HTML списка серверов на строку, чтобы не пересобирать 32 <option>
    # заново для каждой записи — только когда меняется выбранный сервер.
    server_opts_cache = {}
    for i, (srv, k, v) in enumerate(entries):
        expiry = v.get("expiryTs", 0)
        expired = expiry <= now
        when = format_msk(expiry, "%d.%m.%Y %H:%M") if expiry else "—"
        dt_local = _dt.datetime.fromtimestamp(expiry, tz=MSK_TZ).strftime("%Y-%m-%dT%H:%M") if expiry else ""
        status = '<span class="pill bad">истёк</span>' if expired else '<span class="pill ok">активен</span>'
        row_id = f"pr{i}"

        del_btn = (
            f'<form class="inline" method="post" action="{url_for("admin_property_delete", server=srv, key=k)}" '
            f'onsubmit="return confirm(\'Удалить запись?\')">'
            f'<input type="hidden" name="back" value="{back_endpoint}">'
            f'<button class="danger" type="submit">Удалить</button></form>'
        )
        edit_btn = f'<button class="ghost" type="button" onclick="toggleEdit(\'{row_id}\')">Изменить</button>'

        insured = infer_insured(v)
        insured_pill = '<span class="pill ok">🛡 Страхован</span>' if insured else '<span class="pill bad">🚫 Не страхован</span>'
        drop_val = v.get("dropAt")
        drop_default = drop_val if drop_val is not None else get_drop_at_cached(dropat_cfg, srv, v.get("propType", "house"), insured)

        d_val = D_INSURED if insured else D_UNINSURED
        base_pd = v.get("pd", 0)
        current_pd = compute_current_pd(base_pd, v.get("scanTs"), d_val, drop_default, now)
        pd_display = str(current_pd) if current_pd == base_pd else f"{current_pd} <span style='color:var(--muted)'>(было {base_pd})</span>"

        # Checkbox hidden by default; shown only after pressing "Изменить выбранные"
        checkbox = f'<input type="checkbox" class="sel-row" name="selected" value="{srv}|{k}" style="display:none">'

        rows += (
            f"<tr id='view-{row_id}'><td style='width:36px'>{checkbox}</td>"
            f"<td>{server_label(srv)}</td><td>{v.get('propType','?')}</td><td>{pd_display}</td>"
            f"<td>{insured_pill}</td><td>{v.get('propId') or v.get('pos') or '—'}</td><td>{drop_default}</td>"
            f"<td>{when}</td><td>{status}</td><td class='row'>{edit_btn}{del_btn}</td></tr>"
        )

        if srv not in server_opts_cache:
            # Редактору нельзя переносить запись на сервер вне его прав —
            # список серверов для смены ограничен его набором (+ текущий сервер записи)
            selectable = [s for s in SERVER_ORDER if s in VALID_SERVERS and (allowed is None or s in allowed or s == srv)]
            server_opts_cache[srv] = "".join(
                f'<option value="{s}" {"selected" if s == srv else ""}>{server_label(s)}</option>'
                for s in selectable
            )
        server_opts = server_opts_cache[srv]
        rows += f"""
        <tr id='edit-{row_id}' style='display:none'>
          <td colspan='10'>
            <form method="post" action="{url_for('admin_property_update', server=srv, key=k)}" class="row" style="flex-wrap:wrap;gap:8px;align-items:center">
              <input type="hidden" name="back" value="{back_endpoint}">
              <select name="server">{server_opts}</select>
              <select name="propType">
                <option value="house" {"selected" if v.get("propType")=="house" else ""}>🏠 house</option>
                <option value="business" {"selected" if v.get("propType")=="business" else ""}>🏢 business</option>
              </select>
              <input type="number" name="pd" value="{v.get('pd', 0)}" min="1" max="65" required style="width:90px" title="PayDay">
              <select name="insured" title="Застрахованные дома теряют 1 PD/час, незастрахованные — 2 PD/час">
                <option value="1" {"selected" if insured else ""}>🛡 Страхован</option>
                <option value="0" {"selected" if not insured else ""}>🚫 Не страхован</option>
              </select>
              <input type="text" name="propId" placeholder="ID" value="{v.get('propId') or ''}" style="width:120px">
              <input type="text" name="pos" placeholder="Позиция" value="{v.get('pos') or ''}" style="width:120px">
              <label style="color:var(--muted);font-size:13px;display:flex;align-items:center;gap:4px">
                <input type="checkbox" name="manual_time"> вручную
              </label>
              <input type="datetime-local" name="expiry" value="{dt_local}" title="Используется только если отмечено 'вручную'">
              <button type="submit">Сохранить</button>
              <button type="button" class="ghost" onclick="toggleEdit('{row_id}')">Отмена</button>
            </form>
          </td>
        </tr>"""
    return rows

TOGGLE_EDIT_SCRIPT = """
    <script>
    function toggleEdit(id) {
      var view = document.getElementById('view-' + id);
      var edit = document.getElementById('edit-' + id);
      if (!view || !edit) return;
      var editing = edit.style.display !== 'none';
      view.style.display = editing ? '' : 'none';
      edit.style.display = editing ? 'none' : '';
    }
    </script>"""

def _render_batch_action_panel():
    return """
    <div class="card row" style="align-items:center;gap:16px;padding:14px 18px">
      <div class="row" style="gap:10px">
        <button id="edit-selected" class="ghost" type="button">Редактировать</button>
        <button id="delete-selected" class="danger" type="button">Удалить</button>
        <button id="cancel-selected" class="ghost" type="button" style="display:none">Отмена</button>
      </div>
      <div id="selection-hint" style="color:var(--muted);font-size:13px">Отметьте нужные строки в таблице, затем нажмите «Редактировать» или «Удалить»</div>
    </div>
    """

def _render_batch_js(edit_url, delete_url):
    return f"""
    <script>
    (function(){{
      var selectMode = false;
      var editBtn   = document.getElementById('edit-selected');
      var delBtn    = document.getElementById('delete-selected');
      var cancelBtn = document.getElementById('cancel-selected');
      var hint      = document.getElementById('selection-hint');

      function showCheckboxes(show){{
        document.querySelectorAll('.sel-row').forEach(function(cb){{
          cb.style.display = show ? 'inline-block' : 'none';
          if(!show) cb.checked = false;
        }});
      }}
      function getSelected(){{
        var vals=[];
        document.querySelectorAll('.sel-row:checked').forEach(function(cb){{ vals.push(cb.value); }});
        return vals;
      }}
      function enterSelectMode(){{
        selectMode = true;
        showCheckboxes(true);
        editBtn.textContent = 'Изменить';
        cancelBtn.style.display = 'inline-block';
        hint.textContent = 'Отметьте нужные строки, затем нажмите «Изменить» или «Удалить»';
      }}
      function exitSelectMode(){{
        selectMode = false;
        showCheckboxes(false);
        editBtn.textContent = 'Редактировать';
        cancelBtn.style.display = 'none';
        hint.textContent = 'Отметьте нужные строки в таблице, затем нажмите «Редактировать» или «Удалить»';
      }}
      function post(url, arr){{
        var form = document.createElement('form');
        form.method='post';
        form.action=url;
        arr.forEach(function(v){{
          var i=document.createElement('input'); i.type='hidden'; i.name='selected'; i.value=v; form.appendChild(i);
        }});
        document.body.appendChild(form);
        form.submit();
      }}

      editBtn.addEventListener('click', function(e){{
        e.preventDefault();
        if(!selectMode){{ enterSelectMode(); return; }}
        var s = getSelected();
        if(!s.length){{ alert('Отметьте хотя бы одну запись'); return; }}
        post("{edit_url}", s);
      }});

      delBtn.addEventListener('click', function(e){{
        e.preventDefault();
        if(!selectMode){{ enterSelectMode(); return; }}
        var s = getSelected();
        if(!s.length){{ alert('Отметьте хотя бы одну запись'); return; }}
        if(!confirm('Удалить выбранные записи (' + s.length + ')?')) return;
        post("{delete_url}", s);
      }});

      cancelBtn.addEventListener('click', function(e){{
        e.preventDefault();
        exitSelectMode();
      }});

      document.addEventListener('DOMContentLoaded', function(){{ showCheckboxes(false); }});
    }})();
    </script>
    """

@app.route("/admin/properties")
@login_required
def admin_properties():
    now        = int(time.time())
    srv_filter = request.args.get("server", "")

    allowed = allowed_servers_for_current_user()
    if current_role() == "editor" and srv_filter and srv_filter not in allowed:
        srv_filter = ""  # игнорируем фильтр по чужому серверу

    # Загружаем конфиг порогов ОДНИМ запросом, а не по одному на каждую запись —
    # раньше именно это (N+1 запросов к Firebase) тормозило открытие вкладки
    dropat_cfg = db.reference("config/dropAt").get() or {}

    collect_allowed = allowed if current_role() == "editor" else None
    all_entries = [e for e in _collect_property_entries(srv_filter, allowed=collect_allowed) if e[2].get("expiryTs", 0) > now]
    all_entries.sort(key=lambda item: item[2].get("expiryTs", 0))
    rows = _render_property_rows(all_entries, back_endpoint="admin_properties", dropat_cfg=dropat_cfg, allowed=collect_allowed)

    visible_servers = [s for s in SERVER_ORDER if s in VALID_SERVERS and s in allowed]
    server_options = "".join(
        f'<option value="{s}" {"selected" if s == srv_filter else ""}>{server_label(s)}</option>' for s in visible_servers
    )

    if current_role() == "editor" and not allowed:
        flash_msg("err", "Вам пока не назначены сервера — обратитесь к администратору")

    action_panel = _render_batch_action_panel()
    batch_js = _render_batch_js(url_for('admin_properties_batch_edit'), url_for('admin_properties_batch_delete'))

    body = f"""
    <h1>Данные о слётах</h1>
    <div class="card">
      <h2 style="margin-top:0">Добавить вручную</h2>
      <form method="post" action="{url_for('admin_property_add')}" class="row" style="flex-wrap:wrap;gap:8px">
        <select name="server" required><option value="">Сервер</option>{server_options}</select>
        <select name="propType" required>
          <option value="house">🏠 house</option>
          <option value="business">🏢 business</option>
        </select>
        <input type="number" name="pd" placeholder="PayDay" min="1" max="65" required style="width:100px">
        <select name="insured" title="Застрахованные дома теряют 1 PD/час, незастрахованные — 2 PD/час">
          <option value="1" selected>🛡 Страхован</option>
          <option value="0">🚫 Не страхован</option>
        </select>
        <input type="number" name="hours" placeholder="Часов до слёта (вручную)" min="0" step="0.1" style="width:200px">
        <input type="text" name="propId" placeholder="ID (необязательно)" style="width:150px">
        <button type="submit">Добавить</button>
        <div style="width:100%;color:var(--muted);font-size:12px">Укажите «Застрахован ли дом» — время слёта посчитается автоматически по PD и порогу сервера (задаётся в «Настройках»). Поле «Часов до слёта» заполняйте, только если хотите задать время вручную, в обход авторасчёта.</div>
      </form>
    </div>
    <div class="card row">
      <form method="get">
        <select name="server" onchange="this.form.requestSubmit()">
          <option value="">Все серверы</option>{server_options}
        </select>
      </form>
    </div>
    {action_panel}
    <table>
      <tr><th style="width:36px"></th><th>Сервер</th><th>Тип</th><th>PD (сейчас)</th><th>Страховка</th><th>ID/поз.</th><th>Порог</th><th>Слёт</th><th>Статус</th><th></th></tr>
      {rows or "<tr><td colspan='10'>Записей нет</td></tr>"}
    </table>
    {batch_js}
    {TOGGLE_EDIT_SCRIPT}
    """
    return render_page("Слёты", "properties", body)

@app.route("/admin/properties/expired")
@login_required
def admin_expired():
    now        = int(time.time())
    srv_filter = request.args.get("server", "")

    allowed = allowed_servers_for_current_user()
    if current_role() == "editor" and srv_filter and srv_filter not in allowed:
        srv_filter = ""

    dropat_cfg = db.reference("config/dropAt").get() or {}

    collect_allowed = allowed if current_role() == "editor" else None
    all_entries = [e for e in _collect_property_entries(srv_filter, allowed=collect_allowed) if e[2].get("expiryTs", 0) <= now]
    all_entries.sort(key=lambda item: item[2].get("expiryTs", 0), reverse=True)
    rows = _render_property_rows(all_entries, back_endpoint="admin_expired", dropat_cfg=dropat_cfg, allowed=collect_allowed)

    visible_servers = [s for s in SERVER_ORDER if s in VALID_SERVERS and s in allowed]
    server_options = "".join(
        f'<option value="{s}" {"selected" if s == srv_filter else ""}>{server_label(s)}</option>' for s in visible_servers
    )

    action_panel = _render_batch_action_panel()
    batch_js = _render_batch_js(url_for('admin_properties_batch_edit'), url_for('admin_properties_batch_delete'))

    body = f"""
    <h1>Истёкшие слёты</h1>
    <div class="card row">
      <form method="get">
        <select name="server" onchange="this.form.requestSubmit()">
          <option value="">Все серверы</option>{server_options}
        </select>
      </form>
    </div>
    {action_panel}
    <table>
      <tr><th style="width:36px"></th><th>Сервер</th><th>Тип</th><th>PD (сейчас)</th><th>Страховка</th><th>ID/поз.</th><th>Порог</th><th>Слёт</th><th>Статус</th><th></th></tr>
      {rows or "<tr><td colspan='10'>Истёкших записей нет</td></tr>"}
    </table>
    {batch_js}
    {TOGGLE_EDIT_SCRIPT}"""
    return render_page("Истёкшие", "expired", body)

@app.route("/admin/properties/add", methods=["POST"])
@login_required
def admin_property_add():
    server    = request.form.get("server", "")
    prop_type = request.form.get("propType", "")
    prop_id   = request.form.get("propId", "").strip()
    hours_raw = request.form.get("hours", "").strip()
    insured   = request.form.get("insured", "1") == "1"

    try:
        pd = int(request.form.get("pd", 0))
    except ValueError:
        flash_msg("err", "Неверное значение PayDay")
        return redirect(url_for("admin_properties"))

    if server not in VALID_SERVERS or prop_type not in ("house", "business"):
        flash_msg("err", "Неверный сервер или тип")
        return redirect(url_for("admin_properties"))
    if current_role() == "editor" and server not in allowed_servers_for_current_user():
        flash_msg("err", "У вас нет доступа к этому серверу")
        return redirect(url_for("admin_properties"))
    if pd <= 0 or pd > 65:
        flash_msg("err", "PayDay должен быть 1–65")
        return redirect(url_for("admin_properties"))

    now = int(time.time())
    d_val = D_INSURED if insured else D_UNINSURED

    if hours_raw:
        try:
            hours = float(hours_raw)
        except ValueError:
            flash_msg("err", "Неверное значение часов")
            return redirect(url_for("admin_properties"))
        if hours < 0:
            flash_msg("err", "Часы должны быть ≥ 0")
            return redirect(url_for("admin_properties"))
        expiry_h = ((now + int(hours * 3600)) // 3600) * 3600
        drop_at = None
    else:
        drop_at = get_drop_at(server, prop_type, insured)
        expiry_h = calc_expiry_from_pdl(pd, d_val, drop_at, now)

    key = f"{prop_type}_{prop_id}" if prop_id else f"{prop_type}_{expiry_h}_admin_{secrets.token_hex(3)}"

    db.reference(f"properties/{server}/{key}").set({
        "server": server, "propType": prop_type, "pd": pd, "expiryTs": expiry_h,
        "scanTs": now, "propId": prop_id or None, "pos": None,
        "insured": insured, "dropAt": drop_at, "d": d_val, "count": 1,
    })
    flash_msg("ok", "Запись добавлена")
    return redirect(url_for("admin_properties"))

@app.route("/admin/properties/<server>/<key>/delete", methods=["POST"])
@login_required
def admin_property_delete(server, key):
    back = request.form.get("back", "admin_properties")
    if back not in ("admin_properties", "admin_expired"):
        back = "admin_properties"
    if current_role() == "editor" and server not in allowed_servers_for_current_user():
        flash_msg("err", "У вас нет доступа к этому серверу")
        return redirect(url_for(back))
    db.reference(f"properties/{server}/{key}").delete()
    flash_msg("ok", "Запись удалена")
    return redirect(url_for(back))

@app.route("/admin/properties/batch_edit", methods=["POST"])
@login_required
def admin_properties_batch_edit():
    selected = request.form.getlist("selected")
    if not selected:
        flash_msg("err", "Ничего не выбрано")
        return redirect(url_for("admin_properties"))
    allowed = allowed_servers_for_current_user()
    entries = []
    skipped = 0
    for s in selected:
        try:
            srv, key = s.split("|", 1)
        except Exception:
            continue
        if current_role() == "editor" and srv not in allowed:
            skipped += 1
            continue
        v = db.reference(f"properties/{srv}/{key}").get()
        if not v or not isinstance(v, dict):
            continue
        entries.append((srv, key, v))
    if skipped:
        flash_msg("err", f"Пропущено записей без доступа: {skipped}")
    if not entries:
        flash_msg("err", "Выбранные записи не найдены")
        return redirect(url_for("admin_properties"))

    rows = ""
    for i, (srv, key, v) in enumerate(entries):
        idx = i
        dt_local = _dt.datetime.fromtimestamp(v.get("expiryTs", 0), tz=MSK_TZ).strftime("%Y-%m-%dT%H:%M") if v.get("expiryTs") else ""
        selectable = [s for s in SERVER_ORDER if s in VALID_SERVERS and (current_role() == "admin" or s in allowed or s == srv)]
        server_opts = "".join(f'<option value="{s}" {"selected" if s==srv else ""}>{server_label(s)}</option>' for s in selectable)
        rows += f"""
        <tr>
          <td>
            <input type="hidden" name="orig_server_{idx}" value="{srv}">
            <input type="hidden" name="orig_key_{idx}" value="{key}">
            <select name="server_{idx}" style="width:150px">{server_opts}</select>
          </td>
          <td>
            <select name="propType_{idx}" style="width:110px">
              <option value="house" {"selected" if v.get("propType")=="house" else ""}>🏠 house</option>
              <option value="business" {"selected" if v.get("propType")=="business" else ""}>🏢 business</option>
            </select>
          </td>
          <td><input type="number" name="pd_{idx}" value="{v.get('pd',0)}" min="1" max="65" style="width:70px"></td>
          <td>
            <select name="insured_{idx}" style="width:110px">
              <option value="1" {"selected" if infer_insured(v) else ""}>🛡 Страхован</option>
              <option value="0" {"selected" if not infer_insured(v) else ""}>🚫 Не страхован</option>
            </select>
          </td>
          <td><input type="text" name="propId_{idx}" value="{v.get('propId') or ''}" placeholder="ID" style="width:90px"></td>
          <td><input type="text" name="pos_{idx}" value="{v.get('pos') or ''}" placeholder="pos" style="width:90px"></td>
          <td style="text-align:center"><input type="checkbox" name="manual_time_{idx}" style="width:auto" title="Задать время слёта вручную вместо авторасчёта"></td>
          <td><input type="datetime-local" name="expiry_{idx}" value="{dt_local}" style="width:190px"></td>
          <td style="text-align:center"><input type="checkbox" name="delete_{idx}" style="width:auto"></td>
        </tr>
        """

    form = f"""
    <h1>Массовое редактирование ({len(entries)})</h1>
    <div class="card" style="overflow-x:auto">
      <form method="post" action="{url_for('admin_properties_batch_update')}">
        <table>
          <tr><th>Сервер</th><th>Тип</th><th>PD</th><th>Страховка</th><th>ID</th><th>pos</th><th>Вручную?</th><th>Время слёта</th><th>Удалить</th></tr>
          {rows}
        </table>
        <div style="margin-top:8px;color:var(--muted);font-size:12px">Если отмечено «Вручную» — используется указанное время слёта, иначе оно пересчитается автоматически по PD и порогу сервера.</div>
        <input type="hidden" name="count" value="{len(entries)}">
        <div style="margin-top:16px">
          <button type="submit">Сохранить все</button>
          <a href="{url_for('admin_properties')}"><button type="button" class="ghost">Отмена</button></a>
        </div>
      </form>
    </div>
    """
    return render_page("Массовое редактирование", "properties", form)

@app.route("/admin/properties/batch_update", methods=["POST"])
@login_required
def admin_properties_batch_update():
    try:
        count = int(request.form.get("count", 0))
    except ValueError:
        count = 0
    allowed = allowed_servers_for_current_user()
    now = int(time.time())
    updated = 0
    deleted = 0
    skipped = 0
    for i in range(count):
        sidx = str(i)
        orig_server = request.form.get(f"orig_server_{sidx}")
        orig_key = request.form.get(f"orig_key_{sidx}")
        if not orig_key or not orig_server:
            continue
        if current_role() == "editor" and orig_server not in allowed:
            skipped += 1
            continue
        if request.form.get(f"delete_{sidx}") == "on":
            db.reference(f"properties/{orig_server}/{orig_key}").delete()
            deleted += 1
            continue

        new_server = request.form.get(f"server_{sidx}", orig_server)
        if current_role() == "editor" and new_server not in allowed:
            skipped += 1
            continue
        prop_type = request.form.get(f"propType_{sidx}", "house")
        try:
            pd = int(request.form.get(f"pd_{sidx}", 0))
        except Exception:
            pd = 0
        insured = request.form.get(f"insured_{sidx}", "1") == "1"
        d_val = D_INSURED if insured else D_UNINSURED
        prop_id = request.form.get(f"propId_{sidx}", "").strip() or None
        pos = request.form.get(f"pos_{sidx}", "").strip() or None
        manual_time = request.form.get(f"manual_time_{sidx}") == "on"
        expiry_str = request.form.get(f"expiry_{sidx}", "").strip()

        if manual_time and expiry_str:
            try:
                dt = _dt.datetime.strptime(expiry_str, "%Y-%m-%dT%H:%M")
                expiry_ts = int(dt.replace(tzinfo=MSK_TZ).timestamp())
            except Exception:
                expiry_ts = int(now)
            drop_at = None
        else:
            drop_at = get_drop_at(new_server, prop_type, insured)
            expiry_ts = calc_expiry_from_pdl(pd, d_val, drop_at)

        new_record = {
            "server": new_server,
            "propType": prop_type,
            "pd": pd,
            "expiryTs": expiry_ts,
            "scanTs": now,
            "propId": prop_id,
            "pos": pos,
            "insured": insured,
            "dropAt": drop_at,
            "d": d_val,
            "count": 1,
        }

        if new_server != orig_server:
            db.reference(f"properties/{orig_server}/{orig_key}").delete()
            db.reference(f"properties/{new_server}/{orig_key}").set(new_record)
        else:
            db.reference(f"properties/{orig_server}/{orig_key}").set(new_record)
        updated += 1

    if skipped:
        flash_msg("err", f"Пропущено без доступа: {skipped}")
    flash_msg("ok", f"Обновлено: {updated}, удалено: {deleted}")
    return redirect(url_for("admin_properties"))

@app.route("/admin/properties/batch_delete", methods=["POST"])
@login_required
def admin_properties_batch_delete():
    selected = request.form.getlist("selected")
    if not selected:
        flash_msg("err", "Ничего не выбрано")
        return redirect(url_for("admin_properties"))
    allowed = allowed_servers_for_current_user()
    deleted = 0
    skipped = 0
    for s in selected:
        try:
            srv, key = s.split("|", 1)
        except Exception:
            continue
        if current_role() == "editor" and srv not in allowed:
            skipped += 1
            continue
        db.reference(f"properties/{srv}/{key}").delete()
        deleted += 1
    if skipped:
        flash_msg("err", f"Пропущено без доступа: {skipped}")
    flash_msg("ok", f"Удалено: {deleted}")
    return redirect(url_for("admin_properties"))

@app.route("/admin/properties/<server>/<key>/edit", methods=["GET"])
@login_required
def admin_property_edit(server, key):
    if current_role() == "editor" and server not in allowed_servers_for_current_user():
        flash_msg("err", "У вас нет доступа к этому серверу")
        return redirect(url_for("admin_properties"))
    v = db.reference(f"properties/{server}/{key}").get()
    if not v or not isinstance(v, dict):
        flash_msg("err", "Запись не найдена")
        return redirect(url_for("admin_properties"))

    expiry    = v.get("expiryTs", 0)
    dt_local  = _dt.datetime.fromtimestamp(expiry, tz=MSK_TZ).strftime("%Y-%m-%dT%H:%M") if expiry else ""
    server_options = "".join(
        f'<option value="{s}" {"selected" if s == server else ""}>{server_label(s)}</option>'
        for s in SERVER_ORDER if s in VALID_SERVERS
    )
    body = f"""
    <h1>Редактирование записи</h1>
    <div class="card" style="max-width:480px">
      <form method="post" action="{url_for('admin_property_update', server=server, key=key)}">
        <div class="row" style="margin-bottom:10px">
          <label style="width:140px;color:var(--muted)">Сервер</label>
          <select name="server" required>{server_options}</select>
        </div>
        <div class="row" style="margin-bottom:10px">
          <label style="width:140px;color:var(--muted)">Тип</label>
          <select name="propType" required>
            <option value="house" {"selected" if v.get("propType")=="house" else ""}>🏠 house</option>
            <option value="business" {"selected" if v.get("propType")=="business" else ""}>🏢 business</option>
          </select>
        </div>
        <div class="row" style="margin-bottom:10px">
          <label style="width:140px;color:var(--muted)">PayDay (PD)</label>
          <input type="number" name="pd" value="{v.get('pd', 0)}" min="1" max="65" required>
        </div>
        <div class="row" style="margin-bottom:10px">
          <label style="width:140px;color:var(--muted)">Застрахован</label>
          <select name="insured">
            <option value="1" {"selected" if infer_insured(v) else ""}>🛡 Страхован (1 PD/час)</option>
            <option value="0" {"selected" if not infer_insured(v) else ""}>🚫 Не страхован (2 PD/час)</option>
          </select>
        </div>
        <div class="row" style="margin-bottom:10px">
          <label style="width:140px;color:var(--muted)">Время слёта вручную</label>
          <input type="checkbox" name="manual_time" style="width:auto">
        </div>
        <div class="row" style="margin-bottom:10px">
          <label style="width:140px;color:var(--muted)">Время слёта</label>
          <input type="datetime-local" name="expiry" value="{dt_local}">
        </div>
        <div class="row" style="margin-bottom:10px">
          <label style="width:140px;color:var(--muted)">ID</label>
          <input type="text" name="propId" value="{v.get('propId') or ''}">
        </div>
        <div class="row" style="margin-bottom:16px">
          <label style="width:140px;color:var(--muted)">Позиция</label>
          <input type="text" name="pos" value="{v.get('pos') or ''}">
        </div>
        <button type="submit">Сохранить</button>
        <a href="{url_for('admin_properties')}"><button type="button" class="ghost">Отмена</button></a>
      </form>
    </div>"""
    return render_page("Редактирование", "properties", body)

@app.route("/admin/properties/<server>/<key>/update", methods=["POST"])
@login_required
def admin_property_update(server, key):
    back = request.form.get("back", "admin_properties")
    if back not in ("admin_properties", "admin_expired"):
        back = "admin_properties"

    old = db.reference(f"properties/{server}/{key}").get()
    if not old or not isinstance(old, dict):
        flash_msg("err", "Запись не найдена")
        return redirect(url_for(back))

    if current_role() == "editor":
        allowed = allowed_servers_for_current_user()
        if server not in allowed:
            flash_msg("err", "У вас нет доступа к этому серверу")
            return redirect(url_for(back))

    new_server = request.form.get("server", server)
    prop_type  = request.form.get("propType", "house")
    prop_id    = request.form.get("propId", "").strip()
    pos        = request.form.get("pos", "").strip()
    expiry_str = request.form.get("expiry", "")
    manual_time = request.form.get("manual_time") == "on"
    insured    = request.form.get("insured", "1" if infer_insured(old) else "0") == "1"
    try:
        pd = int(request.form.get("pd", 0))
    except ValueError:
        flash_msg("err", "Неверный PD")
        return redirect(url_for("admin_property_edit", server=server, key=key))

    if new_server not in VALID_SERVERS or prop_type not in ("house", "business"):
        flash_msg("err", "Неверный сервер или тип")
        return redirect(url_for("admin_property_edit", server=server, key=key))
    if current_role() == "editor" and new_server not in allowed_servers_for_current_user():
        flash_msg("err", "У вас нет доступа к серверу назначения")
        return redirect(url_for("admin_property_edit", server=server, key=key))
    if pd <= 0 or pd > 65:
        flash_msg("err", "PayDay должен быть 1–65")
        return redirect(url_for("admin_property_edit", server=server, key=key))

    d_val = D_INSURED if insured else D_UNINSURED

    if manual_time and expiry_str:
        try:
            dt = _dt.datetime.strptime(expiry_str, "%Y-%m-%dT%H:%M")
            expiry_ts = int(dt.replace(tzinfo=MSK_TZ).timestamp())
        except Exception:
            flash_msg("err", "Неверный формат времени")
            return redirect(url_for("admin_property_edit", server=server, key=key))
        drop_at = None
    else:
        drop_at = get_drop_at(new_server, prop_type, insured)
        expiry_ts = calc_expiry_from_pdl(pd, d_val, drop_at)

    updated = dict(old)
    updated.update({
        "server":   new_server,
        "propType": prop_type,
        "pd":       pd,
        "expiryTs": expiry_ts,
        "scanTs":   int(time.time()),  # свежий ввод PD — отсчёт распада начинается заново
        "propId":   prop_id or None,
        "pos":      pos or None,
        "insured":  insured,
        "dropAt":   drop_at,
        "d":        d_val,
    })

    if new_server != server:
        db.reference(f"properties/{server}/{key}").delete()
        db.reference(f"properties/{new_server}/{key}").set(updated)
    else:
        db.reference(f"properties/{server}/{key}").set(updated)

    flash_msg("ok", "Запись обновлена")
    return redirect(url_for(back))

# ── Настройки (пороги слёта по серверам) ─────────────────
@app.route("/admin/settings")
@admin_only
def admin_settings():
    dropat_cfg = db.reference("config/dropAt").get() or {}
    sub_required = db.reference("config/subscriptionRequired").get()
    sub_required = True if not isinstance(sub_required, bool) else sub_required

    rows = ""
    for s in SERVER_ORDER:
        if s not in VALID_SERVERS:
            continue
        h_ins = get_drop_at_cached(dropat_cfg, s, "house", True)
        h_uni = get_drop_at_cached(dropat_cfg, s, "house", False)
        b_ins = get_drop_at_cached(dropat_cfg, s, "business", True)
        b_uni = get_drop_at_cached(dropat_cfg, s, "business", False)
        rows += f"""<tr>
          <td>{s}</td>
          <td><input type="number" name="house_insured_{s}" value="{h_ins}" min="0" max="10" style="width:70px"></td>
          <td><input type="number" name="house_uninsured_{s}" value="{h_uni}" min="0" max="10" style="width:70px"></td>
          <td><input type="number" name="business_insured_{s}" value="{b_ins}" min="0" max="10" style="width:70px"></td>
          <td><input type="number" name="business_uninsured_{s}" value="{b_uni}" min="0" max="10" style="width:70px"></td>
        </tr>"""
    body = f"""
    <h1>Настройки</h1>
    <div class="card">
      <h2 style="margin-top:0">Доступ по подписке</h2>
      <p style="color:var(--muted)">
        Если включено — боту нужен активный ключ доступа (как сейчас). Если выключено — ботом сможет пользоваться
        кто угодно без ключа.
      </p>
      <form method="post" action="{url_for('admin_settings_subscription')}" class="row" style="align-items:center">
        <label style="display:flex;align-items:center;gap:8px;font-weight:normal">
          <input type="checkbox" name="required" {"checked" if sub_required else ""}>
          Требовать подписку (ключ доступа)
        </label>
        <button type="submit">Сохранить</button>
      </form>
    </div>
    <div class="card">
      <p style="color:var(--muted);margin-top:0">
        Порог (L) — значение PayDay, при достижении которого объект считается слетевшим. Задаётся отдельно для
        домов и бизнесов, и отдельно для застрахованных (теряют 1 PD в час) и незастрахованных (теряют 2 PD в час)
        на каждом сервере. Используется для автоматического расчёта времени слёта.
      </p>
      <form method="post" action="{url_for('admin_settings_save')}">
        <table>
          <tr><th>Сервер</th><th>🏠🛡 Дом застрах.</th><th>🏠🚫 Дом не застрах.</th><th>🏢🛡 Бизнес застрах.</th><th>🏢🚫 Бизнес не застрах.</th></tr>
          {rows}
        </table>
        <div style="margin-top:14px"><button type="submit">Сохранить</button></div>
      </form>
    </div>"""
    return render_page("Настройки", "settings", body + _render_frozen_section() + _render_exceptions_section())

@app.route("/admin/settings/subscription", methods=["POST"])
@admin_only
def admin_settings_subscription():
    required = request.form.get("required") == "on"
    db.reference("config/subscriptionRequired").set(required)
    flash_msg("ok", "Доступ по подписке " + ("включён" if required else "выключен"))
    return redirect(url_for("admin_settings"))

@app.route("/admin/settings/save", methods=["POST"])
@admin_only
def admin_settings_save():
    updated = 0
    for s in SERVER_ORDER:
        if s not in VALID_SERVERS:
            continue
        for form_prefix, prop_type, insured_key in (
            ("house_insured_", "house", "insured"),
            ("house_uninsured_", "house", "uninsured"),
            ("business_insured_", "business", "insured"),
            ("business_uninsured_", "business", "uninsured"),
        ):
            raw = request.form.get(f"{form_prefix}{s}", "").strip()
            if raw == "":
                continue
            try:
                val = int(raw)
            except ValueError:
                continue
            set_drop_at(s, prop_type, insured_key, val)
            updated += 1
    flash_msg("ok", f"Настройки сохранены ({updated} значений)")
    return redirect(url_for("admin_settings"))

def _render_frozen_section():
    data = db.reference("config/frozenHouses").get() or {}
    rows = ""
    count = 0
    for srv in sorted(data.keys(), key=lambda s: SERVER_ORDER.index(s) if s in SERVER_ORDER else 999):
        rules = data.get(srv)
        if not isinstance(rules, dict):
            continue
        for rule_id, rule in rules.items():
            if not isinstance(rule, dict):
                continue
            count += 1
            r_type = rule.get("propType") or "house"
            type_label = "🏠 Дом" if r_type == "house" else "🏢 Бизнес"
            rows += f"""<tr>
              <td>{srv}</td>
              <td>{type_label}</td>
              <td>{rule.get("count","?")}</td>
              <td>{rule.get("pd","?")} PD</td>
              <td>
                <form class="inline" method="post" action="{url_for('admin_frozen_remove', server=srv, rule_id=rule_id)}">
                  <button type="submit" class="danger">Убрать</button>
                </form>
              </td>
            </tr>"""

    server_options = "".join(
        f'<option value="{s}">{server_label(s)}</option>' for s in SERVER_ORDER if s in VALID_SERVERS
    )
    return f"""
    <div class="card">
      <h2 style="margin-top:0">Замороженные объекты</h2>
      <p style="color:var(--muted)">
        На некоторых серверах есть дома или бизнесы, у которых PD зафиксирован и не падает. Укажите сервер, тип,
        сколько таких объектов и на каком PD они стоят — бот будет пропускать ровно столько объектов этого типа
        с этим PD при каждом скане. Если реальных (не замороженных) объектов на этом же PD окажется больше —
        лишние всё равно попадут в слёты.
      </p>
      <form method="post" action="{url_for('admin_frozen_add')}" class="row" style="margin-bottom:14px">
        <select name="server" required><option value="">Сервер</option>{server_options}</select>
        <select name="prop_type" required>
          <option value="house">🏠 Дом</option>
          <option value="business">🏢 Бизнес</option>
        </select>
        <input type="number" name="count" placeholder="Кол-во" min="1" required style="width:120px">
        <input type="number" name="pd" placeholder="PD" min="1" max="65" required style="width:100px">
        <button type="submit">Добавить</button>
      </form>
      <table>
        <tr><th>Сервер</th><th>Тип</th><th>Кол-во</th><th>PD</th><th></th></tr>
        {rows or "<tr><td colspan='5'>Замороженных объектов пока не задано</td></tr>"}
      </table>
    </div>
    """

@app.route("/admin/frozen/add", methods=["POST"])
@admin_only
def admin_frozen_add():
    server    = request.form.get("server", "")
    prop_type = request.form.get("prop_type", "house")
    if server not in VALID_SERVERS:
        flash_msg("err", "Неверный сервер")
        return redirect(url_for("admin_settings"))
    if prop_type not in ("house", "business"):
        flash_msg("err", "Неверный тип объекта")
        return redirect(url_for("admin_settings"))
    try:
        count = int(request.form.get("count", 0))
        pd    = int(request.form.get("pd", 0))
    except ValueError:
        flash_msg("err", "Неверные значения")
        return redirect(url_for("admin_settings"))
    if count <= 0 or pd <= 0:
        flash_msg("err", "Количество и PD должны быть больше нуля")
        return redirect(url_for("admin_settings"))

    new_ref = db.reference(f"config/frozenHouses/{server}").push()
    new_ref.set({"pd": pd, "count": count, "propType": prop_type, "addedAt": int(time.time())})
    type_label = "дом(ов)" if prop_type == "house" else "бизнес(ов)"
    flash_msg("ok", f"Добавлено: {count} {type_label} на {pd} PD для {server}")
    return redirect(url_for("admin_settings"))

@app.route("/admin/frozen/<server>/<rule_id>/remove", methods=["POST"])
@admin_only
def admin_frozen_remove(server, rule_id):
    db.reference(f"config/frozenHouses/{server}/{rule_id}").delete()
    flash_msg("ok", "Правило убрано")
    return redirect(url_for("admin_settings"))

# ── Исключения: ID домов/бизнесов, которые никогда не попадают в слёты ──
def _render_exceptions_section():
    data = db.reference("exceptions").get() or {}
    rows = ""
    count = 0
    for srv in sorted(data.keys(), key=lambda s: SERVER_ORDER.index(s) if s in SERVER_ORDER else 999):
        ids = data.get(srv)
        if not isinstance(ids, dict):
            continue
        for pid in sorted(ids.keys()):
            count += 1
            rows += f"""<tr>
              <td>{srv}</td>
              <td>{pid}</td>
              <td>
                <form class="inline" method="post" action="{url_for('admin_exception_remove', server=srv, prop_id=pid)}">
                  <button type="submit" class="danger">Убрать</button>
                </form>
              </td>
            </tr>"""

    server_options = "".join(
        f'<option value="{s}">{server_label(s)}</option>' for s in SERVER_ORDER if s in VALID_SERVERS
    )
    return f"""
    <div class="card">
      <h2 style="margin-top:0">Исключения</h2>
      <p style="color:var(--muted)">
        ID домов ИЛИ бизнесов (работает для обоих типов), которые никогда не будут попадать в «Слёты» — ни сейчас,
        ни при последующих сканах. Если ID уже был добавлен ранее, существующая запись будет удалена сразу.
      </p>
      <form method="post" action="{url_for('admin_exception_add')}" class="row" style="margin-bottom:14px">
        <select name="server" required><option value="">Сервер</option>{server_options}</select>
        <input type="text" name="prop_id" placeholder="ID (можно несколько через запятую)" required style="width:260px">
        <button type="submit">Добавить</button>
      </form>
      <table>
        <tr><th>Сервер</th><th>ID</th><th></th></tr>
        {rows or f"<tr><td colspan='3'>Исключений пока нет</td></tr>"}
      </table>
    </div>
    """

@app.route("/admin/exceptions/add", methods=["POST"])
@admin_only
def admin_exception_add():
    server = request.form.get("server", "")
    raw_ids = request.form.get("prop_id", "")
    if server not in VALID_SERVERS:
        flash_msg("err", "Неверный сервер")
        return redirect(url_for("admin_settings"))

    ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
    if not ids:
        flash_msg("err", "Укажите хотя бы один ID")
        return redirect(url_for("admin_settings"))

    now = int(time.time())
    removed = 0
    for pid in ids:
        db.reference(f"exceptions/{server}/{pid}").set({"addedAt": now})
        for prop_type in ("house", "business"):
            key = f"{prop_type}_{pid}"
            if db.reference(f"properties/{server}/{key}").get():
                db.reference(f"properties/{server}/{key}").delete()
                removed += 1
    flash_msg("ok", f"Добавлено в исключения: {len(ids)}" + (f", удалено существующих записей: {removed}" if removed else ""))
    return redirect(url_for("admin_settings"))

@app.route("/admin/exceptions/<server>/<prop_id>/remove", methods=["POST"])
@admin_only
def admin_exception_remove(server, prop_id):
    db.reference(f"exceptions/{server}/{prop_id}").delete()
    flash_msg("ok", "Исключение убрано")
    return redirect(url_for("admin_settings"))

# ── Редакторы (ограниченные аккаунты по серверам) ────────
def _server_checkboxes_html(name="servers", checked=None, id_prefix="", select_all=False):
    checked = checked or set()
    boxes = []
    for s in SERVER_ORDER:
        if s not in VALID_SERVERS:
            continue
        cid = f"{id_prefix}{name}_{s}".replace(" ", "_")
        is_checked = "checked" if s in checked else ""
        boxes.append(
            f'<label for="{cid}" style="display:flex;align-items:center;gap:6px;font-size:13px;font-weight:normal;cursor:pointer">'
            f'<input type="checkbox" id="{cid}" name="{name}" value="{s}" {is_checked}> {s}</label>'
        )
    container_id = f"{id_prefix}{name}_box".replace(" ", "_")
    select_all_btn = ""
    if select_all:
        select_all_btn = (
            f'<button type="button" class="ghost" style="margin-bottom:8px" '
            f'onclick="toggleAllServers(\'{container_id}\')">Выбрать все / Снять все</button>'
        )
    return (
        f'{select_all_btn}'
        f'<div id="{container_id}" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px">{"".join(boxes)}</div>'
    )

SELECT_ALL_SERVERS_JS = """
<script>
function toggleAllServers(containerId) {
  var box = document.getElementById(containerId);
  if (!box) return;
  var boxes = box.querySelectorAll('input[type=checkbox]');
  var anyUnchecked = Array.from(boxes).some(function(cb){ return !cb.checked; });
  boxes.forEach(function(cb){ cb.checked = anyUnchecked; });
}
</script>
"""

@app.route("/admin/editors")
@admin_only
def admin_editors():
    data = db.reference("editors").get() or {}
    editors = []
    for eid, e in data.items():
        if isinstance(e, dict):
            e = dict(e)
            e["id"] = eid
            editors.append(e)
    editors.sort(key=lambda e: e.get("created_at", 0), reverse=True)

    rows = ""
    for e in editors:
        eid = e["id"]
        servers = [s for s in e.get("servers", []) if s in VALID_SERVERS]
        servers_label = ", ".join(servers) if servers else '<span style="color:var(--muted)">нет серверов</span>'
        active = e.get("active", True)
        status_pill = '<span class="pill ok">активен</span>' if active else '<span class="pill muted">отключён</span>'
        toggle_label = "Отключить" if active else "Включить"
        created = format_msk(e.get("created_at", 0), "%d.%m.%Y %H:%M")

        rows += f"""
        <tr id="view-ed{eid}">
          <td class="mono">{e.get("username","—")}</td>
          <td>{servers_label}</td>
          <td>{status_pill}</td>
          <td>{created}</td>
          <td class="row">
            <button class="ghost" type="button" onclick="toggleEdit('ed{eid}')">Сервера</button>
            <form class="inline" method="post" action="{url_for('admin_editor_toggle', eid=eid)}">
              <button class="ghost" type="submit">{toggle_label}</button>
            </form>
            <form class="inline" method="post" action="{url_for('admin_editor_delete', eid=eid)}" onsubmit="return confirm('Удалить редактора {e.get('username','')}?')">
              <button class="danger" type="submit">Удалить</button>
            </form>
          </td>
        </tr>
        <tr id="edit-ed{eid}" style="display:none">
          <td colspan="5">
            <form method="post" action="{url_for('admin_editor_update_servers', eid=eid)}" style="margin-bottom:12px">
              <div style="color:var(--muted);font-size:13px;margin-bottom:8px">Доступные сервера для «{e.get("username","")}»:</div>
              {_server_checkboxes_html(checked=set(servers), id_prefix=f"ed{eid}_")}
              <div style="margin-top:10px">
                <button type="submit">Сохранить сервера</button>
                <button type="button" class="ghost" onclick="toggleEdit('ed{eid}')">Отмена</button>
              </div>
            </form>
            <form method="post" action="{url_for('admin_editor_password', eid=eid)}" class="row" style="border-top:1px solid var(--border);padding-top:12px">
              <input type="password" name="password" placeholder="Новый пароль" required style="width:200px">
              <button type="submit">Сменить пароль</button>
            </form>
          </td>
        </tr>
        """

    body = f"""
    <h1>Редакторы</h1>
    <div class="card">
      <h2 style="margin-top:0">Создать редактора</h2>
      <form method="post" action="{url_for('admin_editor_add')}">
        <div class="row" style="margin-bottom:14px">
          <input type="text" name="username" placeholder="Логин" required style="width:200px">
          <input type="text" name="password" placeholder="Пароль (мин. 4 символа)" required style="width:220px">
        </div>
        <div style="color:var(--muted);font-size:13px;margin-bottom:8px">Доступные сервера:</div>
        {_server_checkboxes_html(select_all=True)}
        <div style="margin-top:14px"><button type="submit">Создать</button></div>
      </form>
    </div>
    <table>
      <tr><th>Логин</th><th>Сервера</th><th>Статус</th><th>Создан</th><th></th></tr>
      {rows or "<tr><td colspan='5'>Редакторов пока нет</td></tr>"}
    </table>
    {TOGGLE_EDIT_SCRIPT}{SELECT_ALL_SERVERS_JS}"""
    return render_page("Редакторы", "editors", body)

@app.route("/admin/editors/add", methods=["POST"])
@admin_only
def admin_editor_add():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    servers  = [s for s in request.form.getlist("servers") if s in VALID_SERVERS]

    if not username or not password:
        flash_msg("err", "Укажите логин и пароль")
        return redirect(url_for("admin_editors"))
    if len(password) < 4:
        flash_msg("err", "Пароль слишком короткий (минимум 4 символа)")
        return redirect(url_for("admin_editors"))
    if secrets.compare_digest(username, ADMIN_USERNAME):
        flash_msg("err", "Этот логин зарезервирован")
        return redirect(url_for("admin_editors"))
    if find_editor_by_username(username):
        flash_msg("err", "Такой логин уже занят")
        return redirect(url_for("admin_editors"))

    new_ref = db.reference("editors").push()
    new_ref.set({
        "username": username,
        "password_hash": generate_password_hash(password),
        "servers": servers,
        "active": True,
        "created_at": int(time.time()),
    })
    flash_msg("ok", f"Редактор «{username}» создан")
    return redirect(url_for("admin_editors"))

@app.route("/admin/editors/<eid>/servers", methods=["POST"])
@admin_only
def admin_editor_update_servers(eid):
    servers = [s for s in request.form.getlist("servers") if s in VALID_SERVERS]
    db.reference(f"editors/{eid}/servers").set(servers)
    flash_msg("ok", "Список серверов обновлён")
    return redirect(url_for("admin_editors"))

@app.route("/admin/editors/<eid>/password", methods=["POST"])
@admin_only
def admin_editor_password(eid):
    password = request.form.get("password", "").strip()
    if len(password) < 4:
        flash_msg("err", "Пароль слишком короткий (минимум 4 символа)")
        return redirect(url_for("admin_editors"))
    db.reference(f"editors/{eid}/password_hash").set(generate_password_hash(password))
    flash_msg("ok", "Пароль обновлён")
    return redirect(url_for("admin_editors"))

@app.route("/admin/editors/<eid>/toggle", methods=["POST"])
@admin_only
def admin_editor_toggle(eid):
    e = db.reference(f"editors/{eid}").get()
    if not e or not isinstance(e, dict):
        flash_msg("err", "Редактор не найден")
        return redirect(url_for("admin_editors"))
    db.reference(f"editors/{eid}/active").set(not e.get("active", True))
    flash_msg("ok", "Статус обновлён")
    return redirect(url_for("admin_editors"))

@app.route("/admin/editors/<eid>/delete", methods=["POST"])
@admin_only
def admin_editor_delete(eid):
    db.reference(f"editors/{eid}").delete()
    flash_msg("ok", "Редактор удалён")
    return redirect(url_for("admin_editors"))

# ── Поместья: порядок серверов в кнопке бота "🏡 Дома с поместьями".
#   Сам контент (картинка+текст) задаётся из телеграм-бота командой
#   /setestate — здесь только порядок, в котором сервера показываются
#   в виде кнопок пользователю.
def _get_estates_ordered():
    """Сервера, у которых есть контент (estates/), в текущем порядке
    показа (estates_order), новые/ещё не упорядоченные — в конце."""
    estates = db.reference("estates").get(shallow=True) or {}
    order = db.reference("estates_order").get() or []
    if not isinstance(order, list):
        order = []
    ordered = [s for s in order if s in estates]
    ordered += [s for s in sorted(estates) if s not in ordered]
    return ordered

@app.route("/admin/estates")
@admin_only
def admin_estates():
    servers = _get_estates_ordered()

    rows = ""
    for i, s in enumerate(servers):
        up_dis   = "disabled" if i == 0 else ""
        down_dis = "disabled" if i == len(servers) - 1 else ""
        rows += f"""
        <tr>
          <td class="mono">{i + 1}</td>
          <td>{s}</td>
          <td class="row">
            <form class="inline" method="post" action="{url_for('admin_estates_move')}">
              <input type="hidden" name="server" value="{s}">
              <input type="hidden" name="dir" value="up">
              <button class="ghost" type="submit" {up_dis}>⬆️</button>
            </form>
            <form class="inline" method="post" action="{url_for('admin_estates_move')}">
              <input type="hidden" name="server" value="{s}">
              <input type="hidden" name="dir" value="down">
              <button class="ghost" type="submit" {down_dis}>⬇️</button>
            </form>
            <form class="inline" method="post" action="{url_for('admin_estates_remove')}" onsubmit="return confirm('Убрать «{s}» из кнопки «Дома с поместьями»?')">
              <input type="hidden" name="server" value="{s}">
              <button class="danger" type="submit">Убрать</button>
            </form>
          </td>
        </tr>"""

    body = f"""
    <h1>Поместья</h1>
    <div class="card" style="margin-bottom:14px">
      <p style="color:var(--muted);margin:0">
        Порядок серверов в кнопке бота «🏡 Дома с поместьями». Картинка и текст
        для каждого сервера задаются из телеграм-бота командой <code>/setestate</code> —
        здесь можно только менять порядок или убрать сервер из списка.
      </p>
    </div>
    <table>
      <tr><th>#</th><th>Сервер</th><th></th></tr>
      {rows or "<tr><td colspan='3'>Пока ни один сервер не настроен — задайте контент через /setestate в боте.</td></tr>"}
    </table>
    """
    return render_page("Поместья", "estates", body)

@app.route("/admin/estates/move", methods=["POST"])
@admin_only
def admin_estates_move():
    server    = request.form.get("server", "")
    direction = request.form.get("dir", "")
    servers   = _get_estates_ordered()
    if server not in servers:
        flash_msg("err", "Сервер не найден")
        return redirect(url_for("admin_estates"))

    idx = servers.index(server)
    if direction == "up" and idx > 0:
        servers[idx - 1], servers[idx] = servers[idx], servers[idx - 1]
    elif direction == "down" and idx < len(servers) - 1:
        servers[idx + 1], servers[idx] = servers[idx], servers[idx + 1]

    db.reference("estates_order").set(servers)
    return redirect(url_for("admin_estates"))

@app.route("/admin/estates/remove", methods=["POST"])
@admin_only
def admin_estates_remove():
    server = request.form.get("server", "")
    db.reference(f"estates/{server}").delete()
    servers = [s for s in _get_estates_ordered() if s != server]
    db.reference("estates_order").set(servers)
    flash_msg("ok", f"«{server}» убран из «Дома с поместьями»")
    return redirect(url_for("admin_estates"))

# ── Риелторка: 2 режима — "Снэпшоты" (последние 3 часа-скана, все
#   сервера сеткой, как раунд обхода) и "История" (для домов без ID —
#   неограниченная история по позиции, сравнение любых 2 сканов) ─────
REALTOR_JS = ""

def _realtor_tabs(active_view):
    snap_url = url_for('admin_realtor', view='snapshot')
    hist_url = url_for('admin_realtor', view='history')
    cmp_url  = url_for('admin_realtor', view='compare')
    snap_cls = "" if active_view == "snapshot" else ' class="ghost"'
    hist_cls = "" if active_view == "history" else ' class="ghost"'
    cmp_cls  = "" if active_view == "compare" else ' class="ghost"'
    return f"""
    <div class="row" style="gap:8px;margin-bottom:14px">
      <a href="{snap_url}"><button type="button"{snap_cls}>📸 Снэпшоты</button></a>
      <a href="{hist_url}"><button type="button"{hist_cls}>📊 История</button></a>
      <a href="{cmp_url}"><button type="button"{cmp_cls}>🔄 Сравнение</button></a>
    </div>
    """

def _render_snapshot_mode(t_param, allowed=None):
    sessions_index = db.reference("sessions_index").get() or []
    if not isinstance(sessions_index, list):
        sessions_index = []
    try:
        sessions_index = sorted({int(x) for x in sessions_index}, reverse=True)
    except (ValueError, TypeError):
        sessions_index = []

    if not sessions_index:
        return '<div class="card">Пока нет ни одного скана. Отсканируйте хотя бы один сервер ботом — раунд появится здесь.</div>'

    selected_bucket = None
    if t_param:
        try:
            t_int = int(t_param)
            if t_int in sessions_index:
                selected_bucket = t_int
        except ValueError:
            pass
    if selected_bucket is None:
        selected_bucket = sessions_index[0]

    time_buttons = ""
    for b in sessions_index:
        label = format_msk(b, "%H:00 (%d.%m)")
        active = (b == selected_bucket)
        url = url_for('admin_realtor', view='snapshot', t=b)
        cls = "" if active else ' class="ghost"'
        time_buttons += f'<a href="{url}"><button type="button"{cls}>{label}</button></a> '

    snap = db.reference(f"sessions/{selected_bucket}").get() or {}
    all_live_props = db.reference("properties").get() or {}
    cards = ""
    if isinstance(snap, dict):
        for srv in sorted(snap.keys(), key=lambda s: SERVER_ORDER.index(s) if s in SERVER_ORDER else 999):
            if allowed is not None and srv not in allowed:
                continue
            entries = snap.get(srv)
            if not isinstance(entries, dict) or not entries:
                continue

            # Статус страховки читаем из ЖИВОЙ таблицы properties, а не из
            # замороженной копии в снэпшоте — иначе после нажатия 🛡/🚫
            # подсветка кнопки визуально не менялась (баг): запись в
            # properties обновлялась, а снэпшот — нет, так как это
            # исторический слепок на момент скана.
            live_props = all_live_props.get(srv) if isinstance(all_live_props, dict) else None
            if not isinstance(live_props, dict):
                live_props = {}

            def _sort_key(item):
                v = item[1]
                prop_type = v.get("propType") if isinstance(v, dict) else "house"
                type_rank = 0 if prop_type == "house" else 1  # сначала дома, потом бизнесы
                pos = v.get("pos") if isinstance(v, dict) else None
                pos_rank = pos if isinstance(pos, (int, float)) else 9999
                return (type_rank, pos_rank)

            rows = ""
            for key, v in sorted(entries.items(), key=_sort_key):
                if not isinstance(v, dict):
                    continue
                icon = "🏠" if v.get("propType") == "house" else "🏢"
                label = v.get("propId") or v.get("pos") or "—"
                pd_val = v.get("pd", "?")
                live_v = live_props.get(key) if isinstance(live_props, dict) else None
                insured_flag = infer_insured(live_v) if isinstance(live_v, dict) else v.get("insured")
                shield_on   = "background:var(--accent);color:#0d0f14;border-color:var(--accent)" if insured_flag is True  else ""
                noentry_on  = "background:var(--danger);color:#0d0f14;border-color:var(--danger)" if insured_flag is False else ""
                rows += f"""<tr>
                  <td style="padding:4px 3px;white-space:nowrap">{v.get('pos','—')}. {icon} №{label}</td>
                  <td style="padding:4px 3px;white-space:nowrap"><span class="pill bad" style="font-size:10px;padding:1px 6px">{pd_val} PD</span></td>
                  <td style="padding:4px 3px;white-space:nowrap;text-align:right">
                    <form class="inline" method="post" action="{url_for('admin_realtor_set_insured', server=srv, key=key)}">
                      <input type="hidden" name="view" value="snapshot">
                      <input type="hidden" name="t" value="{selected_bucket}">
                      <input type="hidden" name="insured" value="1">
                      <button type="submit" class="ghost" title="Застрахован" style="padding:2px 5px;font-size:10px;line-height:1;{shield_on}">🛡</button>
                    </form>
                    <form class="inline" method="post" action="{url_for('admin_realtor_set_insured', server=srv, key=key)}">
                      <input type="hidden" name="view" value="snapshot">
                      <input type="hidden" name="t" value="{selected_bucket}">
                      <input type="hidden" name="insured" value="0">
                      <button type="submit" class="ghost" title="Не застрахован" style="padding:2px 5px;font-size:10px;line-height:1;{noentry_on}">🚫</button>
                    </form>
                    <form class="inline" method="post" action="{url_for('admin_realtor_delete', server=srv, key=key)}" onsubmit="return confirm('Убрать этот дом из списка?')">
                      <input type="hidden" name="view" value="snapshot">
                      <input type="hidden" name="t" value="{selected_bucket}">
                      <button type="submit" class="danger" title="Удалить" style="padding:2px 5px;font-size:10px;line-height:1">✕</button>
                    </form>
                  </td>
                </tr>"""

            cards += f"""
            <div class="card" style="padding:10px 12px;font-size:12px">
              <div style="font-weight:700;margin-bottom:6px;font-size:13px">{server_label(srv)}</div>
              <table style="font-size:12px;table-layout:fixed">
                <tr><th style="padding:3px">Объект</th><th style="padding:3px">Слёт</th><th style="padding:3px"></th></tr>
                {rows}
              </table>
            </div>"""

    return f"""
    <div class="card" style="margin-bottom:14px">
      <div style="color:var(--muted);font-size:13px;margin-bottom:8px">Раунд сканирования (доступны только часы, когда реально что-то сканировали):</div>
      {time_buttons}
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;align-items:start">
      {cards or "<div class='card'>Нет данных за этот час.</div>"}
    </div>
    """

HISTORY_WINDOW_SECONDS = 24 * 3600

def _render_history_mode(allowed=None):
    """Простая хронологическая история сканов домов без ID за последние 24ч —
    в том же компактном стиле карточек, что и 'Снэпшоты'."""
    now = int(time.time())
    since = now - HISTORY_WINDOW_SECONDS
    retention_cutoff = now - HISTORY_RETENTION_SECONDS
    all_history = db.reference("history").get() or {}

    cards = ""
    if isinstance(all_history, dict):
        for srv in sorted(all_history.keys(), key=lambda s: SERVER_ORDER.index(s) if s in SERVER_ORDER else 999):
            if allowed is not None and srv not in allowed:
                continue
            positions = all_history.get(srv)
            if not isinstance(positions, dict):
                continue

            events = []  # (ts, key, pos_label, prop_type, pd)
            for key, entries in positions.items():
                if not isinstance(entries, dict):
                    continue
                parts = key.split("_pos_")
                prop_type_label = parts[0] if parts else "?"
                pos_label = parts[1] if len(parts) > 1 else "?"
                for ts_str, v in entries.items():
                    try:
                        ts = int(ts_str)
                        pd_val = int(v.get("pd")) if isinstance(v, dict) else None
                    except (ValueError, TypeError):
                        continue
                    # Подчищаем то, что уже успело накопиться сверх текущего
                    # окна хранения (раньше оно было 30 дней, теперь 48ч).
                    if ts < retention_cutoff:
                        db.reference(f"history/{srv}/{key}/{ts_str}").delete()
                        continue
                    if pd_val is None or ts < since:
                        continue
                    events.append((ts, key, pos_label, prop_type_label, pd_val))

            if not events:
                continue
            events.sort(key=lambda e: e[0], reverse=True)

            current_props = db.reference(f"properties/{srv}").get() or {}

            rows = ""
            for ts, key, pos_label, prop_type_label, pd_val in events:
                icon = "🏠" if prop_type_label == "house" else "🏢"
                current = current_props.get(key) if isinstance(current_props, dict) else None
                insured_flag = infer_insured(current) if isinstance(current, dict) else None
                shield_on  = "background:var(--accent);color:#0d0f14;border-color:var(--accent)" if insured_flag is True else ""
                noentry_on = "background:var(--danger);color:#0d0f14;border-color:var(--danger)" if insured_flag is False else ""
                rows += f"""<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 3px;border-bottom:1px solid var(--border)">
                    <div style="min-width:0;flex:1 1 auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{pos_label}. {icon} {format_msk(ts, "%d.%m %H:%M")}</div>
                    <div style="flex:0 0 auto"><span class="pill bad" style="font-size:10px;padding:1px 6px">{pd_val} PD</span></div>
                    <div style="display:flex;gap:3px;flex:0 0 auto">
                      <form class="inline" method="post" action="{url_for('admin_realtor_set_insured', server=srv, key=key)}">
                        <input type="hidden" name="view" value="history">
                        <input type="hidden" name="insured" value="1">
                        <button type="submit" class="ghost" title="Застрахован" style="padding:2px 5px;font-size:10px;line-height:1;{shield_on}">🛡</button>
                      </form>
                      <form class="inline" method="post" action="{url_for('admin_realtor_set_insured', server=srv, key=key)}">
                        <input type="hidden" name="view" value="history">
                        <input type="hidden" name="insured" value="0">
                        <button type="submit" class="ghost" title="Не застрахован" style="padding:2px 5px;font-size:10px;line-height:1;{noentry_on}">🚫</button>
                      </form>
                      <form class="inline" method="post" action="{url_for('admin_realtor_delete', server=srv, key=key)}" onsubmit="return confirm('Убрать этот дом из списка?')">
                        <input type="hidden" name="view" value="history">
                        <button type="submit" class="danger" title="Удалить" style="padding:2px 5px;font-size:10px;line-height:1">✕</button>
                      </form>
                    </div>
                  </div>"""

            cards += f"""
            <div class="card" style="padding:10px 12px;font-size:12px">
              <div style="font-weight:700;margin-bottom:6px;font-size:13px">{server_label(srv)} <span style="color:var(--muted);font-weight:normal">({len(events)})</span></div>
              {rows}
            </div>"""

    intro = """
    <div class="card" style="margin-bottom:14px">
      <p style="color:var(--muted);margin:0">
        История сканов домов <b>без ID</b> за последние 24 часа — хронологический лог по всем серверам.
      </p>
    </div>
    """
    grid = f"""
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;align-items:start">
      {cards or "<div class='card'>За последние 24ч сканов не было.</div>"}
    </div>
    """
    return intro + grid

def _render_compare_mode(a_param, b_param, allowed=None, srv_param=""):
    """Сравнение любых двух сканов (снэпшотов-часов) — по ВСЕМ серверам сразу
    или по одному выбранному серверу. Времена для скана 1 / скана 2 выбираются
    отдельными плашками и показывают только те часы, когда реально сканировали
    выбранный сервер (а не вообще все раунды)."""
    sessions_index = db.reference("sessions_index").get() or []
    if not isinstance(sessions_index, list):
        sessions_index = []
    try:
        sessions_index = sorted({int(x) for x in sessions_index}, reverse=True)
    except (ValueError, TypeError):
        sessions_index = []

    if len(sessions_index) < 2:
        return '<div class="card">Нужно как минимум 2 сохранённых скана-часа для сравнения. Пока доступно: ' + str(len(sessions_index)) + '.</div>'

    # sessions_index хранит максимум 3 последних часа-раунда (см. auto_cleanup),
    # поэтому не страшно вытянуть данные по всем сразу — это максимум 3 запроса.
    bucket_data = {}
    for b in sessions_index:
        d = db.reference(f"sessions/{b}").get() or {}
        bucket_data[b] = d if isinstance(d, dict) else {}

    all_servers = sorted(
        {srv for d in bucket_data.values() for srv in d.keys()},
        key=lambda s: SERVER_ORDER.index(s) if s in SERVER_ORDER else 999,
    )
    if allowed is not None:
        all_servers = [s for s in all_servers if s in allowed]

    selected_srv = srv_param if srv_param in all_servers else ""

    srv_pills = ""
    all_url = url_for('admin_realtor', view='compare', a=a_param, b=b_param, srv="")
    all_cls = "" if not selected_srv else ' class="ghost"'
    srv_pills += f'<a href="{all_url}"><button type="button"{all_cls}>Все сервера</button></a> '
    for s in all_servers:
        s_url = url_for('admin_realtor', view='compare', a=a_param, b=b_param, srv=s)
        s_cls = "" if s == selected_srv else ' class="ghost"'
        srv_pills += f'<a href="{s_url}"><button type="button"{s_cls}>{server_label(s)}</button></a> '

    # Часы, доступные для сравнения: если выбран конкретный сервер — только
    # те раунды, где по нему реально есть данные; иначе — все раунды.
    if selected_srv:
        available_buckets = [b for b in sessions_index if bucket_data.get(b, {}).get(selected_srv)]
    else:
        available_buckets = sessions_index

    if len(available_buckets) < 2:
        srv_name = server_label(selected_srv) if selected_srv else "выбранных серверов"
        return f"""
        <div class="card" style="margin-bottom:14px">
          <div class="row" style="gap:8px;flex-wrap:wrap">{srv_pills}</div>
        </div>
        <div class="card">Недостаточно сканов для сервера {srv_name} — нужно минимум 2, доступно: {len(available_buckets)}.</div>
        """

    def _parse_bucket(param, fallback):
        try:
            v = int(param)
            return v if v in available_buckets else fallback
        except (ValueError, TypeError):
            return fallback

    bucket_a = _parse_bucket(a_param, available_buckets[1] if len(available_buckets) > 1 else available_buckets[0])
    bucket_b = _parse_bucket(b_param, available_buckets[0])
    older, newer = min(bucket_a, bucket_b), max(bucket_a, bucket_b)
    elapsed_hours = round((newer - older) / 3600)

    def _time_pills(field, current):
        pills = ""
        for b in available_buckets:
            label = format_msk(b, "%H:00 (%d.%m)")
            active = (b == current)
            other = bucket_b if field == "a" else bucket_a
            a_val = b if field == "a" else other
            b_val = other if field == "a" else b
            url = url_for('admin_realtor', view='compare', a=a_val, b=b_val, srv=selected_srv)
            cls = "" if active else ' class="ghost"'
            pills += f'<a href="{url}"><button type="button"{cls}>{label}</button></a> '
        return pills

    picker = f"""
    <div class="card" style="margin-bottom:14px">
      <div style="color:var(--muted);font-size:13px;margin-bottom:6px">Сервер:</div>
      <div class="row" style="gap:8px;flex-wrap:wrap;margin-bottom:14px">{srv_pills}</div>
      <div style="color:var(--muted);font-size:13px;margin-bottom:6px">Скан 1:</div>
      <div class="row" style="gap:8px;flex-wrap:wrap;margin-bottom:14px">{_time_pills('a', bucket_a)}</div>
      <div style="color:var(--muted);font-size:13px;margin-bottom:6px">Скан 2:</div>
      <div class="row" style="gap:8px;flex-wrap:wrap">{_time_pills('b', bucket_b)}</div>
      <div style="color:var(--muted);font-size:13px;margin-top:10px">между сканами: {elapsed_hours}ч</div>
    </div>
    """

    all_live_props = db.reference("properties").get() or {}

    def _render_window_cards(bucket, snap):
        """Одно окно скана (как в 'Снэпшотах') — только PD, без диффа."""
        wcards = ""
        srv_list = [selected_srv] if selected_srv else sorted(
            snap.keys(), key=lambda s: SERVER_ORDER.index(s) if s in SERVER_ORDER else 999
        )
        for srv in srv_list:
            if allowed is not None and srv not in allowed:
                continue
            entries = snap.get(srv)
            if not isinstance(entries, dict) or not entries:
                continue
            live_props = all_live_props.get(srv) if isinstance(all_live_props, dict) else {}
            if not isinstance(live_props, dict):
                live_props = {}

            def _sort_key(item):
                v = item[1]
                prop_type = v.get("propType") if isinstance(v, dict) else "house"
                type_rank = 0 if prop_type == "house" else 1
                pos = v.get("pos") if isinstance(v, dict) else None
                pos_rank = pos if isinstance(pos, (int, float)) else 9999
                return (type_rank, pos_rank)

            rows = ""
            for key, v in sorted(entries.items(), key=_sort_key):
                if not isinstance(v, dict):
                    continue
                icon = "🏠" if v.get("propType") == "house" else "🏢"
                label = v.get("propId") or v.get("pos") or "—"
                pd_val = v.get("pd", "?")
                live_v = live_props.get(key) if isinstance(live_props, dict) else None
                insured_flag = infer_insured(live_v) if isinstance(live_v, dict) else v.get("insured")
                shield_on   = "background:var(--accent);color:#0d0f14;border-color:var(--accent)" if insured_flag is True  else ""
                noentry_on  = "background:var(--danger);color:#0d0f14;border-color:var(--danger)" if insured_flag is False else ""
                rows += f"""<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 3px;border-bottom:1px solid var(--border)">
                    <div style="min-width:0;flex:1 1 auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{v.get('pos','—')}. {icon} №{label}</div>
                    <div style="flex:0 0 auto"><span class="pill bad" style="font-size:10px;padding:1px 6px">{pd_val} PD</span></div>
                    <div style="display:flex;gap:3px;flex:0 0 auto">
                      <form class="inline" method="post" action="{url_for('admin_realtor_set_insured', server=srv, key=key)}">
                        <input type="hidden" name="view" value="compare">
                        <input type="hidden" name="a" value="{bucket_a}">
                        <input type="hidden" name="b" value="{bucket_b}">
                        <input type="hidden" name="srv" value="{selected_srv}">
                        <input type="hidden" name="insured" value="1">
                        <button type="submit" class="ghost" title="Застрахован" style="padding:2px 5px;font-size:10px;line-height:1;{shield_on}">🛡</button>
                      </form>
                      <form class="inline" method="post" action="{url_for('admin_realtor_set_insured', server=srv, key=key)}">
                        <input type="hidden" name="view" value="compare">
                        <input type="hidden" name="a" value="{bucket_a}">
                        <input type="hidden" name="b" value="{bucket_b}">
                        <input type="hidden" name="srv" value="{selected_srv}">
                        <input type="hidden" name="insured" value="0">
                        <button type="submit" class="ghost" title="Не застрахован" style="padding:2px 5px;font-size:10px;line-height:1;{noentry_on}">🚫</button>
                      </form>
                      <form class="inline" method="post" action="{url_for('admin_realtor_delete', server=srv, key=key)}" onsubmit="return confirm('Убрать этот дом из списка?')">
                        <input type="hidden" name="view" value="compare">
                        <input type="hidden" name="a" value="{bucket_a}">
                        <input type="hidden" name="b" value="{bucket_b}">
                        <input type="hidden" name="srv" value="{selected_srv}">
                        <input type="hidden" name="bucket" value="{bucket}">
                        <button type="submit" class="danger" title="Удалить" style="padding:2px 5px;font-size:10px;line-height:1">✕</button>
                      </form>
                    </div>
                  </div>"""

            wcards += f"""
            <div class="card" style="padding:10px 12px;font-size:12px">
              <div style="font-weight:700;margin-bottom:6px;font-size:13px">{server_label(srv)}</div>
              {rows}
            </div>"""

        return wcards or "<div class='card'>Нет данных за это время.</div>"

    windows = f"""
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;align-items:start">
      <div>
        <div style="font-weight:700;margin-bottom:8px">Скан 1 — {format_msk(bucket_a, "%H:00 (%d.%m)")}</div>
        {_render_window_cards(bucket_a, bucket_data.get(bucket_a, {}))}
      </div>
      <div>
        <div style="font-weight:700;margin-bottom:8px">Скан 2 — {format_msk(bucket_b, "%H:00 (%d.%m)")}</div>
        {_render_window_cards(bucket_b, bucket_data.get(bucket_b, {}))}
      </div>
    </div>
    """
    return picker + windows

@app.route("/admin/realtor")
@login_required
def admin_realtor():
    view = request.args.get("view", "snapshot")
    if view not in ("snapshot", "history", "compare"):
        view = "snapshot"

    tabs = _realtor_tabs(view)
    allowed = allowed_servers_for_current_user() if current_role() == "editor" else None

    if view == "snapshot":
        content = _render_snapshot_mode(request.args.get("t", ""), allowed=allowed)
    elif view == "compare":
        content = _render_compare_mode(request.args.get("a", ""), request.args.get("b", ""), allowed=allowed,
                                        srv_param=request.args.get("srv", ""))
    else:
        content = _render_history_mode(allowed=allowed)

    return render_page("Риелторка", "realtor", f"<h1>Риелторка</h1>{tabs}{content}{REALTOR_JS}")

def _realtor_redirect_url():
    view = request.form.get("view", "snapshot")
    if view == "history":
        return url_for("admin_realtor", view="history")
    if view == "compare":
        return url_for("admin_realtor", view="compare", a=request.form.get("a", ""), b=request.form.get("b", ""),
                        srv=request.form.get("srv", ""))
    return url_for("admin_realtor", view="snapshot", t=request.form.get("t", ""))

@app.route("/admin/realtor/<server>/<key>/set-insured", methods=["POST"])
@login_required
def admin_realtor_set_insured(server, key):
    if current_role() == "editor" and server not in allowed_servers_for_current_user():
        flash_msg("err", "У вас нет доступа к этому серверу")
        return redirect(url_for("admin_realtor"))

    insured = request.form.get("insured") == "1"
    prop = db.reference(f"properties/{server}/{key}").get()
    if isinstance(prop, dict):
        d_val   = D_INSURED if insured else D_UNINSURED
        drop_at = get_drop_at(server, prop.get("propType", "house"), insured)
        db.reference(f"properties/{server}/{key}").update({
            "insured": insured, "d": d_val, "dropAt": drop_at,
        })
        flash_msg("ok", "Статус страховки обновлён")
    else:
        flash_msg("err", "Запись уже не активна (истекла) — статус не изменён")

    return redirect(_realtor_redirect_url())

@app.route("/admin/realtor/<server>/<key>/delete", methods=["POST"])
@login_required
def admin_realtor_delete(server, key):
    if current_role() == "editor" and server not in allowed_servers_for_current_user():
        flash_msg("err", "У вас нет доступа к этому серверу")
        return redirect(url_for("admin_realtor"))

    view = request.form.get("view", "snapshot")

    # Всегда убираем из "живого" списка (properties) — это то, что видно на
    # главной странице "Слёты" и что использует автоопределение страховки.
    db.reference(f"properties/{server}/{key}").delete()

    # Раньше отсюда ВСЕГДА удиралась и вся history/{server}/{key} — из-за
    # этого чистка дублей в "Снэпшотах"/"Сравнении" попутно стирала историю
    # сканов по этой позиции, и раздел "История" пустел без видимой причины.
    # Теперь трогаем history только когда удаление реально инициировано из
    # вкладки "История" — там это ожидаемое поведение (там и данные оттуда
    # же читаются).
    if view == "history":
        db.reference(f"history/{server}/{key}").delete()
    else:
        # А "Снэпшоты"/"Сравнение" рендерятся из sessions/{bucket}/{server},
        # а не из properties — раньше удаление никак её не трогало, поэтому
        # дом просто оставался на экране после нажатия ✕, хотя из properties
        # он уже пропадал. Чистим именно тот скан-час, из которого нажали ✕.
        if view == "compare":
            bucket = request.form.get("bucket", "")
        else:
            bucket = request.form.get("t", "")
        if bucket:
            try:
                bucket_int = int(bucket)
            except (TypeError, ValueError):
                bucket_int = None
            if bucket_int is not None:
                db.reference(f"sessions/{bucket_int}/{server}/{key}").delete()

    flash_msg("ok", "Дом убран из списка")
    return redirect(_realtor_redirect_url())

@app.route("/admin/broadcast")
@admin_only
def admin_broadcast():
    if not BOT_TOKEN:
        flash_msg("err", "BOT_TOKEN не задан в переменных окружения — рассылка недоступна")
    body = f"""
    <h1>Рассылка</h1>
    <div class="card">
      <form method="post" action="{url_for('admin_broadcast_send')}">
        <textarea name="text" placeholder="Текст сообщения (поддерживается Markdown)" required></textarea><br><br>
        <label style="color:var(--muted);font-size:13px">
          <input type="checkbox" name="skip_banned" checked style="width:auto"> Не отправлять забаненным
        </label><br><br>
        <button type="submit" onclick="return confirm('Отправить сообщение всем пользователям?')">Отправить всем</button>
      </form>
    </div>"""
    return render_page("Рассылка", "broadcast", body)

@app.route("/admin/broadcast/send", methods=["POST"])
@admin_only
def admin_broadcast_send():
    text = request.form.get("text", "").strip()
    if not text:
        flash_msg("err", "Пустой текст сообщения")
        return redirect(url_for("admin_broadcast"))
    if not BOT_TOKEN:
        flash_msg("err", "BOT_TOKEN не задан — не могу отправить")
        return redirect(url_for("admin_broadcast"))

    skip_banned = bool(request.form.get("skip_banned"))
    users_data  = db.reference("users").get() or {}
    banned_data = db.reference("banned").get() or {}
    banned_ids  = {str(k) for k in banned_data.keys()}

    sent, failed = 0, 0
    for uid in users_data.keys():
        if skip_banned and str(uid) in banned_ids:
            continue
        try:
            ok = tg_send(int(uid), text)
            sent += 1 if ok else 0
            failed += 0 if ok else 1
        except Exception:
            failed += 1

    flash_msg("ok", f"Отправлено: {sent}, не доставлено: {failed}")
    return redirect(url_for("admin_broadcast"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)