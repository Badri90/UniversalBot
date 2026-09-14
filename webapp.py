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
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

import requests
from flask import Flask, jsonify, request, send_from_directory

import db

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PORT = int(os.getenv("PORT", "8080"))
# фиксированная стоимость абонемента (лари)
DEFAULT_PRICE = float(os.getenv("SUBSCRIPTION_PRICE", "130"))
CURRENCY = os.getenv("CURRENCY", "GEL")

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

    # админ может посмотреть кабинет конкретного клиента глазами клиента
    view_as = request.args.get("as")
    viewing_as_admin = False
    if view_as and require_admin(user):
        client = db.get_client_by_id(int(view_as))
        viewing_as_admin = True
    else:
        client = db.get_client_by_telegram_id(user["id"])

    if not client:
        return jsonify({"registered": False})

    sub = db.get_active_subscription(client["id"])
    subscription = None
    if sub:
        subscription = {
            "title": sub["title"],
            "end_date": fmt_date(sub["end_date"]),
            "unlimited": bool(sub["unlimited"]),
            "visits_total": sub["visits_total"],
            "visits_left": sub["visits_left"],
        }

    # посещения за текущий месяц
    today = datetime.now()
    month_start = today.replace(day=1).strftime("%Y-%m-%d")
    month_end = today.strftime("%Y-%m-%d")
    visits_this_month = db.count_attendance(client["id"], month_start, month_end)

    schedule = [{
        "day_of_week": r["day_of_week"],
        "start_time": r["start_time"],
        "end_time": r["end_time"],
        "title": r["title"],
        "group_type": r["group_type"],
    } for r in db.list_schedule(client["group_type"])]

    return jsonify({
        "registered": True,
        "client_id": client["id"],
        "full_name": client["full_name"],
        "group_type": client["group_type"],
        "belt": client["belt"],
        "stripes": client["stripes"],
        "belt_updated": fmt_date(client["belt_updated"]) if client["belt_updated"] else None,
        "subscription": subscription,
        "visits_this_month": visits_this_month,
        "schedule": schedule,
        "announcement": db.get_announcement(),
        "viewing_as_admin": viewing_as_admin,
    })


