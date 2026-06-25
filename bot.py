import asyncio, time, os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import firebase_admin
from firebase_admin import credentials, db
import json

# Инициализация Firebase через переменную окружения
if not firebase_admin._apps:
    firebase_json = os.environ.get("FIREBASE_CREDENTIALS")
    if firebase_json:
        cred_dict = json.loads(firebase_json)
        cred = credentials.Certificate(cred_dict)
    else:
        cred = credentials.Certificate("serviceAccount.json")

    firebase_admin.initialize_app(cred, {
        "databaseURL": "https://arizona-property-tracker-default-rtdb.europe-west1.firebasedatabase.app"
    })

# ── Настройки ─────────────────────────────────────────────
BOT_TOKEN       = "8553840061:AAGGy8Zh4muaVgjwmU85tBoyIusAVP4bazE"
NOTIFY_HOURS    = 3        # за сколько часов предупреждать о слёте
CHECK_INTERVAL  = 300      # проверка каждые 5 минут

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
            })

    result.sort(key=lambda x: x["expiryTs"])
    return result

def format_time(ts):
    today = datetime.now().strftime("%d.%m")
    dt    = datetime.fromtimestamp(ts)
    day   = dt.strftime("%d.%m")
    hm    = dt.strftime("%H:%M")
    return hm if day == today else f"{hm} {day}"

def build_list_text(props, title="📋 Актуальные слёты"):
    if not props:
        return "✅ Слётов нет или данных пока нет."

    lines = [f"*{title}*\n"]
    for p in props:
        bar = "🔴" if p["hoursLeft"] <= 1 else "🟡" if p["hoursLeft"] <= 3 else "🟢"
        lines.append(
            f"{bar} *{p['server']}* — {p['propType']}\n"
            f"    ⏰ Слетит в {format_time(p['expiryTs'])} "
            f"(через {p['hoursLeft']}ч)"
        )
    return "\n".join(lines)

def get_servers():
    ref  = db.reference("properties")
    data = ref.get() or {}
    return sorted(data.keys())

# ── Команды ───────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Arizona Property Tracker*\n\n"
        "Команды:\n"
        "/list — все ближайшие слёты\n"
        "/soon — слёты в ближайшие 3 часа\n"
        "/servers — выбрать сервер\n"
        "/notify — подписаться на уведомления\n"
        "/unnotify — отписаться"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    props = get_all_props()
    await update.message.reply_text(
        build_list_text(props),
        parse_mode="Markdown"
    )

async def cmd_soon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now   = int(time.time())
    props = [p for p in get_all_props() if p["hoursLeft"] <= 3]
    await update.message.reply_text(
        build_list_text(props, "⚠️ Слёты в ближайшие 3 часа"),
        parse_mode="Markdown"
    )

async def cmd_servers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    servers = get_servers()
    if not servers:
        await update.message.reply_text("Данных пока нет.")
        return

    buttons = [
        [InlineKeyboardButton(s, callback_data=f"srv_{s}")]
        for s in servers
    ]
    await update.message.reply_text(
        "Выбери сервер:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def cb_server(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    server = query.data.replace("srv_", "")
    now    = int(time.time())

    all_props = get_all_props()
    props     = [p for p in all_props if p["server"] == server]

    await query.edit_message_text(
        build_list_text(props, f"📋 {server}"),
        parse_mode="Markdown"
    )

# ── Уведомления ───────────────────────────────────────────
subscribers = set()

async def cmd_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subscribers.add(update.effective_chat.id)
    await update.message.reply_text(
        f"🔔 Подписался! Буду предупреждать за {NOTIFY_HOURS}ч до слёта."
    )

async def cmd_unnotify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subscribers.discard(update.effective_chat.id)
    await update.message.reply_text("🔕 Отписался от уведомлений.")

notified = set()  # чтобы не спамить одно и то же

async def check_and_notify(app):
    props = get_all_props()
    for p in props:
        if p["hoursLeft"] <= NOTIFY_HOURS:
            key = f"{p['server']}_{p['propType']}_{p['expiryTs']}"
            if key not in notified:
                notified.add(key)
                text = (
                    f"⚠️ *Скоро слёт!*\n"
                    f"Сервер: *{p['server']}*\n"
                    f"Тип: {p['propType']}\n"
                    f"Слетит в {format_time(p['expiryTs'])} "
                    f"(через {p['hoursLeft']}ч)"
                )
                for chat_id in subscribers:
                    try:
                        await app.bot.send_message(chat_id, text, parse_mode="Markdown")
                    except Exception:
                        pass

async def notify_loop(app):
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        await check_and_notify(app)

# ── Запуск ────────────────────────────────────────────────
def main():
    app = Application.builder().token(
        os.environ.get("BOT_TOKEN", BOT_TOKEN)
    ).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("list",     cmd_list))
    app.add_handler(CommandHandler("soon",     cmd_soon))
    app.add_handler(CommandHandler("servers",  cmd_servers))
    app.add_handler(CommandHandler("notify",   cmd_notify))
    app.add_handler(CommandHandler("unnotify", cmd_unnotify))
    app.add_handler(CallbackQueryHandler(cb_server, pattern="^srv_"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(notify_loop(app))

    print("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()