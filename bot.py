"""
Telegram-бот для учёта абонементов борцовского зала.

Возможности:
  Для клиентов:
    Кнопка «Открыть кабинет» рядом с полем ввода (или /app) — регистрация
    (группа Adult/Kids, имя, дата рождения), статус абонемента, заявка на
    заморозку — всё внутри мини-аппа, не через текстовые команды.
    /my              — мой абонемент (дублирует кабинет, текстом)
    /trainings       — ближайшие тренировки
    /book <id>       — записаться на тренировку
    /cancel <id>     — отменить запись
    /mybookings      — мои записи

  Для админа (ID из ADMIN_IDS в .env):
    /admin_app                   — панель тренера (клиенты, заморозки, объявление)
    /clients                     — список клиентов с номерами (текстом)
    /add_sub <номер> <название> <ДД.ММ.ГГГГ> [месяцев] [посещений]
        пример: /add_sub 1 "Месячный" 05.10.2026
    /due                         — кто должен оплатить или просрочил
    /add_client <telegram_id> <ФИО>  — добавить клиента вручную (редко нужно)
    /add_training <ДД.ММ.ГГГГ ЧЧ:ММ> <название> [макс_участников]
        пример: /add_training 20.09.2026 18:00 "Вечерняя группа" 15
    /trainings_all               — все ближайшие тренировки с числом записей

Напоминания об истечении абонемента (за 3 дня) рассылаются автоматически раз в сутки.
"""
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp, MenuButtonDefault
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dateutil.relativedelta import relativedelta

import db
import webapp

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def fmt_date(iso_str: str) -> str:
    return datetime.fromisoformat(iso_str).strftime("%d.%m.%Y %H:%M")


# ---------------- Клиентские команды ----------------

def cabinet_keyboard():
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📱 Открыть личный кабинет", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    existing = db.get_client_by_telegram_id(update.effective_user.id)
    kb = cabinet_keyboard()
    if existing:
        await update.message.reply_text(
            f"С возвращением, {existing['full_name']}!\n"
            "Откройте личный кабинет кнопкой ниже.",
            reply_markup=kb,
        )
    else:
        await update.message.reply_text(
            "Добро пожаловать в бот учёта абонементов зала!\n\n"
            "Нажмите кнопку ниже, чтобы зарегистрироваться и открыть личный кабинет.",
            reply_markup=kb,
        )


async def open_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = cabinet_keyboard()
    if not kb:
        await update.message.reply_text("Личный кабинет пока не настроен тренером.")
        return
    await update.message.reply_text("Ваш личный кабинет:", reply_markup=kb)


async def open_admin_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not WEBAPP_URL:
        await update.message.reply_text("Задайте WEBAPP_URL в переменных окружения.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛠 Открыть панель тренера", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin"))
    ]])
    await update.message.reply_text("Панель тренера:", reply_markup=kb)


async def my_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = db.get_client_by_telegram_id(update.effective_user.id)
    if not client:
        await update.message.reply_text("Вы ещё не зарегистрированы. Нажмите /start.")
        return
    sub = db.get_active_subscription(client["id"])
    if not sub:
        await update.message.reply_text("У вас нет активного абонемента. Обратитесь к тренеру.")
        return
    text = (
        f"Абонемент: {sub['title']}\n"
        f"Действует до: {fmt_date(sub['end_date'])}\n"
    )
    if sub["visits_total"] is not None:
        text += f"Осталось посещений: {sub['visits_left']} из {sub['visits_total']}"
    await update.message.reply_text(text)


async def trainings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_upcoming_trainings()
    if not rows:
        await update.message.reply_text("Ближайших тренировок пока нет.")
        return
    lines = ["Ближайшие тренировки:\n"]
    for t in rows:
        taken = db.count_bookings(t["id"])
        lines.append(
            f"#{t['id']} — {t['title']}\n"
            f"   {fmt_date(t['start_at'])} · записано {taken}/{t['max_participants']}"
        )
    lines.append("\nЗаписаться: /book <номер>")
    await update.message.reply_text("\n".join(lines))


