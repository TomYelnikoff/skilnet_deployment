import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session as flask_session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CHAT_DB_PATH", os.path.join(BASE_DIR, "chat_history.db"))

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-6-v1")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT", "You are a helpful, concise and friendly assistant."
)
# Context window, like n8n's default Simple Memory: only the last N messages are
# sent verbatim. Older ones are compacted into a rolling summary that is saved
# on the conversation and sent in the system prompt instead.
CONTEXT_WINDOW_MESSAGES = max(1, int(os.environ.get("CONTEXT_WINDOW_MESSAGES", "5")))
# A small, cheap model does the summarizing (separate rate limit from the chat model).
SUMMARY_MODEL_ID = os.environ.get(
    "SUMMARY_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0"
)
# Compaction is lazy: messages that fell out of the window stay in the context
# verbatim until at least COMPACT_BATCH_MESSAGES of them have piled up AND they
# hold >= COMPACT_MIN_CHARS (below that a summarizer call costs more than it
# saves). COMPACT_FORCE_MESSAGES bounds the pile regardless of size.
COMPACT_BATCH_MESSAGES = max(1, int(os.environ.get("COMPACT_BATCH_MESSAGES", "4")))
COMPACT_MIN_CHARS = int(os.environ.get("COMPACT_MIN_CHARS", "600"))
COMPACT_FORCE_MESSAGES = max(
    COMPACT_BATCH_MESSAGES, int(os.environ.get("COMPACT_FORCE_MESSAGES", "20"))
)
SUMMARY_MAX_TOKENS = 600
SUMMARY_SYSTEM_PROMPT = (
    "You maintain a compact running summary of a conversation between a user and an "
    "AI assistant. Given the previous summary (if any) and newer messages, output the "
    "updated summary only. Keep durable facts, names, numbers, user preferences, "
    "decisions, code/identifiers and unresolved tasks. Drop pleasantries and "
    "repetition. Plain text, at most about 200 words."
)
MAX_MESSAGE_CHARS = 8000
MAX_TITLE_CHARS = 60

MIN_PASSWORD_CHARS = 8
MAX_PASSWORD_CHARS = 128  # bound hashing cost
MAX_EMAIL_CHARS = 254
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Hash checked when the email is unknown, so login timing doesn't reveal accounts.
DUMMY_HASH = generate_password_hash("not-a-real-password")


