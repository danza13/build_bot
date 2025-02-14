import datetime
import calendar
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread_formatting import (
    CellFormat,
    TextFormat,
    Color,
    format_cell_range,
)

# Словник назв місяців російською
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
    CREDENTIALS_JSON = os.getenv("credentials", "")
    if not CREDENTIALS_JSON:
        raise ValueError("Environment variable 'credentials' not found.")

    creds_dict = json.loads(CREDENTIALS_JSON)

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client

def get_month_sheet():
    client = get_gspread_client()
    # Замість відкривати за назвою spreadsheet, відкриємо за ІД
    # Ось варіант open_by_key з ID:
    spreadsheet = client.open_by_key("1FojL9Buaw2MxE1V9zFpeXYwM75ym1MLHeIq44OFn_H4")

    # (Або можна було б так:
    #   spreadsheet = client.open_by_url(
    #       "https://docs.google.com/spreadsheets/d/1FojL9Buaw2MxE1V9zFpeXYwM75ym1MLHeIq44OFn_H4/edit?usp=sharing"
    #   )
    # )

    now = datetime.datetime.now()
    month_name = MONTH_NAMES.get(now.month, "Unknown")
    try:
        sheet = spreadsheet.worksheet(month_name)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=month_name, rows="1000", cols="20")
    return sheet

def get_today_sheet(context=None):
    return get_month_sheet()

def get_days_in_month():
    now = datetime.datetime.now()
    return calendar.monthrange(now.year, now.month)[1]

def merge_cells(sheet, range_str):
    sheet.merge_cells(range_str)

def get_worker_block_header_row(sheet, phone):
    normalized_phone = phone.lstrip("+")
    try:
        cells = sheet.findall(normalized_phone)
        if cells:
            return cells[0].row - 1
        else:
            return None
    except Exception:
        return None

def create_worker_block(sheet, worker, start_row):
    days = get_days_in_month()
    header_row = start_row
    data_start = header_row + 1
    data_end = header_row + days

    # Заголовок (B:M)
    headers = [["ФИО", "Номер телефона", "АВТО", "Начальный пробег",
                "Время начала", "Координаты начала",
                "Промеж 3 часа", "Промеж 6 часов",
                "Время окончания", "Координаты конец",
                "Конечный пробег", "Дата"]]
    sheet.batch_update([{
        'range': f"B{header_row}:M{header_row}",
        'values': headers
    }])

    # Дати в стовпчику M
    now = datetime.datetime.now()
    date_updates = []
    for i in range(1, days + 1):
        day_str = f"{i:02d}.{now.month:02d}"
        cell = f"M{data_start + i - 1}"
        date_updates.append({'range': cell, 'values': [[day_str]]})
    if date_updates:
        sheet.batch_update(date_updates)

    # Об’єднання клітинок для ФИО (B) і Телефона (C)
    merge_cells(sheet, f"B{data_start}:B{data_end}")
    merge_cells(sheet, f"C{data_start}:C{data_end}")
    sheet.update_acell(f"B{data_start}", worker["fio"])
    sheet.update_acell(f"C{data_start}", worker["phone"].lstrip("+"))

    # Форматування
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

    # Рамки
    border_request = {
      "updateBorders": {
        "range": {
          "sheetId": sheet.id,
          "startRowIndex": header_row - 1,
          "endRowIndex": data_end,
          "startColumnIndex": 1,
          "endColumnIndex": 13
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

    # Рядок-розділювач (висота 30)
    gap_request = {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet.id,
                "dimension": "ROWS",
                "startIndex": data_end,
                "endIndex": data_end + 1
            },
            "properties": {
                "pixelSize": 30
            },
            "fields": "pixelSize"
        }
    }
    sheet.spreadsheet.batch_update({"requests": [gap_request]})

    # Ширина стовпців
    column_width_requests = [
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet.id,
                    "dimension": "COLUMNS",
                    "startIndex": 1,   # B
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
                    "startIndex": 2,   # C
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
                    "startIndex": 3,   # D - L
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
                    "startIndex": 12,  # M
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

    next_free_row = data_end + 2
    return next_free_row, header_row

def update_shift_row(sheet, header_row, shift_info):
    current_day = datetime.datetime.now().day
    target_row = header_row + current_day

    if shift_info.get("no_shift", False):
        for col in range(4, 13):
            sheet.update_cell(target_row, col, "-")
        return

    sheet.update_cell(target_row, 4, shift_info.get("car", "-"))
    sheet.update_cell(target_row, 5, shift_info.get("start_mileage", "-"))
    sheet.update_cell(target_row, 6, shift_info.get("start_time", "-"))
    sheet.update_cell(target_row, 7, shift_info.get("start_coords", "-"))
