"""
Telegram Task Bot
=================
Принимает задачи в markdown-формате от администратора,
рассылает персональные чеклисты с кнопками каждому участнику.
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
ADMIN_ID = int(os.environ["ADMIN_ID"])  # твой Telegram user ID
DB_PATH = os.environ.get("DB_PATH", "tasks.db")

ME_ALIASES = {"я (мои задачи)", "я", "мои задачи"}  # секции, которые идут админу


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
    """
    Разбирает формат:
        **Имя**
        - [ ] задача (дата)
        - [ ] другая задача
    Возвращает {person: [{text, date}, ...]}
    """
    sections: dict[str, list[dict]] = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        bold = re.match(r"^\*\*(.+?)\*\*:?$", line)
        if bold:
            current = bold.group(1).strip()
            sections.setdefault(current, [])
            continue
        task = re.match(r"^-\s*\[[ x]\]\s+(.+)$", line, re.IGNORECASE)
        if task and current is not None:
            raw = task.group(1).strip()
            date_m = re.search(r"\(([^)]+)\)\s*$", raw)
            date = date_m.group(1) if date_m else None
            task_text = raw[: date_m.start()].strip() if date_m else raw
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
        existing = conn.execute(
            "SELECT name FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()

    if existing:
        await update.message.reply_text(
            f"👋 Ты уже зарегистрирован как *{existing['name']}*.\n"
            f"Если нужно сменить имя — /link НовоеИмя\n"
            f"Твои задачи — /status",
            parse_mode="Markdown",
        )
        return

    # Регистрируем с именем из Telegram
    name = user.first_name
    if user.last_name:
        name += f" {user.last_name}"

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (chat_id, username, name, created) VALUES (?,?,?,?)",
            (chat_id, user.username or "", name, datetime.now().isoformat()),
        )

    await update.message.reply_text(
        f"👋 Привет, *{name}*! Ты зарегистрирован.\n\n"
        f"Если в задачах тебя зовут иначе (например, просто «Антон»), "
        f"напиши:\n`/link Антон`\n\n"
        f"Задачи будут приходить сюда автоматически ✅",
        parse_mode="Markdown",
    )


async def cmd_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Используй: /link Антон")
        return
    name = " ".join(ctx.args)
    with db() as conn:
        user = update.effective_user
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
            user_row = conn.execute(
                "SELECT name FROM users WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if not user_row:
                await update.message.reply_text("Сначала напиши /start")
                return
            name = user_row["name"]
            tasks = conn.execute(
                "SELECT text, date, done FROM tasks WHERE person=?", (name,)
            ).fetchall()
            if not tasks:
                await update.message.reply_text(f"Задач для *{name}* пока нет.", parse_mode="Markdown")
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


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Таск-бот*\n\n"
        "*Коллегам:*\n"
        "/start — зарегистрироваться\n"
        "/link Имя — указать имя как в задачах\n"
        "/status — мои задачи\n\n"
        "*Администратору:*\n"
        "Скопируй чеклист из артефакта и отправь боту.\n"
        "/status — статус всей команды\n\n"
        "*Формат задач:*\n"
        "```\n**Антон**\n- [ ] задача 1\n- [ ] задача 2\n\n**Маша**\n- [ ] задача 3\n```",
        parse_mode="Markdown",
    )


# ─── Обработка задач от админа ────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_ID:
        await update.message.reply_text("Используй /start для регистрации.")
        return

    text = update.message.text
    sections = parse_tasks(text)
    if not sections:
        await update.message.reply_text(
            "Не нашёл задач в формате чеклиста.\n"
            "Скопируй текст из кнопки «Скопировать всё по ответственным» в артефакте.",
        )
        return

    # Создаём батч
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

    # Получаем зарегистрированных пользователей
    with db() as conn:
        users = conn.execute("SELECT chat_id, name FROM users").fetchall()
    name_to_chat = {u["name"].lower(): u["chat_id"] for u in users}

    sent, missing = [], []

    for person in sections:
        # «Я (мои задачи)» всегда идут администратору
        is_me_section = person.lower() in ME_ALIASES
        target_chat = ADMIN_ID if is_me_section else None

        if not target_chat:
            # Ищем по частичному совпадению имени
            p_lower = person.lower()
            for reg_name, reg_chat in name_to_chat.items():
                if p_lower in reg_name or reg_name in p_lower:
                    target_chat = reg_chat
                    break

        if target_chat:
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
        else:
            missing.append(person)

    reply = f"✅ Батч #{batch_id} отправлен!\n"
    if sent:
        reply += f"\n📨 Получили задачи: {', '.join(sent)}"
    if missing:
        reply += (
            f"\n\n⚠️ Не найдены в боте: {', '.join(missing)}\n"
            "Пусть напишут /start, затем /link с именем из задач."
        )
    await update.message.reply_text(reply)


# ─── Нажатие кнопки чеклиста ──────────────────────────────────────────────────

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

        # Разрешаем тогглить только своё (или админ может всё)
        if chat_id != ADMIN_ID and chat_id != row["msg_chat_id"]:
            await query.answer("Это не твоя задача 🙅", show_alert=True)
            return

        new_done = 0 if row["done"] else 1
        conn.execute("UPDATE tasks SET done=? WHERE id=?", (new_done, task_id))

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
    app.add_handler(CallbackQueryHandler(toggle_task, pattern=r"^t:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
