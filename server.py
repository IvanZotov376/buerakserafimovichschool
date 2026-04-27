import os
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS


app = Flask(__name__)

# Разрешаем запросы с GitHub Pages и для локальной проверки.
# Если нужно, можно оставить только свой домен GitHub Pages.
CORS(app, resources={r"/api/*": {"origins": "*"}})

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "users.db"


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            full_name TEXT,
            avatar_color TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def row_to_user(row):
    return {
        "id": row["id"],
        "login": row["login"],
        "email": row["email"],
        "role": row["role"],
        "full_name": row["full_name"] or row["login"],
        "avatar_color": row["avatar_color"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "success": True,
        "message": "Backend работает",
        "endpoints": ["/api/health", "/api/login", "/api/profile/<login>"]
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"success": True, "status": "ok"})


@app.route("/api/login", methods=["POST", "OPTIONS"])
def login():
    if request.method == "OPTIONS":
        return jsonify({"success": True})

    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    role = (data.get("role") or "").strip()
    full_name = (data.get("full_name") or login_value).strip()
    avatar_color = data.get("avatar_color") or ""

    if not login_value:
        return jsonify({"success": False, "error": "Введите логин"}), 400

    if not email:
        return jsonify({"success": False, "error": "Введите электронную почту"}), 400

    if not password:
        return jsonify({"success": False, "error": "Введите пароль"}), 400

    if not role:
        return jsonify({"success": False, "error": "Выберите роль"}), 400

    now = datetime.utcnow().isoformat()

    conn = get_db_connection()
    existing = conn.execute(
        "SELECT * FROM users WHERE login = ?",
        (login_value,)
    ).fetchone()

    if existing:
        if existing["password"] != password:
            conn.close()
            return jsonify({"success": False, "error": "Неверный пароль"}), 401

        conn.execute(
            """
            UPDATE users
            SET email = ?, role = ?, full_name = ?, avatar_color = ?, updated_at = ?
            WHERE login = ?
            """,
            (email, role, full_name, avatar_color, now, login_value)
        )
        conn.commit()

        user = conn.execute(
            "SELECT * FROM users WHERE login = ?",
            (login_value,)
        ).fetchone()
        conn.close()

        return jsonify({
            "success": True,
            "message": "Вход выполнен",
            "user": row_to_user(user)
        })

    conn.execute(
        """
        INSERT INTO users (login, email, password, role, full_name, avatar_color, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (login_value, email, password, role, full_name, avatar_color, now, now)
    )
    conn.commit()

    user = conn.execute(
        "SELECT * FROM users WHERE login = ?",
        (login_value,)
    ).fetchone()
    conn.close()

    return jsonify({
        "success": True,
        "message": "Пользователь создан и вход выполнен",
        "user": row_to_user(user)
    })


@app.route("/api/profile/<login_value>", methods=["GET"])
def get_profile(login_value):
    conn = get_db_connection()
    user = conn.execute(
        "SELECT * FROM users WHERE login = ?",
        (login_value,)
    ).fetchone()
    conn.close()

    if not user:
        return jsonify({"success": False, "error": "Пользователь не найден"}), 404

    return jsonify({"success": True, "user": row_to_user(user)})


@app.route("/api/users", methods=["GET"])
def get_users():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM users ORDER BY id DESC"
    ).fetchall()
    conn.close()

    return jsonify({
        "success": True,
        "users": [row_to_user(row) for row in rows]
    })


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
