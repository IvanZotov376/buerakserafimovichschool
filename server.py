from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
import requests
import asyncio
try:
    from netschoolapi import NetSchoolAPI
except ImportError:
    NetSchoolAPI = None
from datetime import datetime, timezone, timedelta
import sqlite3
import ssl
import httpcore
import os
import re
import traceback
import smtplib
import time
import html
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

# Патч для sqlite3.Row
import sqlite3 as sqlite3_module
original_row = sqlite3_module.Row
class RowWithGet(original_row):
    def get(self, key, default=None):
        try:
            return self[key]
        except (IndexError, KeyError):
            return default
sqlite3_module.Row = RowWithGet

try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    pass

MOSCOW_TZ = timezone(timedelta(hours=3))
DEFAULT_SCHOOL = os.environ.get('SGO_SCHOOL', 'МКОУ школа №1 г.Серафимовича')
SGO_URL = os.environ.get('SGO_URL', 'https://sgo.volganet.ru/')

def moscow_now():
    return datetime.now(MOSCOW_TZ)

# SSL FIX
try:
    original_connect = httpcore._async.connection.AsyncHTTPConnection._connect
    async def patched_connect(self, request):
        self._ssl_context = ssl.create_default_context()
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE
        return await original_connect(self, request)
    httpcore._async.connection.AsyncHTTPConnection._connect = patched_connect
except Exception as e:
    print("SSL FIX error:", e)

requests.packages.urllib3.disable_warnings()

app = Flask(__name__)
CORS(app, resources={r'/api/*': {'origins': '*'}}, supports_credentials=False, allow_headers=['Content-Type'], methods=['GET', 'POST', 'OPTIONS'])
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

# ================== CONTACT FORM / EMAIL ==================
# Настройки отправки писем с формы контактов.
# Можно больше не вводить set ... в терминале: значения ниже используются по умолчанию.
# ВАЖНО: SMTP_PASSWORD — это пароль приложения Gmail, а не обычный пароль от почты.
# Замените строку "ВСТАВЬТЕ_СЮДА_ПАРОЛЬ_ПРИЛОЖЕНИЯ" на 16-значный пароль приложения без пробелов.
CONTACT_RECEIVER_EMAIL = os.environ.get("CONTACT_RECEIVER_EMAIL", "ivanzotov68@gmail.com")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "ivanzotov68@gmail.com")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "azuoywwofgcvygbx")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Сайт школы")
CONTACT_RATE_LIMIT_SECONDS = int(os.environ.get("CONTACT_RATE_LIMIT_SECONDS", "60"))
CONTACT_RATE_LIMIT = {}

print(f"SERVER STARTED (Moscow: {moscow_now().strftime('%d.%m.%Y %H:%M:%S')})")



@app.route("/", methods=["GET"])
def index_page():
    """Открывает главную страницу сайта прямо с Flask-сервера."""
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")

@app.route("/<path:filename>", methods=["GET"])
def static_files(filename):
    """Раздаёт рядом лежащие html/css/js/png файлы, чтобы не открывать через file://."""
    safe_ext = (".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".svg")
    if not filename.lower().endswith(safe_ext):
        return jsonify({"success": False, "error": "Файл не найден"}), 404
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)

@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "success": True,
        "status": "ok",
        "message": "server.py работает",
        "time": moscow_now().strftime("%d.%m.%Y %H:%M:%S")
    })



def get_requested_school(data=None):
    """Берёт школу из запроса или переменной окружения SGO_SCHOOL."""
    data = data or {}
    return (data.get("school") or request.args.get("school") or DEFAULT_SCHOOL).strip()

def require_netschoolapi():
    if NetSchoolAPI is None:
        raise RuntimeError(
            "Не установлен модуль netschoolapi. Установите зависимости: pip install netschoolapi flask flask-cors requests nest_asyncio"
        )

def run_async(coro):
    """Запускает async-код внутри Flask и не превращает ошибку СГО в HTML 500."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


# ================== DATABASE ==================
DATABASE = "school.db"

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3_module.connect(DATABASE)
        db.row_factory = sqlite3_module.Row
        db.execute("PRAGMA foreign_keys = ON")
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        
        db.execute('''CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        login TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL DEFAULT '123',
                        role TEXT NOT NULL DEFAULT 'Ученик',
                        email TEXT DEFAULT '',
                        full_name TEXT DEFAULT '',
                        avatar_color TEXT DEFAULT '',
                        profile_photo TEXT DEFAULT '',
                        created_at TEXT DEFAULT (datetime('now', 'localtime'))
                    )''')
        
        # Миграция для старых баз: добавляем поле фото профиля, если таблица users уже была создана раньше.
        user_columns = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
        if "profile_photo" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN profile_photo TEXT DEFAULT ''")
        if "email" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
        if "password" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN password TEXT NOT NULL DEFAULT '123'")
        
        db.execute('''CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sender TEXT NOT NULL,
                        recipient TEXT NOT NULL,
                        text TEXT NOT NULL,
                        timestamp TEXT DEFAULT (datetime('now', 'localtime')),
                        read INTEGER DEFAULT 0
                    )''')
        
        db.execute('''CREATE TABLE IF NOT EXISTS announcements (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        content TEXT,
                        date TEXT,
                        author TEXT,
                        source_url TEXT,
                        created_at TEXT DEFAULT (datetime('now', 'localtime'))
                    )''')
        
        db.execute('''CREATE TABLE IF NOT EXISTS teacher_cache (
                        assignment_id INTEGER PRIMARY KEY,
                        teacher_name TEXT,
                        cached_at TEXT DEFAULT (datetime('now', 'localtime'))
                    )''')
        
        db.commit()
        print("Database initialized")

init_db()

# ================== API AUTH ==================
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    user_login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    email = (data.get("email") or "").strip()
    role = (data.get("role") or "Ученик").strip()
    full_name = (data.get("full_name") or user_login).strip()
    avatar_color = data.get("avatar_color", "")
    profile_photo = data.get("profile_photo", "")

    if not user_login:
        return jsonify({"success": False, "error": "Введите логин"}), 400
    if not password:
        return jsonify({"success": False, "error": "Введите пароль"}), 400

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE login = ?", (user_login,)).fetchone()

    if user:
        db.execute(
            """
            UPDATE users
            SET password = ?,
                email = COALESCE(NULLIF(?, ''), email),
                role = COALESCE(NULLIF(?, ''), role),
                full_name = COALESCE(NULLIF(?, ''), full_name),
                avatar_color = COALESCE(NULLIF(?, ''), avatar_color),
                profile_photo = COALESCE(NULLIF(?, ''), profile_photo)
            WHERE login = ?
            """,
            (password, email, role, full_name or user_login, avatar_color, profile_photo, user_login)
        )
    else:
        db.execute(
            """
            INSERT INTO users (login, password, role, email, full_name, avatar_color, profile_photo)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_login, password, role or "Ученик", email, full_name or user_login, avatar_color, profile_photo)
        )

    db.commit()
    user = db.execute("SELECT * FROM users WHERE login = ?", (user_login,)).fetchone()

    return jsonify({
        "success": True,
        "user": {
            "login": user["login"],
            "email": user.get("email") or email,
            "role": user.get("role") or role or "Ученик",
            "full_name": user.get("full_name") or user_login,
            "avatar_color": user.get("avatar_color") or avatar_color,
            "profile_photo": user.get("profile_photo") or profile_photo
        }
    })

