"""Telegram-бот для нагадувань про прийом ліків."""
import datetime as dt
import html
import logging
import os
import re
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    Defaults,
    MessageHandler,
    filters,
)

load_dotenv()

import db  # noqa: E402  (DB_PATH читається з .env)

BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHOR_URL = os.getenv("AUTHOR_URL", "https://t.me/")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("medbot")

# ---------- меню ----------
BTN_ADD = "➕ Додати ліки"
BTN_EDIT = "✏️ Редагувати нагадування"
BTN_DEL = "🗑 Видалити нагадування"
BTN_STOP = "⏸ Зупинити всі нагадування"
BTN_SOCIAL = "🌐 Соцмережа автора"
MENU_BUTTONS = [BTN_ADD, BTN_EDIT, BTN_DEL, BTN_STOP, BTN_SOCIAL]

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_ADD], [BTN_EDIT, BTN_DEL], [BTN_STOP], [BTN_SOCIAL]],
    resize_keyboard=True,
)
MENU_FILTER = filters.Regex("^(" + "|".join(re.escape(b) for b in MENU_BUTTONS) + ")$")
TEXT_INPUT = filters.TEXT & ~filters.COMMAND & ~MENU_FILTER

FOOD = {"after": "Після їжі", "before": "До їжі", "any": "Не важливо"}
MAX_PER_DAY = 12

# стани діалогу
(TARGET, TARGET_ID, NAME, FOOD_ST, COUNT, FIRST, LAST,
 COMMENT_Q, COMMENT, EDIT_PICK, EDIT_FIELD) = range(11)


# ---------- допоміжне ----------

