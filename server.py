from flask import Flask, request, jsonify, g, send_from_directory, Response
from flask_cors import CORS
from werkzeug.utils import secure_filename
import requests
import asyncio
try:
    import netschoolapi as netschoolapi_client
    from netschoolapi import NetSchoolAPI
except Exception as e:
    # netschoolapi может падать не только с ImportError, но и с TypeError
    # из-за несовместимых зависимостей, например typing_extensions + Python 3.12.
    netschoolapi_client = None
    NetSchoolAPI = None
    NETSCHOOLAPI_IMPORT_ERROR = e
else:
    NETSCHOOLAPI_IMPORT_ERROR = None
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
import json
import base64
import uuid
import builtins
import importlib.metadata
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from urllib.parse import quote as url_quote

try:
    import werkzeug
    if not hasattr(werkzeug, "__version__"):
        werkzeug.__version__ = importlib.metadata.version("werkzeug")
except Exception:
    pass

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
STUDENT_PARENT_SCHOOL = 'МКОУ школа №1 г.Серафимовича'
STAFF_SCHOOL = 'МКОУ Буерак-Поповская СШ'
ALT_SCHOOL_1 = 'МБОУ школа №1 г.Серафимовича'
DEFAULT_SCHOOL = os.environ.get('SGO_SCHOOL', STUDENT_PARENT_SCHOOL)

# В СГО школа должна совпадать с названием из справочника.
# Ниже — варианты, которые встречаются на sgo.volganet.ru и в документах школ.
SCHOOL_OPTIONS = [
    STUDENT_PARENT_SCHOOL,
    STAFF_SCHOOL,
    ALT_SCHOOL_1,
]

SCHOOL_ALIASES = {
    'мкоу школа №1 г. серафимовича': [
        'МКОУ школа №1 г.Серафимовича',
        'МБОУ школа №1 г.Серафимовича',
        'МКОУ школа №1 г. Серафимовича',
        'МБОУ школа №1 г. Серафимовича',
        'МКОУ школа №1 города Серафимович',
        'МКОУ школа № 1 города Серафимовича',
        'МКОУ школа №1 города Серафимовича',
    ],
    'мкоу школа №1 г.серафимовича': [
        'МКОУ школа №1 г.Серафимовича',
        'МБОУ школа №1 г.Серафимовича',
        'МКОУ школа №1 г. Серафимовича',
        'МБОУ школа №1 г. Серафимовича',
        'МКОУ школа №1 города Серафимович',
        'МКОУ школа № 1 города Серафимовича',
        'МКОУ школа №1 города Серафимовича',
    ],
    'мбоу школа №1 г.серафимовича': [
        'МБОУ школа №1 г.Серафимовича',
        'МКОУ школа №1 г.Серафимовича',
        'МБОУ школа №1 г. Серафимовича',
        'МКОУ школа №1 г. Серафимовича',
        'МБОУ школа №1 города Серафимович',
        'МКОУ школа №1 города Серафимович',
    ],
    'мкоу буерак-поповская скш': [
        'МКОУ Буерак-Поповская СШ',
        'МКОУ Буерак-Поповская CШ',
        'МКОУ Буерак-Поповская СКШ',
        'МКОУ Буерак-Поповская CШ',
    ],
    'мкоу буерак-поповская сш': [
        'МКОУ Буерак-Поповская СШ',
        'МКОУ Буерак-Поповская CШ',
        'МКОУ Буерак-Поповская СКШ',
        'МКОУ Буерак-Поповская CШ',
    ],
}
SGO_URL = os.environ.get('SGO_URL', 'https://sgo.volganet.ru/')
# Если вы знаете реальный эндпоинт журнала из DevTools браузера — укажите его:
#   set SGO_TEACHER_JOURNAL_ENDPOINTS=/webapi/journals
#   set SGO_TEACHER_FILTER_ENDPOINTS=/webapi/subjectgroups

# In-process кэш субъект-групп (сбрасывается при перезапуске сервера)
_sg_map_cache = {}  # {login -> sg_map}

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


def safe_console_print(*args, **kwargs):
    try:
        builtins.print(*args, **kwargs)
    except OSError:
        pass

# В Windows при запуске Flask из некоторых IDE/терминалов обычный print()
# может падать с [WinError 233] и ломать API. Делаем все последующие print безопасными.
print = safe_console_print

def safe_print_traceback():
    try:
        safe_console_print(traceback.format_exc())
    except Exception:
        pass

app = Flask(__name__)
CORS(app, resources={r'/api/*': {'origins': '*'}}, supports_credentials=False, allow_headers=['Content-Type'], methods=['GET', 'POST', 'OPTIONS', 'DELETE'])
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024


# ================== ROBUST API ERROR/CORS HANDLING ==================
# Эти обработчики нужны, чтобы браузер не получал HTML-ошибку Flask/traceback.
# Теперь все ошибки внутри /api/* возвращаются как JSON, а CORS-заголовки
# добавляются даже к ошибочным ответам.
@app.after_request
def add_api_headers(response):
    if request.path.startswith('/api/'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.errorhandler(Exception)
def api_exception_handler(error):
    if request.path.startswith('/api/'):
        safe_console_print(f"API fatal error on {request.path}: {error}")
        safe_print_traceback()
        return jsonify({
            'success': False,
            'error': str(error),
            'error_type': type(error).__name__,
            'path': request.path,
            'hint': 'Сервер работает, но внутри API произошла ошибка. Смотрите traceback в терминале.'
        }), 200
    raise error

@app.route('/api/ping', methods=['GET', 'OPTIONS'])
def api_ping():
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    return jsonify({'success': True, 'status': 'pong', 'time': moscow_now().strftime('%d.%m.%Y %H:%M:%S')})


# ================== CONTACT FORM / EMAIL ==================
# Отправка уведомления о входе БЕЗ EmailJS и других JS-сервисов.
# Письмо отправляет сам server.py через SMTP почтового ящика-отправителя.
#
# ВАЖНО: чтобы письма реально уходили в интернет, у сайта должен быть
# почтовый ящик-отправитель. Для Gmail/Яндекс/Mail.ru обычно нужен
# "пароль приложения", а не обычный пароль от почты.
#
# Можно указать значения прямо здесь или через переменные окружения:
# SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_NAME.
CONTACT_RECEIVER_EMAIL = os.environ.get("CONTACT_RECEIVER_EMAIL", "ivanzotov68@gmail.com")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "ivanzotov68@gmail.com")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "fpnvzvdraaimtpue")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Сайт школы")
CONTACT_RATE_LIMIT_SECONDS = int(os.environ.get("CONTACT_RATE_LIMIT_SECONDS", "60"))
CONTACT_RATE_LIMIT = {}

print(f"SERVER STARTED (Moscow: {moscow_now().strftime('%d.%m.%Y %H:%M:%S')})")



def is_valid_email_address(value):
    """Простая проверка email для серверной части."""
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (value or "").strip()))

def normalize_role(value):
    return (value or "").strip().lower()

def cabinet_url_for_role(value):
    role = normalize_role(value)
    if role in ("\u0443\u0447\u0435\u043d\u0438\u043a", "\u0440\u043e\u0434\u0438\u0442\u0435\u043b\u044c"):
        return "lk.html"
    if role in ("\u0443\u0447\u0438\u0442\u0435\u043b\u044c", "\u043f\u0440\u0435\u043f\u043e\u0434\u0430\u0432\u0430\u0442\u0435\u043b\u044c"):
        return "lkteacher.html"
    if role in ("\u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440", "\u0434\u0438\u0440\u0435\u043a\u0442\u043e\u0440", "\u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f"):
        return "lkadmin.html"
    if role == "\u0433\u043e\u0441\u0442\u044c":
        return "index.html"
    return ""