@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    email = (data.get("email") or "").strip()
    role = data.get("role", "Ученик")
    full_name = data.get("full_name", login)
    avatar_color = data.get("avatar_color", "")
    profile_photo = data.get("profile_photo", "")

    if not login:
        return jsonify({"success": False, "error": "Логин обязателен"}), 400
    if not password:
        return jsonify({"success": False, "error": "Пароль обязателен"}), 400
    if not email:
        return jsonify({"success": False, "error": "Email обязателен"}), 400

    db = get_db()
    existing = db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()

    if existing:
        db.execute(
            """
            UPDATE users
            SET password = ?, email = ?, role = ?, full_name = ?, avatar_color = ?, profile_photo = ?
            WHERE login = ?
            """,
            (password, email, role, full_name or login, avatar_color, profile_photo, login)
        )
    else:
        db.execute(
            """
            INSERT INTO users (login, password, role, email, full_name, avatar_color, profile_photo)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (login, password, role, email, full_name or login, avatar_color, profile_photo)
        )

    db.commit()
    user = db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()

    return jsonify({
        "success": True,
        "user": {
            "login": user["login"],
            "email": user.get("email") or "",
            "role": user.get("role") or "Ученик",
            "full_name": user.get("full_name") or user["login"],
            "avatar_color": user.get("avatar_color") or "",
            "profile_photo": user.get("profile_photo") or ""
        }
    })

@app.route("/api/user_info", methods=["GET"])
def get_user_info():
    login = request.args.get("login")
    if not login:
        return jsonify({"success": False, "error": "Не указан логин"}), 400
    
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    
    if user:
        return jsonify({"success": True, "user": {"login": user["login"], "role": user.get("role"), "full_name": user.get("full_name") or user["login"], "avatar_color": user.get("avatar_color") or "", "profile_photo": user.get("profile_photo") or ""}})
    else:
        db.execute("INSERT INTO users (login, password, role, full_name) VALUES (?, '123', 'Ученик', ?)", (login, login))
        db.commit()
        return jsonify({"success": True, "user": {"login": login, "role": "Ученик", "full_name": login, "profile_photo": ""}})

# ================== API MESSAGES ==================
@app.route("/api/messages", methods=["GET"])
def get_messages():
    login = request.args.get("login")
    if not login:
        return jsonify({"success": False, "error": "Не указан логин"}), 400
    db = get_db()
    cursor = db.execute("SELECT * FROM messages WHERE sender = ? OR recipient = ? ORDER BY timestamp ASC", (login, login))
    return jsonify({"success": True, "data": [dict(row) for row in cursor.fetchall()]})

@app.route("/api/send", methods=["POST"])
def send_message():
    data = request.json
    sender = data.get("sender")
    recipient = data.get("recipient")
    text = data.get("text", "").strip()
    if not sender or not recipient or not text:
        return jsonify({"success": False, "error": "Не все поля заполнены"}), 400
    db = get_db()
    cursor = db.execute("INSERT INTO messages (sender, recipient, text, timestamp, read) VALUES (?, ?, ?, datetime('now', 'localtime'), 0)", (sender, recipient, text))
    db.commit()
    return jsonify({"success": True, "id": cursor.lastrowid})

@app.route("/api/mark_read", methods=["POST"])
def mark_read():
    data = request.json
    db = get_db()
    db.execute("UPDATE messages SET read = 1 WHERE sender = ? AND recipient = ? AND read = 0", (data.get("partner"), data.get("login")))
    db.commit()
    return jsonify({"success": True})

# ===================== DIARY =====================

TYPE_NAMES = {
    1: "Домашняя работа", 2: "Классная работа", 3: "Домашнее задание",
    4: "Контрольная работа", 5: "Лабораторная работа", 6: "Практическая работа",
    7: "Самостоятельная работа", 8: "Тест", 9: "Зачёт", 10: "Диктант",
    11: "Сочинение", 12: "Изложение", 13: "Проект", 14: "Реферат", 15: "Ответ на уроке",
    16: "ВПР", 17: "Практикум", 18: "Срезовая работа", 19: "Итоговая работа", 20: "Проверочная работа"
}

def get_cookies_from_ns(ns):
    """Извлекает cookies из NetSchoolAPI"""
    for attr in ('cookies', '_cookies'):
        obj = getattr(ns, attr, None)
        if isinstance(obj, dict):
            return obj
    client = getattr(ns, 'client', None) or getattr(ns, '_client', None)
    if client and hasattr(client, 'cookies'):
        return {k: v for k, v in client.cookies.items()}
    return {}

def fetch_teacher_sync(assignment_id, cookies_dict):
    """Получает учителя через синхронный requests"""
    db = get_db()
    cached = db.execute("SELECT teacher_name FROM teacher_cache WHERE assignment_id = ?", (assignment_id,)).fetchone()
    if cached and cached["teacher_name"]:
        return cached["teacher_name"]
    
    try:
        headers = {
            "Accept": "application/json",
            "Referer": "https://sgo.volganet.ru/app/school/studentdiary/"
        }
        url = f"https://sgo.volganet.ru/webapi/assignments/{assignment_id}"
        print(f"Fetching teacher for assignment {assignment_id}...")
        resp = requests.get(url, cookies=cookies_dict, headers=headers, verify=False, timeout=5)
        
        print(f"Response status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"Assignment data: {data}")
            teachers = data.get("teachers", [])
            if teachers:
                teacher_name = teachers[0].get("name", "")
                print(f"Found teacher: {teacher_name}")
                db.execute("INSERT OR REPLACE INTO teacher_cache (assignment_id, teacher_name, cached_at) VALUES (?, ?, datetime('now', 'localtime'))", (assignment_id, teacher_name))
                db.commit()
                return teacher_name
            else:
                print(f"No teachers found in response")
        else:
            print(f"Error response: {resp.text[:200]}")
    except Exception as e:
        print(f"Teacher fetch error for {assignment_id}: {e}")
        traceback.print_exc()
    
    return None



def get_assignment_id(assign):
    """ID задания/работы в СГО."""
    return first_nonempty(
        getattr(assign, "id", None),
        deep_get(assign, "id", "assignmentId", "assignment_id", "workId", "work_id")
    )

def parse_teacher_from_detail(data):
    """Достаёт реального учителя из ответа /webapi/assignments/{id}."""
    if not data:
        return ""

    # Самые частые варианты СГО
    teacher = first_nonempty(
        data.get("teacherName") if isinstance(data, dict) else None,
        data.get("teacher") if isinstance(data, dict) else None,
        data.get("authorName") if isinstance(data, dict) else None,
        data.get("createdByName") if isinstance(data, dict) else None,
        data.get("userName") if isinstance(data, dict) else None,
    )
    if teacher:
        return teacher

    # teachers: [{name: "..."}]
    teachers = data.get("teachers", []) if isinstance(data, dict) else []
    teacher = first_nonempty(teachers)
    if teacher:
        return teacher

    # Вложенные структуры
    for key in ("teacher", "author", "createdBy", "user", "person"):
        nested = data.get(key) if isinstance(data, dict) else None
        teacher = first_nonempty(
            nested.get("name") if isinstance(nested, dict) else None,
            nested.get("fullName") if isinstance(nested, dict) else None,
            nested.get("fio") if isinstance(nested, dict) else None,
        )
        if teacher:
            return teacher

    return ""

def parse_assignment_theme_from_detail(data):
    """Достаёт тему/название именно задания, за которое поставлена оценка."""
    if not data:
        return ""

    theme = first_nonempty(
        data.get("theme") if isinstance(data, dict) else None,
        data.get("topic") if isinstance(data, dict) else None,
        data.get("title") if isinstance(data, dict) else None,
        data.get("name") if isinstance(data, dict) else None,
        data.get("assignmentName") if isinstance(data, dict) else None,
        data.get("workName") if isinstance(data, dict) else None,
        data.get("content") if isinstance(data, dict) else None,
        data.get("description") if isinstance(data, dict) else None,
        data.get("text") if isinstance(data, dict) else None,
    )
    if theme:
        return clean_html_text(theme)

    for key in ("assignment", "work", "lessonAssignment", "task"):
        nested = data.get(key) if isinstance(data, dict) else None
        theme = first_nonempty(
            nested.get("theme") if isinstance(nested, dict) else None,
            nested.get("topic") if isinstance(nested, dict) else None,
            nested.get("title") if isinstance(nested, dict) else None,
            nested.get("name") if isinstance(nested, dict) else None,
            nested.get("content") if isinstance(nested, dict) else None,
            nested.get("description") if isinstance(nested, dict) else None,
            nested.get("text") if isinstance(nested, dict) else None,
        )
        if theme:
            return clean_html_text(theme)

    return ""

def parse_type_from_detail(data):
    if not data:
        return ""
    type_name = first_nonempty(
        data.get("typeName") if isinstance(data, dict) else None,
        data.get("assignmentTypeName") if isinstance(data, dict) else None,
        data.get("workTypeName") if isinstance(data, dict) else None,
    )
    if type_name:
        return type_name

    for key in ("type", "assignmentType", "workType"):
        nested = data.get(key) if isinstance(data, dict) else None
        type_name = first_nonempty(
            nested.get("name") if isinstance(nested, dict) else None,
            nested.get("title") if isinstance(nested, dict) else None,
        )
        if type_name:
            return type_name
    return ""

def fetch_assignment_detail_sync(assignment_id, cookies_dict):
    """Получает реальные сведения о задании из СГО: тема задания и учитель."""
    if not assignment_id:
        return {}

    try:
        headers = {
            "Accept": "application/json",
            "Referer": "https://sgo.volganet.ru/app/school/studentdiary/",
            "User-Agent": "Mozilla/5.0"
        }
        url = f"https://sgo.volganet.ru/webapi/assignments/{assignment_id}"
        resp = requests.get(url, cookies=cookies_dict, headers=headers, verify=False, timeout=8)

        if resp.status_code != 200:
            print(f"Assignment detail {assignment_id}: HTTP {resp.status_code} {resp.text[:200]}")
            return {}

        data = resp.json()
        return {
            "teacher": parse_teacher_from_detail(data),
            "theme": parse_assignment_theme_from_detail(data),
            "type": parse_type_from_detail(data),
            "raw": data
        }
    except Exception as e:
        print(f"Assignment detail fetch error for {assignment_id}: {e}")
        traceback.print_exc()
        return {}


def obj_to_dict(obj):
    """Преобразует объект netschoolapi / dataclass / dict в обычный dict для безопасного чтения."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        from dataclasses import asdict, is_dataclass
        if is_dataclass(obj):
            return asdict(obj)
    except Exception:
        pass
    try:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
    except Exception:
        pass
    try:
        if hasattr(obj, "dict"):
            return obj.dict()
    except Exception:
        pass
    d = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
            if not callable(value):
                d[name] = value
        except Exception:
            pass
    return d

def deep_get(obj, *names):
    """Ищет поле в объекте/словаре, включая camelCase/snake_case и вложенные dict."""
    if obj is None:
        return None

    data = obj_to_dict(obj)
    for name in names:
        if isinstance(data, dict) and name in data:
            return data[name]
        if hasattr(obj, name):
            try:
                return getattr(obj, name)
            except Exception:
                pass

    # регистронезависимый поиск
    lowered = {str(k).lower(): k for k in data.keys()} if isinstance(data, dict) else {}
    for name in names:
        key = lowered.get(str(name).lower())
        if key is not None:
            return data[key]

    return None

def first_nonempty(*values):
    for value in values:
        if value is None:
            continue

        if isinstance(value, (list, tuple, set)):
            for item in value:
                found = first_nonempty(
                    item.get("name") if isinstance(item, dict) else None,
                    item.get("fullName") if isinstance(item, dict) else None,
                    getattr(item, "name", None),
                    getattr(item, "fullName", None),
                    item if isinstance(item, str) else None,
                )
                if found:
                    return found
            continue

        if isinstance(value, dict):
            found = first_nonempty(
                value.get("name"),
                value.get("fullName"),
                value.get("value"),
                value.get("text"),
                value.get("content"),
                value.get("description"),
            )
            if found:
                return found
            continue

        text = str(value).strip()
        if text and text.lower() not in ("none", "null"):
            return text
    return ""

def assignment_content(assign):
    """Достаёт текст задания максимально широко: у netschoolapi/SГО названия полей отличаются по версиям."""
    data = obj_to_dict(assign)

    direct = first_nonempty(
        deep_get(assign, "content", "Content"),
        deep_get(assign, "name", "assignmentName", "AssignmentName"),
        deep_get(assign, "text", "Text"),
        deep_get(assign, "description", "Description"),
        deep_get(assign, "homework", "homeWork", "HomeWork"),
        deep_get(assign, "task", "Task"),
        deep_get(assign, "value", "Value"),
    )
    if direct:
        return clean_html_text(direct)

    # Иногда текст лежит во вложенных структурах
    for key in ("assignment", "work", "homework", "task", "lessonAssignment"):
        nested = data.get(key) if isinstance(data, dict) else None
        nested_text = first_nonempty(
            deep_get(nested, "content", "name", "text", "description", "value")
        )
        if nested_text:
            return clean_html_text(nested_text)

    return ""

def clean_html_text(text):
    text = str(text or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&laquo;", "«").replace("&raquo;", "»")
    text = re.sub(r"\s+", " ", text).strip()
    return text

def assignment_type_id(assign):
    raw = first_nonempty(
        deep_get(assign, "typeId", "type_id", "assignmentTypeId", "workTypeId", "typeID"),
        deep_get(deep_get(assign, "type", "assignmentType", "workType"), "id", "typeId")
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0

def assignment_type_name(assign):
    type_id = assignment_type_id(assign)
    explicit = first_nonempty(
        deep_get(assign, "typeName", "type_name", "assignmentTypeName", "workTypeName"),
        deep_get(deep_get(assign, "type", "assignmentType", "workType"), "name", "title")
    )
    return explicit or TYPE_NAMES.get(type_id, "")

def assignment_mark_value(assign):
    mark = deep_get(assign, "mark", "Mark", "grade", "Grade", "value")
    if isinstance(mark, dict):
        return first_nonempty(mark.get("mark"), mark.get("value"), mark.get("grade"), mark.get("name"))
    # Не считаем текст задания оценкой
    if isinstance(mark, str) and not re.fullmatch(r"[1-5][+-]?", mark.strip()):
        return None
    return mark

def lesson_theme_value(lesson):
    return first_nonempty(
        deep_get(lesson, "theme", "lessonTheme", "topic", "themeName", "title"),
        deep_get(deep_get(lesson, "theme", "topic"), "name", "title", "value"),
    )

def lesson_homework_values(lesson, lesson_theme=""):
    """Домашнее задание может быть на уровне урока, а не assignment."""
    result = []
    for field in ("homework", "homeWork", "homeTask", "homeAssignment", "assignment", "assignmentsText"):
        value = deep_get(lesson, field)
        if isinstance(value, (list, tuple)):
            for item in value:
                text = first_nonempty(item, assignment_content(item))
                text = clean_homework_text(text, lesson_theme)
                if text and text not in result:
                    result.append(text)
        else:
            text = clean_homework_text(value, lesson_theme)
            if text and text not in result:
                result.append(text)
    return result

def clean_homework_text(content, lesson_theme=""):
    content = clean_html_text(content)
    if not content:
        return ""

    lines = []
    for line in re.split(r"[\r\n]+", content):
        line = clean_html_text(line)
        if not line:
            continue
        # Убираем служебные строки темы, но не само ДЗ
        if re.match(r"^(тема|тема\s+урока)\s*[:\-–—]", line, flags=re.I):
            continue
        lines.append(line)

    text = " ".join(lines).strip()
    if not text:
        return ""
    if lesson_theme and text.lower() == clean_html_text(lesson_theme).lower():
        return ""
    return text

def homework_text(assign, lesson_theme=""):
    """Возвращает реальное ДЗ. Важное изменение: если тип не распознан, но оценки нет — считаем это ДЗ."""
    content = assignment_content(assign)
    if not content:
        return ""

    type_id = assignment_type_id(assign)
    type_name = (assignment_type_name(assign) or "").lower()
    mark = assignment_mark_value(assign)

    is_homework = (
        type_id in (1, 3)
        or "домаш" in type_name
        or "дз" == type_name.strip()
    )

    # В netschoolapi/SГО типы часто не приходят. Тогда assignment без оценки почти всегда является домашним заданием.
    if not is_homework and mark is None and type_id == 0:
        is_homework = True

    # Если это явно контрольная/практическая с оценкой — не показываем как ДЗ.
    if not is_homework:
        return ""

    return clean_homework_text(content, lesson_theme)



def mark_student_id(assign):
    mark = deep_get(assign, "mark", "Mark")
    if isinstance(mark, dict):
        return first_nonempty(mark.get("studentId"), mark.get("student_id"))
    return first_nonempty(
        deep_get(mark, "studentId", "student_id"),
        deep_get(assign, "studentId", "student_id")
    )


# ================== MARK DETAILS: DIARY-CONTEXT METHOD ==================
# Другой способ: не восстанавливаем сведения об оценке через отдельные webapi endpoints,
# а берём их из того же объекта дневника, где оценка уже привязана к конкретному уроку.
# Приоритет: mark object -> assignment object -> lesson context.

def mark_object(assign):
    mark = deep_get(assign, "mark", "Mark", "grade", "Grade")
    return mark if mark is not None else {}

def read_field(obj, *keys):
    """Безопасно читает поле из dict/dataclass/pydantic/обычного объекта."""
    if obj is None:
        return ""
    for key in keys:
        val = deep_get(obj, key)
        text = first_nonempty(val)
        if text:
            return clean_html_text(text)
    return ""

def read_nested_field(obj, *keys):
    """Ищет поле не только сверху, но и во вложенных dict/list. Используется осторожно."""
    if obj is None:
        return ""
    stack = [obj_to_dict(obj)]
    seen = 0
    wanted = {k.lower() for k in keys}
    while stack and seen < 120:
        seen += 1
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in wanted:
                    text = first_nonempty(v)
                    if text:
                        return clean_html_text(text)
                if isinstance(v, (dict, list, tuple)):
                    stack.append(v)
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
    return ""

def infer_work_type(text=""):
    t = (text or "").lower()
    rules = [
        ("контрольн", "Контрольная работа"),
        ("самостоят", "Самостоятельная работа"),
        ("провероч", "Проверочная работа"),
        ("практичес", "Практическая работа"),
        ("лаборатор", "Лабораторная работа"),
        ("тест", "Тест"),
        ("диктант", "Диктант"),
        ("сочинен", "Сочинение"),
        ("изложен", "Изложение"),
        ("проект", "Проект"),
        ("зач", "Зачёт"),
        ("домаш", "Домашняя работа"),
        ("ответ", "Ответ на уроке"),
    ]
    for needle, name in rules:
        if needle in t:
            return name
    return "Работа на уроке"

def build_mark_detail_from_diary_context(assign, lesson, day_date):
    """Главный способ формирования деталей оценки без отдельного assignInfo запроса."""
    mark_obj = mark_object(assign)
    mark_value = assignment_mark_value(assign)
    aid = get_assignment_id(assign)

    lesson_subject = first_nonempty(
        getattr(lesson, "subject", None),
        deep_get(lesson, "subjectName"),
        deep_get(lesson, "subject")
    )
    lesson_teacher = first_nonempty(
        getattr(lesson, "teacherName", None),
        getattr(lesson, "teacher", None),
        getattr(lesson, "teachers", None),
        read_nested_field(lesson, "teacherName", "teacherFullName", "teachersStr", "fio", "fullName")
    )
    lesson_theme = lesson_theme_value(lesson)

    subject = first_nonempty(
        read_field(mark_obj, "subjectName", "subject", "disciplineName"),
        read_field(assign, "subjectName", "subject", "disciplineName"),
        lesson_subject,
    )
    teacher = first_nonempty(
        read_field(mark_obj, "teacherName", "teacherFullName", "teachersStr", "employeeName", "fio"),
        read_field(assign, "teacherName", "teacherFullName", "teachersStr", "employeeName", "fio"),
        lesson_teacher,
        "Не указан",
    )
    theme = first_nonempty(
        read_field(mark_obj, "assignmentName", "workName", "taskName", "theme", "topic", "title", "name", "comment"),
        read_field(assign, "assignmentName", "workName", "taskName", "theme", "topic", "title", "name"),
        assignment_content(assign),
        lesson_theme,
        "Не указана",
    )
    type_name = first_nonempty(
        read_field(mark_obj, "typeName", "assignmentTypeName", "workTypeName", "markTypeName"),
        assignment_type_name(assign),
        infer_work_type(theme),
    )

    return {
        "value": str(mark_value).strip(),
        "type": type_name,
        "teacher": teacher,
        "theme": theme,
        "assignmentName": theme,
        "date": day_date,
        "subject": subject,
        "assignment_id": str(aid or ""),
        "lesson_theme": lesson_theme or "",
        "source": "diary-object-context",
    }

# ================== /MARK DETAILS: DIARY-CONTEXT METHOD ==================

def parse_assign_info_payload(payload):
    """Парсит JSON того же окна, что открывается в СГО при клике на оценку."""
    if not payload:
        return {}

    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    assign_info = {}
    assign = {}
    result = {}

    if isinstance(data, dict):
        assign_info = data.get("assignInfo") or data.get("assignmentInfo") or data
        assign = data.get("assign") or data.get("assignment") or data
        result = data.get("result") or data.get("mark") or {}

    teacher = first_nonempty(
        assign_info.get("teachersStr") if isinstance(assign_info, dict) else None,
        assign_info.get("teacherName") if isinstance(assign_info, dict) else None,
        assign_info.get("teacher") if isinstance(assign_info, dict) else None,
        assign_info.get("teacherFio") if isinstance(assign_info, dict) else None,
        assign_info.get("teacherFullName") if isinstance(assign_info, dict) else None,
    )

    theme = first_nonempty(
        assign_info.get("assignmentName") if isinstance(assign_info, dict) else None,
        assign_info.get("problemName") if isinstance(assign_info, dict) else None,
        assign_info.get("name") if isinstance(assign_info, dict) else None,
        assign_info.get("theme") if isinstance(assign_info, dict) else None,
        assign_info.get("topic") if isinstance(assign_info, dict) else None,
        assign.get("assignmentName") if isinstance(assign, dict) else None,
        assign.get("name") if isinstance(assign, dict) else None,
    )

    type_name = first_nonempty(
        assign.get("typeName") if isinstance(assign, dict) else None,
        assign_info.get("typeName") if isinstance(assign_info, dict) else None,
    )

    mark_value = first_nonempty(
        result.get("mark") if isinstance(result, dict) else None,
        result.get("value") if isinstance(result, dict) else None,
    )

    return {
        "teacher": clean_html_text(teacher),
        "theme": clean_html_text(theme),
        "type": clean_html_text(type_name),
        "mark": str(mark_value).strip() if mark_value is not None else "",
        "raw": payload
    }

def fetch_sgo_assign_info_sync(assignment_id, student_id, cookies_dict):
    """
    Получает реальные сведения из того же XHR, что СГО вызывает при клике на оценку.
    В Network этот запрос обычно отображается как: <assignmentId>?studentId=<studentId>
    """
    if not assignment_id:
        return {}

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://sgo.volganet.ru/app/school/studentdiary/",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest"
    }

    sid_q = f"?studentId={student_id}" if student_id else ""

    candidates = [
        # Самый вероятный endpoint: в Network имя выглядит как "232098747?studentId=1019750"
        f"https://sgo.volganet.ru/webapi/assignments/{assignment_id}{sid_q}",

        # Дополнительные варианты разных сборок СГО
        f"https://sgo.volganet.ru/webapi/student/diary/assignInfo/{assignment_id}{sid_q}",
        f"https://sgo.volganet.ru/webapi/studentDiary/assignInfo/{assignment_id}{sid_q}",
        f"https://sgo.volganet.ru/webapi/studentdiary/assignInfo/{assignment_id}{sid_q}",
        f"https://sgo.volganet.ru/webapi/student/diary/assignments/{assignment_id}{sid_q}",
        f"https://sgo.volganet.ru/webapi/studentDiary/assignments/{assignment_id}{sid_q}",
    ]

    if student_id:
        candidates += [
            f"https://sgo.volganet.ru/webapi/student/diary/assignInfo?assignmentId={assignment_id}&studentId={student_id}",
            f"https://sgo.volganet.ru/webapi/studentDiary/assignInfo?assignmentId={assignment_id}&studentId={student_id}",
            f"https://sgo.volganet.ru/webapi/studentdiary/assignInfo?assignmentId={assignment_id}&studentId={student_id}",
        ]

    for url in candidates:
        try:
            print(f"Fetching assignInfo: {url}")
            resp = requests.get(url, cookies=cookies_dict, headers=headers, verify=False, timeout=8)
            print(f"assignInfo status {resp.status_code}")

            if resp.status_code != 200:
                continue

            try:
                payload = resp.json()
            except Exception:
                print(f"assignInfo non-json: {resp.text[:120]}")
                continue

            parsed = parse_assign_info_payload(payload)

            # Даже если учителя нет, theme/type могут быть полезны.
            if parsed.get("teacher") or parsed.get("theme") or parsed.get("type"):
                print("assignInfo parsed:",
                      "teacher=", parsed.get("teacher"),
                      "theme=", (parsed.get("theme") or "")[:80],
                      "type=", parsed.get("type"))
                return parsed
        except Exception as e:
            print(f"assignInfo fetch error for {assignment_id}: {e}")

    return {}



@app.route("/api/sgo/check", methods=["POST", "OPTIONS"])
def sgo_check():
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    data = request.get_json(silent=True) or {}
    login_val = (data.get("login") or "").strip()
    password_val = (data.get("password") or "").strip()
    school = get_requested_school(data)

    if not login_val or not password_val:
        return jsonify({"success": False, "error": "Не переданы логин или пароль"}), 400

    async def _fetch():
        require_netschoolapi()
        ns = NetSchoolAPI(SGO_URL)
        try:
            await ns.login(login_val, password_val, school)
            try:
                await ns.logout()
            except Exception:
                pass
            return jsonify({"success": True, "message": "Подключение к СГО успешно", "school": school})
        except Exception as e:
            traceback.print_exc()
            return jsonify({
                "success": False,
                "error": str(e),
                "school": school,
                "hint": "Проверьте логин, пароль и точное название школы. Можно задать школу через переменную окружения SGO_SCHOOL."
            }), 500

    return run_async(_fetch())

@app.route("/api/diary", methods=["POST", "OPTIONS"])
def diary_api():
    if request.method == "OPTIONS":
        return jsonify({"success": True})

    data = request.get_json(silent=True) or {}
    login_val = (data.get("login") or "").strip()
    password_val = (data.get("password") or "").strip()
    start = data.get("start")
    end = data.get("end")
    school = get_requested_school(data)

    if not login_val or not password_val:
        return jsonify({"success": False, "error": "Не переданы логин или пароль"}), 400
    if not start or not end:
        return jsonify({"success": False, "error": "Не указан период start/end"}), 400

    async def _fetch():
        ns = None
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            print(f"Logging in as {login_val}...")
            await ns.login(login_val, password_val, school)
            print("Login successful!")

            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")

            print(f"Fetching diary from {start} to {end}...")
            diary = await ns.diary(start_dt, end_dt)
            print(f"Got {len(diary.schedule)} days")

            res = []
            for day in diary.schedule:
                day_date = day.day if isinstance(day.day, str) else day.day.strftime("%Y-%m-%d")
                day_data = {"date": day_date, "lessons": []}

                for lesson in getattr(day, "lessons", []) or []:
                    lesson_subject = first_nonempty(
                        getattr(lesson, "subject", None),
                        deep_get(lesson, "subjectName"),
                        deep_get(lesson, "subject")
                    )

                    lesson_teacher = first_nonempty(
                        getattr(lesson, "teacherName", None),
                        getattr(lesson, "teacher", None),
                        getattr(lesson, "teachers", None),
                        read_nested_field(lesson, "teacherName", "teacherFullName", "teachersStr", "fio", "fullName"),
                        "Не указан",
                    )

                    lesson_theme = lesson_theme_value(lesson)

                    hw_list = []
                    for hw0 in lesson_homework_values(lesson, lesson_theme):
                        if hw0 and hw0 not in hw_list:
                            hw_list.append(hw0)

                    marks_details = []

                    for assign in getattr(lesson, "assignments", []) or []:
                        hw = homework_text(assign, lesson_theme)
                        if hw and hw not in hw_list:
                            hw_list.append(hw)

                        mark_value = assignment_mark_value(assign)
                        if mark_value is None or not str(mark_value).strip():
                            continue

                        marks_details.append(build_mark_detail_from_diary_context(assign, lesson, day_date))

                    day_data["lessons"].append({
                        "subject": lesson_subject,
                        "room": getattr(lesson, "room", "") or "",
                        "hw": hw_list,
                        "details": marks_details,
                        "teacher": lesson_teacher,
                        "theme": lesson_theme,
                    })

                res.append(day_data)

            await ns.logout()
            print(f"Diary data ready: {len(res)} days")
            return jsonify({"success": True, "data": res})

        except Exception as e:
            print(f"Diary error: {e}")
            traceback.print_exc()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            return jsonify({"success": False, "error": str(e), "school": school}), 500

    try:
        return run_async(_fetch())
    except Exception as e:
        print(f"Diary fatal error: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e), "school": school}), 500


@app.route("/api/sgo/diary", methods=["POST", "OPTIONS"])
def diary_api_alias():
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    return diary_api()


@app.route("/api/debug/diary", methods=["POST"])
def debug_diary_api():
    """Показывает сырые поля уроков/заданий, чтобы быстро проверить, где СГО отдаёт ДЗ."""
    data = request.json or {}
    login_val = data.get("login")
    password_val = data.get("password")
    start = data.get("start")
    end = data.get("end")
    school = get_requested_school(data)

    async def _fetch():
        require_netschoolapi()
        ns = NetSchoolAPI(SGO_URL)
        try:
            await ns.login(login_val, password_val, school)
            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")
            diary = await ns.diary(start_dt, end_dt)
            out = []
            for day in diary.schedule:
                day_date = day.day if isinstance(day.day, str) else day.day.strftime("%Y-%m-%d")
                item = {"date": day_date, "lessons": []}
                for lesson in day.lessons:
                    lesson_theme = lesson_theme_value(lesson)
                    lesson_info = {
                        "subject": getattr(lesson, "subject", ""),
                        "theme": lesson_theme,
                        "lesson_keys": list(obj_to_dict(lesson).keys())[:80],
                        "lesson_homework": lesson_homework_values(lesson, lesson_theme),
                        "assignments": []
                    }
                    for assign in getattr(lesson, "assignments", []) or []:
                        lesson_info["assignments"].append({
                            "keys": list(obj_to_dict(assign).keys())[:80],
                            "raw": {k: str(v)[:300] for k, v in obj_to_dict(assign).items() if k in ("id","typeId","type_id","typeName","name","content","text","description","mark","homework","assignmentName")},
                            "content": assignment_content(assign),
                            "type_id": assignment_type_id(assign),
                            "type_name": assignment_type_name(assign),
                            "mark": str(assignment_mark_value(assign)),
                            "homework_text": homework_text(assign, lesson_theme),
                            "assignment_id": get_assignment_id(assign),
                        })
                    item["lessons"].append(lesson_info)
                out.append(item)
            await ns.logout()
            return jsonify({"success": True, "data": out})
        except Exception as e:
            traceback.print_exc()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            return jsonify({"success": False, "error": str(e), "school": school}), 500

    return run_async(_fetch())



@app.route("/api/debug/mark-details", methods=["POST"])
def debug_mark_details():
    """Возвращает только оценки с темой/учителем — удобно проверять без интерфейса."""
    response = diary_api()
    return response


# ===================== REPORT =====================
@app.route("/api/report", methods=["POST"])
def report():
    data = request.get_json(silent=True) or {}
    login_val = (data.get("login") or "").strip()
    password_val = (data.get("password") or "").strip()
    start = data.get("start")
    end = data.get("end")
    school = get_requested_school(data)

    if not login_val or not password_val:
        return jsonify({"success": False, "error": "Не переданы логин или пароль"}), 400
    if not start or not end:
        return jsonify({"success": False, "error": "Не указан период start/end"}), 400

    async def _fetch():
        require_netschoolapi()
        ns = NetSchoolAPI(SGO_URL)
        try:
            await ns.login(login_val, password_val, school)
            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")

            from collections import defaultdict
            grid = defaultdict(lambda: defaultdict(list))
            all_dates = set()

            cur = start_dt
            while cur <= end_dt:
                week_end = min(cur + timedelta(days=6), end_dt)
                try:
                    diary = await ns.diary(cur, week_end)
                    for day in diary.schedule:
                        date_str = day.day if isinstance(day.day, str) else day.day.strftime("%Y-%m-%d")
                        all_dates.add(date_str)
                        for lesson in day.lessons:
                            subject = lesson.subject
                            for assign in lesson.assignments:
                                mark = getattr(assign, 'mark', None)
                                if mark is not None:
                                    try:
                                        mark_value = mark.get('mark') if isinstance(mark, dict) else mark
                                        mark_val = str(int(mark_value))
                                        grid[subject][date_str].append(mark_val)
                                    except (ValueError, TypeError):
                                        pass
                except Exception as week_err:
                    print(f"Week error: {week_err}")
                cur += timedelta(days=7)

            sorted_dates = sorted(list(all_dates))
            subjects = sorted(grid.keys())

            averages = {}
            for subj in subjects:
                total = sum(int(m) for d in sorted_dates for m in grid[subj].get(d, []))
                count = sum(len(grid[subj].get(d, [])) for d in sorted_dates)
                averages[subj] = round(total / count, 2) if count else 0

            result = {
                "subjects": subjects,
                "dates": sorted_dates,
                "grid": {s: {d: grid[s].get(d, []) for d in sorted_dates} for s in subjects},
                "averages": averages,
            }

            await ns.logout()
            return jsonify({"success": True, "data": result})

        except Exception as e:
            print("Report error:", e)
            traceback.print_exc()
            try:
                await ns.logout()
            except Exception:
                pass
            return jsonify({"success": False, "error": str(e), "school": school}), 500

    return run_async(_fetch())


# ===================== CONTACT FORM =====================
@app.route("/api/send_contact", methods=["POST", "OPTIONS"])
def send_contact():
    if request.method == "OPTIONS":
        return jsonify({"success": True})

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    sender_email = (data.get("email") or "").strip()
    message = (data.get("message") or "").strip()
    website = (data.get("website") or "").strip()  # honeypot-поле для ботов

    if website:
        return jsonify({"success": False, "error": "Сообщение отклонено защитой от спама"}), 400

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    now = time.time()
    last_sent = CONTACT_RATE_LIMIT.get(client_ip, 0)
    if now - last_sent < CONTACT_RATE_LIMIT_SECONDS:
        wait_seconds = int(CONTACT_RATE_LIMIT_SECONDS - (now - last_sent))
        return jsonify({
            "success": False,
            "error": f"Слишком частая отправка. Повторите через {wait_seconds} сек."
        }), 429

    if not name or not sender_email or not message:
        return jsonify({"success": False, "error": "Заполните имя, email и сообщение"}), 400

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", sender_email):
        return jsonify({"success": False, "error": "Введите корректный email"}), 400


    if (not SMTP_USER or not SMTP_PASSWORD or SMTP_PASSWORD == "ВСТАВЬТЕ_СЮДА_ПАРОЛЬ_ПРИЛОЖЕНИЯ"):
        return jsonify({
            "success": False,
            "error": "SMTP не настроен. Вставьте пароль приложения Gmail в переменную SMTP_PASSWORD в server.py"
        }), 500

    safe_name = html.escape(name)
    safe_email = html.escape(sender_email)
    safe_message = html.escape(message).replace("\n", "<br>")
    subject = f"Сообщение с сайта школы от {name[:60]}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_USER))
    msg["To"] = CONTACT_RECEIVER_EMAIL
    msg["Reply-To"] = sender_email

    plain_text = f"""Новое сообщение с сайта школы

Имя: {name}
Email: {sender_email}
IP: {client_ip}

Сообщение:
{message}
"""
    html_text = f"""
    <h2>Новое сообщение с сайта школы</h2>
    <p><b>Имя:</b> {safe_name}</p>
    <p><b>Email:</b> {safe_email}</p>
    <p><b>IP:</b> {html.escape(client_ip)}</p>
    <hr>
    <p><b>Сообщение:</b></p>
    <p>{safe_message}</p>
    """

    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_text, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        CONTACT_RATE_LIMIT[client_ip] = now
        return jsonify({"success": True, "message": "Сообщение отправлено"})
    except Exception as e:
        print("MAIL ERROR:", e)
        traceback.print_exc()
        return jsonify({"success": False, "error": "Не удалось отправить письмо. Проверьте SMTP-настройки."}), 500


# ===================== ANNOUNCEMENTS =====================
@app.route("/api/announcements", methods=["POST"])
def get_announcements():
    try:
        db = get_db()
        rows = db.execute("SELECT title, content, date, author FROM announcements ORDER BY id DESC LIMIT 20").fetchall()
        cached = [dict(r) for r in rows] if rows else []
        return jsonify({"success": True, "data": cached, "cached": True})
    except Exception as e:
        return jsonify({"success": True, "data": [], "cached": True})


if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"Server: http://0.0.0.0:5000")
    print(f"Local:  http://127.0.0.1:5000")
    print(f"{'='*50}\n")
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False, threaded=True)
