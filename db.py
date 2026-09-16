"""
Работа с базой данных (SQLite).
Все функции синхронные — для такого размера бота этого достаточно.
"""
import os
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager
from dateutil.relativedelta import relativedelta

DB_PATH = os.getenv("DB_PATH", "gym.db")
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                phone TEXT,
                group_type TEXT NOT NULL DEFAULT 'adult',
                birth_date TEXT,
                note TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                approved INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS freeze_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                days_requested INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_at TEXT
            );

            CREATE TABLE IF NOT EXISTS pause_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                action TEXT NOT NULL DEFAULT 'pause',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_at TEXT
            );

            CREATE TABLE IF NOT EXISTS activation_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                requested_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_at TEXT
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                visits_total INTEGER,
                visits_left INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                notified_expiring INTEGER NOT NULL DEFAULT 0,
                last_reminder_date TEXT,
                unlimited INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS trainings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start_at TEXT NOT NULL,
                max_participants INTEGER NOT NULL DEFAULT 20
            );

            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                training_id INTEGER NOT NULL REFERENCES trainings(id) ON DELETE CASCADE,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                UNIQUE(training_id, client_id)
            );

            CREATE TABLE IF NOT EXISTS announcement (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                text TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_of_week INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                title TEXT NOT NULL,
                group_type TEXT NOT NULL DEFAULT 'all',
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                visit_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(client_id, visit_date)
            );
            """
        )
        # Миграция: если база уже существовала без новых колонок — добавляем их
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(clients)")]
        if "group_type" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN group_type TEXT NOT NULL DEFAULT 'adult'")
        if "birth_date" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN birth_date TEXT")
        if "note" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN note TEXT")
        if "belt" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN belt TEXT")
        if "stripes" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN stripes INTEGER NOT NULL DEFAULT 0")
        if "belt_updated" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN belt_updated TEXT")
        if "archived" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "approved" not in cols:
            # у всех, кто зарегистрировался до включения модерации, доступ остаётся
            conn.execute("ALTER TABLE clients ADD COLUMN approved INTEGER NOT NULL DEFAULT 1")

        # Раньше на telegram_id стояло UNIQUE — один аккаунт мог держать только
        # одну карточку. Родителю двоих детей этого не хватает, поэтому
        # пересобираем таблицу без этого ограничения (id сохраняются,
        # поэтому связи с оплатами и посещениями не рвутся).
        has_unique = any(
            row["origin"] == "u"
            for row in conn.execute("PRAGMA index_list(clients)")
        )
        if has_unique:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript(
                """
                CREATE TABLE clients_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    full_name TEXT NOT NULL,
                    phone TEXT,
                    group_type TEXT NOT NULL DEFAULT 'adult',
                    birth_date TEXT,
                    note TEXT,
                    belt TEXT,
                    stripes INTEGER NOT NULL DEFAULT 0,
                    belt_updated TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    approved INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                INSERT INTO clients_new
                    (id, telegram_id, full_name, phone, group_type, birth_date,
                     note, belt, stripes, belt_updated, archived, approved, created_at)
                SELECT id, telegram_id, full_name, phone, group_type, birth_date,
                       note, belt, stripes, belt_updated, archived, 1, created_at
                FROM clients;
                DROP TABLE clients;
                ALTER TABLE clients_new RENAME TO clients;
                """
            )
            conn.execute("PRAGMA foreign_keys = ON")

        pause_cols = [row["name"] for row in conn.execute("PRAGMA table_info(pause_requests)")]
        if pause_cols and "action" not in pause_cols:
            conn.execute("ALTER TABLE pause_requests ADD COLUMN action TEXT NOT NULL DEFAULT 'pause'")

        sub_cols = [row["name"] for row in conn.execute("PRAGMA table_info(subscriptions)")]
        if "last_reminder_date" not in sub_cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN last_reminder_date TEXT")
        if "amount" not in sub_cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN amount REAL")
        if "unlimited" not in sub_cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN unlimited INTEGER NOT NULL DEFAULT 0")


# ---------- Клиенты ----------

def create_client(telegram_id: int, full_name: str, group_type: str = "adult",
                   birth_date: str = None, phone: str = None) -> int:
    """
    Создаёт новую карточку. Один Telegram-аккаунт может держать несколько
    карточек — например, родитель заводит профили на двоих детей.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO clients (telegram_id, full_name, phone, group_type, birth_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, full_name, phone, group_type, birth_date, datetime.now().isoformat()),
        )
        return cur.lastrowid


