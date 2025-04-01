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

# Ми не використовуємо більше PyDrive2 та GoogleDrive для збереження user-файлу
# from pydrive2.auth import GoogleAuth
# from pydrive2.drive import GoogleDrive
# from oauth2client.service_account import ServiceAccountCredentials

# Імпорт функцій для роботи з Google Sheet (не змінюємо, бо треба зберігати у Sheets)
from sheets_helper import (
    get_today_sheet,
    get_worker_block_header_row,
    create_worker_block,
    update_shift_row,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CREDENTIALS_JSON = os.getenv("credentials", "")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Вкажемо шлях до локального файлу зі списком користувачів (наприклад, /data/users.txt)
USERS_FILE_PATH = os.path.join("/data", "users.txt")

# Conversation states
REG_PHONE, REG_FIO = range(2)
WS_WAITING_FOR_LOCATION = 10
WE_WAITING_FOR_LOCATION = 20

PHONE_REGEX = re.compile(r'^(?:\+32\d{8,9}|0\d{9})$')


def now_belgium():
    """Return current local time in 'Europe/Brussels' timezone."""
    return datetime.datetime.now(ZoneInfo("Europe/Brussels"))


# ======================================================
# Local File Handling (instead of Google Drive for users.txt)
# ======================================================
def load_local_file(file_path: str) -> str:
    """Reads the content of a local file at file_path (if exists), else returns empty string."""
    if not os.path.exists(file_path):
        return ""
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()

def save_local_file(file_path: str, content: str) -> None:
    """Writes `content` to a local file at `file_path`."""
    # Створюємо теку /data, якщо вона не існує
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)


# ======================================================
# Users
# ======================================================
def load_registered_users():
    """
    Замість звернення до Google Drive,
    завантажуємо файл /data/users.txt безпосередньо з диска.
    """
    file_content = load_local_file(USERS_FILE_PATH)
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
        users[user_id] = {"phone": phone, "fio": fio}
    return users

def save_registered_user(user_id, phone, fio):
    """
    Зберігаємо файл /data/users.txt локально.
    """
    users_dict = load_registered_users()
    if user_id in users_dict:
        del users_dict[user_id]
    users_dict[user_id] = {"phone": phone, "fio": fio}

    lines = []
    for uid, data in users_dict.items():
        line = f"{uid}, {data['phone']}, {data['fio']}"
        lines.append(line)
    new_content = "\n".join(lines)
    save_local_file(USERS_FILE_PATH, new_content)


# ======================================================
# Keyboards and Main Menu
# ======================================================
def get_location_keyboard():
    button = KeyboardButton("Share location", request_location=True)
    return ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)

def get_main_menu_reply_keyboard(user_id: int, context: CallbackContext):
    """
    Build the main menu:
    - If shift is not active => "Start shift"
    - If active < 1 hour => "Shift in progress"
    - Else => "Finish shift"
    """
    active_work = context.bot_data.get("active_work", {})
    if not active_work.get(user_id, False):
        # Not active
        keyboard = [["Start shift"]]
    else:
        user_data = context.dispatcher.user_data.get(user_id, {})
        shift_start_dt = user_data.get("shift_start_dt")
        if shift_start_dt:
            elapsed = (now_belgium() - shift_start_dt).total_seconds()
            if elapsed >= 3600:
                keyboard = [["Finish shift"]]
            else:
                keyboard = [["Shift in progress"]]
        else:
            keyboard = [["Finish shift"]]  # fallback

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def send_main_menu(user_id: int, context: CallbackContext):
    context.bot.send_message(
        chat_id=user_id,
        text="Please choose an action:",
        reply_markup=get_main_menu_reply_keyboard(user_id, context)
    )


