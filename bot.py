import os
import asyncio
import sqlite3
import logging
from html import escape
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# ABOD LUDO KING ARENA - GROUP POINTS BOT
# - No username required for normal players.
# - Players are identified by Telegram numeric user_id.
# - Admins can target a player by replying to their message,
#   by Telegram mention, by exact registered name, or by ID.
# - Table stakes are reserved when a table is created.
# - Cancel refunds both stakes.
# - Winner receives the pot minus GROUP_TAX_PERCENT.
# - Group owner receives the service tax.
# - Match/win/loss statistics are stored per player.
# - A tiny standard-library HTTP server binds Render's PORT so
#   the bot can safely run as a Render Web Service while polling.
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("abod-ludo")
TABLE_LOCK = asyncio.Lock()

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "points.db")
# On Render, set WEBHOOK_URL to the public service URL, for example:
# https://your-service.onrender.com. Leave empty for local polling mode.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "telegram-webhook").strip("/")
if not WEBHOOK_PATH:
    WEBHOOK_PATH = "telegram-webhook"

# 5% as requested. Can be changed in Render Environment if needed.
try:
    GROUP_TAX_PERCENT = float(os.environ.get("GROUP_TAX_PERCENT", "5"))
except ValueError as exc:
    raise RuntimeError("GROUP_TAX_PERCENT must be a number") from exc
if not 0 <= GROUP_TAX_PERCENT <= 100:
    raise RuntimeError("GROUP_TAX_PERCENT must be between 0 and 100")

# Used only as a compatibility/default value for old installations.
POINTS_PER_WIN = int(os.environ.get("POINTS_PER_WIN", "10"))

# Joining bonus is intentionally disabled as requested.
JOIN_BONUS = 0

