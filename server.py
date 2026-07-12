from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, db
import os, time, json, threading

app = Flask(__name__)

if not firebase_admin._apps:
    firebase_json = os.environ.get("FIREBASE_CREDENTIALS")
    if firebase_json:
        cred_dict = json.loads(firebase_json)
        cred = credentials.Certificate(cred_dict)
    else:
        cred = credentials.Certificate("serviceAccount.json")
    database_url = os.environ.get(
        "FIREBASE_DATABASE_URL",
        "https://kotak-88887-default-rtdb.firebaseio.com"
    )
    firebase_admin.initialize_app(cred, {
        "databaseURL": database_url
    })

SECRET_KEY = os.environ.get("SECRET_KEY", "")

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
    VALID_SERVERS = {
        "Phoenix","Tucson","Scottdale","Chandler","Brainburg","Saint-Rose",
        "Mesa","Red-Rock","Yuma","Surprise","Prescott","Glendale",
        "Kingman","Winslow","Payson","Gilbert","Show Low","Casa-Grande",
        "Page","Sun-City","Queen-Creek","Sedona","Holiday","Wednesday",
        "Yava","Faraway","Bumble Bee","Christmas","Love","Mirage","Drake","Space",
    }
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
        # Пропускаем объекты с pd > 65 или pd <= 0
        if pd > 65 or pd <= 0:
            continue
        # Пропускаем неизвестные типы
        if e.get("propType") not in ("house", "business"):
            continue
        expiry_h = (e["expiryTs"] // 3600) * 3600
        # Пропускаем если время уже прошло
        if expiry_h <= now:
            continue
        key = f"{e['propType']}_{expiry_h}"
        if key not in kept:
            kept[key] = {
                "server":   server,
                "propType": e["propType"],
                "pd":       pd,
                "expiryTs": expiry_h,
                "scanTs":   now,
                "count":    0,
            }
        kept[key]["count"] = kept[key].get("count", 0) + 1
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)