def kb(rows: list[list[tuple[str, str]]], cancel: bool = True) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    if cancel:
        buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def parse_time(text: str) -> str | None:
    """'8', '8:30', '08.30', '20 15' -> 'HH:MM'."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.\-\s](\d{2}))?\s*", text)
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mnt > 59:
        return None
    return f"{h:02d}:{mnt:02d}"


def to_min(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def from_min(x: int) -> str:
    return f"{x // 60:02d}:{x % 60:02d}"


def distribute(first: str, last: str, n: int) -> list[str]:
    """Рівномірно розподіляє n прийомів між first і last (з округленням до 5 хв)."""
    if n == 1:
        return [first]
    f, l = to_min(first), to_min(last)
    step = (l - f) / (n - 1)
    result = []
    for i in range(n):
        if i == 0:
            x = f
        elif i == n - 1:
            x = l
        else:
            x = int(round((f + i * step) / 5) * 5)
        result.append(from_min(x))
    return sorted(set(result), key=to_min)


def user_label(user_id: int) -> str:
    u = db.get_user(user_id)
    if not u:
        return str(user_id)
    name = u.get("first_name") or ""
    if u.get("username"):
        name = f"{name} (@{u['username']})".strip()
    return html.escape(name or str(user_id))


def med_summary(med: dict, viewer_id: int | None = None) -> str:
    lines = [
        f"💊 <b>{html.escape(med['name'])}</b>",
        f"🍽 {FOOD[med['food']]}",
        f"⏰ {', '.join(med['times'])} ({len(med['times'])} р/день)",
    ]
    if med.get("comment"):
        lines.append(f"📝 {html.escape(med['comment'])}")
    if viewer_id is not None and med["owner_id"] != med["target_id"]:
        if med["owner_id"] == viewer_id:
            lines.append(f"👤 Пацієнт: {user_label(med['target_id'])}")
        else:
            lines.append(f"👨‍⚕️ Призначив: {user_label(med['owner_id'])}")
    if not med.get("active", 1):
        lines.append("⏸ <i>Призупинено</i>")
    return "\n".join(lines)


async def say(update: Update, text: str, markup=None) -> None:
    """Відповідь на повідомлення або натискання кнопки (кнопки старого повідомлення прибираються)."""
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_reply_markup(None)
        except BadRequest:
            pass
    await update.effective_chat.send_message(text, reply_markup=markup)


def register(update: Update) -> None:
    u = update.effective_user
    if u:
        db.save_user(u.id, u.username, u.first_name)


# ---------- розклад ----------

def unschedule(app: Application, med_id: int) -> None:
    for job in app.job_queue.get_jobs_by_name(f"med_{med_id}"):
        job.schedule_removal()


def schedule(app: Application, med: dict) -> None:
    unschedule(app, med["id"])
    if not med.get("active", 1):
        return
    for t in med["times"]:
        h, m = map(int, t.split(":"))
        app.job_queue.run_daily(
            send_reminder,
            time=dt.time(h, m, tzinfo=TZ),
            name=f"med_{med['id']}",
            data=med["id"],
        )


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    med = db.get_med(context.job.data)
    if not med or not med["active"]:
        return
    text = [f"⏰ <b>Час прийняти ліки!</b>\n\n💊 <b>{html.escape(med['name'])}</b>",
            f"🍽 {FOOD[med['food']]}"]
    if med["comment"]:
        text.append(f"📝 {html.escape(med['comment'])}")
    if med["owner_id"] != med["target_id"]:
        text.append(f"👨‍⚕️ Призначив: {user_label(med['owner_id'])}")
    try:
        await context.bot.send_message(
            med["target_id"],
            "\n".join(text),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Прийнято", callback_data=f"taken:{med['id']}")]]
            ),
        )
    except Forbidden:
        log.warning("Користувач %s заблокував бота", med["target_id"])


async def taken_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("Чудово! 💪")
    now = dt.datetime.now(TZ).strftime("%H:%M")
    await q.edit_message_text(q.message.text_html + f"\n\n✅ <i>Прийнято о {now}</i>")
    med = db.get_med(int(q.data.split(":")[1]))
    if med and med["owner_id"] != med["target_id"]:
        try:
            await context.bot.send_message(
                med["owner_id"],
                f"✅ {user_label(med['target_id'])} прийняв(ла) <b>{html.escape(med['name'])}</b> о {now}",
            )
        except Forbidden:
            pass


async def post_init(app: Application) -> None:
    db.init()
    meds = db.all_active()
    for med in meds:
        schedule(app, med)
    log.info("Заплановано нагадувань для %d ліків", len(meds))


# ---------- базові команди ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    register(update)
    context.user_data.clear()
    await update.effective_chat.send_message(
        "Привіт! 👋 Я нагадуватиму про прийом ліків.\n\n"
        f"Ваш ID: <code>{update.effective_user.id}</code>\n"
        "<i>Його можна передати лікарю чи рідним, щоб вони налаштували нагадування для вас.</i>\n\n"
        "Оберіть дію в меню 👇",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register(update)
    u = update.effective_user
    extra = f" або @{u.username}" if u.username else ""
    await update.message.reply_text(f"Ваш ID: <code>{u.id}</code>{extra}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await say(update, "Скасовано.", MAIN_MENU)
    return ConversationHandler.END


async def social(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Підписуйтесь на автора 🙌",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Перейти", url=AUTHOR_URL)]]),
    )


# ---------- кроки анкети (спільні для додавання та редагування) ----------

def draft(context) -> dict:
    return context.user_data.setdefault("draft", {})


def editing(context) -> bool:
    return context.user_data.get("mode") == "edit"


async def ask_name(update, context) -> int:
    await say(update, "Введіть назву ліків:", kb([]))
    return NAME


async def ask_food(update, context) -> int:
    await say(update, "Як приймати?", kb([[("🍽 Після їжі", "food:after"), ("⏳ До їжі", "food:before")],
                                          [("🤷 Не важливо", "food:any")]]))
    return FOOD_ST


async def ask_count(update, context) -> int:
    await say(update, "Скільки разів на день? (оберіть або введіть число)",
              kb([[(str(i), f"cnt:{i}") for i in range(1, 4)],
                  [(str(i), f"cnt:{i}") for i in range(4, 7)]]))
    return COUNT


async def ask_first(update, context) -> int:
    q = "О котрій прийом?" if draft(context)["count"] == 1 else "О котрій перший прийом?"
    presets = ["06:00", "07:00", "08:00", "09:00", "10:00", "11:00"]
    await say(update, f"{q}\nОберіть або введіть свій час, напр. <code>7:30</code>",
              kb([[(t, f"tm:{t}") for t in presets[:3]], [(t, f"tm:{t}") for t in presets[3:]]]))
    return FIRST


async def ask_last(update, context) -> int:
    presets = ["18:00", "19:00", "20:00", "21:00", "22:00", "23:00"]
    await say(update, "О котрій останній прийом?\nОберіть або введіть свій час, напр. <code>21:30</code>",
              kb([[(t, f"tm:{t}") for t in presets[:3]], [(t, f"tm:{t}") for t in presets[3:]]]))
    return LAST


async def ask_comment_q(update, context) -> int:
    await say(update, "Чи потрібен додатковий коментар? (буде показуватись у кожному нагадуванні)",
              kb([[("Так", "cmt:yes"), ("Ні", "cmt:no")]]))
    return COMMENT_Q


# --- додавання ---

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    register(update)
    context.user_data.clear()
    context.user_data["mode"] = "add"
    context.user_data["draft"] = {}
    await say(update, "Для кого нагадування?",
              kb([[("👤 Для себе", "tgt:self")], [("👥 Для іншого користувача", "tgt:other")]]))
    return TARGET


async def target_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query.data == "tgt:self":
        draft(context)["target_id"] = update.effective_user.id
        return await ask_name(update, context)
    await say(update,
              "Введіть ID або @username користувача.\n\n"
              "⚠️ Людина має спершу запустити бота (/start) — інакше Telegram не дозволить "
              "надсилати їй повідомлення. Свій ID вона побачить після /start або командою /myid.",
              kb([]))
    return TARGET_ID


async def target_id_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = db.find_user(update.message.text)
    if not user:
        await update.message.reply_text(
            "Не знайшов такого користувача серед тих, хто запускав бота 🤔\n"
            "Перевірте ID/@username або попросіть людину надіслати боту /start і спробуйте ще раз."
        )
        return TARGET_ID
    draft(context)["target_id"] = user["user_id"]
    await update.message.reply_text(f"Пацієнт: {user_label(user['user_id'])} ✅")
    return await ask_name(update, context)


async def name_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()[:100]
    draft(context)["name"] = name
    if editing(context):
        return await finish(update, context)
    return await ask_food(update, context)


async def food_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft(context)["food"] = update.callback_query.data.split(":")[1]
    if editing(context):
        return await finish(update, context)
    return await ask_count(update, context)


async def count_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.callback_query.data.split(":")[1] if update.callback_query else update.message.text.strip()
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_PER_DAY:
        await update.message.reply_text(f"Введіть число від 1 до {MAX_PER_DAY}.")
        return COUNT
    draft(context)["count"] = int(raw)
    return await ask_first(update, context)


async def first_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.callback_query.data[3:] if update.callback_query else update.message.text
    t = parse_time(raw)
    if not t:
        await update.message.reply_text("Не розпізнав час. Формат: <code>8:00</code> або <code>20:30</code>.")
        return FIRST
    d = draft(context)
    d["first"] = t
    if d["count"] == 1:
        d["times"] = [t]
        return await after_times(update, context)
    return await ask_last(update, context)


async def last_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.callback_query.data[3:] if update.callback_query else update.message.text
    t = parse_time(raw)
    d = draft(context)
    if not t:
        await update.message.reply_text("Не розпізнав час. Формат: <code>8:00</code> або <code>20:30</code>.")
        return LAST
    if to_min(t) <= to_min(d["first"]):
        await say(update, f"Останній прийом має бути пізніше за перший ({d['first']}). Спробуйте ще раз:")
        return LAST
    d["times"] = distribute(d["first"], t, d["count"])
    if len(d["times"]) < d["count"]:
        await say(update, "Проміжок замалий для такої кількості прийомів. Введіть пізніший час:")
        return LAST
    await say(update, f"Розклад прийомів: <b>{', '.join(d['times'])}</b>")
    return await after_times(update, context)


async def after_times(update, context) -> int:
    if editing(context):
        return await finish(update, context)
    return await ask_comment_q(update, context)


async def comment_q_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query.data == "cmt:yes":
        await say(update, "Введіть коментар (напр. «запивати великою кількістю води»):", kb([]))
        return COMMENT
    draft(context)["comment"] = None
    return await finish(update, context)


async def comment_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft(context)["comment"] = update.message.text.strip()[:500]
    return await finish(update, context)


async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = draft(context)
    uid = update.effective_user.id
    if editing(context):
        med_id = context.user_data["edit_id"]
        db.update_med(med_id, d["name"], d["food"], d["times"], d.get("comment"))
        med = db.get_med(med_id)
        schedule(context.application, med)
        await say(update, "✅ Нагадування оновлено:\n\n" + med_summary(med, uid), MAIN_MENU)
        if med["target_id"] != uid:
            await notify(context, med["target_id"],
                         f"✏️ {user_label(uid)} змінив(ла) ваше призначення:\n\n" + med_summary(med))
    else:
        med_id = db.add_med(uid, d["target_id"], d["name"], d["food"], d["times"], d.get("comment"))
        med = db.get_med(med_id)
        schedule(context.application, med)
        await say(update, "✅ Нагадування створено:\n\n" + med_summary(med, uid), MAIN_MENU)
        if med["target_id"] != uid:
            ok = await notify(context, med["target_id"],
                              f"👨‍⚕️ {user_label(uid)} призначив(ла) вам ліки:\n\n" + med_summary(med)
                              + "\n\nЯ нагадуватиму вчасно ⏰")
            if not ok:
                await update.effective_chat.send_message(
                    "⚠️ Не вдалося написати пацієнту — можливо, він(вона) заблокував(ла) бота.")
    context.user_data.clear()
    return ConversationHandler.END


async def notify(context, chat_id: int, text: str) -> bool:
    try:
        await context.bot.send_message(chat_id, text)
        return True
    except (Forbidden, BadRequest):
        return False


# --- редагування ---

def meds_keyboard(meds: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for m in meds:
        mark = "⏸ " if not m["active"] else ""
        rows.append([(f"{mark}{m['name']} — {', '.join(m['times'])}"[:60], f"{prefix}:{m['id']}")])
    return kb(rows)


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    register(update)
    context.user_data.clear()
    meds = db.meds_for_user(update.effective_user.id)
    if not meds:
        await update.message.reply_text("У вас поки немає нагадувань.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    await update.message.reply_text("Оберіть нагадування для редагування:",
                                    reply_markup=meds_keyboard(meds, "edit"))
    return EDIT_PICK


def can_access(med: dict | None, uid: int) -> bool:
    return bool(med) and uid in (med["owner_id"], med["target_id"])


async def edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    med = db.get_med(int(update.callback_query.data.split(":")[1]))
    uid = update.effective_user.id
    if not can_access(med, uid):
        await say(update, "Нагадування не знайдено.", MAIN_MENU)
        return ConversationHandler.END
    context.user_data.update(
        mode="edit",
        edit_id=med["id"],
        draft={"name": med["name"], "food": med["food"], "times": med["times"],
               "count": len(med["times"]), "comment": med["comment"]},
    )
    await say(update, med_summary(med, uid) + "\n\nЩо змінити?",
              kb([[("Назву", "fld:name"), ("До/після їжі", "fld:food")],
                  [("Час прийому", "fld:times"), ("Коментар", "fld:comment")]]))
    return EDIT_FIELD


async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = update.callback_query.data.split(":")[1]
    return await {"name": ask_name, "food": ask_food,
                  "times": ask_count, "comment": ask_comment_q}[field](update, context)


# ---------- видалення ----------

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    meds = db.meds_for_user(update.effective_user.id)
    if not meds:
        await update.message.reply_text("У вас поки немає нагадувань.", reply_markup=MAIN_MENU)
        return
    await update.message.reply_text("Оберіть нагадування для видалення:",
                                    reply_markup=meds_keyboard(meds, "del"))


async def delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    med = db.get_med(int(update.callback_query.data.split(":")[1]))
    if not can_access(med, update.effective_user.id):
        await say(update, "Нагадування не знайдено.")
        return
    await say(update, med_summary(med, update.effective_user.id) + "\n\nВидалити це нагадування?",
              kb([[("🗑 Так, видалити", f"delyes:{med['id']}")]]))


async def delete_yes_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    med = db.get_med(int(update.callback_query.data.split(":")[1]))
    if not can_access(med, uid):
        await say(update, "Нагадування не знайдено.")
        return
    unschedule(context.application, med["id"])
    db.delete_med(med["id"])
    await say(update, f"🗑 Нагадування «{html.escape(med['name'])}» видалено.", MAIN_MENU)
    other = med["target_id"] if med["owner_id"] == uid else med["owner_id"]
    if other != uid:
        await notify(context, other,
                     f"🗑 {user_label(uid)} видалив(ла) нагадування «{html.escape(med['name'])}».")


async def plain_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await say(update, "Скасовано.")


# ---------- зупинка / відновлення ----------

async def stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    meds = db.set_active_for_user(update.effective_user.id, False)
    for m in meds:
        unschedule(context.application, m["id"])
    if not meds:
        await update.message.reply_text("У вас немає нагадувань.", reply_markup=MAIN_MENU)
        return
    await update.message.reply_text(
        f"⏸ Усі нагадування зупинено ({len(meds)}). Їх можна відновити будь-коли.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Відновити всі", callback_data="resume")]]),
    )


async def resume_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    meds = db.set_active_for_user(update.effective_user.id, True)
    for m in meds:
        schedule(context.application, m)
    await say(update, f"▶️ Нагадування відновлено ({len(meds)}).", MAIN_MENU)


# ---------- маршрутизація меню ----------

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    register(update)
    context.user_data.clear()
    text = update.message.text
    if text == BTN_DEL:
        await delete_start(update, context)
    elif text == BTN_STOP:
        await stop_all(update, context)
    elif text == BTN_SOCIAL:
        await social(update, context)
    return ConversationHandler.END


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задано BOT_TOKEN у файлі .env")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML, tzinfo=TZ))
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{re.escape(BTN_ADD)}$"), add_start),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_EDIT)}$"), edit_start),
        ],
        states={
            TARGET: [CallbackQueryHandler(target_cb, pattern=r"^tgt:")],
            TARGET_ID: [MessageHandler(TEXT_INPUT, target_id_text)],
            NAME: [MessageHandler(TEXT_INPUT, name_text)],
            FOOD_ST: [CallbackQueryHandler(food_cb, pattern=r"^food:")],
            COUNT: [CallbackQueryHandler(count_input, pattern=r"^cnt:"),
                    MessageHandler(TEXT_INPUT, count_input)],
            FIRST: [CallbackQueryHandler(first_input, pattern=r"^tm:"),
                    MessageHandler(TEXT_INPUT, first_input)],
            LAST: [CallbackQueryHandler(last_input, pattern=r"^tm:"),
                   MessageHandler(TEXT_INPUT, last_input)],
            COMMENT_Q: [CallbackQueryHandler(comment_q_cb, pattern=r"^cmt:")],
            COMMENT: [MessageHandler(TEXT_INPUT, comment_text)],
            EDIT_PICK: [CallbackQueryHandler(edit_pick, pattern=r"^edit:\d+$")],
            EDIT_FIELD: [CallbackQueryHandler(edit_field, pattern=r"^fld:")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(cancel, pattern=r"^cancel$"),
            MessageHandler(MENU_FILTER, menu_router),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(MENU_FILTER, menu_router))
    app.add_handler(CallbackQueryHandler(taken_cb, pattern=r"^taken:\d+$"))
    app.add_handler(CallbackQueryHandler(delete_cb, pattern=r"^del:\d+$"))
    app.add_handler(CallbackQueryHandler(delete_yes_cb, pattern=r"^delyes:\d+$"))
    app.add_handler(CallbackQueryHandler(resume_cb, pattern=r"^resume$"))
    app.add_handler(CallbackQueryHandler(plain_cancel_cb, pattern=r"^cancel$"))

    log.info("Бот запущено")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
