import os
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "users.db"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def has_col(conn, table, col):
    return any(row["name"] == col for row in conn.execute(f"PRAGMA table_info({table})"))


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT UNIQUE NOT NULL,
            email TEXT DEFAULT '',
            password TEXT NOT NULL,
            role TEXT DEFAULT 'Ученик',
            full_name TEXT,
            avatar_color TEXT,
            profile_photo TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    if not has_col(conn, "users", "email"):
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
    if not has_col(conn, "users", "profile_photo"):
        conn.execute("ALTER TABLE users ADD COLUMN profile_photo TEXT DEFAULT ''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            text TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


def user_json(row):
    return {
        "id": row["id"],
        "login": row["login"],
        "email": row["email"] or "",
        "role": row["role"] or "Ученик",
        "full_name": row["full_name"] or row["login"],
        "avatar_color": row["avatar_color"] or "",
        "profile_photo": row["profile_photo"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def message_json(row):
    return {
        "id": row["id"],
        "sender": row["sender"],
        "recipient": row["recipient"],
        "text": row["text"],
        "timestamp": row["timestamp"],
        "read": bool(row["read"]),
    }


@app.get("/")
def index():
    return jsonify({"success": True, "message": "Backend работает"})


@app.get("/api/health")
def health():
    return jsonify({"success": True, "status": "ok"})


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    role = (data.get("role") or "Ученик").strip()
    full_name = (data.get("full_name") or login_value).strip()
    avatar_color = data.get("avatar_color") or ""
    profile_photo = data.get("profile_photo") or ""

    if not login_value:
        return jsonify({"success": False, "error": "Введите логин"}), 400
    if not password:
        return jsonify({"success": False, "error": "Введите пароль"}), 400

    now = datetime.utcnow().isoformat(timespec="seconds")

    conn = db()
    user = conn.execute("SELECT * FROM users WHERE login = ?", (login_value,)).fetchone()

    if user:
        if user["password"] != password:
            conn.close()
            return jsonify({"success": False, "error": "Неверный пароль"}), 401

        conn.execute("""
            UPDATE users
            SET email = COALESCE(NULLIF(?, ''), email),
                role = ?,
                full_name = ?,
                avatar_color = COALESCE(NULLIF(?, ''), avatar_color),
                profile_photo = COALESCE(NULLIF(?, ''), profile_photo),
                updated_at = ?
            WHERE login = ?
        """, (email, role, full_name, avatar_color, profile_photo, now, login_value))
    else:
        conn.execute("""
            INSERT INTO users (login, email, password, role, full_name, avatar_color, profile_photo, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (login_value, email, password, role, full_name, avatar_color, profile_photo, now, now))

    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE login = ?", (login_value,)).fetchone()
    conn.close()

    return jsonify({"success": True, "user": user_json(user)})


@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    role = (data.get("role") or "Ученик").strip()
    full_name = (data.get("full_name") or login_value).strip()
    avatar_color = data.get("avatar_color") or ""
    profile_photo = data.get("profile_photo") or ""

    if not login_value:
        return jsonify({"success": False, "error": "Введите логин"}), 400
    if not password:
        return jsonify({"success": False, "error": "Введите пароль"}), 400

    now = datetime.utcnow().isoformat(timespec="seconds")

    conn = db()
    user = conn.execute("SELECT * FROM users WHERE login = ?", (login_value,)).fetchone()

    if user:
        conn.execute("""
            UPDATE users
            SET email = COALESCE(NULLIF(?, ''), email),
                password = ?,
                role = ?,
                full_name = ?,
                avatar_color = COALESCE(NULLIF(?, ''), avatar_color),
                profile_photo = ?,
                updated_at = ?
            WHERE login = ?
        """, (email, password, role, full_name, avatar_color, profile_photo, now, login_value))
    else:
        conn.execute("""
            INSERT INTO users (login, email, password, role, full_name, avatar_color, profile_photo, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (login_value, email, password, role, full_name, avatar_color, profile_photo, now, now))

    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE login = ?", (login_value,)).fetchone()
    conn.close()

    return jsonify({"success": True, "user": user_json(user)})


@app.get("/api/user_info")
def user_info():
    login_value = (request.args.get("login") or "").strip()
    if not login_value:
        return jsonify({"success": False, "error": "Не передан login"}), 400

    conn = db()
    user = conn.execute("SELECT * FROM users WHERE login = ?", (login_value,)).fetchone()
    conn.close()

    if not user:
        return jsonify({"success": False, "error": "Пользователь не найден"}), 404

    return jsonify({"success": True, "user": user_json(user)})


@app.get("/api/messages")
def messages():
    login_value = (request.args.get("login") or "").strip()
    if not login_value:
        return jsonify({"success": False, "error": "Не передан login"}), 400

    conn = db()
    rows = conn.execute("""
        SELECT *
        FROM messages
        WHERE sender = ? OR recipient = ?
        ORDER BY datetime(timestamp) ASC, id ASC
    """, (login_value, login_value)).fetchall()
    conn.close()

    return jsonify({"success": True, "data": [message_json(row) for row in rows]})


@app.post("/api/send")
def send():
    data = request.get_json(silent=True) or {}

    sender = (data.get("sender") or "").strip()
    recipient = (data.get("recipient") or "").strip()
    text = (data.get("text") or "").strip()

    if not sender or not recipient or not text:
        return jsonify({"success": False, "error": "Заполните отправителя, получателя и текст"}), 400
    if sender == recipient:
        return jsonify({"success": False, "error": "Нельзя отправить сообщение самому себе"}), 400

    conn = db()

    if not conn.execute("SELECT 1 FROM users WHERE login = ?", (sender,)).fetchone():
        conn.close()
        return jsonify({"success": False, "error": "Отправитель не найден"}), 404

    if not conn.execute("SELECT 1 FROM users WHERE login = ?", (recipient,)).fetchone():
        conn.close()
        return jsonify({"success": False, "error": "Получатель не найден. Сначала он должен войти на сайт."}), 404

    now = datetime.utcnow().isoformat(timespec="seconds")
    cur = conn.execute("""
        INSERT INTO messages (sender, recipient, text, timestamp, read)
        VALUES (?, ?, ?, ?, 0)
    """, (sender, recipient, text, now))
    conn.commit()

    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()

    return jsonify({"success": True, "message": message_json(msg)})


@app.post("/api/mark_read")
def mark_read():
    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    partner = (data.get("partner") or "").strip()

    if not login_value or not partner:
        return jsonify({"success": False, "error": "Не переданы login или partner"}), 400

    conn = db()
    conn.execute("""
        UPDATE messages
        SET read = 1
        WHERE sender = ? AND recipient = ?
    """, (partner, login_value))
    conn.commit()
    conn.close()

    return jsonify({"success": True})


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
