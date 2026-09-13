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


def notify_telegram(chat_id: int, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception:
        pass


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
        "client_id": client["id"],
        "full_name": client["full_name"],
        "group_type": client["group_type"],
        "subscription": subscription,
        "announcement": db.get_announcement(),
    })


@flask_app.route("/api/register", methods=["POST"])
def api_register():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not user:
        return jsonify({"error": "auth_failed"}), 401

    full_name = (body.get("full_name") or "").strip()
    group_type = body.get("group_type") if body.get("group_type") in ("adult", "kids") else "adult"
    birth_date = (body.get("birth_date") or "").strip() or None

    if len(full_name) < 2:
        return jsonify({"error": "bad_name"}), 400

    db.upsert_client(user["id"], full_name, group_type, birth_date)

    for admin_id in ADMIN_IDS:
        notify_telegram(
            admin_id,
            f"Новая регистрация: {full_name} "
            f"({'Kids' if group_type == 'kids' else 'Adult'})",
        )

    return jsonify({"ok": True})


# ---------------- API: админ ----------------

def require_admin(user):
    return user and user.get("id") in ADMIN_IDS


@flask_app.route("/api/admin/clients")
def api_admin_clients():
    init_data = request.args.get("initData", "")
    user = validate_init_data(init_data)
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    group_type = request.args.get("group")  # 'adult' | 'kids' | None (все)
    rows = db.list_clients_with_subscription(group_type)
    now = datetime.now()
    result = []
    for c in rows:
        status = "none"
        end_date = None
        if c["sub_end_date"]:
            end = datetime.fromisoformat(c["sub_end_date"])
            end_date = fmt_date(c["sub_end_date"])
            days_left = (end.date() - now.date()).days
            if days_left < 0:
                status = "overdue"
            elif days_left <= 3:
                status = "expiring"
            else:
                status = "active"
        result.append({
            "id": c["id"],
            "full_name": c["full_name"],
            "telegram_id": c["telegram_id"],
            "group_type": c["group_type"],
            "birth_date": c["birth_date"],
            "note": c["note"],
            "sub_title": c["sub_title"],
            "sub_end_date": end_date,
            "visits_left": c["visits_left"],
            "visits_total": c["visits_total"],
            "status": status,
        })
    return jsonify({"clients": result})


@flask_app.route("/api/admin/set_note", methods=["POST"])
def api_admin_set_note():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    client_id = body.get("client_id")
    note = body.get("note", "")
    if not client_id:
        return jsonify({"error": "bad_request"}), 400

    db.update_client_note(client_id, note)
    return jsonify({"ok": True})


@flask_app.route("/api/admin/edit_client", methods=["POST"])
def api_admin_edit_client():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    client_id = body.get("client_id")
    full_name = (body.get("full_name") or "").strip()
    if not client_id or len(full_name) < 2:
        return jsonify({"error": "bad_request"}), 400

    db.update_client_name(client_id, full_name)
    return jsonify({"ok": True})


@flask_app.route("/api/admin/delete_client", methods=["POST"])
def api_admin_delete_client():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    client_id = body.get("client_id")
    if not client_id:
        return jsonify({"error": "bad_request"}), 400

    db.delete_client(client_id)
    return jsonify({"ok": True})


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

    from dateutil.relativedelta import relativedelta
    next_due = payment_date + relativedelta(months=months)
    notify_telegram(
        client["telegram_id"],
        f"Оплата абонемента «{title}» зафиксирована. "
        f"Следующая оплата: {next_due.strftime('%d.%m.%Y')}.",
    )

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


# ---------------- API: заморозка абонемента ----------------

