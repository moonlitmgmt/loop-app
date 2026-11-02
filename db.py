"""Loop database layer: SQLite schema, password hashing, and demo seed data.

All state lives in SQLite. Nothing here calls any outside service; the
paypal_subscriptions table only records PayPal subscription ids and
their status. Demo seed rows are clearly labeled as demo.
"""

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'free',
    bio TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS friendships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a INTEGER NOT NULL,
    user_b INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requester_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_a, user_b)
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    audience TEXT NOT NULL DEFAULT 'public',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS taps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    media_filename TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    is_snip INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artist_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    artist_name TEXT NOT NULL,
    artist_handle TEXT NOT NULL,
    craft TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'local',
    hometown TEXT NOT NULL DEFAULT '',
    story_bio TEXT NOT NULL DEFAULT '',
    reach_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS showcase_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_page_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    media_filename TEXT,
    media_type TEXT NOT NULL DEFAULT 'text',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS linked_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    retention TEXT NOT NULL,
    saved_until TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paypal_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    paypal_subscription_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

DEMO_PASSWORD = "demo1234"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def iso_days_ago(days, hour=12, minute=0):
    dt = datetime.now(timezone.utc).replace(hour=hour, minute=minute, second=0)
    return (dt - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), 200_000
    ).hex()
    return "pbkdf2$%s$%s" % (salt, digest)


def verify_password(password, stored):
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), 200_000
    ).hex()
    return secrets.compare_digest(check, digest)


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            seed_demo_data(conn)
        conn.commit()
    finally:
        conn.close()


def _user_id(conn, username):
    row = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()
    return row["id"] if row else None


