import os
import sqlite3
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ChatMemberStatus

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
POINTS_PER_WIN = int(os.environ.get("POINTS_PER_WIN", "10"))
JOIN_BONUS = int(os.environ.get("JOIN_BONUS", "10"))
DB_PATH = os.environ.get("DB_PATH", "points.db")

# Points are tracked by (chat_id, username) rather than numeric user_id,
# because admins type opponents as plain @username text (e.g. in /newtable),
# and Telegram doesn't expose a numeric ID from typed text alone. This means
# every player needs a public Telegram @username for their points to be
# tracked correctly and consistently everywhere in this bot.


# ---------------- Database ----------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS points (
            chat_id INTEGER,
            username TEXT,
            display_name TEXT,
            points INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, username)
        )"""
    )
    return conn


def norm(username: str) -> str:
    return username.strip().lstrip("@").lower()


def ensure_user(chat_id: int, username: str, display_name: str, starting_points: int = 0):
    conn = db()
    conn.execute(
        """INSERT INTO points (chat_id, username, display_name, points)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, username) DO NOTHING""",
        (chat_id, norm(username), display_name, starting_points),
    )
    conn.commit()
    conn.close()


def add_points(chat_id: int, username: str, display_name: str, amount: int):
    conn = db()
    conn.execute(
        """INSERT INTO points (chat_id, username, display_name, points)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, username)
           DO UPDATE SET points = points + excluded.points, display_name = excluded.display_name""",
        (chat_id, norm(username), display_name, amount),
    )
    conn.commit()
    conn.close()


def get_points(chat_id: int, username: str) -> int:
    conn = db()
    row = conn.execute(
        "SELECT points FROM points WHERE chat_id=? AND username=?",
        (chat_id, norm(username)),
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def get_leaderboard(chat_id: int, limit: int = 10):
    conn = db()
    rows = conn.execute(
        "SELECT display_name, points FROM points WHERE chat_id=? ORDER BY points DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    conn.close()
    return rows


# ---------------- Helpers ----------------

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def parse_mention(arg: str):
    """Returns (display_name, normalized_username) from a raw @username arg."""
    arg = arg.strip()
    return arg, arg.lstrip("@")


# ---------------- Handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎲 Ludo Points Bot (free-to-play, no real money)\n\n"
        "Everyone:\n"
        "/mypoints — check your points (private popup if tapped via button)\n"
        "/leaderboard — top players\n\n"
        "Admins only:\n"
        "/newtable @player1 @player2 — start a table, tap a button to declare the winner\n"
        "/addpoints @user 20 — manually add points (e.g. after checking a screenshot)\n"
        "/removepoints @user 15 — manually deduct points\n"
        "/postpointsbutton — post a pinnable 'Check My Points' button\n\n"
        f"New members get a {JOIN_BONUS}-point joining bonus automatically.\n"
        "Note: your Telegram @username must be public for points to track correctly."
    )


async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        if not member.username:
            await update.message.reply_text(
                f"👋 Welcome {member.first_name}! Set a public @username in Telegram "
                f"settings so I can track your points (joining bonus is on hold until then)."
            )
            continue
        display = f"@{member.username}"
        ensure_user(chat_id, member.username, display, starting_points=JOIN_BONUS)
        await update.message.reply_text(
            f"🎉 Welcome {display}! You've received a {JOIN_BONUS}-point joining bonus. "
            f"Use /mypoints anytime to check your score."
        )


async def mypoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user.username:
        await update.message.reply_text(
            "You need a public Telegram @username set for me to track your points."
        )
        return
    pts = get_points(update.effective_chat.id, user.username)
    await update.message.reply_text(f"🏅 @{user.username}, you have {pts} points.")


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_leaderboard(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No points recorded yet. Play a table to get started!")
        return
    lines = ["🏆 Leaderboard"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (display_name, pts) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{prefix} {display_name} — {pts} pts")
    await update.message.reply_text("\n".join(lines))


async def postpointsbutton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📊 Check My Points", callback_data="check_points")]]
    )
    await update.message.reply_text(
        "Tap below anytime to privately check your points 👇", reply_markup=keyboard
    )


async def newtable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await is_admin(update, context, user.id):
        await update.message.reply_text("Only group admins can start a table.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /newtable @player1 @player2")
        return

    p1_display, p1_username = parse_mention(context.args[0])
    p2_display, p2_username = parse_mention(context.args[1])

    text = f"🎲 Table: {p1_display} vs {p2_display}\n🏆 Winner: Pending"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{p1_display} Won", callback_data=f"win|{p1_username}|{p1_display}"),
                InlineKeyboardButton(f"{p2_display} Won", callback_data=f"win|{p2_username}|{p2_display}"),
            ]
        ]
    )
    await update.message.reply_text(text, reply_markup=keyboard)


async def addpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _manual_adjust(update, context, sign=1)


async def removepoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _manual_adjust(update, context, sign=-1)


async def _manual_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE, sign: int):
    user = update.effective_user
    if not await is_admin(update, context, user.id):
        await update.message.reply_text("Only group admins can adjust points.")
        return

    if len(context.args) < 2 or not context.args[1].lstrip("-").isdigit():
        cmd = "/addpoints" if sign == 1 else "/removepoints"
        await update.message.reply_text(f"Usage: {cmd} @user 20")
        return

    display, username = parse_mention(context.args[0])
    amount = int(context.args[1]) * sign
    add_points(update.effective_chat.id, username, display, amount)
    new_total = get_points(update.effective_chat.id, username)
    verb = "added to" if sign == 1 else "removed from"
    await update.message.reply_text(
        f"✅ {abs(amount)} points {verb} {display}. New total: {new_total} pts."
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "check_points":
        user = query.from_user
        if not user.username:
            await query.answer("Set a public @username to track your points.", show_alert=True)
            return
        pts = get_points(query.message.chat_id, user.username)
        await query.answer(text=f"You have {pts} points 🏅", show_alert=True)
        return

    if data.startswith("win|"):
        _, winner_username, winner_display = data.split("|", 2)

        if not await is_admin(update, context, query.from_user.id):
            await query.answer("Only admins can declare a winner.", show_alert=True)
            return

        if "Pending" not in (query.message.text or ""):
            await query.answer("This table's winner is already decided.", show_alert=True)
            return

        add_points(query.message.chat_id, winner_username, winner_display, POINTS_PER_WIN)

        new_text = query.message.text.replace(
            "🏆 Winner: Pending", f"🏆 Winner: {winner_display} 🎉 (+{POINTS_PER_WIN} pts)"
        )
        await query.edit_message_text(new_text)
        await query.answer("Winner recorded!")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newtable", newtable))
    app.add_handler(CommandHandler("mypoints", mypoints))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("addpoints", addpoints))
    app.add_handler(CommandHandler("removepoints", removepoints))
    app.add_handler(CommandHandler("postpointsbutton", postpointsbutton))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    app.add_handler(CallbackQueryHandler(on_callback))

    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="",
            webhook_url=WEBHOOK_URL.rstrip("/"),
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
