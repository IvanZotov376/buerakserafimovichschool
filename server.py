import os
import sqlite3
import asyncio
from datetime import datetime, date, timedelta
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    from netschoolapi import NetSchoolAPI
except Exception:
    NetSchoolAPI = None


app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "users.db"

NETSCHOOL_URL = os.environ.get("NETSCHOOL_URL", "https://sgo.volganet.ru")
NETSCHOOL_SCHOOL = os.environ.get("NETSCHOOL_SCHOOL", "Буракская СШ")


def run_async(coro):
    return asyncio.run(coro)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def has_col(conn, table, col):
    return any(row["name"] == col for row in conn.execute(f"PRAGMA table_info({table})"))


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT UNIQUE NOT NULL,
            email TEXT DEFAULT '',
            password TEXT DEFAULT '',
            role TEXT DEFAULT 'Ученик',
            full_name TEXT,
            avatar_color TEXT DEFAULT '',
            profile_photo TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    columns = {
        "email": "ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''",
        "password": "ALTER TABLE users ADD COLUMN password TEXT DEFAULT ''",
        "role": "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'Ученик'",
        "full_name": "ALTER TABLE users ADD COLUMN full_name TEXT",
        "avatar_color": "ALTER TABLE users ADD COLUMN avatar_color TEXT DEFAULT ''",
        "profile_photo": "ALTER TABLE users ADD COLUMN profile_photo TEXT DEFAULT ''",
    }

    for col, sql in columns.items():
        if not has_col(conn, "users", col):
            conn.execute(sql)

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


