"""
Telegram Task Bot
=================
Принимает задачи в markdown-формате, рассылает персональные
чеклисты с кнопками ☐/✅ каждому участнику.
"""

import logging
import os
import re
import sqlite3
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DB_PATH = os.environ.get("DB_PATH", "tasks.db")

ME_ALIASES = {"я (мои задачи)", "я", "мои задачи"}


# ─── База данных ───────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id   INTEGER PRIMARY KEY,
                username  TEXT,
                name      TEXT NOT NULL,
                created   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                created   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id    INTEGER NOT NULL REFERENCES batches(id),
                person      TEXT NOT NULL,
                text        TEXT NOT NULL,
                date        TEXT,
                done        INTEGER NOT NULL DEFAULT 0,
                msg_id      INTEGER,
                msg_chat_id INTEGER
            );
        """)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ─── Парсинг markdown ──────────────────────────────────────────────────────────

def parse_tasks(text: str) -> dict[str, list[dict]]:
    sections: dict[str, list[dict]] = {}
    current = None
    # Normalize line endings and strip invisible chars
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in text.split("\n"):
        line = line.strip().strip("​\xa0")  # strip zero-width space, nbsp
        if not line:
            continue
        # Match **Person** or **Person**: — also allow smart quotes/asterisks
        bold = re.match(r"^[\*\*]{2}(.+?)[\*\*]{2}:?$", line) or \
               re.match(r"^\*\*(.+?)\*\*:?\s*$", line)
        if bold:
            current = bold.group(1).strip()
            sections.setdefault(current, [])
            continue
        # Match - [ ] task or • task or * task as fallback
        task = re.match(r"^[-*•]\s*\[[ xXvV✓]\]?\s+(.+)$", line) or \
               re.match(r"^[-*•]\s+(?!\[)(.+)$", line) if current else None
        if task and current is not None:
            raw = task.group(1).strip()
            date_m = re.search(r"\(([^)]+)\)\s*$", raw)
            date = date_m.group(1) if date_m else None
            task_text = raw[: date_m.start()].strip() if date_m else raw
            if task_text:
                sections[current].append({"text": task_text, "date": date})
    return sections


# ─── Сборка сообщения с кнопками ──────────────────────────────────────────────

def build_message(person: str, batch_id: int) -> tuple[str, InlineKeyboardMarkup]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, text, date, done FROM tasks WHERE batch_id=? AND person=?",
            (batch_id, person),
        ).fetchall()

    done_n = sum(1 for r in rows if r["done"])
    total = len(rows)
    header = f"📋 *{person}*\n_{done_n} из {total} выполнено_\n"

    keyboard = []
    for r in rows:
        icon = "✅" if r["done"] else "☐"
        label = f"{icon} {r['text']}"
        if r["date"]:
            label += f" ({r['date']})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"t:{r['id']}")])

    return header, InlineKeyboardMarkup(keyboard)


# ─── Команды ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    with db() as conn:
        existing = conn.execute("SELECT name FROM users WHERE chat_id=?", (chat_id,)).fetchone()

    if existing:
        await update.message.reply_text(
            f"👋 Ты уже зарегистрирован как *{existing['name']}*.\n"
            "Изменить имя — /link НовоеИмя\n"
            "Твои задачи — /status",
            parse_mode="Markdown",
        )
        return

    name = user.first_name
    if user.last_name:
        name += f" {user.last_name}"

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (chat_id, username, name, created) VALUES (?,?,?,?)",
            (chat_id, user.username or "", name, datetime.now().isoformat()),
        )

    await update.message.reply_text(
        f"👋 Привет, *{name}*! Зарегистрирован.\n\n"
        "Если в задачах тебя зовут иначе, напиши:\n`/link Антон`\n\n"
        "Задачи будут приходить автоматически ✅",
        parse_mode="Markdown",
    )


async def cmd_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Используй: /link Антон")
        return
    name = " ".join(ctx.args)
    user = update.effective_user
    chat_id = update.effective_chat.id
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (chat_id, username, name, created) VALUES (?,?,?,?)",
            (chat_id, user.username or "", name, datetime.now().isoformat()),
        )
    await update.message.reply_text(f"✅ Теперь ты — *{name}*.", parse_mode="Markdown")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    with db() as conn:
        if chat_id == ADMIN_ID:
            rows = conn.execute(
                "SELECT person, COUNT(*) total, SUM(done) done_n FROM tasks GROUP BY person"
            ).fetchall()
            if not rows:
                await update.message.reply_text("Задач пока нет.")
                return
            lines = ["📊 *Общий статус:*\n"]
            for r in rows:
                done_n = r["done_n"] or 0
                total = r["total"]
                bar = "▓" * done_n + "░" * (total - done_n)
                icon = "✅" if done_n == total else "🔄"
                lines.append(f"{icon} *{r['person']}*: {done_n}/{total}  `{bar}`")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        else:
            user_row = conn.execute("SELECT name FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            if not user_row:
                await update.message.reply_text("Сначала напиши /start")
                return
            name = user_row["name"]
            tasks = conn.execute(
                "SELECT text, date, done FROM tasks WHERE person=?", (name,)
            ).fetchall()
            if not tasks:
                await update.message.reply_text(
                    f"Задач для *{name}* пока нет.", parse_mode="Markdown"
                )
                return
            done_n = sum(1 for t in tasks if t["done"])
            lines = [f"📋 *Твои задачи ({done_n}/{len(tasks)} выполнено):*\n"]
            for t in tasks:
                icon = "✅" if t["done"] else "☐"
                line = f"{icon} {t['text']}"
                if t["date"]:
                    line += f" _({t['date']})_"
                lines.append(line)
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает как бот видит отправленный текст — для диагностики."""
    if update.effective_chat.id != ADMIN_ID:
        return
    if not ctx.args and not update.message.reply_to_message:
        await update.message.reply_text(
            "Отправь текст задач ответом на это сообщение, или напиши:\n`/debug текст задач`",
            parse_mode="Markdown",
        )
        return

    if update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    else:
        text = " ".join(ctx.args)

    sections = parse_tasks(text)

    # Show raw repr of first 300 chars
    raw_repr = repr(text[:300])
    lines_info = "\n".join(
        f"{i+1}: {repr(line)}" for i, line in enumerate(text.split("\n")[:10])
    )

    if sections:
        result = "✅ Успешно распознал:\n"
        for person, tasks in sections.items():
            result += f"\n👤 {person}:\n"
            for t in tasks:
                result += f"  — {t['text']}"
                if t.get("date"):
                    result += f" ({t['date']})"
                result += "\n"
    else:
        result = "❌ Задачи не найдены.\n\nПервые строки (repr):\n" + lines_info

    await update.message.reply_text(result[:3000])


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Таск-бот*\n\n"
        "*Коллегам:*\n"
        "/start — зарегистрироваться\n"
        "/link Имя — указать имя как в задачах\n"
        "/status — мои задачи\n\n"
        "*Администратору:*\n"
        "Скопируй чеклист из артефакта и отправь боту.\n"
        "/status — статус всей команды",
        parse_mode="Markdown",
    )


