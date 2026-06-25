from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, db
import os, time

app = Flask(__name__)

# Инициализация Firebase
cred = credentials.Certificate("serviceAccount.json")  # твой скачанный json
firebase_admin.initialize_app(cred, {
    "databaseURL": "https://arizona-property-tracker-default-rtdb.europe-west1.firebasedatabase.app"
})

@app.route("/update", methods=["POST"])
def update():
    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400

    server  = data.get("server")
    entries = data.get("entries", [])

    if not server or not entries:
        return jsonify({"error": "missing fields"}), 400

    now = int(time.time())
    ref = db.reference(f"properties/{server}")

    # Читаем существующие записи
    existing = ref.get() or {}

    # Определяем какие типы пришли в этом скане
    incoming_types = set(e["propType"] for e in entries)

    # Удаляем старые записи только того же типа
    kept = {
        k: v for k, v in existing.items()
        if v.get("propType") not in incoming_types
        and v.get("expiryTs", 0) > now
    }

    # Добавляем новые
    for e in entries:
        key = f"{e['propType']}_{e['expiryTs']}_{now}"
        kept[key] = {
            "server":   server,
            "propType": e["propType"],
            "pd":       e["pd"],
            "expiryTs": e["expiryTs"],
            "scanTs":   now,
        }

    ref.set(kept)
    return jsonify({"ok": True, "written": len(entries)})


@app.route("/list", methods=["GET"])
def list_props():
    now        = int(time.time())
    server_filter = request.args.get("server")
    hours_max  = request.args.get("hours")  # фильтр "покажи слёты в ближайшие N часов"

    ref  = db.reference("properties")
    data = ref.get() or {}

    result = []
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)