def send_login_success_email(to_email, user_login, role, school):
    """Отправляет письмо пользователю после успешного входа через SMTP."""
    to_email = (to_email or "").strip()
    if not is_valid_email_address(to_email):
        return False, "Некорректный email получателя"

    if not SMTP_HOST or not SMTP_PORT:
        return False, "SMTP_HOST или SMTP_PORT не настроены в server.py"
    if not SMTP_USER or not SMTP_PASSWORD or SMTP_PASSWORD == "ВСТАВЬТЕ_СЮДА_ПАРОЛЬ_ПРИЛОЖЕНИЯ":
        return False, "SMTP_USER или SMTP_PASSWORD не настроены в server.py"

    login_time = moscow_now().strftime("%d.%m.%Y %H:%M:%S")
    subject = "Успешный вход в личный кабинет"

    plain_text = f"""Успешный вход в личный кабинет
Здравствуйте!

Выполнен успешный вход в личный кабинет.

Логин: {user_login}
Роль: {role}
Школа: {school}
Время входа: {login_time} (МСК)
Если это были не вы, измените пароль и обратитесь к администратору.
"""

    safe_login = html.escape(user_login or "")
    safe_role = html.escape(role or "")
    safe_school = html.escape(school or "")
    safe_time = html.escape(login_time)

    html_text = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.5; color: #1f2937;">
        <h2>Успешный вход в личный кабинет</h2>
        <p>Здравствуйте!</p>
        <p>Выполнен успешный вход в личный кабинет.</p>
        <p>
          <b>Логин:</b> {safe_login}<br>
          <b>Роль:</b> {safe_role}<br>
          <b>Школа:</b> {safe_school}<br>
          <b>Время входа:</b> {safe_time} (МСК)
        </p>
        <p>Если это были не вы, измените пароль и обратитесь к администратору.</p>
      </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_USER))
    msg["To"] = to_email
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_text, "html", "utf-8"))

    try:
        if int(SMTP_PORT) == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.sendmail(SMTP_USER, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.sendmail(SMTP_USER, [to_email], msg.as_string())
        print(f"LOGIN MAIL SENT to {to_email} for login {user_login}")
        return True, "Письмо о входе отправлено"
    except smtplib.SMTPAuthenticationError:
        print("LOGIN MAIL ERROR: SMTP authentication failed")
        safe_print_traceback()
        return False, "SMTP отклонил логин/пароль отправителя. Нужен пароль приложения."
    except smtplib.SMTPRecipientsRefused:
        print(f"LOGIN MAIL ERROR: recipient refused: {to_email}")
        safe_print_traceback()
        return False, "Почтовый сервер отклонил адрес получателя."
    except smtplib.SMTPSenderRefused:
        print(f"LOGIN MAIL ERROR: sender refused: {SMTP_USER}")
        safe_print_traceback()
        return False, "Почтовый сервер отклонил адрес отправителя SMTP_USER."
    except TimeoutError:
        print("LOGIN MAIL ERROR: SMTP timeout")
        safe_print_traceback()
        return False, "Истекло время подключения к SMTP."
    except Exception as e:
        print("LOGIN MAIL ERROR:", e)
        safe_print_traceback()
        return False, f"Не удалось отправить письмо через SMTP: {type(e).__name__}: {e}"


@app.route("/", methods=["GET"])
def index_page():
    """Открывает главную страницу сайта прямо с Flask-сервера."""
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")

@app.route("/<path:filename>", methods=["GET"])
def static_files(filename):
    """Раздаёт рядом лежащие html/css/js/png файлы, чтобы не открывать через file://."""
    safe_ext = ('.html', '.css', '.js', '.png', '.jpg', '.jpeg', '.webp', '.ico', '.svg', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt')
    if not filename.lower().endswith(safe_ext):
        return jsonify({"success": False, "error": "Файл не найден"}), 404
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)



# ================== FOOD MENU AUTO UPDATE ==================
FOOD_SOURCE_URL = "https://buerak.oshkole.ru/food/"
FOOD_LOCAL_DIR = "food_files"
FOOD_MAX_FILES = 10
FOOD_ALLOWED_EXTENSIONS = (".xlsx", ".xls", ".pdf", ".doc", ".docx")
FOOD_CACHE_TTL_SECONDS = 180
_food_menu_cache = {"time": 0.0, "files": []}


def _food_project_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _food_local_dir():
    path = os.path.join(_food_project_dir(), FOOD_LOCAL_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def _food_date_from_filename(filename):
    match = re.search(r"(20\d{2})[-_.](\d{2})[-_.](\d{2})", filename or "")
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _food_title(filename):
    date_value = _food_date_from_filename(filename)
    if date_value:
        yyyy, mm, dd = date_value.split("-")
        return f"Меню на {dd}.{mm}.{yyyy}"
    return os.path.splitext(filename or "Документ меню")[0]


def _is_daily_food_file(filename):
    """Оставляем только ежедневные файлы меню, исключая findex/kp/tm и служебные книги."""
    name = (filename or "").strip().lower()
    if not name.endswith(FOOD_ALLOWED_EXTENSIONS):
        return False
    return bool(_food_date_from_filename(name))


def _find_food_files_on_source():
    """Быстро получает 10 последних добавленных файлов прямо со страницы food.

    На странице https://buerak.oshkole.ru/food/ блок называется «Последние файлы».
    Поэтому порядок берём именно из HTML страницы, а не сортируем по дате имени файла:
    если на сайте файл добавлен позже, он должен появиться выше даже при другой дате меню.
    """
    from urllib.parse import urljoin, unquote

    response = requests.get(FOOD_SOURCE_URL, timeout=8, verify=False, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    })
    response.raise_for_status()
    page = response.text

    links = []
    pattern = r'href=["\']([^"\']+\.(?:xlsx|xls|pdf|docx?|XLSX|XLS|PDF|DOCX?)(?:\?[^"\']*)?)["\']'
    for href in re.findall(pattern, page, flags=re.I):
        url = urljoin(FOOD_SOURCE_URL, html.unescape(href).strip())
        filename = os.path.basename(unquote(url.split("?", 1)[0])).strip()
        if not _is_daily_food_file(filename):
            continue
        links.append({"filename": filename, "source_url": url})

    # Сохраняем порядок сайта, но убираем дубликаты.
    result = []
    seen = set()
    for item in links:
        key = item["filename"].lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= FOOD_MAX_FILES:
            break

    return result


def _download_food_file(item):
    local_dir = _food_local_dir()
    original_filename = item["filename"]
    filename = secure_filename(original_filename) or original_filename.replace("/", "_").replace("\\", "_")
    target_path = os.path.join(local_dir, filename)
    if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
        return filename

    download_url = item["source_url"].replace(" ", "%20")
    response = requests.get(download_url, timeout=12, verify=False, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    response.raise_for_status()
    with open(target_path, "wb") as file:
        file.write(response.content)
    return filename


def _local_food_files(limit=FOOD_MAX_FILES):
    local_files = []
    try:
        for filename in os.listdir(_food_local_dir()):
            if _is_daily_food_file(filename):
                local_files.append({
                    "title": _food_title(filename),
                    "filename": filename,
                    "url": f"/{FOOD_LOCAL_DIR}/{filename}",
                    "date": _food_date_from_filename(filename)
                })
        local_files.sort(key=lambda item: item.get("date") or "", reverse=True)
        return local_files[:limit]
    except Exception:
        return []


def refresh_food_menu_files(force=False):
    """Быстро проверяет страницу питания и возвращает 10 последних файлов.

    ВАЖНО: при открытии food.html больше не скачиваем файлы в папку проекта и
    не удаляем их. Live Server/браузерные расширения часто перезагружают страницу
    при любом изменении файлов в проекте; из-за автоскачивания меню получался
    цикл постоянного обновления страницы. Теперь endpoint только читает страницу
    https://buerak.oshkole.ru/food/ и отдаёт прямые ссылки на последние файлы.
    """
    now_ts = time.time()
    cached_files = _food_menu_cache.get("files") or []
    cached_at = float(_food_menu_cache.get("time") or 0)
    if not force and cached_files and now_ts - cached_at < FOOD_CACHE_TTL_SECONDS:
        return cached_files

    latest = _find_food_files_on_source()
    result = []

    for position, item in enumerate(latest[:FOOD_MAX_FILES], start=1):
        filename = item["filename"]
        result.append({
            "title": _food_title(filename),
            "filename": filename,
            "url": item["source_url"],
            "source_url": item["source_url"],
            "date": _food_date_from_filename(filename),
            "position": position
        })

    _food_menu_cache["time"] = now_ts
    _food_menu_cache["files"] = result
    return result


@app.route("/api/food-menu", methods=["GET", "OPTIONS"])
def api_food_menu():
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    try:
        files = refresh_food_menu_files(force=(request.args.get("force") == "1"))
        return jsonify({
            "success": True,
            "files": files,
            "checked_at": moscow_now().strftime("%d.%m.%Y %H:%M"),
            "source_url": FOOD_SOURCE_URL,
            "count": len(files)
        })
    except Exception as e:
        safe_console_print("FOOD MENU UPDATE ERROR:", e)
        safe_print_traceback()
        local_files = _local_food_files()
        return jsonify({
            "success": bool(local_files),
            "files": local_files,
            "error": str(e),
            "checked_at": moscow_now().strftime("%d.%m.%Y %H:%M"),
            "source_url": FOOD_SOURCE_URL,
            "count": len(local_files)
        })
# ================== /FOOD MENU AUTO UPDATE ==================

@app.route("/api/health", methods=["GET", "OPTIONS"])
def api_health():
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    return jsonify({
        "success": True,
        "status": "ok",
        "message": "server.py работает",
        "time": moscow_now().strftime("%d.%m.%Y %H:%M:%S")
    })




def normalize_school_name(value):
    value = (value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value

def school_candidates(school):
    """Возвращает расширенный список вариантов школы для входа в СГО.

    409 Conflict на /webapi/login часто появляется, когда логин/пароль верные,
    но выбран другой scid учреждения. Поэтому для школы №1 пробуем и МКОУ, и МБОУ,
    а также варианты с/без пробелов и с "Средняя школа"/"СШ".
    """
    school = normalize_school_name(school)
    variants = []
    def add(value):
        value = normalize_school_name(value)
        if value and value not in variants:
            variants.append(value)

    add(school)
    key = school.lower()
    key_no_space_after_g = key.replace("г. ", "г.")
    for k in (key, key_no_space_after_g):
        for item in SCHOOL_ALIASES.get(k, []):
            add(item)

    lower = school.lower()
    if "серафимович" in lower and "№1" in lower:
        for item in [
            'МКОУ школа №1 г.Серафимовича',
            'МКОУ школа №1 г. Серафимовича',
            'МБОУ школа №1 г.Серафимовича',
            'МБОУ школа №1 г. Серафимовича',
            'МБОУ Средняя школа №1 г.Серафимовича',
            'МБОУ Средняя школа № 1 г. Серафимовича',
            'МБОУ СШ №1 г.Серафимовича',
            'МБОУ СШ №1 г. Серафимовича',
        ]:
            add(item)
    if "буерак-попов" in lower:
        for item in SCHOOL_ALIASES.get("мкоу буерак-поповская скш", []):
            add(item)

    for item in SCHOOL_OPTIONS:
        add(item)
    add(DEFAULT_SCHOOL)
    return variants or [DEFAULT_SCHOOL]

def get_requested_school(data=None):
    """Берёт школу из запроса/БД, а не выбирает только по роли."""
    data = data or {}
    requested = normalize_school_name(data.get("school") or request.args.get("school") or "")
    login = (data.get("login") or request.args.get("login") or "").strip()
    role = (data.get("role") or request.args.get("role") or "").strip()

    if requested:
        return requested

    if login:
        try:
            db = get_db()
            user = db.execute("SELECT role, school FROM users WHERE login = ?", (login,)).fetchone()
            if user:
                if user.get("school"):
                    return normalize_school_name(user.get("school"))
                if not role:
                    role = user.get("role", "") or ""
        except Exception:
            pass

    role_normalized = role.strip().lower()
    if role_normalized in ("ученик", "родитель"):
        return STUDENT_PARENT_SCHOOL
    if role_normalized in ("преподаватель", "учитель", "администратор", "администрация"):
        return STAFF_SCHOOL

    return DEFAULT_SCHOOL

def apply_saved_login_fields(login, password="", school=""):
    """Дополняет пароль/школу из users, если браузер их не передал.

    Важно: login.html раньше мог отправить технический пароль local-password,
    когда поле пароля оставили пустым. Его нельзя использовать для СГО и нельзя
    затирать им реальный пароль в school.db.
    """
    login = (login or "").strip()
    password = (password or "").strip()
    school = normalize_school_name(school)
    if not login:
        return password, school
    try:
        user = get_db().execute("SELECT password, school FROM users WHERE login = ?", (login,)).fetchone()
    except Exception:
        user = None
    if user:
        saved_password = (user.get("password") or "").strip()
        if (not password or password == "local-password") and saved_password and saved_password != "local-password":
            password = saved_password
        if not school and user.get("school"):
            school = normalize_school_name(user.get("school"))
    return password, school

async def sgo_login_with_fallback(ns, login_val, password_val, school, allow_teacher=False):
    """Пробует войти в СГО с выбранной школой и её допустимыми вариантами.

    Для преподавателей netschoolapi после успешной авторизации может попытаться
    открыть ученический endpoint /webapi/student/diary/init. СГО отвечает на него
    401 Unauthorized, хотя cookies авторизованной учительской сессии уже получены.
    В учительских запросах allow_teacher=True разрешает такой 401 и продолжает
    работу через страницу /app/school/journal/ и teacher/journal endpoints.
    """
    last_error = None
    tried = []
    for candidate in school_candidates(school):
        tried.append(candidate)
        try:
            await ns.login(login_val, password_val, candidate)
            return candidate
        except Exception as e:
            last_error = e
            err_text = str(e)
            if allow_teacher and "401 Unauthorized" in err_text and "/webapi/student/diary/init" in err_text:
                print("Teacher account detected: student diary init returned 401, continue with journal cookies")
                return candidate
            # SchoolNotFoundError можно безопасно пробовать со следующим названием.
            if e.__class__.__name__ == "SchoolNotFoundError":
                continue
            # 409/AuthError в СГО часто означает несовпадение школы с логином.
            # Поэтому пробуем остальные варианты названия школы, а не падаем сразу.
            if e.__class__.__name__ == "AuthError" or "409 Conflict" in err_text:
                if len(tried) < len(school_candidates(school)):
                    continue
                raise RuntimeError(
                    "СГО отклонил вход. Проверьте логин, пароль и школу. Пробовали: " + "; ".join(tried)
                ) from e
            # Если школа найдена, но ошибка другая — дальше пробовать смысла нет.
            raise
    raise RuntimeError(
        "Школа не найдена в СГО. Пробовали: " + "; ".join(tried)
    ) from last_error

def require_netschoolapi():
    if NetSchoolAPI is None:
        details = f" Ошибка импорта: {type(NETSCHOOLAPI_IMPORT_ERROR).__name__}: {NETSCHOOLAPI_IMPORT_ERROR}" if 'NETSCHOOLAPI_IMPORT_ERROR' in globals() and NETSCHOOLAPI_IMPORT_ERROR else ""
        raise RuntimeError(
            "Не удалось загрузить модуль netschoolapi. Установите/обновите зависимости: "
            "python -m pip install -U typing_extensions netschoolapi flask flask-cors requests nest_asyncio"
            + details
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "school.db")

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
                        school TEXT DEFAULT '',
                        class_name TEXT DEFAULT '',
                        class_name TEXT DEFAULT '',
                        created_at TEXT DEFAULT (datetime('now', 'localtime'))
                    )''')
        
        # Миграция для старых баз: добавляем поле фото профиля, если таблица users уже была создана раньше.
        user_columns = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
        if "profile_photo" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN profile_photo TEXT DEFAULT ''")
        if "school" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN school TEXT DEFAULT ''")
        if "class_name" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN class_name TEXT DEFAULT ''")
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
                        read INTEGER DEFAULT 0,
                        attachments_json TEXT DEFAULT '[]'
                    )''')
        message_columns = [row[1] for row in db.execute("PRAGMA table_info(messages)").fetchall()]
        if "attachments_json" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN attachments_json TEXT DEFAULT '[]'")
        
        db.execute('''CREATE TABLE IF NOT EXISTS announcements (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        content TEXT,
                        date TEXT,
                        author TEXT,
                        source_url TEXT,
                        created_at TEXT DEFAULT (datetime('now', 'localtime'))
                    )''')
        


        # Таблицы админской панели сайта: новости, объявления, галерея, документы.
        db.execute('''CREATE TABLE IF NOT EXISTS site_posts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        page TEXT NOT NULL,
                        title TEXT NOT NULL,
                        content TEXT DEFAULT '',
                        author TEXT DEFAULT '',
                        file_path TEXT DEFAULT '',
                        file_name TEXT DEFAULT '',
                        file_type TEXT DEFAULT '',
                        files_json TEXT DEFAULT '[]',
                        likes INTEGER DEFAULT 0,
                        created_at TEXT DEFAULT (datetime('now', 'localtime'))
                    )''')
        site_post_columns = [row[1] for row in db.execute("PRAGMA table_info(site_posts)").fetchall()]
        if "files_json" not in site_post_columns:
            db.execute("ALTER TABLE site_posts ADD COLUMN files_json TEXT DEFAULT '[]'")
        if "likes" not in site_post_columns:
            db.execute("ALTER TABLE site_posts ADD COLUMN likes INTEGER DEFAULT 0")
        if "author" not in site_post_columns:
            db.execute("ALTER TABLE site_posts ADD COLUMN author TEXT DEFAULT ''")
        if "file_path" not in site_post_columns:
            db.execute("ALTER TABLE site_posts ADD COLUMN file_path TEXT DEFAULT ''")
        if "file_name" not in site_post_columns:
            db.execute("ALTER TABLE site_posts ADD COLUMN file_name TEXT DEFAULT ''")
        if "file_type" not in site_post_columns:
            db.execute("ALTER TABLE site_posts ADD COLUMN file_type TEXT DEFAULT ''")

        # Замены в расписании из панели директора.
        db.execute('''CREATE TABLE IF NOT EXISTS schedule_overrides (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        class_name TEXT NOT NULL,
                        day TEXT NOT NULL,
                        lesson_number TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        room TEXT DEFAULT '',
                        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                        UNIQUE(class_name, day, lesson_number)
                    )''')
        schedule_override_columns = [row[1] for row in db.execute("PRAGMA table_info(schedule_overrides)").fetchall()]
        if "room" not in schedule_override_columns:
            db.execute("ALTER TABLE schedule_overrides ADD COLUMN room TEXT DEFAULT ''")

        db.execute('''CREATE TABLE IF NOT EXISTS teacher_cache (
                        assignment_id INTEGER PRIMARY KEY,
                        teacher_name TEXT,
                        cached_at TEXT DEFAULT (datetime('now', 'localtime'))
                    )''')
        

        # Локальная база для lkteach.html: fallback, если СГО не вернул JSON для расписания/журнала.
        db.execute('''CREATE TABLE IF NOT EXISTS teacher_schedule_local (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        login TEXT NOT NULL,
                        school TEXT DEFAULT '',
                        lesson_date TEXT NOT NULL,
                        lesson_number INTEGER DEFAULT 1,
                        lesson_time TEXT DEFAULT '',
                        subject TEXT DEFAULT '',
                        class_name TEXT DEFAULT '',
                        theme TEXT DEFAULT ''
                    )''')

        db.execute('''CREATE TABLE IF NOT EXISTS teacher_journal_local (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        login TEXT NOT NULL,
                        school TEXT DEFAULT '',
                        class_name TEXT DEFAULT '',
                        subject TEXT DEFAULT '',
                        work_date TEXT NOT NULL,
                        work_title TEXT DEFAULT '',
                        work_type TEXT DEFAULT '',
                        student_name TEXT NOT NULL,
                        mark TEXT DEFAULT ''
                    )''')
        db.commit()
        print("Database initialized")

init_db()

# ================== API AUTH ==================


# Совместимость со старыми участками server.py, где функции назывались иначе.
def _is_admin_role_value(value):
    return _is_admin_profile_value(value)


def _admin_avatar_fallback(db, exclude_login=''):
    item = _admin_profile_fallback(db, '', exclude_login) or {}
    return {
        'profile_photo': item.get('profile_photo') or '',
        'avatar_color': item.get('avatar_color') or ''
    }


@app.route("/api/admin_profile", methods=["GET", "OPTIONS"])
def api_admin_profile():
    """Возвращает фактически сохранённую аватарку администратора/директора.

    Используется lkteacher/lk для диалогов: если диалог создан со старым логином-алиасом
    администратора, берём фото из реального сохранённого профиля lkadmin.
    """
    if request.method == "OPTIONS":
        return jsonify({'success': True})
    db = get_db()
    school = (request.args.get('school') or '').strip()
    login = (request.args.get('login') or '').strip()
    user = None
    if login:
        row = db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
        if row:
            user = _row_to_public_user(row)
            user = _apply_admin_avatar_fallback_to_user(db, user)
    if not user or not (user.get('profile_photo') or user.get('avatar_color')):
        user = _admin_profile_fallback(db, school) or _admin_profile_fallback(db)
    if user:
        user['role'] = user.get('role') or 'Администратор'
        return jsonify({'success': True, 'user': user})
    return jsonify({'success': False, 'error': 'Профиль администратора не найден'}), 404

@app.route("/api/login", methods=["POST", "OPTIONS"])
def api_login():
    """Вход/регистрация без обязательной проверки в СГО.

    Пользователь допускается в кабинет после заполнения формы. Данные сохраняются
    в локальной базе сайта, чтобы /api/user_info возвращал выбранную роль и не
    вызывал циклические переходы между lk.html и lk_school.html.
    """
    if request.method == "OPTIONS":
        return jsonify({"success": True})

    data = request.get_json(silent=True) or {}
    user_login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    email = (data.get("email") or "").strip()
    role = (data.get("role") or "Ученик").strip()
    cabinet_url = cabinet_url_for_role(role)
    school = get_requested_school(data)
    full_name = (data.get("full_name") or user_login).strip()
    avatar_color = data.get("avatar_color", "")
    profile_photo = data.get("profile_photo", "")
    profile_photo_clear = bool(data.get("profile_photo_clear"))

    if not user_login:
        return jsonify({"success": False, "error": "Введите логин"}), 400

    # Если вход выполняется повторно как другая роль (например, Родитель -> Ученик)
    # и поле пароля пустое, login.html может прислать local-password.
    # Подставляем сохранённый реальный пароль и не ломаем рабочий вход в СГО.
    existing_before_login = None
    try:
        existing_before_login = get_db().execute("SELECT password, school FROM users WHERE login = ?", (user_login,)).fetchone()
    except Exception:
        existing_before_login = None
    if (not password or password == "local-password") and existing_before_login:
        saved_password = (existing_before_login.get("password") or "").strip()
        if saved_password and saved_password != "local-password":
            password = saved_password

    if not cabinet_url:
        return jsonify({"success": False, "error": "Выберите корректную роль"}), 400
    if cabinet_url == "index.html":
        return jsonify({
            "success": True,
            "message": "Гостевой вход выполнен",
            "mail_sent": False,
            "mail_message": "Для роли Гость письмо не отправляется",
            "cabinet_url": "index.html",
            "user": None
        })
    if not password:
        return jsonify({"success": False, "error": "Введите пароль"}), 400
    if not email:
        return jsonify({"success": False, "error": "Введите электронную почту"}), 400
    if not is_valid_email_address(email):
        return jsonify({"success": False, "error": "Введите корректную электронную почту"}), 400
    if not school:
        return jsonify({"success": False, "error": "Выберите школу"}), 400

    # ВАЖНО: здесь больше нет вызова NetSchoolAPI / sgo_login_with_fallback.
    # Любой пользователь с заполненной формой допускается в кабинет сайта.
    actual_school = school

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE login = ?", (user_login,)).fetchone()

    if user:
        db.execute(
            """
            UPDATE users
            SET password = CASE WHEN ? IS NOT NULL AND ? != '' AND ? != 'local-password' THEN ? ELSE password END,
                email = COALESCE(NULLIF(?, ''), email),
                role = ?,
                school = ?,
                full_name = COALESCE(NULLIF(?, ''), full_name),
                avatar_color = COALESCE(NULLIF(?, ''), avatar_color),
                profile_photo = COALESCE(NULLIF(?, ''), profile_photo)
            WHERE login = ?
            """,
            (password, password, password, password, email, role or "Ученик", actual_school, full_name or user_login, avatar_color, profile_photo, user_login)
        )
    else:
        db.execute(
            """
            INSERT INTO users (login, password, role, email, school, full_name, avatar_color, profile_photo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_login, password, role or "Ученик", email, actual_school, full_name or user_login, avatar_color, profile_photo)
        )

    db.commit()
    user = db.execute("SELECT * FROM users WHERE login = ?", (user_login,)).fetchone()

    # После успешного входа отправляем уведомление на почту, записанную в профиле.
    # Ошибка SMTP не отменяет сам вход, но возвращается в ответе для диагностики.
    profile_email = (user["email"] or email or "").strip()
    mail_sent, mail_message = send_login_success_email(
        profile_email,
        user["login"],
        user["role"] or role or "Ученик",
        user["school"] or actual_school
    )

    return jsonify({
        "success": True,
        "message": "Вход выполнен" + (", письмо об успешном входе отправлено" if mail_sent else ", но письмо не отправлено"),
        "mail_sent": bool(mail_sent),
        "mail_message": mail_message,
        "cabinet_url": cabinet_url,
        "warning": "Письмо не отправлено: " + mail_message if not mail_sent else "",
        "user": {
            "login": user["login"],
            "email": user.get("email") or email,
            "role": user.get("role") or role or "Ученик",
            "school": user.get("school") or actual_school,
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
    school = get_requested_school(data)
    full_name = data.get("full_name", login)
    avatar_color = data.get("avatar_color", "")
    profile_photo = data.get("profile_photo", "")

    if not login:
        return jsonify({"success": False, "error": "Логин обязателен"}), 400
    # Пароль при обновлении профиля из кабинета не обязателен.
    # Если пароль не передан, сохраняем существующий пароль или пустую строку.
    # Email не обязателен при сохранении профиля из личного кабинета.

    db = get_db()
    existing = db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()

    if existing:
        db.execute(
            """
            UPDATE users
            SET password = COALESCE(NULLIF(?, ''), password),
                email = COALESCE(NULLIF(?, ''), email),
                role = COALESCE(NULLIF(?, ''), role),
                school = COALESCE(NULLIF(?, ''), school),
                full_name = COALESCE(NULLIF(?, ''), full_name),
                avatar_color = COALESCE(NULLIF(?, ''), avatar_color),
                profile_photo = CASE WHEN ? THEN '' ELSE COALESCE(NULLIF(?, ''), profile_photo) END
            WHERE login = ?
            """,
            (password, email, role, school, full_name or login, avatar_color, profile_photo_clear, profile_photo, login)
        )
    else:
        db.execute(
            """
            INSERT INTO users (login, password, role, email, school, full_name, avatar_color, profile_photo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (login, password or '', role, email, school, full_name or login, avatar_color, profile_photo)
        )

    db.commit()
    user = db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()

    return jsonify({
        "success": True,
        "user": {
            "login": user["login"],
            "email": user.get("email") or "",
            "role": user.get("role") or "Ученик",
            "school": user.get("school") or school,
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
    # Гарантируем наличие поля класса в старых базах.
    try:
        user_columns = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
        if "class_name" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN class_name TEXT DEFAULT ''")
            db.commit()
    except Exception:
        pass

    user = db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    
    if user:
        return jsonify({"success": True, "user": {
            "id": user.get("id"),
            "login": user["login"],
            "email": user.get("email") or "",
            "role": user.get("role"),
            "full_name": user.get("full_name") or user["login"],
            "avatar_color": user.get("avatar_color") or "",
            "profile_photo": user.get("profile_photo") or "",
            "school": user.get("school") or "",
            "class_name": user.get("class_name") or ""
        }})
    else:
        db.execute("INSERT INTO users (login, password, role, full_name) VALUES (?, '123', 'Ученик', ?)", (login, login))
        db.commit()
        user = db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
        return jsonify({"success": True, "user": {"id": user.get("id") if user else None, "login": login, "role": "Ученик", "full_name": login, "profile_photo": "", "class_name": ""}})


def _extract_student_class_from_init(init_json):
    """Достаёт класс ученика/ребёнка из JSON СГО максимально устойчиво.

    У разных ролей СГО структура diary/init отличается: у ученика класс может лежать
    в корне, у родителя — внутри students/children/currentStudent, иногда как объект
    class/grade, иногда как пара number + letter. Поэтому сначала проверяем типовые
    поля, затем рекурсивно ищем подходящие значения по всему JSON.
    """
    def clean(value):
        if value is None:
            return ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if 1 <= int(value) <= 11:
                return str(int(value))
            return ""
        text = str(value).strip()
        if not text or text.lower() in ("none", "null", "false"):
            return ""
        return re.sub(r"\s+", " ", text)

    def normalize(value):
        text = clean(value)
        if not text:
            return ""
        # Отсекаем явно не классы: идентификаторы/даты/длинные фразы без слова класс.
        if len(text) > 40 and "класс" not in text.lower():
            return ""
        # 6, 6А, 6-А, 6 а -> 6А класс
        m = re.match(r"^(1[0-1]|[1-9])\s*[-–]?\s*([А-ЯA-Z])?$", text, re.I)
        if m:
            num = m.group(1)
            letter = (m.group(2) or "").upper()
            return f"{num}{letter} класс"
        if re.search(r"\b(1[0-1]|[1-9])\s*[-–]?\s*[А-ЯA-Z]?\s*класс\b", text, re.I):
            return text
        if "класс" in text.lower():
            return text
        return ""

    if not isinstance(init_json, dict):
        return ""

    class_keys = {
        'className', 'class_name', 'class', 'gradeName', 'grade_name', 'grade',
        'classTitle', 'classLabel', 'classLetter', 'parallelName', 'educationClass',
        'studentClass', 'student_class', 'studyClass', 'study_class', 'eduClass',
        'edu_class', 'classNumber', 'class_number', 'klass', 'klassName'
    }
    letter_keys = {'letter', 'litera', 'classLetter', 'class_letter'}
    number_keys = {'number', 'num', 'classNumber', 'class_number', 'grade', 'parallel'}

    def candidate_from_obj(obj):
        if not isinstance(obj, dict):
            return ""
        # 1) Прямые поля.
        for key in class_keys:
            if key in obj:
                val = obj.get(key)
                if isinstance(val, dict):
                    nested = candidate_from_obj(val)
                    if nested:
                        return nested
                got = normalize(val)
                if got:
                    return got
        # 2) Объекты class/grade/classInfo.
        for key in ('class', 'classInfo', 'grade', 'gradeInfo', 'educationClass'):
            val = obj.get(key)
            if isinstance(val, dict):
                got = candidate_from_obj(val)
                if got:
                    return got
        # 3) Пара номер + буква.
        num = ""
        letter = ""
        for key in number_keys:
            if key in obj:
                raw = clean(obj.get(key))
                if re.match(r"^(1[0-1]|[1-9])$", raw):
                    num = raw
                    break
        for key in letter_keys:
            if key in obj:
                raw = clean(obj.get(key))
                if re.match(r"^[А-ЯA-Z]$", raw, re.I):
                    letter = raw.upper()
                    break
        if num:
            return f"{num}{letter} класс"
        return ""

    # Сначала наиболее вероятные текущие ученики/дети.
    current_student_id = init_json.get('currentStudentId') or init_json.get('studentId') or init_json.get('personId')
    students = init_json.get('students') or init_json.get('student') or init_json.get('children') or init_json.get('pupils') or []
    candidates = []
    if isinstance(students, dict):
        if current_student_id is not None:
            candidates.append(students.get(current_student_id) or students.get(str(current_student_id)))
        candidates.extend(students.values())
    elif isinstance(students, list):
        candidates.extend(students)
    elif isinstance(students, dict):
        candidates.append(students)
    for key in ('currentStudent', 'selectedStudent', 'studentInfo', 'pupil', 'child'):
        if isinstance(init_json.get(key), dict):
            candidates.append(init_json.get(key))
    candidates.append(init_json)

    for item in candidates:
        got = candidate_from_obj(item)
        if got:
            return got

    # Последний шанс: рекурсивный обход всего ответа.
    seen = set()
    def walk(obj, parent_key=''):
        oid = id(obj)
        if oid in seen:
            return ""
        seen.add(oid)
        if isinstance(obj, dict):
            got = candidate_from_obj(obj)
            if got:
                return got
            for key, value in obj.items():
                key_text = str(key)
                if key_text in class_keys or re.search(r"class|grade|класс", key_text, re.I):
                    got = normalize(value)
                    if got:
                        return got
                got = walk(value, key_text)
                if got:
                    return got
        elif isinstance(obj, list):
            for item in obj:
                got = walk(item, parent_key)
                if got:
                    return got
        return ""

    return walk(init_json)



def _normalize_class_title(value):
    text = str(value or "").strip()
    if not text or text in ("—", "-"):
        return ""
    if re.search(r"класс", text, re.I):
        return text
    if re.match(r"^(1[0-1]|[1-9])\s*[-–]?\s*[А-ЯA-Z]?$", text, re.I):
        return re.sub(r"\s+", "", text).upper() + " класс"
    return text

def _extract_class_from_student_average_mark(report_json):
    if not isinstance(report_json, dict):
        return ""
    for source in report_json.get("filterSources") or []:
        if not isinstance(source, dict) or source.get("filterId") != "PCLID":
            continue
        default_value = str(source.get("defaultValue") or "").strip()
        items = source.get("items") or []
        chosen = None
        if default_value:
            for item in items:
                if str((item or {}).get("value") or "").strip() == default_value:
                    chosen = item
                    break
        if chosen is None and items:
            chosen = items[0]
        title = (chosen or {}).get("title") if isinstance(chosen, dict) else ""
        return _normalize_class_title(title)
    return ""

def _extract_children_from_student_init(init_json):
    result = []
    def add(value):
        if value is None:
            return
        if isinstance(value, str):
            name = value.strip()
        elif isinstance(value, dict):
            name = str(value.get("nickName") or value.get("name") or value.get("fullName") or value.get("fio") or value.get("title") or "").strip()
        else:
            name = ""
        if name and name not in result:
            result.append(name)
    if isinstance(init_json, dict):
        for key in ("students", "children", "pupils"):
            val = init_json.get(key)
            if isinstance(val, list):
                for item in val:
                    add(item)
            elif isinstance(val, dict):
                for item in val.values():
                    add(item)
        for key in ("student", "currentStudent", "selectedStudent", "child"):
            add(init_json.get(key))
    return result

def _fetch_student_average_mark_report(cookies_dict, headers):
    url = SGO_URL.rstrip('/') + "/webapi/reports/studentaveragemark"
    # В разных сборках СГО этот endpoint может принимать GET или POST.
    for method in ("GET", "POST"):
        try:
            if method == "GET":
                resp = requests.get(url, cookies=cookies_dict or {}, headers=headers, verify=False, timeout=10)
            else:
                resp = requests.post(url, cookies=cookies_dict or {}, headers=headers, json={}, verify=False, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    return {}

@app.route("/api/student_profile_sgo", methods=["GET", "POST", "OPTIONS"])
def student_profile_sgo():
    """Получает класс обучения ученика напрямую из СГО через /webapi/student/diary/init."""
    if request.method == "OPTIONS":
        return jsonify({"success": True})

    data = request.args.to_dict() if request.method == "GET" else (request.get_json(silent=True) or {})
    login_val = (data.get("login") or request.args.get("login") or "").strip()
    password_val = (data.get("password") or request.args.get("password") or "").strip()
    school = get_requested_school(data)
    password_val, school = apply_saved_login_fields(login_val, password_val, school)

    if not login_val or not password_val or password_val == "local-password":
        return jsonify({"success": False, "error": "Не передан реальный пароль СГО"}), 400

    async def _fetch():
        ns = None
        actual_school = school
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            actual_school = await asyncio.wait_for(sgo_login_with_fallback(ns, login_val, password_val, school), timeout=15)
            cookies_dict, at_token = get_cookies_from_ns(ns)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/plain, */*",
                "Referer": SGO_URL.rstrip('/') + "/app/student/diary"
            }
            if at_token:
                headers["at"] = at_token

            sgo_profile = {}
            try:
                settings_url = SGO_URL.rstrip('/') + "/webapi/mysettings"
                settings_resp = requests.get(settings_url, cookies=cookies_dict or {}, headers=headers, verify=False, timeout=10)
                if settings_resp.status_code == 200:
                    settings_json = settings_resp.json()
                    if isinstance(settings_json, dict):
                        first_name = str(settings_json.get('firstName') or '').strip()
                        last_name = str(settings_json.get('lastName') or '').strip()
                        middle_name = str(settings_json.get('middleName') or '').strip()
                        full_name = ' '.join(x for x in [last_name, first_name, middle_name] if x).strip()
                        user_settings = settings_json.get('userSettings') or {}
                        roles_value = settings_json.get('roles') or []
                        if isinstance(roles_value, list):
                            roles_text = ', '.join(str(x) for x in roles_value if x)
                        else:
                            roles_text = str(roles_value or '')
                        sgo_profile = {
                            'full_name': full_name,
                            'firstName': first_name,
                            'lastName': last_name,
                            'middleName': middle_name,
                            'birth_date': str(settings_json.get('birthDate') or '').strip(),
                            'birthDate': str(settings_json.get('birthDate') or '').strip(),
                            'email': str(settings_json.get('email') or '').strip(),
                            'login': str(settings_json.get('loginName') or login_val or '').strip(),
                            'loginName': str(settings_json.get('loginName') or login_val or '').strip(),
                            'role': roles_text,
                            'roles': roles_value,
                            'user_id': str((user_settings.get('userId') if isinstance(user_settings, dict) else '') or settings_json.get('userId') or '').strip(),
                            'userId': str((user_settings.get('userId') if isinstance(user_settings, dict) else '') or settings_json.get('userId') or '').strip(),
                            'source': '/webapi/mysettings'
                        }
                        try:
                            db = get_db()
                            if sgo_profile.get('full_name') or sgo_profile.get('email'):
                                db.execute(
                                    "UPDATE users SET full_name = COALESCE(NULLIF(?, ''), full_name), email = COALESCE(NULLIF(?, ''), email) WHERE login = ?",
                                    (sgo_profile.get('full_name') or '', sgo_profile.get('email') or '', login_val)
                                )
                                db.commit()
                        except Exception:
                            pass
            except Exception as profile_error:
                safe_console_print("STUDENT MYSETTINGS ERROR:", profile_error)

            init_url = SGO_URL.rstrip('/') + "/webapi/student/diary/init"
            resp = requests.get(init_url, cookies=cookies_dict or {}, headers=headers, verify=False, timeout=10)
            if resp.status_code != 200:
                raise RuntimeError(f"СГО вернул статус {resp.status_code} для diary/init")
            init_json = resp.json()
            class_name = _extract_student_class_from_init(init_json)
            children = _extract_children_from_student_init(init_json)
            student_id = init_json.get('currentStudentId') or init_json.get('studentId') or ''
            if (not student_id or str(student_id) == '0') and isinstance(init_json.get('students'), list) and init_json.get('students'):
                student_id = init_json.get('students')[0].get('studentId') or ''

            # Если diary/init не отдаёт className, берём класс из фильтра отчёта StudentAverageMark: filterId=PCLID.
            if not class_name:
                report_json = _fetch_student_average_mark_report(cookies_dict, headers)
                class_name = _extract_class_from_student_average_mark(report_json)

            # Сохраняем найденный класс, чтобы дальше он показывался без повторного запроса.
            if class_name:
                try:
                    db = get_db()
                    columns = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
                    if "class_name" not in columns:
                        db.execute("ALTER TABLE users ADD COLUMN class_name TEXT DEFAULT ''")
                    db.execute("UPDATE users SET class_name = ? WHERE login = ?", (class_name, login_val))
                    db.commit()
                except Exception:
                    pass

            try:
                await ns.logout()
            except Exception:
                pass
            return jsonify({
                "success": True,
                "class_name": class_name,
                "data": {
                    "class_name": class_name,
                    "student_id": student_id,
                    "school": actual_school,
                    "children": children,
                    "students": children,
                    **sgo_profile
                },
                "children": children,
                "students": children,
                "student_id": student_id,
                "school": actual_school,
                "user_profile": sgo_profile,
                **sgo_profile
            })
        except Exception as e:
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            safe_console_print("STUDENT PROFILE SGO ERROR:", e)
            safe_print_traceback()
            return jsonify({"success": False, "error": str(e), "school": actual_school}), 200

    return run_async(_fetch())

# ================== API MESSAGES ==================
def ensure_messages_table():
    """Гарантирует постоянное хранение сообщений в school.db для всех аккаунтов."""
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp TEXT DEFAULT (datetime('now', 'localtime')),
                    read INTEGER DEFAULT 0,
                    attachments_json TEXT DEFAULT '[]'
                )""")
    columns = [row[1] for row in db.execute("PRAGMA table_info(messages)").fetchall()]
    if "attachments_json" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN attachments_json TEXT DEFAULT '[]'")
    db.commit()

MESSAGE_ATTACHMENT_EXTENSIONS = {"pdf", "prd", "doc", "docx", "xls", "xlsx", "jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"}
MESSAGE_ATTACHMENT_MAX_FILES = 3
MESSAGE_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024

def normalize_message_attachments(raw_attachments):
    if not isinstance(raw_attachments, list):
        return []
    normalized = []
    for item in raw_attachments[:MESSAGE_ATTACHMENT_MAX_FILES]:
        if not isinstance(item, dict):
            continue
        original_name = str(item.get("name") or item.get("filename") or "attachment.bin").strip() or "attachment.bin"
        original_name = os.path.basename(original_name.replace("\\", "/"))
        original_name = re.sub(r"[\x00-\x1f\x7f]", "", original_name).strip(" .") or "attachment.bin"
        ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
        safe_name = secure_filename(original_name)
        if ext and (not safe_name or "." not in safe_name):
            stem = safe_name if safe_name and safe_name.lower() != ext else "attachment"
            safe_name = f"{stem}.{ext}"
        name = original_name[:180] if original_name else (safe_name or "attachment.bin")
        mime = str(item.get("type") or "application/octet-stream").split(";", 1)[0].strip() or "application/octet-stream"
        if ext not in MESSAGE_ATTACHMENT_EXTENSIONS and not mime.startswith("image/"):
            continue
        data_url = str(item.get("data") or "")
        if "," not in data_url:
            continue
        header, payload = data_url.split(",", 1)
        if ";base64" not in header.lower():
            continue
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            continue
        if not raw or len(raw) > MESSAGE_ATTACHMENT_MAX_BYTES:
            continue
        normalized.append({
            "name": name,
            "type": mime,
            "size": len(raw),
            "data": "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))
        })
    return normalized

@app.route("/api/messages", methods=["GET", "OPTIONS"])
def get_messages():
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    login = (request.args.get("login") or "").strip()
    if not login:
        return jsonify({"success": False, "error": "Не указан логин"}), 400
    ensure_messages_table()
    db = get_db()
    login_aliases = [login]
    try:
        current_user = db.execute("SELECT role, school FROM users WHERE login = ?", (login,)).fetchone()
        role = (current_user.get("role") if current_user else "" or "").strip().lower()
        school = (current_user.get("school") if current_user else "" or "").strip()
        if role in ("администратор", "директор", "администрация"):
            if school:
                alias_rows = db.execute("SELECT login, role FROM users WHERE school = ?", (school,)).fetchall()
            else:
                alias_rows = db.execute("SELECT login, role FROM users").fetchall()
            for item in alias_rows:
                alias_role = ((item.get("role") or "") if hasattr(item, "get") else "").strip().lower()
                if alias_role not in ("администратор", "директор", "администрация"):
                    continue
                alias = (item.get("login") or "").strip()
                if alias and alias not in login_aliases:
                    login_aliases.append(alias)
    except Exception:
        pass

    if len(login_aliases) > 1:
        placeholders_alias = ",".join("?" for _ in login_aliases)
        rows = [dict(row) for row in db.execute(
            f"""
            SELECT * FROM messages
            WHERE sender IN ({placeholders_alias}) OR recipient IN ({placeholders_alias})
            ORDER BY datetime(timestamp) ASC, id ASC
            """,
            login_aliases + login_aliases
        ).fetchall()]
    else:
        cursor = db.execute(
            "SELECT * FROM messages WHERE sender = ? OR recipient = ? ORDER BY datetime(timestamp) ASC, id ASC",
            (login, login)
        )
        rows = [dict(row) for row in cursor.fetchall()]
        # Если админ вошёл под похожим локальным логином, старые диалоги могли
        # остаться привязанными к прежней записи. Подмешиваем админские алиасы.
        login_lower = login.lower()
        if not rows and any(marker in login_lower for marker in ("дир", "admin", "админ")):
            try:
                alias_rows = db.execute("SELECT login, role FROM users").fetchall()
                for item in alias_rows:
                    alias_role = ((item.get("role") or "") if hasattr(item, "get") else "").strip().lower()
                    alias_login = ((item.get("login") or "") if hasattr(item, "get") else "").strip()
                    role_is_admin = any(marker in alias_role for marker in ("админ", "директор", "администрац", "admin"))
                    login_is_admin = any(marker in alias_login.lower() for marker in ("дир", "admin", "админ"))
                    if alias_login and (role_is_admin or login_is_admin) and alias_login not in login_aliases:
                        login_aliases.append(alias_login)
                if len(login_aliases) > 1:
                    placeholders_alias = ",".join("?" for _ in login_aliases)
                    rows = [dict(row) for row in db.execute(
                        f"""
                        SELECT * FROM messages
                        WHERE sender IN ({placeholders_alias}) OR recipient IN ({placeholders_alias})
                        ORDER BY datetime(timestamp) ASC, id ASC
                        """,
                        login_aliases + login_aliases
                    ).fetchall()]
            except Exception:
                pass
    participants = sorted({*login_aliases, *[r.get("sender") for r in rows if r.get("sender")], *[r.get("recipient") for r in rows if r.get("recipient")]})
    users = []
    if participants:
        placeholders = ",".join("?" for _ in participants)
        try:
            users = [dict(row) for row in db.execute(
                f"SELECT login, role, full_name, avatar_color, profile_photo, school FROM users WHERE login IN ({placeholders})",
                participants
            ).fetchall()]
        except Exception:
            users = []
    # Преобразуем JSON вложений в обычное поле attachments для всех кабинетов.
    for row in rows:
        try:
            parsed = json.loads(row.get("attachments_json") or "[]")
            row["attachments"] = parsed if isinstance(parsed, list) else []
        except Exception:
            row["attachments"] = []
    return jsonify({"success": True, "data": rows, "users": users, "login_aliases": login_aliases})

@app.route("/api/send", methods=["POST", "OPTIONS"])
def send_message():
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    data = request.get_json(silent=True) or {}
    sender = (data.get("sender") or "").strip()
    recipient = (data.get("recipient") or "").strip()
    text = (data.get("text") or "").strip()
    raw_attachments = data.get("attachments") or []
    attachments = normalize_message_attachments(raw_attachments)
    if isinstance(raw_attachments, list) and raw_attachments and len(attachments) != len(raw_attachments[:MESSAGE_ATTACHMENT_MAX_FILES]):
        return jsonify({"success": False, "error": "\u0424\u0430\u0439\u043b \u043d\u0435 \u043f\u0440\u0438\u043a\u0440\u0435\u043f\u043b\u0451\u043d: \u043f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0444\u043e\u0440\u043c\u0430\u0442 \u0438 \u0440\u0430\u0437\u043c\u0435\u0440 \u0434\u043e 25 \u041c\u0411"}), 400
    if not sender or not recipient or (not text and not attachments):
        return jsonify({"success": False, "error": "Не все поля заполнены"}), 400
    ensure_messages_table()
    db = get_db()
    # Создаём минимальные записи пользователей, чтобы диалог сохранялся и был виден между аккаунтами.
    # Если клиент передал профиль отправителя, сразу сохраняем фото/цвет/роль — тогда аватар
    # администратора отображается одинаково у всех участников переписки, а не только локально.
    sender_profile = data.get("sender_profile") or {}
    for login_value in (sender, recipient):
        exists = db.execute("SELECT 1 FROM users WHERE login = ?", (login_value,)).fetchone()
        if not exists:
            db.execute("INSERT INTO users (login, password, role, full_name) VALUES (?, '123', 'Пользователь', ?)", (login_value, login_value))
    if isinstance(sender_profile, dict):
        full_name = (sender_profile.get("full_name") or sender_profile.get("login") or sender).strip()
        role = (sender_profile.get("role") or "").strip()
        avatar_color = (sender_profile.get("avatar_color") or "").strip()
        profile_photo = (sender_profile.get("profile_photo") or "").strip()
        if full_name or role or avatar_color or profile_photo:
            db.execute(
                """
                UPDATE users
                SET full_name = COALESCE(NULLIF(?, ''), full_name),
                    role = COALESCE(NULLIF(?, ''), role),
                    avatar_color = COALESCE(NULLIF(?, ''), avatar_color),
                    profile_photo = COALESCE(NULLIF(?, ''), profile_photo)
                WHERE login = ?
                """,
                (full_name, role, avatar_color, profile_photo, sender)
            )
    cursor = db.execute(
        "INSERT INTO messages (sender, recipient, text, timestamp, read, attachments_json) VALUES (?, ?, ?, datetime('now', 'localtime'), 0, ?)",
        (sender, recipient, text, json.dumps(attachments, ensure_ascii=False))
    )
    db.commit()
    return jsonify({"success": True, "id": cursor.lastrowid})

@app.route("/api/message_attachment/<int:message_id>/<int:attachment_index>", methods=["GET", "OPTIONS"])
def get_message_attachment(message_id, attachment_index):
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    ensure_messages_table()
    row = get_db().execute("SELECT attachments_json FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not row:
        return jsonify({"success": False, "error": "Message not found"}), 404
    try:
        attachments = json.loads(row.get("attachments_json") or "[]")
    except Exception:
        attachments = []
    if not isinstance(attachments, list) or attachment_index < 0 or attachment_index >= len(attachments):
        return jsonify({"success": False, "error": "Attachment not found"}), 404
    attachment = attachments[attachment_index] or {}
    data_url = str(attachment.get("data") or "")
    if "," not in data_url:
        return jsonify({"success": False, "error": "Attachment data is empty"}), 404
    header, payload = data_url.split(",", 1)
    try:
        content = base64.b64decode(payload, validate=True)
    except Exception:
        return jsonify({"success": False, "error": "Attachment data is corrupted"}), 500
    mime = str(attachment.get("type") or "").split(";", 1)[0].strip()
    if not mime and header.startswith("data:"):
        mime = header[5:].split(";", 1)[0]
    if not mime:
        mime = "application/octet-stream"
    original_filename = str(attachment.get("name") or attachment.get("filename") or "attachment.bin").strip() or "attachment.bin"
    fallback_filename = secure_filename(original_filename) or "attachment.bin"
    disposition = "attachment" if request.args.get("download") else "inline"
    response = Response(content, mimetype=mime)
    response.headers["Content-Disposition"] = "%s; filename=\"%s\"; filename*=UTF-8''%s" % (
        disposition,
        fallback_filename.replace('"', ''),
        url_quote(original_filename)
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

@app.route("/api/mark_read", methods=["POST", "OPTIONS"])
def mark_read():
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    data = request.get_json(silent=True) or {}
    login = (data.get("login") or "").strip()
    partner = (data.get("partner") or "").strip()
    if not login or not partner:
        return jsonify({"success": False, "error": "Не указан логин или собеседник"}), 400
    ensure_messages_table()
    db = get_db()
    login_aliases = [
        str(item or "").strip()
        for item in (data.get("login_aliases") or [login])
        if str(item or "").strip()
    ]
    if login not in login_aliases:
        login_aliases.insert(0, login)
    placeholders = ",".join("?" for _ in login_aliases)
    db.execute(
        f"UPDATE messages SET read = 1 WHERE sender = ? AND recipient IN ({placeholders}) AND read = 0",
        [partner] + login_aliases
    )
    db.commit()
    return jsonify({"success": True})



@app.route("/api/messages/clear", methods=["POST", "DELETE", "OPTIONS"])
@app.route("/api/clear_dialog", methods=["POST", "DELETE", "OPTIONS"])
@app.route("/api/clear_messages", methods=["POST", "DELETE", "OPTIONS"])
def clear_messages():
    """Совместимый endpoint очистки диалога для старого фронтенда."""
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    data = request.get_json(silent=True) or {}
    login = (data.get("login") or "").strip()
    partner = (data.get("partner") or "").strip()
    if not login or not partner:
        return jsonify({"success": False, "error": "Не указан логин или собеседник"}), 400

    login_aliases = [
        str(item or "").strip()
        for item in (data.get("login_aliases") or [login])
        if str(item or "").strip()
    ]
    if login not in login_aliases:
        login_aliases.insert(0, login)
    login_aliases = list(dict.fromkeys(login_aliases))

    ensure_messages_table()
    db = get_db()
    placeholders = ",".join("?" for _ in login_aliases)
    params = login_aliases + [partner, partner] + login_aliases
    cursor = db.execute(
        f"""
        DELETE FROM messages
        WHERE (sender IN ({placeholders}) AND recipient = ?)
           OR (sender = ? AND recipient IN ({placeholders}))
        """,
        params
    )
    db.commit()
    return jsonify({"success": True, "deleted": cursor.rowcount})

@app.route("/api/messages/delete", methods=["POST", "DELETE", "OPTIONS"])
@app.route("/api/delete_messages", methods=["POST", "DELETE", "OPTIONS"])
def delete_selected_messages():
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    data = request.get_json(silent=True) or {}
    login = (data.get("login") or "").strip()
    raw_ids = data.get("ids") or data.get("message_ids") or []
    if not login or not isinstance(raw_ids, list):
        return jsonify({"success": False, "error": "РќРµ СѓРєР°Р·Р°РЅС‹ Р»РѕРіРёРЅ РёР»Рё СЃРѕРѕР±С‰РµРЅРёСЏ"}), 400
    ids = []
    for item in raw_ids:
        try:
            value = int(item)
        except Exception:
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return jsonify({"success": False, "error": "РќРµ РІС‹Р±СЂР°РЅС‹ СЃРѕРѕР±С‰РµРЅРёСЏ"}), 400

    login_aliases = [
        str(item or "").strip()
        for item in (data.get("login_aliases") or [login])
        if str(item or "").strip()
    ]
    if login not in login_aliases:
        login_aliases.insert(0, login)
    login_aliases = list(dict.fromkeys(login_aliases))

    ensure_messages_table()
    db = get_db()
    id_placeholders = ",".join("?" for _ in ids)
    login_placeholders = ",".join("?" for _ in login_aliases)
    cursor = db.execute(
        f"""
        DELETE FROM messages
        WHERE id IN ({id_placeholders})
          AND (sender IN ({login_placeholders}) OR recipient IN ({login_placeholders}))
        """,
        ids + login_aliases + login_aliases
    )
    db.commit()
    return jsonify({"success": True, "deleted": cursor.rowcount})

# ===================== DIARY =====================

# Типы работ из СГО: /webapi/grade/assignment/types.
# Важно: эти ID отличаются от старой локальной таблицы. Неверная таблица
# приводила к тому, что в окне оценки показывался тип "—" или неправильный тип.
TYPE_NAMES = {
    1: "Практическая работа",
    2: "Тематическая работа",
    3: "Домашнее задание",
    4: "Контрольная работа",
    5: "Самостоятельная работа",
    6: "Лабораторная работа",
    7: "Проект",
    8: "Диктант",
    9: "Реферат",
    10: "Ответ на уроке",
    11: "Сочинение",
    12: "Изложение",
    13: "Зачёт",
    14: "Тестирование",
    15: "Оценка за тему",
    16: "Диагностическая контрольная работа",
    17: "Практикум",
    18: "Проверочная работа",
    19: "Элемент ДО",
    20: "Работа на уроке",
    21: "Норматив",
    22: "Контурные карты",
    23: "Ведение тетради",
}

def _find_sgo_at_token_in_obj(obj, seen=None, depth=0):
    """Ищет SGO at-token в объекте NetSchoolAPI/httpx без вывода секретов в логи."""
    if obj is None or depth > 4:
        return None
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return None
    seen.add(obj_id)

    for attr in (
        "at", "_at", "AT", "access_token", "_access_token", "token", "_token",
        "auth_token", "_auth_token", "at_token", "_at_token"
    ):
        try:
            value = getattr(obj, attr, None)
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()

    try:
        headers = getattr(obj, "headers", None)
        if headers:
            for key in ("at", "AT", "At", "access-token", "Authorization"):
                try:
                    value = headers.get(key)
                except Exception:
                    value = None
                if isinstance(value, str) and value.strip():
                    if key.lower() == "authorization" and value.lower().startswith("bearer "):
                        value = value.split(" ", 1)[1]
                    return value.strip()
    except Exception:
        pass

    if isinstance(obj, dict):
        for key in ("at", "AT", "access_token", "token", "auth_token", "at_token"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            found = _find_sgo_at_token_in_obj(value, seen, depth + 1)
            if found:
                return found
        return None

    for attr in ("_wrapped_client", "wrapped_client", "client", "_client", "session", "_session", "api", "_api"):
        try:
            child = getattr(obj, attr, None)
        except Exception:
            child = None
        if child is not None:
            found = _find_sgo_at_token_in_obj(child, seen, depth + 1)
            if found:
                return found

    return None


def get_cookies_from_ns(ns):
    """Извлекает cookies и SGO at-token из NetSchoolAPI.

    Для teacher webapi СГО часто недостаточно одних cookies: endpoints вроде
    /webapi/subjectgroups возвращают 401, если не передать заголовок ``at``.
    Поэтому функция теперь возвращает пару: (cookies_dict, at_token).
    """
    cookies = {}
    at_token = _find_sgo_at_token_in_obj(ns)

    if ns is None:
        return {}, None

    for attr in ("cookies", "_cookies"):
        try:
            obj = getattr(ns, attr, None)
        except Exception:
            obj = None
        if isinstance(obj, dict) and obj:
            cookies = dict(obj)
            break

    clients = []
    for attr in ("client", "_client"):
        try:
            obj = getattr(ns, attr, None)
        except Exception:
            obj = None
        if obj is not None:
            clients.append(obj)

    try:
        wrapped = getattr(ns, "_wrapped_client", None)
    except Exception:
        wrapped = None
    if wrapped is not None:
        clients.append(wrapped)
        try:
            wrapped_client = getattr(wrapped, "client", None)
        except Exception:
            wrapped_client = None
        if wrapped_client is not None:
            clients.append(wrapped_client)

    for client in clients:
        if not at_token:
            at_token = _find_sgo_at_token_in_obj(client)
        if cookies:
            continue
        try:
            c = getattr(client, "cookies", None)
        except Exception:
            c = None
        if c is None:
            continue
        try:
            data = {k: v for k, v in c.items()}
            if data:
                cookies = data
        except Exception:
            pass

    return cookies, at_token

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
        safe_print_traceback()
    
    return None



def get_assignment_id(assign):
    """ID задания/работы в СГО."""
    return first_nonempty(
        getattr(assign, "id", None),
        deep_get(assign, "id", "assignmentId", "assignment_id", "workId", "work_id")
    )



def is_real_assignment_id(value):
    """True only for real SGO assignment IDs. Netschoolapi often uses 0 for marks without an assignment; do not query SGO for it."""
    text = str(value or '').strip()
    if not text or text in ('0', 'None', 'null', 'undefined'):
        return False
    try:
        return int(text) > 0
    except Exception:
        return bool(re.fullmatch(r'[1-9]\d*', text))

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

    assignment_name_for_theme = data.get("assignmentName") if isinstance(data, dict) else None
    if assignment_name_for_theme:
        m = re.search(r"по\s+теме\s+[\"«]?(.+?)[\"»]?\s*$", str(assignment_name_for_theme), re.I)
        if m:
            return clean_html_text(m.group(1))

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
    assignment_name_for_type = data.get("assignmentName") if isinstance(data, dict) else None
    if assignment_name_for_type:
        text = str(assignment_name_for_type).strip()
        # Пример СГО: Урок-эксперимент по теме "...".
        m = re.match(r"^(.+?)\s+по\s+теме", text, re.I)
        if m:
            return clean_html_text(m.group(1))
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
    if not is_real_assignment_id(assignment_id):
        return {}

    try:
        headers = {
            "Accept": "application/json",
            "Referer": "https://sgo.volganet.ru/app/school/studentdiary/",
            "User-Agent": "Mozilla/5.0"
        }
        url = f"https://sgo.volganet.ru/webapi/student/diary/assigns/{assignment_id}"
        resp = requests.get(url, cookies=cookies_dict, headers=headers, params={}, verify=False, timeout=8)

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
        safe_print_traceback()
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
            value = data[name]
            return None if callable(value) else value
        if hasattr(obj, name):
            try:
                value = getattr(obj, name)
                if not callable(value):
                    return value
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
        if value is None or callable(value):
            continue

        if isinstance(value, (list, tuple, set)):
            for item in value:
                found = first_nonempty(
                    item.get("name") if isinstance(item, dict) else None,
                    item.get("fullName") if isinstance(item, dict) else None,
                    getattr(item, "name", None) if not callable(getattr(item, "name", None)) else None,
                    getattr(item, "fullName", None) if not callable(getattr(item, "fullName", None)) else None,
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
    data = _safe_jsonable(assign) if '_safe_jsonable' in globals() else obj_to_dict(assign)

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
    text = html.unescape(str(text or ""))
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
    direct_type = deep_get(assign, "type", "assignmentType", "workType")
    explicit = first_nonempty(
        deep_get(assign, "typeName", "type_name", "assignmentTypeName", "workTypeName"),
        direct_type if isinstance(direct_type, str) else None,
        deep_get(direct_type, "name", "title") if isinstance(direct_type, dict) else None,
    )
    explicit = clean_html_text(explicit or "")
    return explicit or TYPE_NAMES.get(type_id, "")


def normalize_assignment_type_name(type_id=None, explicit_name=""):
    """Возвращает полное название типа работы по ID СГО или уже готовому имени."""
    explicit_name = clean_html_text(explicit_name or "")
    if explicit_name and explicit_name not in ("—", "-"):
        return explicit_name
    try:
        tid = int(type_id)
    except (TypeError, ValueError):
        tid = 0
    return TYPE_NAMES.get(tid, "")

def extract_assignment_type_id_from_payload(*objs):
    """Ищет typeId/assignmentTypeId/workTypeId в ответах СГО по заданию."""
    for obj in objs:
        raw = first_nonempty(
            deep_get(obj, "typeId", "type_id", "assignmentTypeId", "workTypeId", "assignment_type_id"),
            deep_get(deep_get(obj, "type", "assignmentType", "workType"), "id", "typeId"),
        )
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0

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

def lesson_room_value(lesson):
    """Extract classroom/cabinet from diary lesson objects returned by different SGO/netschoolapi versions."""
    for field in ("room", "classroom", "cabinet", "place", "auditorium", "audience", "location"):
        value = deep_get(lesson, field)
        if isinstance(value, dict):
            text = first_nonempty(
                value.get("name"),
                value.get("title"),
                value.get("number"),
                value.get("roomNumber"),
                value.get("cabinet"),
                value.get("value"),
            )
        else:
            text = first_nonempty(value)
        if text:
            return clean_html_text(text)

    return read_nested_field(
        lesson,
        "roomName", "classroomName", "cabinetName", "roomNumber",
        "classroomNumber", "cabinet", "place", "location"
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
    """Возвращает реальное ДЗ"""
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
        assign_info.get("teachers") if isinstance(assign_info, dict) else None,
        assign.get("teachersStr") if isinstance(assign, dict) else None,
        assign.get("teacherName") if isinstance(assign, dict) else None,
        assign.get("teacher") if isinstance(assign, dict) else None,
        assign.get("teacherFullName") if isinstance(assign, dict) else None,
        assign.get("teachers") if isinstance(assign, dict) else None,
        data.get("teachers") if isinstance(data, dict) else None,
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

    type_id = extract_assignment_type_id_from_payload(assign, assign_info, data)
    explicit_detail_type = parse_type_from_detail(data) if isinstance(data, dict) else ""
    type_name = normalize_assignment_type_name(type_id, first_nonempty(
        explicit_detail_type,
        assign.get("typeName") if isinstance(assign, dict) else None,
        assign.get("assignmentTypeName") if isinstance(assign, dict) else None,
        assign.get("workTypeName") if isinstance(assign, dict) else None,
        assign_info.get("typeName") if isinstance(assign_info, dict) else None,
        assign_info.get("assignmentTypeName") if isinstance(assign_info, dict) else None,
        assign_info.get("workTypeName") if isinstance(assign_info, dict) else None,
        deep_get(assign, "type", "assignmentType", "workType", "name") if isinstance(assign, dict) else None,
        deep_get(assign_info, "type", "assignmentType", "workType", "name") if isinstance(assign_info, dict) else None,
    ))

    mark_value = first_nonempty(
        result.get("mark") if isinstance(result, dict) else None,
        result.get("value") if isinstance(result, dict) else None,
    )

    subject_group = first_nonempty(
        deep_get(assign_info, "subjectGroup", "subjectgroup"),
        deep_get(assign, "subjectGroup", "subjectgroup"),
        deep_get(data, "subjectGroup", "subjectgroup"),
    )
    sg_id = first_nonempty(
        deep_get(subject_group, "id", "subjectGroupId", "sgId") if subject_group else None,
        deep_get(assign_info, "subjectGroupId", "sgId"),
        deep_get(assign, "subjectGroupId", "sgId"),
        deep_get(data, "subjectGroupId", "sgId"),
    )

    return {
        "teacher": clean_html_text(teacher),
        "theme": clean_html_text(theme),
        "type": clean_html_text(type_name),
        "type_id": type_id,
        "subject_group_id": str(sg_id or "").strip(),
        "mark": str(mark_value).strip() if mark_value is not None else "",
        "raw": payload
    }

def fetch_subjectgroup_teacher_sync(sg_id, cookies_dict, at_token=None):
    """Достаёт учителя по subjectGroupId из /webapi/subjectgroups/{id}."""
    sg_id = str(sg_id or '').strip()
    if not sg_id:
        return ''
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://sgo.volganet.ru/app/school/studentdiary/",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest"
    }
    if at_token:
        headers["at"] = at_token
    try:
        url = f"{SGO_URL.rstrip('/')}/webapi/subjectgroups/{sg_id}"
        resp = requests.get(url, cookies=cookies_dict or {}, headers=headers, verify=False, timeout=8)
        if resp.status_code != 200:
            return ''
        payload = resp.json()
        norm = normalize_subjectgroup_detail(sg_id, payload) if 'normalize_subjectgroup_detail' in globals() else {}
        names = norm.get('teacher_names') or norm.get('teachers') or []
        if isinstance(names, list) and names:
            return clean_html_text(names[0])
        return clean_html_text(first_nonempty(
            deep_get(payload, 'teacherName', 'teacherFullName', 'teachersStr'),
            deep_get(deep_get(payload, 'teacher', 'teachers'), 'name', 'fullName', 'fio', 'teacherName')
        ))
    except Exception as e:
        print(f"subjectgroup teacher fetch error for {sg_id}: {e}")
        return ''

def fetch_sgo_assign_info_sync(assignment_id, student_id, cookies_dict, at_token=None):
    """
    Получает реальные сведения из того же XHR, что СГО вызывает при клике на оценку.
    В Network этот запрос обычно отображается как: <assignmentId>?studentId=<studentId>
    """
    if not is_real_assignment_id(assignment_id):
        return {}

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://sgo.volganet.ru/app/school/studentdiary/",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest"
    }
    if at_token:
        headers["at"] = at_token

    sid_q = f"?studentId={student_id}" if student_id else ""

    candidates = [
        # Реальный endpoint из Network СГО: /webapi/student/diary/assigns/{id}?studentId=...
        f"https://sgo.volganet.ru/webapi/student/diary/assigns/{assignment_id}{sid_q}",

        # Старые/альтернативные варианты
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
            if parsed and (not parsed.get("teacher") or parsed.get("teacher") in ("—", "-", "Не указан")):
                sg_teacher = fetch_subjectgroup_teacher_sync(parsed.get("subject_group_id"), cookies_dict, at_token)
                if sg_teacher:
                    parsed["teacher"] = sg_teacher

            # Даже если учителя нет, theme/type могут быть полезны.
            if parsed.get("teacher") or parsed.get("theme") or parsed.get("type") or parsed.get("subject_group_id"):
                print("assignInfo parsed:",
                      "teacher=", parsed.get("teacher"),
                      "theme=", (parsed.get("theme") or "")[:80],
                      "type=", parsed.get("type"),
                      "sg=", parsed.get("subject_group_id"))
                return parsed
        except Exception as e:
            print(f"assignInfo fetch error for {assignment_id}: {e}")

    return {}



@app.route('/api/assignment_info', methods=['POST', 'OPTIONS'])
@app.route('/api/assignment_info/', methods=['POST', 'OPTIONS'])
def api_assignment_info():
    """Возвращает сведения для модального окна оценки: учитель, тип работы, тема.

    Использует тот же endpoint СГО, который открывается при клике на оценку:
    /webapi/student/diary/assigns/{assignmentId}?studentId={studentId}.
    Тип работы в этом ответе иногда не приходит, поэтому дополнительно принимаем
    typeId/typeName из уже загруженного дневника и нормализуем по таблице TYPE_NAMES.
    """
    if request.method == 'OPTIONS':
        return jsonify({'success': True})

    data = _request_payload()
    login_val = (data.get('login') or '').strip()
    password_val = (data.get('password') or '').strip()
    school = get_requested_school(data)
    assignment_id = str(first_nonempty(data.get('assignment_id'), data.get('assignmentId'), data.get('id')) or '').strip()
    student_id = str(first_nonempty(data.get('student_id'), data.get('studentId')) or '').strip()
    fallback_type_id = first_nonempty(data.get('type_id'), data.get('typeId'))
    fallback_type = first_nonempty(data.get('type'), data.get('typeName'), data.get('workType'), data.get('assignmentType'))

    if not login_val or not password_val:
        return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400
    if not is_real_assignment_id(assignment_id):
        return jsonify({'success': False, 'error': 'Нет реального assignmentId: СГО прислал 0, поэтому подробный запрос невозможен'}), 200

    async def _fetch():
        ns = None
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            actual_school = await sgo_login_with_fallback(ns, login_val, password_val, school)
            cookies_dict, at_token = get_cookies_from_ns(ns)

            # Если studentId не пришёл с фронта, достаём его из diary/init.
            if not student_id:
                try:
                    headers = {'Accept': 'application/json, text/plain, */*', 'User-Agent': 'Mozilla/5.0'}
                    if at_token:
                        headers['at'] = at_token
                    init_resp = requests.get(SGO_URL.rstrip('/') + '/webapi/student/diary/init', cookies=cookies_dict or {}, headers=headers, verify=False, timeout=8)
                    if init_resp.status_code == 200:
                        init_json = init_resp.json()
                        student_id_local = str(init_json.get('currentStudentId') or init_json.get('studentId') or '').strip()
                        if (not student_id_local or student_id_local == '0') and isinstance(init_json.get('students'), list) and init_json.get('students'):
                            student_id_local = str(init_json.get('students')[0].get('studentId') or '').strip()
                        if (not student_id_local or student_id_local == '0') and isinstance(init_json.get('students'), dict) and init_json.get('students'):
                            cur = init_json.get('currentStudentId')
                            students = init_json.get('students') or {}
                            student_obj = students.get(cur) or students.get(str(cur)) or next(iter(students.values()))
                            student_id_local = str((student_obj or {}).get('studentId') or '').strip()
                        if student_id_local and student_id_local != '0':
                            nonlocal_student_id[0] = student_id_local
                except Exception as e:
                    print('assignment_info init studentId error:', e)

            sid = student_id or nonlocal_student_id[0]
            detail = fetch_sgo_assign_info_sync(assignment_id, sid, cookies_dict, at_token) or {}

            if detail and (not detail.get('teacher') or detail.get('teacher') in ('—', '-', 'Не указан')):
                sg_teacher = fetch_subjectgroup_teacher_sync(detail.get('subject_group_id'), cookies_dict, at_token)
                if sg_teacher:
                    detail['teacher'] = sg_teacher

            normalized_type = normalize_assignment_type_name(
                detail.get('type_id') or fallback_type_id,
                first_nonempty(detail.get('type'), fallback_type)
            )
            if normalized_type:
                detail['type'] = normalized_type
            if fallback_type_id and not detail.get('type_id'):
                try:
                    detail['type_id'] = int(fallback_type_id)
                except Exception:
                    detail['type_id'] = fallback_type_id

            try:
                await ns.logout()
            except Exception:
                pass
            return jsonify({'success': True, 'data': detail, 'school': actual_school, 'student_id': sid})
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            return jsonify({'success': False, 'error': str(e), 'data': {}}), 200

    nonlocal_student_id = ['']
    return run_async(_fetch())


@app.route("/api/sgo/check", methods=["POST", "OPTIONS"])
def sgo_check():
    if request.method == "OPTIONS":
        return jsonify({"success": True})
    data = (request.get_json(silent=True) or {}) if request.method == 'POST' else dict(request.args)
    login_val = (data.get("login") or "").strip()
    password_val = (data.get("password") or "").strip()
    school = get_requested_school(data)
    password_val, school = apply_saved_login_fields(login_val, password_val, school)

    if not login_val or not password_val:
        return jsonify({"success": False, "error": "Не переданы логин или пароль"}), 400

    async def _fetch():
        require_netschoolapi()
        ns = NetSchoolAPI(SGO_URL)
        actual_school = school
        try:
            actual_school = await asyncio.wait_for(sgo_login_with_fallback(ns, login_val, password_val, school), timeout=15)
            try:
                await ns.logout()
            except Exception:
                pass
            return jsonify({"success": True, "message": "Подключение к СГО успешно", "school": actual_school})
        except Exception as e:
            safe_print_traceback()
            return jsonify({
                "success": False,
                "error": str(e),
                "school": actual_school,
                "hint": "Проверьте логин, пароль и точное название школы. Можно задать школу через переменную окружения SGO_SCHOOL."
            }), 500

    return run_async(_fetch())


def build_empty_diary_range(start, end):
    """Безопасная заглушка дневника, если СГО временно недоступен.
    Возвращает дни выбранной недели, чтобы личный кабинет не падал из-за WinError/СГО.
    """
    result = []
    try:
        start_dt = datetime.strptime(str(start), "%Y-%m-%d")
        end_dt = datetime.strptime(str(end), "%Y-%m-%d")
    except Exception:
        return result

    cursor = start_dt
    while cursor <= end_dt:
        # Показываем учебные дни, воскресенье можно пропустить
        if cursor.weekday() < 6:
            result.append({"date": cursor.strftime("%Y-%m-%d"), "lessons": []})
        cursor += timedelta(days=1)
    return result

@app.route("/api/diary", methods=["GET", "POST", "OPTIONS"])
def diary_api():
    if request.method == "OPTIONS":
        return jsonify({"success": True})

    data = (request.get_json(silent=True) or {}) if request.method == 'POST' else dict(request.args)
    login_val = (data.get("login") or "").strip()
    password_val = (data.get("password") or "").strip()
    start = data.get("start")
    end = data.get("end")
    school = get_requested_school(data)
    include_attachments = truthy(data.get("include_attachments"))
    password_val, school = apply_saved_login_fields(login_val, password_val, school)

    if not login_val or not password_val or password_val == "local-password":
        return jsonify({"success": False, "error": "Не передан реальный пароль СГО. Введите пароль заново на странице входа."}), 400
    if not start or not end:
        return jsonify({"success": False, "error": "Не указан период start/end"}), 400

    async def _fetch():
        ns = None
        actual_school = school
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            print(f"Logging in as {login_val}...")
            actual_school = await asyncio.wait_for(sgo_login_with_fallback(ns, login_val, password_val, school), timeout=15)
            print(f"Login successful! School: {actual_school}")

            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")

            print(f"Fetching diary from {start} to {end}...")
            diary = await asyncio.wait_for(ns.diary(start_dt, end_dt), timeout=20)
            cookies_dict, at_token = get_cookies_from_ns(ns)
            student_class_name = ""
            student_id_for_assign = ""
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": SGO_URL.rstrip('/') + "/app/student/diary"
                }
                if at_token:
                    headers["at"] = at_token
                init_resp = requests.get(SGO_URL.rstrip('/') + "/webapi/student/diary/init", cookies=cookies_dict or {}, headers=headers, verify=False, timeout=8)
                if init_resp.status_code == 200:
                    init_json = init_resp.json()
                    student_class_name = _extract_student_class_from_init(init_json)
                    student_id_for_assign = str(init_json.get("currentStudentId") or init_json.get("studentId") or "").strip()
                    if not student_id_for_assign or student_id_for_assign == "0":
                        students_init = init_json.get("students") or []
                        if isinstance(students_init, list) and students_init:
                            student_id_for_assign = str(students_init[0].get("studentId") or "").strip()
                    if not student_class_name:
                        try:
                            report_json = _fetch_student_average_mark_report(cookies_dict, headers)
                            student_class_name = _extract_class_from_student_average_mark(report_json)
                        except Exception:
                            pass
                    if student_class_name:
                        try:
                            db = get_db()
                            cols = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
                            if "class_name" not in cols:
                                db.execute("ALTER TABLE users ADD COLUMN class_name TEXT DEFAULT ''")
                            db.execute("UPDATE users SET class_name = ? WHERE login = ?", (student_class_name, login_val))
                            db.commit()
                        except Exception:
                            pass
            except Exception as class_error:
                print("Could not get student class from diary/init:", class_error)
            print(f"Got {len(diary.schedule)} days")

            # Кэши на один запрос дневника: не дёргаем СГО повторно для одинаковых assignmentId/subjectGroupId/предметов.
            assign_info_cache = {}
            subject_teacher_cache = {}

            def cached_assign_info(aid):
                aid = str(aid or '').strip()
                if not is_real_assignment_id(aid):
                    return {}
                if aid not in assign_info_cache:
                    assign_info_cache[aid] = fetch_sgo_assign_info_sync(aid, student_id_for_assign, cookies_dict, at_token) or {}
                return assign_info_cache[aid]

            def teacher_from_assignment_id(aid, subject_name=''):
                detail = cached_assign_info(aid)
                teacher = clean_html_text(detail.get('teacher') or '') if detail else ''
                sg_id = str((detail or {}).get('subject_group_id') or '').strip()
                if (not teacher or teacher in ('—', '-', 'Не указан')) and sg_id:
                    if sg_id not in subject_teacher_cache:
                        subject_teacher_cache[sg_id] = fetch_subjectgroup_teacher_sync(sg_id, cookies_dict, at_token) or ''
                    teacher = clean_html_text(subject_teacher_cache.get(sg_id) or '')
                if teacher and teacher not in ('—', '-', 'Не указан') and subject_name:
                    subject_teacher_cache['subject:' + clean_html_text(subject_name).lower()] = teacher
                return teacher if teacher and teacher not in ('—', '-', 'Не указан') else ''

            res = []
            for day in diary.schedule:
                day_date = day.day if isinstance(day.day, str) else day.day.strftime("%Y-%m-%d")
                day_data = {"date": day_date, "class_name": student_class_name, "lessons": []}

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
                    lesson_room = lesson_room_value(lesson)

                    hw_list = []
                    for hw0 in lesson_homework_values(lesson, lesson_theme):
                        if hw0 and hw0 not in hw_list:
                            hw_list.append(hw0)

                    marks_details = []
                    lesson_attachments = []
                    lesson_assignments = []
                    first_real_assignment_id = ''

                    for assign in getattr(lesson, "assignments", []) or []:
                        hw = homework_text(assign, lesson_theme)
                        if hw and hw not in hw_list:
                            hw_list.append(hw)

                        assign_api = _assignment_to_api(assign) if "_assignment_to_api" in globals() else {
                            "id": str(get_assignment_id(assign) or ""),
                            "title": clean_html_text(assignment_content(assign) or lesson_theme or "Задание"),
                            "raw": _safe_jsonable(assign) if "_safe_jsonable" in globals() else obj_to_dict(assign),
                        }

                        # ВАЖНО: netschoolapi.attachments() фактически принимает ID задания.
                        # Получаем вложения сразу во время разбора diary(), пока есть настоящий Assignment.
                        if include_attachments:
                            assign_attachments = await _attachments_for_assignment(ns, assign, cookies_dict, at_token)
                            if assign_attachments:
                                print(f"Diary attachments: assignment {assign_api.get('id')} -> {len(assign_attachments)} file(s)")
                        else:
                            assign_attachments = _extract_attachments_from_any(assign) if "_extract_attachments_from_any" in globals() else []

                        if assign_attachments:
                            for att in assign_attachments:
                                att = dict(att)
                                att["assignment_id"] = assign_api.get("id", "")
                                att["assignment_title"] = assign_api.get("title", "Задание")
                                lesson_attachments.append(att)

                        assign_api["attachments"] = assign_attachments
                        lesson_assignments.append(assign_api)
                        if not first_real_assignment_id and is_real_assignment_id(assign_api.get("id")):
                            first_real_assignment_id = str(assign_api.get("id") or '')

                        mark_value = assignment_mark_value(assign)
                        if mark_value is None or not str(mark_value).strip():
                            continue

                        mark_detail = build_mark_detail_from_diary_context(assign, lesson, day_date)

                        # Дополняем сведения тем же webapi-запросом, который СГО выполняет
                        # при клике на оценку. Так подтягиваются реальный учитель и тип работы.
                        aid = mark_detail.get("assignment_id") or str(get_assignment_id(assign) or "")
                        if is_real_assignment_id(aid):
                            extra_detail = cached_assign_info(aid)
                            if extra_detail:
                                extra_teacher = clean_html_text(extra_detail.get("teacher") or "")
                                extra_type = clean_html_text(extra_detail.get("type") or "")
                                extra_theme = clean_html_text(extra_detail.get("theme") or "")
                                extra_mark = str(extra_detail.get("mark") or "").strip()
                                if extra_teacher and extra_teacher not in ("—", "-", "Не указан"):
                                    mark_detail["teacher"] = extra_teacher
                                if extra_type and extra_type not in ("—", "-"):
                                    mark_detail["type"] = extra_type
                                if extra_theme and extra_theme not in ("—", "-", "Не указана"):
                                    mark_detail["theme"] = extra_theme
                                    mark_detail["assignmentName"] = extra_theme
                                if extra_mark:
                                    mark_detail["value"] = extra_mark
                                if extra_detail.get("type_id"):
                                    mark_detail["type_id"] = extra_detail.get("type_id")
                                mark_detail["source"] = "diary-object-context+assignInfo"

                        marks_details.append(mark_detail)

                    if (not lesson_teacher or lesson_teacher in ("—", "-", "Не указан")):
                        lesson_teacher = subject_teacher_cache.get('subject:' + clean_html_text(lesson_subject).lower(), '') or teacher_from_assignment_id(first_real_assignment_id, lesson_subject) or "Не указан"
                        for md in marks_details:
                            if (not md.get("teacher") or md.get("teacher") in ("—", "-", "Не указан")) and lesson_teacher != "Не указан":
                                md["teacher"] = lesson_teacher

                    day_data["lessons"].append({
                        "subject": lesson_subject,
                        "class_name": student_class_name,
                        "room": lesson_room,
                        "cabinet": lesson_room,
                        "hw": hw_list,
                        "details": marks_details,
                        "teacher": lesson_teacher,
                        "theme": lesson_theme,
                        "assignments": lesson_assignments,
                        "attachments": lesson_attachments,
                    })

                res.append(day_data)

            await ns.logout()
            print(f"Diary data ready: {len(res)} days")
            return jsonify({"success": True, "data": res})

        except Exception as e:
            print(f"Diary error: {e}")
            safe_print_traceback()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            # Личный кабинет не должен падать, если СГО/консоль Windows вернули WinError 233.
            # Возвращаем пустую неделю с предупреждением вместо ошибки.
            return jsonify({
                "success": True,
                "data": build_empty_diary_range(start, end),
                "warning": "СГО временно недоступен: " + str(e),
                "school": actual_school
            }), 200

    try:
        return run_async(_fetch())
    except Exception as e:
        print(f"Diary fatal error: {e}")
        safe_print_traceback()
        return jsonify({
            "success": True,
            "data": build_empty_diary_range(start, end),
            "warning": "СГО временно недоступен: " + str(e),
            "school": school
        }), 200


@app.route("/api/sgo/diary", methods=["GET", "POST", "OPTIONS"])
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
        actual_school = school
        try:
            actual_school = await asyncio.wait_for(sgo_login_with_fallback(ns, login_val, password_val, school), timeout=15)
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
                            "keys": list((_safe_jsonable(assign) if '_safe_jsonable' in globals() else obj_to_dict(assign)).keys())[:80],
                            "raw": {k: str(v)[:300] for k, v in (_safe_jsonable(assign) if '_safe_jsonable' in globals() else obj_to_dict(assign)).items() if k in ("id","typeId","type_id","typeName","name","content","text","description","mark","homework","assignmentName")},
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
            safe_print_traceback()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            return jsonify({"success": False, "error": str(e), "school": actual_school}), 200

    return run_async(_fetch())



@app.route("/api/debug/mark-details", methods=["POST"])
def debug_mark_details():
    """Возвращает только оценки с темой/учителем — удобно проверять без интерфейса."""
    response = diary_api()
    return response


# ===================== REPORT =====================
@app.route("/api/report", methods=["POST"])
def report():
    data = (request.get_json(silent=True) or {}) if request.method == 'POST' else dict(request.args)
    login_val = (data.get("login") or "").strip()
    password_val = (data.get("password") or "").strip()
    start = data.get("start")
    end = data.get("end")
    school = get_requested_school(data)
    password_val, school = apply_saved_login_fields(login_val, password_val, school)

    if not login_val or not password_val:
        return jsonify({"success": False, "error": "Не переданы логин или пароль"}), 400
    if not start or not end:
        return jsonify({"success": False, "error": "Не указан период start/end"}), 400

    async def _fetch():
        require_netschoolapi()
        ns = NetSchoolAPI(SGO_URL)
        actual_school = school
        try:
            actual_school = await asyncio.wait_for(sgo_login_with_fallback(ns, login_val, password_val, school), timeout=15)
            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")

            from collections import defaultdict
            grid = defaultdict(lambda: defaultdict(list))
            all_dates = set()

            cur = start_dt
            while cur <= end_dt:
                week_end = min(cur + timedelta(days=6), end_dt)
                try:
                    diary = await asyncio.wait_for(ns.diary(cur, week_end), timeout=20)
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
            safe_print_traceback()
            try:
                await ns.logout()
            except Exception:
                pass
            return jsonify({"success": False, "error": str(e), "school": actual_school}), 200

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
    # Раньше здесь было honeypot-поле website.
    # Браузеры иногда автоматически заполняют скрытые поля, из-за этого реальные сообщения
    # ошибочно отклонялись как спам. Поэтому проверку убрали.

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
    except smtplib.SMTPAuthenticationError:
        print("MAIL ERROR: Gmail authentication failed")
        safe_print_traceback()
        return jsonify({
            "success": False,
            "error": "Gmail отклонил вход. Создайте новый пароль приложения Google и укажите его в SMTP_PASSWORD."
        }), 500
    except Exception as e:
        print("MAIL ERROR:", e)
        safe_print_traceback()
        return jsonify({
            "success": False,
            "error": f"Не удалось отправить письмо через SMTP: {type(e).__name__}. Проверьте интернет, порт 587 и SMTP-настройки."
        }), 500


# ===================== ANNOUNCEMENTS =====================
def _is_local_demo_announcement(item):
    """Отсекает старые локальные демонстрационные объявления сайта, если они попали в ответ."""
    try:
        title = clean_html_text((item or {}).get('title', '')).lower()
        author = clean_html_text((item or {}).get('author', '')).lower()
        local_titles = ('конец учебного года', 'день открытых дверей', 'родительское собрание')
        local_authors = ('директор школы', 'завуч', 'администрация школы')
        return any(x in title for x in local_titles) or author in local_authors
    except Exception:
        return False

@app.route("/api/announcements", methods=["GET", "POST", "OPTIONS"])
def get_announcements():
    """Возвращает только реальные объявления из СГО. Локальные объявления больше не подмешиваются."""
    if request.method == "OPTIONS":
        return jsonify({"success": True})

    data = _request_payload()
    login_val = (data.get('login') or '').strip()
    password_val = (data.get('password') or '').strip()
    school = get_requested_school(data)

    if not login_val or not password_val:
        return jsonify({"success": False, "error": "Не переданы логин или пароль", "data": []}), 200

    async def _fetch():
        ns = None
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            actual_school = await sgo_login_with_fallback(ns, login_val, password_val, school)
            items = await ns.announcements()
            try:
                await ns.logout()
            except Exception:
                pass
            data_items = [_announcement_to_api(x) for x in (items or [])]
            data_items = [x for x in data_items if not _is_local_demo_announcement(x)]
            return jsonify({"success": True, "data": data_items, "school": actual_school, "cached": False})
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            return jsonify({"success": False, "error": str(e), "data": [], "cached": False}), 200
    return run_async(_fetch())



# ===================== TEACHER / SCHOOL CABINET =====================
def _teacher_first_nonempty(*values, default=''):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in ('none', 'null', 'не указано'):
            return text
    return default


def _teacher_class_from_lesson(lesson):
    data = obj_to_dict(lesson)
    return _teacher_first_nonempty(
        getattr(lesson, 'class_name', None),
        getattr(lesson, 'className', None),
        getattr(lesson, 'groupName', None),
        getattr(lesson, 'classroom', None),
        deep_get(data, 'className'),
        deep_get(data, 'class', 'name'),
        deep_get(data, 'group', 'name'),
        deep_get(data, 'eduGroup', 'name'),
        deep_get(data, 'studentsGroup', 'name'),
        default='—'
    )


def _teacher_lesson_number(lesson, fallback=1):
    data = obj_to_dict(lesson)
    raw = _teacher_first_nonempty(
        getattr(lesson, 'number', None), getattr(lesson, 'lessonNumber', None),
        deep_get(data, 'number'), deep_get(data, 'lessonNumber'), default=str(fallback)
    )
    return raw


def _teacher_weekday(date_str):
    names = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
    try:
        return names[datetime.strptime(date_str, '%Y-%m-%d').weekday()]
    except Exception:
        return ''


def _collect_teacher_schedule_from_diary(diary):
    result = []
    for day in getattr(diary, 'schedule', []) or []:
        day_date = day.day if isinstance(day.day, str) else day.day.strftime('%Y-%m-%d')
        lessons = []
        for idx, lesson in enumerate(getattr(day, 'lessons', []) or [], 1):
            lessons.append({
                'number': _teacher_lesson_number(lesson, idx),
                'subject': _teacher_first_nonempty(getattr(lesson, 'subject', None), deep_get(obj_to_dict(lesson), 'subjectName'), deep_get(obj_to_dict(lesson), 'subject'), default='—'),
                'class_name': _teacher_class_from_lesson(lesson),
                'room': _teacher_first_nonempty(getattr(lesson, 'room', None), deep_get(obj_to_dict(lesson), 'room'), default=''),
                'theme': lesson_theme_value(lesson) or '',
            })
        if lessons:
            result.append({'date': day_date, 'weekday': _teacher_weekday(day_date), 'lessons': lessons})
    return result



# ================== TEACHER JOURNAL DIRECT SGO HELPERS ==================
# Для преподавателя НЕ используем ns.diary(): он вызывает ученический
# /webapi/student/diary/init и СГО отвечает 401. После авторизации берём cookies
# и работаем напрямую со страницей /app/school/journal/ и JSON webapi.

def sgo_abs(path):
    base = SGO_URL.rstrip('/')
    if str(path).startswith('http'):
        return path
    return base + '/' + str(path).lstrip('/')

def sgo_session_from_cookies(cookies_dict, at_token=None):
    sess = requests.Session()
    sess.verify = False
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': SGO_URL.rstrip('/') + '/app/school/journal/',
    }
    # Ключевой заголовок для teacher JSON API СГО 5.51.
    if at_token:
        headers['at'] = str(at_token).strip()
    sess.headers.update(headers)
    for k, v in (cookies_dict or {}).items():
        try:
            sess.cookies.set(k, v, domain='sgo.volganet.ru')
        except Exception:
            sess.cookies.set(k, v)
    return sess


def fetch_at_token_from_sgo(cookies_dict):
    """Явно получает at-токен через /webapi/auth/getdata.

    Это надёжнее рефлексивного поиска токена в объекте NetSchoolAPI,
    потому что getdata всегда возвращает at в теле ответа если сессия жива.
    Возвращает строку токена или None.
    """
    try:
        url = SGO_URL.rstrip('/') + '/webapi/auth/getdata'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': SGO_URL.rstrip('/') + '/app/school/journal/',
        }
        resp = requests.get(url, cookies=cookies_dict, headers=headers, verify=False, timeout=4, allow_redirects=True)
        print(f'fetch_at_token GET {url} -> HTTP {resp.status_code}')
        if resp.status_code == 200:
            try:
                d = resp.json()
                token = d.get('at') or d.get('AT') or d.get('token') or d.get('access_token') or d.get('auth_token')
                if token:
                    print(f'fetch_at_token: got at-token from getdata')
                    return str(token).strip()
            except Exception:
                pass
        # Fallback: /webapi/context
        url2 = SGO_URL.rstrip('/') + '/webapi/context'
        resp2 = requests.get(url2, cookies=cookies_dict, headers=headers, verify=False, timeout=4, allow_redirects=True)
        print(f'fetch_at_token GET {url2} -> HTTP {resp2.status_code}')
        if resp2.status_code == 200:
            try:
                d2 = resp2.json()
                token2 = d2.get('at') or d2.get('AT')
                if token2:
                    print(f'fetch_at_token: got at-token from context')
                    return str(token2).strip()
            except Exception:
                pass
    except Exception as e:
        print(f'fetch_at_token error: {e}')
    return None

def _json_or_none(resp):
    text = resp.text or ''
    ctype = (resp.headers.get('Content-Type') or '').lower()
    if 'json' not in ctype and not text.lstrip().startswith(('{', '[')):
        return None
    try:
        return resp.json()
    except Exception:
        return None

def sgo_request_json(sess, method, path, params=None, timeout=2):
    # Короткий timeout + печать каждого запроса: больше нет "вечной" загрузки без причины.
    url = sgo_abs(path)
    params = params or {}
    errors = []
    started = time.time()
    try:
        if method == 'POST':
            attempts = (
                ('json', lambda: sess.post(url, json=params, timeout=timeout, allow_redirects=True)),
                ('query', lambda: sess.post(url, params=params, timeout=timeout, allow_redirects=True)),
            )
        else:
            attempts = (('query', lambda: sess.get(url, params=params, timeout=timeout, allow_redirects=True)),)
        for mode, do in attempts:
            resp = do()
            elapsed = round(time.time() - started, 2)
            data = _json_or_none(resp)
            print(f'SGO {method} {url} mode={mode} params={params} -> HTTP {resp.status_code} in {elapsed}s')
            if resp.status_code == 200 and data is not None:
                return data, None
            errors.append(f'HTTP {resp.status_code}: {(resp.text or "")[:120]}')
        return None, f'{method} {url} {params} -> ' + ' | '.join(errors)
    except Exception as e:
        elapsed = round(time.time() - started, 2)
        print(f'SGO {method} {url} params={params} -> {type(e).__name__}: {e} in {elapsed}s')
        return None, f'{method} {url} {params} -> {type(e).__name__}: {e}'


def _iso_range_for_sgo(start, end):
    """Return ISO timestamps accepted by /webapi/schedule/classmeetings."""
    def norm(value, suffix):
        text = str(value or '').strip()
        match = re.search(r'\d{4}-\d{2}-\d{2}', text)
        if not match:
            return text
        return match.group(0) + suffix
    return norm(start, 'T00:00:00.000Z'), norm(end, 'T23:59:59.999Z')


def _collect_sg_ids_from_payload(payload, limit=120):
    ids, seen = [], set()
    for item in _walk_json(payload):
        if not isinstance(item, dict):
            continue
        keys = {str(k).lower() for k in item.keys()}
        value = first_nonempty(
            item.get('sgId'),
            item.get('sgid'),
            item.get('subjectGroupId'),
            item.get('subjectgroupId'),
            item.get('subject_group_id'),
        )
        if not value and 'id' in item and (
            keys & {'subject', 'subjectname', 'subjectgroup', 'class', 'classname',
                    'lesson', 'lessonid', 'classmeetings', 'classmeeting', 'room',
                    'teacher', 'teachers'}
        ):
            value = item.get('id')
        sgid = str(value or '').strip()
        if sgid.isdigit() and sgid not in seen:
            seen.add(sgid)
            ids.append(sgid)
            if len(ids) >= limit:
                break
    return ids


def fetch_sgo_classjournal_dashboard(sess):
    """GET /webapi/dashboard/extensions/classJournal."""
    debug = []
    data, err = sgo_request_json(sess, 'GET', '/webapi/dashboard/extensions/classJournal', {}, timeout=8)
    if data is not None:
        ids = _collect_sg_ids_from_payload(data)
        debug.append(f'GET /webapi/dashboard/extensions/classJournal -> JSON OK; sgIds={len(ids)}')
        return data, ids, debug
    debug.append(err or 'GET /webapi/dashboard/extensions/classJournal -> no JSON')
    return None, [], debug



def _room_subject_from_name(roomname):
    """Пытается получить предмет/назначение кабинета из названия кабинета СГО."""
    text = re.sub(r'^\s*\d+\s*', '', str(roomname or '').strip())
    text = re.sub(r'\s+', ' ', text).strip(' -–—')
    return text or str(roomname or '').strip() or 'Кабинет'


def build_staff_from_rooms_payload(rooms_payload):
    """Строит каталог педсостава по /webapi/rooms: responsible -> кабинеты/предметные кабинеты."""
    staff_map = {}
    rooms = rooms_payload if isinstance(rooms_payload, list) else []
    for room in rooms:
        if not isinstance(room, dict):
            continue
        responsible = room.get('responsible') or {}
        if not isinstance(responsible, dict):
            responsible = {}
        name = str(responsible.get('name') or '').strip()
        if not name:
            continue
        rid = str(responsible.get('id') or name).strip()
        item = staff_map.setdefault(rid, {
            'id': rid,
            'name': name,
            'subjects': [],
            'classes': [],
            'rooms': [],
            'subjectgroups': 0,
            'source': 'rooms'
        })
        roomname = str(room.get('roomname') or '').strip()
        subject = _room_subject_from_name(roomname)
        if subject and subject not in item['subjects']:
            item['subjects'].append(subject)
        if roomname and roomname not in item['rooms']:
            item['rooms'].append(roomname)
    for item in staff_map.values():
        item['subjects'].sort(key=lambda x: str(x).lower())
        item['rooms'].sort(key=lambda x: str(x).lower())
    return sorted(staff_map.values(), key=lambda x: str(x.get('name') or '').lower())


def merge_staff_catalogs(*catalogs):
    """Объединяет педсостав из subjectgroups и rooms без дублей по id/ФИО."""
    merged = {}
    def key_for(item):
        ident = str((item or {}).get('id') or (item or {}).get('teacher_id') or '').strip()
        name = str((item or {}).get('name') or '').strip().lower()
        return ident or name
    for catalog in catalogs:
        for raw in (catalog or []):
            if not isinstance(raw, dict):
                continue
            key = key_for(raw)
            if not key:
                continue
            item = merged.setdefault(key, {
                'id': raw.get('id') or raw.get('teacher_id') or '',
                'name': raw.get('name') or 'Преподаватель',
                'subjects': [],
                'classes': [],
                'rooms': [],
                'subjectgroups': 0,
                'source': ''
            })
            if raw.get('name') and (not item.get('name') or item.get('name') == 'Преподаватель'):
                item['name'] = raw.get('name')
            for field in ('subjects', 'classes', 'rooms'):
                vals = raw.get(field) or []
                if isinstance(vals, str):
                    vals = [vals]
                for val in vals:
                    val = str(val or '').strip()
                    if val and val not in item[field]:
                        item[field].append(val)
            try:
                item['subjectgroups'] += int(raw.get('subjectgroups') or 0)
            except Exception:
                pass
            src = str(raw.get('source') or '').strip()
            if src and src not in str(item.get('source') or ''):
                item['source'] = (str(item.get('source') or '') + ', ' + src).strip(', ')
    for item in merged.values():
        for field in ('subjects', 'classes', 'rooms'):
            item[field].sort(key=lambda x: str(x).lower())
    return sorted(merged.values(), key=lambda x: str(x.get('name') or '').lower())


def fetch_sgo_rooms_staff(sess):
    """GET /webapi/rooms и педсостав по ответственным за кабинеты."""
    debug = []
    data, err = sgo_request_json(sess, 'GET', '/webapi/rooms', {}, timeout=8)
    if data is not None:
        rooms = data if isinstance(data, list) else []
        staff = build_staff_from_rooms_payload(rooms)
        debug.append(f'GET /webapi/rooms -> JSON OK; rooms={len(rooms)}; responsible_staff={len(staff)}')
        return rooms, staff, debug
    debug.append(err or 'GET /webapi/rooms -> no JSON')
    return [], [], debug


def build_staff_from_teachers_payload(teachers_payload):
    """Строит каталог всех преподавателей школы по /webapi/users/staff/teachers?withSubjects=true."""
    result = []
    teachers = teachers_payload if isinstance(teachers_payload, list) else []
    for teacher in teachers:
        if not isinstance(teacher, dict):
            continue
        name = str(teacher.get('name') or '').strip()
        if not name or name.lower() == 'admin':
            continue
        tid = str(teacher.get('id') or name).strip()
        subjects = []
        for subj in (teacher.get('subjects') or []):
            if isinstance(subj, dict):
                value = str(subj.get('name') or '').strip()
            else:
                value = str(subj or '').strip()
            if value and value not in subjects:
                subjects.append(value)
        subjects.sort(key=lambda x: str(x).lower())
        result.append({
            'id': tid,
            'name': name,
            'subjects': subjects,
            'classes': [],
            'rooms': [],
            'subjectgroups': 0,
            'source': 'staff/teachers'
        })
    return sorted(result, key=lambda x: str(x.get('name') or '').lower())


def fetch_sgo_staff_teachers(sess):
    """GET /webapi/users/staff/teachers?withSubjects=true: полный педсостав с предметами."""
    debug = []
    data, err = sgo_request_json(sess, 'GET', '/webapi/users/staff/teachers', {'withSubjects': 'true'}, timeout=10)
    if data is not None:
        teachers = data if isinstance(data, list) else []
        staff = build_staff_from_teachers_payload(teachers)
        debug.append(f'GET /webapi/users/staff/teachers?withSubjects=true -> JSON OK; teachers={len(teachers)}; staff={len(staff)}')
        return teachers, staff, debug
    debug.append(err or 'GET /webapi/users/staff/teachers?withSubjects=true -> no JSON')
    return [], [], debug

def fetch_sgo_subjectgroup_detail(sess, sgid):
    sgid = str(sgid or '').strip()
    if not sgid:
        return None, 'empty sgid'
    return sgo_request_json(sess, 'GET', f'/webapi/subjectgroups/{sgid}', {}, timeout=8)


def fetch_sgo_student_list(sess, sgid):
    sgid = str(sgid or '').strip()
    if not sgid:
        return None, 'empty sgid'
    return sgo_request_json(sess, 'GET', '/webapi/grade/studentList', {'sgId': sgid}, timeout=8)


def fetch_sgo_schedule_by_sgid(sess, sgid, start, end):
    sgid = str(sgid or '').strip()
    if not sgid:
        return None, 'empty sgid'
    start_iso, end_iso = _iso_range_for_sgo(start, end)
    params = {
        'sgId': sgid,
        'start': start_iso,
        'end': end_iso,
        'expand': ['lesson', 'room', 'time', 'teacherId'],
    }
    return sgo_request_json(sess, 'GET', '/webapi/schedule/classmeetings', params, timeout=10)


def normalize_subjectgroup_detail(sgid, detail):
    if isinstance(detail, list) and detail:
        detail = detail[0]
    if not isinstance(detail, dict):
        return {}
    subj = detail.get('subject') or detail.get('lesson') or {}
    cls = detail.get('class') or detail.get('group') or detail.get('grade') or {}
    room = detail.get('room') or {}
    teachers_raw = detail.get('teachers') or detail.get('teacher') or []
    if isinstance(teachers_raw, dict):
        teachers_raw = [teachers_raw]
    elif isinstance(teachers_raw, str):
        teachers_raw = [{'name': teachers_raw}]

    teacher_names, teacher_ids = [], []
    for direct_key in ('teacherId', 'teacherID', 'teacher_id', 'employeeId', 'employeeID'):
        if detail.get(direct_key):
            tid = str(detail.get(direct_key) or '').strip()
            if tid and tid not in teacher_ids:
                teacher_ids.append(tid)
    for teacher in teachers_raw if isinstance(teachers_raw, list) else []:
        if not isinstance(teacher, dict):
            continue
        tid = str(teacher.get('id') or teacher.get('teacherId') or teacher.get('teacher_id') or teacher.get('employeeId') or '').strip()
        name = str(first_nonempty(teacher.get('name'), teacher.get('fullName'), teacher.get('fio'), teacher.get('teacherName')) or '').strip()
        if tid and tid not in teacher_ids:
            teacher_ids.append(tid)
        if name and name not in teacher_names:
            teacher_names.append(name)

    subject_name = first_nonempty(_value_by_keys(subj, ['name', 'subjectName', 'title', 'shortName']), detail.get('subjectName'), detail.get('name'), detail.get('fullName'))
    class_name = first_nonempty(_value_by_keys(cls, ['name', 'className', 'title']), detail.get('className'))
    grade = first_nonempty(_value_by_keys(cls, ['grade']), detail.get('grade'))
    room_name = first_nonempty(_value_by_keys(room, ['name', 'title', 'number']), detail.get('roomName'))
    return {
        'subject': str(subject_name or '').strip(),
        'class_name': str(class_name or grade or '').strip(),
        'grade': str(grade or '').strip(),
        'room': str(room_name or '').strip(),
        'teacher_ids': teacher_ids,
        'teachers': teacher_names,
        'teacher_names': teacher_names,
        'term_ids': [str(x) for x in (detail.get('terms') or [])],
        'sg_id': str(sgid),
        'class_id': str(_value_by_keys(cls, ['id', 'classId']) or detail.get('classId') or ''),
        'subject_id': str(_value_by_keys(subj, ['id', 'subjectId']) or detail.get('subjectId') or ''),
        'full_name': str(detail.get('fullName') or subject_name or '').strip(),
    }


def normalize_students_list(data):
    """Нормализует ответ /webapi/grade/studentList в список учеников с реальным ФИО и id.

    В разных версиях СГО поля могут называться по-разному: fullName/name/fio
    или отдельно lastName/firstName/middleName. Для вывода класса берём именно
    ФИО/имя-фамилию, а не технические id.
    """
    out = []
    for item in _walk_json(data):
        if not isinstance(item, dict):
            continue
        id_candidates = [
            item.get('id'), item.get('studentId'), item.get('StudentId'),
            item.get('personId'), item.get('pupilId'), item.get('userId')
        ]
        id_aliases = []
        for value in id_candidates:
            text = str(value or '').strip()
            if text and text not in id_aliases:
                id_aliases.append(text)
        sid = id_aliases[0] if id_aliases else None
        parts = [
            first_nonempty(item.get('lastName'), item.get('surname'), item.get('familyName')),
            first_nonempty(item.get('firstName'), item.get('givenName')),
            first_nonempty(item.get('middleName'), item.get('patronymic')),
        ]
        name_from_parts = ' '.join(str(x).strip() for x in parts if x and str(x).strip())
        name = first_nonempty(
            item.get('fullName'), item.get('fio'), item.get('studentName'),
            item.get('displayName'), item.get('name'), name_from_parts
        )
        if sid and name:
            rec = {'id': str(sid), 'studentId': str(sid), 'idAliases': id_aliases, 'name': str(name).strip(), 'fullName': str(name).strip()}
            if not any(x['id'] == rec['id'] for x in out):
                out.append(rec)
    return sorted(out, key=lambda x: str(x.get('fullName') or x.get('name') or '').lower())


def fetch_sgo_average_marks(sess, sgid_or_list):
    """Средние оценки по предметной группе через реальный endpoint СГО."""
    raw = sgid_or_list if isinstance(sgid_or_list, (list, tuple, set)) else [sgid_or_list]
    sgids = []
    for x in raw:
        text = str(x or '').strip()
        if not text:
            continue
        try:
            sgids.append(int(text))
        except ValueError:
            sgids.append(text)
    if not sgids:
        return None, 'empty sgId'
    return sgo_request_json(sess, 'POST', '/webapi/v2/average-marks', {'sgId': sgids, 'groupBy': ['StudentId']}, timeout=10)


def normalize_average_marks(data):
    """Возвращает [{'studentId': '...', 'average': 4.25}] из разных форм ответа average-marks."""
    out = []
    avg_keys = [
        'average', 'avg', 'avgMark', 'average_mark', 'markAvg', 'mark_average',
        'value', 'mark', 'markValue', 'averageMark', 'averageValue',
        'weightedAverage', 'result', 'score'
    ]
    name_keys = ['fullName', 'fio', 'studentName', 'displayName', 'name']

    def extract_average(value):
        if isinstance(value, dict):
            return first_nonempty(*[value.get(key) for key in avg_keys], _value_by_keys(value, avg_keys))
        return value

    def add_average(sid, avg):
        avg = extract_average(avg)
        if sid is None or avg is None:
            return
        text = str(avg).replace(',', '.').strip()
        try:
            avg_val = round(float(text), 2)
        except Exception:
            avg_val = avg
        rec = {'studentId': str(sid), 'id': str(sid), 'average': avg_val}
        if not any(x['studentId'] == rec['studentId'] for x in out):
            out.append(rec)

    for item in _walk_json(data):
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            key_text = str(key or '').strip()
            if not key_text.isdigit():
                continue
            if isinstance(value, dict):
                mapped_avg = first_nonempty(*[value.get(key) for key in avg_keys])
                add_average(key_text, mapped_avg)
            elif isinstance(value, (int, float, str)):
                add_average(key_text, value)
        student_obj = item.get('student') if isinstance(item.get('student'), dict) else {}
        pupil_obj = item.get('pupil') if isinstance(item.get('pupil'), dict) else {}
        person_obj = item.get('person') if isinstance(item.get('person'), dict) else {}
        sid = first_nonempty(
            item.get('studentId'), item.get('StudentId'), item.get('student_id'),
            item.get('id'), item.get('personId'), item.get('pupilId'),
            item.get('student') if not isinstance(item.get('student'), dict) else None,
            item.get('pupil') if not isinstance(item.get('pupil'), dict) else None,
            item.get('person') if not isinstance(item.get('person'), dict) else None,
            student_obj.get('id'), student_obj.get('studentId'),
            pupil_obj.get('id'), person_obj.get('id')
        )
        avg = first_nonempty(*[item.get(key) for key in avg_keys])
        before = len(out)
        add_average(sid, avg)
        if sid is not None and len(out) > before:
            name = first_nonempty(
                *[item.get(key) for key in name_keys],
                *[student_obj.get(key) for key in name_keys],
                *[pupil_obj.get(key) for key in name_keys],
                *[person_obj.get(key) for key in name_keys],
            )
            if name:
                out[-1]['name'] = str(name).strip()
                out[-1]['fullName'] = str(name).strip()
    return out


def fetch_sgo_subjectgroup_bundle(sess, sgids, start, end, limit=80):
    """Fetch detail, students and expanded schedule for every sgId."""
    details, debug, schedule_all = {}, [], []
    seen = []
    for sgid in sgids or []:
        sgid = str(sgid or '').strip()
        if sgid and sgid not in seen:
            seen.append(sgid)

    for sgid in seen[:max(1, int(limit or 80))]:
        item = {'found': False, 'students': [], 'students_count': 0, 'schedule': [], 'schedule_count': 0}

        detail, err = fetch_sgo_subjectgroup_detail(sess, sgid)
        if detail is not None:
            item['found'] = True
            item['detail'] = detail
            item['normalized'] = normalize_subjectgroup_detail(sgid, detail)
            debug.append(f'GET /webapi/subjectgroups/{sgid} -> JSON OK')
        else:
            debug.append(f'GET /webapi/subjectgroups/{sgid} -> {err}')

        students, err = fetch_sgo_student_list(sess, sgid)
        if students is not None:
            item['students'] = normalize_students_list(students)
            item['students_count'] = len(item['students'])
            debug.append(f'GET /webapi/grade/studentList?sgId={sgid} -> JSON OK ({item["students_count"]} students)')
        else:
            debug.append(f'GET /webapi/grade/studentList?sgId={sgid} -> {err}')

        item['averages'] = []
        item['averages_count'] = 0
        debug.append(f'POST /webapi/v2/average-marks sgId={sgid} skipped: средние оценки отключены')

        sched, err = fetch_sgo_schedule_by_sgid(sess, sgid, start, end)
        if sched is not None:
            arr = sched if isinstance(sched, list) else (
                (sched.get('items') or sched.get('data') or sched.get('classMeetings') or [])
                if isinstance(sched, dict) else []
            )
            if isinstance(arr, list):
                item['schedule'] = arr
                item['schedule_count'] = len(arr)
                schedule_all.extend(arr)
            debug.append(f'GET /webapi/schedule/classmeetings?sgId={sgid} -> JSON OK ({item["schedule_count"]} lessons)')
        else:
            debug.append(f'GET /webapi/schedule/classmeetings?sgId={sgid} -> {err}')

        details[sgid] = item
    return details, schedule_all, debug


def empty_journal_from_students_schedule(students, schedule, sgid='', sg_info=None, start='', end=''):
    """Build a journal grid shell from studentList + schedule/classmeetings."""
    sg_info = sg_info or {}
    norm_students = normalize_students_list(students) if students is not None else []
    students_names = [x.get('name') for x in norm_students if x.get('name')]
    columns = []
    seen_cols = set()
    for entry in schedule or []:
        if not isinstance(entry, dict):
            continue
        if sgid and str(entry.get('subjectGroupId') or '') != str(sgid):
            continue
        date_match = re.search(r'\d{4}-\d{2}-\d{2}', str(entry.get('day') or entry.get('date') or ''))
        date_value = date_match.group(0) if date_match else ''
        if start and date_value and date_value < start:
            continue
        if end and date_value and date_value > end:
            continue
        col_id = str(entry.get('id') or entry.get('classMeetingId') or f'{date_value}:{entry.get("number") or ""}')
        if not col_id or col_id in seen_cols:
            continue
        seen_cols.add(col_id)
        lesson = entry.get('lesson') if isinstance(entry.get('lesson'), dict) else {}
        title = first_nonempty(
            lesson.get('name') if isinstance(lesson, dict) else '',
            lesson.get('title') if isinstance(lesson, dict) else '',
            entry.get('theme'),
            entry.get('lessonName'),
            'Урок',
        )
        columns.append({
            'id': col_id,
            'date': date_value,
            'title': title,
            'type': 'Урок',
            'lesson': entry.get('number') or entry.get('lessonNumber') or '',
            'classMeetingId': str(entry.get('id') or ''),
        })
    columns.sort(key=lambda c: (str(c.get('date') or ''), str(c.get('lesson') or ''), str(c.get('id') or '')))
    return {
        'students': students_names,
        'dates': sorted({c.get('date') for c in columns if c.get('date')}),
        'columns': columns,
        'grid': {name: {str(c.get('id')): [] for c in columns} for name in students_names},
        'classes': [{'value': sg_info.get('class_id') or sg_info.get('class_name') or '', 'label': sg_info.get('class_name') or ''}] if sg_info else [],
        'subjects': [{'value': str(sgid), 'label': sg_info.get('subject') or str(sgid)}] if sgid else [],
        'class_label': sg_info.get('class_name') or '',
        'subject_label': sg_info.get('subject') or '',
        'source': 'studentList+schedule/classmeetings',
    }

def discover_sgo_journal_endpoints(sess):
    endpoints, debug = set(), []
    page_url = sgo_abs('/app/school/journal/')
    try:
        page = sess.get(page_url, headers={'Accept': 'text/html,*/*'}, timeout=15, allow_redirects=True)
        debug.append(f'GET {page_url} -> HTTP {page.status_code}')
        html_text = page.text or ''
        for txt in [html_text]:
            for mm in re.finditer(r'["\']([^"\']*webapi[^"\']*(?:journal|grade|class|subject|lesson|schedule|mark)[^"\']*)["\']', txt, flags=re.I):
                ep = html.unescape(mm.group(1)).replace('\\/', '/')
                if ep.startswith('http') or ep.startswith('/webapi'):
                    endpoints.add(ep)
        for m in re.finditer(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', html_text, flags=re.I):
            js_url = sgo_abs(html.unescape(m.group(1)))
            try:
                js = sess.get(js_url, headers={'Accept': 'application/javascript,*/*'}, timeout=15, allow_redirects=True)
                debug.append(f'GET {js_url} -> HTTP {js.status_code}')
                if js.status_code != 200:
                    continue
                for mm in re.finditer(r'["\']([^"\']*webapi[^"\']*(?:journal|grade|class|subject|lesson|schedule|mark)[^"\']*)["\']', js.text or '', flags=re.I):
                    ep = html.unescape(mm.group(1)).replace('\\/', '/')
                    if ep.startswith('http') or ep.startswith('/webapi'):
                        endpoints.add(ep)
            except Exception as e:
                debug.append(f'JS {js_url} -> {e}')
    except Exception as e:
        debug.append(f'GET {page_url} -> {e}')
    clean = [ep for ep in endpoints if not any(ch in ep for ch in '{}$')]
    return clean, debug

def _walk_json(x):
    yield x
    if isinstance(x, dict):
        for v in x.values():
            yield from _walk_json(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_json(v)

def _value_by_keys(d, keys):
    if not isinstance(d, dict):
        return ''
    low = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        v = low.get(key.lower())
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, dict):
            nested = _value_by_keys(v, ['name', 'title', 'label', 'fullName', 'fio', 'text', 'value'])
            if nested:
                return nested
    return ''

def extract_teacher_meta_from_payload(payload):
    classes, subjects = set(), set()
    for item in _walk_json(payload):
        if not isinstance(item, dict):
            continue
        cls = _value_by_keys(item, ['className', 'class_name', 'class', 'groupName', 'group', 'gradeName', 'grade'])
        subj = _value_by_keys(item, ['subjectName', 'subject_name', 'subject', 'disciplineName', 'discipline'])
        if cls and len(cls) <= 80:
            classes.add(cls)
        if subj and len(subj) <= 120:
            subjects.add(subj)
    return classes, subjects

def extract_schedule_from_payload(payload):
    """Пытается собрать расписание из разных JSON СГО.

    Для учителя СГО часто отдаёт расписание прямо в journal payload:
    journals[].classMeeting + journals[].assignments. Поэтому сначала
    разбираем эту структуру, потом используем универсальный обход.
    """
    days = {}

    # Реальная структура журнала: journals[].classMeeting
    if isinstance(payload, dict) and isinstance(payload.get('journals'), list):
        for j in payload.get('journals') or []:
            if not isinstance(j, dict):
                continue
            subject = str(first_nonempty(j.get('subjectGroupName'), j.get('subjectName'), j.get('subject')) or '—')
            class_name = str(first_nonempty(j.get('className'), j.get('grade'), j.get('groupName')) or '—')
            assignment_by_meeting = {}
            for a in j.get('assignments') or []:
                if not isinstance(a, dict):
                    continue
                cmid = str(a.get('classMeetingId') or a.get('classmeetingId') or '').strip()
                title = str(first_nonempty(a.get('assignmentName'), a.get('name'), a.get('title')) or '').strip()
                if title and title != '---Не указана---':
                    assignment_by_meeting.setdefault(cmid, []).append(title)
            for cm in j.get('classMeeting') or j.get('classMeetings') or []:
                if not isinstance(cm, dict):
                    continue
                mid = str(cm.get('id') or cm.get('classmeetingId') or cm.get('classMeetingId') or '').strip()
                date_raw = str(cm.get('date') or '')
                m = re.search(r'\d{4}-\d{2}-\d{2}', date_raw)
                if not m:
                    continue
                date_str = m.group(0)
                num = first_nonempty(cm.get('scheduleTimeNum'), cm.get('lessonNumber'), cm.get('number')) or ''
                themes = assignment_by_meeting.get(mid, [])
                days.setdefault(date_str, []).append({
                    'number': str(num or len(days.get(date_str, [])) + 1),
                    'subject': subject,
                    'class_name': class_name,
                    'room': '',
                    'theme': '; '.join(themes[:3]) if themes else '',
                })

    # Универсальный обход на случай другого endpoint расписания.
    for item in _walk_json(payload):
        if not isinstance(item, dict):
            continue
        date_val = _value_by_keys(item, ['date', 'day', 'lessonDate', 'startDate'])
        m = re.search(r'\d{4}-\d{2}-\d{2}', date_val or '')
        if not m:
            continue
        subj = _value_by_keys(item, ['subjectName', 'subject', 'disciplineName', 'discipline'])
        if not subj:
            continue
        date_str = m.group(0)
        lesson = {
            'number': _value_by_keys(item, ['number', 'lessonNumber', 'lessonNo', 'order', 'scheduleTimeNum']) or str(len(days.get(date_str, [])) + 1),
            'subject': subj,
            'class_name': _value_by_keys(item, ['className', 'class', 'groupName', 'gradeName']) or '—',
            'room': _value_by_keys(item, ['room', 'roomName', 'cabinet', 'place']),
            'theme': _value_by_keys(item, ['theme', 'topic', 'title', 'lessonTheme']),
        }
        # не добавляем очевидный дубль
        if lesson not in days.setdefault(date_str, []):
            days[date_str].append(lesson)

    for d in days:
        def _num(x):
            try:
                return int(str(x.get('number') or '0').split('.')[0])
            except Exception:
                return 0
        days[d].sort(key=_num)

    return [{'date': d, 'weekday': _teacher_weekday(d), 'lessons': lessons} for d, lessons in sorted(days.items())]

def extract_journal_from_payload(payload, class_name='', subject_filter=''):
    from collections import defaultdict
    students, dates = set(), set()
    grid = defaultdict(lambda: defaultdict(list))
    classes, subjects = extract_teacher_meta_from_payload(payload)
    for item in _walk_json(payload):
        if not isinstance(item, dict):
            continue
        mark = _value_by_keys(item, ['mark', 'markValue', 'grade', 'gradeValue', 'value', 'result', 'markText'])
        if not mark or len(str(mark)) > 10:
            continue
        student = _value_by_keys(item, ['studentName', 'pupilName', 'learnerName', 'personName', 'fullName', 'fio', 'student', 'pupil'])
        if not student:
            continue
        date_val = _value_by_keys(item, ['date', 'day', 'markDate', 'lessonDate', 'createdAt'])
        m = re.search(r'\d{4}-\d{2}-\d{2}', date_val or '')
        if not m:
            continue
        item_class = _value_by_keys(item, ['className', 'class', 'groupName', 'gradeName'])
        item_subject = _value_by_keys(item, ['subjectName', 'subject', 'disciplineName', 'discipline'])
        if class_name and item_class and item_class != class_name:
            continue
        if subject_filter and item_subject and item_subject != subject_filter:
            continue
        date_str = m.group(0)
        students.add(student)
        dates.add(date_str)
        grid[student][date_str].append(str(mark))
    sorted_dates = sorted(dates)
    sorted_students = sorted(students)
    return {
        'students': sorted_students,
        'dates': sorted_dates,
        'grid': {st: {d: grid[st].get(d, []) for d in sorted_dates} for st in sorted_students},
        'classes': sorted(classes),
        'subjects': sorted(subjects),
    }

def _teacher_params(start=None, end=None, class_name='', subject=''):
    params = {}
    if start: params.update({'start': start, 'dateStart': start, 'from': start, 'beginDate': start})
    if end: params.update({'end': end, 'dateEnd': end, 'to': end, 'endDate': end})
    if class_name: params.update({'className': class_name, 'class_name': class_name, 'class': class_name})
    if subject: params.update({'subject': subject, 'subjectName': subject})
    return params

def _teacher_candidates(kind, discovered=None):
    base = {
        'meta': ['/webapi/teacher/journal/init','/webapi/journal/init','/webapi/school/journal/init','/webapi/teacher/classes','/webapi/teacher/subjects','/webapi/journal/classes','/webapi/journal/subjects','/webapi/context','/webapi/profile'],
        'schedule': ['/webapi/teacher/schedule','/webapi/schedule/teacher','/webapi/teacher/diary','/webapi/teacher/journal/init','/webapi/journal/init'],
        'journal': ['/webapi/teacher/journal','/webapi/journal','/webapi/school/journal','/webapi/teacher/journal/marks','/webapi/journal/marks','/webapi/gradebook','/webapi/teacher/gradebook','/webapi/teacher/journal/init','/webapi/journal/init'],
    }[kind]
    found = discovered or []
    words = {'meta': ('class','subject','journal','context','profile'), 'schedule': ('schedule','lesson','journal'), 'journal': ('journal','grade','mark','lesson')}[kind]
    result = []
    for x in base + [x for x in found if any(w in x.lower() for w in words)]:
        if x not in result and not any(ch in x for ch in '{}$'):
            result.append(x)
    return result[:80]

def teacher_direct_fetch(ns, kind, start=None, end=None, class_name="", subject=""):
    cookies, at_token = get_cookies_from_ns(ns)
    debug = [f"cookies: {', '.join(sorted(cookies.keys())) if cookies else 'EMPTY'}", f"at-token present: {bool(at_token)}"]
    if not cookies:
        return [], debug + ["Нет cookies авторизации после входа в СГО"]
    sess = sgo_session_from_cookies(cookies, at_token=at_token)
    try:
        page = sess.get(sgo_abs("/app/school/journal/"), headers={"Accept": "text/html,*/*"}, timeout=15, allow_redirects=True)
        debug.append(f"open journal page -> HTTP {page.status_code}")
    except Exception as e:
        debug.append(f"open journal page -> {e}")
    discovered, disc_debug = discover_sgo_journal_endpoints(sess)
    debug.extend(disc_debug[:8])
    params = _teacher_params(start, end, class_name, subject)
    payloads = []
    for ep in _teacher_candidates(kind, discovered):
        data, err = sgo_request_json(sess, "GET", ep, params)
        if data is not None:
            payloads.append(data); debug.append(f"GET {sgo_abs(ep)} -> JSON OK"); continue
        debug.append(err)
        data, err = sgo_request_json(sess, "POST", ep, params)
        if data is not None:
            payloads.append(data); debug.append(f"POST {sgo_abs(ep)} -> JSON OK")
        else:
            debug.append(err)
    return payloads, debug[:40]



def resolve_teacher_credentials(data):
    """Берёт логин/пароль для учительских вкладок из запроса или сохранённого профиля.

    lkteach.html всегда работает со школой МКОУ Буерак-Поповская СШ, чтобы запросы
    журнала и расписания не уходили в ученическую школу из другого кабинета.
    """
    data = data or {}
    login_val = (data.get('login') or request.args.get('login') or '').strip()
    password_val = (data.get('password') or request.args.get('password') or '').strip()
    school = STAFF_SCHOOL

    if login_val and not password_val:
        try:
            db = get_db()
            user = db.execute("SELECT password FROM users WHERE login = ?", (login_val,)).fetchone()
            if user and user.get('password'):
                password_val = user.get('password')
        except Exception:
            pass

    return login_val, password_val, school

async def safe_teacher_close(ns):
    """Не вызываем logout: СГО часто отдаёт NoResponseFromServer."""
    if ns is None:
        return
    for obj in (getattr(ns, "_wrapped_client", None), getattr(ns, "client", None), getattr(ns, "_client", None)):
        client = getattr(obj, "client", obj) if obj is not None else None
        close = getattr(client, "aclose", None)
        if close:
            try:
                await close()
            except Exception:
                pass
            return

async def _teacher_login(login_val, password_val, school):
    require_netschoolapi()
    ns = NetSchoolAPI(SGO_URL)
    await asyncio.wait_for(sgo_login_with_fallback(ns, login_val, password_val, school, allow_teacher=True), timeout=15)
    return ns



# ================== TEACHER JOURNAL KNOWN SGO JSON HELPERS (v8) ==================
def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]

TEACHER_FALLBACK_SUBJECTS = [
    {'value': '4068818', 'label': 'Биология'},
    {'value': '4068819', 'label': 'География'},
    {'value': '4068829', 'label': 'Ин.яз./Английский язык'},
    {'value': '4068823', 'label': 'Информатика и ИКТ'},
    {'value': '4068824', 'label': 'История'},
    {'value': '4068827', 'label': 'Литература'},
    {'value': '4273689', 'label': 'Математика/Алгебра'},
    {'value': '4068834', 'label': 'Математика/Вероятность и статистика'},
    {'value': '4068822', 'label': 'Математика/Геометрия'},
    {'value': '4068825', 'label': 'Обществознание (Базовый уровень)'},
    {'value': '4068832', 'label': 'Основы безопасности и защиты Родины'},
    {'value': '4068828', 'label': 'Русский язык'},
    {'value': '4068831', 'label': 'Труд (технология)'},
    {'value': '4068820', 'label': 'Физика'},
    {'value': '4275586', 'label': 'Физкультура'},
    {'value': '4068821', 'label': 'Химия'},
    {'value': '4068830', 'label': 'Элективный курс "Подготовка к ОГЭ и устному собеседованию"'},
]
TEACHER_FALLBACK_TERMS = [
    {'value': '143369', 'label': '1 четверть'},
    {'value': '143370', 'label': '2 четверть'},
    {'value': '143371', 'label': '3 четверть'},
    {'value': '143372', 'label': '4 четверть'},
]
TEACHER_AUTO_CLASSES = [{'value': '', 'label': 'Автоматически / все доступные классы'}]

# ============================================================
# SGO 5.51 — РЕАЛЬНЫЕ ЭНДПОИНТЫ (из DevTools браузера)
# ============================================================
# Расписание: GET /webapi/schedule/classmeetings?sgId=<subjectGroupId>&start=<ISO>&end=<ISO>&expand=lesson&expand=room&expand=time&expand=teacherId
# Журнал через /webapi/journals в этой панели НЕ опрашиваем: endpoint возвращает 404 в текущей версии СГО.
# Субъект-группы: GET /webapi/subjectgroups — список всех классов/предметов учителя
#
# Формат субъект-групп:
#   [{id, class:{id,name,grade}, subject:{id,name,shortName}, teachers:[{id,name}], terms:[...], room:{id,name}, fullName}]
#
# Формат расписания (classmeetings):
#   [{id, day:"YYYY-MM-DDT00:00:00", number, scheduleTimeId, subjectGroupId, lessonId, room:{id,name}, teacherId:[...]}]
#
# Формат журнала:
#   {editLimit:{...}, journals:[{subjectGroupId, subjectGroupName, classId, className, grade, students:[...], assignments:[...], classMeeting:[...], marks:[...]}], markSettings:{...}}
# ============================================================

# Пробуемые эндпоинты в порядке приоритета
SGO_511_SUBJECTGROUP_EPS = [
    '/webapi/subjectgroups',
    '/webapi/teacher/subjectgroups',
    '/webapi/subjectgroups/teacher',
    '/webapi/edu/subjectgroups',
]
SGO_511_CLASSMEETINGS_EPS = [
    # В актуальном СГО рабочий путь для расписания преподавателя открывается
    # только через конкретный sgId. Старые /webapi/classmeetings и
    # /webapi/teacher/classmeetings дают 404, поэтому здесь их больше нет.
    '/webapi/schedule/classmeetings',
]
SGO_511_JOURNAL_EPS = [
    # Оставлено пустым намеренно: /webapi/journals в этой инсталляции СГО
    # возвращает 404. Данные карточек берём из studentList + schedule/classmeetings.
]


def parse_subjectgroups_array(data):
    """Парсит ответ /webapi/subjectgroups — массив субъект-групп.

    Возвращает:
      sg_map: {str(id) -> {subject, class_name, grade, room, teacher_ids, term_ids}}
      subjects: [{value, label}]
      classes:  [{value, label}]
      terms:    [{value, label}]
    """
    if not isinstance(data, list):
        return {}, [], [], []

    sg_map = {}
    subjects_d, classes_d, terms_d = {}, {}, {}
    for sg in data:
        if not isinstance(sg, dict):
            continue
        sgid = str(sg.get('id') or '').strip()
        if not sgid:
            continue

        subj = sg.get('subject') or {}
        cls  = sg.get('class')  or {}
        grp  = sg.get('group')  or {}
        room = sg.get('room')   or {}

        subject_name = str(subj.get('name') or sg.get('name') or sg.get('fullName') or '').strip()
        class_name   = str(cls.get('name') or '').strip()
        grade        = str(cls.get('grade') or '').strip()
        room_name    = str(room.get('name') or '').strip()
        teachers_raw = sg.get('teachers') or sg.get('teacher') or []
        if isinstance(teachers_raw, dict):
            teachers_raw = [teachers_raw]
        elif isinstance(teachers_raw, str):
            teachers_raw = [{'name': teachers_raw}]
        teacher_ids = []
        teacher_names = []
        for direct_key in ('teacherId', 'teacherID', 'teacher_id', 'employeeId', 'employeeID'):
            if sg.get(direct_key):
                tid = str(sg.get(direct_key) or '').strip()
                if tid and tid not in teacher_ids:
                    teacher_ids.append(tid)
        for t in teachers_raw if isinstance(teachers_raw, list) else []:
            if not isinstance(t, dict):
                continue
            tid = str(t.get('id') or t.get('teacherId') or t.get('teacher_id') or t.get('employeeId') or '').strip()
            if tid and tid not in teacher_ids:
                teacher_ids.append(tid)
            tn = str(t.get('name') or t.get('fullName') or t.get('fio') or t.get('teacherName') or '').strip()
            if tn and tn not in teacher_names:
                teacher_names.append(tn)
        term_ids     = [str(t) for t in (sg.get('terms') or [])]

        sg_map[sgid] = {
            'subject':    subject_name,
            'class_name': class_name,
            'grade':      grade,
            'room':       room_name,
            'teacher_ids': teacher_ids,
            'teachers':    teacher_names,
            'teacher_names': teacher_names,
            'term_ids':    term_ids,
            'sg_id':      sgid,
            'class_id':   str(cls.get('id') or ''),
            'subject_id': str(subj.get('id') or ''),
            'full_name':  str(sg.get('fullName') or subject_name or '').strip(),
        }

        # ВАЖНО: для /webapi/journals нужен subjectGroupId (sg), а не subject.id.
        sid_key = sgid
        if subject_name and sid_key not in subjects_d:
            label = str(sg.get('fullName') or subject_name).strip()
            subjects_d[sid_key] = label

        cid_key = str(cls.get('id') or class_name)
        if class_name and cid_key not in classes_d:
            classes_d[cid_key] = class_name

        for termid in term_ids:
            if termid not in terms_d:
                terms_d[termid] = termid  # label придёт из fallback

    subjects = [{'value': k, 'label': v} for k, v in sorted(subjects_d.items(), key=lambda kv: kv[1])]
    classes  = [{'value': k, 'label': v} for k, v in sorted(classes_d.items(),  key=lambda kv: kv[1])]
    terms    = [{'value': k, 'label': k} for k in sorted(terms_d.keys())]
    return sg_map, subjects, classes, terms



def _sg_students_from_structured(structured):
    """Нормализует учеников из extract_journal_structured в короткий список."""
    out = []
    if not isinstance(structured, dict):
        return out
    for st in structured.get('students') or []:
        if not isinstance(st, dict):
            continue
        name = first_nonempty(st.get('name'), st.get('fullName'), st.get('fio'), st.get('student'))
        sid = first_nonempty(st.get('id'), st.get('studentId'), st.get('personId'), name)
        if name and not any(x.get('name') == name for x in out):
            out.append({'id': str(sid or ''), 'name': str(name)})
    return out


def build_subjectgroup_catalog(sg_map, journal_details=None):
    """Собирает каталог: классы -> предметы -> педагоги/ученики."""
    journal_details = journal_details or {}
    classes, staff, subjects, rows = {}, {}, {}, []
    for sgid, info in (sg_map or {}).items():
        sgid = str(sgid)
        jd = journal_details.get(sgid) or {}
        class_name = first_nonempty(info.get('class_name'), info.get('class'), info.get('grade'), jd.get('class_name'), 'Без класса')
        subject = first_nonempty(info.get('subject'), info.get('subject_name'), jd.get('subject'), info.get('full_name'), 'Без предмета')
        teachers = info.get('teachers') or info.get('teacher_names') or []
        if not teachers and info.get('teacher_ids'):
            teachers = [f"ID {x}" for x in info.get('teacher_ids') or []]
        students = jd.get('students') or []
        row = {
            'sg_id': sgid, 'id': sgid, 'subject_group_id': sgid,
            'class_id': info.get('class_id') or '', 'class_name': str(class_name), 'grade': info.get('grade') or '',
            'subject_id': info.get('subject_id') or '', 'subject': str(subject),
            'teachers': teachers, 'teacher_ids': info.get('teacher_ids') or [],
            'room': info.get('room') or '', 'terms': info.get('term_ids') or [],
            'students': students, 'students_count': len(students),
            'columns_count': int(jd.get('columns_count') or 0), 'marks_count': int(jd.get('marks_count') or 0),
            'journal_found': bool(jd.get('found')), 'journal_error': jd.get('error') or '',
        }
        rows.append(row)
        cls = classes.setdefault(str(class_name), {'name': str(class_name), 'subjects': [], 'students_count': 0, 'students': []})
        cls['subjects'].append(row)
        known_students = {x.get('name') for x in cls['students']}
        for st in students:
            if st.get('name') and st.get('name') not in known_students:
                cls['students'].append(st); known_students.add(st.get('name'))
        cls['students_count'] = len(cls['students'])
        subj = subjects.setdefault(str(subject), {'name': str(subject), 'classes': [], 'teachers': []})
        if str(class_name) not in subj['classes']:
            subj['classes'].append(str(class_name))
        for t in teachers:
            if t and t not in subj['teachers']:
                subj['teachers'].append(t)
        for t in teachers:
            item = staff.setdefault(str(t), {'name': str(t), 'subjects': [], 'classes': [], 'subjectgroups': []})
            if str(subject) not in item['subjects']:
                item['subjects'].append(str(subject))
            if str(class_name) not in item['classes']:
                item['classes'].append(str(class_name))
            item['subjectgroups'].append(sgid)
    return {
        'rows': sorted(rows, key=lambda r: (str(r.get('class_name')), str(r.get('subject')), str(r.get('sg_id')))),
        'classes': sorted(classes.values(), key=lambda x: str(x.get('name'))),
        'subjects': sorted(subjects.values(), key=lambda x: str(x.get('name'))),
        'staff': sorted(staff.values(), key=lambda x: str(x.get('name'))),
    }


def fetch_subjectgroup_journal_details(sess, sg_map, start, end, limit=80):
    """По subjectGroupId пробует получить журнал и учеников."""
    details, debug = {}, []
    if int(limit or 0) <= 0:
        return details, debug
    sgids = list((sg_map or {}).keys())[:max(1, int(limit or 80))]
    for sgid in sgids:
        info = (sg_map or {}).get(str(sgid), {})
        data, jdebug = fetch_sgo_journal_511(sess, sgid, start, end)
        debug.extend(jdebug[:2])
        item = {'found': False, 'students': [], 'columns_count': 0, 'marks_count': 0, 'class_name': info.get('class_name') or '', 'subject': info.get('subject') or ''}
        if data is None:
            item['error'] = 'journal endpoint did not return JSON'
            details[str(sgid)] = item
            continue
        structured = extract_journal_structured(data, '', '', start, end)
        if structured:
            students = _sg_students_from_structured(structured)
            grid = structured.get('grid') or {}
            marks_count = 0
            if isinstance(grid, dict):
                for cells in grid.values():
                    if isinstance(cells, dict):
                        for vals in cells.values():
                            marks_count += len(vals) if isinstance(vals, list) else (1 if vals else 0)
            item.update({
                'found': bool(students or structured.get('columns')),
                'students': students, 'columns_count': len(structured.get('columns') or []),
                'marks_count': marks_count,
                'class_name': structured.get('class_label') or item['class_name'],
                'subject': structured.get('subject_label') or item['subject'],
            })
        details[str(sgid)] = item
    return details, debug


def parse_classmeetings_schedule(data, sg_map, start=None, end=None):
    """Парсит ответ /webapi/classmeetings — массив уроков.

    data:   [{id, day:"YYYY-MM-DDT00:00:00", number, subjectGroupId, room:{...}, ...}]
    sg_map: результат parse_subjectgroups_array
    Возвращает список дней: [{date, weekday, lessons:[{number, subject, class_name, room}]}]
    """
    if not isinstance(data, list):
        return []

    ru_wd = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
    days = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        day_raw = str(entry.get('day') or '').strip()
        m = re.search(r'(\d{4}-\d{2}-\d{2})', day_raw)
        if not m:
            continue
        date = m.group(1)
        if start and date < start:
            continue
        if end and date > end:
            continue

        sgid    = str(entry.get('subjectGroupId') or '').strip()
        number  = entry.get('number') or entry.get('lessonNumber') or ''
        room_obj = entry.get('room') or {}
        room = first_nonempty(room_obj.get('name'), room_obj.get('title'), room_obj.get('number')) if isinstance(room_obj, dict) else str(room_obj or '').strip()
        sg_info = sg_map.get(sgid) or {}
        if not room:
            room = first_nonempty(sg_info.get('room'), sg_info.get('roomName'), sg_info.get('cabinet'))
        time_obj = entry.get('time') or {}
        if isinstance(time_obj, dict):
            start_time = first_nonempty(time_obj.get('start'), time_obj.get('begin'), time_obj.get('startTime'), entry.get('start'), entry.get('startTime'))
            end_time = first_nonempty(time_obj.get('end'), time_obj.get('finish'), time_obj.get('endTime'), entry.get('end'), entry.get('endTime'))
            start_text = str(start_time or '').strip()[:5]
            end_text = str(end_time or '').strip()[:5]
            lesson_time = (start_text + (' - ' + end_text if end_text else '')).strip()
        else:
            lesson_time = str(time_obj or '').strip()
        subject  = sg_info.get('subject') or sgid or '—'
        cls_name = sg_info.get('class_name') or sg_info.get('grade') or ''
        if cls_name and sg_info.get('grade') and cls_name != sg_info.get('grade'):
            cls_name = f"{sg_info['grade']} ({cls_name})" if sg_info.get('group') else cls_name

        if date not in days:
            try:
                wd_idx = datetime.strptime(date, '%Y-%m-%d').weekday()
                wd = ru_wd[wd_idx]
            except Exception:
                wd = ''
            days[date] = {'date': date, 'weekday': wd, 'lessons': []}

        days[date]['lessons'].append({
            'number':     number,
            'subject':    subject,
            'class_name': cls_name,
            'class':      cls_name,
            'time':       lesson_time,
            'lesson_time': lesson_time,
            'room':       room,
            'sg_id':      sgid,
            'lesson_id':  str(entry.get('lessonId') or ''),
            'theme':      '',
        })

    result = []
    for day in sorted(days.values(), key=lambda d: d['date']):
        day['lessons'].sort(key=lambda l: int(str(l['number']).split('/')[0]) if str(l.get('number','')).split('/')[0].isdigit() else 0)
        result.append(day)
    return result




def _teacher_deep_find_values(payload, keys, limit=20):
    """Ищет значения по набору ключей во вложенном JSON/HTML-friendly объекте."""
    wanted = {str(k).lower() for k in keys}
    found = []
    stack = [payload]
    seen = 0
    while stack and seen < 2000 and len(found) < limit:
        seen += 1
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in wanted:
                    text = first_nonempty(v)
                    if text and text not in found:
                        found.append(text)
                if isinstance(v, (dict, list, tuple)):
                    stack.append(v)
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
    return found


def fetch_sgo_user_profile(sess, login_val='', local_user=None):
    """Пытается получить ФИО и ID пользователя из нескольких endpoint-ов СГО и HTML кабинета."""
    local_user = local_user or {}
    result = {
        'login': login_val or local_user.get('login') or '',
        'full_name': local_user.get('full_name') or '',
        'email': local_user.get('email') or '',
        'role': local_user.get('role') or 'Учитель',
        'school': local_user.get('school') or '',
        'user_id': '',
        'source': 'local',
        'raw_sources': []
    }
    endpoints = ['/webapi/mysettings','/webapi/context','/webapi/security/context','/webapi/auth/getdata','/webapi/account/context','/webapi/user/context','/webapi/profile']
    for ep in endpoints:
        data, err = sgo_request_json(sess, 'GET', ep, {}, timeout=6)
        if data is None:
            result['raw_sources'].append(f'GET {ep} -> {err}')
            continue
        result['raw_sources'].append(f'GET {ep} -> JSON OK')
        if ep == '/webapi/mysettings' and isinstance(data, dict):
            result['raw'] = data
            first_name = str(data.get('firstName') or '').strip()
            last_name = str(data.get('lastName') or '').strip()
            middle_name = str(data.get('middleName') or '').strip()
            full_from_parts = ' '.join([x for x in [last_name, first_name, middle_name] if x]).strip()
            if full_from_parts:
                result['full_name'] = full_from_parts
            if data.get('loginName'):
                result['login'] = str(data.get('loginName') or result.get('login') or '').strip()
            if data.get('birthDate'):
                result['birth_date'] = str(data.get('birthDate') or '').strip()
            if data.get('email'):
                result['email'] = str(data.get('email') or '').strip()
            if data.get('roles'):
                result['role'] = ', '.join(map(str, data.get('roles') or [])) if isinstance(data.get('roles'), list) else str(data.get('roles'))
            settings = data.get('userSettings') or {}
            if isinstance(settings, dict) and settings.get('userId'):
                result['user_id'] = str(settings.get('userId'))
            elif data.get('userId'):
                result['user_id'] = str(data.get('userId'))
            result['source'] = ep
        names = _teacher_deep_find_values(data, ['fullName','fullname','fio','ФИО','name','userName','username','displayName','personName'], limit=8)
        ids = _teacher_deep_find_values(data, ['userId','userid','id','personId','personid','employeeId','employeeid','teacherId','teacherid'], limit=8)
        emails = _teacher_deep_find_values(data, ['email','mail','eMail'], limit=3)
        roles = _teacher_deep_find_values(data, ['role','roleName','roles','accountType'], limit=3)
        birth_dates = _teacher_deep_find_values(data, ['birthDate','birthday','dateOfBirth'], limit=3)
        if names and not result.get('full_name'):
            for n in names:
                if len(str(n)) > 2 and not str(n).startswith('/'):
                    result['full_name'] = str(n)
                    break
        if ids and not result.get('user_id'):
            result['user_id'] = str(ids[0])
        if emails and not result.get('email'):
            result['email'] = str(emails[0])
        if roles and not result.get('role'):
            result['role'] = ', '.join(map(str, roles[0])) if isinstance(roles[0], list) else str(roles[0])
        if birth_dates and not result.get('birth_date'):
            result['birth_date'] = str(birth_dates[0])
        if (result.get('full_name') or result.get('user_id')) and result.get('source') == 'local':
            result['source'] = ep
    try:
        resp = sess.get(sgo_abs('/app/school/journal/'), headers={'Accept': 'text/html,*/*'}, timeout=8, allow_redirects=True)
        if resp.status_code == 200:
            txt = resp.text or ''
            result['raw_sources'].append('GET /app/school/journal/ -> HTML OK')
            if not result.get('full_name'):
                for pattern in [r'"fullName"\s*:\s*"([^"]+)"', r'"userName"\s*:\s*"([^"]+)"', r'"displayName"\s*:\s*"([^"]+)"', r'ФИО\s*[:=]\s*([^<"\n]+)']:
                    m = re.search(pattern, txt, flags=re.I)
                    if m:
                        result['full_name'] = html.unescape(m.group(1)).strip()
                        result['source'] = 'html'
                        break
            if not result.get('user_id'):
                for pattern in [r'"userId"\s*:\s*"?(\d+)"?', r'"id"\s*:\s*"?(\d+)"?']:
                    m = re.search(pattern, txt, flags=re.I)
                    if m:
                        result['user_id'] = m.group(1)
                        result['source'] = 'html'
                        break
    except Exception as e:
        result['raw_sources'].append(f'HTML profile parse -> {type(e).__name__}: {e}')
    if not result.get('full_name'):
        result['full_name'] = local_user.get('full_name') or ''
    if not result.get('email'):
        result['email'] = local_user.get('email') or ''
    return result


def teacher_profile_id_from_subjectgroups(sg_map):
    counts = {}
    for info in (sg_map or {}).values():
        for tid in info.get('teacher_ids') or []:
            tid = str(tid or '').strip()
            if tid:
                counts[tid] = counts.get(tid, 0) + 1
    if not counts:
        return ''
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def build_teacher_workload(subjectgroups=None, schedule=None, journal=None):
    """Сводит классы, предметы и нагрузку учителя из subjectgroups + расписания + журнала."""
    subjectgroups = subjectgroups or []
    schedule = schedule or []
    journal = journal or {}
    subjects = {}
    classes = {}
    pairs = {}
    for sg in subjectgroups:
        subject = first_nonempty(sg.get('subject'), sg.get('name'), default='—')
        cls = first_nonempty(sg.get('className'), sg.get('class'), default='—')
        sgid = first_nonempty(sg.get('id'), sg.get('sgId'), default='')
        if subject and subject != '—': subjects[subject] = subjects.get(subject, 0) + 1
        if cls and cls != '—': classes[cls] = classes.get(cls, 0) + 1
        key = (subject, cls)
        pairs[key] = pairs.get(key, {'subject': subject, 'class_name': cls, 'subjectgroups': 0, 'lessons': 0, 'students': 0, 'sg_ids': []})
        pairs[key]['subjectgroups'] += 1
        if sgid: pairs[key]['sg_ids'].append(str(sgid))
    total_lessons = 0
    for day in schedule:
        for lesson in (day.get('lessons') or []):
            total_lessons += 1
            subject = first_nonempty(lesson.get('subject'), default='—')
            cls = first_nonempty(lesson.get('class_name'), lesson.get('className'), default='—')
            if subject and subject != '—': subjects[subject] = subjects.get(subject, 0) + 1
            if cls and cls != '—': classes[cls] = classes.get(cls, 0) + 1
            key = (subject, cls)
            pairs[key] = pairs.get(key, {'subject': subject, 'class_name': cls, 'subjectgroups': 0, 'lessons': 0, 'students': 0, 'sg_ids': []})
            pairs[key]['lessons'] += 1
    if isinstance(journal, dict) and isinstance(journal.get('rows'), list):
        by_subject_students = {}
        for row in journal.get('rows') or []:
            subject = first_nonempty(row.get('subject'), default='—')
            student = first_nonempty(row.get('student'), default='')
            if subject and student:
                by_subject_students.setdefault(subject, set()).add(student)
        for pair in pairs.values():
            if pair['subject'] in by_subject_students:
                pair['students'] = len(by_subject_students[pair['subject']])
    pair_list = sorted(pairs.values(), key=lambda x: (str(x.get('class_name')), str(x.get('subject'))))
    return {
        'classes': sorted(classes.keys()),
        'subjects': sorted(subjects.keys()),
        'classes_count': len(classes),
        'subjects_count': len(pair_list),
        'distinct_subjects_count': len(subjects),
        'subjectgroups_count': len(subjectgroups),
        'scheduled_lessons_count': total_lessons,
        'pairs': pair_list,
        'by_subject': [{'subject': k, 'items': v} for k, v in sorted(subjects.items())],
        'by_class': [{'class_name': k, 'items': v} for k, v in sorted(classes.items())],
    }


def _normalize_teacher_match_value(value):
    value = str(value or '').strip().lower()
    value = re.sub(r'\s+', ' ', value)
    return value


def subjectgroup_options_from_map(sg_map):
    """Строит списки предметов/классов/периодов уже после фильтрации subjectgroups."""
    subjects_d, classes_d, terms_d = {}, {}, {}
    for sgid, info in (sg_map or {}).items():
        sgid = str(sgid)
        subject_name = str(info.get('subject') or info.get('subject_name') or info.get('full_name') or '').strip()
        class_name = str(info.get('class_name') or info.get('class') or info.get('grade') or '').strip()
        if subject_name:
            subjects_d[sgid] = str(info.get('full_name') or subject_name).strip()
        if class_name:
            classes_d[str(info.get('class_id') or class_name)] = class_name
        for termid in info.get('term_ids') or []:
            if termid:
                terms_d[str(termid)] = str(termid)
    subjects = [{'value': k, 'label': v} for k, v in sorted(subjects_d.items(), key=lambda kv: kv[1])]
    classes = [{'value': k, 'label': v} for k, v in sorted(classes_d.items(), key=lambda kv: kv[1])]
    terms = [{'value': k, 'label': v} for k, v in sorted(terms_d.items(), key=lambda kv: kv[1])]
    return subjects, classes, terms


def subjectgroup_rows_from_map(sg_map):
    rows = []
    for sgid, info in (sg_map or {}).items():
        rows.append({
            'id': sgid,
            'sgId': sgid,
            'subjectGroupId': sgid,
            'subjectId': info.get('subject_id') or '',
            'classId': info.get('class_id') or '',
            'subject': info.get('subject') or info.get('subject_name') or info.get('name') or '',
            'name': info.get('subject') or info.get('name') or '',
            'className': info.get('class') or info.get('class_name') or '',
            'class': info.get('class') or info.get('class_name') or '',
            'teachers': info.get('teachers') or info.get('teacher_names') or [],
            'teacher_ids': info.get('teacher_ids') or [],
            'grade': info.get('grade') or '',
            'room': info.get('room') or '',
            'terms': info.get('term_ids') or [],
            'fullName': info.get('full_name') or '',
        })
    return rows


def filter_subjectgroups_for_teacher(sg_map, profile=None, login_val='', local_user=None):
    """Оставляет subjectgroups только текущего учителя, когда СГО отдаёт группы всей школы."""
    profile = profile or {}
    local_user = local_user or {}
    if not sg_map:
        return {}, 'subjectgroups empty'

    teacher_ids = {
        str(x).strip()
        for x in (
            profile.get('user_id'),
            profile.get('teacher_id'),
            profile.get('employee_id'),
            local_user.get('user_id') if isinstance(local_user, dict) else '',
        )
        if str(x or '').strip()
    }
    teacher_names = {
        _normalize_teacher_match_value(x)
        for x in (
            profile.get('full_name'),
            profile.get('fio'),
            profile.get('name'),
            local_user.get('full_name') if isinstance(local_user, dict) else '',
        )
        if _normalize_teacher_match_value(x)
    }

    if teacher_ids:
        filtered = {
            sgid: info for sgid, info in (sg_map or {}).items()
            if teacher_ids.intersection({str(x).strip() for x in (info.get('teacher_ids') or [])})
        }
        if filtered:
            return filtered, f'subjectgroups filtered by teacher id: {len(filtered)}/{len(sg_map)}'

    if teacher_names:
        filtered = {}
        for sgid, info in (sg_map or {}).items():
            names = [_normalize_teacher_match_value(x) for x in (info.get('teachers') or info.get('teacher_names') or [])]
            if any(any(wanted == actual or wanted in actual or actual in wanted for actual in names) for wanted in teacher_names):
                filtered[sgid] = info
        if filtered:
            return filtered, f'subjectgroups filtered by teacher name: {len(filtered)}/{len(sg_map)}'

    return {}, 'subjectgroups teacher filter did not match; hidden to avoid school-wide data'


def _teacher_ids_from_teacher_context(value):
    """Рекурсивно достаёт ID преподавателя только из teacher/employee-контекста.
    Не принимает обычные id классов, уроков или предметов за ID учителя.
    """
    ids = []
    def add(v):
        if v is None:
            return
        if isinstance(v, (list, tuple, set)):
            for x in v: add(x)
            return
        text = str(v).strip()
        if text and text not in ids:
            ids.append(text)
    def walk(obj, ctx=''):
        ctx_l = str(ctx or '').lower()
        teacher_ctx = any(w in ctx_l for w in ('teacher', 'employee', 'препод', 'учител'))
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in ('teacherid','teacher_id','teacherids','teacher_ids','employeeid','employee_id'):
                    add(v)
                elif kl == 'id' and teacher_ctx:
                    add(v)
                elif any(w in kl for w in ('teacher', 'employee', 'препод', 'учител')):
                    if isinstance(v, dict):
                        for dk in ('id','teacherId','teacher_id','employeeId','employee_id'):
                            if v.get(dk): add(v.get(dk))
                    elif isinstance(v, list):
                        for it in v:
                            if isinstance(it, dict):
                                for dk in ('id','teacherId','teacher_id','employeeId','employee_id'):
                                    if it.get(dk): add(it.get(dk))
                            else:
                                add(it)
                    else:
                        add(v)
                    walk(v, kl)
                else:
                    walk(v, kl if teacher_ctx else '')
        elif isinstance(obj, list):
            for it in obj:
                walk(it, ctx)
    walk(value)
    return ids

def _bundle_item_has_teacher_id(item, teacher_id):
    teacher_id = str(teacher_id or '').strip()
    if not teacher_id or not isinstance(item, dict):
        return False
    ids = []
    norm = item.get('normalized') or {}
    if isinstance(norm, dict):
        ids.extend([str(x).strip() for x in (norm.get('teacher_ids') or []) if str(x or '').strip()])
    ids.extend(_teacher_ids_from_teacher_context(item.get('detail')))
    for m in item.get('schedule') or []:
        ids.extend(_teacher_ids_from_teacher_context(m))
    return teacher_id in {str(x).strip() for x in ids if str(x or '').strip()}

def _filter_bundle_by_teacher_id(bundle, teacher_id):
    teacher_id = str(teacher_id or '').strip()
    if not teacher_id:
        return {}
    return {str(sgid): item for sgid, item in (bundle or {}).items() if _bundle_item_has_teacher_id(item, teacher_id)}


def fetch_sgo_subjectgroups(sess):
    """Запрашивает /webapi/subjectgroups и аналоги. Возвращает (data, debug_list)."""
    debug = []
    for ep in SGO_511_SUBJECTGROUP_EPS:
        data, err = sgo_request_json(sess, 'GET', ep, {})
        if data is not None:
            debug.append(f'GET {ep} -> JSON OK ({len(data) if isinstance(data, list) else "dict"})')
            return data, debug
        debug.append(err)
        data, err = sgo_request_json(sess, 'POST', ep, {})
        if data is not None:
            debug.append(f'POST {ep} -> JSON OK')
            return data, debug
        debug.append(err)
    return None, debug


def truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "да"}


def fetch_sgo_classmeetings(sess, start, end, teacher_id='', sgids=None):
    """Расписание учителя только через teacher-owned sgId.

    Старые endpoint-ы /webapi/classmeetings и /webapi/teacher/classmeetings
    в текущем СГО возвращают 404. Поэтому эта функция больше не делает
    общих запросов по teacherId и не создаёт шум в логах. Если sgids не
    переданы — запрос пропускается.
    """
    debug = []
    sgids = [str(x).strip() for x in (sgids or []) if str(x or '').strip()]
    if not sgids:
        return None, ['schedule/classmeetings skipped: no teacher-owned sgId']
    all_items = []
    for sgid in sgids[:80]:
        data, err = fetch_sgo_schedule_by_sgid(sess, sgid, start, end)
        if data is None:
            debug.append(f'GET /webapi/schedule/classmeetings?sgId={sgid} -> {err}')
            continue
        arr = data if isinstance(data, list) else ((data.get('items') or data.get('data') or data.get('classMeetings') or []) if isinstance(data, dict) else [])
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict) and not (item.get('subjectGroupId') or item.get('sgId')):
                    item['subjectGroupId'] = sgid
                all_items.append(item)
            debug.append(f'GET /webapi/schedule/classmeetings?sgId={sgid} -> JSON OK ({len(arr)} lessons)')
    return (all_items if all_items else None), debug

def fetch_sgo_journal_511(sess, sgid, start, end):
    """Журнал через /webapi/journals отключён, чтобы не получать 404.

    В lkteacher удалён раздел «Журнал», а для карточек предметов используются
    /webapi/grade/studentList и /webapi/schedule/classmeetings по sgId.
    """
    return None, [f'journals skipped for sgId={sgid}: /webapi/journals returns 404 in this SGO build']

def _merge_options(primary, fallback):
    seen = set(); out = []
    for arr in (primary or [], fallback or []):
        for it in arr:
            val = str((it or {}).get('value') or '').strip()
            lab = str((it or {}).get('label') or '').strip()
            if val not in seen:
                seen.add(val); out.append({'value': val, 'label': lab or val})
    return out

def extract_filter_options(payload):
    out = {"subjects": [], "terms": [], "teachers": [], "defaults": {}}
    for item in _walk_json(payload):
        if not isinstance(item, dict):
            continue
        fid = str(item.get('filterId') or item.get('id') or '').upper()
        items = item.get('items')
        if not isinstance(items, list):
            continue
        target = None
        if fid in ('SGID', 'SUBJECTGROUPID', 'SUBJECT'):
            target = 'subjects'
        elif fid in ('TERMID', 'TERM'):
            target = 'terms'
        elif fid in ('TEACHERNAME', 'TEACHERID', 'TEACHER'):
            target = 'teachers'
        if not target:
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            title = str(it.get('title') or it.get('name') or it.get('label') or it.get('text') or '').strip()
            value = str(it.get('value') or it.get('id') or '').strip()
            if title and value and not any(x['value'] == value for x in out[target]):
                out[target].append({'value': value, 'label': title})
        default_value = str(item.get('defaultValue') or '').strip()
        if default_value:
            out['defaults'][fid] = default_value
    return out

def extract_teacher_options_from_payload(payload):
    classes, subjects = {}, {}
    filters = extract_filter_options(payload)
    for it in filters.get('subjects', []):
        subjects[str(it['value'])] = it['label']
    journals = payload.get('journals') if isinstance(payload, dict) else None
    for j in _as_list(journals):
        if not isinstance(j, dict):
            continue
        cid = first_nonempty(j.get('classId'), j.get('classID'), j.get('groupId'))
        cname = first_nonempty(j.get('className'), j.get('grade'), j.get('groupName'))
        sid = first_nonempty(j.get('subjectGroupId'), j.get('subjectId'), j.get('SGID'))
        sname = first_nonempty(j.get('subjectGroupName'), j.get('subjectName'), j.get('subject'))
        if cid and cname:
            classes[str(cid)] = str(cname)
        elif cname:
            classes[str(cname)] = str(cname)
        if sid and sname:
            subjects[str(sid)] = str(sname)
        elif sname:
            subjects[str(sname)] = str(sname)
    for item in _walk_json(payload):
        if not isinstance(item, dict):
            continue
        cid = first_nonempty(item.get('classId'), item.get('classID'), item.get('class_id'))
        cname = first_nonempty(item.get('className'), item.get('class_name'), item.get('gradeName'), item.get('grade'))
        sid = first_nonempty(item.get('subjectGroupId'), item.get('subjectId'), item.get('subject_id'), item.get('SGID'))
        sname = first_nonempty(item.get('subjectGroupName'), item.get('subjectName'), item.get('subject'), item.get('disciplineName'))
        if cid and cname and len(str(cname)) <= 80:
            classes[str(cid)] = str(cname)
        if sid and sname and len(str(sname)) <= 160:
            subjects[str(sid)] = str(sname)
    return {
        'classes': [{'value': k, 'label': v} for k, v in sorted(classes.items(), key=lambda kv: (kv[1], kv[0]))],
        'subjects': [{'value': k, 'label': v} for k, v in sorted(subjects.items(), key=lambda kv: (kv[1], kv[0]))],
        'terms': filters.get('terms', []),
        'teachers': filters.get('teachers', []),
        'defaults': filters.get('defaults', {}),
    }

def extract_journal_structured(payload, class_filter='', subject_filter='', start=None, end=None):
    """Разбирает реальный ответ СГО с ключом journals.

    Возвращает журнал в виде:
    - students: список ФИО
    - columns: реальные колонки журнала по assignments/classMeeting
    - grid: оценки/посещаемость по student -> assignmentId/classMeetingId
    - dates: уникальные даты для совместимости со старым фронтом
    """
    from collections import defaultdict
    class_filter = str(class_filter or '').strip()
    subject_filter = str(subject_filter or '').strip()
    students_order, columns_order, dates_order = [], [], []
    grid = defaultdict(lambda: defaultdict(list))
    classes, subjects = {}, {}
    subject_label = ''
    class_label = ''

    journals = payload.get('journals') if isinstance(payload, dict) else None
    if not isinstance(journals, list):
        return None

    type_names = globals().get('TYPE_NAMES', {}) or {}

    for j in journals:
        if not isinstance(j, dict):
            continue

        cid = str(first_nonempty(j.get('classId'), j.get('classID'), j.get('groupId')) or '').strip()
        cname = str(first_nonempty(j.get('className'), j.get('grade'), j.get('groupName')) or '').strip()
        sid = str(first_nonempty(j.get('subjectGroupId'), j.get('subjectId'), j.get('SGID')) or '').strip()
        sname = str(first_nonempty(j.get('subjectGroupName'), j.get('subjectName'), j.get('subject')) or '').strip()

        if cid and cname:
            classes[cid] = cname
        if sid and sname:
            subjects[sid] = sname

        # Фильтруем только если пользователь реально выбрал конкретный класс/предмет.
        if class_filter and class_filter not in (cid, cname):
            continue
        if subject_filter and subject_filter not in (sid, sname):
            continue

        class_label = cname or class_label
        subject_label = sname or subject_label

        student_by_id = {}
        for st in j.get('students') or []:
            if not isinstance(st, dict):
                continue
            stid = str(st.get('id') or st.get('studentId') or '').strip()
            name = str(st.get('fullName') or st.get('name') or st.get('fio') or '').strip()
            if stid and name:
                student_by_id[stid] = name
                if name not in students_order:
                    students_order.append(name)

        meeting_date = {}
        meeting_number = {}
        for cm in j.get('classMeeting') or j.get('classMeetings') or []:
            if not isinstance(cm, dict):
                continue
            mid = str(cm.get('id') or cm.get('classmeetingId') or cm.get('classMeetingId') or '').strip()
            date_raw = str(cm.get('date') or '')
            m = re.search(r'\d{4}-\d{2}-\d{2}', date_raw)
            if mid and m:
                d = m.group(0)
                if start and d < start:
                    continue
                if end and d > end:
                    continue
                meeting_date[mid] = d
                meeting_number[mid] = first_nonempty(cm.get('scheduleTimeNum'), cm.get('lessonNumber'), cm.get('number')) or ''
                if d not in dates_order:
                    dates_order.append(d)

        assignment_date = {}
        assignment_seen = set()
        for a in j.get('assignments') or []:
            if not isinstance(a, dict):
                continue
            aid = str(a.get('id') or a.get('assignmentId') or '').strip()
            cmid = str(a.get('classMeetingId') or a.get('classmeetingId') or '').strip()
            if not aid:
                continue
            d = meeting_date.get(cmid)
            if not d:
                continue

            assignment_date[aid] = d
            if aid in assignment_seen:
                continue
            assignment_seen.add(aid)

            type_id = a.get('typeId')
            type_label = type_names.get(type_id, '') if isinstance(type_id, int) else type_names.get(int(type_id), '') if str(type_id or '').isdigit() else ''
            title = str(first_nonempty(a.get('assignmentName'), a.get('name'), a.get('title')) or '').strip()
            if not title or title == '---Не указана---':
                title = type_label or 'Работа'

            columns_order.append({
                'id': aid,
                'date': d,
                'title': title,
                'type': type_label,
                'lesson': meeting_number.get(cmid, ''),
                'classMeetingId': cmid,
                'weight': a.get('weight', ''),
            })

        # Если заданий нет, но есть уроки и посещаемость — создаём колонки уроков,
        # чтобы расписание/посещаемость тоже было видно.
        if not columns_order:
            for mid, d in meeting_date.items():
                columns_order.append({
                    'id': 'meeting_' + mid,
                    'date': d,
                    'title': 'Урок',
                    'type': 'Посещаемость',
                    'lesson': meeting_number.get(mid, ''),
                    'classMeetingId': mid,
                })

        for mark_obj in j.get('marks') or []:
            if not isinstance(mark_obj, dict):
                continue
            mark = first_nonempty(mark_obj.get('mark'), mark_obj.get('value'), mark_obj.get('markValue'))
            if not mark:
                continue
            stid = str(mark_obj.get('studentId') or mark_obj.get('pupilId') or '').strip()
            aid = str(mark_obj.get('assignmentId') or '').strip()
            student = student_by_id.get(stid, stid)
            if not student or not aid:
                continue
            if aid not in assignment_date:
                continue
            if student not in students_order:
                students_order.append(student)
            grid[student][aid].append(str(mark))

        for att in j.get('attendance') or []:
            if not isinstance(att, dict):
                continue
            reason = str(att.get('reason') or '').strip()
            if not reason:
                continue
            stid = str(att.get('studentId') or '').strip()
            cmid = str(att.get('classmeetingId') or att.get('classMeetingId') or '').strip()
            student = student_by_id.get(stid, stid)
            if not student or cmid not in meeting_date:
                continue

            # Посещаемость кладём в первую работу этого урока; если работы нет — в колонку урока.
            col_id = None
            for col in columns_order:
                if str(col.get('classMeetingId') or '') == cmid:
                    col_id = str(col.get('id') or '')
                    break
            if not col_id:
                col_id = 'meeting_' + cmid
            grid[student][col_id].append(reason)

    columns_order.sort(key=lambda c: (str(c.get('date') or ''), str(c.get('lesson') or ''), str(c.get('id') or '')))
    dates_order = sorted(set(dates_order or [str(c.get('date')) for c in columns_order if c.get('date')]))

    return {
        'students': students_order,
        'dates': dates_order,
        'columns': columns_order,
        'grid': {st: {str(c.get('id') or c.get('date')): grid[st].get(str(c.get('id') or c.get('date')), []) for c in columns_order} for st in students_order},
        'classes': [{'value': k, 'label': v} for k, v in sorted(classes.items(), key=lambda kv: (kv[1], kv[0]))],
        'subjects': [{'value': k, 'label': v} for k, v in sorted(subjects.items(), key=lambda kv: (kv[1], kv[0]))],
        'class_label': class_label,
        'subject_label': subject_label,
    }

def _guess_teacher_defaults(payloads):
    defaults, subjects, terms, teachers = {}, [], [], []
    for payload in payloads:
        opts = extract_filter_options(payload)
        defaults.update(opts.get('defaults', {}))
        subjects.extend(opts.get('subjects', [])); terms.extend(opts.get('terms', [])); teachers.extend(opts.get('teachers', []))
    def uniq(items):
        seen = set(); out = []
        for it in items:
            key = it.get('value')
            if key and key not in seen:
                seen.add(key); out.append(it)
        return out
    return {'defaults': defaults, 'subjects': uniq(subjects), 'terms': uniq(terms), 'teachers': uniq(teachers)}

def _teacher_payload_variants(subject='', term='', teacher='', class_id='', start=None, end=None):
    variants = []
    base = {}
    if subject: base.update({'SGID': subject, 'sgid': subject, 'subjectGroupId': subject})
    if term: base.update({'TERMID': term, 'termId': term})
    if teacher: base.update({'TEACHERNAME': teacher, 'teacherId': teacher})
    if class_id: base.update({'classId': class_id, 'CLASSID': class_id})
    if start: base.update({'start': start, 'dateStart': start, 'from': start})
    if end: base.update({'end': end, 'dateEnd': end, 'to': end})
    variants.append(base)
    filters = []
    if subject: filters.append({'filterId': 'SGID', 'value': subject})
    if term: filters.append({'filterId': 'TERMID', 'value': term})
    if teacher: filters.append({'filterId': 'TEACHERNAME', 'value': teacher})
    if filters:
        variants.append({'filters': filters})
        variants.append({'filter': filters})
        variants.append({'selectedFilters': filters})
        variants.append({'values': {f['filterId']: f['value'] for f in filters}})
    return variants

def _env_endpoint_list(name):
    raw = os.environ.get(name, '')
    return [x.strip() for x in raw.split(',') if x.strip()]

def _unique_options(items):
    seen = set()
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        value = str(it.get('value') or '').strip()
        label = str(it.get('label') or it.get('title') or it.get('name') or value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append({'value': value, 'label': label or value})
    return out

def _teacher_filter_probe_bodies(subject='', term='', teacher='', class_id=''):
    bodies = [{}]
    flat = {}
    if subject:
        flat.update({'SGID': subject, 'sgid': subject, 'subjectGroupId': subject, 'subjectId': subject})
    if term:
        flat.update({'TERMID': term, 'termId': term, 'term': term})
    if teacher:
        flat.update({'TEACHERNAME': teacher, 'teacherId': teacher, 'teacherName': teacher})
    if class_id:
        flat.update({'CLASSID': class_id, 'classId': class_id, 'classid': class_id})
    if flat:
        bodies.append(flat)
    filters = []
    if subject:
        filters.append({'filterId': 'SGID', 'value': subject})
    if term:
        filters.append({'filterId': 'TERMID', 'value': term})
    if teacher:
        filters.append({'filterId': 'TEACHERNAME', 'value': teacher})
    if class_id:
        filters.append({'filterId': 'CLASSID', 'value': class_id})
    if filters:
        vals = {f['filterId']: f['value'] for f in filters}
        bodies.extend([
            {'filters': filters},
            {'filter': filters},
            {'selectedFilters': filters},
            {'values': vals},
            {'data': vals},
            {'request': vals},
        ])
    out, seen = [], set()
    for b in bodies:
        key = repr(b)
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out

def _candidate_values(selected, defaults, key, options, fallback):
    vals = []
    def add(v):
        v = str(v or '').strip()
        if v and v not in vals:
            vals.append(v)
    add(selected)
    add((defaults or {}).get(key))
    for it in options or []:
        if 'хим' in str(it.get('label', '')).lower():
            add(it.get('value'))
    for it in options or []:
        add(it.get('value'))
    for it in fallback or []:
        add(it.get('value'))
    return vals

def _teacher_payload_has_journal_marks(payload):
    if not isinstance(payload, dict):
        return False
    journals = payload.get('journals')
    if not isinstance(journals, list):
        return False
    for j in journals:
        if not isinstance(j, dict):
            continue
        if j.get('students') or j.get('marks') or j.get('assignments') or j.get('classMeeting'):
            return True
    return False

def teacher_known_fetch(ns, kind, start=None, end=None, class_name='', subject='', term_id=''):
    """Альтернативный способ: перебирает реальные фильтры SGID/TERMID и endpoints.

    Важно: если пользователь в интерфейсе выбрал предмет, по которому данных нет,
    сервер не останавливается на нём, а пробует остальные доступные SGID, включая
    Химию из вашего Network-ответа.
    """
    cookies, at_token = get_cookies_from_ns(ns)
    debug = [f"cookies: {', '.join(sorted(cookies.keys())) if cookies else 'EMPTY'}", f"at-token present: {bool(at_token)}"]
    if not cookies:
        return [], debug + ['Нет cookies авторизации после входа в СГО']

    sess = sgo_session_from_cookies(cookies, at_token=at_token)
    discovered = []
    try:
        page = sess.get(sgo_abs('/app/school/journal/'), headers={'Accept': 'text/html,*/*'}, timeout=20, allow_redirects=True)
        debug.append(f'GET /app/school/journal/ -> HTTP {page.status_code}; final={page.url}')
        if 'login' in str(page.url).lower() and '/journal' not in str(page.url).lower():
            debug.append('Внимание: СГО вернул страницу входа вместо журнала — cookies не авторизованы для teacher UI.')
    except Exception as e:
        debug.append(f'GET /app/school/journal/ -> {e}')
    try:
        discovered, disc_debug = discover_sgo_journal_endpoints(sess)
        debug.extend(disc_debug[:12])
    except Exception as e:
        debug.append(f'discover endpoints -> {e}')

    filter_eps = _env_endpoint_list('SGO_TEACHER_FILTER_ENDPOINTS') + [
        '/webapi/teacher/journal/filter', '/webapi/teacher/journal/filters',
        '/webapi/teacher/journals/filter', '/webapi/teacher/journals/filters',
        '/webapi/school/journal/filter', '/webapi/school/journal/filters',
        '/webapi/school/journals/filter', '/webapi/school/journals/filters',
        '/webapi/journal/filter', '/webapi/journal/filters',
        '/webapi/journals/filter', '/webapi/journals/filters',
        '/webapi/gradebook/filter', '/webapi/gradebook/filters',
        '/webapi/gradebooks/filter', '/webapi/gradebooks/filters',
        '/webapi/reports/journal/filter', '/webapi/reports/journal/filters',
    ]
    for ep in discovered:
        low = ep.lower()
        if 'filter' in low and ep not in filter_eps:
            filter_eps.insert(0, ep)

    journal_eps = _env_endpoint_list('SGO_TEACHER_JOURNAL_ENDPOINTS') + [
        '/webapi/teacher/journal', '/webapi/teacher/journal/class', '/webapi/teacher/journal/data',
        '/webapi/teacher/journals', '/webapi/teacher/journals/class', '/webapi/teacher/journals/data',
        '/webapi/school/journal', '/webapi/school/journal/class', '/webapi/school/journal/data',
        '/webapi/school/journals', '/webapi/school/journals/class', '/webapi/school/journals/data',
        '/webapi/journal', '/webapi/journal/class', '/webapi/journal/data',
        '/webapi/journals', '/webapi/journals/class', '/webapi/journals/data',
        '/webapi/gradebook', '/webapi/gradebook/class', '/webapi/gradebook/data',
        '/webapi/gradebook/journal', '/webapi/gradebook/journals',
        '/webapi/gradebooks', '/webapi/gradebooks/class', '/webapi/gradebooks/data',
    ]
    for ep in discovered:
        low = ep.lower()
        if any(w in low for w in ('journal', 'gradebook', 'mark')) and 'filter' not in low and ep not in journal_eps:
            journal_eps.insert(0, ep)

    payloads = []
    seen_payloads = set()
    def add_payload(data, label):
        if data is None:
            return False
        key = repr(data)[:5000]
        if key not in seen_payloads:
            seen_payloads.add(key)
            payloads.append(data)
            debug.append(label)
        return True

    # 1. Получаем filters, terms, teachers.
    for ep in filter_eps[:80]:
        for body in _teacher_filter_probe_bodies():
            for method in ('GET', 'POST'):
                data, err = sgo_request_json(sess, method, ep, body)
                if add_payload(data, f'{method} {ep} -> JSON OK'):
                    break
                else:
                    if len(debug) < 100:
                        debug.append(err)
            if payloads and extract_filter_options(payloads[-1]).get('subjects'):
                break

    defaults_pack = _guess_teacher_defaults(payloads)
    defaults = defaults_pack.get('defaults', {})
    subjects_opts = _unique_options(defaults_pack.get('subjects', []))
    terms_opts = _unique_options(defaults_pack.get('terms', []))
    teachers_opts = _unique_options(defaults_pack.get('teachers', []))

    subject_values = _candidate_values(subject, defaults, 'SGID', subjects_opts, TEACHER_FALLBACK_SUBJECTS)
    term_values = _candidate_values(term_id, defaults, 'TERMID', terms_opts, TEACHER_FALLBACK_TERMS)
    teacher_values = _candidate_values('', defaults, 'TEACHERNAME', teachers_opts, []) or ['']
    class_values = [str(class_name or '').strip()] if str(class_name or '').strip() else ['']

    # 2. Для meta тоже пробуем журнал: именно journals[] даёт classId/className.
    max_subjects = len(subject_values) if kind == 'journal' else min(len(subject_values), 8)
    max_terms = len(term_values) if kind == 'journal' else min(len(term_values), 3)

    attempts = 0
    max_attempts = 1200 if kind == 'journal' else 320
    for ep in journal_eps[:90]:
        ep_variants = [ep]
        # Многие реализации кладут SGID в path, поэтому пробуем и /endpoint/<SGID>.
        for sid in subject_values[:max_subjects]:
            if sid and not ep.rstrip('/').endswith('/' + sid):
                ep_variants.append(ep.rstrip('/') + '/' + sid)
        for ep2 in ep_variants[:1 + max_subjects]:
            for sid in subject_values[:max_subjects] or ['']:
                for tid in term_values[:max_terms] or ['']:
                    for teach in teacher_values[:2]:
                        for cid in class_values:
                            for body in _teacher_filter_probe_bodies(sid, tid, teach, cid):
                                for method in ('POST', 'GET'):
                                    attempts += 1
                                    if attempts > max_attempts:
                                        debug.append(f'Остановлено после {max_attempts} попыток endpoint/параметров')
                                        return payloads, debug[:160]
                                    data, err = sgo_request_json(sess, method, ep2, body)
                                    if add_payload(data, f'{method} {ep2} sid={sid or "-"} term={tid or "-"} teacher={teach or "-"} -> JSON OK'):
                                        if _teacher_payload_has_journal_marks(data):
                                            return payloads, debug[:160]
                                        if kind == 'journal':
                                            parsed = extract_journal_structured(data, cid, sid, start, end)
                                            if parsed and (parsed.get('students') or parsed.get('dates')):
                                                return payloads, debug[:160]
                                        break
                                    else:
                                        if len(debug) < 120:
                                            debug.append(err)
    return payloads, debug[:160]


def ensure_local_teacher_demo_data(login_val, school='', start='', end='', subject='Химия', class_name='8А'):
    """Создаёт минимальные локальные данные для кабинета учителя.

    Это fallback для школьного проекта: если СГО не отдаёт teacher JSON,
    lkteach.html всё равно получает расписание и журнал из SQLite school.db.
    """
    if not login_val:
        return
    db = get_db()
    existing = db.execute("SELECT COUNT(*) AS c FROM teacher_schedule_local WHERE login = ?", (login_val,)).fetchone()
    if existing and int(existing["c"] or 0) > 0:
        return

    try:
        base = datetime.strptime(start or moscow_now().strftime("%Y-%m-%d"), "%Y-%m-%d")
    except Exception:
        base = moscow_now().replace(hour=12, minute=0, second=0, microsecond=0)
    # приводим к понедельнику
    base = base - timedelta(days=base.weekday())

    schedule_rows = [
        (0, 1, "8:30 - 9:10", "Химия", "8А", "Строение атома"),
        (0, 2, "9:25 - 10:05", "Химия", "9Б", "Кислоты и основания"),
        (1, 3, "10:25 - 11:05", "Биология", "7А", "Клетка и ткани"),
        (2, 1, "8:30 - 9:10", "Химия", "8А", "Периодическая система"),
        (3, 4, "11:25 - 12:05", "Химия", "10А", "Окислительно-восстановительные реакции"),
        (4, 2, "9:25 - 10:05", "Химия", "8А", "Валентность"),
    ]
    for day_offset, num, time_text, subj, cls, theme in schedule_rows:
        d = (base + timedelta(days=day_offset)).strftime("%Y-%m-%d")
        db.execute(
            """INSERT INTO teacher_schedule_local
               (login, school, lesson_date, lesson_number, lesson_time, subject, class_name, theme)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (login_val, school, d, num, time_text, subj, cls, theme)
        )

    students = ["Иванов Иван", "Петрова Анна", "Сидоров Максим", "Кузнецова Мария", "Смирнов Артём"]
    works = [
        (base + timedelta(days=0), "Самостоятельная работа", "Самостоятельная работа", ["5", "4", "4", "5", "3"]),
        (base + timedelta(days=2), "Проверочная работа", "Проверочная работа", ["4", "5", "3", "5", "4"]),
        (base + timedelta(days=4), "Ответ на уроке", "Ответ на уроке", ["5", "", "4", "4", "5"]),
    ]
    for work_date, title, typ, marks in works:
        for student, mark in zip(students, marks):
            db.execute(
                """INSERT INTO teacher_journal_local
                   (login, school, class_name, subject, work_date, work_title, work_type, student_name, mark)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (login_val, school, class_name, subject, work_date.strftime("%Y-%m-%d"), title, typ, student, mark)
            )
    db.commit()

def local_teacher_schedule(login_val, school='', start='', end=''):
    ensure_local_teacher_demo_data(login_val, school, start, end)
    db = get_db()
    rows = db.execute(
        """SELECT * FROM teacher_schedule_local
           WHERE login = ? AND lesson_date BETWEEN ? AND ?
           ORDER BY lesson_date, lesson_number""",
        (login_val, start, end)
    ).fetchall()
    days = {}
    for r in rows:
        date = r["lesson_date"]
        days.setdefault(date, {"date": date, "weekday": datetime.strptime(date, "%Y-%m-%d").strftime("%A"), "lessons": []})
        days[date]["lessons"].append({
            "number": r["lesson_number"],
            "time": r["lesson_time"],
            "subject": r["subject"],
            "class_name": r["class_name"],
            "theme": r["theme"],
        })
    ru_weekdays = {
        "Monday": "Понедельник", "Tuesday": "Вторник", "Wednesday": "Среда",
        "Thursday": "Четверг", "Friday": "Пятница", "Saturday": "Суббота", "Sunday": "Воскресенье"
    }
    result = []
    for item in days.values():
        item["weekday"] = ru_weekdays.get(item["weekday"], item["weekday"])
        result.append(item)
    return result

def local_teacher_journal(login_val, school='', start='', end='', class_name='', subject=''):
    ensure_local_teacher_demo_data(login_val, school, start, end, subject or "Химия", class_name or "8А")
    db = get_db()
    params = [login_val, start, end]
    where = "login = ? AND work_date BETWEEN ? AND ?"
    if class_name:
        where += " AND class_name = ?"
        params.append(class_name)
    if subject:
        where += " AND subject = ?"
        params.append(subject)
    rows = db.execute(
        f"""SELECT * FROM teacher_journal_local
            WHERE {where}
            ORDER BY work_date, work_title, student_name""",
        params
    ).fetchall()
    if not rows and (class_name or subject):
        rows = db.execute(
            """SELECT * FROM teacher_journal_local
               WHERE login = ? AND work_date BETWEEN ? AND ?
               ORDER BY work_date, work_title, student_name""",
            (login_val, start, end)
        ).fetchall()
    students = []
    columns_map = {}
    grid = {}
    for r in rows:
        st = r["student_name"]
        if st not in students:
            students.append(st)
        col_id = f'{r["work_date"]}|{r["work_title"]}'
        columns_map[col_id] = {"id": col_id, "date": r["work_date"], "title": r["work_title"], "type": r["work_type"]}
        grid.setdefault(st, {}).setdefault(col_id, [])
        if r["mark"]:
            grid[st][col_id].append(r["mark"])
    columns = list(columns_map.values())
    dates = sorted({c["date"] for c in columns})
    first = rows[0] if rows else None
    return {
        "students": students,
        "dates": dates,
        "columns": columns,
        "grid": grid,
        "classes": [{"value": first["class_name"], "label": first["class_name"]}] if first else [],
        "subjects": [{"value": first["subject"], "label": first["subject"]}] if first else [],
        "class_label": (first["class_name"] if first else (class_name or "")),
        "subject_label": (first["subject"] if first else (subject or "")),
    }


@app.route('/api/teacher/meta', methods=['GET', 'POST', 'OPTIONS'])
def teacher_meta_api():
    if request.method == 'OPTIONS': return jsonify({'success': True})
    data = request.get_json(silent=True) or dict(request.args)
    login_val, password_val, school = resolve_teacher_credentials(data)
    if not login_val or not password_val: return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400
    async def _fetch():
        ns = None
        try:
            ns = await _teacher_login(login_val, password_val, school)
            cookies, at_token = get_cookies_from_ns(ns)
            if not at_token and cookies:
                at_token = fetch_at_token_from_sgo(cookies)
            sess = sgo_session_from_cookies(cookies, at_token=at_token)
            all_debug = [f'at-token present: {bool(at_token)}']

            # ── 1. Пробуем /webapi/subjectgroups (SGO 5.51 реальный endpoint) ──
            sg_data, sg_debug = fetch_sgo_subjectgroups(sess)
            all_debug.extend(sg_debug)
            sg_map, subjects, classes, terms = {}, [], [], []
            if sg_data is not None:
                sg_map, subjects, classes, terms = parse_subjectgroups_array(sg_data)

            # ── 2. Если subjectgroups пустой — fallback на старый перебор ──
            if not subjects:
                payloads, debug2 = teacher_known_fetch(ns, 'meta')
                all_debug.extend(debug2[:20])
                classes_map = {}; subjects_map = {}; defaults_extra = {}
                for payload in payloads:
                    opts = extract_teacher_options_from_payload(payload)
                    for it in opts.get('classes', []): classes_map[it['value']] = it['label']
                    for it in opts.get('subjects', []): subjects_map[it['value']] = it['label']
                    defaults_extra.update(opts.get('defaults', {}))
                    terms.extend(opts.get('terms', []))
                classes  = [{'value': k, 'label': v} for k, v in sorted(classes_map.items(), key=lambda kv: (kv[1], kv[0]))]
                subjects = [{'value': k, 'label': v} for k, v in sorted(subjects_map.items(), key=lambda kv: (kv[1], kv[0]))]

            # ── 2.5. Если старые мета-endpoint-ы пустые, берём sgId из минипанели
            # /webapi/dashboard/extensions/classJournal и раскрываем их через
            # /webapi/subjectgroups/{id}, /webapi/grade/studentList и
            # /webapi/schedule/classmeetings. Это основной путь для реальных
            # карточек lkteacher, когда /webapi/subjectgroups не даёт список.
            classjournal_dashboard = None
            subjectgroup_api_details = {}
            if not sg_map:
                try:
                    cj_data, cj_ids, cj_debug = fetch_sgo_classjournal_dashboard(sess)
                    all_debug.extend(cj_debug[:20])
                    classjournal_dashboard = {
                        'found': cj_data is not None,
                        'sg_ids': cj_ids[:120],
                    }
                    if cj_ids:
                        today = moscow_now().strftime('%Y-%m-%d')
                        bundle, bundle_schedule, bundle_debug = fetch_sgo_subjectgroup_bundle(
                            sess,
                            cj_ids,
                            today,
                            (moscow_now() + timedelta(days=21)).strftime('%Y-%m-%d'),
                            limit=80,
                        )
                        all_debug.extend(bundle_debug[:80])
                        subjectgroup_api_details = bundle
                        for sgid, item in (bundle or {}).items():
                            norm = (item or {}).get('normalized') or {}
                            if norm:
                                sg_map[str(sgid)] = norm
                        if sg_map:
                            subjects, classes, terms = subjectgroup_options_from_map(sg_map)
                except Exception as e:
                    all_debug.append(f'classJournal meta fallback error: {type(e).__name__}: {e}')

            # ── 3. Defaults ──
            defaults = {}
            for it in subjects:
                if 'хим' in str(it.get('label', '')).lower():
                    defaults['SGID'] = it.get('value')
                    break
            if not defaults.get('SGID') and subjects:
                defaults['SGID'] = subjects[0]['value']

            # Merge fallback lists
            if not classes:
                classes = TEACHER_AUTO_CLASSES
            subjects = _merge_options(subjects, TEACHER_FALLBACK_SUBJECTS)
            terms    = _merge_options(terms,    TEACHER_FALLBACK_TERMS)
            if not defaults.get('TERMID') and terms:
                defaults['TERMID'] = terms[-1].get('value')

            # Сохраняем sg_map в сессии (через simple cache) для schedule/journal
            _sg_map_cache[login_val] = sg_map

            await safe_teacher_close(ns)
            note = '' if sg_map else 'Субъект-группы не найдены через /webapi/subjectgroups. Используется список из базы данных.'
            return jsonify({
                'success': True,
                'classes': classes,
                'subjects': subjects,
                'terms': terms,
                'defaults': defaults,
                'school': school,
                'note': note,
                'subjectgroups': subjectgroup_rows_from_map(sg_map),
                'class_journal_dashboard': classjournal_dashboard,
                'subjectgroup_api_details': subjectgroup_api_details,
                'debug': all_debug[:60],
            })
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                await safe_teacher_close(ns)
            return jsonify({'success': False, 'error': str(e), 'school': school}), 500
    return run_async(_fetch())

@app.route('/api/teacher/schedule', methods=['GET', 'POST', 'OPTIONS'])
def teacher_schedule_api():
    if request.method == 'OPTIONS': return jsonify({'success': True})
    data = request.get_json(silent=True) or dict(request.args)
    login_val, password_val, school = resolve_teacher_credentials(data)
    start = data.get('start'); end = data.get('end')
    if not login_val or not password_val: return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400
    if not start or not end: return jsonify({'success': False, 'error': 'Не указан период start/end'}), 400
    async def _fetch():
        ns = None
        try:
            ns = await _teacher_login(login_val, password_val, school)
            cookies, at_token = get_cookies_from_ns(ns)
            if not at_token and cookies:
                at_token = fetch_at_token_from_sgo(cookies)
            sess = sgo_session_from_cookies(cookies, at_token=at_token)
            all_debug = [f'at-token present: {bool(at_token)}']

            # ── 1. Получаем субъект-группы (для join по subjectGroupId) ──
            sg_map = _sg_map_cache.get(login_val) or {}
            if not sg_map:
                sg_data, sg_debug = fetch_sgo_subjectgroups(sess)
                all_debug.extend(sg_debug)
                if sg_data:
                    sg_map, _, _, _ = parse_subjectgroups_array(sg_data)
                    _sg_map_cache[login_val] = sg_map

            # ── 2. Получаем расписание /webapi/classmeetings ──
            teacher_id = ''
            for info in sg_map.values():
                tids = info.get('teacher_ids') or []
                if tids:
                    teacher_id = tids[0]
                    break
            all_debug.append(f'teacher_id from subjectgroups = {teacher_id or "EMPTY"}')
            cm_data, cm_debug = fetch_sgo_classmeetings(sess, start, end, teacher_id=teacher_id)
            all_debug.extend(cm_debug)

            result = []
            if cm_data is not None:
                result = parse_classmeetings_schedule(cm_data, sg_map, start, end)
                all_debug.append(f'parse_classmeetings_schedule -> {sum(len(d["lessons"]) for d in result)} уроков в {len(result)} днях')

            await safe_teacher_close(ns)

            if not result:
                return jsonify({
                    'success': False,
                    'error': 'СГО не вернул расписание через /webapi/classmeetings. Локальная SQLite отключена, чтобы не маскировать проблему.',
                    'school': school,
                    'debug': all_debug[:80]
                }), 502
            return jsonify({'success': True, 'data': result, 'school': school, 'note': '', 'debug': all_debug[:80]})
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                await safe_teacher_close(ns)
            return jsonify({'success': False, 'error': str(e), 'school': school, 'debug': [traceback.format_exc()]}), 500
    return run_async(_fetch())

@app.route('/api/teacher/journal', methods=['GET', 'POST', 'OPTIONS'])
def teacher_journal_api():
    if request.method == 'OPTIONS': return jsonify({'success': True})
    data = request.get_json(silent=True) or dict(request.args)
    login_val, password_val, school = resolve_teacher_credentials(data)
    start = data.get('start'); end = data.get('end')
    class_name     = (data.get('classId')   or data.get('class_id')   or data.get('class_name') or '').strip()
    subject_filter = (data.get('subjectId') or data.get('subject_id') or data.get('subject')     or '').strip()
    term_id        = (data.get('termId')    or data.get('term_id')    or data.get('term')         or '').strip()
    if not login_val or not password_val: return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400
    if not start or not end: return jsonify({'success': False, 'error': 'Не указан период start/end'}), 400
    async def _fetch():
        ns = None
        try:
            ns = await _teacher_login(login_val, password_val, school)
            cookies, at_token = get_cookies_from_ns(ns)
            if not at_token and cookies:
                at_token = fetch_at_token_from_sgo(cookies)
            sess = sgo_session_from_cookies(cookies, at_token=at_token)
            all_debug = [f'at-token present: {bool(at_token)}']

            # ── 1. Получаем субъект-группы ──
            sg_map = _sg_map_cache.get(login_val) or {}
            if not sg_map:
                sg_data, sg_debug = fetch_sgo_subjectgroups(sess)
                all_debug.extend(sg_debug)
                if sg_data:
                    sg_map, _, _, _ = parse_subjectgroups_array(sg_data)
                    _sg_map_cache[login_val] = sg_map

            # ── 2. Определяем какие SGID запрашивать ──
            # subject_filter может быть числовым ID (из дропдауна) или именем
            sgid_candidates = []
            if subject_filter:
                # Если это прямо ID из subjectgroups — берём его первым
                if subject_filter in sg_map:
                    sgid_candidates.append(subject_filter)
                # Ищем совпадение по имени предмета
                for sgid, info in sg_map.items():
                    if subject_filter.lower() in info.get('subject', '').lower():
                        if sgid not in sgid_candidates:
                            sgid_candidates.append(sgid)
            if not sgid_candidates:
                # Берём все доступные SGID (сначала химию)
                for sgid, info in sg_map.items():
                    if 'хим' in info.get('subject', '').lower():
                        sgid_candidates.insert(0, sgid)
                    else:
                        sgid_candidates.append(sgid)
            # Fallback: числовые ID из TEACHER_FALLBACK_SUBJECTS
            if not sgid_candidates:
                sgid_candidates = [it['value'] for it in TEACHER_FALLBACK_SUBJECTS if 'хим' in it['label'].lower()]
                sgid_candidates += [it['value'] for it in TEACHER_FALLBACK_SUBJECTS if 'хим' not in it['label'].lower()]

            try:
                cj_data, cj_ids, cj_debug = fetch_sgo_classjournal_dashboard(sess)
                all_debug.extend(cj_debug[:10])
                for sgid in cj_ids:
                    if sgid not in sgid_candidates:
                        sgid_candidates.append(sgid)
            except Exception as e:
                all_debug.append(f'classJournal dashboard error: {type(e).__name__}: {e}')

            # ── 3. Запрашиваем /webapi/journals для каждого SGID ──
            merged = None
            for sgid in sgid_candidates[:12]:
                j_data, j_debug = fetch_sgo_journal_511(sess, sgid, start, end)
                all_debug.extend(j_debug[:5])
                if j_data is None:
                    continue
                structured = extract_journal_structured(j_data, class_name, '', start, end)
                if structured and (structured.get('students') or structured.get('columns')):
                    merged = structured
                    all_debug.append(f'Журнал найден: sg={sgid}, студентов={len(structured.get("students",[]))}, колонок={len(structured.get("columns",[]))}')
                    break

            note = ''
            if not merged or not merged.get('students'):
                bundle, bundle_schedule, bundle_debug = fetch_sgo_subjectgroup_bundle(sess, sgid_candidates[:12], start, end, limit=12)
                all_debug.extend(bundle_debug[:80])
                for sgid in sgid_candidates[:12]:
                    item = (bundle or {}).get(str(sgid)) or {}
                    students = item.get('students') or []
                    schedule = item.get('schedule') or []
                    if not students:
                        continue
                    norm = item.get('normalized') or (sg_map or {}).get(str(sgid), {}) or {}
                    if norm and str(sgid) not in sg_map:
                        sg_map[str(sgid)] = norm
                    merged = empty_journal_from_students_schedule(
                        students,
                        schedule or bundle_schedule,
                        sgid=str(sgid),
                        sg_info=norm,
                        start=start,
                        end=end,
                    )
                    note = 'Данные загружены через /webapi/grade/studentList и /webapi/schedule/classmeetings; оценки появятся, когда /webapi/journals вернет marks.'
                    all_debug.append(f'journal shell built from studentList+schedule: sg={sgid}, students={len(merged.get("students", []))}, columns={len(merged.get("columns", []))}')
                    break

            await safe_teacher_close(ns)

            if not merged or not merged.get('students'):
                return jsonify({
                    'success': False,
                    'error': 'СГО не вернул оценки через /webapi/journals. Локальная SQLite отключена, чтобы не маскировать проблему.',
                    'school': school,
                    'debug': all_debug[:120]
                }), 502
            # Добавляем названия классов из sg_map если они не распарсились
            if merged and not merged.get('class_label') and sg_map:
                sgid_used = subject_filter or (sgid_candidates[0] if sgid_candidates else '')
                if sgid_used in sg_map:
                    merged['class_label'] = sg_map[sgid_used].get('class_name', '')
                    merged['subject_label'] = merged.get('subject_label') or sg_map[sgid_used].get('subject', '')

            classes = merged.get('classes') or TEACHER_AUTO_CLASSES
            subjects = merged.get('subjects') or []
            return jsonify({
                'success': True,
                'data': {
                    'students':     merged.get('students', []),
                    'dates':        merged.get('dates', []),
                    'columns':      merged.get('columns', []),
                    'grid':         merged.get('grid', {}),
                    'class_label':  merged.get('class_label', ''),
                    'subject_label':merged.get('subject_label', ''),
                },
                'classes': classes, 'subjects': subjects,
                'school': school, 'note': note, 'debug': all_debug[:60],
            })
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                await safe_teacher_close(ns)
            return jsonify({'success': False, 'error': str(e), 'school': school, 'debug': [traceback.format_exc()]}), 500
    return run_async(_fetch())


# ================== TEACHER SGO PROBE / DIAGNOSTICS ==================
def _short_text(value, limit=900):
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        text = repr(value)
    text = text.replace("\x00", "")
    return text[:limit] + ("..." if len(text) > limit else "")


def _json_preview(value, limit=2500):
    try:
        import json as _json
        text = _json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        text = repr(value)
    return _short_text(text, limit)


def _probe_http(sess, method, path, params=None, timeout=10):
    """Один диагностический HTTP-запрос в СГО: статус, тип ответа, JSON/HTML snippet."""
    url = sgo_abs(path)
    params = params or {}
    started = time.time()
    item = {"method": method, "url": url, "params": params}
    try:
        if method == "POST":
            resp = sess.post(url, json=params, timeout=timeout, allow_redirects=True)
        else:
            resp = sess.get(url, params=params, timeout=timeout, allow_redirects=True)
        item.update({
            "ok": resp.status_code == 200,
            "status": resp.status_code,
            "final_url": resp.url,
            "content_type": resp.headers.get("Content-Type", ""),
            "elapsed_sec": round(time.time() - started, 2),
        })
        data = _json_or_none(resp)
        if data is not None:
            item["json"] = True
            item["preview"] = _json_preview(data)
            if isinstance(data, dict):
                item["keys"] = list(data.keys())[:40]
            elif isinstance(data, list):
                item["items"] = len(data)
                if data:
                    item["first_item"] = _json_preview(data[0], 1200)
        else:
            item["json"] = False
            item["preview"] = _short_text(resp.text or "", 1200)
        return item
    except Exception as e:
        item.update({"ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_sec": round(time.time() - started, 2)})
        return item


@app.route('/api/teacher/probe', methods=['POST', 'OPTIONS'])
def teacher_probe_api():
    """Проверяет, получается ли вообще что-то взять из авторизованного кабинета СГО.

    В отличие от обычных /api/teacher/journal и /api/teacher/schedule, этот endpoint
    не пытается красиво собрать журнал. Он возвращает диагностику: авторизация,
    cookies, доступность страниц и JSON endpoint-ов, первые куски ответов.
    """
    if request.method == 'OPTIONS':
        return jsonify({'success': True})

    data = request.get_json(silent=True) or {}
    login_val = (data.get('login') or '').strip()
    password_val = (data.get('password') or '').strip()
    school = get_requested_school(data)
    start = (data.get('start') or '').strip() or (moscow_now() - timedelta(days=7)).strftime('%Y-%m-%d')
    end = (data.get('end') or '').strip() or (moscow_now() + timedelta(days=14)).strftime('%Y-%m-%d')
    debug_probe = truthy(data.get('debug_probe'))

    if not login_val or not password_val:
        return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400

    async def _fetch():
        ns = None
        report = {
            'success': True,
            'login': login_val,
            'school_requested': school,
            'period': {'start': start, 'end': end},
            'steps': [],
            'pages': [],
            'json_endpoints': [],
            'parsed': {},
            'debug': [],
        }
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            actual_school = await asyncio.wait_for(sgo_login_with_fallback(ns, login_val, password_val, school, allow_teacher=True), timeout=15)
            report['school_actual'] = actual_school
            report['steps'].append({'name': 'NetSchoolAPI login', 'ok': True, 'message': 'Авторизация выполнена или получены teacher cookies'})

            cookies, at_token = get_cookies_from_ns(ns)
            report['cookies'] = sorted(list((cookies or {}).keys()))
            report['at_token_present'] = bool(at_token)
            report.setdefault('debug', []).append(f'at-token present: {bool(at_token)}')
            if not cookies:
                report['steps'].append({'name': 'cookies', 'ok': False, 'message': 'После входа cookies пустые'})
                await safe_teacher_close(ns)
                return jsonify(report)

            sess = sgo_session_from_cookies(cookies, at_token=at_token)

            # 1) Проверяем доступ к основным страницам кабинета.
            for page in ['/', '/app/', '/app/school/journal/', '/app/school/diary/', '/webapi/context', '/webapi/profile']:
                report['pages'].append(_probe_http(sess, 'GET', page, {}, timeout=10))

            # 2) Ищем endpoint-ы в HTML/JS страницы журнала.
            try:
                discovered, disc_debug = discover_sgo_journal_endpoints(sess)
                report['discovered_endpoints'] = discovered[:80]
                report['debug'].extend(disc_debug[:40])
            except Exception as e:
                report['debug'].append(f'discover_sgo_journal_endpoints: {type(e).__name__}: {e}')
                discovered = []

            # 3) Пробуем известные endpoint-ы СГО с разными параметрами.
            date_params = {'startDate': start, 'endDate': end, 'start': start, 'end': end, 'from': start, 'to': end}
            known = [
                '/webapi/context',
                '/webapi/profile',
                '/webapi/teacher/subjectgroups',
                '/webapi/subjectgroups',
                '/webapi/teacher/classmeetings',
                '/webapi/classmeetings',
                '/webapi/teacher/timetable',
                '/webapi/timetable',
                '/webapi/teacher/journal/init',
                '/webapi/journal/init',
                '/webapi/teacher/journal/filter',
                '/webapi/journal/filter',
                '/webapi/teacher/journals',
                '/webapi/journals',
                '/webapi/teacher/journal',
                '/webapi/journal',
                '/webapi/teacher/gradebook',
                '/webapi/gradebook',
            ]
            for ep in discovered:
                low = ep.lower()
                if any(word in low for word in ['journal', 'schedule', 'timetable', 'classmeeting', 'subjectgroup', 'gradebook', 'context', 'profile']):
                    if ep not in known:
                        known.append(ep)
            known = known[:60]
            for ep in known:
                params = date_params if any(w in ep.lower() for w in ['meeting', 'journal', 'schedule', 'timetable', 'gradebook']) else {}
                report['json_endpoints'].append(_probe_http(sess, 'GET', ep, params, timeout=8))
                if len(report['json_endpoints']) < 90 and any(w in ep.lower() for w in ['journal', 'filter', 'gradebook']):
                    report['json_endpoints'].append(_probe_http(sess, 'POST', ep, params, timeout=8))

            # 4) Используем уже написанные парсеры, чтобы понять, что реально удалось собрать.
            try:
                sg_data, sg_debug = fetch_sgo_subjectgroups(sess)
                report['debug'].extend(sg_debug[:30])
                if sg_data is not None:
                    sg_map, subjects, classes, terms = parse_subjectgroups_array(sg_data)
                    report['parsed']['subjectgroups'] = {
                        'count': len(sg_map),
                        'subjects': subjects[:20],
                        'classes': classes[:20],
                        'terms': terms[:20],
                    }
                    _sg_map_cache[login_val] = sg_map
                else:
                    sg_map = {}
            except Exception as e:
                sg_map = {}
                report['parsed']['subjectgroups_error'] = f'{type(e).__name__}: {e}'

            try:
                teacher_id = ''
                for info in sg_map.values():
                    tids = info.get('teacher_ids') or []
                    if tids:
                        teacher_id = tids[0]
                        break
                cm_data, cm_debug = fetch_sgo_classmeetings(sess, start, end, teacher_id=teacher_id)
                report['debug'].extend(cm_debug[:30])
                if cm_data is not None:
                    sched = parse_classmeetings_schedule(cm_data, sg_map, start, end)
                    report['parsed']['schedule'] = {
                        'days': len(sched),
                        'lessons': sum(len(d.get('lessons', [])) for d in sched),
                        'sample': sched[:3],
                    }
            except Exception as e:
                report['parsed']['schedule_error'] = f'{type(e).__name__}: {e}'

            try:
                journal_found = None
                sgids = list((sg_map or {}).keys())[:12]
                if not sgids:
                    sgids = [it.get('value') for it in TEACHER_FALLBACK_SUBJECTS if it.get('value')][:12]
                for sgid in sgids:
                    j_data, j_debug = fetch_sgo_journal_511(sess, sgid, start, end)
                    report['debug'].extend(j_debug[:4])
                    if j_data is None:
                        continue
                    structured = extract_journal_structured(j_data, '', '', start, end)
                    if structured and (structured.get('students') or structured.get('columns') or structured.get('dates')):
                        journal_found = {
                            'sgid': sgid,
                            'students': len(structured.get('students', [])),
                            'columns': len(structured.get('columns', [])),
                            'dates': structured.get('dates', [])[:20],
                            'sample_students': structured.get('students', [])[:5],
                            'sample_grid': dict(list((structured.get('grid') or {}).items())[:3]),
                        }
                        break
                report['parsed']['journal'] = journal_found or {'found': False, 'message': 'Структурированный журнал не найден'}
            except Exception as e:
                report['parsed']['journal_error'] = f'{type(e).__name__}: {e}'

            await safe_teacher_close(ns)
            return jsonify(report)
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                await safe_teacher_close(ns)
            report['success'] = False
            report['error'] = str(e)
            report['traceback'] = traceback.format_exc()
            return jsonify(report), 500

    return run_async(_fetch())


# ================== LKTEACHER DASHBOARD (POST ALIAS + REVERSE SGO API) ==================
@app.route('/api/lkteacher/dashboard', methods=['GET', 'POST', 'OPTIONS'])
def lkteacher_dashboard_api():
    """Единая точка для lkteacher.html.

    lkteacher.html отправляет POST на /api/lkteacher/dashboard. Если POST-route нет,
    Flask цепляет универсальный static route /<path:filename> и получает 405.
    Здесь также используется reverse-подход: вход через NetSchoolAPI -> cookies ->
    requests.Session -> реальные webapi endpoint-ы СГО.
    """
    if request.method == 'OPTIONS':
        return jsonify({'success': True})

    data = request.get_json(silent=True) or request.form or request.args or {}
    login_val = (data.get('login') or '').strip() 
    password_val = (data.get('password') or '').strip()
    school = get_requested_school(data) or 'МКОУ Буерак-Поповская СКШ'
    password_val, school = apply_saved_login_fields(login_val, password_val, school)
    start = (data.get('start') or '').strip() or (moscow_now() - timedelta(days=7)).strftime('%Y-%m-%d')
    end = (data.get('end') or '').strip() or (moscow_now() + timedelta(days=14)).strftime('%Y-%m-%d')
    debug_probe = truthy(data.get('debug_probe'))

    async def _fetch():
        ns = None
        result = {
            'success': True,
            'login': login_val,
            'school': school,
            'school_requested': school,
            'school_actual': school,
            'period': {'start': start, 'end': end},
            'cookies': [],
            'pages': [],
            'discovered_endpoints': [],
            'json_endpoints': [],
            'subjectgroups': [],
            'schedule': [],
            'journal': {'found': False, 'message': 'Структурированный журнал не найден.'},
            'user_profile': {},
            'workload': {},
            'parsed': {},
            'debug': [],
        }
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            actual_school = await asyncio.wait_for(sgo_login_with_fallback(ns, login_val, password_val, school, allow_teacher=True), timeout=15)
            result['school_actual'] = actual_school
            result['school'] = actual_school
            result['debug'].append('NetSchoolAPI login: ok')

            cookies, at_token = get_cookies_from_ns(ns)
            result['cookies'] = sorted(list((cookies or {}).keys()))
            result['debug'].append(f'at-token from reflection: {bool(at_token)}')
            if not cookies:
                result['success'] = False
                result['error'] = 'После входа в СГО cookies пустые. Невозможно выполнить reverse API запросы.'
                await safe_teacher_close(ns)
                return jsonify(result), 401

            # Если at-токен не нашли рефлексией — явно запрашиваем через /webapi/auth/getdata
            if not at_token:
                at_token = fetch_at_token_from_sgo(cookies)
                result['debug'].append(f'at-token from getdata: {bool(at_token)}')
            result['at_token_present'] = bool(at_token)

            sess = sgo_session_from_cookies(cookies, at_token=at_token)

            # Пед. состав: сразу пробуем получить всех ответственных за кабинеты из /webapi/rooms.
            # Этот endpoint часто доступен учителю и даёт список преподавателей школы,
            # даже если /webapi/subjectgroups отфильтрован только по текущему учителю.
            rooms_staff_catalog = []
            try:
                rooms_data, rooms_staff_catalog, rooms_debug = fetch_sgo_rooms_staff(sess)
                result['rooms'] = rooms_data
                result['rooms_staff_catalog'] = rooms_staff_catalog
                result['debug'].extend(rooms_debug[:20])
                result['parsed']['rooms'] = {
                    'rooms': len(rooms_data or []),
                    'responsible_staff': len(rooms_staff_catalog or [])
                }
            except Exception:
                rooms_staff_catalog = []
                result['rooms'] = []
                result['rooms_staff_catalog'] = []
                result['debug'].append('rooms staff error: ' + traceback.format_exc())

            # Полный педсостав школы: основной источник — /webapi/users/staff/teachers?withSubjects=true.
            # В отличие от grade/journal/filter он не ограничивается только вошедшим учителем.
            staff_teachers_catalog = []
            try:
                staff_teachers_data, staff_teachers_catalog, staff_teachers_debug = fetch_sgo_staff_teachers(sess)
                result['staff_teachers'] = staff_teachers_data
                result['staff_teachers_catalog'] = staff_teachers_catalog
                result['debug'].extend(staff_teachers_debug[:20])
                result['parsed']['staff_teachers'] = {
                    'teachers': len(staff_teachers_data or []),
                    'staff': len(staff_teachers_catalog or []),
                    'source': '/webapi/users/staff/teachers?withSubjects=true'
                }
            except Exception:
                staff_teachers_catalog = []
                result['staff_teachers'] = []
                result['staff_teachers_catalog'] = []
                result['debug'].append('staff teachers error: ' + traceback.format_exc())

            local_user = {}
            try:
                row = get_db().execute('SELECT * FROM users WHERE login = ?', (login_val,)).fetchone()
                if row:
                    local_user = dict(row)
            except Exception:
                local_user = {}
            try:
                result['user_profile'] = fetch_sgo_user_profile(sess, login_val=login_val, local_user=local_user)
                result['debug'].append('teacher profile source: ' + str(result['user_profile'].get('source') or ''))
            except Exception:
                result['user_profile'] = {'login': login_val, 'full_name': local_user.get('full_name',''), 'email': local_user.get('email',''), 'role': local_user.get('role','Учитель'), 'school': local_user.get('school', school), 'user_id': '', 'source': 'local-error'}
                result['debug'].append('teacher profile error: ' + traceback.format_exc())

            discovered = []
            if debug_probe:
                for page in ['/', '/app/', '/app/school/journal/', '/app/school/diary/', '/webapi/context', '/webapi/security/context', '/webapi/auth/getdata', '/webapi/profile']:
                    result['pages'].append(_probe_http(sess, 'GET', page, {}, timeout=3))
                try:
                    discovered, disc_debug = discover_sgo_journal_endpoints(sess)
                    result['discovered_endpoints'] = discovered[:100]
                    result['debug'].extend(disc_debug[:60])
                except Exception as e:
                    result['debug'].append(f'discover_sgo_journal_endpoints: {type(e).__name__}: {e}')
            else:
                result['debug'].append('debug_probe disabled: skipped slow SGO endpoint scan')

            date_params = {'startDate': start, 'endDate': end, 'dateStart': start, 'dateEnd': end, 'start': start, 'end': end, 'from': start, 'to': end}
            known = [
                '/webapi/context', '/webapi/profile', '/webapi/dashboard/extensions/classJournal',
                '/webapi/teacher/subjectgroups', '/webapi/subjectgroups', '/webapi/grade/studentList',
                '/webapi/schedule/classmeetings',
                '/webapi/teacher/gradebook', '/webapi/gradebook',
            ]
            for ep in discovered:
                low = str(ep).lower()
                if any(w in low for w in ['journal', 'schedule', 'timetable', 'classmeeting', 'subjectgroup', 'gradebook', 'context', 'profile']):
                    if ep not in known:
                        known.append(ep)
            if debug_probe:
                for ep in known[:20]:
                    params = date_params if any(w in ep.lower() for w in ['meeting', 'journal', 'schedule', 'timetable', 'gradebook']) else {}
                    result['json_endpoints'].append(_probe_http(sess, 'GET', ep, params, timeout=3))
                    if any(w in ep.lower() for w in ['journal', 'filter', 'gradebook']):
                        result['json_endpoints'].append(_probe_http(sess, 'POST', ep, params, timeout=3))

            sg_map = {}
            all_sg_map_for_staff = {}
            sg_filter_matched = False
            try:
                sg_data, sg_debug = fetch_sgo_subjectgroups(sess)
                result['debug'].extend(sg_debug[:40])
                if sg_data is not None:
                    sg_map, subjects, classes, terms = parse_subjectgroups_array(sg_data)
                    all_sg_map_for_staff = dict(sg_map or {})
                    try:
                        result['all_subjectgroup_details'] = build_subjectgroup_catalog(all_sg_map_for_staff, {})
                        result['staff_catalog'] = result['all_subjectgroup_details'].get('staff') or []
                        result['parsed']['all_staff'] = {
                            'staff': len(result.get('staff_catalog') or []),
                            'subjectgroups': len(all_sg_map_for_staff or {})
                        }
                    except Exception:
                        result['staff_catalog'] = []
                        result['debug'].append('all staff catalog build error: ' + traceback.format_exc())
                    profile_teacher_id = str((result.get('user_profile') or {}).get('user_id') or (result.get('user_profile') or {}).get('teacher_id') or '').strip()
                    if profile_teacher_id:
                        exact = {str(sgid): info for sgid, info in (sg_map or {}).items() if profile_teacher_id in {str(x).strip() for x in (info.get('teacher_ids') or [])}}
                        if exact:
                            sg_map = exact
                            filter_note = f'subjectgroups strictly filtered by profile teacherId {profile_teacher_id}: {len(sg_map)}/{len(sg_data)}'
                            sg_filter_matched = True
                        else:
                            sg_map = {}
                            filter_note = f'subjectgroups hidden: no exact teacherId {profile_teacher_id} match in /webapi/subjectgroups; waiting for classJournal bundle'
                            sg_filter_matched = False
                    else:
                        sg_map, filter_note = filter_subjectgroups_for_teacher(sg_map, result.get('user_profile') or {}, login_val=login_val, local_user=local_user)
                        sg_filter_matched = filter_note.startswith('subjectgroups filtered')
                    subjects, classes, terms = subjectgroup_options_from_map(sg_map)
                    result['debug'].append(filter_note)
                    if not (result.get('user_profile') or {}).get('user_id') and sg_filter_matched:
                        fallback_teacher_id = teacher_profile_id_from_subjectgroups(sg_map)
                        if fallback_teacher_id:
                            result['user_profile']['user_id'] = fallback_teacher_id
                            result['debug'].append('teacher profile id from filtered subjectgroups')
                    result['subjectgroups'] = subjectgroup_rows_from_map(sg_map)
                    result['parsed']['subjectgroups'] = {'count': len(result['subjectgroups']), 'subjects': subjects[:40], 'classes': classes[:40], 'terms': terms[:20]}
                    _sg_map_cache[login_val] = sg_map
            except Exception as e:
                result['parsed']['subjectgroups_error'] = f'{type(e).__name__}: {e}'
                result['debug'].append('subjectgroups error: ' + traceback.format_exc())

            try:
                cj_data, cj_ids, cj_debug = fetch_sgo_classjournal_dashboard(sess)
                result['debug'].extend(cj_debug[:20])
                result['class_journal_dashboard'] = {
                    'found': cj_data is not None,
                    'sg_ids': cj_ids[:120],
                    'raw_preview': json.dumps(cj_data, ensure_ascii=False)[:3000] if cj_data is not None else '',
                }
                result['parsed']['class_journal_dashboard'] = {'found': cj_data is not None, 'ids': len(cj_ids)}
                if cj_ids:
                    sgids_for_bundle = list((sg_map or {}).keys())
                    for sgid in cj_ids:
                        if str(sgid) not in sgids_for_bundle:
                            sgids_for_bundle.append(str(sgid))
                    bundle, bundle_schedule, bundle_debug = fetch_sgo_subjectgroup_bundle(sess, sgids_for_bundle, start, end, limit=80)
                    result['debug'].extend(bundle_debug[:120])
                    # Пед. состав должен показывать всех преподавателей школы, а не только текущего учителя.
                    # Поэтому до фильтрации bundle по teacherId собираем общий каталог из всех sgId,
                    # найденных через /webapi/dashboard/extensions/classJournal и /webapi/subjectgroups/{sgId}.
                    try:
                        all_bundle_sg_map_for_staff = {}
                        for _sgid, _item in (bundle or {}).items():
                            _norm = (_item or {}).get('normalized') if isinstance(_item, dict) else None
                            if isinstance(_norm, dict) and _norm:
                                all_bundle_sg_map_for_staff[str(_sgid)] = _norm
                        if all_bundle_sg_map_for_staff:
                            _all_bundle_catalog = build_subjectgroup_catalog(all_bundle_sg_map_for_staff, {})
                            _all_bundle_staff = _all_bundle_catalog.get('staff') or []
                            if len(_all_bundle_staff) > len(result.get('staff_catalog') or []):
                                result['all_subjectgroup_details'] = _all_bundle_catalog
                                result['staff_catalog'] = _all_bundle_staff
                                result['parsed']['all_staff'] = {
                                    'staff': len(_all_bundle_staff),
                                    'subjectgroups': len(all_bundle_sg_map_for_staff),
                                    'source': 'classJournal subjectgroup bundle before teacher filter'
                                }
                    except Exception:
                        result['debug'].append('all staff catalog from classJournal bundle error: ' + traceback.format_exc())
                    profile_teacher_id = str((result.get('user_profile') or {}).get('user_id') or (result.get('user_profile') or {}).get('teacher_id') or '').strip()
                    if profile_teacher_id:
                        filtered_bundle = _filter_bundle_by_teacher_id(bundle, profile_teacher_id)
                        result['debug'].append(f'classJournal bundle strictly filtered by teacherId {profile_teacher_id}: {len(filtered_bundle)}/{len(bundle or {})}')
                        bundle = filtered_bundle
                        # Оставляем расписание только по тем sgId, где найден текущий teacherId.
                        allowed_sgids = {str(x) for x in (bundle or {}).keys()}
                        bundle_schedule = [m for m in (bundle_schedule or []) if str(m.get('subjectGroupId') or m.get('sgId') or '') in allowed_sgids]
                    result['subjectgroup_api_details'] = bundle
                    if not sg_map:
                        for sgid, item in (bundle or {}).items():
                            norm = (item or {}).get('normalized') or {}
                            if norm:
                                sg_map[str(sgid)] = norm
                        if sg_map:
                            subjects, classes, terms = subjectgroup_options_from_map(sg_map)
                            result['subjectgroups'] = subjectgroup_rows_from_map(sg_map)
                            result['parsed']['subjectgroups'] = {'count': len(result['subjectgroups']), 'subjects': subjects[:40], 'classes': classes[:40], 'terms': terms[:20]}
                            _sg_map_cache[login_val] = sg_map
                    else:
                        # Если sg_map уже был отфильтрован, синхронизируем details с ним.
                        allowed_sgids = {str(x) for x in (sg_map or {}).keys()}
                        result['subjectgroup_api_details'] = {str(k): v for k, v in (bundle or {}).items() if str(k) in allowed_sgids}
            except Exception as e:
                result['parsed']['class_journal_dashboard_error'] = f'{type(e).__name__}: {e}'
                result['debug'].append('classJournal dashboard error: ' + traceback.format_exc())

            try:
                # Расписание строим только из уже отфильтрованного bundle_schedule,
                # полученного через /webapi/schedule/classmeetings?sgId=... .
                # Общие запросы /webapi/classmeetings?teacherId=... отключены,
                # потому что в СГО они дают 404 и не нужны для teacher-only режима.
                if 'bundle_schedule' in locals() and bundle_schedule:
                    result['schedule'] = parse_classmeetings_schedule(bundle_schedule, sg_map, start, end)
                    result['parsed']['schedule'] = {
                        'days': len(result['schedule']),
                        'lessons': sum(len(d.get('lessons', [])) for d in result['schedule']),
                        'source': '/webapi/schedule/classmeetings?sgId=...',
                        'sample': result['schedule'][:3],
                    }
                else:
                    result['schedule'] = []
                    result['parsed']['schedule'] = {
                        'days': 0,
                        'lessons': 0,
                        'source': 'skipped: no teacher-owned sgId/schedule returned',
                    }
            except Exception as e:
                result['parsed']['schedule_error'] = f'{type(e).__name__}: {e}'
                result['debug'].append('schedule error: ' + traceback.format_exc())

            try:
                # Раздел «Журнал» удалён из lkteacher. Не опрашиваем /webapi/journals,
                # чтобы не получать серию 404. Состав классов/учеников берётся
                # ниже из subjectgroup_api_details (studentList + schedule).
                result['journal'] = {'found': False, 'message': 'Раздел журнала отключён; данные учеников берутся из studentList по sgId.'}
                result['parsed']['journal'] = {'found': False, 'source': 'disabled'}
            except Exception as e:
                result['parsed']['journal_error'] = f'{type(e).__name__}: {e}'
                result['debug'].append('journal skip error: ' + traceback.format_exc())

            try:
                detail_limit = 6 if debug_probe else 0
                sg_journal_details, sg_j_debug = fetch_subjectgroup_journal_details(sess, sg_map, start, end, limit=detail_limit)
                result['debug'].extend(sg_j_debug[:40])
                for sgid, item in (result.get('subjectgroup_api_details') or {}).items():
                    jd = sg_journal_details.setdefault(str(sgid), {})
                    if item.get('students') and not jd.get('students'):
                        jd['students'] = item.get('students')
                        jd['found'] = True
                    if item.get('students_count') is not None:
                        jd['students_count'] = item.get('students_count')
                    if item.get('schedule_count') is not None:
                        jd['schedule_count'] = item.get('schedule_count')
                    norm = item.get('normalized') or {}
                    if norm:
                        jd['class_name'] = jd.get('class_name') or norm.get('class_name') or ''
                        jd['subject'] = jd.get('subject') or norm.get('subject') or ''
                result['subjectgroup_details'] = build_subjectgroup_catalog(sg_map, sg_journal_details)
                result['parsed']['subjectgroup_details'] = {
                    'rows': len(result['subjectgroup_details'].get('rows') or []),
                    'classes': len(result['subjectgroup_details'].get('classes') or []),
                    'staff': len(result['subjectgroup_details'].get('staff') or []),
                    'with_students': sum(1 for r in (result['subjectgroup_details'].get('rows') or []) if r.get('students_count')),
                }
            except Exception as e:
                result['subjectgroup_details'] = build_subjectgroup_catalog(sg_map, {}) if sg_map else {'rows': [], 'classes': [], 'subjects': [], 'staff': []}
                result['parsed']['subjectgroup_details_error'] = f'{type(e).__name__}: {e}'
                result['debug'].append('subjectgroup details error: ' + traceback.format_exc())

            try:
                # Финально объединяем педсостав из subjectgroups/classJournal и /webapi/rooms.
                # Так на вкладке «Пед. состав» отображаются все преподаватели школы,
                # а не только учитель текущего логина.
                result['staff_catalog'] = merge_staff_catalogs(result.get('staff_teachers_catalog') or [], result.get('staff_catalog') or [], result.get('rooms_staff_catalog') or [])
                result['parsed']['all_staff'] = {
                    'staff': len(result.get('staff_catalog') or []),
                    'staff_teachers': len(result.get('staff_teachers_catalog') or []),
                    'rooms_staff': len(result.get('rooms_staff_catalog') or []),
                    'source': 'staff/teachers + subjectgroups/classJournal + rooms'
                }
            except Exception:
                result['debug'].append('final staff catalog merge error: ' + traceback.format_exc())

            try:
                result['workload'] = build_teacher_workload(result.get('subjectgroups') or [], result.get('schedule') or [], result.get('journal') or {})
                result['parsed']['workload'] = {'classes_count': result['workload'].get('classes_count', 0), 'subjects_count': result['workload'].get('subjects_count', 0), 'pairs_count': len(result['workload'].get('pairs') or [])}
            except Exception:
                result['workload'] = {}
                result['debug'].append('workload build error: ' + traceback.format_exc())

            await safe_teacher_close(ns)
            return jsonify(result)
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                await safe_teacher_close(ns)
            result['success'] = False
            result['error'] = str(e)
            result['traceback'] = traceback.format_exc()
            return jsonify(result), 500

    return run_async(_fetch())



# ================== LKTEACHER SUBJECT CARD: расписание + ученики + средние оценки ==================
def _num_average(values):
    nums = []
    for v in values or []:
        text = str(v).replace(',', '.').strip()
        m = re.search(r'-?\d+(?:\.\d+)?', text)
        if m:
            try:
                nums.append(float(m.group(0)))
            except Exception:
                pass
    return round(sum(nums) / len(nums), 2) if nums else None

def parse_subjectcard_journal_payload(payload, sgid='', start='', end=''):
    """Возвращает учеников и средние оценки для карточки предмета из ответа /webapi/journals."""
    journals = payload.get('journals') if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    if not isinstance(journals, list):
        journals = []
    chosen = None
    sgid = str(sgid or '').strip()
    for j in journals:
        if not isinstance(j, dict):
            continue
        jid = str(first_nonempty(j.get('subjectGroupId'), j.get('sgId'), j.get('SGID')) or '').strip()
        if not sgid or jid == sgid:
            chosen = j
            break
    if not chosen:
        return {'students': [], 'averages': [], 'columns': [], 'marks': []}

    students = []
    student_by_id = {}
    for st in chosen.get('students') or []:
        if not isinstance(st, dict):
            continue
        sid = str(first_nonempty(st.get('id'), st.get('studentId'), st.get('personId')) or '').strip()
        name = str(first_nonempty(st.get('fullName'), st.get('name'), st.get('fio')) or '').strip()
        if sid and name:
            students.append({'id': sid, 'fullName': name})
            student_by_id[sid] = name

    # Даты работ через assignments -> classMeeting
    meeting_date = {}
    for cm in chosen.get('classMeeting') or chosen.get('classMeetings') or []:
        if isinstance(cm, dict):
            mid = str(first_nonempty(cm.get('id'), cm.get('classMeetingId'), cm.get('classmeetingId')) or '')
            m = re.search(r'\d{4}-\d{2}-\d{2}', str(cm.get('date') or cm.get('day') or ''))
            if mid and m:
                meeting_date[mid] = m.group(0)
    assignment_date = {}
    columns = []
    for a in chosen.get('assignments') or []:
        if not isinstance(a, dict):
            continue
        aid = str(first_nonempty(a.get('id'), a.get('assignmentId')) or '').strip()
        cmid = str(first_nonempty(a.get('classMeetingId'), a.get('classmeetingId')) or '').strip()
        d = meeting_date.get(cmid, '')
        if aid:
            assignment_date[aid] = d
            title = str(first_nonempty(a.get('assignmentName'), a.get('name'), a.get('title')) or 'Работа').strip()
            columns.append({'id': aid, 'date': d, 'title': title, 'classMeetingId': cmid})

    marks_by_student = {}
    marks_out = []
    for mobj in chosen.get('marks') or []:
        if not isinstance(mobj, dict):
            continue
        mark = first_nonempty(mobj.get('mark'), mobj.get('value'), mobj.get('markValue'))
        if mark in (None, ''):
            continue
        sid = str(first_nonempty(mobj.get('studentId'), mobj.get('pupilId')) or '').strip()
        aid = str(first_nonempty(mobj.get('assignmentId'), mobj.get('workId')) or '').strip()
        d = assignment_date.get(aid, '')
        if start and d and d < start:
            continue
        if end and d and d > end:
            continue
        marks_by_student.setdefault(sid, []).append(mark)
        marks_out.append({'studentId': sid, 'assignmentId': aid, 'mark': str(mark), 'date': d})

    averages = []
    # Сначала реальные average из СГО, если есть
    for src_key in ('averages', 'averageMarks', 'studentAverages', 'results'):
        for item in chosen.get(src_key) or []:
            if isinstance(item, dict):
                sid = str(first_nonempty(item.get('studentId'), item.get('id'), item.get('pupilId')) or '').strip()
                avg = first_nonempty(item.get('average'), item.get('avg'), item.get('value'))
                if sid and avg not in (None, ''):
                    try:
                        avg = round(float(str(avg).replace(',', '.')), 2)
                    except Exception:
                        pass
                    averages.append({'studentId': sid, 'average': avg})
    if not averages:
        for st in students:
            avg = _num_average(marks_by_student.get(str(st.get('id')), []))
            if avg is not None:
                averages.append({'studentId': st.get('id'), 'average': avg})

    return {'students': students, 'averages': averages, 'columns': columns, 'marks': marks_out}

def normalize_subjectcard_schedule(cm_data, sgid='', start='', end=''):
    out = []
    sgid = str(sgid or '').strip()
    for entry in cm_data or []:
        if not isinstance(entry, dict):
            continue

        entry_sgid = first_nonempty(
            entry.get('subjectGroupId'), entry.get('sgId'), entry.get('subjectgroupId'),
            (entry.get('subjectGroup') or {}).get('id') if isinstance(entry.get('subjectGroup'), dict) else None
        )
        entry_sgid = str(entry_sgid or '').strip()
        # /webapi/schedule/classmeetings уже запрашивается с sgId. В некоторых ответах
        # subjectGroupId не приходит совсем, поэтому нельзя отбрасывать такие строки.
        if sgid and entry_sgid and entry_sgid != sgid:
            continue

        day_raw = str(first_nonempty(
            entry.get('day'), entry.get('date'), entry.get('lessonDate'),
            entry.get('start'), entry.get('startTime'), entry.get('begin'), entry.get('beginTime')
        ) or '')
        m = re.search(r'\d{4}-\d{2}-\d{2}', day_raw)
        d = m.group(0) if m else ''
        if start and d and d < start:
            continue
        if end and d and d > end:
            continue

        room_obj = entry.get('room') if isinstance(entry.get('room'), dict) else {}
        lesson_obj = entry.get('lesson') if isinstance(entry.get('lesson'), dict) else {}
        time_obj = entry.get('time') if isinstance(entry.get('time'), dict) else {}
        class_obj = entry.get('class') if isinstance(entry.get('class'), dict) else {}

        out.append({
            'id': entry.get('id'),
            'day': d or day_raw,
            'date': d or day_raw,
            'relay': entry.get('relay'),
            'number': first_nonempty(entry.get('number'), entry.get('lessonNumber'), entry.get('lessonNo'), entry.get('num')),
            'scheduleTimeId': entry.get('scheduleTimeId'),
            'subjectGroupId': entry_sgid or sgid,
            'lessonId': first_nonempty(entry.get('lessonId'), lesson_obj.get('id')),
            'room': entry.get('room') or {'name': first_nonempty(entry.get('roomName'), entry.get('room'), room_obj.get('name'), room_obj.get('number'))},
            'lesson': entry.get('lesson') or ({
                'id': entry.get('lessonId'),
                'name': first_nonempty(entry.get('lessonName'), entry.get('subject'), entry.get('theme'), lesson_obj.get('name'), lesson_obj.get('title'))
            } if (entry.get('lessonId') or entry.get('lessonName') or entry.get('subject') or entry.get('theme') or lesson_obj) else None),
            'time': entry.get('time') or {
                'start': first_nonempty(entry.get('start'), entry.get('startTime'), entry.get('begin'), entry.get('beginTime'), time_obj.get('start')),
                'end': first_nonempty(entry.get('end'), entry.get('endTime'), entry.get('finish'), entry.get('finishTime'), time_obj.get('end'))
            },
            'class_name': first_nonempty(entry.get('className'), entry.get('class_name'), class_obj.get('name'), class_obj.get('displayName')),
            'className': first_nonempty(entry.get('className'), entry.get('class_name'), class_obj.get('name'), class_obj.get('displayName')),
            'teacherId': entry.get('teacherId') or entry.get('teacherIds') or [],
            'isChangingCm': bool(entry.get('isChangingCm', False)),
        })
    out.sort(key=lambda x: (str(x.get('day') or ''), int(str(x.get('number') or '0').split('/')[0]) if str(x.get('number') or '0').split('/')[0].isdigit() else 0))
    return out

@app.route('/api/lkteacher/subjectcard', methods=['GET', 'POST', 'OPTIONS'])
def lkteacher_subjectcard_api():
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    data = request.get_json(silent=True) or request.form or request.args or {}
    login_val = (data.get('login') or '').strip()
    password_val = (data.get('password') or '').strip()
    school = get_requested_school(data) or STAFF_SCHOOL
    sgid = str(data.get('sgId') or data.get('subjectGroupId') or data.get('subject') or '').strip()
    start = (data.get('start') or '').strip() or (moscow_now() - timedelta(days=7)).strftime('%Y-%m-%d')
    end = (data.get('end') or '').strip() or (moscow_now() + timedelta(days=30)).strftime('%Y-%m-%d')
    if not login_val or not password_val:
        return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400
    if not sgid:
        return jsonify({'success': False, 'error': 'Не передан sgId предметной группы'}), 400

    async def _fetch():
        ns = None
        result = {'success': True, 'sgId': sgid, 'period': {'start': start, 'end': end}, 'schedule': [], 'students': [], 'averages': [], 'subject': {}, 'debug': []}
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            actual_school = await asyncio.wait_for(sgo_login_with_fallback(ns, login_val, password_val, school, allow_teacher=True), timeout=15)
            cookies, at_token = get_cookies_from_ns(ns)
            if not at_token and cookies:
                at_token = fetch_at_token_from_sgo(cookies)
            sess = sgo_session_from_cookies(cookies, at_token=at_token)
            result['school'] = actual_school
            result['debug'].append(f'at-token present: {bool(at_token)}')

            sg_map = _sg_map_cache.get(login_val) or {}
            if not sg_map:
                sg_data, sg_debug = fetch_sgo_subjectgroups(sess)
                result['debug'].extend(sg_debug[:20])
                if sg_data is not None:
                    sg_map, _, _, _ = parse_subjectgroups_array(sg_data)
                    _sg_map_cache[login_val] = sg_map
            result['subject'] = sg_map.get(sgid) or {}

            detail, detail_err = fetch_sgo_subjectgroup_detail(sess, sgid)
            if detail is not None:
                result['subject'] = {**(result.get('subject') or {}), **normalize_subjectgroup_detail(sgid, detail)}
                result['debug'].append(f'GET /webapi/subjectgroups/{sgid} -> JSON OK')
            else:
                result['debug'].append(f'GET /webapi/subjectgroups/{sgid} -> {detail_err}')

            students_data, students_err = fetch_sgo_student_list(sess, sgid)
            if students_data is not None:
                result['students'] = [{
                    'id': x.get('id'),
                    'studentId': x.get('studentId') or x.get('id'),
                    'idAliases': x.get('idAliases') or [x.get('id')],
                    'fullName': x.get('name'),
                    'name': x.get('name')
                } for x in normalize_students_list(students_data)]
                result['debug'].append(f'GET /webapi/grade/studentList?sgId={sgid} -> JSON OK ({len(result["students"])} students)')
            else:
                result['debug'].append(f'GET /webapi/grade/studentList?sgId={sgid} -> {students_err}')

            # Средние оценки временно не запрашиваем: карточка предмета сейчас
            # строится только из subjectgroups/{id}, studentList и schedule/classmeetings.
            avg_data, avg_err = fetch_sgo_average_marks(sess, [sgid])
            if avg_data is not None:
                result['averages'] = normalize_average_marks(avg_data)
                result['debug'].append(f'POST /webapi/v2/average-marks sgId={sgid} -> JSON OK ({len(result["averages"])} averages)')
                result['debug'].append('average studentId sample: ' + ', '.join(str(x.get('studentId')) for x in result['averages'][:5]))
            else:
                result['averages'] = []
                result['debug'].append(f'POST /webapi/v2/average-marks sgId={sgid} -> {avg_err}')

            cm_data, cm_err = fetch_sgo_schedule_by_sgid(sess, sgid, start, end)
            if cm_data is not None:
                arr = cm_data if isinstance(cm_data, list) else (
                    (cm_data.get('items') or cm_data.get('data') or cm_data.get('classMeetings') or [])
                    if isinstance(cm_data, dict) else []
                )
                result['schedule'] = normalize_subjectcard_schedule(arr, sgid, start, end)
                result['debug'].append(f'GET /webapi/schedule/classmeetings?sgId={sgid} -> JSON OK ({len(result["schedule"])} lessons)')
            else:
                result['debug'].append(f'GET /webapi/schedule/classmeetings?sgId={sgid} -> {cm_err}')

            j_data, j_debug = fetch_sgo_journal_511(sess, sgid, start, end)
            result['debug'].extend(j_debug[:40])
            if j_data is not None:
                jp = parse_subjectcard_journal_payload(j_data, sgid, start, end)
                if jp.get('students'):
                    # Журнал может вернуть оценки/колонки, но список учеников и средние оценки
                    # оставляем из точных endpoint-ов studentList и average-marks.
                    result['columns'] = jp.get('columns') or []
                    result['marks'] = jp.get('marks') or []
                    if not result.get('students'):
                        result['students'] = jp.get('students') or []
                    if not result.get('averages'):
                        result['averages'] = jp.get('averages') or []
                else:
                    if not result.get('averages'):
                        result['averages'] = jp.get('averages') or []
                    result['columns'] = jp.get('columns') or []
                    result['marks'] = jp.get('marks') or []
            else:
                result['journal_message'] = 'Журнал по выбранной предметной группе не вернул JSON.'

            await safe_teacher_close(ns)
            return jsonify(result)
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                await safe_teacher_close(ns)
            result['success'] = False
            result['error'] = str(e)
            result['traceback'] = traceback.format_exc()
            return jsonify(result), 200
    return run_async(_fetch())

# ================== NETSCHOOL EXTRA STUDENT METHODS ==================
# Реализация методов netschoolapi для lk.html:
#   .overdue(start, end), .attachments(assignment), .announcements(), .school(), download_attachment*

def _date_from_api(value, fallback=None):
    if not value:
        return fallback
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except Exception:
        return fallback


def _safe_jsonable(value, _seen=None, _depth=0):
    """Безопасно приводит объект netschoolapi к JSON-friendly виду без бесконечной рекурсии."""
    if _seen is None:
        _seen = set()

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, datetime):
        return value.isoformat()
    try:
        from datetime import date as _date
        if isinstance(value, _date):
            return value.isoformat()
    except Exception:
        pass

    if _depth > 3:
        return str(value)[:500]

    obj_id = id(value)
    if obj_id in _seen:
        return None
    _seen.add(obj_id)

    if isinstance(value, dict):
        return {
            str(k): _safe_jsonable(v, _seen, _depth + 1)
            for k, v in value.items()
            if not str(k).startswith('_')
        }

    if isinstance(value, (list, tuple, set)):
        return [_safe_jsonable(v, _seen, _depth + 1) for v in list(value)[:100]]

    try:
        from dataclasses import fields, is_dataclass
        if is_dataclass(value):
            return {
                f.name: _safe_jsonable(getattr(value, f.name, None), _seen, _depth + 1)
                for f in fields(value)
                if not f.name.startswith('_')
            }
    except Exception:
        pass

    source = {}
    try:
        source = dict(vars(value))
    except Exception:
        source = {}

    if not source:
        for name in (
            'id', 'assignmentId', 'workId', 'name', 'title', 'content', 'text',
            'description', 'type', 'typeId', 'mark', 'date', 'dueDate', 'subject',
            'subjectName', 'attachments', 'files'
        ):
            try:
                attr = getattr(value, name, None)
            except Exception:
                attr = None
            if attr is not None:
                source[name] = attr

    data = {}
    for k, v in source.items():
        if str(k).startswith('_') or callable(v):
            continue
        data[str(k)] = _safe_jsonable(v, _seen, _depth + 1)

    return data if data else str(value)[:500]
def _assignment_to_api(assign):
    raw = _safe_jsonable(assign)
    title = first_nonempty(
        deep_get(assign, 'assignmentName', 'name', 'title', 'theme', 'topic'),
        assignment_content(assign),
        'Задание'
    )
    return {
        'id': str(get_assignment_id(assign) or first_nonempty(deep_get(assign, 'assignmentId', 'workId'), '')),
        'title': clean_html_text(title),
        'content': clean_html_text(assignment_content(assign)),
        'type': clean_html_text(assignment_type_name(assign) or ''),
        'type_id': assignment_type_id(assign),
        'mark': assignment_mark_value(assign),
        'subject': clean_html_text(first_nonempty(deep_get(assign, 'subjectName', 'subject', 'disciplineName'), '')),
        'date': str(first_nonempty(deep_get(assign, 'date', 'day', 'dueDate', 'deadline', 'finishDate'), '')),
        'raw': raw,
    }


def _attachment_to_api(att):
    raw = _safe_jsonable(att)
    att_id = first_nonempty(
        deep_get(att, 'id', 'attachmentId', 'fileId', 'resourceId', 'attachId'),
        deep_get(raw, 'id', 'attachmentId', 'fileId', 'resourceId', 'attachId')
    )
    name = first_nonempty(
        deep_get(att, 'name', 'filename', 'fileName', 'title', 'originalFileName', 'displayName'),
        deep_get(raw, 'name', 'filename', 'fileName', 'title', 'originalFileName', 'displayName'),
        'Файл'
    )
    url = first_nonempty(
        deep_get(att, 'url', 'downloadUrl', 'href', 'link', 'fileUrl', 'path'),
        deep_get(raw, 'url', 'downloadUrl', 'href', 'link', 'fileUrl', 'path')
    )
    return {
        'id': str(att_id or ''),
        'name': clean_html_text(name),
        'filename': clean_html_text(name),
        'url': url or '',
        'size': first_nonempty(deep_get(att, 'size', 'fileSize', 'length'), deep_get(raw, 'size', 'fileSize', 'length'), ''),
        'raw': raw,
    }




async def _attachments_for_assignment(ns, assign, cookies_dict=None, at_token=None):
    """Возвращает вложения задания.

    Важно: в актуальном netschoolapi метод attachments в реальности принимает
    ID задания (assign.id), хотя в руководстве он описан как attachments(Assignment).
    Поэтому сначала берём assignment_id и вызываем ns.attachments(assignment_id).
    """
    assign_id = first_nonempty(
        get_assignment_id(assign),
        deep_get(assign, 'id', 'assignmentId', 'assignment_id', 'workId', 'work_id'),
        deep_get(_safe_jsonable(assign), 'id', 'assignmentId', 'assignment_id', 'workId', 'work_id'),
    )
    out = []

    # Официальный метод netschoolapi. Для версий, где метод принимает объект,
    # оставлен fallback ниже, но основной рабочий вариант — ID задания.
    if hasattr(ns, 'attachments'):
        if assign_id:
            try:
                raw_items = await ns.attachments(int(assign_id))
                out = [_attachment_to_api(x) for x in (raw_items or [])]
            except Exception as e:
                print(f"attachments({assign_id}) error: {e}")
        if not out:
            try:
                raw_items = await ns.attachments(assign)
                out = [_attachment_to_api(x) for x in (raw_items or [])]
            except Exception as e:
                print(f"attachments(Assignment) fallback error for {assign_id}: {e}")

    # Иногда в JSON задания уже есть массивы attachments/files.
    if not out:
        out = _extract_attachments_from_any(assign)

    # Последний fallback — прямой вызов webapi как в netschoolapi:
    # POST /webapi/student/diary/get-attachments?studentId=... {assignId:[id]}
    if not out and assign_id:
        out = fetch_assignment_attachments_sync(assign_id, cookies_dict, at_token)

    return out

def _looks_like_attachment_dict(value):
    if not isinstance(value, dict):
        return False
    keys = {str(k).lower() for k in value.keys()}
    name_keys = {'name', 'filename', 'file_name', 'filename', 'title', 'originalfilename', 'displayname'}
    id_keys = {'id', 'attachmentid', 'fileid', 'resourceid', 'attachid'}
    url_keys = {'url', 'downloadurl', 'href', 'link', 'fileurl', 'path'}
    return bool(keys & name_keys) and bool(keys & (id_keys | url_keys))


def _extract_attachments_from_any(value, limit=30):
    found = []
    seen = set()

    def add(item):
        api = _attachment_to_api(item)
        key = (api.get('id') or '', api.get('url') or '', api.get('filename') or api.get('name') or '')
        if key in seen:
            return
        seen.add(key)
        found.append(api)

    def walk(obj, depth=0):
        if obj is None or depth > 7 or len(found) >= limit:
            return
        if isinstance(obj, dict):
            for k in ('attachments', 'files', 'resources', 'materials', 'attachedFiles', 'assignmentAttachments'):
                v = obj.get(k)
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict) and _looks_like_attachment_dict(item):
                            add(item)
                        else:
                            walk(item, depth + 1)
                elif isinstance(v, dict):
                    if _looks_like_attachment_dict(v):
                        add(v)
                    else:
                        walk(v, depth + 1)
            if _looks_like_attachment_dict(obj):
                add(obj)
                return
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, (list, tuple, set)):
            for item in obj:
                walk(item, depth + 1)
        else:
            raw = _safe_jsonable(obj)
            if isinstance(raw, (dict, list, tuple)):
                walk(raw, depth + 1)

    walk(value)
    return found