def ensure_user(login, password="", email="", role="Ученик", full_name="", avatar_color="", profile_photo=""):
    login = (login or "").strip()
    if not login:
        return None

    conn = db()
    current = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    now = now_iso()

    if current:
        conn.execute("""
            UPDATE users
            SET email = COALESCE(NULLIF(?, ''), email),
                password = COALESCE(NULLIF(?, ''), password),
                role = COALESCE(NULLIF(?, ''), role),
                full_name = COALESCE(NULLIF(?, ''), full_name),
                avatar_color = COALESCE(NULLIF(?, ''), avatar_color),
                profile_photo = COALESCE(NULLIF(?, ''), profile_photo),
                updated_at = ?
            WHERE login = ?
        """, (email, password, role, full_name, avatar_color, profile_photo, now, login))
    else:
        conn.execute("""
            INSERT INTO users (login, email, password, role, full_name, avatar_color, profile_photo, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            login,
            email or "",
            password or "",
            role or "Ученик",
            full_name or login,
            avatar_color or "",
            profile_photo or "",
            now,
            now,
        ))

    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    conn.close()
    return user


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def safe_attr(obj, name, default=""):
    return getattr(obj, name, default) or default


def to_front_date(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def format_mark(value):
    if value is None:
        return ""
    return str(value)


def diary_to_frontend(diary):
    result = []
    schedule = getattr(diary, "schedule", []) or []

    for day in schedule:
        day_date = safe_attr(day, "day", date.today())
        lessons_result = []

        for lesson in getattr(day, "lessons", []) or []:
            subject = safe_attr(lesson, "subject", "Предмет")
            assignments = getattr(lesson, "assignments", []) or []

            homework = []
            marks = []
            details = []

            for assignment in assignments:
                content = safe_attr(assignment, "content", "")
                mark = getattr(assignment, "mark", None)

                if content:
                    homework.append(content)

                if mark is not None:
                    mark_value = format_mark(mark)
                    marks.append(mark_value)
                    details.append({
                        "value": mark_value,
                        "subject": subject,
                        "date": day_date.strftime("%d.%m.%Y") if hasattr(day_date, "strftime") else str(day_date),
                        "type": safe_attr(assignment, "type", "Не указано"),
                        "assignmentName": content,
                        "theme": content,
                        "teacher": "",
                        "comment": safe_attr(assignment, "comment", ""),
                        "id": safe_attr(assignment, "id", ""),
                    })

            lessons_result.append({
                "number": safe_attr(lesson, "number", ""),
                "subject": subject,
                "teacher": "",
                "theme": "",
                "room": safe_attr(lesson, "room", ""),
                "hw": homework,
                "homework": homework,
                "marks": marks,
                "details": details,
            })

        result.append({
            "date": to_front_date(day_date),
            "lessons": lessons_result,
        })

    return result


def build_report(days):
    subjects = []
    dates = []
    grid = {}

    for day in days:
        day_date = day.get("date")
        if day_date not in dates:
            dates.append(day_date)

        for lesson in day.get("lessons", []):
            subject = lesson.get("subject") or "Предмет"
            if subject not in subjects:
                subjects.append(subject)

            grid.setdefault(subject, {}).setdefault(day_date, [])
            for mark in lesson.get("marks", []):
                if mark:
                    grid[subject][day_date].append(mark)

    averages = {}

    for subject in subjects:
        values = []
        for day_date in dates:
            for mark in grid.get(subject, {}).get(day_date, []):
                try:
                    values.append(float(str(mark).replace(",", ".")))
                except ValueError:
                    pass

        averages[subject] = round(sum(values) / len(values), 2) if values else 0

    return {
        "subjects": subjects,
        "dates": dates,
        "grid": grid,
        "averages": averages,
    }


async def load_sgo_diary(login, password, start, end):
    if NetSchoolAPI is None:
        raise RuntimeError("netschoolapi не установлен")

    async with NetSchoolAPI(NETSCHOOL_URL) as ns:
        await ns.login(login, password, NETSCHOOL_SCHOOL)
        return await ns.diary(start=start, end=end)


async def load_sgo_announcements(login, password):
    if NetSchoolAPI is None:
        raise RuntimeError("netschoolapi не установлен")

    async with NetSchoolAPI(NETSCHOOL_URL) as ns:
        await ns.login(login, password, NETSCHOOL_SCHOOL)
        return await ns.announcements(take=50)


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "success": True,
        "message": "Backend работает",
        "netschool_url": NETSCHOOL_URL,
        "netschool_school": NETSCHOOL_SCHOOL,
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"success": True, "status": "ok"})


@app.route("/api/login", methods=["POST"])
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

    user = ensure_user(login_value, password, email, role, full_name, avatar_color, profile_photo)
    return jsonify({"success": True, "user": user_json(user)})


@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    if not login_value:
        return jsonify({"success": False, "error": "Введите логин"}), 400

    user = ensure_user(
        login=login_value,
        password=data.get("password") or "",
        email=(data.get("email") or "").strip(),
        role=(data.get("role") or "Ученик").strip(),
        full_name=(data.get("full_name") or login_value).strip(),
        avatar_color=data.get("avatar_color") or "",
        profile_photo=data.get("profile_photo") or "",
    )

    return jsonify({"success": True, "user": user_json(user)})


@app.route("/api/user_info", methods=["GET"])
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


@app.route("/api/users", methods=["GET"])
def users():
    conn = db()
    rows = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    conn.close()

    return jsonify({"success": True, "users": [user_json(row) for row in rows]})


@app.route("/api/diary", methods=["POST"])
def diary():
    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    password = data.get("password") or ""
    start_text = data.get("start")
    end_text = data.get("end")

    if not login_value or not password:
        return jsonify({"success": False, "error": "Не переданы логин или пароль СГО"}), 400

    try:
        start = parse_date(start_text) if start_text else date.today() - timedelta(days=date.today().weekday())
        end = parse_date(end_text) if end_text else start + timedelta(days=6)

        diary_data = run_async(load_sgo_diary(login_value, password, start, end))
        return jsonify({"success": True, "data": diary_to_frontend(diary_data)})
    except Exception as error:
        return jsonify({
            "success": False,
            "error": "Не удалось загрузить дневник из СГО",
            "details": str(error),
        }), 500


@app.route("/api/report", methods=["POST"])
def report():
    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    password = data.get("password") or ""
    start_text = data.get("start")
    end_text = data.get("end")

    if not login_value or not password:
        return jsonify({"success": False, "error": "Не переданы логин или пароль СГО"}), 400
    if not start_text or not end_text:
        return jsonify({"success": False, "error": "Не переданы даты"}), 400

    try:
        start = parse_date(start_text)
        end = parse_date(end_text)

        all_days = []
        cursor = start

        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=6), end)
            diary_data = run_async(load_sgo_diary(login_value, password, cursor, chunk_end))
            all_days.extend(diary_to_frontend(diary_data))
            cursor = chunk_end + timedelta(days=1)

        return jsonify({"success": True, "data": build_report(all_days)})
    except Exception as error:
        return jsonify({
            "success": False,
            "error": "Не удалось загрузить успеваемость из СГО",
            "details": str(error),
        }), 500


@app.route("/api/announcements", methods=["POST"])
def announcements():
    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    password = data.get("password") or ""

    if not login_value or not password:
        return jsonify({"success": False, "error": "Не переданы логин или пароль СГО"}), 400

    try:
        items = run_async(load_sgo_announcements(login_value, password))
        result = []

        for item in items:
            author = safe_attr(item, "author", None)
            result.append({
                "title": safe_attr(item, "name", "Объявление"),
                "content": safe_attr(item, "content", ""),
                "date": str(safe_attr(item, "post_date", "")),
                "author": safe_attr(author, "full_name", "Администрация") if author else "Администрация",
            })

        return jsonify({"success": True, "data": result})
    except Exception as error:
        return jsonify({
            "success": False,
            "error": "Не удалось загрузить объявления из СГО",
            "details": str(error),
        }), 500


@app.route("/api/messages", methods=["GET"])
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


@app.route("/api/send", methods=["POST"])
def send():
    data = request.get_json(silent=True) or {}

    sender = (data.get("sender") or "").strip()
    recipient = (data.get("recipient") or "").strip()
    text = (data.get("text") or "").strip()

    if not sender or not recipient or not text:
        return jsonify({"success": False, "error": "Заполните отправителя, получателя и текст"}), 400
    if sender == recipient:
        return jsonify({"success": False, "error": "Нельзя отправить сообщение самому себе"}), 400

    ensure_user(
        login=sender,
        password=data.get("sender_password") or "",
        role=data.get("sender_role") or "Ученик",
        full_name=data.get("sender_full_name") or sender,
    )

    conn = db()
    recipient_user = conn.execute("SELECT 1 FROM users WHERE login = ?", (recipient,)).fetchone()

    if not recipient_user:
        conn.close()
        return jsonify({
            "success": False,
            "error": "Получатель не найден. Он должен хотя бы один раз войти на сайт.",
        }), 404

    cursor = conn.execute("""
        INSERT INTO messages (sender, recipient, text, timestamp, read)
        VALUES (?, ?, ?, ?, 0)
    """, (sender, recipient, text, now_iso()))
    conn.commit()

    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
    conn.close()

    return jsonify({"success": True, "message": message_json(msg)})


@app.route("/api/mark_read", methods=["POST"])
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