def load_secret_key():
    """SECRET_KEY env var, else a random key persisted next to the database so
    sessions survive restarts (the /data volume in Docker)."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), ".secret_key")
    for _ in range(2):
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
    raise RuntimeError("Could not load or create the session secret key")


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set SESSION_COOKIE_SECURE=1 when serving over HTTPS.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

session = boto3.Session(region_name=AWS_REGION)
# Adaptive retries absorb short Bedrock throttling bursts instead of failing the turn.
bedrock = session.client(
    "bedrock-runtime",
    config=Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=90),
)


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        # Lightweight migration: token usage columns (NULL for older rows).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        for col in ("input_tokens", "output_tokens"):
            if col not in cols:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER")
        # Migration: conversations belong to a user (pre-auth rows stay NULL
        # and are not visible to any account).
        conv_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
        if "user_id" not in conv_cols:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN user_id INTEGER "
                "REFERENCES users(id) ON DELETE CASCADE"
            )
        # Migration: compacted context. summary covers messages with id <= summary_upto.
        conv_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
        if "summary" not in conv_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN summary TEXT")
        if "summary_upto" not in conv_cols:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN summary_upto INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_user "
            "ON conversations(user_id, updated_at DESC)"
        )


def conversation_row(db, conv_id):
    """The conversation, only if it belongs to the signed-in user."""
    return db.execute(
        "SELECT id, title, created_at, updated_at FROM conversations "
        "WHERE id = ? AND user_id = ?",
        (conv_id, g.user["id"]),
    ).fetchone()


def compact_context(db, conv_id, window_first_id):
    """Fold messages older than the context window into the saved summary, lazily.

    Returns (summary, pending_rows, tokens):
      summary       saved summary text or None
      pending_rows  older messages NOT yet in the summary; the caller sends them
                    verbatim before the window so no context is lost
      tokens        (input, output) of the summarizer call, only if one was made
    On a summarizer failure nothing is lost: the messages stay pending and
    compaction is retried next turn.
    """
    conv = db.execute(
        "SELECT summary, summary_upto FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    summary, upto = (conv["summary"], conv["summary_upto"]) if conv else (None, 0)

    older = db.execute(
        "SELECT id, role, content FROM messages "
        "WHERE conversation_id = ? AND id > ? AND id < ? ORDER BY id",
        (conv_id, upto, window_first_id),
    ).fetchall()
    if not older:
        return summary, [], None

    chars = sum(len(m["content"]) for m in older)
    due = len(older) >= COMPACT_BATCH_MESSAGES and (
        chars >= COMPACT_MIN_CHARS or len(older) >= COMPACT_FORCE_MESSAGES
    )
    if not due:
        return summary, older, None

    transcript = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in older)
    prompt = (
        f"Previous summary:\n{summary or '(none)'}\n\n"
        f"Newer messages to fold in:\n{transcript}\n\nUpdated summary:"
    )
    try:
        resp = bedrock.converse(
            modelId=SUMMARY_MODEL_ID,
            system=[{"text": SUMMARY_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": SUMMARY_MAX_TOKENS, "temperature": 0.2},
        )
    except (ClientError, NoCredentialsError) as err:
        app.logger.error("Context compaction failed, will retry next turn: %s", err)
        return summary, older, None

    new_summary = "".join(
        b["text"] for b in resp["output"]["message"]["content"] if "text" in b
    ).strip()
    if not new_summary:
        return summary, older, None
    db.execute(
        "UPDATE conversations SET summary = ?, summary_upto = ? WHERE id = ?",
        (new_summary, older[-1]["id"], conv_id),
    )
    db.commit()
    usage = resp.get("usage", {})
    return new_summary, [], (usage.get("inputTokens") or 0, usage.get("outputTokens") or 0)


# -------------------------------------------------------------------- auth
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        uid = flask_session.get("user_id")
        user = (
            get_db().execute("SELECT id, email FROM users WHERE id = ?", (uid,)).fetchone()
            if uid is not None
            else None
        )
        if user is None:
            flask_session.clear()
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required."), 401
            return redirect(url_for("login_page"))
        g.user = user
        return view(*args, **kwargs)

    return wrapped


def credentials_from_request():
    data = request.get_json(silent=True) or {}
    email = data.get("email")
    password = data.get("password")
    email = email.strip().lower() if isinstance(email, str) else ""
    return email, password if isinstance(password, str) else ""


def start_session(user_id):
    flask_session.clear()
    flask_session.permanent = True
    flask_session["user_id"] = user_id


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/login")
def login_page():
    if flask_session.get("user_id") is not None:
        return redirect(url_for("index"))
    return render_template("auth.html", mode="login")


@app.get("/signup")
def signup_page():
    if flask_session.get("user_id") is not None:
        return redirect(url_for("index"))
    return render_template("auth.html", mode="signup")


@app.post("/api/signup")
def signup():
    email, password = credentials_from_request()
    if not email or len(email) > MAX_EMAIL_CHARS or not EMAIL_RE.match(email):
        return jsonify(error="Enter a valid email address."), 400
    if len(password) < MIN_PASSWORD_CHARS:
        return jsonify(error=f"Password must be at least {MIN_PASSWORD_CHARS} characters."), 400
    if len(password) > MAX_PASSWORD_CHARS:
        return jsonify(error=f"Password must be at most {MAX_PASSWORD_CHARS} characters."), 400

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, generate_password_hash(password), now()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="An account with this email already exists."), 409
    start_session(cur.lastrowid)
    return jsonify(user={"email": email}), 201


@app.post("/api/login")
def login():
    email, password = credentials_from_request()
    user = None
    if email and password and len(password) <= MAX_PASSWORD_CHARS:
        user = get_db().execute(
            "SELECT id, email, password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
    ok = check_password_hash(user["password_hash"] if user else DUMMY_HASH, password)
    if user is None or not ok:
        return jsonify(error="Invalid email or password."), 401
    start_session(user["id"])
    return jsonify(user={"email": user["email"]})


@app.post("/api/logout")
def logout():
    flask_session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required
def me():
    return jsonify(user={"email": g.user["email"]})


# ------------------------------------------------------------------ routes
@app.get("/")
@login_required
def index():
    return render_template("index.html", email=g.user["email"])


@app.get("/api/conversations")
@login_required
def list_conversations():
    rows = get_db().execute(
        "SELECT id, title, created_at, updated_at FROM conversations "
        "WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
        (g.user["id"],),
    ).fetchall()
    return jsonify(conversations=[dict(r) for r in rows])


@app.get("/api/conversations/<int:conv_id>")
@login_required
def get_conversation(conv_id):
    db = get_db()
    conv = conversation_row(db, conv_id)
    if conv is None:
        return jsonify(error="Conversation not found."), 404
    msgs = db.execute(
        "SELECT role, content, created_at, input_tokens, output_tokens FROM messages "
        "WHERE conversation_id = ? ORDER BY id",
        (conv_id,),
    ).fetchall()
    totals = db.execute(
        "SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens "
        "FROM messages WHERE conversation_id = ?",
        (conv_id,),
    ).fetchone()
    return jsonify(
        conversation=dict(conv),
        messages=[dict(m) for m in msgs],
        totals=dict(totals),
    )


@app.patch("/api/conversations/<int:conv_id>")
@login_required
def rename_conversation(conv_id):
    title = ((request.get_json(silent=True) or {}).get("title") or "").strip()
    if not title:
        return jsonify(error="Title cannot be empty."), 400
    db = get_db()
    cur = db.execute(
        "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
        (title[:MAX_TITLE_CHARS], conv_id, g.user["id"]),
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify(error="Conversation not found."), 404
    return jsonify(conversation=dict(conversation_row(db, conv_id)))


@app.delete("/api/conversations/<int:conv_id>")
@login_required
def delete_conversation(conv_id):
    db = get_db()
    cur = db.execute(
        "DELETE FROM conversations WHERE id = ? AND user_id = ?", (conv_id, g.user["id"])
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify(error="Conversation not found."), 404
    return jsonify(ok=True)


@app.post("/api/chat")
@login_required
def chat():
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()[:MAX_MESSAGE_CHARS]
    conv_id = data.get("conversation_id")

    if not text:
        return jsonify(error="Message is empty."), 400

    db = get_db()
    created_new = False

    if conv_id is None:
        title = " ".join(text.split())[:MAX_TITLE_CHARS]
        ts = now()
        cur = db.execute(
            "INSERT INTO conversations (title, created_at, updated_at, user_id) "
            "VALUES (?, ?, ?, ?)",
            (title, ts, ts, g.user["id"]),
        )
        conv_id = cur.lastrowid
        created_new = True
    elif conversation_row(db, conv_id) is None:
        return jsonify(error="Conversation not found."), 404

    user_msg_id = db.execute(
        "INSERT INTO messages (conversation_id, role, content, created_at) "
        "VALUES (?, 'user', ?, ?)",
        (conv_id, text, now()),
    ).lastrowid
    db.commit()

    def rollback_user_turn():
        # Keep stored history valid (no unanswered user turn) on failure.
        if created_new:
            db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        else:
            db.execute("DELETE FROM messages WHERE id = ?", (user_msg_id,))
        db.commit()

    # Sliding window: only the last N messages go to the model verbatim.
    rows = db.execute(
        "SELECT id, role, content FROM messages WHERE conversation_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (conv_id, CONTEXT_WINDOW_MESSAGES),
    ).fetchall()[::-1]
    while rows and rows[0]["role"] != "user":
        rows.pop(0)

    # Older messages are folded into the saved rolling summary once enough have
    # piled up; until then they ride along verbatim (never silently dropped).
    summary, pending, summary_tokens = compact_context(
        db, conv_id, rows[0]["id"] if rows else 0
    )
    rows = list(pending) + rows
    while rows and rows[0]["role"] != "user":
        rows.pop(0)
    messages = [{"role": r["role"], "content": [{"text": r["content"]}]} for r in rows]
    system_text = SYSTEM_PROMPT
    if summary:
        system_text += "\n\nSummary of the earlier conversation:\n" + summary

    try:
        response = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": system_text}],
            messages=messages,
            inferenceConfig={"maxTokens": 1000, "temperature": 0.5},
        )
    except NoCredentialsError:
        rollback_user_turn()
        return jsonify(error="AWS credentials not found. Run: aws configure"), 500
    except ClientError as err:
        app.logger.error("Bedrock error: %s", err)
        rollback_user_turn()
        if err.response["Error"].get("Code") == "ThrottlingException":
            return jsonify(error="The model is busy right now. Please try again in a few seconds."), 429
        return jsonify(error=err.response["Error"]["Message"]), 502

    reply = "".join(
        b["text"] for b in response["output"]["message"]["content"] if "text" in b
    )

    usage = response.get("usage", {})
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    if summary_tokens and input_tokens is not None:
        # Count the compaction call too, so totals reflect the real cost.
        input_tokens += summary_tokens[0]
        output_tokens = (output_tokens or 0) + summary_tokens[1]

    ts = now()
    db.execute(
        "INSERT INTO messages "
        "(conversation_id, role, content, created_at, input_tokens, output_tokens) "
        "VALUES (?, 'assistant', ?, ?, ?, ?)",
        (conv_id, reply, ts, input_tokens, output_tokens),
    )
    db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conv_id))
    db.commit()

    totals = db.execute(
        "SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens "
        "FROM messages WHERE conversation_id = ?",
        (conv_id,),
    ).fetchone()

    return jsonify(
        reply=reply,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        totals=dict(totals),
        conversation=dict(conversation_row(db, conv_id)),
    )


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
