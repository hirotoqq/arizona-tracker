import threading
import asyncio
import os
from flask import Flask
from server import app as flask_app

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

async def run_bot():
    import json
    import time
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
    import firebase_admin
    from firebase_admin import credentials, db

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

    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    NOTIFY_HOURS = 3
    subscribers  = set()
    notified     = set()

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
        from datetime import datetime
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

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 *Arizona Property Tracker*\n\n"
            "Команды:\n"
            "/list — все ближайшие слёты\n"
            "/soon — слёты в ближайшие 3 часа\n"
            "/servers — выбрать сервер\n"
            "/notify — подписаться на уведомления\n"
            "/unnotify — отписаться",
            parse_mode="Markdown"
        )

    async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            build_list_text(get_all_props()), parse_mode="Markdown"
        )

    async def cmd_soon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        props = [p for p in get_all_props() if p["hoursLeft"] <= 3]
        await update.message.reply_text(
            build_list_text(props, "⚠️ Слёты в ближайшие 3 часа"),
            parse_mode="Markdown"
        )

    async def cmd_servers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ref  = db.reference("properties")
        data = ref.get() or {}
        servers = sorted(data.keys())
        if not servers:
            await update.message.reply_text("Данных пока нет.")
            return
        buttons = [[InlineKeyboardButton(s, callback_data=f"srv_{s}")] for s in servers]
        await update.message.reply_text(
            "Выбери сервер:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def cb_server(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query  = update.callback_query
        await query.answer()
        server = query.data.replace("srv_", "")
        props  = [p for p in get_all_props() if p["server"] == server]
        await query.edit_message_text(
            build_list_text(props, f"📋 {server}"), parse_mode="Markdown"
        )

    async def cmd_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        subscribers.add(update.effective_chat.id)
        await update.message.reply_text(f"🔔 Подписался! Буду предупреждать за {NOTIFY_HOURS}ч до слёта.")

    async def cmd_unnotify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        subscribers.discard(update.effective_chat.id)
        await update.message.reply_text("🔕 Отписался от уведомлений.")

    async def notify_loop(application):
        while True:
            await asyncio.sleep(300)
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
                                await application.bot.send_message(
                                    chat_id, text, parse_mode="Markdown"
                                )
                            except Exception:
                                pass

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start",    cmd_start))
    application.add_handler(CommandHandler("list",     cmd_list))
    application.add_handler(CommandHandler("soon",     cmd_soon))
    application.add_handler(CommandHandler("servers",  cmd_servers))
    application.add_handler(CommandHandler("notify",   cmd_notify))
    application.add_handler(CommandHandler("unnotify", cmd_unnotify))
    application.add_handler(CallbackQueryHandler(cb_server, pattern="^srv_"))

    print("Бот запущен!")

    async with application:
        await application.start()
        await application.updater.start_polling()
        asyncio.create_task(notify_loop(application))
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    asyncio.run(run_bot())