def fetch_assignment_attachments_sync(assignment_id, cookies_dict, at_token=None):
    if not assignment_id:
        return []
    headers = {
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://sgo.volganet.ru/app/school/studentdiary/',
        'User-Agent': 'Mozilla/5.0',
        'X-Requested-With': 'XMLHttpRequest',
    }
    if at_token:
        headers['at'] = at_token
    base = SGO_URL.rstrip('/')
    # Основной endpoint, который использует netschoolapi.attachments().
    try:
        student_id = None
        try:
            # Если есть at-token/cookies, studentId можно получить из init.
            init_url = f'{base}/webapi/student/diary/init'
            init_resp = requests.get(init_url, cookies=cookies_dict or {}, headers=headers, verify=False, timeout=8)
            if init_resp.status_code == 200:
                init_json = init_resp.json()
                cur = init_json.get('currentStudentId')
                students = init_json.get('students') or {}
                if cur is not None and str(cur) in {str(k) for k in students.keys()}:
                    student = students.get(cur) or students.get(str(cur)) or {}
                    student_id = student.get('studentId')
                if not student_id and isinstance(students, dict) and students:
                    student = next(iter(students.values())) or {}
                    student_id = student.get('studentId')
        except Exception as e:
            print(f'Attachments init studentId error: {e}')

        if student_id:
            url = f'{base}/webapi/student/diary/get-attachments'
            resp = requests.post(url, params={'studentId': student_id}, json={'assignId': [int(assignment_id)]}, cookies=cookies_dict or {}, headers=headers, verify=False, timeout=8)
            if resp.status_code == 200:
                payload = resp.json()
                data = payload[0].get('attachments', []) if isinstance(payload, list) and payload else payload
                items = [_attachment_to_api(x) for x in (data or [])]
                if items:
                    print(f'Attachments official endpoint found {len(items)} files for assignment {assignment_id}')
                    return items
            else:
                print(f'Attachments official endpoint HTTP {resp.status_code}: {resp.text[:160]}')
    except Exception as e:
        print(f'Attachments official endpoint error for assignment {assignment_id}: {e}')

    candidates = [
        f'{base}/webapi/assignments/{assignment_id}',
        f'{base}/webapi/student/diary/assignments/{assignment_id}',
        f'{base}/webapi/studentDiary/assignments/{assignment_id}',
        f'{base}/webapi/studentdiary/assignments/{assignment_id}',
        f'{base}/webapi/student/diary/assignInfo/{assignment_id}',
        f'{base}/webapi/studentDiary/assignInfo/{assignment_id}',
        f'{base}/webapi/studentdiary/assignInfo/{assignment_id}',
    ]
    for url in candidates:
        try:
            resp = requests.get(url, cookies=cookies_dict or {}, headers=headers, verify=False, timeout=8)
            if resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except Exception:
                continue
            items = _extract_attachments_from_any(payload)
            if items:
                print(f'Attachments fallback found {len(items)} files for assignment {assignment_id} via {url}')
                return items
        except Exception as e:
            print(f'Attachments fallback error for assignment {assignment_id}: {e}')
    return []


