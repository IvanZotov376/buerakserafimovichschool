import os
import asyncio
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, jsonify, request
from flask_cors import CORS

from netschoolapi import NetSchoolAPI


app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "users.db"

NETSCHOOL_URL = os.environ.get("NETSCHOOL_URL", "https://sgo.volganet.ru").rstrip("/")
NETSCHOOL_SCHOOL = os.environ.get("NETSCHOOL_SCHOOL", "Буракская СШ")


def run_async(coro):
    return asyncio.run(coro)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def has_col(conn, table, col):
    return any(row["name"] == col for row in conn.execute(f"PRAGMA table_info({table})"))


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

    for col, sql in [
        ("email", "ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''"),
        ("password", "ALTER TABLE users ADD COLUMN password TEXT DEFAULT ''"),
        ("role", "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'Ученик'"),
        ("full_name", "ALTER TABLE users ADD COLUMN full_name TEXT"),
        ("avatar_color", "ALTER TABLE users ADD COLUMN avatar_color TEXT DEFAULT ''"),
        ("profile_photo", "ALTER TABLE users ADD COLUMN profile_photo TEXT DEFAULT ''"),
    ]:
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


def ensure_local_user(login_value, password="", email="", role="Ученик", full_name="", avatar_color="", profile_photo=""):
    login_value = (login_value or "").strip()
    if not login_value:
        return None

    conn = db()
    user = conn.execute("SELECT * FROM users WHERE login = ?", (login_value,)).fetchone()
    now = now_iso()

    if user:
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
        """, (email, password, role, full_name, avatar_color, profile_photo, now, login_value))
    else:
        conn.execute("""
            INSERT INTO users (login, email, password, role, full_name, avatar_color, profile_photo, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            login_value,
            email or "",
            password or "",
            role or "Ученик",
            full_name or login_value,
            avatar_color or "",
            profile_photo or "",
            now,
            now,
        ))

    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE login = ?", (login_value,)).fetchone()
    conn.close()
    return user


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def dt_to_str(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def lesson_hw(assignments) -> List[str]:
    result = []
    for a in assignments or []:
        content = getattr(a, "content", "") or ""
        if content:
            result.append(content)
    return result


def assignment_to_detail(a, subject: str, lesson_day: date) -> Dict[str, Any]:
    mark = getattr(a, "mark", None)
    return {
        "value": "" if mark is None else str(mark),
        "subject": subject,
        "date": lesson_day.strftime("%d.%m.%Y"),
        "type": getattr(a, "type", "") or "Не указано",
        "assignmentName": getattr(a, "content", "") or "",
        "theme": getattr(a, "content", "") or "",
        "teacher": "",
        "comment": getattr(a, "comment", "") or "",
        "deadline": dt_to_str(getattr(a, "deadline", "")),
        "id": getattr(a, "id", ""),
    }


def diary_to_frontend(diary) -> List[Dict[str, Any]]:
    days = []
    for day in getattr(diary, "schedule", []) or []:
        day_date = getattr(day, "day", None)
        lessons = []
        for lesson in getattr(day, "lessons", []) or []:
            subject = getattr(lesson, "subject", "") or "Предмет"
            assignments = getattr(lesson, "assignments", []) or []
            details = []
            marks = []
            for a in assignments:
                mark = getattr(a, "mark", None)
                if mark is not None:
                    marks.append(str(mark))
                    details.append(assignment_to_detail(a, subject, day_date))
            lessons.append({
                "number": getattr(lesson, "number", ""),
                "subject": subject,
                "teacher": "",
                "theme": "",
                "room": getattr(lesson, "room", "") or "",
                "start": dt_to_str(getattr(lesson, "start", "")),
                "end": dt_to_str(getattr(lesson, "end", "")),
                "hw": lesson_hw(assignments),
                "homework": lesson_hw(assignments),
                "marks": marks,
                "details": details,
            })
        days.append({
            "date": day_date.isoformat() if hasattr(day_date, "isoformat") else str(day_date),
            "lessons": lessons,
        })
    return days


def build_report(days: List[Dict[str, Any]]) -> Dict[str, Any]:
    subjects = []
    dates = []
    grid: Dict[str, Dict[str, List[str]]] = {}

    for day in days:
        d = day["date"]
        if d not in dates:
            dates.append(d)

        for lesson in day.get("lessons", []):
            subj = lesson.get("subject") or "Предмет"
            if subj not in subjects:
                subjects.append(subj)

            grid.setdefault(subj, {}).setdefault(d, [])
            for mark in lesson.get("marks", []):
                if mark not in ("", None):
                    grid[subj][d].append(str(mark))

    averages = {}
    for subj in subjects:
        vals = []
        for d in dates:
            for m in grid.get(subj, {}).get(d, []):
                try:
                    vals.append(float(str(m).replace(",", ".")))
                except ValueError:
                    pass
        averages[subj] = round(sum(vals) / len(vals), 2) if vals else 0

    return {
        "subjects": subjects,
        "dates": dates,
        "grid": grid,
        "averages": averages,
    }


async def fetch_sgo_diary(login_value: str, password: str, start: date, end: date):
    async with NetSchoolAPI(NETSCHOOL_URL, default_requests_timeout=25) as ns:
        await ns.login(login_value, password, NETSCHOOL_SCHOOL, requests_timeout=25)
        return await ns.diary(start=start, end=end, requests_timeout=25)


async def fetch_sgo_announcements(login_value: str, password: str):
    async with NetSchoolAPI(NETSCHOOL_URL, default_requests_timeout=25) as ns:
        await ns.login(login_value, password, NETSCHOOL_SCHOOL, requests_timeout=25)
        return await ns.announcements(take=50, requests_timeout=25)


@app.get("/")
def index():
    return jsonify({
        "success": True,
        "message": "Backend работает",
        "netschool_url": NETSCHOOL_URL,
        "netschool_school": NETSCHOOL_SCHOOL,
    })


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

    # Проверяем, что данные СГО настоящие. Если СГО временно не отвечает, локальный вход всё равно сохраняем.
    try:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        run_async(fetch_sgo_diary(login_value, password, monday, monday + timedelta(days=1)))
    except Exception as e:
        # Не блокируем вход в личный кабинет, но сервер позже вернёт ошибку СГО при загрузке дневника.
        print("SGO login check warning:", repr(e))

    user = ensure_local_user(login_value, password, email, role, full_name, avatar_color, profile_photo)
    return jsonify({"success": True, "message": "Вход выполнен", "user": user_json(user)})


@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    login_value = (data.get("login") or "").strip()

    if not login_value:
        return jsonify({"success": False, "error": "Введите логин"}), 400

    user = ensure_local_user(
        login_value=login_value,
        password=data.get("password") or "",
        email=(data.get("email") or "").strip(),
        role=(data.get("role") or "Ученик").strip(),
        full_name=(data.get("full_name") or login_value).strip(),
        avatar_color=data.get("avatar_color") or "",
        profile_photo=data.get("profile_photo") or "",
    )
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


@app.get("/api/users")
def users():
    conn = db()
    rows = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify({"success": True, "users": [user_json(row) for row in rows]})


@app.post("/api/diary")
def diary_endpoint():
    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    password = data.get("password") or ""
    start_s = data.get("start")
    end_s = data.get("end")

    if not login_value or not password:
        return jsonify({"success": False, "error": "Не переданы логин или пароль СГО"}), 400

    try:
        start = parse_date(start_s) if start_s else date.today() - timedelta(days=date.today().weekday())
        end = parse_date(end_s) if end_s else start + timedelta(days=6)
        diary_data = run_async(fetch_sgo_diary(login_value, password, start, end))
        return jsonify({"success": True, "data": diary_to_frontend(diary_data)})
    except Exception as e:
        return jsonify({
            "success": False,
            "error": "Не удалось загрузить дневник из СГО",
            "details": str(e),
        }), 500


@app.post("/api/report")
def report_endpoint():
    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    password = data.get("password") or ""
    start_s = data.get("start")
    end_s = data.get("end")

    if not login_value or not password:
        return jsonify({"success": False, "error": "Не переданы логин или пароль СГО"}), 400
    if not start_s or not end_s:
        return jsonify({"success": False, "error": "Не переданы даты отчёта"}), 400

    try:
        start = parse_date(start_s)
        end = parse_date(end_s)

        all_days = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=6), end)
            diary_data = run_async(fetch_sgo_diary(login_value, password, cursor, chunk_end))
            all_days.extend(diary_to_frontend(diary_data))
            cursor = chunk_end + timedelta(days=1)

        return jsonify({"success": True, "data": build_report(all_days)})
    except Exception as e:
        return jsonify({
            "success": False,
            "error": "Не удалось загрузить успеваемость из СГО",
            "details": str(e),
        }), 500


