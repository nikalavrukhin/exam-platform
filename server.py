# ============================================================================
#  server.py — бэкенд сайта для сдачи контрольных работ.
#
#  Что он делает:
#    1. отдаёт странице настройки (заголовок, список задач, галочки);
#    2. принимает работы учеников и складывает их в базу SQLite;
#    3. пускает админа по паролю и показывает ему сданные работы;
#    4. проверяет Python-код на синтаксические ошибки (подчёркивание в поле кода).
#
#  Никаких внешних библиотек не нужно — только то, что идёт вместе с Python.
#  Сам HTML-сайт (index.html) отдаёт nginx, а сюда приходят только запросы /api/...
# ============================================================================

import ast          # модуль, который умеет «разбирать» Python-код в дерево — нужен для поиска синтаксических ошибок
import hmac         # из него берём compare_digest — безопасное сравнение паролей
import json         # перевод между Python-словарями и JSON-текстом (так общаются браузер и сервер)
import os           # доступ к переменным окружения и к папкам
import secrets      # генерация случайных токенов, которые нельзя угадать
import sqlite3      # встроенная база данных (один файл, ставить ничего не надо)
import threading    # Lock — «замок», чтобы потоки не портили общие данные одновременно
import time         # текущее время (для токенов и блокировки)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # готовый простой HTTP-сервер

# ---------------------------------------------------------------- настройки

# Путь к файлу базы. Берётся из переменной окружения DB, а если её нет — /data/app.db
DB_PATH = os.environ.get("DB", "/data/app.db")
# Пароль админа. Берётся из ADMIN_PASSWORD, обязательна, без неё сервер не стартует
PASSWORD = os.environ["ADMIN_PASSWORD"]
# Порт, на котором сервер слушает запросы (nginx пересылает сюда /api/)
PORT = int(os.environ.get("PORT", "8000"))
# Максимальный размер тела запроса — 1 МБ (защита от огромных запросов)
MAX_BODY = 1_000_000
# Сколько живёт вход админа: 12 часов в секундах
TOKEN_TTL = 12 * 3600

# Настройки сайта по умолчанию — используются, пока админ ничего не сохранил
DEFAULT_CONFIG = {
    "title": "Контрольная работа",   # заголовок страницы
    "autoindent": True,               # галочка «Автоотступ» включена
    "syntax": False,                  # галочка «Подсвечивать ошибки» выключена
    "editable": True,                 # галочка «Можно исправлять и отзывать работу» включена
    "tasks": [{"title": "Задача 1", "text": ""}],  # одна задача без условия
}

# Замок: пока один поток что-то делает с tokens/fails, остальные ждут
lock = threading.Lock()
# Выданные админам токены: {токен: время_когда_протухнет}. Хранятся в памяти — при перезапуске сервера админ входит заново
tokens = {}
# Неудачные попытки входа: {ip: (сколько_ошибок, до_какого_времени_заблокирован)}
fails = {}


# ---------------------------------------------------------------- база данных

def db():
    """Открывает соединение с базой."""
    con = sqlite3.connect(DB_PATH)   # подключаемся к файлу базы (создаётся сам, если нет)
    con.row_factory = sqlite3.Row    # чтобы к колонкам можно было обращаться по имени: row["first"]
    return con                       # отдаём соединение