# ----------------------------- Database -----------------------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = db()

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
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tables (
            chat_id INTEGER NOT NULL,
            table_id INTEGER NOT NULL,
            player1_id INTEGER NOT NULL,
            player2_id INTEGER NOT NULL,
            stake INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            winner_id INTEGER,
            tax INTEGER NOT NULL DEFAULT 0,
            payout INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            PRIMARY KEY (chat_id, table_id)
        )
    """)

    # Always create the compatibility table. New databases do not have the
    # legacy `points` table, but ensure_user still checks this table during
    # username migration.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS legacy_points (
            chat_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            display_name TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, username)
        )
    """)

    # Old bot versions stored points by username. Preserve that data
    # so an existing points.db is not silently destroyed.
    old_exists = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='points'
    """).fetchone()

    if old_exists:
        old_columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(points)").fetchall()
        ]
        if {"chat_id", "username", "display_name", "points"}.issubset(old_columns):
            rows = conn.execute("""
                SELECT chat_id, username, display_name, points
                FROM points
            """).fetchall()

            for chat_id, username, display_name, points in rows:
                if username:
                    conn.execute("""
                        INSERT INTO legacy_points
                            (chat_id, username, display_name, points)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(chat_id, username) DO UPDATE SET
                            display_name=excluded.display_name,
                            points=excluded.points
                    """, (chat_id, username.lower().lstrip("@"),
                          display_name, int(points)))

    conn.commit()
    conn.close()


def display_name(user) -> str:
    parts = [getattr(user, "first_name", "") or ""]
    if getattr(user, "last_name", None):
        parts.append(user.last_name)
    return " ".join(x.strip() for x in parts if x.strip()) or "Player"


def norm_text(value: str) -> str:
    value = (value or "").strip().lower()
    value = value.replace("@", "")
    return " ".join(value.split())


def ensure_user(chat_id: int, user, starting_points: int = 0):
    name = display_name(user)
    username = getattr(user, "username", None)

    conn = db()
    conn.execute("""
        INSERT INTO users
            (chat_id, user_id, username, display_name, points)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            username=excluded.username,
            display_name=excluded.display_name,
            updated_at=CURRENT_TIMESTAMP
    """, (chat_id, user.id, username, name, starting_points))

    # One-time migration: if this user's old username existed in the
    # previous bot database, move the old balance to the numeric ID.
    if username:
        legacy = conn.execute("""
            SELECT points FROM legacy_points
            WHERE chat_id=? AND username=?
        """, (chat_id, username.lower())).fetchone()
        if legacy:
            current = conn.execute("""
                SELECT points FROM users WHERE chat_id=? AND user_id=?
            """, (chat_id, user.id)).fetchone()
            # Only import if this new numeric record has no balance yet.
            if current and int(current[0]) == 0 and int(legacy[0]) != 0:
                conn.execute("""
                    UPDATE users SET points=?, updated_at=CURRENT_TIMESTAMP
                    WHERE chat_id=? AND user_id=?
                """, (int(legacy[0]), chat_id, user.id))
                conn.execute("""
                    DELETE FROM legacy_points WHERE chat_id=? AND username=?
                """, (chat_id, username.lower()))

    conn.commit()
    conn.close()


def get_user(chat_id: int, user_id: int):
    conn = db()
    row = conn.execute("""
        SELECT user_id, username, display_name, points, matches, wins, losses
        FROM users WHERE chat_id=? AND user_id=?
    """, (chat_id, user_id)).fetchone()
    conn.close()
    return row


def get_points(chat_id: int, user_id: int) -> int:
    row = get_user(chat_id, user_id)
    return int(row[3]) if row else 0


def change_points(chat_id: int, user_id: int, amount: int):
    conn = db()
    conn.execute("""
        UPDATE users
        SET points=points+?, updated_at=CURRENT_TIMESTAMP
        WHERE chat_id=? AND user_id=?
    """, (int(amount), chat_id, user_id))
    conn.commit()
    conn.close()


def set_points(chat_id: int, user_id: int, amount: int):
    conn = db()
    conn.execute("""
        UPDATE users
        SET points=?, updated_at=CURRENT_TIMESTAMP
        WHERE chat_id=? AND user_id=?
    """, (max(0, int(amount)), chat_id, user_id))
    conn.commit()
    conn.close()


def update_stats(chat_id: int, user_id: int, win: bool):
    conn = db()
    conn.execute("""
        UPDATE users
        SET matches=matches+1,
            wins=wins+?,
            losses=losses+?,
            updated_at=CURRENT_TIMESTAMP
        WHERE chat_id=? AND user_id=?
    """, (1 if win else 0, 0 if win else 1, chat_id, user_id))
    conn.commit()
    conn.close()


def leaderboard_rows(chat_id: int, limit: int = 10):
    conn = db()
    rows = conn.execute("""
        SELECT display_name, points, wins, losses, matches
        FROM users
        WHERE chat_id=?
        ORDER BY points DESC, wins DESC, matches DESC
        LIMIT ?
    """, (chat_id, limit)).fetchall()
    conn.close()
    return rows


def all_users(chat_id: int):
    conn = db()
    rows = conn.execute("""
        SELECT user_id, display_name, username, points, matches, wins, losses
        FROM users
        WHERE chat_id=?
        ORDER BY points DESC, display_name COLLATE NOCASE
    """, (chat_id,)).fetchall()
    conn.close()
    return rows


def recent_tables(chat_id: int, limit: int = 10):
    conn = db()
    rows = conn.execute("""
        SELECT table_id, player1_id, player2_id, stake, status,
               winner_id, tax, payout, created_at
        FROM tables
        WHERE chat_id=?
        ORDER BY table_id DESC
        LIMIT ?
    """, (chat_id, limit)).fetchall()
    conn.close()
    return rows


def next_table_id(chat_id: int) -> int:
    conn = db()
    row = conn.execute("""
        SELECT COALESCE(MAX(table_id), 0)+1
        FROM tables WHERE chat_id=?
    """, (chat_id,)).fetchone()
    conn.close()
    return int(row[0])


def get_table(chat_id: int, table_id: int):
    conn = db()
    row = conn.execute("""
        SELECT table_id, player1_id, player2_id, stake, status,
               winner_id, tax, payout, created_by
        FROM tables
        WHERE chat_id=? AND table_id=?
    """, (chat_id, table_id)).fetchone()
    conn.close()
    return row


def active_table_for_player(chat_id: int, user_id: int):
    conn = db()
    row = conn.execute("""
        SELECT table_id, player1_id, player2_id, stake, status
        FROM tables
        WHERE chat_id=? AND status='active'
          AND (player1_id=? OR player2_id=?)
        LIMIT 1
    """, (chat_id, user_id, user_id)).fetchone()
    conn.close()
    return row


def create_table_record(chat_id, table_id, p1, p2, stake, created_by):
    conn = db()
    conn.execute("""
        INSERT INTO tables
            (chat_id, table_id, player1_id, player2_id, stake, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (chat_id, table_id, p1, p2, stake, created_by))
    conn.commit()
    conn.close()


def finish_table(chat_id, table_id, winner_id, tax, payout):
    conn = db()
    conn.execute("""
        UPDATE tables
        SET status='finished', winner_id=?, tax=?, payout=?,
            finished_at=CURRENT_TIMESTAMP
        WHERE chat_id=? AND table_id=? AND status='active'
    """, (winner_id, tax, payout, chat_id, table_id))
    conn.commit()
    conn.close()


