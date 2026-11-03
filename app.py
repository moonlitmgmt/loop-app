"""Loop: a social app with a feed, Taps (short videos), stories, Snips
(disappearing messages), Discover Local Artists, linked accounts, Lives
with tier retention rules, and Pro / Elite tiers.

Run:  python app.py   (see README.md for install steps)
Data:  SQLite file loop.db next to this file; uploads live in uploads/.
Payments: PayPal Subscriptions (see paypal.py). When PayPal credentials
and plan ids are configured, the Premium page offers the real PayPal
subscribe/cancel flow; otherwise it says payments are not connected yet
and tier switching stays a clearly labeled demo switch that charges
nothing. No credential is ever stored in code or logged.
"""

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask, g, render_template, request, redirect, url_for, session,
    flash, send_from_directory, abort, jsonify,
)
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename

from db import (
    connect, init_db, hash_password, verify_password, now_iso,
)
from media import parse_mp4_duration
from paypal import (
    PayPalError,
    cancel_subscription,
    create_subscription,
    get_access_token,
    get_subscription,
    paypal_configured,
    paypal_plan_id,
    paypal_ready,
    verify_webhook_signature,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("LOOP_DB", os.path.join(BASE_DIR, "loop.db"))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

app = Flask(__name__)
app.secret_key = os.environ.get("LOOP_SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB uploads
os.makedirs(UPLOAD_DIR, exist_ok=True)
init_db(DB_PATH)

TIER_NAMES = {"free": "Free", "pro": "Pro", "elite": "Elite"}
TIER_PRICES = {"free": "Free", "pro": "$4.99/mo", "elite": "$19.99/mo"}
TAP_LIMITS = {"free": 60, "pro": 180, "elite": 600}
LINK_CAPS = {"free": 0, "pro": 2, "elite": None}
LIVE_OPTIONS = {
    "free": ["not_saved"],
    "pro": ["24h"],
    "elite": ["not_saved", "24h", "7d", "30d", "keep"],
}
LIVE_LABELS = {
    "not_saved": "Not saved",
    "24h": "Saved for 24 hours",
    "7d": "Saved for 7 days",
    "30d": "Saved for 30 days",
    "keep": "Kept until you delete it",
}
LIVE_DELTAS = {
    "not_saved": None,
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "keep": None,
}
CRAFTS = ["Music", "Art", "Dance", "Comedy", "Photography"]
# Phase 2 monetization, ledger only. One clear platform rate applies to
# both creator tips and marketplace sales. It is configurable through
# LOOP_PLATFORM_FEE_RATE (a fraction, e.g. 0.10) and is shown to both
# sides wherever a fee is recorded. No real money moves here.
try:
    PLATFORM_FEE_RATE = float(os.environ.get("LOOP_PLATFORM_FEE_RATE", "0.10"))
except ValueError:
    PLATFORM_FEE_RATE = 0.10
PLATFORM_FEE_RATE = min(max(PLATFORM_FEE_RATE, 0.0), 0.5)
PLATFORM_FEE_PERCENT = int(round(PLATFORM_FEE_RATE * 100))
AD_SLOTS_PER_FEED = 2
AD_SLOTS_PER_DISCOVER = 1
VIDEO_EXTS = {".mp4", ".webm", ".mov"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = connect(DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def cleanup_expired():
    """Drop rows whose expiry has passed. Listings also filter by expiry."""
    db = get_db()
    db.execute("DELETE FROM stories WHERE expires_at <= ?", (now_iso(),))
    db.execute(
        "DELETE FROM messages WHERE is_snip = 1 AND expires_at IS NOT NULL"
        " AND expires_at <= ?",
        (now_iso(),),
    )
    db.commit()


# ---------------------------------------------------------------- phase 1
HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")


@app.template_filter("hashtag_links")
def hashtag_links(text):
    """Escape post text, then turn #tags into links to their tag page."""
    escaped = escape(text or "")

    def link(match):
        tag = match.group(1)
        return Markup('<a href="/tag/%s">#%s</a>') % (tag, tag)

    return Markup(HASHTAG_RE.sub(link, str(escaped)))


def post_engagement(db, post_ids):
    """Return {post_id: {'likes': n, 'comments': n}} for the given posts."""
    out = {pid: {"likes": 0, "comments": 0} for pid in post_ids}
    if not post_ids:
        return out
    marks = ",".join("?" * len(post_ids))
    for row in db.execute(
        "SELECT post_id, COUNT(*) AS n FROM post_likes"
        " WHERE post_id IN (%s) GROUP BY post_id" % marks, post_ids,
    ).fetchall():
        out[row["post_id"]]["likes"] = row["n"]
    for row in db.execute(
        "SELECT post_id, COUNT(*) AS n FROM post_comments"
        " WHERE post_id IN (%s) GROUP BY post_id" % marks, post_ids,
    ).fetchall():
        out[row["post_id"]]["comments"] = row["n"]
    return out


def feed_score(post, engagement):
    """Transparent engagement score: likes count 10, comments 15, a
    view 0.5, minus half a point per hour of age. New posts therefore
    start near the top, liked/commented posts stay up longer, and old
    quiet posts sink. Ties fall back to newest first."""
    try:
        created = datetime.strptime(
            post["created_at"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        age_hours = max(
            0.0,
            (datetime.now(timezone.utc) - created).total_seconds() / 3600.0,
        )
    except (ValueError, TypeError, KeyError):
        age_hours = 0.0
    eng = engagement.get(post["id"], {"likes": 0, "comments": 0})
    views = post["view_count"] if "view_count" in post.keys() else 0
    return (
        eng["likes"] * 10 + eng["comments"] * 15 + (views or 0) * 0.5
        - age_hours * 0.5
    )


def is_admin_user(user):
    if not user or "is_admin" not in user.keys():
        return False
    return bool(user["is_admin"])


def cents_to_dollars(cents):
    return "$%.2f" % ((cents or 0) / 100.0)


def split_amount(amount_cents):
    """Split a ledger amount into (platform_fee, creator_amount) at the
    single published platform rate, rounding the fee to whole cents."""
    fee = int(round(amount_cents * PLATFORM_FEE_RATE))
    return fee, amount_cents - fee


# ---------------------------------------------------------------- auth
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute(
        "SELECT * FROM users WHERE id = ?", (uid,)
    ).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    me = current_user()
    unread = 0
    if me:
        try:
            unread = get_db().execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ?"
                " AND is_read = 0",
                (me["id"],),
            ).fetchone()[0]
        except Exception:
            unread = 0
    return {
        "me": me,
        "is_admin": is_admin_user(me),
        "TIER_NAMES": TIER_NAMES,
        "paypal_ready": paypal_ready(),
        "PLATFORM_FEE_PERCENT": PLATFORM_FEE_PERCENT,
        "unread_count": unread,
        "avatar_url": avatar_url,
        "avatar_initial": avatar_initial,
    }


def avatar_url(user):
    """Uploaded picture URL for a user row, or None for the initials
    avatar. Tolerates rows from databases that predate the avatar
    column (migrate() adds it, but old SELECTs should never crash)."""
    if user is None:
        return None
    try:
        filename = user["avatar_filename"]
    except (KeyError, IndexError, TypeError):
        return None
    if not filename:
        return None
    return url_for("uploaded_file", filename=filename)


def avatar_initial(user):
    """First letter of the display name (or username) for the styled
    initials avatar shown when no picture has been uploaded."""
    if user is None:
        return "?"
    try:
        name = user["display_name"] or user["username"]
    except (KeyError, IndexError, TypeError):
        return "?"
    return (name or "?")[:1].upper()


def is_friend(a, b):
    if a == b:
        return True
    lo, hi = min(a, b), max(a, b)
    row = get_db().execute(
        "SELECT 1 FROM friendships WHERE user_a = ? AND user_b = ?"
        " AND status = 'accepted'",
        (lo, hi),
    ).fetchone()
    return row is not None


def friends_of(uid):
    return get_db().execute(
        "SELECT u.* FROM friendships f JOIN users u ON u.id ="
        " CASE WHEN f.user_a = ? THEN f.user_b ELSE f.user_a END"
        " WHERE (f.user_a = ? OR f.user_b = ?) AND f.status = 'accepted'"
        " ORDER BY u.display_name",
        (uid, uid, uid),
    ).fetchall()


def notify(db, user_id, actor_id, kind, target_type, target_id, body):
    """Create a notification, never for the actor themselves."""
    if not user_id or user_id == actor_id:
        return
    db.execute(
        "INSERT INTO notifications (user_id, actor_id, kind, target_type,"
        " target_id, body, is_read, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (user_id, actor_id, kind, target_type, target_id, body, now_iso()),
    )


def group_ids_of(uid):
    return {
        r["group_id"] for r in get_db().execute(
            "SELECT group_id FROM group_members WHERE user_id = ?",
            (uid,),
        ).fetchall()
    }


def groups_of(uid):
    return get_db().execute(
        "SELECT g.*, u.display_name AS owner_name FROM groups g"
        " JOIN users u ON u.id = g.owner_id"
        " JOIN group_members m ON m.group_id = g.id"
        " WHERE m.user_id = ? ORDER BY g.name",
        (uid,),
    ).fetchall()


def post_visible_to(post, viewer_id, friend_ids, group_ids):
    """Group posts are members-only; friends posts need friendship;
    public posts are open. Hidden posts are handled by the caller."""
    gid = post["group_id"] if "group_id" in post.keys() else None
    if gid:
        return post["user_id"] == viewer_id or gid in group_ids
    if post["audience"] == "public":
        return True
    return post["user_id"] == viewer_id or post["user_id"] in friend_ids


def streak_between(a, b):
    rows = get_db().execute(
        "SELECT DISTINCT substr(created_at, 1, 10) AS d FROM messages"
        " WHERE (sender_id = ? AND recipient_id = ?)"
        " OR (sender_id = ? AND recipient_id = ?)",
        (a, b, b, a),
    ).fetchall()
    dates = {r["d"] for r in rows}
    today = datetime.now(timezone.utc).date()
    if today.isoformat() in dates:
        end = today
    elif (today - timedelta(days=1)).isoformat() in dates:
        end = today - timedelta(days=1)
    else:
        return 0
    count, day = 0, end
    while day.isoformat() in dates:
        count += 1
        day -= timedelta(days=1)
    return count


# ---------------------------------------------------------------- uploads
def save_upload(file_storage, allowed_exts):
    """Save an uploaded file under a random name. Returns filename or None."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in allowed_exts:
        return None
    name = "%s%s" % (uuid.uuid4().hex, ext)
    file_storage.save(os.path.join(UPLOAD_DIR, name))
    return name


def tap_limit_error(duration, tier):
    """Return an error string if duration exceeds the tier limit, else None."""
    limit = TAP_LIMITS[tier]
    if duration <= limit:
        return None
    if tier == "free":
        path = "Upgrade to Pro (up to 3 minutes) or Elite (up to 10 minutes)."
    elif tier == "pro":
        path = "Upgrade to Elite (up to 10 minutes)."
    else:
        path = "Elite allows Taps up to 10 minutes."
    return (
        "That video is %.0f seconds, but your %s plan allows Taps up to"
        " %d seconds. %s" % (duration, TIER_NAMES[tier], limit, path)
    )


def video_duration_for_upload(path, form_value):
    """Best duration we can establish: parsed file value wins, else the
    duration the browser read from the file before upload."""
    parsed = parse_mp4_duration(path) if path.endswith(".mp4") else None
    if parsed is not None:
        return parsed
    try:
        value = float(form_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------- index/auth
@app.route("/")
def index():
    if current_user():
        return redirect(url_for("feed"))
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        display = request.form.get("display_name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        error = None
        if not username or not display or not email or not password:
            error = "All fields are required."
        elif db.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone():
            error = "That username is taken."
        elif db.execute(
            "SELECT 1 FROM users WHERE email = ?", (email,)
        ).fetchone():
            error = "That email is already registered."
        if error:
            flash(error, "error")
        else:
            cur = db.execute(
                "INSERT INTO users (username, display_name, email,"
                " password_hash, tier, created_at) VALUES (?, ?, ?, ?,"
                " 'free', ?)",
                (username, display, email, hash_password(password), now_iso()),
            )
            db.commit()
            session["user_id"] = cur.lastrowid
            flash("Welcome to Loop! Your account is on the Free plan.", "ok")
            return redirect(url_for("feed"))
    return render_template("auth.html", mode="signup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if user and verify_password(password, user["password_hash"]):
            session["user_id"] = user["id"]
            flash("Signed in as %s." % user["display_name"], "ok")
            return redirect(url_for("feed"))
        flash("Wrong username or password.", "error")
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    flash("Signed out.", "ok")
    return redirect(url_for("login"))


# ---------------------------------------------------------------- feed
@app.route("/feed", methods=["GET", "POST"])
@login_required
def feed():
    me = current_user()
    db = get_db()
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        audience = request.form.get("audience", "public")
        if audience not in ("public", "friends"):
            audience = "public"
        group_id = request.form.get("group_id", "").strip()
        if group_id.isdigit() and int(group_id) in group_ids_of(me["id"]):
            audience = "group"
            group_id = int(group_id)
        else:
            group_id = None
        if not body:
            flash("Write something first.", "error")
        else:
            db.execute(
                "INSERT INTO posts (user_id, body, audience, group_id,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (me["id"], body, audience, group_id, now_iso()),
            )
            db.commit()
            if group_id:
                members = db.execute(
                    "SELECT user_id FROM group_members WHERE group_id = ?",
                    (group_id,),
                ).fetchall()
                group = db.execute(
                    "SELECT name FROM groups WHERE id = ?", (group_id,)
                ).fetchone()
                for m in members:
                    notify(
                        db, m["user_id"], me["id"], "group", "group",
                        group_id,
                        "%s posted in %s." % (
                            me["display_name"],
                            group["name"] if group else "a group"),
                    )
                db.commit()
            flash("Posted.", "ok")
        return redirect(url_for("feed"))
    friend_ids = {f["id"] for f in friends_of(me["id"])}
    group_ids = group_ids_of(me["id"])
    rows = db.execute(
        "SELECT p.*, u.username, u.display_name, u.avatar_filename"
        " FROM posts p"
        " JOIN users u ON u.id = p.user_id"
        " WHERE p.hidden = 0"
        " ORDER BY p.created_at DESC, p.id DESC LIMIT 200"
    ).fetchall()
    visible = [
        p for p in rows
        if post_visible_to(p, me["id"], friend_ids, group_ids)
    ]
    # Simple view counter: one view per visible post per feed load.
    # It is a popularity signal, not a unique-viewer count.
    for p in visible:
        db.execute(
            "UPDATE posts SET view_count = view_count + 1 WHERE id = ?",
            (p["id"],),
        )
    if visible:
        db.commit()
        rows = db.execute(
            "SELECT p.*, u.username, u.display_name, u.avatar_filename"
            " FROM posts p"
            " JOIN users u ON u.id = p.user_id"
            " WHERE p.id IN (%s)"
            % ",".join("?" * len(visible)),
            [p["id"] for p in visible],
        ).fetchall()
        by_id = {p["id"]: p for p in rows}
        visible = [by_id[p["id"]] for p in visible if p["id"] in by_id]
    engagement = post_engagement(db, [p["id"] for p in visible])
    liked = {
        r["post_id"] for r in db.execute(
            "SELECT post_id FROM post_likes WHERE user_id = ?",
            (me["id"],),
        ).fetchall()
    }
    sort = request.args.get("sort", "ranked")
    if sort == "latest":
        ordered = visible
    else:
        # Ranked: engagement + recency. Ties fall back to newest first.
        ordered = sorted(
            visible,
            key=lambda p: (feed_score(p, engagement), p["created_at"]),
            reverse=True,
        )
        sort = "ranked"
    comments = {}
    for p in ordered:
        comments[p["id"]] = db.execute(
            "SELECT c.*, u.username, u.display_name FROM post_comments c"
            " JOIN users u ON u.id = c.user_id WHERE c.post_id = ?"
            " ORDER BY c.created_at ASC, c.id ASC",
            (p["id"],),
        ).fetchall()
    saved = {
        r["post_id"] for r in db.execute(
            "SELECT post_id FROM saved_posts WHERE user_id = ?",
            (me["id"],),
        ).fetchall()
    }
    return render_template(
        "feed.html", posts=ordered, engagement=engagement, liked=liked,
        comments=comments, sort=sort, my_groups=groups_of(me["id"]),
        saved=saved,
    )


@app.route("/posts/<int:post_id>/like", methods=["POST"])
@login_required
def post_like(post_id):
    me = current_user()
    db = get_db()
    post = db.execute(
        "SELECT * FROM posts WHERE id = ? AND hidden = 0", (post_id,)
    ).fetchone()
    if not post:
        abort(404)
    existing = db.execute(
        "SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?",
        (post_id, me["id"]),
    ).fetchone()
    if existing:
        db.execute(
            "DELETE FROM post_likes WHERE post_id = ? AND user_id = ?",
            (post_id, me["id"]),
        )
    else:
        db.execute(
            "INSERT INTO post_likes (post_id, user_id, created_at)"
            " VALUES (?, ?, ?)",
            (post_id, me["id"], now_iso()),
        )
        notify(
            db, post["user_id"], me["id"], "like", "post", post_id,
            "%s liked your post." % me["display_name"],
        )
    db.commit()
    return redirect(url_for("feed"))


@app.route("/posts/<int:post_id>/comment", methods=["POST"])
@login_required
def post_comment(post_id):
    me = current_user()
    db = get_db()
    post = db.execute(
        "SELECT * FROM posts WHERE id = ? AND hidden = 0", (post_id,)
    ).fetchone()
    if not post:
        abort(404)
    body = request.form.get("body", "").strip()
    if not body:
        flash("Write a comment first.", "error")
    else:
        db.execute(
            "INSERT INTO post_comments (post_id, user_id, body, created_at)"
            " VALUES (?, ?, ?, ?)",
            (post_id, me["id"], body, now_iso()),
        )
        notify(
            db, post["user_id"], me["id"], "comment", "post", post_id,
            "%s commented on your post." % me["display_name"],
        )
        db.commit()
    return redirect(url_for("feed"))


@app.route("/tag/<tag>")
@login_required
def tag_page(tag):
    me = current_user()
    db = get_db()
    tag = re.sub(r"[^A-Za-z0-9_]", "", tag)[:50]
    if not tag:
        abort(404)
    friend_ids = {f["id"] for f in friends_of(me["id"])}
    group_ids = group_ids_of(me["id"])
    rows = db.execute(
        "SELECT p.*, u.username, u.display_name FROM posts p"
        " JOIN users u ON u.id = p.user_id"
        " WHERE p.hidden = 0 AND p.body LIKE ?"
        " ORDER BY p.created_at DESC, p.id DESC LIMIT 200",
        ("%" + "#" + tag + "%",),
    ).fetchall()
    visible = [
        p for p in rows
        if post_visible_to(p, me["id"], friend_ids, group_ids)
    ]
    engagement = post_engagement(db, [p["id"] for p in visible])
    return render_template(
        "tag.html", tag=tag, posts=visible, engagement=engagement,
    )


# ---------------------------------------------------------------- search
@app.route("/search")
@login_required
def search():
    me = current_user()
    db = get_db()
    q = request.args.get("q", "").strip()[:100]
    users = artists = posts = []
    if q:
        like = "%" + q + "%"
        users = db.execute(
            "SELECT * FROM users WHERE username LIKE ? OR display_name"
            " LIKE ? OR bio LIKE ? ORDER BY display_name LIMIT 50",
            (like, like, like),
        ).fetchall()
        artists = db.execute(
            "SELECT ap.*, u.username, u.display_name FROM artist_pages ap"
            " JOIN users u ON u.id = ap.user_id"
            " WHERE ap.hidden = 0 AND (ap.artist_name LIKE ?"
            " OR ap.artist_handle LIKE ? OR ap.story_bio LIKE ?)"
            " ORDER BY ap.artist_name LIMIT 50",
            (like, like, like),
        ).fetchall()
        friend_ids = {f["id"] for f in friends_of(me["id"])}
        rows = db.execute(
            "SELECT p.*, u.username, u.display_name FROM posts p"
            " JOIN users u ON u.id = p.user_id"
            " WHERE p.hidden = 0 AND p.body LIKE ?"
            " ORDER BY p.created_at DESC, p.id DESC LIMIT 100",
            (like,),
        ).fetchall()
        posts = [
            p for p in rows
            if p["audience"] == "public"
            or p["user_id"] == me["id"]
            or p["user_id"] in friend_ids
        ]
    return render_template(
        "search.html", q=q, users=users, artists=artists, posts=posts,
    )


# ---------------------------------------------------------------- reports
@app.route("/report", methods=["POST"])
@login_required
def report():
    me = current_user()
    db = get_db()
    target_type = request.form.get("target_type", "")
    target_id = request.form.get("target_id", "")
    reason = request.form.get("reason", "").strip()[:500]
    if target_type not in ("post", "artist") or not target_id.isdigit():
        flash("That report could not be filed.", "error")
        return redirect(url_for("feed"))
    target_id = int(target_id)
    exists = None
    if target_type == "post":
        exists = db.execute(
            "SELECT 1 FROM posts WHERE id = ?", (target_id,)
        ).fetchone()
    else:
        exists = db.execute(
            "SELECT 1 FROM artist_pages WHERE id = ?", (target_id,)
        ).fetchone()
    if not exists:
        abort(404)
    dup = db.execute(
        "SELECT 1 FROM reports WHERE reporter_id = ? AND target_type = ?"
        " AND target_id = ? AND status = 'open'",
        (me["id"], target_type, target_id),
    ).fetchone()
    if dup:
        flash("You already have an open report for this item.", "error")
    else:
        db.execute(
            "INSERT INTO reports (reporter_id, target_type, target_id,"
            " reason, status, created_at) VALUES (?, ?, ?, ?, 'open', ?)",
            (me["id"], target_type, target_id, reason, now_iso()),
        )
        db.commit()
        flash("Reported. It is now in the moderation queue.", "ok")
    if target_type == "artist":
        return redirect(url_for("artist_view", page_id=target_id))
    return redirect(url_for("feed"))


@app.route("/moderation", methods=["GET", "POST"])
@login_required
def moderation():
    me = current_user()
    if not is_admin_user(me):
        abort(403)
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "")
        report_id = request.form.get("report_id", "")
        report = db.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone() if report_id.isdigit() else None
        if not report:
            abort(404)
        if action == "hide":
            if report["target_type"] == "post":
                db.execute(
                    "UPDATE posts SET hidden = 1 WHERE id = ?",
                    (report["target_id"],),
                )
            else:
                db.execute(
                    "UPDATE artist_pages SET hidden = 1 WHERE id = ?",
                    (report["target_id"],),
                )
        elif action == "unhide":
            if report["target_type"] == "post":
                db.execute(
                    "UPDATE posts SET hidden = 0 WHERE id = ?",
                    (report["target_id"],),
                )
            else:
                db.execute(
                    "UPDATE artist_pages SET hidden = 0 WHERE id = ?",
                    (report["target_id"],),
                )
        elif action in ("dismiss", "resolve"):
            pass
        else:
            flash("Unknown moderation action.", "error")
            return redirect(url_for("moderation"))
        db.execute(
            "UPDATE reports SET status = 'resolved', resolved_at = ?"
            " WHERE id = ?",
            (now_iso(), report["id"]),
        )
        db.commit()
        flash("Moderation action saved.", "ok")
        return redirect(url_for("moderation"))
    open_reports = db.execute(
        "SELECT r.*, u.username, u.display_name FROM reports r"
        " JOIN users u ON u.id = r.reporter_id"
        " WHERE r.status = 'open' ORDER BY r.created_at ASC, r.id ASC"
    ).fetchall()
    resolved = db.execute(
        "SELECT r.*, u.username, u.display_name FROM reports r"
        " JOIN users u ON u.id = r.reporter_id"
        " WHERE r.status = 'resolved' ORDER BY r.resolved_at DESC,"
        " r.id DESC LIMIT 50"
    ).fetchall()
    previews = {}
    for r in list(open_reports) + list(resolved):
        if r["target_type"] == "post":
            row = db.execute(
                "SELECT body, hidden FROM posts WHERE id = ?",
                (r["target_id"],),
            ).fetchone()
            previews[r["id"]] = (
                ("Post: " + row["body"][:200]) if row else "Post not found",
                bool(row["hidden"]) if row else False,
            )
        else:
            row = db.execute(
                "SELECT artist_name, hidden FROM artist_pages WHERE id = ?",
                (r["target_id"],),
            ).fetchone()
            previews[r["id"]] = (
                ("Artist page: " + row["artist_name"]) if row
                else "Artist page not found",
                bool(row["hidden"]) if row else False,
            )
    return render_template(
        "moderation.html", open_reports=open_reports, resolved=resolved,
        previews=previews,
    )


# ---------------------------------------------------------------- profiles
@app.route("/profile", methods=["GET", "POST"])
@login_required
def my_profile():
    return redirect(url_for("profile_view", username=current_user()["username"]))


@app.route("/profile/<username>", methods=["GET", "POST"])
@login_required
def profile_view(username):
    me = current_user()
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if not user:
        abort(404)
    if request.method == "POST" and user["id"] == me["id"]:
        # Profile pictures are free for all tiers. Only the owner can
        # change their own picture; posts from anyone else hit this
        # branch only when user["id"] == me["id"], so a stranger's
        # form post is ignored and falls through to the normal view.
        try:
            old_avatar = user["avatar_filename"]
        except (KeyError, IndexError, TypeError):
            old_avatar = None
        if request.form.get("remove_avatar"):
            if old_avatar:
                try:
                    os.remove(os.path.join(UPLOAD_DIR, old_avatar))
                except OSError:
                    pass
            db.execute(
                "UPDATE users SET avatar_filename = NULL WHERE id = ?",
                (me["id"],),
            )
            db.commit()
            flash("Profile picture removed.", "ok")
            return redirect(url_for("profile_view", username=username))
        avatar_file = request.files.get("avatar")
        if avatar_file and avatar_file.filename:
            filename = save_upload(avatar_file, IMAGE_EXTS)
            if filename is None:
                flash(
                    "That is not an accepted picture. Use a PNG, JPG,"
                    " JPEG, GIF, or WebP image.",
                    "error",
                )
            else:
                if old_avatar:
                    try:
                        os.remove(os.path.join(UPLOAD_DIR, old_avatar))
                    except OSError:
                        pass
                db.execute(
                    "UPDATE users SET avatar_filename = ? WHERE id = ?",
                    (filename, me["id"]),
                )
                db.commit()
                flash("Profile picture saved.", "ok")
            return redirect(url_for("profile_view", username=username))
        bio = request.form.get("bio", "").strip()
        db.execute("UPDATE users SET bio = ? WHERE id = ?", (bio, me["id"]))
        db.commit()
        flash("Bio saved.", "ok")
        return redirect(url_for("profile_view", username=username))
    can_see_personal = user["id"] == me["id"] or is_friend(me["id"], user["id"])
    personal = []
    if can_see_personal:
        personal = db.execute(
            "SELECT * FROM posts WHERE user_id = ? AND audience = 'friends'"
            " ORDER BY created_at DESC, id DESC",
            (user["id"],),
        ).fetchall()
    artists = db.execute(
        "SELECT * FROM artist_pages WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    links = db.execute(
        "SELECT * FROM linked_accounts WHERE user_id = ?"
        " ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    # Activity summary. Loop has no directed follow system — its
    # connection graph is friendships — so the connection count is the
    # friend count, labelled as such rather than invented followers.
    stats = {
        "friends": len(friends_of(user["id"])),
        "posts": db.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id = ? AND hidden = 0",
            (user["id"],),
        ).fetchone()[0],
        "taps": db.execute(
            "SELECT COUNT(*) FROM taps WHERE user_id = ?", (user["id"],)
        ).fetchone()[0],
        "artist_pages": len(artists),
        "groups": db.execute(
            "SELECT COUNT(*) FROM group_members WHERE user_id = ?",
            (user["id"],),
        ).fetchone()[0],
        "likes_received": db.execute(
            "SELECT COUNT(*) FROM post_likes l JOIN posts p"
            " ON p.id = l.post_id WHERE p.user_id = ?",
            (user["id"],),
        ).fetchone()[0],
        "comments_received": db.execute(
            "SELECT COUNT(*) FROM post_comments c JOIN posts p"
            " ON p.id = c.post_id WHERE p.user_id = ?",
            (user["id"],),
        ).fetchone()[0],
    }
    recent = db.execute(
        "SELECT 'post' AS kind, body AS title, created_at FROM posts"
        " WHERE user_id = ? AND hidden = 0 AND audience != 'group'"
        " UNION ALL SELECT 'tap', title, created_at FROM taps"
        " WHERE user_id = ? ORDER BY created_at DESC, title ASC LIMIT 5",
        (user["id"], user["id"]),
    ).fetchall()
    return render_template(
        "profile.html", user=user, personal=personal,
        can_see_personal=can_see_personal, artists=artists, links=links,
        LINK_CAPS=LINK_CAPS, stats=stats, recent=recent,
    )


# ---------------------------------------------------------------- friends
@app.route("/friends", methods=["GET", "POST"])
@login_required
def friends():
    me = current_user()
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "send":
            other = db.execute(
                "SELECT * FROM users WHERE username = ?",
                (request.form.get("username", "").strip(),),
            ).fetchone()
            if not other:
                flash("No user with that username.", "error")
            elif other["id"] == me["id"]:
                flash("That is you.", "error")
            else:
                lo, hi = min(me["id"], other["id"]), max(me["id"], other["id"])
                exists = db.execute(
                    "SELECT 1 FROM friendships WHERE user_a = ? AND user_b = ?",
                    (lo, hi),
                ).fetchone()
                if exists:
                    flash("You already have a request or friendship there.", "error")
                else:
                    db.execute(
                        "INSERT INTO friendships (user_a, user_b, status,"
                        " requester_id, created_at) VALUES (?, ?, 'pending',"
                        " ?, ?)",
                        (lo, hi, me["id"], now_iso()),
                    )
                    notify(
                        db, other["id"], me["id"], "friend_request",
                        "user", me["id"],
                        "%s sent you a friend request."
                        % me["display_name"],
                    )
                    db.commit()
                    flash("Friend request sent.", "ok")
        elif action == "accept":
            fid = request.form.get("fid", "")
            row = db.execute(
                "SELECT * FROM friendships WHERE id = ?", (fid,)
            ).fetchone()
            if row and row["status"] == "pending" and row["requester_id"] != me["id"] \
                    and me["id"] in (row["user_a"], row["user_b"]):
                db.execute(
                    "UPDATE friendships SET status = 'accepted' WHERE id = ?",
                    (fid,),
                )
                notify(
                    db, row["requester_id"], me["id"], "friend_accept",
                    "user", me["id"],
                    "%s accepted your friend request."
                    % me["display_name"],
                )
                db.commit()
                flash("Friend request accepted.", "ok")
        elif action == "remove":
            friend_id = request.form.get("friend_id", "")
            if friend_id:
                lo, hi = min(me["id"], int(friend_id)), max(me["id"], int(friend_id))
                db.execute(
                    "DELETE FROM friendships WHERE user_a = ? AND user_b = ?",
                    (lo, hi),
                )
                db.commit()
                flash("Removed.", "ok")
        return redirect(url_for("friends"))
    my_friends = friends_of(me["id"])
    incoming = db.execute(
        "SELECT f.*, u.username, u.display_name FROM friendships f"
        " JOIN users u ON u.id = f.requester_id"
        " WHERE (f.user_a = ? OR f.user_b = ?) AND f.status = 'pending'"
        " AND f.requester_id != ?",
        (me["id"], me["id"], me["id"]),
    ).fetchall()
    outgoing = db.execute(
        "SELECT f.*, u.username, u.display_name FROM friendships f"
        " JOIN users u ON u.id = CASE WHEN f.user_a = ? THEN f.user_b"
        " ELSE f.user_a END"
        " WHERE (f.user_a = ? OR f.user_b = ?) AND f.status = 'pending'"
        " AND f.requester_id = ?",
        (me["id"], me["id"], me["id"], me["id"]),
    ).fetchall()
    return render_template(
        "friends.html", friends=my_friends, incoming=incoming,
        outgoing=outgoing,
    )


# ---------------------------------------------------------------- phase 3
@app.route("/groups", methods=["GET", "POST"])
@login_required
def groups():
    me = current_user()
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:100]
        description = request.form.get("description", "").strip()[:500]
        if not name:
            flash("Give the group a name.", "error")
        else:
            try:
                cur = db.execute(
                    "INSERT INTO groups (name, description, owner_id,"
                    " created_at) VALUES (?, ?, ?, ?)",
                    (name, description, me["id"], now_iso()),
                )
                db.execute(
                    "INSERT INTO group_members (group_id, user_id,"
                    " created_at) VALUES (?, ?, ?)",
                    (cur.lastrowid, me["id"], now_iso()),
                )
                db.commit()
                flash("Group created. You are its first member.", "ok")
                return redirect(url_for("group_view", group_id=cur.lastrowid))
            except Exception:
                db.rollback()
                flash("A group with that name already exists.", "error")
        return redirect(url_for("groups"))
    rows = db.execute(
        "SELECT g.*, u.display_name AS owner_name,"
        " (SELECT COUNT(*) FROM group_members m WHERE m.group_id = g.id)"
        " AS member_count FROM groups g JOIN users u ON u.id = g.owner_id"
        " ORDER BY g.name"
    ).fetchall()
    mine = group_ids_of(me["id"])
    return render_template("groups.html", groups=rows, mine=mine)


@app.route("/groups/<int:group_id>", methods=["GET", "POST"])
@login_required
def group_view(group_id):
    me = current_user()
    db = get_db()
    group = db.execute(
        "SELECT g.*, u.display_name AS owner_name FROM groups g"
        " JOIN users u ON u.id = g.owner_id WHERE g.id = ?",
        (group_id,),
    ).fetchone()
    if not group:
        abort(404)
    members = db.execute(
        "SELECT u.* FROM group_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.group_id = ? ORDER BY u.display_name",
        (group_id,),
    ).fetchall()
    is_member = any(m["id"] == me["id"] for m in members)
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "join" and not is_member:
            db.execute(
                "INSERT INTO group_members (group_id, user_id, created_at)"
                " VALUES (?, ?, ?)",
                (group_id, me["id"], now_iso()),
            )
            notify(
                db, group["owner_id"], me["id"], "group_join", "group",
                group_id,
                "%s joined your group %s."
                % (me["display_name"], group["name"]),
            )
            db.commit()
            flash("Joined %s." % group["name"], "ok")
        elif action == "leave" and is_member:
            db.execute(
                "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
                (group_id, me["id"]),
            )
            db.commit()
            flash("Left %s." % group["name"], "ok")
        elif action == "post":
            if not is_member:
                flash("Join the group before posting in it.", "error")
            else:
                body = request.form.get("body", "").strip()
                if not body:
                    flash("Write something first.", "error")
                else:
                    db.execute(
                        "INSERT INTO posts (user_id, body, audience,"
                        " group_id, created_at) VALUES (?, ?, 'group', ?, ?)",
                        (me["id"], body, group_id, now_iso()),
                    )
                    for m in members:
                        notify(
                            db, m["id"], me["id"], "group", "group",
                            group_id,
                            "%s posted in %s."
                            % (me["display_name"], group["name"]),
                        )
                    db.commit()
                    flash("Posted to the group.", "ok")
        return redirect(url_for("group_view", group_id=group_id))
    posts = []
    if is_member:
        posts = db.execute(
            "SELECT p.*, u.username, u.display_name FROM posts p"
            " JOIN users u ON u.id = p.user_id"
            " WHERE p.group_id = ? AND p.hidden = 0"
            " ORDER BY p.created_at DESC, p.id DESC",
            (group_id,),
        ).fetchall()
    engagement = post_engagement(db, [p["id"] for p in posts])
    return render_template(
        "group_view.html", group=group, members=members,
        is_member=is_member, posts=posts, engagement=engagement,
    )


@app.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications():
    me = current_user()
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "mark_all":
            db.execute(
                "UPDATE notifications SET is_read = 1 WHERE user_id = ?",
                (me["id"],),
            )
            db.commit()
            flash("All notifications marked as read.", "ok")
        elif action == "mark_one":
            nid = request.form.get("notification_id", "")
            if nid.isdigit():
                db.execute(
                    "UPDATE notifications SET is_read = 1 WHERE id = ?"
                    " AND user_id = ?",
                    (int(nid), me["id"]),
                )
                db.commit()
        return redirect(url_for("notifications"))
    rows = db.execute(
        "SELECT n.*, u.display_name AS actor_name FROM notifications n"
        " LEFT JOIN users u ON u.id = n.actor_id WHERE n.user_id = ?"
        " ORDER BY n.created_at DESC, n.id DESC LIMIT 100",
        (me["id"],),
    ).fetchall()
    return render_template("notifications.html", notifications=rows)


@app.route("/posts/<int:post_id>/save", methods=["POST"])
@login_required
def post_save(post_id):
    me = current_user()
    db = get_db()
    post = db.execute(
        "SELECT * FROM posts WHERE id = ? AND hidden = 0", (post_id,)
    ).fetchone()
    if not post:
        abort(404)
    existing = db.execute(
        "SELECT 1 FROM saved_posts WHERE post_id = ? AND user_id = ?",
        (post_id, me["id"]),
    ).fetchone()
    if existing:
        db.execute(
            "DELETE FROM saved_posts WHERE post_id = ? AND user_id = ?",
            (post_id, me["id"]),
        )
        flash("Removed from saved posts.", "ok")
    else:
        db.execute(
            "INSERT INTO saved_posts (post_id, user_id, created_at)"
            " VALUES (?, ?, ?)",
            (post_id, me["id"], now_iso()),
        )
        flash("Saved.", "ok")
    db.commit()
    return redirect(request.form.get("next") or url_for("feed"))


@app.route("/saved")
@login_required
def saved():
    me = current_user()
    db = get_db()
    friend_ids = {f["id"] for f in friends_of(me["id"])}
    group_ids = group_ids_of(me["id"])
    rows = db.execute(
        "SELECT p.*, u.username, u.display_name FROM saved_posts s"
        " JOIN posts p ON p.id = s.post_id"
        " JOIN users u ON u.id = p.user_id"
        " WHERE s.user_id = ? AND p.hidden = 0"
        " ORDER BY s.created_at DESC, s.id DESC",
        (me["id"],),
    ).fetchall()
    visible = [
        p for p in rows
        if post_visible_to(p, me["id"], friend_ids, group_ids)
    ]
    engagement = post_engagement(db, [p["id"] for p in visible])
    return render_template(
        "saved.html", posts=visible, engagement=engagement,
    )


# ---------------------------------------------------------------- taps
@app.route("/taps", methods=["GET", "POST"])
@login_required
def taps():
    me = current_user()
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip() or "Untitled Tap"
        file = request.files.get("video")
        form_duration = request.form.get("duration_seconds", "")
        filename = save_upload(file, VIDEO_EXTS)
        if not filename:
            flash("Choose a video file (mp4, webm, or mov).", "error")
            return redirect(url_for("taps"))
        path = os.path.join(UPLOAD_DIR, filename)
        duration = video_duration_for_upload(path, form_duration)
        if duration is None:
            os.remove(path)
            flash("Could not read this video's length. Try another file.",
                  "error")
            return redirect(url_for("taps"))
        error = tap_limit_error(duration, me["tier"])
        if error:
            os.remove(path)
            flash(error, "error")
            return redirect(url_for("taps"))
        db.execute(
            "INSERT INTO taps (user_id, title, filename, duration_seconds,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (me["id"], title, filename, duration, now_iso()),
        )
        db.commit()
        flash("Tap posted (%.0f seconds)." % duration, "ok")
        return redirect(url_for("taps"))
    rows = db.execute(
        "SELECT t.*, u.username, u.display_name FROM taps t"
        " JOIN users u ON u.id = t.user_id"
        " ORDER BY t.created_at DESC, t.id DESC"
    ).fetchall()
    return render_template(
        "taps.html", taps=rows, limit=TAP_LIMITS[me["tier"]],
    )


@app.route("/uploads/<filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------------------------------------------------------------- stories
@app.route("/stories", methods=["GET", "POST"])
@login_required
def stories():
    me = current_user()
    db = get_db()
    cleanup_expired()
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        media = save_upload(request.files.get("image"), IMAGE_EXTS)
        if not body and not media:
            flash("Write something or add a photo.", "error")
        else:
            expires = (datetime.now(timezone.utc) + timedelta(hours=24))
            db.execute(
                "INSERT INTO stories (user_id, body, media_filename,"
                " expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (me["id"], body, media,
                 expires.strftime("%Y-%m-%d %H:%M:%S"), now_iso()),
            )
            db.commit()
            flash("Story posted. It disappears in 24 hours.", "ok")
        return redirect(url_for("stories"))
    friend_ids = {f["id"] for f in friends_of(me["id"])} | {me["id"]}
    rows = db.execute(
        "SELECT s.*, u.username, u.display_name FROM stories s"
        " JOIN users u ON u.id = s.user_id"
        " WHERE s.expires_at > ? ORDER BY s.created_at DESC, s.id DESC",
        (now_iso(),),
    ).fetchall()
    visible = [s for s in rows if s["user_id"] in friend_ids]
    return render_template("stories.html", stories=visible)


# ---------------------------------------------------------------- messages
@app.route("/messages")
@login_required
def messages():
    me = current_user()
    cleanup_expired()
    db = get_db()
    convos = []
    for friend in friends_of(me["id"]):
        latest = db.execute(
            "SELECT * FROM messages WHERE ((sender_id = ? AND recipient_id = ?)"
            " OR (sender_id = ? AND recipient_id = ?))"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (me["id"], friend["id"], friend["id"], me["id"], now_iso()),
        ).fetchone()
        convos.append({
            "friend": friend,
            "latest": latest,
            "streak": streak_between(me["id"], friend["id"]),
        })
    convos.sort(
        key=lambda c: (c["latest"]["created_at"] if c["latest"] else ""),
        reverse=True,
    )
    return render_template("messages.html", convos=convos)


@app.route("/messages/<username>", methods=["GET", "POST"])
@login_required
def thread(username):
    me = current_user()
    db = get_db()
    other = db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if not other:
        abort(404)
    if request.method == "POST":
        if not is_friend(me["id"], other["id"]):
            flash("Add them as a friend before messaging.", "error")
            return redirect(url_for("thread", username=username))
        body = request.form.get("body", "").strip()
        is_snip = 1 if request.form.get("is_snip") else 0
        if not body:
            flash("Write something first.", "error")
        else:
            expires = None
            if is_snip:
                expires = (datetime.now(timezone.utc)
                           + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                "INSERT INTO messages (sender_id, recipient_id, body, is_snip,"
                " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (me["id"], other["id"], body, is_snip, expires, now_iso()),
            )
            db.commit()
            flash("Snip sent. It disappears in 24 hours." if is_snip
                  else "Message sent.", "ok")
        return redirect(url_for("thread", username=username))
    cleanup_expired()
    rows = db.execute(
        "SELECT * FROM messages WHERE ((sender_id = ? AND recipient_id = ?)"
        " OR (sender_id = ? AND recipient_id = ?))"
        " AND (expires_at IS NULL OR expires_at > ?)"
        " ORDER BY created_at ASC, id ASC",
        (me["id"], other["id"], other["id"], me["id"], now_iso()),
    ).fetchall()
    return render_template(
        "thread.html", other=other, messages=rows,
        streak=streak_between(me["id"], other["id"]),
    )


@app.route("/messages/<username>/updates")
@login_required
def thread_updates(username):
    """JSON feed for thread polling: messages after a given id, in the
    same full-page thread view. No new realtime service is involved."""
    me = current_user()
    db = get_db()
    other = db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if not other:
        abort(404)
    try:
        after = int(request.args.get("after", "0"))
    except ValueError:
        after = 0
    cleanup_expired()
    rows = db.execute(
        "SELECT * FROM messages WHERE id > ? AND ((sender_id = ?"
        " AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?))"
        " AND (expires_at IS NULL OR expires_at > ?)"
        " ORDER BY created_at ASC, id ASC",
        (after, me["id"], other["id"], other["id"], me["id"], now_iso()),
    ).fetchall()
    return jsonify({
        "messages": [
            {
                "id": m["id"],
                "sender": "You" if m["sender_id"] == me["id"]
                else other["display_name"],
                "body": m["body"],
                "created_at": m["created_at"],
                "is_snip": bool(m["is_snip"]),
                "expires_at": m["expires_at"],
            }
            for m in rows
        ],
    })


# ---------------------------------------------------------------- discover
@app.route("/discover")
@login_required
def discover():
    craft = request.args.get("craft", "all")
    scope = request.args.get("scope", "everyone")
    if craft not in CRAFTS:
        craft = "all"
    if scope not in ("everyone", "near", "worldwide"):
        scope = "everyone"
    query = (
        "SELECT ap.*, u.username, u.display_name FROM artist_pages ap"
        " JOIN users u ON u.id = ap.user_id WHERE ap.hidden = 0"
    )
    params = []
    if craft != "all":
        query += " AND ap.craft = ?"
        params.append(craft)
    if scope == "near":
        query += " AND ap.scope = 'local'"
    elif scope == "worldwide":
        query += " AND ap.scope = 'worldwide'"
    query += " ORDER BY ap.created_at DESC, ap.id DESC"
    artists = get_db().execute(query, params).fetchall()
    return render_template(
        "discover.html", artists=artists, crafts=CRAFTS,
        craft=craft, scope=scope,
    )


# ---------------------------------------------------------------- artist pages
@app.route("/artists/new", methods=["GET", "POST"])
@login_required
def artist_new():
    me = current_user()
    if request.method == "POST":
        name = request.form.get("artist_name", "").strip()
        handle = request.form.get("artist_handle", "").strip()
        craft = request.form.get("craft", "")
        scope = request.form.get("scope", "local")
        hometown = request.form.get("hometown", "").strip()
        story = request.form.get("story_bio", "").strip()
        reach = request.form.get("reach_text", "").strip()
        if not name or not handle or craft not in CRAFTS \
                or scope not in ("local", "worldwide"):
            flash("Artist name, handle, craft, and scope are required.",
                  "error")
        else:
            db = get_db()
            cur = db.execute(
                "INSERT INTO artist_pages (user_id, artist_name,"
                " artist_handle, craft, scope, hometown, story_bio,"
                " reach_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (me["id"], name, handle, craft, scope, hometown, story,
                 reach, now_iso()),
            )
            db.commit()
            flash("Artist page created.", "ok")
            return redirect(url_for("artist_view", page_id=cur.lastrowid))
    return render_template("artist_form.html", crafts=CRAFTS)


@app.route("/artists/<int:page_id>", methods=["GET", "POST"])
@login_required
def artist_view(page_id):
    me = current_user()
    db = get_db()
    page = db.execute(
        "SELECT ap.*, u.username, u.display_name FROM artist_pages ap"
        " JOIN users u ON u.id = ap.user_id WHERE ap.id = ?",
        (page_id,),
    ).fetchone()
    if not page:
        abort(404)
    if request.method == "POST":
        if page["user_id"] != me["id"]:
            flash("Only the page owner can add showcase items.", "error")
            return redirect(url_for("artist_view", page_id=page_id))
        title = request.form.get("title", "").strip()
        media = save_upload(request.files.get("media"), VIDEO_EXTS | IMAGE_EXTS)
        media_type = "text"
        if media:
            ext = os.path.splitext(media)[1].lower()
            media_type = "video" if ext in VIDEO_EXTS else "image"
            if media_type == "video":
                duration = video_duration_for_upload(
                    os.path.join(UPLOAD_DIR, media),
                    request.form.get("duration_seconds", ""),
                )
                if duration is None:
                    os.remove(os.path.join(UPLOAD_DIR, media))
                    flash("Could not read that video's length.", "error")
                    return redirect(url_for("artist_view", page_id=page_id))
                error = tap_limit_error(duration, me["tier"])
                if error:
                    os.remove(os.path.join(UPLOAD_DIR, media))
                    flash(error, "error")
                    return redirect(url_for("artist_view", page_id=page_id))
        if not title and not media:
            flash("Give the showcase item a title or a file.", "error")
        else:
            db.execute(
                "INSERT INTO showcase_items (artist_page_id, title,"
                " media_filename, media_type, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (page_id, title or "Untitled", media, media_type, now_iso()),
            )
            db.commit()
            flash("Added to the showcase.", "ok")
        return redirect(url_for("artist_view", page_id=page_id))
    items = db.execute(
        "SELECT * FROM showcase_items WHERE artist_page_id = ?"
        " ORDER BY created_at DESC, id DESC",
        (page_id,),
    ).fetchall()
    shop_items = db.execute(
        "SELECT * FROM marketplace_items WHERE artist_page_id = ?"
        " ORDER BY created_at DESC, id DESC",
        (page_id,),
    ).fetchall()
    tip_rows = db.execute(
        "SELECT t.*, u.username, u.display_name FROM tips t"
        " JOIN users u ON u.id = t.sender_id"
        " WHERE t.artist_page_id = ? ORDER BY t.created_at DESC, t.id DESC",
        (page_id,),
    ).fetchall()
    tip_total = db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM tips"
        " WHERE artist_page_id = ?",
        (page_id,),
    ).fetchone()[0]
    return render_template(
        "artist_view.html", page=page, items=items,
        limit=TAP_LIMITS[me["tier"]], shop_items=shop_items,
        tip_rows=tip_rows, tip_total=tip_total,
    )


# ---------------------------------------------------------------- phase 2
@app.route("/artists/<int:page_id>/tip", methods=["POST"])
@login_required
def artist_tip(page_id):
    """Record a creator tip in the ledger only. No payment is taken and
    no money moves; the payout stays pending until a real payment
    provider and payout method are connected by the account owner."""
    me = current_user()
    db = get_db()
    page = db.execute(
        "SELECT * FROM artist_pages WHERE id = ?", (page_id,)
    ).fetchone()
    if not page:
        abort(404)
    if page["user_id"] == me["id"]:
        flash("You cannot tip your own artist page.", "error")
        return redirect(url_for("artist_view", page_id=page_id))
    try:
        amount_cents = int(round(float(request.form.get("amount", "")) * 100))
    except (TypeError, ValueError):
        amount_cents = 0
    note = request.form.get("note", "").strip()[:500]
    if amount_cents < 100:
        flash("Tips start at $1.00.", "error")
        return redirect(url_for("artist_view", page_id=page_id))
    fee, creator_amount = split_amount(amount_cents)
    db.execute(
        "INSERT INTO tips (sender_id, recipient_id, artist_page_id,"
        " amount_cents, platform_fee_cents, creator_amount_cents, fee_rate,"
        " note, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?,"
        " 'pending_payout', ?)",
        (me["id"], page["user_id"], page_id, amount_cents, fee,
         creator_amount, PLATFORM_FEE_RATE, note, now_iso()),
    )
    db.commit()
    flash(
        "Tip recorded in the ledger (no payment taken yet): %s total —"
        " %s to the creator, %s platform fee at %d%%. Payout is pending"
        " until a payment provider is connected."
        % (cents_to_dollars(amount_cents), cents_to_dollars(creator_amount),
           cents_to_dollars(fee), PLATFORM_FEE_PERCENT),
        "ok",
    )
    return redirect(url_for("artist_view", page_id=page_id))


@app.route("/artists/<int:page_id>/items", methods=["POST"])
@login_required
def artist_item_new(page_id):
    me = current_user()
    db = get_db()
    page = db.execute(
        "SELECT * FROM artist_pages WHERE id = ?", (page_id,)
    ).fetchone()
    if not page:
        abort(404)
    if page["user_id"] != me["id"]:
        flash("Only the page owner can list items.", "error")
        return redirect(url_for("artist_view", page_id=page_id))
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()[:1000]
    try:
        price_cents = int(round(float(request.form.get("price", "")) * 100))
    except (TypeError, ValueError):
        price_cents = 0
    if not title or price_cents < 100:
        flash("Give the item a title and a price of at least $1.00.",
              "error")
        return redirect(url_for("artist_view", page_id=page_id))
    db.execute(
        "INSERT INTO marketplace_items (artist_page_id, owner_id, title,"
        " description, price_cents, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (page_id, me["id"], title, description, price_cents, now_iso()),
    )
    db.commit()
    flash("Item listed. Sales are ledger records only — no checkout"
          " or payment runs yet.", "ok")
    return redirect(url_for("artist_view", page_id=page_id))


@app.route("/items/<int:item_id>/buy", methods=["POST"])
@login_required
def marketplace_buy(item_id):
    """Record a marketplace sale in the ledger only. No checkout runs
    and no money moves; the seller amount stays pending payout."""
    me = current_user()
    db = get_db()
    item = db.execute(
        "SELECT * FROM marketplace_items WHERE id = ?", (item_id,)
    ).fetchone()
    if not item:
        abort(404)
    if item["owner_id"] == me["id"]:
        flash("You cannot buy your own item.", "error")
        return redirect(url_for("artist_view", page_id=item["artist_page_id"]))
    fee, seller_amount = split_amount(item["price_cents"])
    db.execute(
        "INSERT INTO marketplace_sales (item_id, buyer_id, seller_id,"
        " price_cents, commission_cents, seller_amount_cents,"
        " commission_rate, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_payout', ?)",
        (item_id, me["id"], item["owner_id"], item["price_cents"], fee,
         seller_amount, PLATFORM_FEE_RATE, now_iso()),
    )
    db.commit()
    flash(
        "Sale recorded in the ledger (no payment taken): %s — %s to the"
        " seller, %s commission at %d%%. Payout is pending until a"
        " payment provider is connected."
        % (cents_to_dollars(item["price_cents"]),
           cents_to_dollars(seller_amount), cents_to_dollars(fee),
           PLATFORM_FEE_PERCENT),
        "ok",
    )
    return redirect(url_for("artist_view", page_id=item["artist_page_id"]))


@app.route("/revenue")
@login_required
def revenue():
    """One-page revenue summary. Every figure is a ledger figure from
    this database — demo tier switches and demo ledger records included
    — and none of it is real billed or paid-out revenue."""
    me = current_user()
    db = get_db()
    tier_counts = {
        r["tier"]: r["n"] for r in db.execute(
            "SELECT tier, COUNT(*) AS n FROM users GROUP BY tier"
        ).fetchall()
    }
    projected_mrr_cents = (
        tier_counts.get("pro", 0) * 499 + tier_counts.get("elite", 0) * 1999
    )
    paypal_active = db.execute(
        "SELECT COUNT(*) FROM paypal_subscriptions WHERE status = 'active'"
    ).fetchone()[0]
    tip_totals = db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS total,"
        " COALESCE(SUM(platform_fee_cents), 0) AS fees,"
        " COALESCE(SUM(creator_amount_cents), 0) AS creators,"
        " COUNT(*) AS n FROM tips"
    ).fetchone()
    sale_totals = db.execute(
        "SELECT COALESCE(SUM(price_cents), 0) AS total,"
        " COALESCE(SUM(commission_cents), 0) AS fees,"
        " COALESCE(SUM(seller_amount_cents), 0) AS sellers,"
        " COUNT(*) AS n FROM marketplace_sales"
    ).fetchone()
    my_pending = db.execute(
        "SELECT (SELECT COALESCE(SUM(creator_amount_cents), 0) FROM tips"
        " WHERE recipient_id = ? AND status = 'pending_payout')"
        " + (SELECT COALESCE(SUM(seller_amount_cents), 0) FROM"
        " marketplace_sales WHERE seller_id = ?"
        " AND status = 'pending_payout')",
        (me["id"], me["id"]),
    ).fetchone()[0]
    creators = []
    if is_admin_user(me):
        creators = db.execute(
            "SELECT u.display_name, u.username,"
            " (SELECT COALESCE(SUM(creator_amount_cents), 0) FROM tips"
            "  WHERE recipient_id = u.id AND status = 'pending_payout')"
            " + (SELECT COALESCE(SUM(seller_amount_cents), 0) FROM"
            "  marketplace_sales WHERE seller_id = u.id"
            "  AND status = 'pending_payout') AS pending"
            " FROM users u ORDER BY pending DESC, u.display_name"
        ).fetchall()
        creators = [c for c in creators if c["pending"]]
    return render_template(
        "revenue.html",
        tier_counts=tier_counts,
        projected_mrr_cents=projected_mrr_cents,
        paypal_active=paypal_active,
        tip_totals=tip_totals, sale_totals=sale_totals,
        my_pending=my_pending, creators=creators,
        ad_slots_per_feed=AD_SLOTS_PER_FEED,
        ad_slots_per_discover=AD_SLOTS_PER_DISCOVER,
    )


# ---------------------------------------------------------------- linked accounts
@app.route("/links", methods=["GET", "POST"])
@login_required
def links():
    me = current_user()
    db = get_db()
    if request.method == "POST":
        platform = request.form.get("platform", "").strip()
        handle = request.form.get("handle", "").strip()
        cap = LINK_CAPS[me["tier"]]
        count = db.execute(
            "SELECT COUNT(*) FROM linked_accounts WHERE user_id = ?",
            (me["id"],),
        ).fetchone()[0]
        if not platform or not handle:
            flash("Platform and handle are both required.", "error")
        elif cap is not None and count >= cap:
            if me["tier"] == "free":
                flash("Your Free plan includes no linked accounts. Upgrade"
                      " to Pro (up to 2) or Elite (unlimited) to add links.",
                      "error")
            else:
                flash("You already have 2 linked accounts, the Pro limit."
                      " Upgrade to Elite for unlimited linked accounts.",
                      "error")
        else:
            db.execute(
                "INSERT INTO linked_accounts (user_id, platform, handle,"
                " created_at) VALUES (?, ?, ?, ?)",
                (me["id"], platform, handle, now_iso()),
            )
            db.commit()
            flash("Linked account added.", "ok")
        return redirect(url_for("links"))
    rows = db.execute(
        "SELECT * FROM linked_accounts WHERE user_id = ?"
        " ORDER BY created_at DESC",
        (me["id"],),
    ).fetchall()
    cap = LINK_CAPS[me["tier"]]
    return render_template("links.html", links=rows, cap=cap)


# ---------------------------------------------------------------- lives
@app.route("/lives", methods=["GET", "POST"])
@login_required
def lives():
    me = current_user()
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip() or "Untitled Live"
        retention = request.form.get("retention", "")
        if retention not in LIVE_OPTIONS[me["tier"]]:
            flash("That retention choice is not available on your plan.",
                  "error")
            return redirect(url_for("lives"))
        delta = LIVE_DELTAS[retention]
        saved_until = None
        if delta is not None:
            saved_until = (datetime.now(timezone.utc) + delta).strftime(
                "%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO lives (user_id, title, retention, saved_until,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (me["id"], title, retention, saved_until, now_iso()),
        )
        db.commit()
        if retention == "not_saved":
            flash("Live created. It will not be saved.", "ok")
        else:
            flash("Live created. Replay: %s." % LIVE_LABELS[retention], "ok")
        return redirect(url_for("lives"))
    rows = db.execute(
        "SELECT l.*, u.username, u.display_name FROM lives l"
        " JOIN users u ON u.id = l.user_id"
        " WHERE l.retention != 'not_saved'"
        " AND (l.saved_until IS NULL OR l.saved_until > ?)"
        " ORDER BY l.created_at DESC, l.id DESC",
        (now_iso(),),
    ).fetchall()
    return render_template(
        "lives.html", lives=rows, options=LIVE_OPTIONS[me["tier"]],
        labels=LIVE_LABELS,
    )


# ---------------------------------------------------------------- pwa
@app.route("/manifest.json")
def manifest():
    return send_from_directory(
        os.path.join(BASE_DIR, "static"), "manifest.json",
        mimetype="application/manifest+json",
    )


@app.route("/sw.js")
def service_worker():
    # Served from the site root so its scope covers all Loop pages.
    return send_from_directory(
        os.path.join(BASE_DIR, "static"), "sw.js",
        mimetype="application/javascript",
    )


# ---------------------------------------------------------------- premium
def current_subscription(user_id):
    """The user's latest pending or active PayPal subscription, if any."""
    return get_db().execute(
        "SELECT * FROM paypal_subscriptions WHERE user_id = ?"
        " AND status IN ('pending', 'active')"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


@app.route("/premium", methods=["GET", "POST"])
@login_required
def premium():
    me = current_user()
    if request.method == "POST":
        if paypal_ready():
            # Real payments are connected: the demo switch must not hand
            # out paid tiers for free.
            flash("Use the PayPal subscribe buttons below — demo tier"
                  " switching is disabled while payments are connected.",
                  "error")
            return redirect(url_for("premium"))
        tier = request.form.get("tier", "")
        if tier in ("free", "pro", "elite"):
            get_db().execute(
                "UPDATE users SET tier = ? WHERE id = ?", (tier, me["id"])
            )
            get_db().commit()
            flash("Demo switch: you are now on %s. No payment was charged"
                  " — payments are not connected yet."
                  % TIER_NAMES[tier], "ok")
            return redirect(url_for("premium"))
    return render_template(
        "premium.html", prices=TIER_PRICES, tap_limits=TAP_LIMITS,
        link_caps=LINK_CAPS, live_labels=LIVE_LABELS,
        sub=current_subscription(me["id"]),
    )


@app.route("/premium/subscribe", methods=["POST"])
@login_required
def premium_subscribe():
    """Start a PayPal subscription for Pro or Elite and redirect the
    user to PayPal to approve it. Nothing is charged here."""
    me = current_user()
    tier = request.form.get("tier", "")
    if tier not in ("pro", "elite"):
        flash("Choose Pro or Elite.", "error")
        return redirect(url_for("premium"))
    if not paypal_ready():
        flash("Payments are not connected yet. Loop takes no payments"
              " until PayPal is linked.", "error")
        return redirect(url_for("premium"))
    try:
        token = get_access_token()
        sub_id, approve_url = create_subscription(
            token,
            paypal_plan_id(tier),
            url_for("premium_paypal_return", _external=True),
            url_for("premium_paypal_cancel", _external=True),
        )
    except PayPalError:
        flash("Could not start PayPal checkout. Please try again later.",
              "error")
        return redirect(url_for("premium"))
    db = get_db()
    db.execute(
        "INSERT INTO paypal_subscriptions (user_id, paypal_subscription_id,"
        " tier, status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'pending', ?, ?)",
        (me["id"], sub_id, tier, now_iso(), now_iso()),
    )
    db.commit()
    return redirect(approve_url)


@app.route("/premium/paypal/return")
@login_required
def premium_paypal_return():
    """PayPal sends the user back here after approval. The tier changes
    only after PayPal itself reports the subscription ACTIVE."""
    me = current_user()
    sub_id = request.args.get("subscription_id") \
        or request.args.get("token", "")
    db = get_db()
    row = db.execute(
        "SELECT * FROM paypal_subscriptions"
        " WHERE paypal_subscription_id = ? AND user_id = ?",
        (sub_id, me["id"]),
    ).fetchone()
    if not row:
        flash("Could not find that PayPal subscription.", "error")
        return redirect(url_for("premium"))
    try:
        token = get_access_token()
        data = get_subscription(token, sub_id)
    except PayPalError:
        flash("Could not confirm your PayPal subscription yet. Your plan"
              " will update once PayPal confirms it.", "error")
        return redirect(url_for("premium"))
    status = data.get("status", "")
    if status == "ACTIVE":
        db.execute(
            "UPDATE users SET tier = ? WHERE id = ?", (row["tier"], me["id"])
        )
        db.execute(
            "UPDATE paypal_subscriptions SET status = 'active',"
            " updated_at = ? WHERE id = ?",
            (now_iso(), row["id"]),
        )
        db.commit()
        flash("Welcome to %s! Your PayPal subscription is active."
              % TIER_NAMES[row["tier"]], "ok")
    else:
        flash("Your PayPal subscription is %s. Your plan will update once"
              " it becomes active." % status.lower(), "ok")
    return redirect(url_for("premium"))


@app.route("/premium/paypal/cancel")
@login_required
def premium_paypal_cancel():
    """The user cancelled approval on PayPal's side. Nothing was taken."""
    flash("PayPal checkout was cancelled. No payment was taken.", "ok")
    return redirect(url_for("premium"))


@app.route("/premium/cancel-subscription", methods=["POST"])
@login_required
def premium_cancel_subscription():
    """Cancel the user's PayPal subscription and drop them to Free."""
    me = current_user()
    db = get_db()
    row = db.execute(
        "SELECT * FROM paypal_subscriptions WHERE user_id = ?"
        " AND status = 'active' ORDER BY created_at DESC, id DESC LIMIT 1",
        (me["id"],),
    ).fetchone()
    if row and paypal_configured():
        try:
            token = get_access_token()
            cancel_subscription(
                token, row["paypal_subscription_id"],
                "Cancelled by user on Loop",
            )
        except PayPalError:
            flash("Could not reach PayPal to cancel. Please try again"
                  " later.", "error")
            return redirect(url_for("premium"))
        db.execute(
            "UPDATE paypal_subscriptions SET status = 'cancelled',"
            " updated_at = ? WHERE id = ?",
            (now_iso(), row["id"]),
        )
    db.execute("UPDATE users SET tier = 'free' WHERE id = ?", (me["id"],))
    db.commit()
    flash("Subscription cancelled. You are now on the Free plan.", "ok")
    return redirect(url_for("premium"))


@app.route("/webhooks/paypal", methods=["POST"])
def paypal_webhook():
    """PayPal subscription events. Signature verification is REQUIRED
    before trusting any event in production: without a PAYPAL_WEBHOOK_ID
    configured, or when verification fails, the event is acknowledged
    and ignored and no tier ever changes."""
    raw = request.get_data()
    try:
        event = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return "bad event", 400
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID", "").strip()
    if not webhook_id:
        app.logger.warning(
            "PayPal webhook received but PAYPAL_WEBHOOK_ID is not set;"
            " event ignored."
        )
        return "ignored", 200
    try:
        token = get_access_token()
        verified = verify_webhook_signature(
            token, request.headers, raw, webhook_id
        )
    except PayPalError:
        # Transient failure: non-2xx so PayPal retries the event.
        return "verification unavailable", 500
    if not verified:
        return "ignored", 200
    event_type = event.get("event_type", "")
    resource = event.get("resource", {}) or {}
    sub_id = resource.get("id", "")
    if not sub_id:
        return "ignored", 200
    db = get_db()
    row = db.execute(
        "SELECT * FROM paypal_subscriptions"
        " WHERE paypal_subscription_id = ?",
        (sub_id,),
    ).fetchone()
    if not row:
        return "ignored", 200
    if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
        db.execute(
            "UPDATE users SET tier = ? WHERE id = ?",
            (row["tier"], row["user_id"]),
        )
        db.execute(
            "UPDATE paypal_subscriptions SET status = 'active',"
            " updated_at = ? WHERE id = ?",
            (now_iso(), row["id"]),
        )
        db.commit()
    elif event_type in ("BILLING.SUBSCRIPTION.CANCELLED",
                        "BILLING.SUBSCRIPTION.EXPIRED"):
        db.execute(
            "UPDATE users SET tier = 'free' WHERE id = ?",
            (row["user_id"],),
        )
        db.execute(
            "UPDATE paypal_subscriptions SET status = 'cancelled',"
            " updated_at = ? WHERE id = ?",
            (now_iso(), row["id"]),
        )
        db.commit()
    # Other event types (e.g. payment failures) are acknowledged and
    # left for a future dunning step; they change no tier on their own.
    return "ok", 200


if __name__ == "__main__":
    # 0.0.0.0 + $PORT so a host like Render can serve it publicly.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")),
            debug=False)