def upsert_client(telegram_id: int, full_name: str, group_type: str = "adult",
                   birth_date: str = None, phone: str = None):
    """Совместимость: обновляет первую карточку аккаунта либо создаёт новую."""
    existing = get_client_by_telegram_id(telegram_id)
    if existing:
        with get_conn() as conn:
            conn.execute(
                "UPDATE clients SET full_name = ?, group_type = ?, birth_date = ? WHERE id = ?",
                (full_name, group_type, birth_date, existing["id"]),
            )
        return existing["id"]
    return create_client(telegram_id, full_name, group_type, birth_date, phone)


def get_client_by_telegram_id(telegram_id: int):
    """Первая активная карточка аккаунта (для приветствия и совместимости)."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM clients WHERE telegram_id = ?
            ORDER BY archived, id LIMIT 1
            """,
            (telegram_id,),
        ).fetchone()


def list_clients_by_telegram_id(telegram_id: int, include_archived: bool = False):
    """Все карточки аккаунта — родитель может вести несколько детей."""
    query = "SELECT * FROM clients WHERE telegram_id = ?"
    if not include_archived:
        query += " AND archived = 0"
    query += " ORDER BY id"
    with get_conn() as conn:
        return conn.execute(query, (telegram_id,)).fetchall()


def client_belongs_to(client_id: int, telegram_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM clients WHERE id = ? AND telegram_id = ?",
            (client_id, telegram_id),
        ).fetchone()
        return row is not None


def get_client_by_id(client_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM clients WHERE id = ?", (client_id,)
        ).fetchone()


# ---------- Настройки ----------

def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


# ---------- Модерация регистраций ----------

def list_pending_clients():
    """Новые регистрации, ожидающие подтверждения тренера."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM clients WHERE approved = 0 AND archived = 0
            ORDER BY created_at
            """
        ).fetchall()


def approve_client(client_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE clients SET approved = 1 WHERE id = ?", (client_id,))


def set_client_group(client_id: int, group_type: str, reset_belt: bool = False):
    """
    Перевод между детской и взрослой группой. Детские и взрослые пояса —
    разные системы, поэтому при переходе пояс обычно сбрасывается,
    чтобы тренер присвоил новый.
    """
    with get_conn() as conn:
        if reset_belt:
            conn.execute(
                """
                UPDATE clients SET group_type = ?, belt = NULL, stripes = 0, belt_updated = NULL
                WHERE id = ?
                """,
                (group_type, client_id),
            )
        else:
            conn.execute(
                "UPDATE clients SET group_type = ? WHERE id = ?", (group_type, client_id)
            )


# ---------- Объявления ----------

def get_announcement() -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT text FROM announcement WHERE id = 1").fetchone()
        return row["text"] if row else ""


def set_announcement(text: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO announcement (id, text, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at
            """,
            (text, datetime.now().isoformat()),
        )


def update_client_name(client_id: int, full_name: str):
    with get_conn() as conn:
        conn.execute("UPDATE clients SET full_name = ? WHERE id = ?", (full_name, client_id))


def update_client_note(client_id: int, note: str):
    with get_conn() as conn:
        conn.execute("UPDATE clients SET note = ? WHERE id = ?", (note, client_id))


def set_client_archived(client_id: int, archived: bool):
    """
    Архив вместо удаления: клиент пропадает из рабочих списков, но вся его
    история (оплаты, посещения, пояс) сохраняется и его можно вернуть.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE clients SET archived = ? WHERE id = ?",
            (1 if archived else 0, client_id),
        )


def delete_client(client_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))


def list_clients_with_subscription(group_type: str = None, archived: bool = False):
    """Для админ-панели мини-аппа: клиенты + их текущий активный абонемент (если есть)."""
    query = """
        SELECT c.id, c.full_name, c.telegram_id, c.group_type, c.birth_date, c.note,
               c.belt, c.stripes, c.belt_updated,
               s.id AS sub_id, s.title AS sub_title, s.end_date AS sub_end_date, s.unlimited,
               s.visits_total, s.visits_left, s.status AS sub_status
        FROM clients c
        LEFT JOIN subscriptions s ON s.id = (
            SELECT id FROM subscriptions
            WHERE client_id = c.id AND status = 'active'
            ORDER BY end_date DESC LIMIT 1
        )
    """
    query += " WHERE c.archived = ? AND c.approved = 1"
    params = [1 if archived else 0]
    if group_type:
        query += " AND c.group_type = ?"
        params.append(group_type)
    query += " ORDER BY c.full_name"
    with get_conn() as conn:
        return conn.execute(query, tuple(params)).fetchall()


# ---------- Заморозка абонемента ----------

def create_freeze_request(client_id: int, days_requested: int):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO freeze_requests (client_id, days_requested, status, created_at)
            VALUES (?, ?, 'pending', ?)
            """,
            (client_id, days_requested, datetime.now().isoformat()),
        )


def has_pending_freeze_request(client_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM freeze_requests WHERE client_id = ? AND status = 'pending' LIMIT 1",
            (client_id,),
        ).fetchone()
        return row is not None


def list_pending_freeze_requests():
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT f.*, c.full_name, c.telegram_id FROM freeze_requests f
            JOIN clients c ON c.id = f.client_id
            WHERE f.status = 'pending'
            ORDER BY f.created_at
            """
        ).fetchall()


