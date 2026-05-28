"""
Telegram Task Bot — Native Checklist Edition
=============================================
Отправляет задачи как нативные чеклисты Telegram (не кнопки).
Пользователи отмечают пункты прямо в Telegram, бот отслеживает изменения.
"""

import logging
import os
import re
import sqlite3
from datetime import datetime

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
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
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id      INTEGER NOT NULL REFERENCES batches(id),
                tg_task_id    INTEGER NOT NULL,  -- id внутри чеклиста Telegram
                person        TEXT NOT NULL,
                text          TEXT NOT NULL,
                date          TEXT,
                done          INTEGER NOT NULL DEFAULT 0,
                msg_id        INTEGER,
                msg_chat_id   INTEGER
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


# ─── Telegram Checklist API ────────────────────────────────────────────────────

async def send_checklist(chat_id: int, title: str, tasks: list[dict]) -> dict:
    """
    Отправляет нативный чеклист Telegram через Bot API 9.0+.
    others_can_mark_tasks_as_done=True — коллеги могут отмечать задачи.
    """
    tg_tasks = [
        {
            "id": i + 1,
            "text": (t["text"] + (f" ({t['date']})" if t.get("date") else ""))[:100],
        }
        for i, t in enumerate(tasks[:30])
    ]
    payload = {
        "chat_id": chat_id,
        "checklist": {
            "title": title[:255],
            "tasks": tg_tasks,
            "others_can_mark_tasks_as_done": True,
        },
    }
    logger.info(f"sendChecklist → chat_id={chat_id}, title={title!r}, {len(tg_tasks)} tasks")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendChecklist",
                json=payload,
            )
            data = r.json()
            if not data.get("ok"):
                logger.error(f"sendChecklist error: {data}")
            return data
    except Exception as e:
        logger.error(f"sendChecklist exception: {e}")
        return {"ok": False, "description": str(e)}


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


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Отправляет тестовый чеклист — проверяет, работает ли API."""
    if update.effective_chat.id != ADMIN_ID:
        return
    result = await send_checklist(
        chat_id=ADMIN_ID,
        title="Тест чеклиста",
        tasks=[
            {"text": "Задача 1 — отметь меня", "date": None},
            {"text": "Задача 2 — и меня тоже", "date": None},
        ],
    )
    if result.get("ok"):
        await update.message.reply_text("✅ Чеклист отправлен! Проверь сообщение выше.")
    else:
        desc = result.get("description", "неизвестная ошибка")
        await update.message.reply_text(
            f"❌ Ошибка API: `{desc}`\n\n"
            "Возможные причины:\n"
            "• Telegram не обновлён до версии с поддержкой чеклистов\n"
            "• Bot API сервер не поддерживает sendChecklist",
            parse_mode="Markdown",
        )


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
        cur = conn.execute("INSERT INTO batches (created) VALUES (?)", (datetime.now().isoformat(),))
        batch_id = cur.lastrowid

    with db() as conn:
        users = conn.execute("SELECT chat_id, name FROM users").fetchall()
    name_to_chat = {u["name"].lower(): u["chat_id"] for u in users}

    sent, missing = [], []

    for person, tasks in sections.items():
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

        # Сохраняем задачи в БД
        with db() as conn:
            for i, t in enumerate(tasks):
                conn.execute(
                    "INSERT INTO tasks (batch_id, tg_task_id, person, text, date, msg_chat_id)"
                    " VALUES (?,?,?,?,?,?)",
                    (batch_id, i + 1, person, t["text"], t.get("date"), target_chat),
                )

        # Отправляем нативный чеклист
        result = await send_checklist(target_chat, person, tasks)

        if result.get("ok"):
            msg_id = result["result"]["message_id"]
            with db() as conn:
                conn.execute(
                    "UPDATE tasks SET msg_id=? WHERE batch_id=? AND person=?",
                    (msg_id, batch_id, person),
                )
            sent.append(person)
            logger.info(f"Checklist sent to {person} (chat {target_chat}), msg_id={msg_id}")
        else:
            desc = result.get("description", "неизвестная ошибка")
            logger.error(f"sendChecklist failed for {person}: {desc}")
            # Fallback: обычное сообщение если чеклисты не поддерживаются
            lines = [f"📋 *{person}*\n"]
            for t in tasks:
                line = f"☐ {t['text']}"
                if t.get("date"):
                    line += f" ({t['date']})"
                lines.append(line)
            try:
                msg = await ctx.bot.send_message(
                    chat_id=target_chat,
                    text="\n".join(lines),
                    parse_mode="Markdown",
                )
                with db() as conn:
                    conn.execute(
                        "UPDATE tasks SET msg_id=? WHERE batch_id=? AND person=?",
                        (msg.message_id, batch_id, person),
                    )
                sent.append(f"{person} (текст)")
            except Exception as e:
                missing.append(f"{person} (ошибка: {e})")

    reply = f"✅ Батч #{batch_id} отправлен!\n"
    if sent:
        reply += f"\n📨 Получили: {', '.join(sent)}"
    if missing:
        reply += (
            f"\n\n⚠️ Не найдены: {', '.join(missing)}\n"
            "Пусть напишут /start, потом /link со своим именем."
        )
    await update.message.reply_text(reply)


# ─── Отслеживание отметок в чеклисте ─────────────────────────────────────────

async def handle_any_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Ловит все апдейты — в том числе edited_message с обновлённым чеклистом.
    Когда пользователь отмечает пункт, Telegram присылает edited_message
    с полем checklist содержащим актуальное состояние.
    """
    raw = update.to_dict()

    for key in ("edited_message", "message"):
        msg = raw.get(key, {})
        checklist = msg.get("checklist")
        if not checklist:
            continue

        msg_id = msg.get("message_id")
        chat_id = msg.get("chat", {}).get("id")
        if not msg_id or not chat_id:
            continue

        tg_tasks = checklist.get("tasks", [])
        with db() as conn:
            for t in tg_tasks:
                tg_id = t.get("id")
                is_done = 1 if t.get("is_checked") else 0
                conn.execute(
                    "UPDATE tasks SET done=? WHERE msg_id=? AND msg_chat_id=? AND tg_task_id=?",
                    (is_done, msg_id, chat_id, tg_id),
                )
        logger.info(f"Checklist update: msg_id={msg_id}, {len(tg_tasks)} tasks synced")
        break


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Ловим все апдейты для синхронизации состояния чеклиста
    app.add_handler(TypeHandler(Update, handle_any_update))

    logger.info("Bot started (checklist mode)")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