def cancel_table_record(chat_id, table_id):
    conn = db()
    conn.execute("""
        UPDATE tables
        SET status='cancelled', finished_at=CURRENT_TIMESTAMP
        WHERE chat_id=? AND table_id=? AND status='active'
    """, (chat_id, table_id))
    conn.commit()
    conn.close()


# ----------------------------- Admins -----------------------------

async def telegram_is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
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
        log.exception("Unable to check Telegram admin status")
        return False


def local_admin_exists(chat_id: int, user_id: int) -> bool:
    conn = db()
    row = conn.execute("""
        SELECT 1 FROM admins WHERE chat_id=? AND user_id=?
    """, (chat_id, user_id)).fetchone()
    conn.close()
    return bool(row)


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    # Real Telegram group admins/owner are always admins.
    if await telegram_is_group_admin(update, context, user_id):
        return True
    return local_admin_exists(update.effective_chat.id, user_id)


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await is_admin(update, context, update.effective_user.id):
        await reply(update, "⛔ Only group admins can use this command.")
        return False
    return True


async def get_group_owner_id(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        for member in admins:
            if member.status == ChatMemberStatus.OWNER:
                return member.user.id
    except Exception:
        log.exception("Could not find group owner")
    return None


# ----------------------------- Telegram helpers -----------------------------

async def reply(update: Update, text: str, **kwargs):
    if update.message:
        return await update.message.reply_text(text, **kwargs)
    if update.callback_query and update.callback_query.message:
        return await update.callback_query.message.reply_text(text, **kwargs)
    return None


def chat_title(update: Update) -> str:
    chat = update.effective_chat
    return chat.title if chat and chat.title else "Ludo Group"


def mention_html(user_id: int, name: str) -> str:
    safe = (
        str(name)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def player_label(row):
    # row from users: user_id, username, display_name, points, ...
    return row[2]


def register_message_user(update: Update):
    if not update.effective_chat or not update.effective_user:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    try:
        ensure_user(update.effective_chat.id, update.effective_user)
    except Exception:
        log.exception("Could not register user")


# ----------------------------- Player resolver -----------------------------

def users_for_chat(chat_id: int):
    conn = db()
    rows = conn.execute("""
        SELECT user_id, username, display_name, points, matches, wins, losses
        FROM users WHERE chat_id=?
    """, (chat_id,)).fetchall()
    conn.close()
    return rows


def resolve_name(chat_id: int, text: str):
    """
    Resolve a player without requiring @username:
      1) numeric Telegram ID
      2) exact display name
      3) exact username
      4) unique normalized partial display-name match
    Returns the full user row or None.
    """
    q = norm_text(text)
    if not q:
        return None

    rows = users_for_chat(chat_id)

    if q.isdigit():
        uid = int(q)
        matches = [r for r in rows if r[0] == uid]
        if matches:
            return matches[0]

    exact_name = [r for r in rows if norm_text(r[2]) == q]
    if len(exact_name) == 1:
        return exact_name[0]
    if len(exact_name) > 1:
        return None

    exact_username = [
        r for r in rows if r[1] and norm_text(r[1]) == q
    ]
    if len(exact_username) == 1:
        return exact_username[0]

    partial = [
        r for r in rows
        if q in norm_text(r[2]) or (r[1] and q in norm_text(r[1]))
    ]
    if len(partial) == 1:
        return partial[0]

    return None


def mentioned_users(update: Update):
    """
    Extract Telegram entity mentions. TEXT_MENTION is ideal because it
    contains the actual user_id even when the user has no username.
    """
    msg = update.message
    if not msg or not msg.entities:
        return []

    found = []
    text = msg.text or ""
    for ent in msg.entities:
        try:
            if ent.type == "text_mention" and ent.user:
                found.append(ent.user)
            elif ent.type == "mention":
                # Telegram entity offsets are UTF-16 based; parse_entity
                # handles them correctly for emoji/non-BMP characters.
                found.append(msg.parse_entity(ent))
        except Exception:
            continue
    return found


def resolve_from_reply(update: Update):
    msg = update.message
    if not msg or not msg.reply_to_message:
        return None

    target = msg.reply_to_message.from_user
    if not target or target.is_bot:
        return None

    ensure_user(update.effective_chat.id, target)
    return get_user(update.effective_chat.id, target.id)


def resolve_target(update: Update, raw_text: str = ""):
    # Best method: replying to the player's message.
    replied = resolve_from_reply(update)
    if replied:
        return replied

    # Second-best: Telegram's real user mention.
    mentions = mentioned_users(update)
    if mentions:
        first = mentions[0]
        if hasattr(first, "id"):
            ensure_user(update.effective_chat.id, first)
            return get_user(update.effective_chat.id, first.id)
        return resolve_name(update.effective_chat.id, first)

    return resolve_name(update.effective_chat.id, raw_text)


def split_two_players(chat_id: int, raw: str):
    """
    Supports:
      /new SicBo Ajju Mansuri 20
      /new @SicBo @AjjuMansuri 20
      /new SicBo Ajju Mansuri 20   (registered display names)
    We try every split and select the pair that resolves uniquely.
    """
    tokens = raw.split()
    if len(tokens) < 2:
        return None, None

    candidates = []
    for i in range(1, len(tokens)):
        left = " ".join(tokens[:i]).strip()
        right = " ".join(tokens[i:]).strip()
        a = resolve_name(chat_id, left)
        b = resolve_name(chat_id, right)
        if a and b and a[0] != b[0]:
            candidates.append((a, b))

    # Prefer the split with the longest exact player-name matches.
    if candidates:
        candidates.sort(
            key=lambda pair: (
                len(norm_text(pair[0][2])),
                len(norm_text(pair[1][2])),
            ),
            reverse=True,
        )
        return candidates[0]

    # Fallback for simple one-token names.
    if len(tokens) == 2:
        a = resolve_name(chat_id, tokens[0])
        b = resolve_name(chat_id, tokens[1])
        if a and b and a[0] != b[0]:
            return a, b

    return None, None


# ----------------------------- Commands -----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_message_user(update)

    text = (
        f"🎲 <b>{chat_title(update)}</b>\n"
        "Ludo Points Arena — no real-money gambling.\n\n"
        "👤 <b>Players</b>\n"
        "/mypoints — current points\n"
        "/mystats — matches, wins, losses\n"
        "/leaderboard — top players\n"
        "/id — your Telegram ID\n"
        "/help — commands\n\n"
        "👑 <b>Admins</b>\n"
        "/new Player1 Player2 50 — create table\n"
        "/cancel 12 — cancel/refund table #12\n"
        "/win 12 1 — player 1 wins table #12\n"
        "/add 50 — add points (reply to player)\n"
        "/minus 50 — remove points (reply to player)\n"
        "/set 500 — set balance (reply to player)\n"
        "/list — recent tables\n"
        "/users — registered players\n"
        "/addadmin 123456789 — add bot admin\n"
        "/rmadmin 123456789 — remove bot admin\n"
        "/admins — list bot admins\n\n"
        f"💰 Table winner payout: pot − {GROUP_TAX_PERCENT:g}% group service tax.\n"
        "🚫 No joining bonus. Username is NOT required."
    )
    await reply(update, text, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_message_user(update)
    await reply(
        update,
        f"🪪 <b>{display_name(update.effective_user)}</b>\n"
        f"Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )


async def mypoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_message_user(update)
    pts = get_points(update.effective_chat.id, update.effective_user.id)
    await reply(
        update,
        f"💰 <b>{display_name(update.effective_user)}</b>\n"
        f"Balance: <b>{pts} pts</b>",
        parse_mode="HTML",
    )


async def mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_message_user(update)
    row = get_user(update.effective_chat.id, update.effective_user.id)
    if not row:
        await reply(update, "📊 No stats yet.")
        return

    _, _, name, points, matches, wins, losses = row
    name = escape(str(name))
    await reply(
        update,
        f"📊 <b>{name}</b>\n\n"
        f"💰 Balance: <b>{points} pts</b>\n"
        f"🎮 Matches: <b>{matches}</b>\n"
        f"🏆 Wins: <b>{wins}</b>\n"
        f"❌ Losses: <b>{losses}</b>",
        parse_mode="HTML",
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = leaderboard_rows(update.effective_chat.id)
    if not rows:
        await reply(update, "🏆 No players registered yet.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 <b>{chat_title(update)} — Leaderboard</b>"]
    for i, (name, pts, wins, losses, matches) in enumerate(rows):
        name = escape(str(name))
        prefix = medals[i] if i < 3 else f"{i+1}."
        lines.append(
            f"{prefix} <b>{name}</b> — {pts} pts "
            f"(W {wins} / L {losses})"
        )
    await reply(update, "\n".join(lines), parse_mode="HTML")


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await manual_adjust(update, context, 1)


async def minus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await manual_adjust(update, context, -1)


async def manual_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE, sign: int):
    if not await require_admin(update, context):
        return

    if not context.args:
        cmd = "/add" if sign > 0 else "/minus"
        await reply(
            update,
            f"Usage: {cmd} 20 (reply to player)\n"
            f"or {cmd} Player Name 20"
        )
        return

    amount_token = context.args[-1]
    if not amount_token.lstrip("-").isdigit():
        await reply(update, "❌ Last value must be a number.")
        return

    amount = abs(int(amount_token))
    target_text = " ".join(context.args[:-1]).strip()

    target = resolve_target(update, target_text)
    if not target:
        await reply(
            update,
            "❌ Player not found.\n"
            "Best method: reply to the player's message and use "
            f"{'/add' if sign > 0 else '/minus'} 20."
        )
        return

    uid, username, name, points, *_ = target
    safe_name = escape(str(name))

    if sign < 0 and points < amount:
        await reply(
            update,
            f"❌ {safe_name} has only {points} pts. Cannot remove {amount}."
        )
        return

    change_points(update.effective_chat.id, uid, sign * amount)
    new_total = get_points(update.effective_chat.id, uid)

    icon = "➕" if sign > 0 else "➖"
    await reply(
        update,
        f"{icon} <b>{safe_name}</b> → {sign*amount:+d} pts\n"
        f"💰 Balance: <b>{new_total} pts</b>",
        parse_mode="HTML",
    )


async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    if not context.args or not context.args[-1].isdigit():
        await reply(update, "Usage: /set 500 (reply to player)")
        return

    amount = int(context.args[-1])
    target_text = " ".join(context.args[:-1]).strip()
    target = resolve_target(update, target_text)

    if not target:
        await reply(
            update,
            "❌ Player not found. Reply to the player's message and use /set 500."
        )
        return

    uid, _, name, *_ = target
    safe_name = escape(str(name))
    set_points(update.effective_chat.id, uid, amount)
    await reply(
        update,
        f"⚙️ <b>{safe_name}</b>\n💰 New balance: <b>{amount} pts</b>",
        parse_mode="HTML",
    )


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    if len(context.args) < 3 or not context.args[-1].isdigit():
        await reply(
            update,
            "🎲 Usage: /new Player1 Player2 50\n"
            "Example: /new SicBo Ajju Mansuri 50"
        )
        return

    stake = int(context.args[-1])
    if stake <= 0:
        await reply(update, "❌ Table points must be greater than 0.")
        return

    players_raw = " ".join(context.args[:-1])
    p1, p2 = split_two_players(update.effective_chat.id, players_raw)

    # If the command uses Telegram TEXT_MENTION entities, use them.
    mentions = mentioned_users(update)
    real_mentions = [m for m in mentions if hasattr(m, "id")]
    if len(real_mentions) >= 2:
        ensure_user(update.effective_chat.id, real_mentions[0])
        ensure_user(update.effective_chat.id, real_mentions[1])
        p1 = get_user(update.effective_chat.id, real_mentions[0].id)
        p2 = get_user(update.effective_chat.id, real_mentions[1].id)

    if not p1 or not p2:
        await reply(
            update,
            "❌ I couldn't identify both players.\n\n"
            "Use their registered names, e.g.\n"
            "/new SicBo Ajju Mansuri 50\n\n"
            "If names are similar/duplicate, mention/tag the two players "
            "or register them first with /start."
        )
        return

    if p1[0] == p2[0]:
        await reply(update, "❌ A player cannot play against himself.")
        return

    # No overlapping active tables for either player.
    if active_table_for_player(update.effective_chat.id, p1[0]):
        await reply(update, f"⚠️ {p1[2]} is already in an active table.")
        return
    if active_table_for_player(update.effective_chat.id, p2[0]):
        await reply(update, f"⚠️ {p2[2]} is already in an active table.")
        return

    b1 = int(p1[3])
    b2 = int(p2[3])

    if b1 < stake:
        await reply(
            update,
            f"❌ Balance check failed!\n"
            f"{escape(str(p1[2]))} has {b1} pts, but table needs {stake} pts."
        )
        return

    if b2 < stake:
        await reply(
            update,
            f"❌ Balance check failed!\n"
            f"{escape(str(p2[2]))} has {b2} pts, but table needs {stake} pts."
        )
        return

    table_id = next_table_id(update.effective_chat.id)

    # Reserve both stakes immediately.
    change_points(update.effective_chat.id, p1[0], -stake)
    change_points(update.effective_chat.id, p2[0], -stake)

    create_table_record(
        update.effective_chat.id,
        table_id,
        p1[0],
        p2[0],
        stake,
        update.effective_user.id,
    )

    group = escape(chat_title(update))
    safe_p1 = escape(str(p1[2]))
    safe_p2 = escape(str(p2[2]))
    total_pot = stake * 2

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"🏆 {str(p1[2])[:28]}",
                callback_data=f"win:{table_id}:1",
            ),
            InlineKeyboardButton(
                f"🏆 {str(p2[2])[:28]}",
                callback_data=f"win:{table_id}:2",
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"cancel:{table_id}",
            )
        ],
    ])

    text = (
        "👀 <b>Balance check passed!</b>\n\n"
        f"🎲 <b>{group}</b>\n"
        f"<b>Table No. {table_id}</b>\n"
        f"👤 {safe_p1} <b>vs</b> {safe_p2}\n"
        f"💰 Table Points: <b>{stake}</b> each\n"
        f"🏦 Total Pot: <b>{total_pot}</b> pts\n"
        f"🏆 Winner: <b>Pending</b>\n\n"
        f"🔒 {stake} pts from each player is reserved."
    )

    await reply(
        update,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    if not context.args or not context.args[0].isdigit():
        await reply(update, "Usage: /cancel 12")
        return

    await cancel_table_logic(
        update.effective_chat.id,
        int(context.args[0]),
        update,
        context,
    )


async def win_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    if len(context.args) < 2 or not context.args[0].isdigit() or context.args[1] not in ("1", "2"):
        await reply(update, "Usage: /win 12 1   (1 = player 1, 2 = player 2)")
        return

    await finish_table_logic(
        update.effective_chat.id,
        int(context.args[0]),
        int(context.args[1]),
        update,
        context,
    )


async def finish_table_logic(chat_id, table_id, winner_slot, update, context):
    # Telegram may deliver two fast callback clicks concurrently. Serialize
    # settlement/refund operations in this process to prevent double payouts.
    async with TABLE_LOCK:
        await _finish_table_logic_locked(chat_id, table_id, winner_slot, update, context)


async def _finish_table_logic_locked(chat_id, table_id, winner_slot, update, context):
    table = get_table(chat_id, table_id)
    if not table:
        await reply(update, f"❌ Table #{table_id} not found.")
        return

    _, p1_id, p2_id, stake, status, winner_id, old_tax, old_payout, _ = table

    if status != "active":
        await reply(update, f"⚠️ Table #{table_id} is already {status}.")
        return

    winner_id = p1_id if winner_slot == 1 else p2_id
    loser_id = p2_id if winner_slot == 1 else p1_id

    p1 = get_user(chat_id, p1_id)
    p2 = get_user(chat_id, p2_id)
    winner = get_user(chat_id, winner_id)
    loser = get_user(chat_id, loser_id)

    if not winner or not loser:
        await reply(update, "❌ Player data missing; table was not settled.")
        return

    pot = int(stake) * 2
    tax = int(pot * GROUP_TAX_PERCENT / 100)
    payout = pot - tax

    # Winner receives pot minus group service tax.
    change_points(chat_id, winner_id, payout)

    # Match statistics are recorded only after a real result.
    update_stats(chat_id, winner_id, True)
    update_stats(chat_id, loser_id, False)

    # Service tax goes to the Telegram group owner.
    owner_id = await get_group_owner_id(chat_id, context)
    owner_name = None
    if owner_id:
        owner_user = get_user(chat_id, owner_id)
        if not owner_user:
            try:
                owner_member = await context.bot.get_chat_member(chat_id, owner_id)
                ensure_user(chat_id, owner_member.user)
                owner_user = get_user(chat_id, owner_id)
            except Exception:
                owner_user = None

        if owner_user:
            change_points(chat_id, owner_id, tax)
            owner_name = owner_user[2]

    finish_table(chat_id, table_id, winner_id, tax, payout)

    safe_winner = escape(str(winner[2]))
    safe_loser = escape(str(loser[2]))
    winner_after = get_points(chat_id, winner_id)
    loser_after = get_points(chat_id, loser_id)

    text = (
        f"🎉 <b>Table #{table_id} Finished!</b>\n\n"
        f"🏆 Winner: <b>{safe_winner}</b>\n"
        f"💰 Table: <b>{stake} + {stake} = {pot}</b> pts\n"
        f"🧾 Group tax: <b>{tax}</b> pts ({GROUP_TAX_PERCENT:g}%)\n"
        f"💵 Winner payout: <b>{payout}</b> pts\n\n"
        f"🥇 {safe_winner} balance: <b>{winner_after}</b> pts\n"
        f"❌ {safe_loser} balance: <b>{loser_after}</b> pts\n"
        f"📊 {safe_winner}: W +1\n"
        f"📊 {safe_loser}: L +1"
    )
    if owner_name:
        text += f"\n👑 Owner service tax: <b>{escape(str(owner_name))}</b> +{tax} pts"

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text,
                parse_mode="HTML",
            )
        except Exception:
            await reply(update, text, parse_mode="HTML")
    else:
        await reply(update, text, parse_mode="HTML")


