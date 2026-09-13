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
                telegram_id INTEGER UNIQUE NOT NULL,
                full_name TEXT NOT NULL,
                phone TEXT,
                group_type TEXT NOT NULL DEFAULT 'adult',
                birth_date TEXT,
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

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                visits_total INTEGER,
                visits_left INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                notified_expiring INTEGER NOT NULL DEFAULT 0
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
            """
        )
        # Миграция: если база уже существовала без новых колонок — добавляем их
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(clients)")]
        if "group_type" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN group_type TEXT NOT NULL DEFAULT 'adult'")
        if "birth_date" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN birth_date TEXT")


# ---------- Клиенты ----------

def upsert_client(telegram_id: int, full_name: str, group_type: str = "adult",
                   birth_date: str = None, phone: str = None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO clients (telegram_id, full_name, phone, group_type, birth_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                full_name=excluded.full_name, group_type=excluded.group_type,
                birth_date=excluded.birth_date
            """,
            (telegram_id, full_name, phone, group_type, birth_date, datetime.now().isoformat()),
        )


def get_client_by_telegram_id(telegram_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return row


def get_client_by_id(client_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM clients WHERE id = ?", (client_id,)
        ).fetchone()


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


def delete_client(client_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))


def list_clients_with_subscription(group_type: str = None):
    """Для админ-панели мини-аппа: клиенты + их текущий активный абонемент (если есть)."""
    query = """
        SELECT c.id, c.full_name, c.telegram_id, c.group_type, c.birth_date,
               s.title AS sub_title, s.end_date AS sub_end_date,
               s.visits_total, s.visits_left, s.status AS sub_status
        FROM clients c
        LEFT JOIN subscriptions s ON s.id = (
            SELECT id FROM subscriptions
            WHERE client_id = c.id AND status = 'active'
            ORDER BY end_date DESC LIMIT 1
        )
    """
    params = ()
    if group_type:
        query += " WHERE c.group_type = ?"
        params = (group_type,)
    query += " ORDER BY c.full_name"
    with get_conn() as conn:
        return conn.execute(query, params).fetchall()


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
                SELECT * FROM subscriptions WHERE client_id = ? AND status = 'active'
                ORDER BY end_date DESC LIMIT 1
                """,
                (req["client_id"],),
            ).fetchone()
            if sub:
                new_end = datetime.fromisoformat(sub["end_date"]) + timedelta(days=req["days_requested"])
                conn.execute(
                    "UPDATE subscriptions SET end_date = ? WHERE id = ?",
                    (new_end.isoformat(), sub["id"]),
                )
    return req


def find_clients_by_name(name_part: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM clients WHERE full_name LIKE ?", (f"%{name_part}%",)
        ).fetchall()


def list_all_clients():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM clients ORDER BY full_name").fetchall()


# ---------- Абонементы ----------

def add_subscription(client_id: int, title: str, payment_date: datetime,
                      months: int = 1, visits: int = None):
    """
    payment_date — дата, когда клиент оплатил.
    Следующая оплата (end_date) считается как та же дата через `months` месяцев
    (05.10 -> 05.11 при months=1), а не просто "+30 дней".
    """
    end = payment_date + relativedelta(months=months)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions
                (client_id, title, start_date, end_date, visits_total, visits_left, status)
            VALUES (?, ?, ?, ?, ?, ?, 'active')
            """,
            (client_id, title, payment_date.isoformat(), end.isoformat(), visits, visits),
        )


def get_active_subscription(client_id: int):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE client_id = ? AND status = 'active'
            ORDER BY end_date DESC LIMIT 1
            """,
            (client_id,),
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


def expire_old_subscriptions():
    """Помечает просроченные абонементы как expired. Возвращает список истёкших (для инфо)."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET status = 'expired' "
            "WHERE status = 'active' AND end_date < ?",
            (now,),
        )


def subscriptions_expiring_soon(days_ahead: int = 3):
    """Активные абонементы, которые истекают в ближайшие days_ahead дней и ещё не уведомлены."""
    now = datetime.now()
    soon = now + timedelta(days=days_ahead)
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT s.*, c.telegram_id, c.full_name FROM subscriptions s
            JOIN clients c ON c.id = s.client_id
            WHERE s.status = 'active'
              AND s.end_date BETWEEN ? AND ?
              AND s.notified_expiring = 0
            """,
            (now.isoformat(), soon.isoformat()),
        ).fetchall()


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


def mark_notified(subscription_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET notified_expiring = 1 WHERE id = ?",
            (subscription_id,),
        )


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