def _absolute_sgo_url(url):
    url = str(url or '').strip()
    if not url:
        return ''
    if url.startswith('//'):
        return 'https:' + url
    if url.startswith('http://') or url.startswith('https://'):
        return url
    if url.startswith('/'):
        return SGO_URL.rstrip('/') + url
    return SGO_URL.rstrip('/') + '/' + url.lstrip('/')


def _announcement_author_name(item, raw):
    author = deep_get(item, 'author') or (raw.get('author') if isinstance(raw, dict) else None)
    author_raw = _safe_jsonable(author)
    name = first_nonempty(
        deep_get(author, 'nickname', 'nickName'),
        deep_get(author, 'full_name', 'fullName', 'name'),
        author_raw.get('nickname') if isinstance(author_raw, dict) else None,
        author_raw.get('nickName') if isinstance(author_raw, dict) else None,
        author_raw.get('full_name') if isinstance(author_raw, dict) else None,
        author_raw.get('fullName') if isinstance(author_raw, dict) else None,
        author_raw.get('name') if isinstance(author_raw, dict) else None,
        deep_get(item, 'authorName', 'senderName'),
        deep_get(raw, 'authorName', 'senderName'),
    )
    if not name:
        text = str(author or '')
        match = re.search(r"nickname=['\"]([^'\"]+)['\"]", text) or re.search(r"full_name=['\"]([^'\"]+)['\"]", text)
        name = match.group(1) if match else text
    return clean_html_text(name or 'СГО')