async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = db.get_client_by_telegram_id(update.effective_user.id)
    if not client:
        await update.message.reply_text("Сначала нажмите /start.")
        return
    if not context.args:
        await update.message.reply_text("Укажите номер тренировки: /book 3")
        return
    try:
        training_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер тренировки должен быть числом.")
        return
    training = db.get_training(training_id)
    if not training:
        await update.message.reply_text("Тренировка с таким номером не найдена.")
        return
    taken = db.count_bookings(training_id)
    if taken >= training["max_participants"]:
        await update.message.reply_text("На эту тренировку уже нет мест.")
        return
    db.book_training(training_id, client["id"])
    await update.message.reply_text(
        f"Вы записаны на «{training['title']}» ({fmt_date(training['start_at'])})."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = db.get_client_by_telegram_id(update.effective_user.id)
    if not client:
        await update.message.reply_text("Сначала нажмите /start.")
        return
    if not context.args:
        await update.message.reply_text("Укажите номер тренировки: /cancel 3")
        return
    training_id = int(context.args[0])
    db.cancel_booking(training_id, client["id"])
    await update.message.reply_text("Запись отменена.")


async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = db.get_client_by_telegram_id(update.effective_user.id)
    if not client:
        await update.message.reply_text("Сначала нажмите /start.")
        return
    rows = db.list_client_bookings(client["id"])
    if not rows:
        await update.message.reply_text("У вас нет предстоящих записей.")
        return
    lines = [f"#{t['id']} {t['title']} — {fmt_date(t['start_at'])}" for t in rows]
    await update.message.reply_text("Ваши записи:\n" + "\n".join(lines))


# ---------------- Админские команды ----------------

async def add_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /add_client <telegram_id> <ФИО>")
        return
    telegram_id = int(context.args[0])
    full_name = " ".join(context.args[1:])
    db.upsert_client(telegram_id, full_name)
    await update.message.reply_text(f"Клиент {full_name} добавлен.")


async def add_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    # /add_sub <номер_клиента> "Название" <ДД.ММ.ГГГГ дата оплаты> [месяцев] [посещений]
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            'Формат: /add_sub <номер_клиента> "Название" ДД.ММ.ГГГГ [месяцев] [посещений]\n'
            'Номер клиента посмотрите командой /clients\n'
            'Пример: /add_sub 3 "Абонемент" 05.10.2026\n'
            '(следующая оплата будет автоматически 05.11.2026)'
        )
        return

    try:
        client_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Первым аргументом укажите номер клиента (см. /clients).")
        return

    # ищем среди args дату в формате ДД.ММ.ГГГГ
    date_idx = None
    for i, a in enumerate(args[1:], start=1):
        if a.count(".") == 2:
            date_idx = i
            break
    if date_idx is None:
        await update.message.reply_text("Не найдена дата оплаты в формате ДД.ММ.ГГГГ.")
        return

    try:
        payment_date = datetime.strptime(args[date_idx], "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text("Дата должна быть в формате ДД.ММ.ГГГГ, например 05.10.2026")
        return

    title = " ".join(args[1:date_idx]).strip('"')
    rest = args[date_idx + 1:]
    months = int(rest[0]) if len(rest) >= 1 and rest[0].isdigit() else 1
    visits = int(rest[1]) if len(rest) >= 2 and rest[1].isdigit() else None

    client = db.get_client_by_id(client_id)
    if not client:
        await update.message.reply_text(
            "Клиент с таким номером не найден. Посмотрите список: /clients\n"
            "Если клиента там нет — пусть он напишет боту /start."
        )
        return

    db.add_subscription(client["id"], title, payment_date, months, visits)
    next_due = payment_date + relativedelta(months=months)
    await update.message.reply_text(
        f"Абонемент «{title}» для {client['full_name']} оформлен.\n"
        f"Оплата: {payment_date.strftime('%d.%m.%Y')}\n"
        f"Следующая оплата: {next_due.strftime('%d.%m.%Y')}"
    )
    try:
        await context.bot.send_message(
            client["telegram_id"],
            f"Вам оформлен абонемент «{title}». "
            f"Следующая оплата: {next_due.strftime('%d.%m.%Y')}. Приятных тренировок!",
        )
    except Exception:
        logger.info("Не удалось уведомить клиента %s", client["telegram_id"])


async def due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-команда: кто должен оплатить в ближайшие 5 дней или уже просрочил."""
    if not is_admin(update.effective_user.id):
        return
    rows = db.list_payment_status(days_ahead=5)
    if not rows:
        await update.message.reply_text("Ни у кого нет скорых или просроченных оплат.")
        return
    now = datetime.now()
    overdue_lines, soon_lines = [], []
    for s in rows:
        end = datetime.fromisoformat(s["end_date"])
        line = f"{s['full_name']} — {end.strftime('%d.%m.%Y')} (tg:{s['telegram_id']})"
        if end < now:
            overdue_lines.append(line)
        else:
            soon_lines.append(line)
    text = ""
    if overdue_lines:
        text += "🔴 Просрочили оплату:\n" + "\n".join(overdue_lines) + "\n\n"
    if soon_lines:
        text += "🟡 Скоро нужно оплатить:\n" + "\n".join(soon_lines)
    await update.message.reply_text(text.strip())


async def clients_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = db.list_all_clients()
    if not rows:
        await update.message.reply_text("Клиентов пока нет.")
        return
    lines = [f"{c['id']}: {c['full_name']} (tg:{c['telegram_id']})" for c in rows]
    await update.message.reply_text("\n".join(lines))


async def add_training_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    # /add_training 20.09.2026 18:00 "Название" [макс]
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            'Формат: /add_training ДД.ММ.ГГГГ ЧЧ:ММ "Название" [макс_участников]'
        )
        return
    date_str, time_str = args[0], args[1]
    try:
        start_at = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
    except ValueError:
        await update.message.reply_text("Дата/время в формате ДД.ММ.ГГГГ ЧЧ:ММ")
        return
    if args[-1].isdigit():
        title = " ".join(args[2:-1]).strip('"')
        max_p = int(args[-1])
    else:
        title = " ".join(args[2:]).strip('"')
        max_p = 20
    db.add_training(title, start_at, max_p)
    await update.message.reply_text(f"Тренировка «{title}» добавлена на {date_str} {time_str}.")


async def trainings_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await trainings(update, context)


# ---------------- Фоновая задача: напоминания ----------------

async def remind_expiring_subscriptions(app: Application):
    db.expire_old_subscriptions()
    for sub in db.subscriptions_expiring_soon(days_ahead=3):
        try:
            await app.bot.send_message(
                sub["telegram_id"],
                f"Напоминаем: по абонементу «{sub['title']}» нужно оплатить "
                f"до {fmt_date(sub['end_date'])}.",
            )
            db.mark_notified(sub["id"])
        except Exception:
            logger.warning("Не удалось отправить напоминание клиенту %s", sub["telegram_id"])


async def setup_menu_button(app: Application):
    """Ставит постоянную кнопку рядом с полем ввода, которая сразу открывает мини-апп."""
    if WEBAPP_URL:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Открыть кабинет", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    else:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonDefault())


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Заполните BOT_TOKEN в файле .env")

    db.init_db()
    webapp.run_in_background()

    app = Application.builder().token(BOT_TOKEN).post_init(setup_menu_button).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("my", my_subscription))
    app.add_handler(CommandHandler("trainings", trainings))
    app.add_handler(CommandHandler("book", book))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("mybookings", my_bookings))
    app.add_handler(CommandHandler("app", open_app))
    app.add_handler(CommandHandler("admin_app", open_admin_app))

    app.add_handler(CommandHandler("add_client", add_client))
    app.add_handler(CommandHandler("add_sub", add_sub))
    app.add_handler(CommandHandler("due", due))
    app.add_handler(CommandHandler("clients", clients_list))
    app.add_handler(CommandHandler("add_training", add_training_cmd))
    app.add_handler(CommandHandler("trainings_all", trainings_all))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        remind_expiring_subscriptions,
        "cron",
        hour=10,
        minute=0,
        kwargs={"app": app},
    )
    scheduler.start()

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