async def cancel_table_logic(chat_id, table_id, update, context):
    # Serialize refunds with winner settlement and other cancel callbacks.
    async with TABLE_LOCK:
        await _cancel_table_logic_locked(chat_id, table_id, update, context)


async def _cancel_table_logic_locked(chat_id, table_id, update, context):
    table = get_table(chat_id, table_id)
    if not table:
        await reply(update, f"❌ Table #{table_id} not found.")
        return

    _, p1_id, p2_id, stake, status, _, _, _, _ = table

    if status != "active":
        await reply(update, f"⚠️ Table #{table_id} is already {status}.")
        return

    # Refund the reserved stakes.
    change_points(chat_id, p1_id, stake)
    change_points(chat_id, p2_id, stake)
    cancel_table_record(chat_id, table_id)

    p1 = get_user(chat_id, p1_id)
    p2 = get_user(chat_id, p2_id)
    safe_p1 = escape(str(p1[2])) if p1 else str(p1_id)
    safe_p2 = escape(str(p2[2])) if p2 else str(p2_id)

    text = (
        f"❌ <b>Table #{table_id} Cancelled</b>\n\n"
        f"↩️ {safe_p1}: +{stake} refund\n"
        f"↩️ {safe_p2}: +{stake} refund\n"
        "📊 No match/loss was recorded."
    )

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text,
                parse_mode="HTML",
            )
        except Exception:
            await reply(update, text, parse_mode="HTML")
    else:
        await reply(update, text, parse_mode="HTML")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    rows = recent_tables(update.effective_chat.id)
    if not rows:
        await reply(update, "📋 No tables yet.")
        return

    lines = ["📋 <b>Recent Tables</b>"]
    for row in rows:
        table_id, p1_id, p2_id, stake, status, winner_id, tax, payout, created = row
        p1 = get_user(update.effective_chat.id, p1_id)
        p2 = get_user(update.effective_chat.id, p2_id)
        n1 = p1[2] if p1 else str(p1_id)
        n2 = p2[2] if p2 else str(p2_id)

        if status == "finished":
            winner = get_user(update.effective_chat.id, winner_id)
            result = f"🏆 {winner[2]}" if winner else "🏆 Finished"
        elif status == "cancelled":
            result = "❌ Cancelled"
        else:
            result = "⏳ Pending"

        lines.append(
            f"🎲 <b>#{table_id}</b> {n1} vs {n2}\n"
            f"💰 {stake} each • {result}"
        )

    await reply(update, "\n".join(lines), parse_mode="HTML")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    rows = all_users(update.effective_chat.id)
    if not rows:
        await reply(update, "👥 No registered players.")
        return

    lines = ["👥 <b>Registered Players</b>"]
    for i, (uid, name, username, points, matches, wins, losses) in enumerate(rows, 1):
        lines.append(
            f"{i}. {escape(str(name))} — {points} pts | "
            f"W {wins} / L {losses} | ID {uid}"
        )

    # Telegram message length protection.
    text = "\n".join(lines)
    await reply(update, text[:3900], parse_mode="HTML")


