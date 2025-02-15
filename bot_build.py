import logging
import datetime
import re
import os
import json
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Bot
)
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    ConversationHandler,
    CallbackContext,
)

# PyDrive2 для роботи з файлами на Google Drive:
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials

# Імпорт функцій для роботи з таблицею
from sheets_helper import (
    get_today_sheet,
    get_worker_block_header_row,
    create_worker_block,
    update_shift_row,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CREDENTIALS_JSON = os.getenv("credentials", "")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# СТАНИ
REG_PHONE, REG_FIO = range(1, 3)
WS_DRIVING_CHOICE = 11
WS_WAITING_FOR_START_MILEAGE = 12
WS_CHOOSE_CAR = 13
WS_WAITING_FOR_LOCATION = 14
WE_WAITING_FOR_LOCATION, WE_WAITING_FOR_MILEAGE = range(20, 22)

PHONE_REGEX = re.compile(r'^(?:\+32\d{8,9}|0\d{9})$')


def now_belgium():
    """Повертає локальний час у часовому поясі 'Europe/Brussels'."""
    return datetime.datetime.now(ZoneInfo("Europe/Brussels"))


# ======================================================
# Робота з файлами на Google Drive
# ======================================================
def get_drive_service_account():
    if not CREDENTIALS_JSON:
        raise ValueError("Environment variable 'credentials' (JSON) is not set.")
    creds_dict = json.loads(CREDENTIALS_JSON)

    gauth = GoogleAuth()
    gauth.credentials = ServiceAccountCredentials._from_parsed_json_keyfile(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive = GoogleDrive(gauth)
    return drive

def load_drive_file(file_name):
    drive = get_drive_service_account()
    file_list = drive.ListFile({
        'q': f"'{DRIVE_FOLDER_ID}' in parents and title = '{file_name}' and trashed=false"
    }).GetList()
    if file_list:
        f = file_list[0]
        return f.GetContentString()
    else:
        return ""

def save_drive_file(file_name, content):
    drive = get_drive_service_account()
    file_list = drive.ListFile({
        'q': f"'{DRIVE_FOLDER_ID}' in parents and title = '{file_name}' and trashed=false"
    }).GetList()
    if file_list:
        f = file_list[0]
    else:
        f = drive.CreateFile({"parents": [{"id": DRIVE_FOLDER_ID}], "title": file_name})
    f.SetContentString(content)
    f.Upload()


# ======================================================
# Користувачі та авто
# ======================================================
def load_registered_users():
    file_content = load_drive_file("users.txt")
    if not file_content.strip():
        return {}
    lines = file_content.strip().split("\n")
    users = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            user_id = int(parts[0].strip())
        except ValueError:
            continue
        phone = parts[1].strip()
        fio = parts[2].strip()
        users[user_id] = {"phone": phone, "fio": fio, "car": ""}
    return users

def save_registered_user(user_id, phone, fio):
    users_dict = load_registered_users()
    if user_id in users_dict:
        del users_dict[user_id]
    users_dict[user_id] = {"phone": phone, "fio": fio, "car": ""}

    lines = []
    for uid, data in users_dict.items():
        line = f"{uid}, {data['phone']}, {data['fio']}, "
        lines.append(line)
    new_content = "\n".join(lines)
    save_drive_file("users.txt", new_content)

def load_cars():
    file_content = load_drive_file("cars.txt").strip()
    if not file_content:
        default_content = (
            "Марка авто, Колір, Номер авто\n"
            "Peugeot Expert(большой), 2FVK026\n"
            "Peugeot Partner (серый), 2GBH011\n"
            "Peugeot Expert (белый), 2EVB969\n"
            "Peugeot Partner (Гриша), 2СRF684\n"
        )
        save_drive_file("cars.txt", default_content)
        file_content = default_content

    lines = file_content.split("\n")
    cars = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("Марка"):
            continue
        cars.append(line)
    return cars


# ======================================================
# Формування меню
# ======================================================
def get_location_keyboard():
    button = KeyboardButton("Поделиться геолокацией", request_location=True)
    return ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)

def get_main_menu_reply_keyboard(user_id: int, context: CallbackContext):
    """
    Формує головне меню:
    - Якщо зміна не активна => Приступаю
    - Якщо активна менше години => Идёт смена
    - Якщо активна більше години => Завершаю
    """
    active_work = context.bot_data.get("active_work", {})
    if not active_work.get(user_id, False):
        # Зміна не активна
        keyboard = [["Приступаю"]]
    else:
        # Зміна триває
        user_data = context.dispatcher.user_data.get(user_id, {})
        shift_start_dt = user_data.get("shift_start_dt")
        if shift_start_dt:
            elapsed = (now_belgium() - shift_start_dt).total_seconds()
            if elapsed >= 3600:
                keyboard = [["Завершаю"]]
            else:
                keyboard = [["Идёт смена"]]
        else:
            keyboard = [["Завершаю"]]  # fallback

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def send_main_menu(user_id: int, context: CallbackContext):
    context.bot.send_message(
        chat_id=user_id,
        text="Выберите действие:",
        reply_markup=get_main_menu_reply_keyboard(user_id, context)
    )


# ======================================================
# Реєстрація нового користувача
# ======================================================
REG_PHONE, REG_FIO = range(1, 3)

def start_command(update: Update, context: CallbackContext) -> int:
    """/start – Починаємо реєстрацію або показуємо меню."""
    user_id = update.effective_user.id

    if "registered_users" not in context.bot_data:
        context.bot_data["registered_users"] = load_registered_users()

    if user_id in context.bot_data["registered_users"]:
        update.message.reply_text("Вы уже зарегистрированы!")
        send_main_menu(user_id, context)
        return ConversationHandler.END

    button = KeyboardButton("Поделиться номером", request_contact=True)
    reply_markup = ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text(
        "Введите, пожалуйста, ваш бельгийский номер телефона:",
        reply_markup=reply_markup
    )
    return REG_PHONE

def reg_phone(update: Update, context: CallbackContext) -> int:
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = update.message.text.strip()
    phone = phone.replace(" ", "")

    if not PHONE_REGEX.match(phone):
        update.message.reply_text("Неверный формат номера. Введите номер в формате +32XXXXXXXXX или 0XXXXXXXXX:")
        return REG_PHONE

    context.user_data['phone'] = phone
    update.message.reply_text("Введите ваше ФИО:", reply_markup=ReplyKeyboardRemove())
    return REG_FIO

def reg_fio(update: Update, context: CallbackContext) -> int:
    context.user_data['fio'] = update.message.text.strip()
    update.message.reply_text("Регистрация завершена.")

    user_id = update.effective_user.id

    # Запис у bot_data
    if "registered_users" not in context.bot_data:
        context.bot_data["registered_users"] = {}

    context.bot_data["registered_users"][user_id] = {
        "phone": context.user_data['phone'],
        "fio": context.user_data['fio'],
        "car": ""
    }

    save_registered_user(user_id, context.user_data['phone'], context.user_data['fio'])
    send_main_menu(user_id, context)
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext) -> int:
    """Команда /cancel завершує розмову, прибирає клавіатуру."""
    update.message.reply_text("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ======================================================
# Початок зміни
# ======================================================
WS_DRIVING_CHOICE = 11
WS_WAITING_FOR_START_MILEAGE = 12
WS_CHOOSE_CAR = 13
WS_WAITING_FOR_LOCATION = 14

def start_work_entry(update: Update, context: CallbackContext) -> int:
    """Натиснули «Приступаю». Запитуємо, чи за кермом."""
    reply_markup = ReplyKeyboardMarkup([["Да", "Нет"]], one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text("Вы будете за рулём?", reply_markup=reply_markup)
    return WS_DRIVING_CHOICE

def ws_driving_choice_response(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip().lower()
    user_id = update.effective_user.id

    if text == "да":
        # Якщо за кермом
        update.message.reply_text("Введите начальный пробег автомобиля:", reply_markup=ReplyKeyboardRemove())
        return WS_WAITING_FOR_START_MILEAGE
    else:
        context.dispatcher.user_data.setdefault(user_id, {})
        context.dispatcher.user_data[user_id]["car"] = "-"
        context.dispatcher.user_data[user_id]["start_mileage"] = "-"
        # Запит локації
        update.message.reply_text(
            "Отправьте, пожалуйста, свою геолокацию для начала смены.",
            reply_markup=get_location_keyboard()
        )
        return WS_WAITING_FOR_LOCATION

def ws_waiting_for_start_mileage(update: Update, context: CallbackContext) -> int:
    start_mileage = update.message.text.strip()
    if not start_mileage.isdigit():
        update.message.reply_text("Неверный формат. Введите, пожалуйста, только числа для пробега.")
        return WS_WAITING_FOR_START_MILEAGE

    user_id = update.effective_user.id
    context.dispatcher.user_data.setdefault(user_id, {})
    context.dispatcher.user_data[user_id]["start_mileage"] = start_mileage

    # Пропонуємо вибрати авто
    cars = load_cars()
    if not cars:
        update.message.reply_text("Список автомобилей пуст. Обратитесь к администратору.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    reply_markup = ReplyKeyboardMarkup([[car] for car in cars], one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text("Выберите автомобиль из списка:", reply_markup=reply_markup)
    return WS_CHOOSE_CAR

def ws_choose_car(update: Update, context: CallbackContext) -> int:
    chosen_car = update.message.text.strip()
    cars = load_cars()
    if chosen_car not in cars:
        # Якщо невірно обрано
        reply_markup = ReplyKeyboardMarkup([[car] for car in cars], one_time_keyboard=True, resize_keyboard=True)
        update.message.reply_text("Выбранный автомобиль отсутствует в списке. Пожалуйста, выберите автомобиль:", reply_markup=reply_markup)
        return WS_CHOOSE_CAR

    user_id = update.effective_user.id
    context.dispatcher.user_data[user_id]["car"] = chosen_car

    update.message.reply_text(
        "Отправьте, пожалуйста, свою геолокацию для начала смены.",
        reply_markup=get_location_keyboard()
    )
    return WS_WAITING_FOR_LOCATION

def ws_receive_location(update: Update, context: CallbackContext) -> int:
    """Отримуємо локацію для початку зміни."""
    loc = update.message.location
    if not loc:
        update.message.reply_text(
            "Геолокация не получена. Нажмите кнопку 'Поделиться геолокацией'.",
            reply_markup=get_location_keyboard()
        )
        return WS_WAITING_FOR_LOCATION

    user_id = update.effective_user.id
    now_time = now_belgium().strftime("%H:%M:%S")
    start_coords = f"{loc.latitude}, {loc.longitude}"

    # Якщо чомусь не завантажені
    if "registered_users" not in context.bot_data:
        context.bot_data["registered_users"] = load_registered_users()

    reg_data = context.bot_data["registered_users"][user_id]
    worker = {
        "fio": reg_data["fio"],
        "phone": reg_data["phone"],
        "car": context.dispatcher.user_data[user_id].get("car", "-"),
        "start_mileage": context.dispatcher.user_data[user_id].get("start_mileage", "-"),
    }

    sheet = get_today_sheet(context)
    header_row = get_worker_block_header_row(sheet, worker["phone"].lstrip("+"))
    if header_row is None:
        all_values = sheet.get_all_values()
        start_row = len(all_values) + 2
        _, header_row = create_worker_block(sheet, worker, start_row)

    shift_info = {
        "car": worker["car"] if worker["car"] else "-",
        "start_mileage": worker["start_mileage"] if worker["start_mileage"] else "-",
        "start_time": now_time,
        "start_coords": start_coords,
    }
    update_shift_row(sheet, header_row, shift_info)

    context.dispatcher.user_data[user_id]["sheet_header_row"] = header_row
    context.dispatcher.user_data[user_id]["shift_start_dt"] = now_belgium()

    # Позначаємо зміну як активну
    active_work = context.bot_data.get("active_work", {})
    active_work[user_id] = True
    context.bot_data["active_work"] = active_work

    # Плануємо запити 3 та 6 годин
    schedule_intermediate_jobs(user_id, context)

    update.message.reply_text("Рабочий день начат. Данные записаны.", reply_markup=ReplyKeyboardRemove())
    send_main_menu(user_id, context)
    return ConversationHandler.END


# ======================================================
# Завершення зміни
# ======================================================
def finish_work_entry(update: Update, context: CallbackContext) -> int:
    """Починаємо процес завершення зміни."""
    user_id = update.effective_user.id
    # Ставимо finishing_mode
    context.dispatcher.user_data.setdefault(user_id, {})
    context.dispatcher.user_data[user_id]["finishing_mode"] = True

    update.message.reply_text(
        "Вы завершаете рабочий день.\nОтправьте, пожалуйста, свою геолокацию для завершения смены.",
        reply_markup=get_location_keyboard()
    )
    return WE_WAITING_FOR_LOCATION

def we_receive_location(update: Update, context: CallbackContext) -> int:
    loc = update.message.location
    if not loc:
        update.message.reply_text(
            "Геолокация не получена. Нажмите кнопку 'Поделиться геолокацией'.",
            reply_markup=get_location_keyboard()
        )
        return WE_WAITING_FOR_LOCATION

    user_id = update.effective_user.id
    finish_coords = f"{loc.latitude}, {loc.longitude}"
    context.dispatcher.user_data[user_id]["finish_coords"] = finish_coords

    # Якщо водій
    if context.dispatcher.user_data[user_id].get("car", "") != "-":
        update.message.reply_text("Введите конечный пробег автомобиля:")
        return WE_WAITING_FOR_MILEAGE
    else:
        # Якщо не за кермом - одразу завершуємо
        return record_finish(update, context, mileage="-")

def we_receive_mileage(update: Update, context: CallbackContext) -> int:
    mileage = update.message.text.strip()
    if not mileage.isdigit():
        update.message.reply_text("Введите, пожалуйста, числовое значение для конечного пробега.")
        return WE_WAITING_FOR_MILEAGE

    return record_finish(update, context, mileage)

def record_finish(update: Update, context: CallbackContext, mileage: str) -> int:
    """Записуємо дані фінішу. Прибираємо клавіатуру, показуємо «Приступаю»."""
    user_id = update.effective_user.id
    finish_time = now_belgium().strftime("%H:%M:%S")

    header_row = context.dispatcher.user_data[user_id].get("sheet_header_row")
    if not header_row:
        update.message.reply_text("Ошибка: запись текущей смены не найдена.", reply_markup=ReplyKeyboardRemove())
        context.dispatcher.user_data[user_id]["finishing_mode"] = False
        return ConversationHandler.END

    sheet = get_today_sheet(context)
    current_day = now_belgium().day
    target_row = header_row + current_day

    sheet.update_cell(target_row, 10, finish_time)
    sheet.update_cell(target_row, 11, context.dispatcher.user_data[user_id].get("finish_coords", ""))
    sheet.update_cell(target_row, 12, mileage)

    # Скасовуємо завдання 3 і 6 год
    cancel_intermediate_jobs(user_id, context)

    # Зміна більше не активна
    active_work = context.bot_data.get("active_work", {})
    active_work[user_id] = False
    context.bot_data["active_work"] = active_work

    # Вимикаємо finishing_mode
    context.dispatcher.user_data[user_id]["finishing_mode"] = False

    # Прибираємо поточну клавіатуру (щоб «Поделиться геолокацией» зникла)
    update.message.reply_text(
        "Рабочий день завершён. Данные сохранены.",
        reply_markup=ReplyKeyboardRemove()
    )
    # Тепер надсилаємо головне меню, де буде «Приступаю»
    send_main_menu(user_id, context)
    return ConversationHandler.END


# ======================================================
# Проміжні локації (3 та 6 годин)
# ======================================================
def intermediate_geo_request(context: CallbackContext):
    """Функція, яку job_queue викликає за 3 і 6 годин."""
    user_id = context.job.context
    active_work = context.bot_data.get("active_work", {})
    if active_work.get(user_id, False):
        context.bot.send_message(
            chat_id=user_id,
            text="Пожалуйста, отправьте вашу промежуточную геолокацию (используйте кнопку 'Поделиться геолокацией').",
            reply_markup=get_location_keyboard()
        )

def schedule_intermediate_jobs(user_id: int, context: CallbackContext):
    jobs = []
    delays = [3*3600, 6*3600]
    for delay in delays:
        job = context.job_queue.run_once(intermediate_geo_request, delay, context=user_id)
        jobs.append(job)

    context.dispatcher.user_data.setdefault(user_id, {})
    context.dispatcher.user_data[user_id]["intermediate_jobs"] = jobs

def cancel_intermediate_jobs(user_id: int, context: CallbackContext):
    if user_id in context.dispatcher.user_data:
        jobs = context.dispatcher.user_data[user_id].get("intermediate_jobs", [])
        for job in jobs:
            job.schedule_removal()
        context.dispatcher.user_data[user_id]["intermediate_jobs"] = []


# ======================================================
# Прийом локації поза основним ланцюгом (default_location_handler)
# ======================================================
def default_location_handler(update: Update, context: CallbackContext) -> None:
    """
    Якщо надсилають локацію поза процесом початку/завершення.
    Якщо finishing_mode=True, ігноруємо, щоб не заважати завершенню.
    """
    user_id = update.effective_user.id

    # Якщо зміна не активна => ігноруємо
    if not context.bot_data.get("active_work", {}).get(user_id, False):
        return

    user_data = context.dispatcher.user_data.get(user_id, {})

    # Якщо вже завершує, ігноруємо
    if user_data.get("finishing_mode", False):
        return

    if "sheet_header_row" not in user_data or "shift_start_dt" not in user_data:
        return

    shift_start_dt = user_data["shift_start_dt"]
    # Якщо менше 5 хвилин — ігноруємо
    if (now_belgium() - shift_start_dt).total_seconds() < 300:
        return

    # Якщо вже було 2 проміжні локації — ігноруємо
    intermediate_count = user_data.get("intermediate_count", 0)
    if intermediate_count >= 2:
        return

    loc = update.message.location
    if loc:
        header_row = user_data["sheet_header_row"]
        sheet = get_today_sheet(context)
        current_day = now_belgium().day
        target_row = header_row + current_day

        # col=8 => «Промеж 3 часа», col=9 => «Промеж 6 часов»
        col = 8 if intermediate_count == 0 else 9
        geo_str = f"{loc.latitude}, {loc.longitude}"
        sheet.update_cell(target_row, col, geo_str)
        user_data["intermediate_count"] = intermediate_count + 1

        update.message.reply_text(
            f"Промежуточная геолокация {intermediate_count+1} записана.",
            reply_markup=ReplyKeyboardRemove()
        )
        # Знову показуємо меню
        send_main_menu(user_id, context)


# ======================================================
# Решта команд
# ======================================================
def menu_command(update: Update, context: CallbackContext) -> None:
    """Команда /menu."""
    send_main_menu(update.message.chat_id, context)

def inactive_shift_button_handler(update: Update, context: CallbackContext) -> None:
    """Якщо кнопка «Идёт смена» натиснута до завершення 1 години."""
    update.message.reply_text("Смена еще не длится 1 час. Для завершения смены подождите, пожалуйста.")


# ======================================================
# Головна функція запуску
# ======================================================
def main() -> None:
    # Видаляємо Webhook, якщо було
    bot = Bot(token=BOT_TOKEN)
    bot.delete_webhook()

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.bot_data["registered_users"] = load_registered_users()
    dp.bot_data["active_work"] = {}

    # ---- Реєстрація
    reg_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            REG_PHONE: [
                MessageHandler(Filters.contact | (Filters.text & ~Filters.command), reg_phone)
            ],
            REG_FIO: [
                MessageHandler(Filters.text & ~Filters.command, reg_fio)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    dp.add_handler(reg_handler)

    # ---- Початок зміни
    work_start_handler = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^Приступаю$"), start_work_entry)],
        states={
            WS_DRIVING_CHOICE: [
                MessageHandler(Filters.regex("^(Да|Нет)$"), ws_driving_choice_response)
            ],
            WS_WAITING_FOR_START_MILEAGE: [
                MessageHandler(Filters.text & ~Filters.command, ws_waiting_for_start_mileage)
            ],
            WS_CHOOSE_CAR: [
                MessageHandler(Filters.text & ~Filters.command, ws_choose_car)
            ],
            WS_WAITING_FOR_LOCATION: [
                MessageHandler(Filters.location, ws_receive_location)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    dp.add_handler(work_start_handler)

    # ---- Завершення зміни
    work_end_handler = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^Завершаю$"), finish_work_entry)],
        states={
            WE_WAITING_FOR_LOCATION: [
                MessageHandler(Filters.location, we_receive_location)
            ],
            WE_WAITING_FOR_MILEAGE: [
                MessageHandler(Filters.text & ~Filters.command, we_receive_mileage)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    dp.add_handler(work_end_handler)

    # Обробка локації поза розмовами: проміжні локації
    dp.add_handler(MessageHandler(Filters.location, default_location_handler), group=1)

    # /menu
    dp.add_handler(CommandHandler('menu', menu_command))

    # «Идёт смена»
    dp.add_handler(MessageHandler(Filters.regex("^Идёт смена$"), inactive_shift_button_handler))

    # Запускаємо long-polling
    updater.start_polling(drop_pending_updates=True)
    updater.idle()


if __name__ == '__main__':
    main()
