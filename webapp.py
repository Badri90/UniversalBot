"""
Веб-сервер для Telegram Mini App (личный кабинет клиента + панель админа).
Работает в том же процессе, что и бот, и использует ту же базу данных (gym.db),
поэтому отдельно деплоить его не нужно — он поднимается автоматически вместе с ботом.

Проверка подлинности: Telegram передаёт initData, подписанные хэшем на основе
токена бота. Мы проверяем эту подпись, чтобы быть уверены, что запрос
действительно пришёл из Telegram-клиента конкретного пользователя, а не
подделан кем-то извне.
"""
import hashlib
import hmac
import json
import os
import threading
from datetime import datetime
from urllib.parse import parse_qsl

import requests
from flask import Flask, jsonify, request, send_from_directory

import db

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PORT = int(os.getenv("PORT", "8080"))

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

flask_app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")


def validate_init_data(init_data: str):
    """
    Проверяет подпись initData от Telegram WebApp.
    Возвращает dict с данными пользователя, либо None если подпись неверна.
    """
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    user_json = parsed.get("user")
    if not user_json:
        return None
    return json.loads(user_json)


def fmt_date(iso_str: str) -> str:
    return datetime.fromisoformat(iso_str).strftime("%d.%m.%Y")


# ---------------- Статические страницы ----------------

@flask_app.route("/")
def index_page():
    return send_from_directory(STATIC_DIR, "index.html")


@flask_app.route("/admin")
def admin_page():
    return send_from_directory(STATIC_DIR, "admin.html")


# ---------------- API: клиент ----------------

@flask_app.route("/api/me")
def api_me():
    init_data = request.args.get("initData", "")
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "auth_failed"}), 401

    client = db.get_client_by_telegram_id(user["id"])
    if not client:
        return jsonify({"registered": False})

    sub = db.get_active_subscription(client["id"])
    subscription = None
    if sub:
        subscription = {
            "title": sub["title"],
            "end_date": fmt_date(sub["end_date"]),
            "visits_total": sub["visits_total"],
            "visits_left": sub["visits_left"],
        }

    return jsonify({
        "registered": True,
        "full_name": client["full_name"],
        "subscription": subscription,
        "announcement": db.get_announcement(),
    })


# ---------------- API: админ ----------------

def require_admin(user):
    return user and user.get("id") in ADMIN_IDS


@flask_app.route("/api/admin/clients")
def api_admin_clients():
    init_data = request.args.get("initData", "")
    user = validate_init_data(init_data)
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    rows = db.list_clients_with_subscription()
    now = datetime.now()
    result = []
    for c in rows:
        status = "none"
        end_date = None
        if c["sub_end_date"]:
            end = datetime.fromisoformat(c["sub_end_date"])
            end_date = fmt_date(c["sub_end_date"])
            status = "overdue" if end < now else "active"
        result.append({
            "id": c["id"],
            "full_name": c["full_name"],
            "telegram_id": c["telegram_id"],
            "sub_title": c["sub_title"],
            "sub_end_date": end_date,
            "visits_left": c["visits_left"],
            "visits_total": c["visits_total"],
            "status": status,
        })
    return jsonify({"clients": result})


@flask_app.route("/api/admin/mark_payment", methods=["POST"])
def api_admin_mark_payment():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    client_id = body.get("client_id")
    title = body.get("title", "Абонемент")
    payment_date_str = body.get("payment_date")
    months = int(body.get("months", 1))
    visits = body.get("visits")
    visits = int(visits) if visits not in (None, "") else None

    client = db.get_client_by_id(client_id)
    if not client:
        return jsonify({"error": "client_not_found"}), 404

    try:
        payment_date = datetime.strptime(payment_date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return jsonify({"error": "bad_date"}), 400

    db.add_subscription(client["id"], title, payment_date, months, visits)

    # уведомляем клиента напрямую через Bot API (без общего event loop с ботом)
    try:
        from dateutil.relativedelta import relativedelta
        next_due = payment_date + relativedelta(months=months)
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": client["telegram_id"],
                "text": f"Оплата абонемента «{title}» зафиксирована. "
                        f"Следующая оплата: {next_due.strftime('%d.%m.%Y')}.",
            },
            timeout=5,
        )
    except Exception:
        pass

    return jsonify({"ok": True})


@flask_app.route("/api/admin/announcement", methods=["GET", "POST"])
def api_admin_announcement():
    if request.method == "GET":
        init_data = request.args.get("initData", "")
        user = validate_init_data(init_data)
        if not require_admin(user):
            return jsonify({"error": "forbidden"}), 403
        return jsonify({"text": db.get_announcement()})

    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403
    db.set_announcement(body.get("text", ""))
    return jsonify({"ok": True})


def run_in_background():
    """Запускает веб-сервер в отдельном потоке, не блокируя бота."""
    thread = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False),
        daemon=True,
    )
    thread.start()
