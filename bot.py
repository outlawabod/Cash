import os
import sqlite3
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# ABOD LUDO KING — FREE-TO-PLAY POINTS BOT
# No real money / no cash wagering.
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("abod-ludo")

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "points.db")
JOIN_BONUS = int(os.environ.get("JOIN_BONUS", "10"))
ALLOW_NEGATIVE_POINTS = os.environ.get("ALLOW_NEGATIVE_POINTS", "false").lower() == "true"

# WEBHOOK_URL is intentionally optional.
# If it is empty, Render runs Telegram polling + a health HTTP server.
# This avoids the webhook/404 problem you were hitting.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")

# ------------------------------------------------------------
# HTTP health server for Render
# ------------------------------------------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ABOD Ludo Bot is online."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Health server listening on 0.0.0.0:%s", PORT)
    return server


# ------------------------------------------------------------
# Database
# ------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            display_name TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            matches INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            points_won INTEGER NOT NULL DEFAULT 0,
            points_lost INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT,
            PRIMARY KEY(chat_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            table_no INTEGER NOT NULL,
            player1_id INTEGER NOT NULL,
            player1_username TEXT,
            player1_name TEXT NOT NULL,
            player2_id INTEGER NOT NULL,
            player2_username TEXT,
            player2_name TEXT NOT NULL,
            points INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            winner_id INTEGER,
            winner_name TEXT,
            loser_id INTEGER,
            loser_name TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            UNIQUE(chat_id, table_no)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY(chat_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            custom_name TEXT,
            automatic_table_mode INTEGER NOT NULL DEFAULT 0,
            confirm_mode INTEGER NOT NULL DEFAULT 0,
            silent_mode INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def display_name(user):
    if user.username:
        return f"@{user.username}"
    return user.full_name or f"user_{user.id}"


def username_of(user):
    return user.username.lower() if user.username else None


def ensure_user(chat_id, user, starting_points=0):
    conn = db()
    conn.execute("""
        INSERT INTO users
        (chat_id,user_id,username,display_name,points,joined_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(chat_id,user_id) DO UPDATE SET
            username=excluded.username,
            display_name=excluded.display_name
    """, (
        chat_id, user.id, username_of(user), display_name(user),
        starting_points, now()
    ))
    conn.commit()
    conn.close()


def get_user(chat_id, user_id):
    conn = db()
    row = conn.execute("""
        SELECT chat_id,user_id,username,display_name,points,matches,wins,
               losses,points_won,points_lost
        FROM users WHERE chat_id=? AND user_id=?
    """, (chat_id, user_id)).fetchone()
    conn.close()
    return row


def get_or_create_user(chat_id, user):
    row = get_user(chat_id, user.id)
    if row is None:
        ensure_user(chat_id, user, 0)
        row = get_user(chat_id, user.id)
    return row


def change_points(chat_id, user_id, amount):
    conn = db()
    row = conn.execute(
        "SELECT points FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    ).fetchone()
    if not row:
        conn.close()
        return None
    old = row[0]
    new = old + amount
    if not ALLOW_NEGATIVE_POINTS:
        new = max(0, new)
    actual = new - old
    conn.execute(
        "UPDATE users SET points=? WHERE chat_id=? AND user_id=?",
        (new, chat_id, user_id)
    )
    conn.commit()
    conn.close()
    return actual


def next_table_no(chat_id):
    conn = db()
    row = conn.execute(
        "SELECT COALESCE(MAX(table_no),0)+1 FROM tables WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    conn.close()
    return row[0]


def get_pending_table(chat_id, table_no):
    conn = db()
    row = conn.execute("""
        SELECT id,chat_id,table_no,player1_id,player1_username,player1_name,
               player2_id,player2_username,player2_name,points,status,
               winner_id,winner_name,loser_id,loser_name
        FROM tables WHERE chat_id=? AND table_no=?
    """, (chat_id, table_no)).fetchone()
    conn.close()
    return row


# ------------------------------------------------------------
# Permissions
# ------------------------------------------------------------

async def telegram_admin(update, context, user_id):
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except Exception:
        return False


async def is_admin(update, context, user_id):
    chat_id = update.effective_chat.id
    if await telegram_admin(update, context, user_id):
        return True
    conn = db()
    row = conn.execute(
        "SELECT 1 FROM admins WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    ).fetchone()
    conn.close()
    return bool(row)


async def admin_only(update, context):
    ok = await is_admin(update, context, update.effective_user.id)
    if not ok and update.effective_message:
        await update.effective_message.reply_text(
            "⛔ This command is for group admins only."
        )
    return ok


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

async def resolve_member(context, chat_id, arg):
    """Resolve @username or numeric Telegram user id inside the current group."""
    arg = arg.strip()
    if arg.startswith("@"):
        target = arg[1:]
        try:
            member = await context.bot.get_chat_member(chat_id, target)
            return member.user
        except Exception:
            return None
    if arg.isdigit():
        try:
            member = await context.bot.get_chat_member(chat_id, int(arg))
            return member.user
        except Exception:
            return None
    return None


def table_keyboard(table_id, p1_name, p2_name):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"🏆 {p1_name} Won",
                callback_data=f"winner:{table_id}:{1}"
            ),
            InlineKeyboardButton(
                f"🏆 {p2_name} Won",
                callback_data=f"winner:{table_id}:{2}"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ Cancel Table",
                callback_data=f"cancel:{table_id}"
            )
        ]
    ])


def table_text(table):
    (
        tid, chat_id, table_no, p1id, p1u, p1name,
        p2id, p2u, p2name, points, status,
        winner_id, winner_name, loser_id, loser_name
    ) = table
    if status == "pending":
        result = "🏆 Winner: Pending"
    elif status == "cancelled":
        result = "❌ Table: Cancelled"
    else:
        result = f"🏆 Winner: {winner_name} 🎉 (+{points} pts)"
        result += f"\n📉 Loser: {loser_name} (-{points} pts)"
    return (
        f"🎲 𝑻𝒂𝒃𝒍𝒆 𝑵𝒐. {table_no}\n"
        f"👤 {p1name}  vs  {p2name}\n"
        f"💰 Table Points: {points}\n"
        f"{result}"
    )


async def post_table(update, context, table):
    await update.effective_message.reply_text(
        table_text(table),
        reply_markup=table_keyboard(
            table[0], table[5], table[8]
        )
    )


# ------------------------------------------------------------
# Player commands
# ------------------------------------------------------------

async def start(update, context):
    ensure_user(update.effective_chat.id, update.effective_user, 0)
    await update.message.reply_text(
        "🎲 ABOD Ludo King — Points Bot\n"
        "Free-to-play • No real money\n\n"
        "👤 Players:\n"
        "/mypoints — current points\n"
        "/mystats — matches, wins, losses\n"
        "/leaderboard — top players\n"
        "/id — your Telegram ID\n"
        "/help — commands\n\n"
        "👑 Admins:\n"
        "/newtable @player1 @player2 500 — create table\n"
        "/cancel 12 — cancel table #12\n"
        "/win 12 1 — player 1 wins table #12\n"
        "/addpoints @user 50 — add points\n"
        "/removepoints @user 50 — remove points\n"
        "/set_balance @user 500 — set exact balance\n"
        "/list — recent tables\n"
        "/list_users — registered players\n"
        "/add_admin 123456789 — add bot-admin\n"
        "/rmv_admin 123456789 — remove bot-admin\n"
    )


async def help_cmd(update, context):
    await start(update, context)


async def mypoints(update, context):
    row = get_or_create_user(update.effective_chat.id, update.effective_user)
    await update.message.reply_text(
        f"🏅 {row[3]}\n"
        f"💰 Balance: {row[4]} pts"
    )


async def mystats(update, context):
    row = get_or_create_user(update.effective_chat.id, update.effective_user)
    total = row[5]
    wins = row[6]
    losses = row[7]
    won_pts = row[8]
    lost_pts = row[9]
    await update.message.reply_text(
        f"📊 𝑴𝒚 𝑺𝒕𝒂𝒕𝒔 — {row[3]}\n\n"
        f"💰 Points: {row[4]}\n"
        f"🎮 Matches: {total}\n"
        f"🏆 Wins: {wins}\n"
        f"📉 Losses: {losses}\n"
        f"➕ Points won: {won_pts}\n"
        f"➖ Points lost: {lost_pts}"
    )


async def leaderboard(update, context):
    conn = db()
    rows = conn.execute("""
        SELECT display_name,points,matches,wins,losses
        FROM users WHERE chat_id=?
        ORDER BY points DESC, wins DESC
        LIMIT 10
    """, (update.effective_chat.id,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No players recorded yet.")
        return
    lines = ["🏆 𝑳𝒆𝒂𝒅𝒆𝒓𝒃𝒐𝒂𝒓𝒅"]
    medals = ["🥇","🥈","🥉"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        lines.append(
            f"{prefix} {r[0]} — {r[1]} pts | W {r[3]} / L {r[4]}"
        )
    await update.message.reply_text("\n".join(lines))


async def id_cmd(update, context):
    await update.message.reply_text(
        f"🆔 Your Telegram ID: `{update.effective_user.id}`",
        parse_mode="Markdown"
    )


# ------------------------------------------------------------
# Admin: table creation / result
# ------------------------------------------------------------

async def newtable(update, context):
    if not await admin_only(update, context):
        return

    args = list(context.args)
    if args and args[-1].lower() == "points":
        args.pop()

    if len(args) != 3 or not args[2].isdigit() or int(args[2]) <= 0:
        await update.message.reply_text(
            "Usage:\n/newtable @player1 @player2 500\n\n"
            "Example:\n/newtable @Ajju @Mansuri 500"
        )
        return

    p1 = await resolve_member(context, update.effective_chat.id, args[0])
    p2 = await resolve_member(context, update.effective_chat.id, args[1])
    points = int(args[2])

    if not p1 or not p2:
        await update.message.reply_text(
            "❌ I couldn't find one of the players in this group.\n"
            "Both players should be in the group and have a public @username."
        )
        return

    if p1.id == p2.id:
        await update.message.reply_text("❌ A player cannot play against themselves.")
        return

    ensure_user(update.effective_chat.id, p1, 0)
    ensure_user(update.effective_chat.id, p2, 0)

    table_no = next_table_no(update.effective_chat.id)
    conn = db()
    cur = conn.execute("""
        INSERT INTO tables (
            chat_id,table_no,player1_id,player1_username,player1_name,
            player2_id,player2_username,player2_name,points,status,
            created_by,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)
    """, (
        update.effective_chat.id, table_no,
        p1.id, username_of(p1), display_name(p1),
        p2.id, username_of(p2), display_name(p2),
        points, update.effective_user.id, now()
    ))
    table_id = cur.lastrowid
    conn.commit()
    conn.close()

    table = get_pending_table(update.effective_chat.id, table_no)
    await post_table(update, context, table)


async def cancel_cmd(update, context):
    if not await admin_only(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /cancel TABLE_NO")
        return
    table_no = int(context.args[0])
    conn = db()
    cur = conn.execute("""
        UPDATE tables SET status='cancelled'
        WHERE chat_id=? AND table_no=? AND status='pending'
    """, (update.effective_chat.id, table_no))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        await update.message.reply_text("❌ Pending table not found.")
        return
    await update.message.reply_text(f"❌ Table No. {table_no} cancelled.")


async def win_cmd(update, context):
    if not await admin_only(update, context):
        return
    if len(context.args) != 2 or not context.args[0].isdigit() or context.args[1] not in ("1","2"):
        await update.message.reply_text("Usage: /win TABLE_NO 1\nor /win TABLE_NO 2")
        return
    await resolve_winner(
        update, context,
        table_no=int(context.args[0]),
        player_slot=int(context.args[1]),
        source_message=None,
    )


async def resolve_winner(update, context, table_no, player_slot, source_message=None):
    chat_id = update.effective_chat.id
    conn = db()
    table = conn.execute("""
        SELECT id,chat_id,table_no,player1_id,player1_username,player1_name,
               player2_id,player2_username,player2_name,points,status,
               winner_id,winner_name,loser_id,loser_name
        FROM tables WHERE chat_id=? AND table_no=?
    """, (chat_id, table_no)).fetchone()

    if not table:
        conn.close()
        if source_message:
            return False, "❌ Table not found."
        await update.message.reply_text("❌ Table not found.")
        return False, None

    (
        tid, _, _, p1id, _, p1name,
        p2id, _, p2name, points, status,
        _, _, _, _
    ) = table

    if status != "pending":
        conn.close()
        msg = "❌ This table is already closed."
        if source_message:
            return False, msg
        await update.message.reply_text(msg)
        return False, None

    winner_id, winner_name = (
        (p1id, p1name) if player_slot == 1 else (p2id, p2name)
    )
    loser_id, loser_name = (
        (p2id, p2name) if player_slot == 1 else (p1id, p1name)
    )

    # Winner receives the table value.
    # Loser loses the same value (or as much as available if negative
    # balances are disabled).
    conn.execute("""
        UPDATE users
        SET points=points+?,
            matches=matches+1,
            wins=wins+1,
            points_won=points_won+?
        WHERE chat_id=? AND user_id=?
    """, (points, points, chat_id, winner_id))

    if ALLOW_NEGATIVE_POINTS:
        conn.execute("""
            UPDATE users
            SET points=points-?,
                matches=matches+1,
                losses=losses+1,
                points_lost=points_lost+?
            WHERE chat_id=? AND user_id=?
        """, (points, points, chat_id, loser_id))
        actual_loss = points
    else:
        old_row = conn.execute(
            "SELECT points FROM users WHERE chat_id=? AND user_id=?",
            (chat_id, loser_id)
        ).fetchone()
        old_points = old_row[0] if old_row else 0
        actual_loss = min(points, max(0, old_points))
        conn.execute("""
            UPDATE users
            SET points=MAX(0,points-?),
                matches=matches+1,
                losses=losses+1,
                points_lost=points_lost+?
            WHERE chat_id=? AND user_id=?
        """, (points, actual_loss, chat_id, loser_id))

    conn.execute("""
        UPDATE tables
        SET status='completed', winner_id=?, winner_name=?,
            loser_id=?, loser_name=?, resolved_at=?
        WHERE id=?
    """, (winner_id, winner_name, loser_id, loser_name, now(), tid))
    conn.commit()
    conn.close()

    new_table = get_pending_table(chat_id, table_no)
    result_text = table_text(new_table)

    if source_message:
        try:
            await source_message.edit_text(result_text)
        except Exception as e:
            log.warning("Could not edit table message: %s", e)
        return True, f"🏆 {winner_name} wins +{points} pts."

    await update.message.reply_text(result_text)
    return True, None


# ------------------------------------------------------------
# Callback buttons
# ------------------------------------------------------------

async def callback(update, context):
    q = update.callback_query
    data = q.data or ""

    if data.startswith("winner:"):
        if not await is_admin(update, context, q.from_user.id):
            await q.answer("⛔ Admin only.", show_alert=True)
            return

        _, table_id, slot = data.split(":")
        table_id = int(table_id)
        slot = int(slot)

        conn = db()
        row = conn.execute(
            "SELECT chat_id,table_no,status FROM tables WHERE id=?",
            (table_id,)
        ).fetchone()
        conn.close()

        if not row:
            await q.answer("Table not found.", show_alert=True)
            return

        chat_id, table_no, status = row
        if status != "pending":
            await q.answer("This table is already closed.", show_alert=True)
            return

        # Build a tiny fake-independent flow using the callback's message.
        fake_update = update
        ok, msg = await resolve_winner(
            fake_update, context, table_no, slot, q.message
        )
        if ok:
            await q.answer("🏆 Winner recorded!")
        else:
            await q.answer(msg or "Unable to resolve.", show_alert=True)
        return

    if data.startswith("cancel:"):
        if not await is_admin(update, context, q.from_user.id):
            await q.answer("⛔ Admin only.", show_alert=True)
            return
        table_id = int(data.split(":")[1])
        conn = db()
        cur = conn.execute("""
            UPDATE tables SET status='cancelled'
            WHERE id=? AND status='pending'
        """, (table_id,))
        conn.commit()
        conn.close()
        if cur.rowcount:
            try:
                await q.message.edit_text("❌ This table has been cancelled by an admin.")
            except Exception:
                pass
            await q.answer("Table cancelled.")
        else:
            await q.answer("Table already closed.", show_alert=True)
        return

    if data == "my_points_button":
        row = get_or_create_user(q.message.chat_id, q.from_user)
        await q.answer(
            f"💰 {row[4]} points\n🎮 {row[5]} matches | 🏆 {row[6]} wins | 📉 {row[7]} losses",
            show_alert=True,
        )


async def postpointsbutton(update, context):
    if not await admin_only(update, context):
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Check My Points", callback_data="my_points_button")
    ]])
    await update.message.reply_text(
        "📊 Tap the button to privately check your points.",
        reply_markup=kb
    )


# ------------------------------------------------------------
# Admin points commands
# ------------------------------------------------------------

async def adjust_points(update, context, sign):
    if not await admin_only(update, context):
        return
    if len(context.args) != 2 or not context.args[1].lstrip("-").isdigit():
        cmd = "/addpoints" if sign > 0 else "/removepoints"
        await update.message.reply_text(f"Usage: {cmd} @user 50")
        return

    target = await resolve_member(context, update.effective_chat.id, context.args[0])
    if not target:
        await update.message.reply_text("❌ User not found in this group.")
        return

    amount = abs(int(context.args[1])) * sign
    ensure_user(update.effective_chat.id, target, 0)
    actual = change_points(update.effective_chat.id, target.id, amount)
    row = get_user(update.effective_chat.id, target.id)

    await update.message.reply_text(
        f"✅ {display_name(target)} balance changed by {actual:+d} pts.\n"
        f"💰 New balance: {row[4]} pts"
    )


async def addpoints(update, context):
    await adjust_points(update, context, 1)


async def removepoints(update, context):
    await adjust_points(update, context, -1)


async def set_balance(update, context):
    if not await admin_only(update, context):
        return
    if len(context.args) != 2 or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /set_balance @user 500")
        return
    target = await resolve_member(context, update.effective_chat.id, context.args[0])
    if not target:
        await update.message.reply_text("❌ User not found.")
        return
    value = int(context.args[1])
    ensure_user(update.effective_chat.id, target, 0)
    conn = db()
    conn.execute(
        "UPDATE users SET points=? WHERE chat_id=? AND user_id=?",
        (value, update.effective_chat.id, target.id)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ {display_name(target)} balance set to {value} pts."
    )


# ------------------------------------------------------------
# Admin tables/users
# ------------------------------------------------------------

async def list_tables(update, context):
    if not await admin_only(update, context):
        return
    conn = db()
    rows = conn.execute("""
        SELECT table_no,player1_name,player2_name,points,status,winner_name
        FROM tables WHERE chat_id=?
        ORDER BY table_no DESC LIMIT 15
    """, (update.effective_chat.id,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No tables yet.")
        return
    lines = ["📋 Recent Tables"]
    for no,p1,p2,pts,status,winner in rows:
        result = winner if winner else status
        lines.append(f"🎲 #{no} {p1} vs {p2} • {pts} pts • {result}")
    await update.message.reply_text("\n".join(lines))


async def list_users(update, context):
    if not await admin_only(update, context):
        return
    conn = db()
    rows = conn.execute("""
        SELECT display_name,points,matches,wins,losses
        FROM users WHERE chat_id=?
        ORDER BY points DESC
    """, (update.effective_chat.id,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No users recorded.")
        return
    lines = ["👥 Players"]
    for r in rows[:50]:
        lines.append(f"{r[0]} — {r[1]} pts | {r[2]} M | W {r[3]} | L {r[4]}")
    await update.message.reply_text("\n".join(lines))


# ------------------------------------------------------------
# Admin management
# ------------------------------------------------------------

async def add_admin(update, context):
    if not await telegram_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⛔ Only a Telegram group owner/admin can add bot-admins.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /add_admin USER_ID")
        return
    uid = int(context.args[0])
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO admins(chat_id,user_id) VALUES(?,?)",
        (update.effective_chat.id, uid)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Bot-admin added: {uid}")


async def rmv_admin(update, context):
    if not await telegram_admin(update, context, update.effective_user.id):
        await update.message.reply_text("⛔ Only a Telegram group owner/admin can remove bot-admins.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /rmv_admin USER_ID")
        return
    uid = int(context.args[0])
    conn = db()
    conn.execute(
        "DELETE FROM admins WHERE chat_id=? AND user_id=?",
        (update.effective_chat.id, uid)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Bot-admin removed: {uid}")


async def list_admins(update, context):
    if not await admin_only(update, context):
        return
    conn = db()
    rows = conn.execute(
        "SELECT user_id FROM admins WHERE chat_id=?",
        (update.effective_chat.id,)
    ).fetchall()
    conn.close()
    tg_admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    ids = [str(x.user.id) for x in tg_admins]
    lines = ["👑 Telegram admins: " + ", ".join(ids)]
    if rows:
        lines.append("🤖 Extra bot-admins: " + ", ".join(str(x[0]) for x in rows))
    else:
        lines.append("🤖 Extra bot-admins: none")
    await update.message.reply_text("\n".join(lines))


# ------------------------------------------------------------
# Notifications / utility
# ------------------------------------------------------------

async def send_notification(update, context):
    if not await admin_only(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /send_notification your message")
        return
    text = " ".join(context.args)
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM users WHERE chat_id=?",
        (update.effective_chat.id,)
    ).fetchall()
    conn.close()

    sent = 0
    for (uid,) in rows:
        try:
            await context.bot.send_message(uid, f"📢 {text}")
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"📢 Notification sent to {sent} users.")


async def post_new_member(update, context):
    chat_id = update.effective_chat.id
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        existing = get_user(chat_id, member.id)
        if existing is None:
            ensure_user(chat_id, member, JOIN_BONUS)
            await update.message.reply_text(
                f"🎉 Welcome {display_name(member)}!\n"
                f"💰 Joining bonus: +{JOIN_BONUS} pts\n"
                f"Use /mypoints to check your balance."
            )
        else:
            ensure_user(chat_id, member, 0)


# ------------------------------------------------------------
# Command aliases / menu
# ------------------------------------------------------------

PLAYER_COMMANDS = [
    ("start", "Start the bot"),
    ("mypoints", "Check your points"),
    ("mystats", "Matches, wins and losses"),
    ("leaderboard", "Top players"),
    ("id", "Show your Telegram ID"),
    ("help", "Show help"),
]

ADMIN_COMMANDS = PLAYER_COMMANDS + [
    ("newtable", "Create a points table"),
    ("win", "Set table winner"),
    ("cancel", "Cancel a table"),
    ("addpoints", "Add points to a player"),
    ("removepoints", "Remove points from a player"),
    ("set_balance", "Set exact player balance"),
    ("list", "List recent tables"),
    ("list_users", "List players"),
    ("postpointsbutton", "Post Check My Points button"),
    ("add_admin", "Add bot-admin"),
    ("rmv_admin", "Remove bot-admin"),
    ("list_admins", "List bot-admins"),
    ("send_notification", "Send group notification"),
]


async def setup_commands(app):
    # Telegram supports one command list per scope. Group admins will see
    # the admin menu; normal members see the player menu.
    await app.bot.set_my_commands(
        [BotCommand(a,b) for a,b in PLAYER_COMMANDS]
    )


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    start_health_server()

    app = Application.builder().token(BOT_TOKEN).post_init(setup_commands).build()

    # Player commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mypoints", mypoints))
    app.add_handler(CommandHandler("balance", mypoints))
    app.add_handler(CommandHandler("mystats", mystats))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("id", id_cmd))

    # Admin commands
    app.add_handler(CommandHandler("newtable", newtable))
    app.add_handler(CommandHandler("win", win_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("addpoints", addpoints))
    app.add_handler(CommandHandler("add", addpoints))
    app.add_handler(CommandHandler("removepoints", removepoints))
    app.add_handler(CommandHandler("minus", removepoints))
    app.add_handler(CommandHandler("remove", removepoints))
    app.add_handler(CommandHandler("set_balance", set_balance))
    app.add_handler(CommandHandler("list", list_tables))
    app.add_handler(CommandHandler("list_users", list_users))
    app.add_handler(CommandHandler("postpointsbutton", postpointsbutton))
    app.add_handler(CommandHandler("add_admin", add_admin))
    app.add_handler(CommandHandler("rmv_admin", rmv_admin))
    app.add_handler(CommandHandler("list_admins", list_admins))
    app.add_handler(CommandHandler("send_notification", send_notification))

    # Callbacks + new members
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, post_new_member)
    )

    # Optional webhook. Leave WEBHOOK_URL empty on Render for polling mode.
    if WEBHOOK_URL:
        log.info("Starting webhook mode at %s/webhook", WEBHOOK_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=f"{WEBHOOK_URL}/webhook",
        )
    else:
        log.info("Starting polling mode (recommended for your current Render setup).")
        app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