@flask_app.route("/api/my_attendance")
def api_my_attendance():
    """Календарь посещений клиента за выбранный месяц."""
    user = validate_init_data(request.args.get("initData", ""))
    if not user:
        return jsonify({"error": "auth_failed"}), 401

    view_as = request.args.get("as")
    if view_as and require_admin(user):
        client = db.get_client_by_id(int(view_as))
    else:
        client = db.get_client_by_telegram_id(user["id"])
    if not client:
        return jsonify({"error": "not_registered"}), 404

    month = request.args.get("month")
    now = datetime.now()
    if month:
        try:
            base = datetime.strptime(month + "-01", "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "bad_month"}), 400
    else:
        base = now.replace(day=1)

    from dateutil.relativedelta import relativedelta
    start = base.strftime("%Y-%m-%d")
    end = (base + relativedelta(months=1) - timedelta(days=1)).strftime("%Y-%m-%d")

    dates = db.client_attendance_dates(client["id"], start, end)
    return jsonify({
        "month_key": base.strftime("%Y-%m"),
        "year": base.year,
        "month": base.month,
        "days": [d[8:10].lstrip("0") for d in dates],
        "total": len(dates),
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
            if c["unlimited"]:
                status = "unlimited"
                end_date = None
            elif days_left < 0:
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
            "unlimited": bool(c["unlimited"]),
            "belt": c["belt"],
            "stripes": c["stripes"],
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
    unlimited = bool(body.get("unlimited"))
    amount = body.get("amount")
    try:
        amount = float(amount) if amount not in (None, "") else DEFAULT_PRICE
    except (TypeError, ValueError):
        amount = DEFAULT_PRICE

    client = db.get_client_by_id(client_id)
    if not client:
        return jsonify({"error": "client_not_found"}), 404

    try:
        payment_date = datetime.strptime(payment_date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return jsonify({"error": "bad_date"}), 400

    db.add_subscription(client["id"], title, payment_date, months, visits, amount, unlimited)
    db.resolve_pending_activation_requests(client["id"])

    if unlimited:
        notify_telegram(
            client["telegram_id"],
            f"Вам оформлен безлимитный абонемент «{title}». Срок не ограничен.",
        )
    else:
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

    active_sub = db.get_active_subscription(client["id"])
    if not active_sub:
        return jsonify({"error": "no_active_subscription"}), 400

    # безлимитный абонемент не истекает — замораживать нечего
    if active_sub["unlimited"]:
        return jsonify({"error": "unlimited_subscription"}), 400

    if db.has_pending_freeze_request(client["id"]):
        return jsonify({"error": "already_pending"}), 409

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

    if db.has_pending_activation_request(client["id"]):
        return jsonify({"error": "already_pending"}), 409

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

    result = db.decide_activation_request(request_id, approve, 1, DEFAULT_PRICE)
    if not result:
        return jsonify({"error": "not_found_or_decided"}), 404
    req = result["request"]

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


# ---------------- API: расписание ----------------

@flask_app.route("/api/admin/schedule", methods=["GET", "POST"])
def api_admin_schedule():
    if request.method == "GET":
        user = validate_init_data(request.args.get("initData", ""))
        if not require_admin(user):
            return jsonify({"error": "forbidden"}), 403
        rows = db.list_schedule()
        return jsonify({"schedule": [{
            "id": r["id"],
            "day_of_week": r["day_of_week"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "title": r["title"],
            "group_type": r["group_type"],
        } for r in rows]})

    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    try:
        day = int(body.get("day_of_week"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad_day"}), 400
    if day < 0 or day > 6:
        return jsonify({"error": "bad_day"}), 400

    start_time = (body.get("start_time") or "").strip()
    end_time = (body.get("end_time") or "").strip() or None
    title = (body.get("title") or "").strip()
    group_type = body.get("group_type") if body.get("group_type") in ("adult", "kids", "all") else "all"

    if not start_time or not title:
        return jsonify({"error": "bad_request"}), 400

    entry_id = body.get("id")
    if entry_id:
        db.update_schedule_entry(entry_id, day, start_time, title, group_type, end_time)
    else:
        db.add_schedule_entry(day, start_time, title, group_type, end_time)
    return jsonify({"ok": True})


@flask_app.route("/api/admin/schedule_delete", methods=["POST"])
def api_admin_schedule_delete():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403
    entry_id = body.get("id")
    if not entry_id:
        return jsonify({"error": "bad_request"}), 400
    db.delete_schedule_entry(entry_id)
    return jsonify({"ok": True})


# ---------------- API: посещаемость ----------------

@flask_app.route("/api/admin/attendance")
def api_admin_attendance():
    user = validate_init_data(request.args.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    visit_date = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    group_type = request.args.get("group")
    try:
        datetime.strptime(visit_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "bad_date"}), 400

    present = db.attendance_on_date(visit_date)
    rows = db.list_clients_with_subscription(group_type)

    month_start = visit_date[:8] + "01"
    counts = db.attendance_counts_for_period(month_start, visit_date)

    return jsonify({
        "date": visit_date,
        "clients": [{
            "id": c["id"],
            "full_name": c["full_name"],
            "group_type": c["group_type"],
            "present": c["id"] in present,
            "month_visits": counts.get(c["id"], 0),
        } for c in rows],
    })


@flask_app.route("/api/admin/attendance_toggle", methods=["POST"])
def api_admin_attendance_toggle():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    client_id = body.get("client_id")
    visit_date = (body.get("date") or "").strip()
    present = bool(body.get("present"))
    try:
        datetime.strptime(visit_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "bad_date"}), 400
    if not client_id:
        return jsonify({"error": "bad_request"}), 400

    if present:
        db.mark_attendance(client_id, visit_date)
    else:
        db.unmark_attendance(client_id, visit_date)
    return jsonify({"ok": True})


# ---------------- API: пояса ----------------

@flask_app.route("/api/admin/set_belt", methods=["POST"])
def api_admin_set_belt():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    client_id = body.get("client_id")
    belt = (body.get("belt") or "").strip() or None
    try:
        stripes = int(body.get("stripes", 0))
    except (TypeError, ValueError):
        stripes = 0
    stripes = max(0, min(4, stripes))

    if not client_id:
        return jsonify({"error": "bad_request"}), 400

    client = db.get_client_by_id(client_id)
    if not client:
        return jsonify({"error": "client_not_found"}), 404

    previous = (client["belt"], client["stripes"])
    db.update_belt(client_id, belt, stripes)

    # поздравляем клиента, если пояс или число полосок изменились
    if belt and previous != (belt, stripes):
        notify_telegram(
            client["telegram_id"],
            f"Поздравляем с аттестацией! Ваш пояс: {belt_label(belt)}"
            + (f", полосок: {stripes}" if stripes else "") + ".",
        )

    return jsonify({"ok": True})


BELT_LABELS = {
    "white": "белый", "grey": "серый", "yellow": "жёлтый", "orange": "оранжевый",
    "green": "зелёный", "blue": "синий", "purple": "фиолетовый",
    "brown": "коричневый", "black": "чёрный",
}


def belt_label(belt: str) -> str:
    return BELT_LABELS.get(belt, belt)


# ---------------- API: статистика ----------------

@flask_app.route("/api/admin/stats")
def api_admin_stats():
    user = validate_init_data(request.args.get("initData", ""))
    if not require_admin(user):
        return jsonify({"error": "forbidden"}), 403

    month = request.args.get("month")  # 'YYYY-MM'
    now = datetime.now()
    if month:
        try:
            base = datetime.strptime(month + "-01", "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "bad_month"}), 400
    else:
        base = now.replace(day=1)

    from dateutil.relativedelta import relativedelta
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + relativedelta(months=1) - timedelta(seconds=1)

    stats = db.get_stats(start.isoformat(), end.isoformat())
    stats["month"] = start.strftime("%m.%Y")
    stats["month_key"] = start.strftime("%Y-%m")
    return jsonify(stats)


def run_in_background():
    """Запускает веб-сервер в отдельном потоке, не блокируя бота."""
    thread = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False),
        daemon=True,
    )
    thread.start()