def get_freeze_request(request_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM freeze_requests WHERE id = ?", (request_id,)
        ).fetchone()


def decide_freeze_request(request_id: int, approve: bool):
    """
    Если одобрено — сдвигает дату окончания активного абонемента клиента
    на days_requested дней вперёд (клиент "донашивает" пропущенные дни).
    """
    req = get_freeze_request(request_id)
    if not req or req["status"] != "pending":
        return None

    with get_conn() as conn:
        new_status = "approved" if approve else "rejected"
        conn.execute(
            "UPDATE freeze_requests SET status = ?, decided_at = ? WHERE id = ?",
            (new_status, datetime.now().isoformat(), request_id),
        )
        if approve:
            sub = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE client_id = ? AND status = 'active' AND end_date >= ?
                ORDER BY end_date DESC LIMIT 1
                """,
                (req["client_id"], datetime.now().isoformat()),
            ).fetchone()
            if sub:
                new_end = datetime.fromisoformat(sub["end_date"]) + timedelta(days=req["days_requested"])
                conn.execute(
                    "UPDATE subscriptions SET end_date = ? WHERE id = ?",
                    (new_end.isoformat(), sub["id"]),
                )
    return req


# ---------- Приостановка членства ----------

def create_pause_request(client_id: int, action: str = "pause"):
    """action: 'pause' — уйти в архив, 'resume' — вернуться из архива."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pause_requests (client_id, action, status, created_at)
            VALUES (?, ?, 'pending', ?)
            """,
            (client_id, action, datetime.now().isoformat()),
        )


def has_pending_pause_request(client_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM pause_requests WHERE client_id = ? AND status = 'pending' LIMIT 1",
            (client_id,),
        ).fetchone() is not None


def list_pending_pause_requests():
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT p.*, c.full_name, c.telegram_id FROM pause_requests p
            JOIN clients c ON c.id = p.client_id
            WHERE p.status = 'pending'
            ORDER BY p.created_at
            """
        ).fetchall()