# ======================================================
# Registration
# ======================================================
def start_command(update: Update, context: CallbackContext) -> int:
    """Triggered by /start – Begin registration or show the main menu."""
    user_id = update.effective_user.id

    if "registered_users" not in context.bot_data:
        context.bot_data["registered_users"] = load_registered_users()

    if user_id in context.bot_data["registered_users"]:
        update.message.reply_text("You are already registered!")
        send_main_menu(user_id, context)
        return ConversationHandler.END

    button = KeyboardButton("Share my phone number", request_contact=True)
    reply_markup = ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text(
        "Please enter your Belgian phone number (+32XXXXXXXXX or 0XXXXXXXXX):",
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
        update.message.reply_text("Invalid format. Please try again in the format +32XXXXXXXXX or 0XXXXXXXXX:")
        return REG_PHONE

    context.user_data['phone'] = phone
    update.message.reply_text("Please enter your full name:", reply_markup=ReplyKeyboardRemove())
    return REG_FIO

def reg_fio(update: Update, context: CallbackContext) -> int:
    context.user_data['fio'] = update.message.text.strip()
    update.message.reply_text("Registration complete.")

    user_id = update.effective_user.id
    if "registered_users" not in context.bot_data:
        context.bot_data["registered_users"] = {}

    context.bot_data["registered_users"][user_id] = {
        "phone": context.user_data['phone'],
        "fio": context.user_data['fio']
    }

    save_registered_user(user_id, context.user_data['phone'], context.user_data['fio'])
    send_main_menu(user_id, context)
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext) -> int:
    """Triggered by /cancel – end conversation and remove keyboard."""
    update.message.reply_text("Action canceled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ======================================================
# Start Shift
# ======================================================
def start_work_entry(update: Update, context: CallbackContext) -> int:
    """User clicked 'Start shift'. Ask for location."""
    update.message.reply_text(
        "Please send your location to start your workday.",
        reply_markup=get_location_keyboard()
    )
    return WS_WAITING_FOR_LOCATION

def ws_receive_location(update: Update, context: CallbackContext) -> int:
    """Receive location to start shift."""
    loc = update.message.location
    if not loc:
        update.message.reply_text(
            "Location not received. Please press 'Share location'.",
            reply_markup=get_location_keyboard()
        )
        return WS_WAITING_FOR_LOCATION

    user_id = update.effective_user.id
    now_time = now_belgium().strftime("%H:%M:%S")
    start_coords = f"{loc.latitude}, {loc.longitude}"

    if "registered_users" not in context.bot_data:
        context.bot_data["registered_users"] = load_registered_users()

    reg_data = context.bot_data["registered_users"][user_id]
    worker = {
        "fio": reg_data["fio"],
        "phone": reg_data["phone"],
    }

    sheet = get_today_sheet(context)
    header_row = get_worker_block_header_row(sheet, worker["phone"].lstrip("+"))
    if header_row is None:
        all_values = sheet.get_all_values()
        start_row = len(all_values) + 2
        _, header_row = create_worker_block(sheet, worker, start_row)

    shift_info = {
        "start_time": now_time,
        "start_coords": start_coords,
    }
    update_shift_row(sheet, header_row, shift_info)

    context.dispatcher.user_data[user_id]["sheet_header_row"] = header_row
    context.dispatcher.user_data[user_id]["shift_start_dt"] = now_belgium()

    # Mark shift as active
    active_work = context.bot_data.get("active_work", {})
    active_work[user_id] = True
    context.bot_data["active_work"] = active_work

    # Schedule intermediate location requests (3h, 6h)
    schedule_intermediate_jobs(user_id, context)

    update.message.reply_text("Workday started. Data recorded.", reply_markup=ReplyKeyboardRemove())
    send_main_menu(user_id, context)
    return ConversationHandler.END


# ======================================================
# Finish Shift
# ======================================================
def finish_work_entry(update: Update, context: CallbackContext) -> int:
    """User clicked 'Finish shift'."""
    user_id = update.effective_user.id
    context.dispatcher.user_data.setdefault(user_id, {})
    context.dispatcher.user_data[user_id]["finishing_mode"] = True

    update.message.reply_text(
        "You are finishing your workday. Please share your location.",
        reply_markup=get_location_keyboard()
    )
    return WE_WAITING_FOR_LOCATION

def we_receive_location(update: Update, context: CallbackContext) -> int:
    """Receive final location to finish shift."""
    loc = update.message.location
    if not loc:
        update.message.reply_text(
            "Location not received. Please press 'Share location'.",
            reply_markup=get_location_keyboard()
        )
        return WE_WAITING_FOR_LOCATION

    user_id = update.effective_user.id
    finish_coords = f"{loc.latitude}, {loc.longitude}"
    context.dispatcher.user_data[user_id]["finish_coords"] = finish_coords

    return record_finish(update, context)

def record_finish(update: Update, context: CallbackContext) -> int:
    """Write finishing data to the sheet and reset status."""
    user_id = update.effective_user.id
    finish_time = now_belgium().strftime("%H:%M:%S")

    header_row = context.dispatcher.user_data[user_id].get("sheet_header_row")
    if not header_row:
        update.message.reply_text("Error: current shift data not found.", reply_markup=ReplyKeyboardRemove())
        context.dispatcher.user_data[user_id]["finishing_mode"] = False
        return ConversationHandler.END

    sheet = get_today_sheet(context)
    current_day = now_belgium().day
    target_row = header_row + current_day

    # Write finish time (H=8) and finish coords (I=9)
    sheet.update_cell(target_row, 8, finish_time)
    sheet.update_cell(target_row, 9, context.dispatcher.user_data[user_id].get("finish_coords", ""))

    # Cancel intermediate location jobs
    cancel_intermediate_jobs(user_id, context)

    # Mark shift as inactive
    active_work = context.bot_data.get("active_work", {})
    active_work[user_id] = False
    context.bot_data["active_work"] = active_work

    context.dispatcher.user_data[user_id]["finishing_mode"] = False

    update.message.reply_text(
        "Workday finished. Data saved.",
        reply_markup=ReplyKeyboardRemove()
    )
    send_main_menu(user_id, context)
    return ConversationHandler.END


# ======================================================
# Intermediate Location Requests (3h, 6h)
# ======================================================
def intermediate_geo_request(context: CallbackContext):
    """job_queue callback for requesting intermediate location at 3h and 6h."""
    user_id = context.job.context
    active_work = context.bot_data.get("active_work", {})
    if active_work.get(user_id, False):
        context.bot.send_message(
            chat_id=user_id,
            text="Please send your intermediate location (use 'Share location').",
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
# Default Location Handler (outside main conv)
# ======================================================
def default_location_handler(update: Update, context: CallbackContext) -> None:
    """
    If user sends location outside start/finish steps, it may be a 3h or 6h intermediate location.
    If finishing_mode is True, ignore.
    """
    user_id = update.effective_user.id

    # If shift not active => ignore
    if not context.bot_data.get("active_work", {}).get(user_id, False):
        return

    user_data = context.dispatcher.user_data.get(user_id, {})

    # If user is finishing, ignore
    if user_data.get("finishing_mode", False):
        return

    if "sheet_header_row" not in user_data or "shift_start_dt" not in user_data:
        return

    shift_start_dt = user_data["shift_start_dt"]
    # Ignore if shift started less than 5 min ago
    if (datetime.datetime.now(ZoneInfo("Europe/Brussels")) - shift_start_dt).total_seconds() < 300:
        return

    # If user has already sent 2 intermediate locations, ignore
    intermediate_count = user_data.get("intermediate_count", 0)
    if intermediate_count >= 2:
        return

    loc = update.message.location
    if loc:
        header_row = user_data["sheet_header_row"]
        sheet = get_today_sheet(context)
        current_day = now_belgium().day
        target_row = header_row + current_day

        # col=6 => Промеж 3 часа, col=7 => Промеж 6 часов
        col = 6 if intermediate_count == 0 else 7
        geo_str = f"{loc.latitude}, {loc.longitude}"
        sheet.update_cell(target_row, col, geo_str)
        user_data["intermediate_count"] = intermediate_count + 1

        update.message.reply_text(
            f"Intermediate location {intermediate_count+1} recorded.",
            reply_markup=ReplyKeyboardRemove()
        )
        send_main_menu(user_id, context)


# ======================================================
# Other Commands
# ======================================================
def menu_command(update: Update, context: CallbackContext) -> None:
    """Triggered by /menu."""
    send_main_menu(update.message.chat_id, context)

def inactive_shift_button_handler(update: Update, context: CallbackContext) -> None:
    """If 'Shift in progress' is tapped before 1 hour has passed."""
    update.message.reply_text("Your shift has not reached 1 hour yet. Please wait to finish the shift.")


# ======================================================
# Main
# ======================================================
def main() -> None:
    # Remove any existing webhook
    bot = Bot(token=BOT_TOKEN)
    bot.delete_webhook()

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.bot_data["registered_users"] = load_registered_users()
    dp.bot_data["active_work"] = {}

    # Registration
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

    # Start shift
    work_start_handler = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^Start shift$"), start_work_entry)],
        states={
            WS_WAITING_FOR_LOCATION: [
                MessageHandler(Filters.location, ws_receive_location)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    dp.add_handler(work_start_handler)

    # Finish shift
    work_end_handler = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^Finish shift$"), finish_work_entry)],
        states={
            WE_WAITING_FOR_LOCATION: [
                MessageHandler(Filters.location, we_receive_location)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    dp.add_handler(work_end_handler)

    # Handle location messages outside the main conversation (intermediate updates)
    dp.add_handler(MessageHandler(Filters.location, default_location_handler), group=1)

    # /menu
    dp.add_handler(CommandHandler('menu', menu_command))

    # "Shift in progress"
    dp.add_handler(MessageHandler(Filters.regex("^Shift in progress$"), inactive_shift_button_handler))

    # Start polling
    updater.start_polling(drop_pending_updates=True)
    updater.idle()


if __name__ == '__main__':
    main()
