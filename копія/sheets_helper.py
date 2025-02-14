import datetime
import calendar
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread_formatting import (
    CellFormat,
    TextFormat,
    Color,
    format_cell_range,
)
from dotenv import load_dotenv

# Завантаження змінних оточення
load_dotenv()
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")

# Словник назв місяців російською мовою
MONTH_NAMES = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь"
}

def get_gspread_client():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS, scope)
    client = gspread.authorize(creds)
    return client

def get_month_sheet():
    """
    Повертає об’єкт Worksheet для поточного місяця.
    Назва листа встановлюється за MONTH_NAMES (наприклад, "Февраль").
    Якщо такого листа немає, він створюється.
    """
    client = get_gspread_client()
    spreadsheet = client.open("BUILD")
    now = datetime.datetime.now()
    month_name = MONTH_NAMES.get(now.month, "Unknown")
    try:
        sheet = spreadsheet.worksheet(month_name)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=month_name, rows="1000", cols="20")
    return sheet

def get_today_sheet(context=None):
    """
    Для сумісності повертає лист поточного місяця.
    """
    return get_month_sheet()

def get_days_in_month():
    now = datetime.datetime.now()
    return calendar.monthrange(now.year, now.month)[1]

def merge_cells(sheet, range_str):
    """
    Об’єднує клітинки у вказаному діапазоні (наприклад, "B3:B33").
    """
    sheet.merge_cells(range_str)

def create_shift_record(fio, start_time, start_coords, car, start_mileage, context=None):
    """
    Створює запис зміни (однострочний) у листі поточного місяця.
    Запис включає ФИО, час початку, координати, автомобіль та початковий пробіг.
    Повертає номер доданого рядка.
    """
    sheet = get_today_sheet()
    row_data = [fio, start_time, start_coords, car, start_mileage, "", "", "", "", "", ""]
    sheet.append_row(row_data)
    all_values = sheet.get_all_values()
    return len(all_values)

