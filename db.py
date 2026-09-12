"""
Работа с базой данных (SQLite).
Все функции синхронные — для такого размера бота этого достаточно.
"""
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager

DB_PATH = "gym.db"


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
                created_at TEXT NOT NULL
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
            """
        )


# ---------- Клиенты ----------

def upsert_client(telegram_id: int, full_name: str, phone: str = None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO clients (telegram_id, full_name, phone, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET full_name=excluded.full_name
            """,
            (telegram_id, full_name, phone, datetime.now().isoformat()),
        )


def get_client_by_telegram_id(telegram_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return row


def find_clients_by_name(name_part: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM clients WHERE full_name LIKE ?", (f"%{name_part}%",)
        ).fetchall()


def list_all_clients():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM clients ORDER BY full_name").fetchall()


# ---------- Абонементы ----------

def add_subscription(client_id: int, title: str, days: int, visits: int = None):
    start = datetime.now()
    end = start + timedelta(days=days)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions
                (client_id, title, start_date, end_date, visits_total, visits_left, status)
            VALUES (?, ?, ?, ?, ?, ?, 'active')
            """,
            (client_id, title, start.isoformat(), end.isoformat(), visits, visits),
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