# ─── Получение задач от админа ────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_ID:
        await update.message.reply_text("Используй /start для регистрации.")
        return

    text = update.message.text
    sections = parse_tasks(text)
    if not sections:
        await update.message.reply_text(
            "Не нашёл задач. Скопируй текст из кнопки «Скопировать всё» в артефакте."
        )
        return

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO batches (created) VALUES (?)", (datetime.now().isoformat(),)
        )
        batch_id = cur.lastrowid
        for person, tasks in sections.items():
            for t in tasks:
                conn.execute(
                    "INSERT INTO tasks (batch_id, person, text, date) VALUES (?,?,?,?)",
                    (batch_id, person, t["text"], t.get("date")),
                )

    with db() as conn:
        users = conn.execute("SELECT chat_id, name FROM users").fetchall()
    name_to_chat = {u["name"].lower(): u["chat_id"] for u in users}

    sent, missing = [], []

    for person in sections:
        is_me = person.lower() in ME_ALIASES
        target_chat = ADMIN_ID if is_me else None

        if not target_chat:
            p_lower = person.lower()
            for reg_name, reg_chat in name_to_chat.items():
                if p_lower in reg_name or reg_name in p_lower:
                    target_chat = reg_chat
                    break

        if not target_chat:
            missing.append(person)
            continue

        msg_text, keyboard = build_message(person, batch_id)
        try:
            msg = await ctx.bot.send_message(
                chat_id=target_chat,
                text=msg_text,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            with db() as conn:
                conn.execute(
                    "UPDATE tasks SET msg_id=?, msg_chat_id=? WHERE batch_id=? AND person=?",
                    (msg.message_id, target_chat, batch_id, person),
                )
            sent.append(person)
        except Exception as e:
            logger.error(f"Не удалось отправить {person}: {e}")
            missing.append(f"{person} (ошибка)")

    reply = f"✅ Батч #{batch_id} отправлен!\n"
    if sent:
        reply += f"\n📨 Получили: {', '.join(sent)}"
    if missing:
        reply += (
            f"\n\n⚠️ Не найдены: {', '.join(missing)}\n"
            "Пусть напишут /start, потом /link со своим именем."
        )
    await update.message.reply_text(reply)


# ─── Нажатие кнопки ───────────────────────────────────────────────────────────

async def toggle_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    task_id = int(query.data.split(":")[1])
    chat_id = update.effective_chat.id

    with db() as conn:
        row = conn.execute(
            "SELECT done, person, batch_id, msg_chat_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()

        if not row:
            await query.answer("Задача не найдена", show_alert=True)
            return

        if chat_id != ADMIN_ID and chat_id != row["msg_chat_id"]:
            await query.answer("Это не твоя задача 🙅", show_alert=True)
            return

        conn.execute(
            "UPDATE tasks SET done=? WHERE id=?",
            (0 if row["done"] else 1, task_id),
        )

    msg_text, keyboard = build_message(row["person"], row["batch_id"])
    try:
        await query.edit_message_text(
            text=msg_text, reply_markup=keyboard, parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"edit_message_text: {e}")


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CallbackQueryHandler(toggle_task, pattern=r"^t:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
