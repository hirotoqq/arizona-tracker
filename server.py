from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, db
import os, time, json

app = Flask(__name__)

# Инициализация Firebase через переменную окружения
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

@app.route("/update", methods=["POST"])
def update():
    raw = request.get_data(as_text=False)
    print(f"DEBUG raw={raw[:200]}")
    try:
    data = json.loads(raw.decode('utf-8', errors='replace'))
except Exception as e:
    print(f"DEBUG parse error: {e}")
    return jsonify({"error": "parse error"}), 400
    if not data:
        print("DEBUG: no data received")
        return jsonify({"error": "no data"}), 400

    server  = data.get("server")
    entries = data.get("entries", [])

    print(f"DEBUG server={server} entries={entries}")

    if not server or not entries:
        print("DEBUG: missing fields")
        return jsonify({"error": "missing fields"}), 400

    now = int(time.time())
    ref = db.reference(f"properties/{server}")

    existing = ref.get() or {}

    incoming_types = set(e["propType"] for e in entries)

    kept = {
        k: v for k, v in existing.items()
        if v.get("propType") not in incoming_types
        and v.get("expiryTs", 0) > now
    }

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
    now           = int(time.time())
    server_filter = request.args.get("server")
    hours_max     = request.args.get("hours")

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


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)