import logging
import datetime
import re
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    ConversationHandler,
    CallbackContext,
)

# Імпорт функцій для роботи з таблицею
from sheets_helper import get_today_sheet, create_shift_record, get_month_sheet, add_worker_block

# Завантаження змінних оточення
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------------
# Визначення станів розмови
REG_PHONE, REG_FIO = range(1, 3)
WS_DRIVING_CHOICE = 11
WS_WAITING_FOR_START_MILEAGE = 12
WS_CHOOSE_CAR = 13
WS_WAITING_FOR_LOCATION = 14
WE_WAITING_FOR_LOCATION, WE_WAITING_FOR_MILEAGE = range(20, 22)
INTERMEDIATE_WAITING_FOR_LOCATION = 30

PHONE_REGEX = re.compile(r'^(?:\+32\d{8,9}|0\d{9})$')

# ------------------------------
# Завантаження зареєстрованих користувачів з файлу users.txt
def load_registered_users():
    users = {}
    if os.path.exists("users.txt"):
        with open("users.txt", "r", encoding="utf-8") as f:
            for line in f:
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

# ------------------------------
# Завантаження списку автомобілів з файлу cars.txt
def load_cars():
    if not os.path.exists("cars.txt"):
        with open("cars.txt", "w", encoding="utf-8") as f:
            f.write("Марка авто, Колір, Номер авто\n")
            f.write("Toyota, Red, ABC123\n")
    cars = []
    with open("cars.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("Марка") or not line:
                continue
            cars.append(line)
    return cars

# ------------------------------
# Reply-клавіатура для кнопки "Поделиться геолокацией"
def get_location_keyboard():
    button = KeyboardButton("Поделиться геолокацией", request_location=True)
    return ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)