def decide_pause_request(request_id: int, approve: bool):
    """Одобрение переносит карточку в архив — членство приостановлено."""
    with get_conn() as conn:
        req = conn.execute(
            "SELECT * FROM pause_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not req or req["status"] != "pending":
            return None
        conn.execute(
            "UPDATE pause_requests SET status = ?, decided_at = ? WHERE id = ?",
            ("approved" if approve else "rejected", datetime.now().isoformat(), request_id),
        )
        if approve:
            archived = 1 if req["action"] == "pause" else 0
            conn.execute(
                "UPDATE clients SET archived = ? WHERE id = ?",
                (archived, req["client_id"]),
            )
    return req


# ---------- Активация абонемента ----------

def create_activation_request(client_id: int, requested_date: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO activation_requests (client_id, requested_date, status, created_at)
            VALUES (?, ?, 'pending', ?)
            """,
            (client_id, requested_date, datetime.now().isoformat()),
        )


def has_pending_activation_request(client_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM activation_requests WHERE client_id = ? AND status = 'pending' LIMIT 1",
            (client_id,),
        ).fetchone()
        return row is not None


def list_pending_activation_requests():
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT a.*, c.full_name, c.telegram_id FROM activation_requests a
            JOIN clients c ON c.id = a.client_id
            WHERE a.status = 'pending'
            ORDER BY a.created_at
            """
        ).fetchall()


def get_activation_request(request_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM activation_requests WHERE id = ?", (request_id,)
        ).fetchone()


def update_activation_request_date(request_id: int, new_date: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE activation_requests SET requested_date = ? WHERE id = ? AND status = 'pending'",
            (new_date, request_id),
        )


def resolve_pending_activation_requests(client_id: int, status: str = "approved"):
    """
    Закрывает необработанные заявки клиента. Нужно, когда тренер отметил оплату
    вручную в карточке — иначе заявка осталась бы висеть во вкладке «Активация»
    и её повторное одобрение создало бы второй абонемент.
    """
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE activation_requests SET status = ?, decided_at = ?
            WHERE client_id = ? AND status = 'pending'
            """,
            (status, datetime.now().isoformat(), client_id),
        )


def decide_activation_request(request_id: int, approve: bool, months: int = 1,
                               amount: float = None):
    """
    Если одобрено — создаёт активный абонемент клиента, начиная с requested_date.
    Если у клиента уже есть действующий абонемент (например, тренер отметил
    оплату вручную), второй не создаётся — заявка просто закрывается.
    """
    req = get_activation_request(request_id)
    if not req or req["status"] != "pending":
        return None

    created = False
    with get_conn() as conn:
        new_status = "approved" if approve else "rejected"
        conn.execute(
            "UPDATE activation_requests SET status = ?, decided_at = ? WHERE id = ?",
            (new_status, datetime.now().isoformat(), request_id),
        )
        if approve:
            existing = conn.execute(
                """
                SELECT 1 FROM subscriptions
                WHERE client_id = ? AND status = 'active' AND end_date >= ?
                LIMIT 1
                """,
                (req["client_id"], datetime.now().isoformat()),
            ).fetchone()
            if not existing:
                start = datetime.fromisoformat(req["requested_date"])
                end = start + relativedelta(months=months)
                conn.execute(
                    """
                    INSERT INTO subscriptions
                        (client_id, title, start_date, end_date, visits_total,
                         visits_left, status, amount)
                    VALUES (?, ?, ?, ?, NULL, NULL, 'active', ?)
                    """,
                    (req["client_id"], "", start.isoformat(), end.isoformat(), amount),
                )
                created = True

    return {"request": req, "created": created}


# ---------- Расписание ----------

def list_schedule(group_type: str = None):
    """Расписание на неделю. group_type=None — всё; иначе занятия группы + общие."""
    query = "SELECT * FROM schedule"
    params = ()
    if group_type:
        query += " WHERE group_type = ? OR group_type = 'all'"
        params = (group_type,)
    query += " ORDER BY day_of_week, start_time"
    with get_conn() as conn:
        return conn.execute(query, tuple(params)).fetchall()


def add_schedule_entry(day_of_week: int, start_time: str, title: str,
                        group_type: str = "all", end_time: str = None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO schedule (day_of_week, start_time, end_time, title, group_type, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (day_of_week, start_time, end_time, title, group_type, datetime.now().isoformat()),
        )


def update_schedule_entry(entry_id: int, day_of_week: int, start_time: str,
                           title: str, group_type: str, end_time: str = None):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE schedule
            SET day_of_week = ?, start_time = ?, end_time = ?, title = ?,
                group_type = ?, updated_at = ?
            WHERE id = ?
            """,
            (day_of_week, start_time, end_time, title, group_type,
             datetime.now().isoformat(), entry_id),
        )


def delete_schedule_entry(entry_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM schedule WHERE id = ?", (entry_id,))


# ---------- Посещения ----------

def mark_attendance(client_id: int, visit_date: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO attendance (client_id, visit_date, created_at)
            VALUES (?, ?, ?)
            """,
            (client_id, visit_date, datetime.now().isoformat()),
        )


def unmark_attendance(client_id: int, visit_date: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM attendance WHERE client_id = ? AND visit_date = ?",
            (client_id, visit_date),
        )


def attendance_on_date(visit_date: str):
    """Множество id клиентов, отмеченных на эту дату."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT client_id FROM attendance WHERE visit_date = ?", (visit_date,)
        ).fetchall()
        return {r["client_id"] for r in rows}


def count_attendance(client_id: int, date_from: str, date_to: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM attendance
            WHERE client_id = ? AND visit_date BETWEEN ? AND ?
            """,
            (client_id, date_from, date_to),
        ).fetchone()
        return row["n"]


def client_attendance_dates(client_id: int, date_from: str, date_to: str):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT visit_date FROM attendance
            WHERE client_id = ? AND visit_date BETWEEN ? AND ?
            ORDER BY visit_date DESC
            """,
            (client_id, date_from, date_to),
        ).fetchall()
        return [r["visit_date"] for r in rows]


def attendance_counts_for_period(date_from: str, date_to: str):
    """Сколько раз каждый клиент приходил за период — для колонки посещаемости."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT client_id, COUNT(*) AS n FROM attendance
            WHERE visit_date BETWEEN ? AND ?
            GROUP BY client_id
            """,
            (date_from, date_to),
        ).fetchall()
        return {r["client_id"]: r["n"] for r in rows}


