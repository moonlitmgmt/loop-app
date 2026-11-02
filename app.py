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
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask, g, render_template, request, redirect, url_for, session,
    flash, send_from_directory, abort,
)
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
    return {
        "me": current_user(),
        "TIER_NAMES": TIER_NAMES,
        "paypal_ready": paypal_ready(),
    }


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
        if not body:
            flash("Write something first.", "error")
        else:
            db.execute(
                "INSERT INTO posts (user_id, body, audience, created_at)"
                " VALUES (?, ?, ?, ?)",
                (me["id"], body, audience, now_iso()),
            )
            db.commit()
            flash("Posted.", "ok")
        return redirect(url_for("feed"))
    friend_ids = {f["id"] for f in friends_of(me["id"])}
    rows = db.execute(
        "SELECT p.*, u.username, u.display_name FROM posts p"
        " JOIN users u ON u.id = p.user_id"
        " ORDER BY p.created_at DESC, p.id DESC LIMIT 200"
    ).fetchall()
    visible = [
        p for p in rows
        if p["audience"] == "public"
        or p["user_id"] == me["id"]
        or p["user_id"] in friend_ids
    ]
    return render_template("feed.html", posts=visible)


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
    return render_template(
        "profile.html", user=user, personal=personal,
        can_see_personal=can_see_personal, artists=artists, links=links,
        LINK_CAPS=LINK_CAPS,
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
        " JOIN users u ON u.id = ap.user_id WHERE 1 = 1"
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
    return render_template(
        "artist_view.html", page=page, items=items,
        limit=TAP_LIMITS[me["tier"]],
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