@flask_app.route("/api/freeze_request", methods=["POST"])
def api_freeze_request():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not user:
        return jsonify({"error": "auth_failed"}), 401

    client = db.get_client_by_telegram_id(user["id"])
    if not client:
        return jsonify({"error": "not_registered"}), 404

    try:
        days = int(body.get("days"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad_days"}), 400
    if days < 1 or days > 90:
        return jsonify({"error": "bad_days"}), 400

    db.create_freeze_request(client["id"], days)

    for admin_id in ADMIN_IDS:
        notify_telegram(
            admin_id,
            f"Заявка на заморозку от {client['full_name']} на {days} дн.\n"
            f"Откройте /admin_app, чтобы одобрить или отклонить.",
        )

    return jsonify({"ok": True})


@flask_app.route("/api/admin/freeze_requests")
def api_admin_freeze_requests():
    init_data = request.args.get("initData", "")
    user = validate_init_data(init_data)
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    rows = db.list_pending_freeze_requests()
    result = [{
        "id": r["id"],
        "full_name": r["full_name"],
        "days_requested": r["days_requested"],
        "created_at": fmt_date(r["created_at"]),
    } for r in rows]
    return jsonify({"requests": result})


@flask_app.route("/api/admin/freeze_decision", methods=["POST"])
def api_admin_freeze_decision():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    request_id = body.get("request_id")
    approve = bool(body.get("approve"))

    req = db.decide_freeze_request(request_id, approve)
    if not req:
        return jsonify({"error": "not_found_or_decided"}), 404

    client = db.get_client_by_id(req["client_id"])
    if client:
        if approve:
            notify_telegram(
                client["telegram_id"],
                f"Ваша заявка на заморозку ({req['days_requested']} дн.) одобрена. "
                "Дата следующей оплаты сдвинута.",
            )
        else:
            notify_telegram(
                client["telegram_id"],
                f"Ваша заявка на заморозку ({req['days_requested']} дн.) отклонена. "
                "Свяжитесь с тренером для уточнения.",
            )

    return jsonify({"ok": True})


# ---------------- API: активация абонемента ----------------

@flask_app.route("/api/activate_request", methods=["POST"])
def api_activate_request():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not user:
        return jsonify({"error": "auth_failed"}), 401

    client = db.get_client_by_telegram_id(user["id"])
    if not client:
        return jsonify({"error": "not_registered"}), 404

    requested_date = (body.get("requested_date") or "").strip()
    try:
        datetime.strptime(requested_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "bad_date"}), 400

    db.create_activation_request(client["id"], requested_date)

    for admin_id in ADMIN_IDS:
        notify_telegram(
            admin_id,
            f"Заявка на активацию абонемента от {client['full_name']} "
            f"с {fmt_date(requested_date)}.\n"
            f"Откройте /admin_app, чтобы одобрить или отклонить.",
        )

    return jsonify({"ok": True})


@flask_app.route("/api/admin/activation_requests")
def api_admin_activation_requests():
    init_data = request.args.get("initData", "")
    user = validate_init_data(init_data)
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    rows = db.list_pending_activation_requests()
    result = [{
        "id": r["id"],
        "full_name": r["full_name"],
        "requested_date": r["requested_date"][:10],
        "requested_date_display": fmt_date(r["requested_date"]),
        "created_at": fmt_date(r["created_at"]),
    } for r in rows]
    return jsonify({"requests": result})


@flask_app.route("/api/admin/activation_edit_date", methods=["POST"])
def api_admin_activation_edit_date():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    request_id = body.get("request_id")
    new_date = (body.get("new_date") or "").strip()
    try:
        datetime.strptime(new_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "bad_date"}), 400

    db.update_activation_request_date(request_id, new_date)
    return jsonify({"ok": True})


@flask_app.route("/api/admin/activation_decision", methods=["POST"])
def api_admin_activation_decision():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    request_id = body.get("request_id")
    approve = bool(body.get("approve"))

    req = db.decide_activation_request(request_id, approve)
    if not req:
        return jsonify({"error": "not_found_or_decided"}), 404

    client = db.get_client_by_id(req["client_id"])
    if client:
        if approve:
            notify_telegram(
                client["telegram_id"],
                f"Ваш абонемент активирован с {fmt_date(req['requested_date'])}.",
            )
        else:
            notify_telegram(
                client["telegram_id"],
                "Заявка на активацию абонемента отклонена. "
                "Свяжитесь с тренером для уточнения.",
            )

    return jsonify({"ok": True})


def run_in_background():
    """Запускает веб-сервер в отдельном потоке, не блокируя бота."""
    thread = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False),
        daemon=True,
    )
    thread.start()
