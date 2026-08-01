from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string
import firebase_admin
from firebase_admin import credentials, db
import os, time, json, threading, secrets, string
from functools import wraps
from datetime import timedelta
import requests

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
    # Проверка секретного ключа
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

    # Валидация сервера
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

    written = 0
    for e in entries:
        pd = e.get("pd", 0)
        if pd > 65 or pd <= 0:
            continue
        if e.get("propType") not in ("house", "business"):
            continue
        expiry_h = (e["expiryTs"] // 3600) * 3600
        if expiry_h <= now:
            continue

        prop_id = e.get("propId")
        pos     = e.get("pos")

        # Если есть ID — храним каждый объект отдельно
        if prop_id:
            key = f"{e['propType']}_{prop_id}"
            kept[key] = {
                "server":   server,
                "propType": e["propType"],
                "pd":       pd,
                "expiryTs": expiry_h,
                "scanTs":   now,
                "propId":   prop_id,
                "pos":      pos,
                "count":    1,
            }
        else:
            # Без ID — группируем по времени
            key = f"{e['propType']}_{expiry_h}_{pos or 0}"
            if key not in kept:
                kept[key] = {
                    "server":   server,
                    "propType": e["propType"],
                    "pd":       pd,
                    "expiryTs": expiry_h,
                    "scanTs":   now,
                    "propId":   None,
                    "pos":      pos,
                    "count":    1,
                }
        written += 1

    ref.set(kept)
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

# ══════════════════════════════════════════════════════════
#  ADMIN PANEL
# ══════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper

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
</style>
"""

def render_page(title, active, body):
    nav_items = [
        ("dashboard", "Дашборд"), ("keys", "Ключи"), ("users", "Пользователи"),
        ("properties", "Слёты"), ("broadcast", "Рассылка"),
    ]
    nav = "".join(
        f'<a href="{url_for("admin_"+ep)}" class="{"active" if active==ep else ""}">{label}</a>'
        for ep, label in nav_items
    )
    flashes = ""
    for kind, msg in session.pop("_flashes", []):
        flashes += f'<div class="flash {kind}">{msg}</div>'
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Arizona Tracker Admin</title>{BASE_CSS}</head><body>
<div class="nav">
  <span class="brand">🏙 Arizona Tracker</span>
  {nav}
  <span class="spacer"></span>
  <a href="{url_for('admin_logout')}">Выйти</a>
</div>
<div class="wrap">{flashes}{body}</div>
</body></html>"""

def flash_msg(kind, msg):
    flashes = session.get("_flashes", [])
    flashes.append((kind, msg))
    session["_flashes"] = flashes

# ── Логин ─────────────────────────────────────────────────
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if (not ADMIN_PASSWORD or
                not secrets.compare_digest(u, ADMIN_USERNAME) or
                not secrets.compare_digest(p, ADMIN_PASSWORD)):
            error = '<div class="flash err">Неверный логин или пароль</div>'
        else:
            session.permanent = True
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
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
    return redirect(url_for("admin_dashboard"))

# ── Дашборд ───────────────────────────────────────────────
@app.route("/admin/dashboard")
@login_required
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

# ── Ключи ─────────────────────────────────────────────────
@app.route("/admin/keys")
@login_required
def admin_keys():
    data = db.reference("access_keys").get() or {}
    rows = ""
    for key, v in sorted(data.items(), key=lambda kv: kv[1].get("created_at", 0) if isinstance(kv[1], dict) else 0, reverse=True):
        if not isinstance(v, dict):
            continue
        activated = v.get("activated")
        status    = '<span class="pill ok">использован</span>' if activated else '<span class="pill muted">свободен</span>'
        used_by   = v.get("activated_by", "—")
        created   = time.strftime("%d.%m.%Y %H:%M", time.localtime(v.get("created_at", 0)))
        revoke_btn = (
            f'<form class="inline" method="post" action="{url_for("admin_key_revoke", key=key)}" '
            f'onsubmit="return confirm(\'Удалить ключ {key}?\')">'
            f'<button class="danger" type="submit">Удалить</button></form>'
        ) if not activated else ""
        rows += (
            f"<tr><td class='mono'>{key}</td><td>{v.get('duration_days','?')} дн.</td>"
            f"<td>{status}</td><td>{used_by}</td><td>{created}</td><td>{revoke_btn}</td></tr>"
        )

    body = f"""
    <h1>Ключи доступа</h1>
    <div class="card">
      <form method="post" action="{url_for('admin_key_generate')}" class="row">
        <input type="number" name="days" placeholder="Дней" min="1" required style="width:100px">
        <input type="number" name="count" placeholder="Количество" min="1" max="50" value="1" style="width:130px">
        <button type="submit">Сгенерировать</button>
      </form>
    </div>
    <table>
      <tr><th>Ключ</th><th>Срок</th><th>Статус</th><th>Активировал</th><th>Создан</th><th></th></tr>
      {rows or "<tr><td colspan='6'>Ключей пока нет</td></tr>"}
    </table>"""
    return render_page("Ключи", "keys", body)

@app.route("/admin/keys/generate", methods=["POST"])
@login_required
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
@login_required
def admin_key_revoke(key):
    ref  = db.reference(f"access_keys/{key}")
    data = ref.get()
    if data and not data.get("activated"):
        ref.delete()
        flash_msg("ok", f"Ключ {key} удалён")
    else:
        flash_msg("err", "Ключ уже использован или не найден")
    return redirect(url_for("admin_keys"))

# ── Пользователи ──────────────────────────────────────────
@app.route("/admin/users")
@login_required
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
            access = f'<span class="pill ok">до {time.strftime("%d.%m.%Y %H:%M", time.localtime(exp))}</span>'
        elif exp:
            access = '<span class="pill bad">истёк</span>'
        else:
            access = '<span class="pill muted">нет</span>'

        ban_pill = '<span class="pill bad">забанен</span>' if is_banned else ""
        ban_btn  = (
            f'<form class="inline" method="post" action="{url_for("admin_user_unban", uid=uid)}">'
            f'<button class="ghost" type="submit">Разбанить</button></form>'
            if is_banned else
            f'<form class="inline" method="post" action="{url_for("admin_user_ban", uid=uid)}" '
            f'onsubmit="return prompt(\'Причина бана:\')!=null">'
            f'<input type="hidden" name="reason" value="через админку">'
            f'<button class="danger" type="submit">Забанить</button></form>'
        )
        rows += f"""<tr>
          <td class='mono'>{uid}</td><td>{uname}</td><td>{name}</td>
          <td>{access} {ban_pill}</td>
          <td>{u.get('last_seen','—')}</td>
          <td class="row">
            <form class="inline" method="post" action="{url_for('admin_user_extend', uid=uid)}">
              <input type="number" name="days" placeholder="дней" min="1" style="width:70px">
              <button type="submit">+</button>
            </form>
            {ban_btn}
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
    <table>
      <tr><th>ID</th><th>Ник</th><th>Имя</th><th>Доступ</th><th>Был(а)</th><th>Действия</th></tr>
      {rows or "<tr><td colspan='6'>Никого не найдено</td></tr>"}
    </table>"""
    return render_page("Пользователи", "users", body)

@app.route("/admin/users/<uid>/extend", methods=["POST"])
@login_required
def admin_user_extend(uid):
    try:
        days = int(request.form.get("days", 0))
    except ValueError:
        days = 0
    if days <= 0:
        flash_msg("err", "Укажи число дней больше нуля")
        return redirect(url_for("admin_users"))

    now     = int(time.time())
    sub     = db.reference(f"subscriptions/{uid}").get() or {}
    current = sub.get("expires_at", 0) if isinstance(sub, dict) else 0
    base    = max(now, current)
    expires_at = base + days * 86400
    db.reference(f"subscriptions/{uid}").set({
        "key": "admin_panel", "expires_at": expires_at, "activated_at": now,
    })
    tg_send(int(uid), f"⏳ Твой доступ продлён администратором до {time.strftime('%d.%m.%Y %H:%M', time.localtime(expires_at))} МСК.")
    flash_msg("ok", f"Доступ для {uid} продлён на {days} дн.")
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<uid>/ban", methods=["POST"])
@login_required
def admin_user_ban(uid):
    reason  = request.form.get("reason", "через админку")
    now_str = time.strftime("%d.%m.%Y %H:%M МСК")
    db.reference(f"banned/{uid}").set({"reason": reason, "date": now_str})
    flash_msg("ok", f"Пользователь {uid} забанен")
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<uid>/unban", methods=["POST"])
@login_required
def admin_user_unban(uid):
    db.reference(f"banned/{uid}").delete()
    flash_msg("ok", f"Пользователь {uid} разбанен")
    return redirect(url_for("admin_users"))

# ── Слёты (properties) ────────────────────────────────────
@app.route("/admin/properties")
@login_required
def admin_properties():
    now      = int(time.time())
    data     = db.reference("properties").get() or {}
    srv_filter = request.args.get("server", "")

    all_entries = []
    for srv, entries in data.items():
        if not isinstance(entries, dict):
            continue
        if srv_filter and srv != srv_filter:
            continue
        for k, v in entries.items():
            if not isinstance(v, dict):
                continue
            all_entries.append((srv, k, v))

    # Ближайшие слёты — первыми
    all_entries.sort(key=lambda item: item[2].get("expiryTs", 0))

    rows = ""
    for i, (srv, k, v) in enumerate(all_entries):
        expiry = v.get("expiryTs", 0)
        expired = expiry <= now
        when = time.strftime("%d.%m.%Y %H:%M", time.localtime(expiry)) if expiry else "—"
        dt_local = time.strftime("%Y-%m-%dT%H:%M", time.localtime(expiry)) if expiry else ""
        status = '<span class="pill bad">истёк</span>' if expired else '<span class="pill ok">активен</span>'
        row_id = f"pr{i}"

        del_btn = (
            f'<form class="inline" method="post" action="{url_for("admin_property_delete", server=srv, key=k)}" '
            f'onsubmit="return confirm(\'Удалить запись?\')"><button class="danger" type="submit">Удалить</button></form>'
        )
        edit_btn = f'<button class="ghost" type="button" onclick="toggleEdit(\'{row_id}\')">Изменить</button>'

        rows += (
            f"<tr id='view-{row_id}'><td>{server_label(srv)}</td><td>{v.get('propType','?')}</td><td>{v.get('pd','—')}</td>"
            f"<td>{v.get('propId') or v.get('pos') or '—'}</td><td>{when}</td><td>{status}</td><td class='row'>{edit_btn}{del_btn}</td></tr>"
        )

        server_opts = "".join(
            f'<option value="{s}" {"selected" if s == srv else ""}>{server_label(s)}</option>'
            for s in SERVER_ORDER if s in VALID_SERVERS
        )
        rows += f"""
        <tr id='edit-{row_id}' style='display:none'>
          <td colspan='7'>
            <form method="post" action="{url_for('admin_property_update', server=srv, key=k)}" class="row" style="flex-wrap:wrap;gap:8px;align-items:center">
              <select name="server">{server_opts}</select>
              <select name="propType">
                <option value="house" {"selected" if v.get("propType")=="house" else ""}>🏠 house</option>
                <option value="business" {"selected" if v.get("propType")=="business" else ""}>🏢 business</option>
              </select>
              <input type="number" name="pd" value="{v.get('pd', 0)}" min="1" max="65" required style="width:90px">
              <input type="datetime-local" name="expiry" value="{dt_local}" required>
              <input type="text" name="propId" placeholder="ID" value="{v.get('propId') or ''}" style="width:120px">
              <input type="text" name="pos" placeholder="Позиция" value="{v.get('pos') or ''}" style="width:120px">
              <button type="submit">Сохранить</button>
              <button type="button" class="ghost" onclick="toggleEdit('{row_id}')">Отмена</button>
            </form>
          </td>
        </tr>"""

    server_options = "".join(
        f'<option value="{s}" {"selected" if s == srv_filter else ""}>{server_label(s)}</option>' for s in SERVER_ORDER if s in VALID_SERVERS
    )
    body = f"""
    <h1>Данные о слётах</h1>
    <div class="card">
      <h2 style="margin-top:0">Добавить вручную</h2>
      <form method="post" action="{url_for('admin_property_add')}" class="row">
        <select name="server" required><option value="">Сервер</option>{server_options}</select>
        <select name="propType" required>
          <option value="house">🏠 house</option>
          <option value="business">🏢 business</option>
        </select>
        <input type="number" name="pd" placeholder="PayDay" min="1" max="65" required style="width:100px">
        <input type="number" name="hours" placeholder="Часов до слёта" min="0" step="0.1" required style="width:150px">
        <input type="text" name="propId" placeholder="ID (необязательно)" style="width:150px">
        <button type="submit">Добавить</button>
      </form>
    </div>
    <div class="card row">
      <form method="get">
        <select name="server" onchange="this.form.submit()">
          <option value="">Все серверы</option>{server_options}
        </select>
      </form>
    </div>
    <table>
      <tr><th>Сервер</th><th>Тип</th><th>PD</th><th>ID/поз.</th><th>Слёт</th><th>Статус</th><th></th></tr>
      {rows or "<tr><td colspan='7'>Записей нет</td></tr>"}
    </table>
    <script>
    function toggleEdit(id) {{
      var view = document.getElementById('view-' + id);
      var edit = document.getElementById('edit-' + id);
      if (!view || !edit) return;
      var editing = edit.style.display !== 'none';
      view.style.display = editing ? '' : 'none';
      edit.style.display = editing ? 'none' : '';
    }}
    </script>"""
    return render_page("Слёты", "properties", body)

@app.route("/admin/properties/add", methods=["POST"])
@login_required
def admin_property_add():
    server   = request.form.get("server", "")
    prop_type = request.form.get("propType", "")
    prop_id  = request.form.get("propId", "").strip()
    try:
        pd    = int(request.form.get("pd", 0))
        hours = float(request.form.get("hours", 0))
    except ValueError:
        flash_msg("err", "Неверные числовые значения")
        return redirect(url_for("admin_properties"))

    if server not in VALID_SERVERS or prop_type not in ("house", "business"):
        flash_msg("err", "Неверный сервер или тип")
        return redirect(url_for("admin_properties"))
    if pd <= 0 or pd > 65 or hours < 0:
        flash_msg("err", "PayDay 1–65, часы ≥ 0")
        return redirect(url_for("admin_properties"))

    now      = int(time.time())
    expiry_h = ((now + int(hours * 3600)) // 3600) * 3600
    key      = f"{prop_type}_{prop_id}" if prop_id else f"{prop_type}_{expiry_h}_admin_{secrets.token_hex(3)}"

    db.reference(f"properties/{server}/{key}").set({
        "server": server, "propType": prop_type, "pd": pd, "expiryTs": expiry_h,
        "scanTs": now, "propId": prop_id or None, "pos": None, "count": 1,
    })
    flash_msg("ok", "Запись добавлена")
    return redirect(url_for("admin_properties"))

@app.route("/admin/properties/<server>/<key>/delete", methods=["POST"])
@login_required
def admin_property_delete(server, key):
    db.reference(f"properties/{server}/{key}").delete()
    flash_msg("ok", "Запись удалена")
    return redirect(url_for("admin_properties"))

@app.route("/admin/properties/<server>/<key>/edit", methods=["GET"])
@login_required
def admin_property_edit(server, key):
    v = db.reference(f"properties/{server}/{key}").get()
    if not v or not isinstance(v, dict):
        flash_msg("err", "Запись не найдена")
        return redirect(url_for("admin_properties"))

    expiry    = v.get("expiryTs", 0)
    dt_local  = time.strftime("%Y-%m-%dT%H:%M", time.localtime(expiry)) if expiry else ""
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
          <label style="width:140px;color:var(--muted)">Время слёта</label>
          <input type="datetime-local" name="expiry" value="{dt_local}" required>
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
    old = db.reference(f"properties/{server}/{key}").get()
    if not old or not isinstance(old, dict):
        flash_msg("err", "Запись не найдена")
        return redirect(url_for("admin_properties"))

    new_server = request.form.get("server", server)
    prop_type  = request.form.get("propType", "house")
    prop_id    = request.form.get("propId", "").strip()
    pos        = request.form.get("pos", "").strip()
    expiry_str = request.form.get("expiry", "")
    try:
        pd = int(request.form.get("pd", 0))
    except ValueError:
        flash_msg("err", "Неверный PD")
        return redirect(url_for("admin_property_edit", server=server, key=key))

    if new_server not in VALID_SERVERS or prop_type not in ("house", "business"):
        flash_msg("err", "Неверный сервер или тип")
        return redirect(url_for("admin_property_edit", server=server, key=key))
    if pd <= 0 or pd > 65:
        flash_msg("err", "PayDay должен быть 1–65")
        return redirect(url_for("admin_property_edit", server=server, key=key))

    try:
        import datetime as _dt
        expiry_ts = int(_dt.datetime.strptime(expiry_str, "%Y-%m-%dT%H:%M").timestamp())
    except ValueError:
        flash_msg("err", "Неверный формат времени")
        return redirect(url_for("admin_property_edit", server=server, key=key))

    updated = dict(old)
    updated.update({
        "server":   new_server,
        "propType": prop_type,
        "pd":       pd,
        "expiryTs": expiry_ts,
        "propId":   prop_id or None,
        "pos":      pos or None,
    })

    if new_server != server:
        db.reference(f"properties/{server}/{key}").delete()
        db.reference(f"properties/{new_server}/{key}").set(updated)
    else:
        db.reference(f"properties/{server}/{key}").set(updated)

    flash_msg("ok", "Запись обновлена")
    return redirect(url_for("admin_properties"))

# ── Рассылка ──────────────────────────────────────────────
@app.route("/admin/broadcast")
@login_required
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
@login_required
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