def _announcement_to_api(item):
    raw = _safe_jsonable(item)
    if not isinstance(raw, dict):
        raw = {'value': raw}
    return {
        'title': clean_html_text(first_nonempty(deep_get(item, 'title', 'name', 'subject'), deep_get(raw, 'title', 'name', 'subject'), 'Объявление')),
        'content': clean_html_text(first_nonempty(deep_get(item, 'content', 'text', 'body', 'message', 'description'), deep_get(raw, 'content', 'text', 'body', 'message', 'description'), '')),
        'date': str(first_nonempty(deep_get(item, 'date', 'createdAt', 'publishDate'), deep_get(raw, 'date', 'createdAt', 'publishDate'), '')),
        'author': _announcement_author_name(item, raw),
        'raw': raw,
    }


def _school_to_api(item):
    raw = _safe_jsonable(item)
    if not isinstance(raw, dict):
        raw = {'value': raw}
    return {
        'name': clean_html_text(first_nonempty(deep_get(item, 'name', 'schoolName', 'title'), raw.get('name'), raw.get('schoolName'), raw.get('title'), '')),
        'address': clean_html_text(first_nonempty(deep_get(item, 'address'), raw.get('address'), '')),
        'phone': clean_html_text(first_nonempty(deep_get(item, 'phone', 'phones'), raw.get('phone'), raw.get('phones'), '')),
        'email': clean_html_text(first_nonempty(deep_get(item, 'email', 'mail'), raw.get('email'), raw.get('mail'), '')),
        'raw': raw,
    }