def seed_demo_data(conn):
    """Seed clearly labeled demo rows. Demo password for all three is demo1234."""
    now = now_iso()
    demos = [
        ("demo_amy", "Amy Rivera (demo)", "amy.demo@example.com", "free",
         "Demo account. I make music and short videos."),
        ("demo_bo", "Bo Carter (demo)", "bo.demo@example.com", "pro",
         "Demo account. Guitarist and songwriter."),
        ("demo_june", "June Park (demo)", "june.demo@example.com", "elite",
         "Demo account. Photographer and visual artist."),
    ]
    for username, display, email, tier, bio in demos:
        conn.execute(
            "INSERT INTO users (username, display_name, email, password_hash,"
            " tier, bio, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, display, email, hash_password(DEMO_PASSWORD), tier, bio, now),
        )
    amy = _user_id(conn, "demo_amy")
    bo = _user_id(conn, "demo_bo")
    june = _user_id(conn, "demo_june")

    # Accepted friendships: Amy <-> Bo, Bo <-> June.
    for a, b in ((amy, bo), (bo, june)):
        lo, hi = min(a, b), max(a, b)
        conn.execute(
            "INSERT INTO friendships (user_a, user_b, status, requester_id,"
            " created_at) VALUES (?, ?, 'accepted', ?, ?)",
            (lo, hi, a, now),
        )

    # Posts: public ones plus one friends-only post from Bo.
    conn.execute(
        "INSERT INTO posts (user_id, body, audience, created_at)"
        " VALUES (?, ?, 'public', ?)",
        (amy, "Demo post: hello Loop! This is demo content.", now),
    )
    conn.execute(
        "INSERT INTO posts (user_id, body, audience, created_at)"
        " VALUES (?, ?, 'public', ?)",
        (bo, "Demo post: new song sketch is up on my artist page.", now),
    )
    conn.execute(
        "INSERT INTO posts (user_id, body, audience, created_at)"
        " VALUES (?, ?, 'friends', ?)",
        (bo, "Demo friends-only post: rough draft lyrics, friends only.", now),
    )

    # Artist pages: one local, one worldwide.
    cur = conn.execute(
        "INSERT INTO artist_pages (user_id, artist_name, artist_handle, craft,"
        " scope, hometown, story_bio, reach_text, created_at)"
        " VALUES (?, 'Bo Beats', '@bo_beats', 'Music', 'local', 'Oshkosh',"
        " 'Demo story: a guitarist writing songs in the Fox Valley.',"
        " 'Demo contact: message me here on Loop.', ?)",
        (bo, now),
    )
    bo_page = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO artist_pages (user_id, artist_name, artist_handle, craft,"
        " scope, hometown, story_bio, reach_text, created_at)"
        " VALUES (?, 'June Frames', '@june_frames', 'Photography',"
        " 'worldwide', 'Kyoto',"
        " 'Demo story: a photographer chasing light around the world.',"
        " 'Demo contact: message me here on Loop.', ?)",
        (june, now),
    )
    june_page = cur.lastrowid
    conn.execute(
        "INSERT INTO showcase_items (artist_page_id, title, media_type,"
        " created_at) VALUES (?, 'Demo track: River Song (demo)', 'text', ?)",
        (bo_page, now),
    )
    conn.execute(
        "INSERT INTO showcase_items (artist_page_id, title, media_type,"
        " created_at) VALUES (?, 'Demo photo set: Morning Light (demo)',"
        " 'text', ?)",
        (june_page, now),
    )

    # Linked accounts: Bo is at the Pro cap of 2; June (Elite) has 3.
    conn.execute(
        "INSERT INTO linked_accounts (user_id, platform, handle, created_at)"
        " VALUES (?, 'YouTube', '@bo_beats_demo', ?)",
        (bo, now),
    )
    conn.execute(
        "INSERT INTO linked_accounts (user_id, platform, handle, created_at)"
        " VALUES (?, 'Patreon', 'bo_beats_demo', ?)",
        (bo, now),
    )
    conn.execute(
        "INSERT INTO linked_accounts (user_id, platform, handle, created_at)"
        " VALUES (?, 'Twitch', 'june_frames_demo', ?)",
        (june, now),
    )

    # Messages across three consecutive days give Bo <-> Amy a 3-day streak.
    for days_ago, body in (
        (2, "Demo message day 1: hey!"),
        (1, "Demo message day 2: still here."),
        (0, "Demo message day 3: streak alive."),
    ):
        conn.execute(
            "INSERT INTO messages (sender_id, recipient_id, body, is_snip,"
            " expires_at, created_at) VALUES (?, ?, ?, 0, NULL, ?)",
            (amy, bo, body, iso_days_ago(days_ago)),
        )
    # One active Snip from Amy to Bo, expiring 24 hours after it was sent.
    sent = datetime.now(timezone.utc)
    expiry = (sent + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO messages (sender_id, recipient_id, body, is_snip,"
        " expires_at, created_at) VALUES (?, ?, ?, 1, ?, ?)",
        (amy, bo, "Demo Snip: this disappears in 24 hours.",
         expiry, sent.strftime("%Y-%m-%d %H:%M:%S")),
    )

    # Stories: one active from June, one already expired from Amy.
    conn.execute(
        "INSERT INTO stories (user_id, body, expires_at, created_at)"
        " VALUES (?, ?, ?, ?)",
        (june, "Demo story: golden hour shoot today.",
         (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
             "%Y-%m-%d %H:%M:%S"), now),
    )
    conn.execute(
        "INSERT INTO stories (user_id, body, expires_at, created_at)"
        " VALUES (?, ?, ?, ?)",
        (amy, "Demo story: this one already expired.",
         (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
             "%Y-%m-%d %H:%M:%S"), now),
    )

    # Lives: one Pro replay saved 24 hours, one Elite replay kept.
    conn.execute(
        "INSERT INTO lives (user_id, title, retention, saved_until,"
        " created_at) VALUES (?, ?, '24h', ?, ?)",
        (bo, "Demo Live: acoustic set",
         (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
             "%Y-%m-%d %H:%M:%S"), now),
    )
    conn.execute(
        "INSERT INTO lives (user_id, title, retention, saved_until,"
        " created_at) VALUES (?, ?, 'keep', NULL, ?)",
        (june, "Demo Live: photo walk", now),
    )