# ------------------------------
# Головне меню (reply-клавіатура)
def get_main_menu_reply_keyboard(user_id: int, context: CallbackContext):
    active_work = context.bot_data.get("active_work", {})
    if not active_work.get(user_id, False):
        keyboard = [["Приступаю"]]
    else:
        user_data = context.dispatcher.user_data.get(user_id, {})
        shift_start_dt = user_data.get("shift_start_dt")
        if shift_start_dt and (datetime.datetime.now() - shift_start_dt).total_seconds() >= 3600:
            keyboard = [["Завершаю"]]
        else:
            keyboard = [["Идёт смена"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def send_main_menu(user_id: int, context: CallbackContext) -> None:
    context.bot.send_message(chat_id=user_id, text="Выберите действие:", 
                             reply_markup=get_main_menu_reply_keyboard(user_id, context))

# ------------------------------
# Робота з таблицею через sheets_helper.py

# ------------------------------
# Реєстрація користувача
def start_command(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    if "registered_users" not in context.bot_data:
        context.bot_data["registered_users"] = load_registered_users()
    if user_id in context.bot_data["registered_users"]:
        update.message.reply_text("Вы уже зарегистрированы!")
        send_main_menu(user_id, context)
        return ConversationHandler.END
    button = KeyboardButton("Поделиться номером", request_contact=True)
    reply_markup = ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text("Введите, пожалуйста, ваш бельгийский номер телефона:", reply_markup=reply_markup)
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
    context.bot_data["registered_users"][user_id] = {
        "phone": context.user_data.get('phone'),
        "fio": context.user_data.get('fio'),
        "car": ""
    }
    try:
        with open("users.txt", "a", encoding="utf-8") as f:
            f.write(f"{user_id}, {context.user_data.get('phone')}, {context.user_data.get('fio')}, \n")
    except Exception as e:
        logger.error(f"Ошибка записи в файл users.txt: {e}")
    send_main_menu(user_id, context)
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ------------------------------
# Процес "Приступаю" (початок зміни)
def start_work_entry(update: Update, context: CallbackContext) -> int:
    reply_markup = ReplyKeyboardMarkup([["Да", "Нет"]], one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text("Вы будете за рулём?", reply_markup=reply_markup)
    return WS_DRIVING_CHOICE

def ws_driving_choice_response(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip().lower()
    if text == "да":
        update.message.reply_text("Введите начальный пробег автомобиля:")
        return WS_WAITING_FOR_START_MILEAGE
    else:
        user_id = update.effective_user.id
        if user_id not in context.dispatcher.user_data:
            context.dispatcher.user_data[user_id] = {}
        context.dispatcher.user_data[user_id]["car"] = "-"
        context.dispatcher.user_data[user_id]["start_mileage"] = "-"
        update.message.reply_text("Отправьте, пожалуйста, свою геолокацию для начала смены.", reply_markup=get_location_keyboard())
        return WS_WAITING_FOR_LOCATION

def ws_waiting_for_start_mileage(update: Update, context: CallbackContext) -> int:
    start_mileage = update.message.text.strip()
    user_id = update.effective_user.id
    if user_id not in context.dispatcher.user_data:
        context.dispatcher.user_data[user_id] = {}
    context.dispatcher.user_data[user_id]["start_mileage"] = start_mileage
    cars = load_cars()
    if not cars:
        update.message.reply_text("Список автомобилей пуст. Обратитесь к администратору.")
        return ConversationHandler.END
    reply_markup = ReplyKeyboardMarkup([[car] for car in cars], one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text("Выберите автомобиль из списка:", reply_markup=reply_markup)
    return WS_CHOOSE_CAR

def ws_choose_car(update: Update, context: CallbackContext) -> int:
    chosen_car = update.message.text.strip()
    cars = load_cars()
    if chosen_car not in cars:
        reply_markup = ReplyKeyboardMarkup([[car] for car in cars], one_time_keyboard=True, resize_keyboard=True)
        update.message.reply_text("Выбранный автомобиль отсутствует в списке. Пожалуйста, выберите автомобиль:", reply_markup=reply_markup)
        return WS_CHOOSE_CAR
    user_id = update.effective_user.id
    if user_id not in context.dispatcher.user_data:
        context.dispatcher.user_data[user_id] = {}
    context.dispatcher.user_data[user_id]["car"] = chosen_car
    update.message.reply_text("Отправьте, пожалуйста, свою геолокацию для начала смены.", reply_markup=get_location_keyboard())
    return WS_WAITING_FOR_LOCATION

def ws_receive_location(update: Update, context: CallbackContext) -> int:
    loc = update.message.location
    if not loc:
        update.message.reply_text("Геолокация не получена. Нажмите кнопку 'Поделиться геолокацией'.", reply_markup=get_location_keyboard())
        return WS_WAITING_FOR_LOCATION
    user_id = update.effective_user.id
    now = datetime.datetime.now().strftime("%H:%M:%S")
    start_coords = f"{loc.latitude}, {loc.longitude}"
    reg_data = context.bot_data["registered_users"][user_id]
    fio = reg_data["fio"]
    if context.dispatcher.user_data[user_id].get("car") != "-":
        car = context.dispatcher.user_data[user_id].get("car", "")
        start_mileage = context.dispatcher.user_data[user_id].get("start_mileage", "")
    else:
        car = "-"
        start_mileage = "-"
    sheet = get_today_sheet(context)
    existing_data = sheet.get_all_values()
    if len(existing_data) < 2:
        start_row = 2
    else:
        start_row = len(existing_data) + 1
    worker_data = {
        "fio": fio,
        "phone": reg_data["phone"],
        "car": car,
        "start_mileage": start_mileage
    }
    next_free_row = add_worker_block(sheet, worker_data, start_row, start_time=now, start_coords=start_coords)
    d = datetime.datetime.now().day
    sheet_row = start_row + d
    context.dispatcher.user_data[user_id]["sheet_row"] = sheet_row
    context.dispatcher.user_data[user_id]["intermediate_count"] = 0
    context.dispatcher.user_data[user_id]["shift_start_dt"] = datetime.datetime.now()
    active_work = context.bot_data.get("active_work", {})
    active_work[user_id] = True
    context.bot_data["active_work"] = active_work
    schedule_intermediate_jobs(user_id, context)
    update.message.reply_text("Рабочий день начат.", reply_markup=ReplyKeyboardRemove())
    send_main_menu(user_id, context)
    return ConversationHandler.END

def finish_work_entry(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("Вы завершаете рабочий день.\nОтправьте, пожалуйста, свою геолокацию для завершения смены.", reply_markup=get_location_keyboard())
    return WE_WAITING_FOR_LOCATION

def we_receive_location(update: Update, context: CallbackContext) -> int:
    loc = update.message.location
    if not loc:
        update.message.reply_text("Геолокация не получена. Нажмите кнопку 'Поделиться геолокацией'.", reply_markup=get_location_keyboard())
        return WE_WAITING_FOR_LOCATION
    finish_coords = f"{loc.latitude}, {loc.longitude}"
    user_id = update.effective_user.id
    context.dispatcher.user_data[user_id]["finish_coords"] = finish_coords
    if context.dispatcher.user_data[user_id].get("car", "") != "-":
        update.message.reply_text("Введите конечный пробег автомобиля:")
        return WE_WAITING_FOR_MILEAGE
    else:
        return record_finish(update, context, mileage="-")

def we_receive_mileage(update: Update, context: CallbackContext) -> int:
    mileage = update.message.text.strip()
    return record_finish(update, context, mileage)

def record_finish(update: Update, context: CallbackContext, mileage: str) -> int:
    user_id = update.effective_user.id
    finish_time = datetime.datetime.now().strftime("%H:%M:%S")
    sheet_row = context.dispatcher.user_data[user_id].get("sheet_row")
    if not sheet_row:
        update.message.reply_text("Ошибка: запись текущей смены не найдена.")
        return ConversationHandler.END
    sheet = get_today_sheet(context)
    sheet.update_cell(sheet_row, 10, finish_time)
    sheet.update_cell(sheet_row, 11, context.dispatcher.user_data[user_id].get("finish_coords", ""))
    sheet.update_cell(sheet_row, 12, mileage)
    cancel_intermediate_jobs(user_id, context)
    active_work = context.bot_data.get("active_work", {})
    active_work[user_id] = False
    context.bot_data["active_work"] = active_work
    update.message.reply_text("Рабочий день завершён. Данные сохранены.", reply_markup=ReplyKeyboardRemove())
    send_main_menu(user_id, context)
    return ConversationHandler.END

def intermediate_geo_request(context: CallbackContext):
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
    if user_id not in context.dispatcher.user_data:
        context.dispatcher.user_data[user_id] = {}
    context.dispatcher.user_data[user_id]["intermediate_jobs"] = jobs

def cancel_intermediate_jobs(user_id: int, context: CallbackContext):
    if user_id in context.dispatcher.user_data:
        jobs = context.dispatcher.user_data[user_id].get("intermediate_jobs", [])
        for job in jobs:
            job.schedule_removal()
        context.dispatcher.user_data[user_id]["intermediate_jobs"] = []

def default_location_handler(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    if not context.bot_data.get("active_work", {}).get(user_id, False):
        return
    user_data = context.dispatcher.user_data.get(user_id, {})
    if "sheet_row" not in user_data or "shift_start_dt" not in user_data:
        return
    shift_start_dt = user_data["shift_start_dt"]
    if (datetime.datetime.now() - shift_start_dt).total_seconds() < 300:
        return
    intermediate_count = user_data.get("intermediate_count", 0)
    if intermediate_count >= 2:
        return
    loc = update.message.location
    if loc:
        sheet_row = user_data.get("sheet_row")
        sheet = get_today_sheet(context)
        col = 8 + intermediate_count
        geo_str = f"{loc.latitude}, {loc.longitude}"
        sheet.update_cell(sheet_row, col, geo_str)
        user_data["intermediate_count"] = intermediate_count + 1
        update.message.reply_text(f"Промежуточная геолокация {intermediate_count+1} записана.", reply_markup=ReplyKeyboardRemove())

def menu_command(update: Update, context: CallbackContext) -> None:
    send_main_menu(update.message.chat_id, context)

def main() -> None:
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    reg_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            REG_PHONE: [MessageHandler(Filters.contact | (Filters.text & ~Filters.command), reg_phone)],
            REG_FIO: [MessageHandler(Filters.text & ~Filters.command, reg_fio)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    dp.add_handler(reg_handler)

    work_start_handler = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^Приступаю$"), start_work_entry)],
        states={
            WS_DRIVING_CHOICE: [MessageHandler(Filters.regex("^(Да|Нет)$"), ws_driving_choice_response)],
            WS_WAITING_FOR_START_MILEAGE: [MessageHandler(Filters.text & ~Filters.command, ws_waiting_for_start_mileage)],
            WS_CHOOSE_CAR: [MessageHandler(Filters.text & ~Filters.command, ws_choose_car)],
            WS_WAITING_FOR_LOCATION: [MessageHandler(Filters.location, ws_receive_location)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    dp.add_handler(work_start_handler)

    work_end_handler = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^Завершаю$"), finish_work_entry)],
        states={
            WE_WAITING_FOR_LOCATION: [MessageHandler(Filters.location, we_receive_location)],
            WE_WAITING_FOR_MILEAGE: [MessageHandler(Filters.text & ~Filters.command, we_receive_mileage)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    dp.add_handler(work_end_handler)

    dp.add_handler(MessageHandler(Filters.location, default_location_handler), group=1)
    dp.add_handler(CommandHandler('menu', menu_command))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