# ---------- Пояса ----------

def update_belt(client_id: int, belt: str, stripes: int, belt_date: str = None):
    """
    belt_date — дата аттестации в формате YYYY-MM-DD. Необязательна:
    если не указана, у клиента показывается только пояс, без даты.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE clients SET belt = ?, stripes = ?, belt_updated = ? WHERE id = ?",
            (belt, stripes, belt_date or None, client_id),
        )


# ---------- Статистика ----------

def get_stats(month_start: str, month_end: str):
    """Сводка для админа: клиенты, активные абонементы, доход за период."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        # архивные клиенты в текущих показателях не участвуют
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM clients WHERE archived = 0 AND approved = 1"
        ).fetchone()["n"]
        adults = conn.execute(
            "SELECT COUNT(*) AS n FROM clients WHERE group_type = 'adult' AND archived = 0 AND approved = 1"
        ).fetchone()["n"]
        kids = conn.execute(
            "SELECT COUNT(*) AS n FROM clients WHERE group_type = 'kids' AND archived = 0 AND approved = 1"
        ).fetchone()["n"]
        archived = conn.execute(
            "SELECT COUNT(*) AS n FROM clients WHERE archived = 1"
        ).fetchone()["n"]

        active = conn.execute(
            """
            SELECT COUNT(DISTINCT s.client_id) AS n FROM subscriptions s
            JOIN clients c ON c.id = s.client_id
            WHERE s.status = 'active' AND s.end_date >= ? AND c.archived = 0 AND c.approved = 1
            """,
            (now,),
        ).fetchone()["n"]

        # безлимитные (бесплатные / особые условия) в доход не попадают
        revenue_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS payments
            FROM subscriptions
            WHERE start_date BETWEEN ? AND ? AND unlimited = 0
              AND status != 'superseded'
            """,
            (month_start, month_end),
        ).fetchone()

        unlimited_count = conn.execute(
            """
            SELECT COUNT(DISTINCT s.client_id) AS n FROM subscriptions s
            JOIN clients c ON c.id = s.client_id
            WHERE s.status = 'active' AND s.unlimited = 1 AND c.archived = 0 AND c.approved = 1
            """
        ).fetchone()["n"]

        new_clients = conn.execute(
            "SELECT COUNT(*) AS n FROM clients WHERE created_at BETWEEN ? AND ?",
            (month_start, month_end),
        ).fetchone()["n"]

        visits = conn.execute(
            "SELECT COUNT(*) AS n FROM attendance WHERE visit_date BETWEEN ? AND ?",
            (month_start[:10], month_end[:10]),
        ).fetchone()["n"]

    return {
        "total_clients": total,
        "adults": adults,
        "kids": kids,
        "active_subscriptions": active,
        "unlimited_clients": unlimited_count,
        "paid_subscriptions": active - unlimited_count,
        "without_subscription": total - active,
        "revenue": revenue_row["total"],
        "payments": revenue_row["payments"],
        "archived_clients": archived,
        "new_clients": new_clients,
        "visits": visits,
    }


def find_clients_by_name(name_part: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM clients WHERE full_name LIKE ?", (f"%{name_part}%",)
        ).fetchall()


def list_all_clients():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM clients ORDER BY full_name").fetchall()


# ---------- Абонементы ----------

def supersede_active_subscriptions(client_id: int):
    """
    Помечает действующие абонементы клиента как отменённые.
    Нужно при переводе на особые условия и обратно: иначе старая оплата
    продолжала бы считаться в доходе, хотя абонемент уже не действует.
    """
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE subscriptions SET status = 'superseded'
            WHERE client_id = ? AND status = 'active' AND end_date >= ?
            """,
            (client_id, datetime.now().isoformat()),
        )


def list_client_subscriptions(client_id: int, limit: int = 24):
    """История оплат клиента — чтобы тренер мог увидеть и удалить ошибочную."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, title, start_date, end_date, amount, status, unlimited
            FROM subscriptions WHERE client_id = ?
            ORDER BY start_date DESC LIMIT ?
            """,
            (client_id, limit),
        ).fetchall()


def delete_subscription(subscription_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))