async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await telegram_is_group_admin(update, context, update.effective_user.id):
        await reply(update, "⛔ Only real Telegram group admins can manage bot admins.")
        return

    if not context.args or not context.args[0].isdigit():
        await reply(update, "Usage: /addadmin 123456789")
        return

    uid = int(context.args[0])
    conn = db()
    conn.execute("""
        INSERT OR IGNORE INTO admins(chat_id, user_id) VALUES (?, ?)
    """, (update.effective_chat.id, uid))
    conn.commit()
    conn.close()
    await reply(update, f"👑 Bot admin added: <code>{uid}</code>", parse_mode="HTML")


async def rmadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await telegram_is_group_admin(update, context, update.effective_user.id):
        await reply(update, "⛔ Only real Telegram group admins can manage bot admins.")
        return

    if not context.args or not context.args[0].isdigit():
        await reply(update, "Usage: /rmadmin 123456789")
        return

    uid = int(context.args[0])
    conn = db()
    conn.execute("""
        DELETE FROM admins WHERE chat_id=? AND user_id=?
    """, (update.effective_chat.id, uid))
    conn.commit()
    conn.close()
    await reply(update, f"🗑️ Bot admin removed: <code>{uid}</code>", parse_mode="HTML")


async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    conn = db()
    rows = conn.execute("""
        SELECT user_id FROM admins WHERE chat_id=?
        ORDER BY user_id
    """, (update.effective_chat.id,)).fetchall()
    conn.close()

    owner_id = await get_group_owner_id(update.effective_chat.id, context)

    lines = ["👑 <b>Bot Admins</b>"]
    if owner_id:
        lines.append(f"🏠 Group Owner: <code>{owner_id}</code>")
    for (uid,) in rows:
        lines.append(f"• <code>{uid}</code>")

    await reply(update, "\n".join(lines), parse_mode="HTML")


