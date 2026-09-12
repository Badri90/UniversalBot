"""
Telegram-бот для учёта абонементов борцовского зала.

Возможности:
  Для клиентов:
    /start           — регистрация
    /my              — мой абонемент (сроки / остаток посещений)
    /trainings       — ближайшие тренировки
    /book <id>       — записаться на тренировку
    /cancel <id>     — отменить запись
    /mybookings      — мои записи

  Для админа (ID из ADMIN_IDS в .env):
    /add_client <telegram_id> <ФИО>
    /add_sub <telegram_id> <название> <дней> [посещений]
        пример: /add_sub 123456789 "Месячный" 30 12
    /clients                     — список клиентов
    /add_training <ДД.ММ.ГГГГ ЧЧ:ММ> <название> [макс_участников]
        пример: /add_training 20.09.2026 18:00 "Вечерняя группа" 15
    /trainings_all               — все ближайшие тренировки с числом записей

Напоминания об истечении абонемента (за 3 дня) рассылаются автоматически раз в сутки.
"""
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def fmt_date(iso_str: str) -> str:
    return datetime.fromisoformat(iso_str).strftime("%d.%m.%Y %H:%M")


# ---------------- Клиентские команды ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_client(user.id, user.full_name)
    await update.message.reply_text(
        f"Привет, {user.full_name}!\n\n"
        "Это бот учёта абонементов зала.\n"
        "/my — мой абонемент\n"
        "/trainings — ближайшие тренировки\n"
        "/mybookings — мои записи"
    )


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
    # /add_sub <telegram_id> <"название"> <дней> [посещений]
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            'Формат: /add_sub <telegram_id> "Название" <дней> [посещений]'
        )
        return
    telegram_id = int(args[0])
    # название может быть в кавычках с пробелами — простое объединение всего кроме первого и двух последних
    if len(args) >= 4 and args[-1].isdigit() and args[-2].isdigit():
        title = " ".join(args[1:-2]).strip('"')
        days = int(args[-2])
        visits = int(args[-1])
    else:
        title = " ".join(args[1:-1]).strip('"')
        days = int(args[-1])
        visits = None

    client = db.get_client_by_telegram_id(telegram_id)
    if not client:
        await update.message.reply_text(
            "Такого клиента нет. Сначала добавьте: /add_client <telegram_id> <ФИО>"
        )
        return
    db.add_subscription(client["id"], title, days, visits)
    await update.message.reply_text(
        f"Абонемент «{title}» на {days} дн. добавлен для {client['full_name']}."
    )
    try:
        await context.bot.send_message(
            telegram_id,
            f"Вам оформлен абонемент «{title}» на {days} дней. Приятных тренировок!",
        )
    except Exception:
        logger.info("Не удалось уведомить клиента %s", telegram_id)


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
                f"Ваш абонемент «{sub['title']}» истекает {fmt_date(sub['end_date'])}. "
                "Не забудьте продлить!",
            )
            db.mark_notified(sub["id"])
        except Exception:
            logger.warning("Не удалось отправить напоминание клиенту %s", sub["telegram_id"])


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Заполните BOT_TOKEN в файле .env")

    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("my", my_subscription))
    app.add_handler(CommandHandler("trainings", trainings))
    app.add_handler(CommandHandler("book", book))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("mybookings", my_bookings))

    app.add_handler(CommandHandler("add_client", add_client))
    app.add_handler(CommandHandler("add_sub", add_sub))
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
