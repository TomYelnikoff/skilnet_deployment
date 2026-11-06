import os
import sqlite3
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from flask import Flask, g, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CHAT_DB_PATH", os.path.join(BASE_DIR, "chat_history.db"))

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-6-v1")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT", "You are a helpful, concise and friendly assistant."
)
MAX_HISTORY_MESSAGES = 40  # cap context sent to the model
MAX_MESSAGE_CHARS = 8000
MAX_TITLE_CHARS = 60

app = Flask(__name__)

session = boto3.Session(region_name=AWS_REGION)
bedrock = session.client("bedrock-runtime")


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


def conversation_row(db, conv_id):
    return db.execute(
        "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
        (conv_id,),
    ).fetchone()


# ------------------------------------------------------------------ routes
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/conversations")
def list_conversations():
    rows = get_db().execute(
        "SELECT id, title, created_at, updated_at FROM conversations "
        "ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    return jsonify(conversations=[dict(r) for r in rows])


@app.get("/api/conversations/<int:conv_id>")
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
def rename_conversation(conv_id):
    title = ((request.get_json(silent=True) or {}).get("title") or "").strip()
    if not title:
        return jsonify(error="Title cannot be empty."), 400
    db = get_db()
    cur = db.execute(
        "UPDATE conversations SET title = ? WHERE id = ?",
        (title[:MAX_TITLE_CHARS], conv_id),
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify(error="Conversation not found."), 404
    return jsonify(conversation=dict(conversation_row(db, conv_id)))


@app.delete("/api/conversations/<int:conv_id>")
def delete_conversation(conv_id):
    db = get_db()
    cur = db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify(error="Conversation not found."), 404
    return jsonify(ok=True)


@app.post("/api/chat")
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
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, ts, ts),
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

    rows = db.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (conv_id, MAX_HISTORY_MESSAGES),
    ).fetchall()[::-1]
    messages = [{"role": r["role"], "content": [{"text": r["content"]}]} for r in rows]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    try:
        response = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            inferenceConfig={"maxTokens": 1000, "temperature": 0.5},
        )
    except NoCredentialsError:
        rollback_user_turn()
        return jsonify(error="AWS credentials not found. Run: aws configure"), 500
    except ClientError as err:
        app.logger.error("Bedrock error: %s", err)
        rollback_user_turn()
        return jsonify(error=err.response["Error"]["Message"]), 502

    reply = "".join(
        b["text"] for b in response["output"]["message"]["content"] if "text" in b
    )

    usage = response.get("usage", {})
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")

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