@app.post("/api/announcements")
def announcements_endpoint():
    data = request.get_json(silent=True) or {}

    login_value = (data.get("login") or "").strip()
    password = data.get("password") or ""

    if not login_value or not password:
        return jsonify({"success": False, "error": "Не переданы логин или пароль СГО"}), 400

    try:
        announcements = run_async(fetch_sgo_announcements(login_value, password))
        result = []
        for ann in announcements:
            author = getattr(ann, "author", None)
            result.append({
                "title": getattr(ann, "name", "") or "Объявление",
                "content": getattr(ann, "content", "") or "",
                "date": dt_to_str(getattr(ann, "post_date", "")),
                "author": getattr(author, "full_name", "") if author else "Администрация",
            })
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({
            "success": False,
            "error": "Не удалось загрузить объявления из СГО",
            "details": str(e),
        }), 500


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

    ensure_local_user(
        sender,
        password=data.get("sender_password") or "",
        role=data.get("sender_role") or "Ученик",
        full_name=data.get("sender_full_name") or sender,
    )

    conn = db()
    recipient_user = conn.execute("SELECT 1 FROM users WHERE login = ?", (recipient,)).fetchone()
    if not recipient_user:
        conn.close()
        return jsonify({"success": False, "error": "Получатель не найден. Он должен хотя бы один раз войти на сайт."}), 404

    cur = conn.execute("""
        INSERT INTO messages (sender, recipient, text, timestamp, read)
        VALUES (?, ?, ?, ?, 0)
    """, (sender, recipient, text, now_iso()))
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
