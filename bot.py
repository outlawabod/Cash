import os
import sqlite3
import logging

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# ABOD COMMUNITY POINTS BOT
# Clean free-to-play community points tracker.
#
# Removed:
# - tables / stakes
# - winner / loser buttons
# - pot / tax / owner payout
#
# Kept:
# - Telegram user_id based player tracking
# - /mypoints / /mystats / /leaderboard / /id
# - admin + / - / set points
# - welcome messages
# - group name in bot messages
# - @outlawabod developer credit on /start
# - webhook support for Render / other webhook hosts
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("abod-community-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "points.db")
JOIN_BONUS = int(os.environ.get("JOIN_BONUS", "0"))


# ---------------- Database ----------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            username TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            matches INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id)
        )
    """)

    conn.commit()
    return conn


def ensure_user(chat_id, user, starting_points=0):
    if not user or user.is_bot:
        return

    conn = db()
    conn.execute("""
        INSERT INTO users(
            chat_id, user_id, display_name, username, points
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            display_name=excluded.display_name,
            username=excluded.username,
            updated_at=CURRENT_TIMESTAMP
    """, (
        chat_id,
        user.id,
        user.full_name or user.first_name or str(user.id),
        user.username,
        starting_points,
    ))
    conn.commit()
    conn.close()


def get_user(chat_id, user_id):
    conn = db()
    row = conn.execute("""
        SELECT user_id, display_name, username,
               points, matches, wins, losses
        FROM users
        WHERE chat_id=? AND user_id=?
    """, (chat_id, user_id)).fetchone()
    conn.close()
    return row


def change_points(chat_id, user_id, amount):
    conn = db()
    conn.execute("""
        UPDATE users
        SET points=MAX(0, points+?),
            updated_at=CURRENT_TIMESTAMP
        WHERE chat_id=? AND user_id=?
    """, (int(amount), chat_id, user_id))
    conn.commit()

    row = conn.execute("""
        SELECT points FROM users
        WHERE chat_id=? AND user_id=?
    """, (chat_id, user_id)).fetchone()
    conn.close()
    return int(row[0]) if row else 0


def set_points(chat_id, user_id, amount):
    conn = db()
    conn.execute("""
        UPDATE users
        SET points=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE chat_id=? AND user_id=?
    """, (max(0, int(amount)), chat_id, user_id))
    conn.commit()
    conn.close()
    return max(0, int(amount))


# ---------------- Helpers ----------------

def group_name(update):
    chat = update.effective_chat
    return chat.title if chat and chat.title else "Community"


def mention(user_id, name):
    safe = (name or "Player").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


async def is_group_admin(update, context, user_id):
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return False

    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except Exception:
        log.exception("Admin check failed")
        return False


async def require_admin(update, context):
    if not await is_group_admin(
        update,
        context,
        update.effective_user.id,
    ):
        await update.message.reply_text("⛔ Admin only.")
        return False
    return True


def reply_target(update):
    message = update.message
    if not message or not message.reply_to_message:
        return None

    user = message.reply_to_message.from_user
    if not user or user.is_bot:
        return None

    return user


# ---------------- Player commands ----------------

async def start(update, context):
    user = update.effective_user
    chat_id = update.effective_chat.id
    ensure_user(chat_id, user)

    await update.message.reply_text(
        f"🎲 <b>{group_name(update)}</b>\n\n"
        "👤 <b>Player Commands</b>\n"
        "• /mypoints — check points\n"
        "• /mystats — matches & stats\n"
        "• /leaderboard — top players\n"
        "• /id — your Telegram ID\n"
        "• /help — commands\n\n"
        "👑 <b>Admin</b>\n"
        "• Reply to a player + /add 20\n"
        "• Reply to a player + /minus 20\n"
        "• Reply to a player + /set 100\n\n"
        "🛠️ Developed by <b>@outlawabod</b>",
        parse_mode="HTML",
    )


async def help_cmd(update, context):
    await start(update, context)


async def id_cmd(update, context):
    user = update.effective_user
    ensure_user(update.effective_chat.id, user)

    await update.message.reply_text(
        f"🪪 {mention(user.id, user.full_name)}\n"
        f"Telegram ID: <code>{user.id}</code>",
        parse_mode="HTML",
    )


async def mypoints(update, context):
    user = update.effective_user
    chat_id = update.effective_chat.id
    ensure_user(chat_id, user)

    row = get_user(chat_id, user.id)
    points = row[3] if row else 0

    await update.message.reply_text(
        f"💰 {mention(user.id, user.full_name)}\n"
        f"Points: <b>{points}</b>",
        parse_mode="HTML",
    )


async def mystats(update, context):
    user = update.effective_user
    chat_id = update.effective_chat.id
    ensure_user(chat_id, user)

    row = get_user(chat_id, user.id)
    if not row:
        await update.message.reply_text("📊 No stats available yet.")
        return

    _, name, _, points, matches, wins, losses = row

    await update.message.reply_text(
        f"📊 <b>{name}</b>\n\n"
        f"💰 Points: <b>{points}</b>\n"
        f"🎮 Matches: <b>{matches}</b>\n"
        f"🏆 Wins: <b>{wins}</b>\n"
        f"❌ Losses: <b>{losses}</b>",
        parse_mode="HTML",
    )


async def leaderboard(update, context):
    chat_id = update.effective_chat.id

    conn = db()
    rows = conn.execute("""
        SELECT user_id, display_name, points, matches, wins, losses
        FROM users
        WHERE chat_id=?
        ORDER BY points DESC, wins DESC, display_name COLLATE NOCASE
        LIMIT 10
    """, (chat_id,)).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("🏆 Leaderboard is empty.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 <b>{group_name(update)} — Leaderboard</b>", ""]

    for index, (uid, name, points, matches, wins, losses) in enumerate(rows):
        prefix = medals[index] if index < 3 else f"{index + 1}."
        lines.append(
            f"{prefix} {mention(uid, name)} — "
            f"<b>{points}</b> pts"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


# ---------------- Admin point commands ----------------

async def adjust_points(update, context, mode):
    if not await require_admin(update, context):
        return

    target = reply_target(update)
    if not target:
        await update.message.reply_text(
            "👤 Reply to the player's message first.\n\n"
            f"Example: reply → /{mode} 20"
        )
        return

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            f"Usage: /{mode} 20"
        )
        return

    amount = abs(int(context.args[0]))
    chat_id = update.effective_chat.id
    ensure_user(chat_id, target)

    if mode == "add":
        balance = change_points(chat_id, target.id, amount)
        result = f"➕ +{amount}"
    elif mode == "minus":
        balance = change_points(chat_id, target.id, -amount)
        result = f"➖ -{amount}"
    else:
        balance = set_points(chat_id, target.id, amount)
        result = f"🎯 {amount}"

    await update.message.reply_text(
        f"{result} pts\n"
        f"👤 {mention(target.id, target.full_name)}\n"
        f"💰 Balance: <b>{balance} pts</b>",
        parse_mode="HTML",
    )


async def add_cmd(update, context):
    await adjust_points(update, context, "add")


async def minus_cmd(update, context):
    await adjust_points(update, context, "minus")


async def set_cmd(update, context):
    await adjust_points(update, context, "set")


# ---------------- Welcome ----------------

async def new_members(update, context):
    chat_id = update.effective_chat.id

    for user in update.message.new_chat_members:
        if user.is_bot:
            continue

        ensure_user(chat_id, user, JOIN_BONUS)

        if JOIN_BONUS:
            await update.message.reply_text(
                f"👋 Welcome {mention(user.id, user.full_name)}! 🎲\n"
                f"🎁 +{JOIN_BONUS} points.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"👋 Welcome {mention(user.id, user.full_name)}! 🎲\n"
                "Use /mypoints to check your balance.",
                parse_mode="HTML",
            )


# ---------------- Error handling ----------------

async def error_handler(update, context):
    log.exception("Unhandled Telegram bot error", exc_info=context.error)


# ---------------- Main ----------------

def main():
    db().close()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mypoints", mypoints))
    app.add_handler(CommandHandler("mystats", mystats))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("id", id_cmd))

    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("minus", minus_cmd))
    app.add_handler(CommandHandler("set", set_cmd))

    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            new_members,
        )
    )

    app.add_error_handler(error_handler)

    if WEBHOOK_URL:
        log.info("Starting webhook mode on port %s", PORT)
        log.info("Webhook endpoint: %s/webhook", WEBHOOK_URL)

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=f"{WEBHOOK_URL}/webhook",
            drop_pending_updates=True,
        )
    else:
        log.info("WEBHOOK_URL not set; starting polling mode.")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