async def postpoints_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 My Points", callback_data="mypoints")]
    ])
    await reply(
        update,
        "📌 <b>Check your balance anytime</b> 👇",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ----------------------------- Callback buttons -----------------------------

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    if data == "mypoints":
        user = query.from_user
        ensure_user(query.message.chat_id, user)
        pts = get_points(query.message.chat_id, user.id)
        await query.answer(
            f"💰 {pts} points",
            show_alert=True,
        )
        return

    if data.startswith("win:"):
        parts = data.split(":")
        if len(parts) != 3:
            return

        try:
            table_id = int(parts[1])
            slot = int(parts[2])
        except (TypeError, ValueError):
            await query.answer("Invalid table action.", show_alert=True)
            return
        if slot not in (1, 2):
            await query.answer("Invalid winner selection.", show_alert=True)
            return

        if not await is_admin(update, context, query.from_user.id):
            await query.answer(
                "⛔ Only admins can declare the winner.",
                show_alert=True,
            )
            return

        await query.answer("🏆 Processing result...")
        await finish_table_logic(
            query.message.chat_id,
            table_id,
            slot,
            update,
            context,
        )
        return

    if data.startswith("cancel:"):
        parts = data.split(":")
        if len(parts) != 2:
            return

        try:
            table_id = int(parts[1])
        except (TypeError, ValueError):
            await query.answer("Invalid table action.", show_alert=True)
            return

        if not await is_admin(update, context, query.from_user.id):
            await query.answer(
                "⛔ Only admins can cancel a table.",
                show_alert=True,
            )
            return

        await query.answer("❌ Cancelling table...")
        await cancel_table_logic(
            query.message.chat_id,
            table_id,
            update,
            context,
        )
        return


# ----------------------------- Group registration -----------------------------

async def new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        # No joining bonus. Just register the player by numeric ID/name.
        ensure_user(chat_id, member)

        await update.message.reply_text(
            f"👋 Welcome <b>{display_name(member)}</b>!\n"
            "🎲 You're registered. No @username is required.\n"
            "💰 Check balance: /mypoints\n"
            "📊 Stats: /mystats",
            parse_mode="HTML",
        )


# ----------------------------- Render HTTP health server -----------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ABOD Ludo Bot is alive"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        # Keep Render logs clean; Telegram activity is already logged.
        return


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("HTTP health server listening on 0.0.0.0:%s", PORT)
    return server


# ----------------------------- Main -----------------------------

def main():
    init_db()
    start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()

    # Player commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mypoints", mypoints))
    app.add_handler(CommandHandler("mystats", mystats))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("id", id_cmd))

    # Admin commands
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("newtable", new_cmd))  # compatibility
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("win", win_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("addpoints", add_cmd))  # compatibility
    app.add_handler(CommandHandler("minus", minus_cmd))
    app.add_handler(CommandHandler("removepoints", minus_cmd))  # compatibility
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("set_balance", set_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("list_users", users_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("rmadmin", rmadmin_cmd))
    app.add_handler(CommandHandler("list_admins", admins_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("postpoints", postpoints_cmd))
    app.add_handler(CommandHandler("postpointsbutton", postpoints_cmd))

    # Group registration and buttons
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            new_members,
        )
    )
    app.add_handler(CallbackQueryHandler(callback))

    log.info("Starting ABOD Ludo Bot in polling mode...")
    log.info(
        "Table tax=%s%% | joining bonus=%s | port=%s",
        GROUP_TAX_PERCENT,
        JOIN_BONUS,
        PORT,
    )

    if WEBHOOK_URL:
        # Webhook mode is recommended for Render Web Service. Telegram sends
        # updates to Render as HTTP POST requests, so the service receives
        # real traffic instead of relying on an idle polling process.
        webhook_url = f"{WEBHOOK_URL}/{WEBHOOK_PATH}"
        log.info("Starting webhook mode at %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
    else:
        # Local/fallback mode. Render should use WEBHOOK_URL instead.
        start_health_server()
        log.info("Starting polling mode...")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )


if __name__ == "__main__":
    main()