def _request_payload():
    return (request.get_json(silent=True) or {}) if request.method == 'POST' else dict(request.args)


@app.route('/api/overdue', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/overdue/', methods=['GET', 'POST', 'OPTIONS'])
def api_overdue():
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    data = _request_payload()
    login_val = (data.get('login') or '').strip()
    password_val = (data.get('password') or '').strip()
    start = data.get('start')
    end = data.get('end')
    school = get_requested_school(data)
    if not login_val or not password_val:
        return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400

    async def _fetch():
        ns = None
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            actual_school = await sgo_login_with_fallback(ns, login_val, password_val, school)
            start_dt = _date_from_api(start)
            end_dt = _date_from_api(end)
            kwargs = {}
            if start_dt:
                kwargs['start'] = start_dt
            if end_dt:
                kwargs['end'] = end_dt
            items = await ns.overdue(**kwargs)
            try:
                await ns.logout()
            except Exception:
                pass
            return jsonify({'success': True, 'data': [_assignment_to_api(x) for x in (items or [])], 'school': actual_school})
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            return jsonify({'success': False, 'error': str(e), 'data': []}), 200
    return run_async(_fetch())


@app.route('/api/school', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/school/', methods=['GET', 'POST', 'OPTIONS'])
def api_school_info():
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    data = _request_payload()
    login_val = (data.get('login') or '').strip()
    password_val = (data.get('password') or '').strip()
    school = get_requested_school(data)
    if not login_val or not password_val:
        return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400

    async def _fetch():
        ns = None
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            actual_school = await sgo_login_with_fallback(ns, login_val, password_val, school)
            info = await ns.school()
            try:
                await ns.logout()
            except Exception:
                pass
            out = _school_to_api(info)
            if not out.get('name'):
                out['name'] = actual_school
            return jsonify({'success': True, 'data': out, 'school': actual_school})
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            return jsonify({'success': False, 'error': str(e), 'data': {'name': school}}), 200
    return run_async(_fetch())


@app.route('/api/sgo/announcements', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/sgo/announcements/', methods=['GET', 'POST', 'OPTIONS'])
def api_sgo_announcements():
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    data = _request_payload()
    login_val = (data.get('login') or '').strip()
    password_val = (data.get('password') or '').strip()
    school = get_requested_school(data)
    if not login_val or not password_val:
        return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400

    async def _fetch():
        ns = None
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            actual_school = await sgo_login_with_fallback(ns, login_val, password_val, school)
            items = await ns.announcements()
            try:
                await ns.logout()
            except Exception:
                pass
            data_items = [_announcement_to_api(x) for x in (items or [])]
            data_items = [x for x in data_items if not _is_local_demo_announcement(x)]
            return jsonify({'success': True, 'data': data_items, 'school': actual_school})
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            return jsonify({'success': False, 'error': str(e), 'data': []}), 200
    return run_async(_fetch())


@app.route('/api/attachments', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/attachments/', methods=['GET', 'POST', 'OPTIONS'])
def api_attachments():
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    data = _request_payload()
    login_val = (data.get('login') or '').strip()
    password_val = (data.get('password') or '').strip()
    school = get_requested_school(data)
    assignment_raw = data.get('assignment') or data.get('raw') or {}
    if not login_val or not password_val:
        return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400
    if not assignment_raw:
        return jsonify({'success': False, 'error': 'Не передано задание assignment'}), 400

    async def _fetch():
        ns = None
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            actual_school = await sgo_login_with_fallback(ns, login_val, password_val, school)
            cookies_dict, at_token = get_cookies_from_ns(ns)
            out = await _attachments_for_assignment(ns, assignment_raw, cookies_dict, at_token)
            try:
                await ns.logout()
            except Exception:
                pass
            return jsonify({'success': True, 'data': out, 'school': actual_school})
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            return jsonify({'success': False, 'error': str(e), 'data': []}), 200
    return run_async(_fetch())


@app.route('/api/download_attachment', methods=['POST', 'OPTIONS'])
@app.route('/api/download_attachment/', methods=['POST', 'OPTIONS'])
def api_download_attachment():
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    data = request.get_json(silent=True) or {}
    login_val = (data.get('login') or '').strip()
    password_val = (data.get('password') or '').strip()
    school = get_requested_school(data)
    attachment_raw = data.get('attachment') or data.get('raw') or {}
    if not login_val or not password_val:
        return jsonify({'success': False, 'error': 'Не переданы логин или пароль'}), 400
    if not attachment_raw:
        return jsonify({'success': False, 'error': 'Не передано вложение attachment'}), 400

    async def _fetch():
        ns = None
        try:
            require_netschoolapi()
            ns = NetSchoolAPI(SGO_URL)
            await sgo_login_with_fallback(ns, login_val, password_val, school)
            # Возвращаем base64, чтобы фронт мог скачать файл без сохранения на сервере.
            import base64
            content = None
            download_error = None
            att_id = first_nonempty(
                deep_get(attachment_raw, 'id', 'attachmentId', 'fileId', 'resourceId', 'attachId'),
                deep_get(attachment_raw, 'raw', 'id'),
                deep_get(attachment_raw, 'raw', 'attachmentId'),
            )
            try:
                # netschoolapi 5.x/11.x: instance method download_attachment(id, BytesIO)
                if att_id and hasattr(ns, 'download_attachment'):
                    from io import BytesIO
                    buf = BytesIO()
                    await ns.download_attachment(int(att_id), buf)
                    content = buf.getvalue()
                elif netschoolapi_client and hasattr(netschoolapi_client, 'download_attachment_as_bytes'):
                    content = await netschoolapi_client.download_attachment_as_bytes(ns, attachment_raw)
            except Exception as e:
                download_error = e
                print(f"download_attachment error, trying direct url: {e}")

            if content is None:
                cookies_dict, at_token = get_cookies_from_ns(ns)
                headers = {
                    'Accept': '*/*',
                    'Referer': 'https://sgo.volganet.ru/app/school/studentdiary/',
                    'User-Agent': 'Mozilla/5.0',
                    'X-Requested-With': 'XMLHttpRequest',
                }
                if at_token:
                    headers['at'] = at_token
                url = _absolute_sgo_url(first_nonempty(
                    deep_get(attachment_raw, 'url', 'downloadUrl', 'href', 'link', 'fileUrl', 'path'),
                    deep_get(attachment_raw, 'raw', 'url'),
                    deep_get(attachment_raw, 'raw', 'downloadUrl'),
                    deep_get(attachment_raw, 'raw', 'href'),
                    deep_get(attachment_raw, 'raw', 'link')
                ))
                if not url and att_id:
                    url = SGO_URL.rstrip('/') + f'/webapi/attachments/{int(att_id)}'
                if url:
                    resp = requests.get(url, cookies=cookies_dict or {}, headers=headers, verify=False, timeout=20)
                    if resp.status_code == 200:
                        content = resp.content
                    else:
                        raise RuntimeError(f'СГО не отдал файл по ссылке: HTTP {resp.status_code}') from download_error
                else:
                    raise RuntimeError('Во вложении нет id или ссылки для скачивания. ' + (str(download_error) if download_error else ''))

            name = first_nonempty(deep_get(attachment_raw, 'name', 'filename', 'fileName', 'title'), deep_get(attachment_raw, 'raw', 'name'), deep_get(attachment_raw, 'raw', 'fileName'), 'attachment.bin')
            try:
                await ns.logout()
            except Exception:
                pass
            return jsonify({'success': True, 'filename': clean_html_text(name), 'base64': base64.b64encode(content or b'').decode('ascii')})
        except Exception as e:
            safe_print_traceback()
            if ns is not None:
                try:
                    await ns.logout()
                except Exception:
                    pass
            return jsonify({'success': False, 'error': str(e)}), 200
    return run_async(_fetch())


# ================== SITE ADMIN PANEL ==================
ALLOWED_ADMIN_PAGES = {'warnings', 'news', 'gallery', 'information'}

def ensure_admin_tables():
    """Создаёт/обновляет таблицы админской панели при любом обращении к её API.
    Это убирает ошибку SQLite `no such table`, если база school.db уже существовала старой версии.
    """
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS site_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT DEFAULT '',
                    author TEXT DEFAULT '',
                    file_path TEXT DEFAULT '',
                    file_name TEXT DEFAULT '',
                    file_type TEXT DEFAULT '',
                    files_json TEXT DEFAULT '[]',
                    likes INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                )''')
    columns = [row[1] for row in db.execute("PRAGMA table_info(site_posts)").fetchall()]
    migrations = {
        'author': "ALTER TABLE site_posts ADD COLUMN author TEXT DEFAULT ''",
        'file_path': "ALTER TABLE site_posts ADD COLUMN file_path TEXT DEFAULT ''",
        'file_name': "ALTER TABLE site_posts ADD COLUMN file_name TEXT DEFAULT ''",
        'file_type': "ALTER TABLE site_posts ADD COLUMN file_type TEXT DEFAULT ''",
        'files_json': "ALTER TABLE site_posts ADD COLUMN files_json TEXT DEFAULT '[]'",
        'likes': "ALTER TABLE site_posts ADD COLUMN likes INTEGER DEFAULT 0",
    }
    for name, sql in migrations.items():
        if name not in columns:
            db.execute(sql)
    db.execute('''CREATE TABLE IF NOT EXISTS schedule_overrides (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    class_name TEXT NOT NULL,
                    day TEXT NOT NULL,
                    lesson_number TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                    UNIQUE(class_name, day, lesson_number)
                )''')
    db.commit()

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

def _save_uploaded_files(files):
    saved = []
    for f in files or []:
        if not f or not getattr(f, 'filename', ''):
            continue
        original = secure_filename(f.filename)
        if not original:
            continue
        ext = os.path.splitext(original)[1].lower()
        filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
        f.save(os.path.join(UPLOAD_DIR, filename))
        saved.append({'path': f'uploads/{filename}', 'name': f.filename, 'type': getattr(f, 'mimetype', '') or ''})
    return saved

def _post_to_dict(row):
    d = dict(row)
    try:
        files = json.loads(d.get('files_json') or '[]')
    except Exception:
        files = []
    if not files and d.get('file_path'):
        files = [{'path': d.get('file_path'), 'name': d.get('file_name') or 'Файл', 'type': d.get('file_type') or ''}]
    d['files'] = files
    return d


@app.route('/api/admin/health', methods=['GET', 'OPTIONS'])
def api_admin_health():
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    ensure_admin_tables()
    return jsonify({'success': True, 'status': 'ok'})

@app.route('/api/admin/posts', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/admin/posts/', methods=['GET', 'POST', 'OPTIONS'])
def admin_posts_api():
    ensure_admin_tables()
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    db = get_db()
    if request.method == 'GET':
        page = (request.args.get('page') or '').strip()
        where, params = '', []
        if page in ALLOWED_ADMIN_PAGES:
            where, params = 'WHERE page = ?', [page]
        rows = db.execute(f"""SELECT id, page, title, content, author, file_path, file_name, file_type,
                                  COALESCE(files_json, '[]') AS files_json, COALESCE(likes, 0) AS likes, created_at
                           FROM site_posts {where} ORDER BY id DESC""", params).fetchall()
        return jsonify({'success': True, 'posts': [_post_to_dict(r) for r in rows]})
    page = (request.form.get('page') or '').strip()
    if page not in ALLOWED_ADMIN_PAGES:
        return jsonify({'success': False, 'error': 'Неизвестный раздел сайта'}), 400
    title = (request.form.get('title') or '').strip() or ('Фотография' if page == 'gallery' else '')
    content = (request.form.get('content') or '').strip()
    author = (request.form.get('author') or '').strip() or 'Директор'
    if not title:
        return jsonify({'success': False, 'error': 'Введите заголовок или название'}), 400
    files = _save_uploaded_files(request.files.getlist('files') or request.files.getlist('file'))
    first = files[0] if files else {}
    db.execute('''INSERT INTO site_posts (page, title, content, author, file_path, file_name, file_type, files_json, likes)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)''',
               (page, title, content, author, first.get('path',''), first.get('name',''), first.get('type',''), json.dumps(files, ensure_ascii=False)))
    db.commit()
    post_id = db.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    row = db.execute('SELECT *, COALESCE(files_json, "[]") AS files_json, COALESCE(likes, 0) AS likes FROM site_posts WHERE id=?', (post_id,)).fetchone()
    return jsonify({'success': True, 'post': _post_to_dict(row)})

@app.route('/api/site/posts', methods=['GET', 'OPTIONS'])
@app.route('/api/site/posts/', methods=['GET', 'OPTIONS'])
@app.route('/api/posts', methods=['GET', 'OPTIONS'])
@app.route('/api/posts/', methods=['GET', 'OPTIONS'])
def public_site_posts_api():
    """Публичная выдача записей сайта, созданных в lkadmin.html.

    Админка сохраняет записи в site_posts через /api/admin/posts, а страницы
    сайта могут читать их через /api/site/posts или /api/posts.
    """
    ensure_admin_tables()
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    page = (request.args.get('page') or request.args.get('section') or '').strip()
    where, params = '', []
    if page in ALLOWED_ADMIN_PAGES:
        where, params = 'WHERE page = ?', [page]
    rows = get_db().execute(f"""SELECT id, page, title, content, author, file_path, file_name, file_type,
                                      COALESCE(files_json, '[]') AS files_json, COALESCE(likes, 0) AS likes, created_at
                               FROM site_posts {where} ORDER BY id DESC""", params).fetchall()
    posts = [_post_to_dict(r) for r in rows]
    return jsonify({'success': True, 'posts': posts, 'data': posts})

@app.route('/api/admin/posts/<int:post_id>', methods=['DELETE', 'OPTIONS'])
@app.route('/api/admin/posts/<int:post_id>/', methods=['DELETE', 'OPTIONS'])
def admin_post_delete_api(post_id):
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    db = get_db()
    db.execute('DELETE FROM site_posts WHERE id=?', (post_id,))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/admin/gallery/<int:post_id>/like', methods=['POST', 'OPTIONS'])
@app.route('/api/gallery/<int:post_id>/like', methods=['POST', 'OPTIONS'])
def gallery_like_api(post_id):
    ensure_admin_tables()
    if request.method == 'OPTIONS':
        return jsonify({'success': True})

    data = request.get_json(silent=True) or {}
    action = (data.get('action') or 'toggle').strip()

    db = get_db()
    row = db.execute("SELECT id, COALESCE(likes, 0) AS likes FROM site_posts WHERE id=? AND page='gallery'", (post_id,)).fetchone()
    if not row:
        return jsonify({'success': False, 'error': 'Фото не найдено'}), 404

    likes = int(row['likes'] or 0)
    if action == 'unlike':
        likes = max(0, likes - 1)
    else:
        likes += 1

    db.execute("UPDATE site_posts SET likes=? WHERE id=? AND page='gallery'", (likes, post_id))
    db.commit()
    return jsonify({'success': True, 'likes': likes})



def ensure_schedule_overrides_v2():
    """Хранит правки расписания по каждому предмету внутри одного номера урока."""
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS schedule_overrides_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    class_name TEXT NOT NULL,
                    day TEXT NOT NULL,
                    lesson_number TEXT NOT NULL,
                    sub_index INTEGER NOT NULL DEFAULT 0,
                    subject TEXT NOT NULL,
                    room TEXT DEFAULT '',
                    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                    UNIQUE(class_name, day, lesson_number, sub_index)
                )""")
    try:
        old_rows = db.execute('SELECT class_name, day, lesson_number, subject, updated_at FROM schedule_overrides').fetchall()
        for row in old_rows:
            db.execute("""INSERT OR IGNORE INTO schedule_overrides_v2
                          (class_name, day, lesson_number, sub_index, subject, room, updated_at)
                          VALUES (?, ?, ?, 0, ?, '', COALESCE(?, datetime('now', 'localtime')))""",
                       (row['class_name'], row['day'], row['lesson_number'], row['subject'], row['updated_at']))
    except Exception:
        pass
    db.commit()

@app.route('/api/admin/schedule', methods=['GET', 'POST', 'OPTIONS'])
def admin_schedule_api():
    ensure_admin_tables()
    ensure_schedule_overrides_v2()
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    db = get_db()
    if request.method == 'GET':
        rows = db.execute('''SELECT id, class_name, day, lesson_number, sub_index, subject, room, updated_at
                             FROM schedule_overrides_v2 ORDER BY id DESC''').fetchall()
        return jsonify({'success': True, 'overrides': [dict(r) for r in rows]})

    data = request.get_json(silent=True) or request.form
    class_name = (data.get('class_name') or data.get('class') or '').strip()
    day = (data.get('day') or '').strip()
    lesson_number = str(data.get('lesson_number') or data.get('lesson') or '').strip().rstrip('.')
    try:
        sub_index = int(data.get('sub_index') or 0)
    except Exception:
        sub_index = 0
    subject = (data.get('subject') or '').strip()
    room = (data.get('room') or '').strip()

    if not class_name or not day or not lesson_number or not subject:
        return jsonify({'success': False, 'error': 'Заполните класс, день, номер урока и новый предмет'}), 400

    db.execute('''INSERT INTO schedule_overrides_v2
                  (class_name, day, lesson_number, sub_index, subject, room, updated_at)
                  VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                  ON CONFLICT(class_name, day, lesson_number, sub_index)
                  DO UPDATE SET subject=excluded.subject,
                                room=excluded.room,
                                updated_at=datetime('now', 'localtime')''',
               (class_name, day, lesson_number, sub_index, subject, room))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/site/schedule', methods=['GET', 'OPTIONS'])
@app.route('/api/site/schedule/', methods=['GET', 'OPTIONS'])
@app.route('/api/schedule/overrides', methods=['GET', 'OPTIONS'])
def schedule_overrides_api():
    ensure_admin_tables()
    ensure_schedule_overrides_v2()
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    rows = get_db().execute('''SELECT class_name, day, lesson_number, sub_index, subject, room, updated_at
                               FROM schedule_overrides_v2''').fetchall()
    return jsonify({'success': True, 'overrides': [dict(r) for r in rows]})




# ================== ADMIN SCHEDULE EDITING ==================
def _normalize_schedule_room(value):
    value = (value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value

def _validate_schedule_payload(data):
    class_name = (data.get("class_name") or data.get("className") or data.get("class") or "").strip()
    day = (data.get("day") or data.get("weekday") or "").strip()
    lesson_number = str(data.get("lesson_number") or data.get("lessonNumber") or data.get("lesson") or "").strip()
    subject = (data.get("subject") or data.get("lesson_name") or data.get("lessonName") or "").strip()
    room = _normalize_schedule_room(data.get("room") or data.get("cabinet") or "")

    if not class_name:
        return None, "Не указан класс"
    if not day:
        return None, "Не указан день"
    if not lesson_number:
        return None, "Не указан номер урока"

    if not subject:
        subject = "---"
        room = ""

    if subject == "---":
        room = ""
    elif not re.fullmatch(r"каб\. \d+", room):
        return None, "Кабинет должен быть указан строго в формате: каб. X"

    return {
        "class_name": class_name,
        "day": day,
        "lesson_number": lesson_number,
        "subject": subject,
        "room": room
    }, ""

@app.route("/api/admin/schedule", methods=["GET", "POST", "DELETE", "OPTIONS"])
def api_admin_schedule():
    if request.method == "OPTIONS":
        return jsonify({"success": True})

    db = get_db()

    if request.method == "GET":
        rows = db.execute("""
            SELECT class_name, day, lesson_number, subject, COALESCE(room, '') AS room, updated_at
            FROM schedule_overrides
            ORDER BY class_name, day, CAST(lesson_number AS INTEGER)
        """).fetchall()
        return jsonify({
            "success": True,
            "overrides": [dict(row) for row in rows]
        })

    data = request.get_json(silent=True) or {}

    if request.method == "DELETE":
        payload, error = _validate_schedule_payload(data)
        if not payload:
            return jsonify({"success": False, "error": error}), 400

        db.execute("""
            DELETE FROM schedule_overrides
            WHERE class_name = ? AND day = ? AND lesson_number = ?
        """, (payload["class_name"], payload["day"], payload["lesson_number"]))
        db.commit()
        return jsonify({"success": True, "deleted": True})

    payload, error = _validate_schedule_payload(data)
    if not payload:
        return jsonify({"success": False, "error": error}), 400

    db.execute("""
        INSERT INTO schedule_overrides (class_name, day, lesson_number, subject, room, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
        ON CONFLICT(class_name, day, lesson_number)
        DO UPDATE SET
            subject = excluded.subject,
            room = excluded.room,
            updated_at = datetime('now', 'localtime')
    """, (
        payload["class_name"],
        payload["day"],
        payload["lesson_number"],
        payload["subject"],
        payload["room"],
    ))
    db.commit()

    return jsonify({
        "success": True,
        "override": payload
    })
# ================== /ADMIN SCHEDULE EDITING ==================

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

def is_sgo_auth_exception(exc):
    text = str(exc or "")
    return (
        exc.__class__.__name__ in ("AuthError", "HTTPStatusError")
        or "409 Conflict" in text
        or "СГО отклонил вход" in text
        or "/webapi/login" in text
    )