def add_worker_block(sheet, worker, start_row, start_time=None, start_coords=None):
    """
    Додає блок для одного співробітника у лист.

    Блок розташовується у стовпцях B:M.
    Наприклад, якщо start_row = 2 і у місяці 31 день, блок буде B2:M33.
    """
    days = get_days_in_month()
    header_row = start_row
    data_start = start_row + 1
    data_end = start_row + days  # Наприклад, при 31 дні: рядки 3-33

    # 1. Заповнення заголовків
    headers = [["ФИО", "Номер телефона", "АВТО", "Начальный пробег", "Время начала",
                "Координаты начала", "Промеж 3 часа", "Промеж 6 часов", "Время окончания",
                "Координаты конец", "Конечный пробег", "Дата"]]
    sheet.batch_update([{
        'range': f"B{header_row}:M{header_row}",
        'values': headers
    }])
    
    # 2. Заповнення стовпця "Дата" для кожного дня (формат dd.mm)
    now = datetime.datetime.now()
    date_updates = []
    for i in range(1, days + 1):
        day_str = f"{i:02d}.{now.month:02d}"
        cell = f"M{data_start + i - 1}"
        date_updates.append({'range': cell, 'values': [[day_str]]})
    if date_updates:
        sheet.batch_update(date_updates)
    
    # 3. Об’єднання клітинок для ФИО (B) та номера телефона (C)
    merge_cells(sheet, f"B{data_start}:B{data_end}")
    merge_cells(sheet, f"C{data_start}:C{data_end}")
    sheet.update_acell(f"B{data_start}", worker["fio"])
    sheet.update_acell(f"C{data_start}", worker["phone"])
    
    # 4. Заповнення стовпців D, E, F та G лише для сьогоднішнього дня
    today = datetime.datetime.now().day  # Наприклад, сьогодні 13 число
    target_row = data_start + (today - 1)
    sheet.update_cell(target_row, 4, worker["car"] if worker["car"] != "" else "-")
    sheet.update_cell(target_row, 5, worker["start_mileage"] if worker["start_mileage"] != "" else "-")
    if start_time:
        sheet.update_cell(target_row, 6, start_time)  # Время начала (F)
    if start_coords:
        sheet.update_cell(target_row, 7, start_coords)  # Координаты начала (G)
    
    # 5. Форматування блоку за допомогою gspread_formatting
    general_format = CellFormat(
        backgroundColor=Color(0.97, 0.97, 0.97),
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE",
        textFormat=TextFormat(bold=True, fontSize=13)
    )
    block_range = f"B{header_row}:M{data_end}"
    format_cell_range(sheet, block_range, general_format)
    
    header_format = CellFormat(
        backgroundColor=Color(0.97, 0.97, 0.97),
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE",
        textFormat=TextFormat(bold=True, fontSize=15)
    )
    header_range = f"B{header_row}:M{header_row}"
    format_cell_range(sheet, header_range, header_format)
    
    phone_format = CellFormat(
        backgroundColor=Color(0.97, 0.97, 0.97),
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE",
        textFormat=TextFormat(bold=True, fontSize=17)
    )
    phone_range = f"C{data_start}:C{data_end}"
    format_cell_range(sheet, phone_range, phone_format)
    
    # 6. Створення зовнішніх та внутрішніх рамок за допомогою updateBorders
    border_request = {
      "updateBorders": {
        "range": {
          "sheetId": sheet.id,
          "startRowIndex": header_row - 1,
          "endRowIndex": data_end,
          "startColumnIndex": 1,  # Стовпець B
          "endColumnIndex": 13    # Стовпці B..M
        },
        "top": {
          "style": "SOLID_THICK",
          "width": 1,
          "color": {"red": 0, "green": 0, "blue": 0}
        },
        "bottom": {
          "style": "SOLID_THICK",
          "width": 1,
          "color": {"red": 0, "green": 0, "blue": 0}
        },
        "left": {
          "style": "SOLID_THICK",
          "width": 1,
          "color": {"red": 0, "green": 0, "blue": 0}
        },
        "right": {
          "style": "SOLID_THICK",
          "width": 1,
          "color": {"red": 0, "green": 0, "blue": 0}
        },
        "innerHorizontal": {
          "style": "SOLID",
          "width": 1,
          "color": {"red": 0, "green": 0, "blue": 0}
        },
        "innerVertical": {
          "style": "SOLID",
          "width": 1,
          "color": {"red": 0, "green": 0, "blue": 0}
        }
      }
    }
    sheet.spreadsheet.batch_update({"requests": [border_request]})
    
    # 7. Встановлення заданих розмірів стовпців
    # Стовпець B (індекс 1) - 300 пікселів
    # Стовпець C (індекс 2) - 180 пікселів
    # Стовпці D-L (індекси 3-11) - 200 пікселів
    # Стовпець M (індекс 12) - 70 пікселів
    column_width_requests = [
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet.id,
                    "dimension": "COLUMNS",
                    "startIndex": 1,   # Стовпець B
                    "endIndex": 2
                },
                "properties": {
                    "pixelSize": 300
                },
                "fields": "pixelSize"
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet.id,
                    "dimension": "COLUMNS",
                    "startIndex": 2,   # Стовпець C
                    "endIndex": 3
                },
                "properties": {
                    "pixelSize": 180
                },
                "fields": "pixelSize"
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet.id,
                    "dimension": "COLUMNS",
                    "startIndex": 3,   # Стовпці D-L (D: індекс 3, L: індекс 11)
                    "endIndex": 12
                },
                "properties": {
                    "pixelSize": 200
                },
                "fields": "pixelSize"
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet.id,
                    "dimension": "COLUMNS",
                    "startIndex": 12,  # Стовпець M
                    "endIndex": 13
                },
                "properties": {
                    "pixelSize": 70
                },
                "fields": "pixelSize"
            }
        }
    ]
    sheet.spreadsheet.batch_update({"requests": column_width_requests})
    
    # Автоматична підгонка висоти рядків (залишається без змін)
    resize_rows_request = {
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": sheet.id,
                "dimension": "ROWS",
                "startIndex": header_row - 1,
                "endIndex": data_end
            }
        }
    }
    sheet.spreadsheet.batch_update({"requests": [resize_rows_request]})
    
    # 8. Встановлення висоти для рядка між блоками (50 пікселів)
    gap_request = {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet.id,
                "dimension": "ROWS",
                "startIndex": data_end,
                "endIndex": data_end + 1
            },
            "properties": {
                "pixelSize": 50
            },
            "fields": "pixelSize"
        }
    }
    sheet.spreadsheet.batch_update({"requests": [gap_request]})
    
    next_free_row = data_end + 2
    return next_free_row

if __name__ == "__main__":
    # Тестовий приклад для одного співробітника
    sheet = get_month_sheet()
    start_row = 2  # перший блок починається з рядка 2
    worker_data = {
        "fio": "Иванов Иван Иванович",
        "phone": "+71234567890",
        "car": "Toyota Camry",
        "start_mileage": "10000"
    }
    next_row = add_worker_block(sheet, worker_data, start_row, start_time="08:00:00", start_coords="55.7558, 37.6176")
    print("Блок створено. Наступний вільний рядок:", next_row)