def add_subscription(client_id: int, title: str, payment_date: datetime,
                      months: int = 1, visits: int = None, amount: float = None,
                      unlimited: bool = False):
    """
    payment_date — дата, когда клиент оплатил.
    Следующая оплата (end_date) считается как та же дата через `months` месяцев
    (05.10 -> 05.11 при months=1), а не просто "+30 дней".
    amount — сумма оплаты, нужна для подсчёта дохода в статистике.
    unlimited — абонемент на особых условиях (бесплатно, тренер, партнёр):
    не истекает, не даёт напоминаний и не учитывается в доходе.
    """
    if unlimited:
        end = payment_date + relativedelta(years=100)
        amount = None
    else:
        end = payment_date + relativedelta(months=months)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions
                (client_id, title, start_date, end_date, visits_total, visits_left,
                 status, amount, unlimited)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (client_id, title, payment_date.isoformat(), end.isoformat(),
             visits, visits, amount, 1 if unlimited else 0),
        )


def get_active_subscription(client_id: int):
    """
    Действующий абонемент: не только со статусом 'active', но и с датой
    окончания в будущем. Без проверки даты истёкший абонемент показывался
    клиенту как действующий.
    """
    now = datetime.now().isoformat()
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE client_id = ? AND status = 'active' AND end_date >= ?
            ORDER BY end_date DESC LIMIT 1
            """,
            (client_id, now),
        ).fetchone()


def use_visit(subscription_id: int):
    with get_conn() as conn:
        sub = conn.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
        ).fetchone()
        if sub and sub["visits_left"] is not None and sub["visits_left"] > 0:
            conn.execute(
                "UPDATE subscriptions SET visits_left = visits_left - 1 WHERE id = ?",
                (subscription_id,),
            )


def latest_subscriptions_for_reminders():
    """
    Последний абонемент каждого клиента (независимо от статуса) — используется
    для рассылки напоминаний об оплате: за 3 дня, в день истечения, и затем
    каждый день, пока клиент не оплатит новый абонемент.
    """
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT s.*, c.telegram_id, c.full_name FROM subscriptions s
            JOIN clients c ON c.id = s.client_id
            WHERE c.archived = 0 AND c.approved = 1
              AND s.unlimited = 0
              AND s.status = 'active'
              AND s.id = (
                SELECT id FROM subscriptions
                WHERE client_id = s.client_id AND status = 'active'
                ORDER BY end_date DESC LIMIT 1
            )
            """
        ).fetchall()


def set_last_reminder_date(subscription_id: int, date_str: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET last_reminder_date = ? WHERE id = ?",
            (date_str, subscription_id),
        )


def list_payment_status(days_ahead: int = 5):
    """
    Возвращает все активные абонементы с client info, отсортированные по дате оплаты.
    Используется админом, чтобы разом увидеть, кто скоро должен платить и кто уже просрочил.
    """
    now = datetime.now()
    horizon = now + timedelta(days=days_ahead)
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT s.*, c.telegram_id, c.full_name FROM subscriptions s
            JOIN clients c ON c.id = s.client_id
            WHERE s.status = 'active' AND s.end_date <= ?
            ORDER BY s.end_date
            """,
            (horizon.isoformat(),),
        ).fetchall()


# ---------- Тренировки и записи ----------

def add_training(title: str, start_at: datetime, max_participants: int = 20):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO trainings (title, start_at, max_participants) VALUES (?, ?, ?)",
            (title, start_at.isoformat(), max_participants),
        )


def list_upcoming_trainings():
    now = datetime.now().isoformat()
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM trainings WHERE start_at >= ? ORDER BY start_at",
            (now,),
        ).fetchall()


def get_training(training_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM trainings WHERE id = ?", (training_id,)
        ).fetchone()


def count_bookings(training_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM bookings WHERE training_id = ?", (training_id,)
        ).fetchone()
        return row["n"]


def book_training(training_id: int, client_id: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bookings (training_id, client_id, created_at) VALUES (?, ?, ?)",
            (training_id, client_id, datetime.now().isoformat()),
        )


def cancel_booking(training_id: int, client_id: int):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM bookings WHERE training_id = ? AND client_id = ?",
            (training_id, client_id),
        )


def list_client_bookings(client_id: int):
    now = datetime.now().isoformat()
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT t.* FROM bookings b
            JOIN trainings t ON t.id = b.training_id
            WHERE b.client_id = ? AND t.start_at >= ?
            ORDER BY t.start_at
            """,
            (client_id, now),
        ).fetchall()