def init_db():
    """Создаёт папку и таблицы при первом запуске."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)  # создаём папку под базу, если её ещё нет
    with db() as con:                                     # with — сам сохранит изменения (commit) в конце
        # Таблица настроек: пары «ключ — значение». У нас один ключ 'config' с JSON настроек
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        # Таблица работ: id, имя, фамилия, время сдачи (секунды) и ответы (JSON-текст)
        con.execute(
            "CREATE TABLE IF NOT EXISTS submissions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, first TEXT, last TEXT, "
            "created INTEGER, answers TEXT)"
        )
        # Новые колонки для исправления/отзыва работ. Если база старая (без них) — добавляем, данные остаются целыми
        cols = {r[1] for r in con.execute("PRAGMA table_info(submissions)")}   # какие колонки уже есть
        for name, ddl in (("token", "TEXT"),                   # секретный токен ученика (по нему он находит свою работу)
                          ("updated", "INTEGER"),              # время последнего изменения
                          ("status", "TEXT DEFAULT 'sent'"),   # 'sent' — отправлена, 'withdrawn' — отозвана
                          ("history", "TEXT DEFAULT '[]'")):   # история изменений (JSON-список событий)
            if name not in cols:
                con.execute(f"ALTER TABLE submissions ADD COLUMN {name} {ddl}")


def get_config():
    """Достаёт настройки сайта из базы (или берёт значения по умолчанию)."""
    with db() as con:
        # Ищем строку с ключом 'config'
        row = con.execute("SELECT value FROM settings WHERE key='config'").fetchone()
    # Если строка есть — разбираем JSON, если нет — копия настроек по умолчанию
    cfg = json.loads(row["value"]) if row else dict(DEFAULT_CONFIG)
    # Совместимость со старой версией сайта, где был «размер отступа» вместо галочки:
    if "autoindent" not in cfg:
        cfg["autoindent"] = cfg.get("indent", 4) > 0   # отступ > 0 значит галочка включена
    cfg.setdefault("syntax", False)   # если ключа syntax нет — ставим «выключено»
    cfg.setdefault("editable", True)  # если ключа editable нет (старая версия) — исправлять можно
    cfg.pop("indent", None)           # старый ключ indent больше не нужен — выкидываем
    return cfg


def clean_config(data):
    """Проверяет и чистит настройки, которые прислал админ, прежде чем их сохранить."""
    # Заголовок: в строку, без пробелов по краям, максимум 120 символов; пустой — заменяем на стандартный
    title = str(data.get("title", "")).strip()[:120] or DEFAULT_CONFIG["title"]
    tasks = []                                    # сюда соберём очищенные задачи
    for t in data.get("tasks", [])[:30]:          # не больше 30 задач
        if not isinstance(t, dict):               # если элемент не «словарь» — пропускаем мусор
            continue
        tasks.append({
            "title": str(t.get("title", "")).strip()[:120] or "Задача",  # название задачи (пустое → «Задача»)
            "text": str(t.get("text", ""))[:5000],                        # условие, максимум 5000 символов
        })
    if not tasks:                                 # если задач не осталось совсем —
        tasks = DEFAULT_CONFIG["tasks"]           # возвращаем одну стандартную, чтобы страница не была пустой
    return {
        "title": title,
        "autoindent": bool(data.get("autoindent", True)),  # галочку приводим к True/False
        "syntax": bool(data.get("syntax", False)),
        "editable": bool(data.get("editable", True)),
        "tasks": tasks,
    }


def check_syntax(code):
    """Проверяет Python-код на синтаксические ошибки. Код НЕ запускается, только разбирается."""
    try:
        # PyCF_ONLY_AST = «только разобрать в дерево, не выполнять». Если код кривой — вылетит SyntaxError
        compile(code, "<code>", "exec", flags=ast.PyCF_ONLY_AST)
    except SyntaxError as e:
        # Возвращаем, где ошибка: строка, колонка, где она заканчивается, и текст ошибки.
        # getattr(..., None) нужен, потому что в старых версиях Python end_lineno/end_offset нет
        return {"line": e.lineno, "col": e.offset, "end_line": getattr(e, "end_lineno", None),
                "end_col": getattr(e, "end_offset", None), "msg": e.msg}
    except (RecursionError, MemoryError, ValueError):
        pass   # странный код (слишком глубокий, с нулевыми байтами) — просто молча считаем, что ошибки нет
    return None   # None = ошибок нет


# ---------------------------------------------------------------- помощники для работ

def read_names(data):
    """Достаёт из запроса имя и фамилию (без пробелов по краям, не длиннее 100 символов)."""
    return str(data.get("first", "")).strip()[:100], str(data.get("last", "")).strip()[:100]


def build_answers(cfg, given):
    """Превращает присланный список кодов в список {название задачи, код}. Если прислали не список — вернёт None."""
    if not isinstance(given, list):
        return None
    answers = []
    for i, task in enumerate(cfg["tasks"]):                # идём по задачам из настроек (а не по присланному!)
        code = given[i] if i < len(given) else ""          # код к i-й задаче, а если не прислали — пустая строка
        # Сохраняем вместе с названием задачи (на случай, если админ потом её переименует), код режем до 50000
        answers.append({"title": task["title"], "code": str(code)[:50000]})
    return answers


def find_by_token(con, token):
    """Ищет работу по секретному токену ученика. Токен — 32 символа; всё остальное сразу отбрасываем."""
    if not isinstance(token, str) or len(token) != 32:
        return None
    return con.execute("SELECT * FROM submissions WHERE token=?", (token,)).fetchone()


def history_of(row):
    """История изменений работы. У работ, которые ещё не менялись, она пуста — тогда создаём первую запись «Сдана»."""
    history = json.loads(row["history"] or "[]")
    if not history:
        history = [{"t": row["created"], "type": "sent", "first": row["first"],
                    "last": row["last"], "answers": json.loads(row["answers"])}]
    return history


def trim_history(history):
    """Не даём истории расти бесконечно: оставляем первую запись и последние 59."""
    return history[:1] + history[-59:] if len(history) > 60 else history


def admin_view(r):
    """Как работа выглядит для админа: ответы, статус, число исправлений и история изменений. Токена тут нет!"""
    history = json.loads(r["history"] or "[]")
    return {"id": r["id"], "first": r["first"], "last": r["last"], "created": r["created"],
            "updated": r["updated"] or r["created"],                       # время последнего изменения (или сдачи)
            "status": r["status"] or "sent",
            # сколько раз ученик исправлял работу (отозвать и сдать заново тоже считается исправлением)
            "edits": sum(1 for e in history if e.get("type") in ("edit", "resend")),
            "answers": json.loads(r["answers"]),
            "history": history if len(history) > 1 else []}              # если менялось — отдаём всю историю


def mine_view(r):
    """Как работа выглядит для самого ученика: то же самое, но без id и без истории."""
    v = admin_view(r)
    v.pop("id")
    v.pop("history")
    return v


# ---------------------------------------------------------------- обработка запросов

class Handler(BaseHTTPRequestHandler):
    """Каждый входящий запрос обрабатывается методами этого класса: do_GET, do_POST, do_DELETE."""

    server_version = "nginx"   # что сервер сообщает о себе в заголовке (чтобы не светить «Python»)
    sys_version = ""           # версию Python тоже скрываем

    def log_message(self, *args):
        pass   # отключаем болтливые логи каждого запроса

    def ip(self):
        # nginx передаёт настоящий IP посетителя в заголовке X-Real-IP; если его нет — берём адрес соединения
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def send_json(self, code, obj):
        """Отправляет браузеру ответ в виде JSON с нужным статус-кодом (200 — ок, 401 — нет доступа и т.д.)."""
        body = json.dumps(obj, ensure_ascii=False).encode()   # словарь → JSON-текст → байты (кириллица остаётся читаемой)
        self.send_response(code)                                # первая строка ответа: статус
        self.send_header("Content-Type", "application/json; charset=utf-8")  # говорим, что это JSON в UTF-8
        self.send_header("Content-Length", str(len(body)))      # длина ответа в байтах
        self.send_header("Cache-Control", "no-store")           # просим браузер ничего не кэшировать
        self.end_headers()                                      # конец заголовков
        self.wfile.write(body)                                  # само тело ответа

    def read_json(self):
        """Читает тело запроса и превращает JSON в словарь. Если что-то не так — возвращает None."""
        try:
            n = int(self.headers.get("Content-Length", "0"))   # сколько байт прислал браузер
        except ValueError:
            n = 0                                               # если там не число — считаем, что тела нет
        if n <= 0 or n > MAX_BODY:                              # пусто или слишком много — отказ
            return None
        try:
            data = json.loads(self.rfile.read(n))               # читаем ровно n байт и разбираем как JSON
        except ValueError:
            return None                                         # сломанный JSON — отказ
        return data if isinstance(data, dict) else None         # принимаем только «словарь», не список и не строку

    def is_admin(self):
        """Проверяет, что запрос пришёл от вошедшего админа (по токену в заголовке Authorization)."""
        auth = self.headers.get("Authorization", "")                       # заголовок вида «Bearer abc123...»
        token = auth[7:] if auth.startswith("Bearer ") else ""             # отрезаем слово «Bearer » (7 символов)
        with lock:                                                         # берём замок на время работы с tokens
            exp = tokens.get(token)                                        # когда токен протухнет (или None)
            if exp and exp > time.time():                                  # токен есть и ещё не просрочен
                return True
            tokens.pop(token, None)                                        # просроченный/левый токен выбрасываем
        return False

    def route(self):
        # Берём путь без параметров (?...) и без слэша в конце: "/api/config/?x=1" → "/api/config"
        return self.path.split("?", 1)[0].rstrip("/")

    # -------- GET: «дай мне данные» --------
    def do_GET(self):
        path = self.route()
        if path == "/api/config":                      # страница просит настройки
            return self.send_json(200, get_config())   # отдаём всем (ученикам они нужны, чтобы нарисовать форму)
        if path == "/api/admin/submissions":           # админ просит список сданных работ
            if not self.is_admin():                    # а он точно админ?
                return self.send_json(401, {"error": "auth"})   # нет → 401 «не авторизован»
            with db() as con:
                # Берём все работы, самые свежие сверху
                rows = con.execute("SELECT * FROM submissions ORDER BY id DESC").fetchall()
            return self.send_json(200, [admin_view(r) for r in rows])   # каждую работу превращаем в понятный админу словарь
        self.send_json(404, {"error": "not found"})    # любой другой адрес — 404

    # -------- POST: «вот тебе данные, обработай» --------
    def do_POST(self):
        path = self.route()
        data = self.read_json()                        # читаем присланный JSON
        if data is None:                               # не получилось прочитать
            return self.send_json(400, {"error": "bad request"})   # 400 «плохой запрос»

        # --- ученик отправляет работу ---
        if path == "/api/submit":
            first, last = read_names(data)
            if not first or not last:                          # если что-то пустое —
                return self.send_json(400, {"error": "Укажите имя и фамилию"})
            answers = build_answers(get_config(), data.get("answers", []))
            if answers is None:                                # ответы должны быть списком
                return self.send_json(400, {"error": "bad request"})
            token = secrets.token_hex(16)                      # секретный токен из 32 символов — ученик сохранит его в браузере
            now = int(time.time())
            with db() as con:
                con.execute(
                    # Знаки ? подставляются значениями ниже — так нельзя «сломать» базу хитрым текстом (защита от SQL-инъекций)
                    "INSERT INTO submissions (first, last, created, answers, token, updated, status, history) "
                    "VALUES (?,?,?,?,?,?,'sent','[]')",
                    (first, last, now, json.dumps(answers, ensure_ascii=False), token, now),
                )
            return self.send_json(200, {"ok": True, "token": token})   # токен отдаём только один раз, ученику

        # --- ученик открыл сайт снова и спрашивает: «а есть моя работа?» ---
        if path == "/api/mine":
            with db() as con:
                row = find_by_token(con, data.get("token"))
            if not row:
                return self.send_json(404, {"error": "not found"})
            return self.send_json(200, mine_view(row))

        # --- ученик исправляет свою работу ---
        if path == "/api/update":
            cfg = get_config()
            if not cfg["editable"]:                            # админ выключил исправление
                return self.send_json(403, {"error": "Исправлять работы сейчас нельзя"})
            first, last = read_names(data)
            answers = build_answers(cfg, data.get("answers", []))
            if not first or not last or answers is None:
                return self.send_json(400, {"error": "Укажите имя и фамилию"})
            with db() as con:
                row = find_by_token(con, data.get("token"))
                if not row:
                    return self.send_json(404, {"error": "not found"})
                was_withdrawn = row["status"] == "withdrawn"   # работа была отозвана?
                # Изменилось ли что-то по сути (имя, фамилия или код)?
                changed = (first, last, answers) != (row["first"], row["last"], json.loads(row["answers"]))
                if not changed and not was_withdrawn:          # ничего не поменялось — ничего не записываем
                    return self.send_json(200, {"ok": True, "changed": False})
                now = int(time.time())
                history = history_of(row)                      # история (с первой записью «Сдана», если её ещё нет)
                # Новая запись: «исправлено» или «сдана заново» (если до этого работа была отозвана) + снимок новой версии
                history.append({"t": now, "type": "resend" if was_withdrawn else "edit",
                                "first": first, "last": last, "answers": answers})
                con.execute(
                    "UPDATE submissions SET first=?, last=?, answers=?, status='sent', updated=?, history=? WHERE id=?",
                    (first, last, json.dumps(answers, ensure_ascii=False), now,
                     json.dumps(trim_history(history), ensure_ascii=False), row["id"]),
                )
            return self.send_json(200, {"ok": True, "changed": True})

        # --- ученик отзывает свою работу ---
        if path == "/api/withdraw":
            if not get_config()["editable"]:
                return self.send_json(403, {"error": "Отзывать работы сейчас нельзя"})
            with db() as con:
                row = find_by_token(con, data.get("token"))
                if not row:
                    return self.send_json(404, {"error": "not found"})
                if row["status"] != "withdrawn":               # если уже отозвана — ничего не делаем
                    now = int(time.time())
                    history = history_of(row)
                    history.append({"t": now, "type": "withdraw"})   # запись «отозвана» (без снимка кода)
                    con.execute(
                        "UPDATE submissions SET status='withdrawn', updated=?, history=? WHERE id=?",
                        (now, json.dumps(trim_history(history), ensure_ascii=False), row["id"]),
                    )
            return self.send_json(200, {"ok": True})

        # --- проверка синтаксиса кода (подчёркивание ошибок) ---
        if path == "/api/check":
            if not get_config()["syntax"]:                      # если админ выключил подсветку —
                return self.send_json(200, {"error": None})     # всегда отвечаем «ошибок нет»
            # Проверяем код (максимум 50000 символов) и отдаём найденную ошибку или None
            return self.send_json(200, {"error": check_syntax(str(data.get("code", ""))[:50000])})

        # --- вход админа ---
        if path == "/api/admin/login":
            ip = self.ip()                                      # кто пытается войти
            now = time.time()
            with lock:
                count, until = fails.get(ip, (0, 0))            # сколько раз он уже ошибся и до когда заблокирован
                if until > now:                                 # блокировка ещё действует
                    return self.send_json(429, {"error": "Слишком много попыток, подождите"})   # 429 «слишком часто»
            # Сравниваем пароли «в постоянное время» — так по скорости ответа нельзя угадать пароль по буквам
            ok = hmac.compare_digest(str(data.get("password", "")).encode(), PASSWORD.encode())
            with lock:
                if not ok:                                      # пароль неверный
                    count += 1                                  # +1 ошибка
                    # После 5-й ошибки блокируем на 300 секунд (5 минут)
                    fails[ip] = (count, now + 300 if count >= 5 else 0)
                    return self.send_json(403, {"error": "Неверный пароль"})   # 403 «запрещено»
                fails.pop(ip, None)                             # вошёл правильно — забываем прошлые ошибки
                token = secrets.token_hex(24)                   # случайный токен из 48 символов
                tokens[token] = now + TOKEN_TTL                 # запоминаем его и срок жизни
            return self.send_json(200, {"token": token})        # отдаём токен браузеру, он будет слать его дальше

        # --- админ сохраняет настройки сайта ---
        if path == "/api/admin/config":
            if not self.is_admin():
                return self.send_json(401, {"error": "auth"})
            cfg = clean_config(data)                            # чистим то, что пришло
            with db() as con:
                con.execute(
                    # «Вставь строку, а если ключ config уже есть — обнови значение»
                    "INSERT INTO settings (key, value) VALUES ('config', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(cfg, ensure_ascii=False),),
                )
            return self.send_json(200, cfg)                     # возвращаем сохранённый вариант

        self.send_json(404, {"error": "not found"})             # неизвестный адрес

    # -------- DELETE: «удали это» --------
    def do_DELETE(self):
        path = self.route()
        if path.startswith("/api/admin/submissions/"):          # адрес вида /api/admin/submissions/15
            if not self.is_admin():
                return self.send_json(401, {"error": "auth"})
            try:
                sid = int(path.rsplit("/", 1)[1])               # берём число после последнего «/» — это id работы
            except ValueError:
                return self.send_json(400, {"error": "bad id"}) # не число — ошибка
            with db() as con:
                con.execute("DELETE FROM submissions WHERE id=?", (sid,))   # удаляем работу из базы
            return self.send_json(200, {"ok": True})
        self.send_json(404, {"error": "not found"})


# ---------------------------------------------------------------- запуск

if __name__ == "__main__":   # этот блок выполняется, только когда файл запущен напрямую (python server.py)
    init_db()                # создаём таблицы, если их нет
    # Запускаем сервер на всех адресах (0.0.0.0) и порту PORT; Threading = каждый запрос в своём потоке
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()   # и крутимся вечно
