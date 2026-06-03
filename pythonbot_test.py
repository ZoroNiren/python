from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.types import BotCommand
from aiogram import types
import asyncio
import json
import logging
import socket
import sys
from collections import OrderedDict
from datetime import datetime, timedelta

import os
from typing import List, Any
import math
import re

import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.utils.exceptions import BotBlocked, ChatNotFound, TerminatedByOtherGetUpdates, UserDeactivated

from database import (
    create_tables,
    add_reminder_column,
    add_balance_columns,
    add_project_schedule_columns,
    get_connection,
    is_user_onboarded,
    set_user_onboarded,
)

BOT_TOKEN = "8600363493:AAEizpBjAuZ9ACb4PuokyT388BcqqqUIGR0"

# ===== Google Sheets sync =====
GSHEETS_SYNC_ENABLED = True
GSHEETS_SYNC_INTERVAL_SECONDS = 30

# ID таблицы (из ссылки вида https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit)
GSHEETS_SPREADSHEET_ID = "137I8lTA2y5DtKcdPXrToZlfH1I-C3M9P3xgbPqajryk"

# Путь до JSON ключа Service Account
GSHEETS_SERVICE_ACCOUNT_JSON = "service_account.json"

GSHEETS_WORKSHEET_FINANCE = "Финансы"
GSHEETS_WORKSHEET_GOALS = "Цели и задачи"

TEST_MODE = True  # TEST BUILD
REPORT_INTERVAL_DAYS = 14
DOCUMENTS_INTERVAL_DAYS = 30
PERIOD_SECONDS = 60 if TEST_MODE else REPORT_INTERVAL_DAYS * 24 * 60 * 60

QUARTER_TEST_MODE = TEST_MODE
QUARTER_TEST_DELAY_SECONDS = 1200

GOOGLE_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLScffotegBelZK6crQFXjhm3r6aL68fSXXHLr8tstYkOHiEPDw/viewform?usp=publish-editor"

ADMIN_ID = 720940126  # ←←← ВСТАВЬТЕ СЮДА СВОЙ TELEGRAM ID АДМИНА

ONBOARDING_TEXT = (
    "Добро пожаловать!\n"
    "Этот бот помогает вести учет финансов и задач вашего проекта.\n\n"
    "Как пользоваться ботом 🧐:\n\n"

    "1) После старта введите название проекта или название стартапа.\n"
    "2) Укажите суммы денег на расчетных счетах и на депозитах.\n"
    "3) Сформулируйте одну главную цель проекта на текущий квартал. Вы можете расписать всё максимально подробно или наоборот кратко.\n"
    "4) Введите категории доходов, затем категории расходов. \n"
    "5) Внесите, пожалуйста, задачи на ближайшие 30 дней. \n"
    "6) Отправьте выписку банка, ОДС и ОСВ (фото JPG/PNG или PDF).\n\n"

    "🛎️ Дальше бот будет напоминать Вам, когда нужно внести новые данные и обновить информацию.\n\n"
    "⚠️ Важно ⚠️:\n"
    "• пожалуйста не редактируйте отправленные сообщения!\n"
    "• используйте меню для навигации между разделами\n"
    "• чтобы начать всё заново, введите команду— /start\n"
    "⬇️Нажмите кнопку ниже, чтобы подтвердить, что вы ознакомились с инструкцией.\n"
)

onboarding_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Ознакомлен")],
        [KeyboardButton(text="Ознакомлена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)


# ===== Single-instance + duplicate protection =====
_INSTANCE_LOCK_SOCKET = None
_RECENT_MESSAGE_KEYS: "OrderedDict[tuple[int, int], None]" = OrderedDict()
_MAX_RECENT_MESSAGE_KEYS = 5000


def acquire_single_instance_lock() -> None:
    """Не даём второму локальному экземпляру стартовать параллельно."""
    global _INSTANCE_LOCK_SOCKET
    if _INSTANCE_LOCK_SOCKET is not None:
        return

    port = 47000 + sum(ord(ch) for ch in BOT_TOKEN) % 1000
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        logging.error("⛔️ Уже запущен другой локальный экземпляр бота с этим токеном.")
        raise SystemExit(1)

    _INSTANCE_LOCK_SOCKET = sock


def is_duplicate_message(message: Message) -> bool:
    key = (message.chat.id, message.message_id)
    if key in _RECENT_MESSAGE_KEYS:
        return True
    _RECENT_MESSAGE_KEYS[key] = None
    _RECENT_MESSAGE_KEYS.move_to_end(key)
    while len(_RECENT_MESSAGE_KEYS) > _MAX_RECENT_MESSAGE_KEYS:
        _RECENT_MESSAGE_KEYS.popitem(last=False)
    return False

async def notify_admin(text: str) -> None:
    if not ADMIN_ID or ADMIN_ID == 0:
        return
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as e:
        logging.exception(f"⛔️ Ошибка отправки уведомления администратору: {e}")

REMINDER_TEST_MODE = TEST_MODE
REMINDER_24H_SECONDS = 2 * 60 * 60
REMINDER_48H_SECONDS = 48 * 60 * 60

if REMINDER_TEST_MODE:
    REMINDER_24H_SECONDS = 120
    REMINDER_48H_SECONDS = 180

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

commands_texts = {
    "command1": ONBOARDING_TEXT, 
    "command2": "Введите, пожалуйста, название проекта",
    "command3": "💵 Введите, пожалуйста, сумму денег на расчетных счетах",
    "command4": "💵 Введите, пожалуйста, сумму денег на депозитах",
    "command5": "Старое значение цели проекта будет показано ниже. После этого введите новую цель проекта.",
    "command6": "Старые категории доходов будут показаны ниже. Затем выберите, что хотите изменить.",
    "command7": "Старые категории расходов будут показаны ниже. Затем выберите, что хотите изменить.",
    "command8": "Старые задачи будут показаны ниже. Затем выберите, что хотите изменить.",
    "command9": "Старые статусы задач будут показаны ниже. Затем выберите, что хотите изменить.",
    "command10": "Старые суммы расходов будут показаны ниже. Затем выберите, что хотите изменить.",
    "command11": "Старые суммы доходов будут показаны ниже. Затем выберите, что хотите изменить.",
    "command12": "Старые задачи на ближайшие 30 дней будут показаны ниже. Затем выберите, что хотите изменить.",
    "command13": "Старое название выписки банка будет показано ниже. Затем отправьте новый файл.",
    "command14": "Старое значение цели на квартал будет показано ниже. После этого введите новую цель.",
}

bot_commands = [
    BotCommand(command="command1", description="Инструкция"),
    BotCommand(command="command2", description="Название проекта"),
    BotCommand(command="command3", description="Расчетный счет"),
    BotCommand(command="command4", description="Депозит"),
    BotCommand(command="command5", description="Цель проекта"),
    BotCommand(command="command6", description="Категории доходов"),
    BotCommand(command="command7", description="Категории расходов"),
    BotCommand(command="command8", description="Задачи"),
    BotCommand(command="command9", description="Статус задачи"),
    BotCommand(command="command10", description="Сумма расходов"),
    BotCommand(command="command11", description="Сумма доходов"),
    BotCommand(command="command12", description="Задачи на 30 дней"),
    BotCommand(command="command13", description="Выписка банка"),
    BotCommand(command="command14", description="Цель на квартал"),
]

@dp.message_handler(commands=[f"command{i}" for i in range(1, 15)])
async def handle_command(message: types.Message):
    if is_duplicate_message(message):
        return

    user_id = message.from_user.id
    cmd = message.text.lstrip("/").split()[0]

    if cmd != "command1" and not await _ensure_onboarded(message, user_id):
        return

    if cmd in EDITABLE_COMMANDS:
        await handle_edit_command(message, user_id, cmd)
        return

    text = commands_texts.get(cmd)
    if text:
        await message.reply(text)


@dp.message_handler(commands=["command"])
async def handle_command_alias(message: types.Message):
    if is_duplicate_message(message):
        return

    raw_text = (message.text or "").strip()
    parts = raw_text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.reply("Используйте формат /command5 или /command 5.")
        return

    command_number = int(parts[1])
    if command_number < 1 or command_number > 14:
        await message.reply("Команда должна быть в диапазоне от 1 до 14.")
        return

    message.text = f"/command{command_number}"
    await handle_command(message)


async def set_bot_commands(bot):
    await bot.set_my_commands(bot_commands)


async def setup_bot_commands_on_startup(_):
    await set_bot_commands(bot)
    print("Бот запущен и команды установлены")

user_state: dict[int, dict] = {}
expense_flow: dict[int, list] = {}
income_flow: dict[int, list] = {}
reminder_state: dict[int, dict] = {}
income_reminder_state: dict[int, dict] = {}
report_files_state: dict[int, dict] = {}
task_check_state: dict[int, dict] = {}
new_tasks_state: dict[int, dict] = {}
quarter_goal_state: dict[int, dict] = {}
edit_state: dict[int, dict] = {}

create_tables()
add_reminder_column()
add_balance_columns()
add_project_schedule_columns()


def _json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, tuple):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)
def normalize_text(text: str) -> str:
    if not text:
        return ""
    return text.strip().lower()

READY_WORDS = {
    "готово",
    "ready",
    "done",
    "ok",
    "дайын",
    "болды",
}

def save_chat_state_for_user(user_id: int) -> None:
    try:
        payload = {
            "user_state": _json_safe(user_state.get(user_id)) if user_id in user_state else None,
            "expense_flow": _json_safe(expense_flow.get(user_id)) if user_id in expense_flow else None,
            "income_flow": _json_safe(income_flow.get(user_id)) if user_id in income_flow else None,
            "reminder_state": _json_safe(reminder_state.get(user_id)) if user_id in reminder_state else None,
            "income_reminder_state": _json_safe(income_reminder_state.get(user_id)) if user_id in income_reminder_state else None,
            "report_files_state": _json_safe(report_files_state.get(user_id)) if user_id in report_files_state else None,
            "task_check_state": _json_safe(task_check_state.get(user_id)) if user_id in task_check_state else None,
            "new_tasks_state": _json_safe(new_tasks_state.get(user_id)) if user_id in new_tasks_state else None,
            "quarter_goal_state": _json_safe(quarter_goal_state.get(user_id)) if user_id in quarter_goal_state else None,
            "edit_state": _json_safe(edit_state.get(user_id)) if user_id in edit_state else None,
        }

        state_json = json.dumps(payload, ensure_ascii=False)

        conn = get_connection()
        cursor = conn.cursor()

        last_activity_ts = int(datetime.now().timestamp())

        cursor.execute(
            """
            INSERT INTO chat_state (user_id, state_json, last_activity_ts, r24_sent, r48_sent, updated_at)
            VALUES (?, ?, ?, 0, 0, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                state_json=excluded.state_json,
                last_activity_ts=excluded.last_activity_ts,
                updated_at=CURRENT_TIMESTAMP
            """,
            (user_id, state_json, last_activity_ts),
        )

        conn.commit()
        conn.close()
    except Exception as e:
        logging.exception(f"⛔️ Ошибка сохранения состояния чата для user_id={user_id}: {e}")


def _restore_tasks_as_tuples(task_state: dict) -> None:
    tasks = task_state.get("tasks")
    if isinstance(tasks, list):
        task_state["tasks"] = [tuple(x) if isinstance(x, list) and len(x) == 2 else x for x in tasks]


def load_chat_state_all() -> None:
    user_state.clear()
    expense_flow.clear()
    income_flow.clear()
    reminder_state.clear()
    income_reminder_state.clear()
    report_files_state.clear()
    task_check_state.clear()
    new_tasks_state.clear()
    quarter_goal_state.clear()
    edit_state.clear()
    logging.info("ℹ️ Интерактивные состояния не восстанавливаются после перезапуска, чтобы избежать зависших сценариев.")


def touch_user_activity(user_id: int) -> None:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        now_ts = int(datetime.now().timestamp())
        cursor.execute(
            """
            INSERT INTO chat_state (user_id, state_json, last_activity_ts, r24_sent, r48_sent, updated_at)
            VALUES (?, ?, ?, 0, 0, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                last_activity_ts=excluded.last_activity_ts,
                r24_sent=0,
                r48_sent=0,
                updated_at=CURRENT_TIMESTAMP
            """,
            (user_id, json.dumps({}, ensure_ascii=False), now_ts),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.exception(f"⛔️ Ошибка touch_user_activity для user_id={user_id}: {e}")


def clear_user_state(user_id: int) -> None:
    for m in [user_state, expense_flow, income_flow, reminder_state, income_reminder_state, report_files_state, task_check_state, new_tasks_state, quarter_goal_state, edit_state]:
        m.pop(user_id, None)
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chat_state WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.exception(f"⛔️ Ошибка очистки состояния чата для user_id={user_id}: {e}")


def disable_user_notifications(user_id: int) -> None:
    for m in [user_state, expense_flow, income_flow, reminder_state, income_reminder_state, report_files_state, task_check_state, new_tasks_state, quarter_goal_state, edit_state]:
        m.pop(user_id, None)

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE chat_state SET r24_sent = 1, r48_sent = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,),
        )
        cursor.execute(
            """
            UPDATE periods
            SET reminder_sent = 1
            WHERE project_id IN (SELECT id FROM projects WHERE user_id = ?)
            """,
            (user_id,),
        )
        cursor.execute(
            "UPDATE projects SET docs_request_sent = 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.exception(f"⛔️ Ошибка отключения уведомлений для user_id={user_id}: {e}")


async def safe_send_message(user_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(user_id, text, **kwargs)
        return True
    except (ChatNotFound, BotBlocked, UserDeactivated) as e:
        logging.warning(f"⚠️ Уведомления для user_id={user_id} отключены: {e}")
        disable_user_notifications(user_id)
        return False
    except Exception as e:
        logging.exception(f"⛔️ Ошибка при отправке пользователю {user_id}: {e}")
        return False


task_status_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Сделано")],
        [KeyboardButton(text="🔄 В процессе")],
        [KeyboardButton(text="❌ Не сделано")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

quarter_status_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Достигнуто")],
        [KeyboardButton(text="❌ Не достигнуто")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

finance_categories_kb = ReplyKeyboardRemove()

finance_categories_kb = ReplyKeyboardRemove()

def _get_gsheets_client() -> gspread.Client:
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(GSHEETS_SERVICE_ACCOUNT_JSON, scopes=scopes)
    return gspread.authorize(creds)

def safe_float(value):
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return 0
        return v
    except Exception:
        return 0


float_pattern = re.compile(r"^[0-9]+([.,][0-9]+)?$")


def parse_float_from_user(text: str) -> float | None:
    """
    Аккуратно парсит число, запрещая любые спецсимволы, буквы и т.п.
    Допускается формат: 123, 123.45, 123,45 (запятая будет преобразована в точку).
    """
    if not text:
        return None

    cleaned = text.strip().replace(" ", "")

    if not float_pattern.match(cleaned):
        return None

    cleaned = cleaned.replace(",", ".")

    try:
        value = float(cleaned)
    except ValueError:
        return None

    if math.isnan(value) or math.isinf(value):
        return None

    return value


EDITABLE_COMMANDS = {f"command{i}" for i in range(2, 15)}
TASK_STATUS_LABELS = {
    "done": "✅ Сделано",
    "in_progress": "🔄 В процессе",
    "not_done": "❌ Не сделано",
}
TASK_STATUS_INPUT_MAP = {
    "✅ Сделано": "done",
    "🔄 В процессе": "in_progress",
    "❌ Не сделано": "not_done",
}
COMMAND_8_AND_12_SET = {"command8", "command12"}


def get_latest_project(user_id: int) -> tuple[int, str] | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, project_name
        FROM projects
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return int(row[0]), row[1] or "Без названия"


def get_latest_period(project_id: int) -> tuple[int, float, float, float] | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id,
               COALESCE(previous_balance, 0),
               COALESCE(account_balance, 0),
               COALESCE(deposit_balance, 0)
        FROM periods
        WHERE project_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (project_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return int(row[0]), safe_float(row[1]), safe_float(row[2]), safe_float(row[3])


def format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def build_numbered_list(items: list[dict], label_key: str, value_key: str | None = None) -> str:
    lines = []
    for idx, item in enumerate(items, start=1):
        line = f"{idx}. {item[label_key]}"
        if value_key:
            line += f" — {item[value_key]}"
        lines.append(line)
    return "\n".join(lines)


def get_command_items(command: str, project_id: int) -> tuple[list[dict], str]:
    conn = get_connection()
    cursor = conn.cursor()
    items: list[dict] = []
    prompt = ""

    if command == "command6":
        cursor.execute(
            "SELECT id, name FROM categories WHERE project_id = ? AND type = 'income' ORDER BY id",
            (project_id,),
        )
        items = [{"id": row[0], "label": row[1]} for row in cursor.fetchall()]
        prompt = "Категории доходов"
    elif command == "command7":
        cursor.execute(
            "SELECT id, name FROM categories WHERE project_id = ? AND type = 'expense' ORDER BY id",
            (project_id,),
        )
        items = [{"id": row[0], "label": row[1]} for row in cursor.fetchall()]
        prompt = "Категории расходов"
    elif command in COMMAND_8_AND_12_SET:
        cursor.execute(
            "SELECT id, task_text FROM tasks WHERE project_id = ? ORDER BY id",
            (project_id,),
        )
        items = [{"id": row[0], "label": row[1]} for row in cursor.fetchall()]
        prompt = "Задачи"
    elif command == "command9":
        cursor.execute(
            "SELECT id, task_text, status FROM tasks WHERE project_id = ? ORDER BY id",
            (project_id,),
        )
        items = [
            {
                "id": row[0],
                "label": row[1],
                "value": TASK_STATUS_LABELS.get(row[2], row[2]),
            }
            for row in cursor.fetchall()
        ]
        prompt = "Статусы задач"
    elif command == "command10":
        cursor.execute(
            """
            SELECT e.id, e.category, e.amount
            FROM expenses e
            JOIN periods p ON p.id = e.period_id
            WHERE p.project_id = ?
            ORDER BY e.id DESC
            """,
            (project_id,),
        )
        seen = set()
        for row in cursor.fetchall():
            if row[1] in seen:
                continue
            seen.add(row[1])
            items.append({"id": row[0], "label": row[1], "value": format_number(safe_float(row[2]))})
        prompt = "Суммы расходов"
    elif command == "command11":
        cursor.execute(
            """
            SELECT i.id, i.category, i.amount
            FROM incomes i
            JOIN periods p ON p.id = i.period_id
            WHERE p.project_id = ?
            ORDER BY i.id DESC
            """,
            (project_id,),
        )
        seen = set()
        for row in cursor.fetchall():
            if row[1] in seen:
                continue
            seen.add(row[1])
            items.append({"id": row[0], "label": row[1], "value": format_number(safe_float(row[2]))})
        prompt = "Суммы доходов"

    conn.close()
    return items, prompt


def start_edit_flow(user_id: int, command: str, payload: dict) -> None:
    edit_state[user_id] = {"command": command, **payload}
    touch_user_activity(user_id)
    save_chat_state_for_user(user_id)


def get_next_docs_request_at() -> str:
    delay = timedelta(seconds=30) if TEST_MODE else timedelta(days=DOCUMENTS_INTERVAL_DAYS)
    return (datetime.now() + delay).strftime("%Y-%m-%d %H:%M:%S")


def project_requires_documents(project_id: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT next_docs_request_at, COALESCE(docs_request_sent, 0)
        FROM projects
        WHERE id = ?
        """,
        (project_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        return False
    due_at = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    return due_at <= datetime.now() and not bool(row[1])


def mark_documents_requested(project_id: int) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE projects SET docs_request_sent = 1 WHERE id = ?",
        (project_id,),
    )
    conn.commit()
    conn.close()


def schedule_next_documents_request(project_id: int) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE projects
        SET next_docs_request_at = ?, docs_request_sent = 0
        WHERE id = ?
        """,
        (get_next_docs_request_at(), project_id),
    )
    conn.commit()
    conn.close()


async def handle_edit_command(message: Message, user_id: int, command: str) -> bool:
    latest_project = get_latest_project(user_id)
    if not latest_project:
        await message.answer("Сначала создайте проект через /start.")
        return True

    project_id, project_name = latest_project

    if command == "command2":
        start_edit_flow(user_id, command, {"step": "awaiting_new_value", "project_id": project_id})
        await message.answer(
            f"Старое название проекта: {project_name}\n\nВведите новое название проекта."
        )
        return True

    if command in {"command3", "command4"}:
        latest_period = get_latest_period(project_id)
        if not latest_period:
            await message.answer("Для этого проекта еще нет сохраненного финансового периода.")
            return True
        period_id, _, account_balance, deposit_balance = latest_period
        old_value = account_balance if command == "command3" else deposit_balance
        label = "сумма на расчетных счетах" if command == "command3" else "сумма на депозитах"
        start_edit_flow(user_id, command, {"step": "awaiting_new_value", "project_id": project_id, "period_id": period_id})
        await message.answer(
            f"Старое значение ({label}): {format_number(old_value)}\n\nВведите новое значение."
        )
        return True

    if command in {"command5", "command14"}:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, goal_text
            FROM quarter_goals
            WHERE project_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (project_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            await message.answer("Для этого проекта еще нет сохраненной цели квартала.")
            return True
        goal_id, goal_text = row
        prompt = "цель проекта" if command == "command5" else "цель на квартал"
        start_edit_flow(user_id, command, {"step": "awaiting_new_value", "goal_id": goal_id, "project_id": project_id})
        await message.answer(
            f"Старое значение ({prompt}): {goal_text}\n\nВведите новое значение."
        )
        return True

    if command == "command13":
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, file_name
            FROM files
            WHERE project_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (project_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            await message.answer("Для этого проекта еще нет сохраненной выписки банка.")
            return True
        file_id, file_name = row
        start_edit_flow(user_id, command, {"step": "awaiting_new_file", "file_row_id": file_id, "project_id": project_id})
        await message.answer(
            f"Старая выписка банка: {file_name}\n\nОтправьте новый файл или фото."
        )
        return True

    items, prompt = get_command_items(command, project_id)
    if not items:
        await message.answer(f"Для проекта «{project_name}» пока нет данных для команды {command}.")
        return True

    start_edit_flow(
        user_id,
        command,
        {"step": "awaiting_selection", "project_id": project_id, "items": items},
    )

    old_values = build_numbered_list(items, "label", "value" if items and "value" in items[0] else None)
    await message.answer(
        f"Текущие данные ({prompt}):\n{old_values}\n\nНапишите номер записи, которую хотите изменить."
    )
    return True

def _fetch_finance_table_rows() -> List[List[Any]]:
    """
    Лист 1 — Финансы:
    - входящие остатки (счета + депозит) -> periods.previous_balance (входящий остаток периода)
    - доходы -> SUM(incomes.amount) по period_id
    - расходы -> SUM(expenses.amount) по period_id
    - финансовый результат каждые 30 дней -> входящий + доходы - расходы
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            pr.id AS project_id,
            pr.project_name,
            p.id AS period_id,
            p.start_date,
            p.end_date,
            COALESCE(p.previous_balance, 0) AS incoming_balance,
            COALESCE(inc.total_income, 0) AS total_income,
            COALESCE(exp.total_expense, 0) AS total_expense
        FROM projects pr
        JOIN periods p ON p.project_id = pr.id
        LEFT JOIN (
            SELECT period_id, SUM(amount) AS total_income
            FROM incomes
            GROUP BY period_id
        ) inc ON inc.period_id = p.id
        LEFT JOIN (
            SELECT period_id, SUM(amount) AS total_expense
            FROM expenses
            GROUP BY period_id
        ) exp ON exp.period_id = p.id
        ORDER BY pr.id ASC, p.start_date ASC
        """
    )
    rows = cur.fetchall()
    conn.close()

    table: List[List[Any]] = []
    for project_id, project_name, period_id, start_date, end_date, incoming, income, expense in rows:
        incoming = safe_float(incoming)
        income = safe_float(income)
        expense = safe_float(expense)

        result = incoming + income - expense

        table.append(
        [
            project_id,
            project_name,
            period_id,
            start_date,
            end_date,
            incoming,
            income,
            expense,
            result,
        ]
    )
    return table

# ================= GOOGLE SHEETS =================

def _sync_finance_sheet_once() -> None:
    gc = _get_gsheets_client()
    sh = gc.open_by_key(GSHEETS_SPREADSHEET_ID)

    try:
        ws = sh.worksheet(GSHEETS_WORKSHEET_FINANCE)
    except Exception:
        ws = sh.add_worksheet(title=GSHEETS_WORKSHEET_FINANCE, rows=2000, cols=20)

    header = [
        "ID проекта",
        "Название проекта",
        "ID периода",
        "Дата начала периода",
        "Дата окончания периода",
        "Входящий остаток",
        "Доходы",
        "Расходы",
        "Финансовый результат",
    ]

    body = _fetch_finance_table_rows()
    values = [header] + body

    ws.clear()
    ws.update(values=values, range_name="A1", value_input_option="RAW")


def _fetch_goals_and_tasks_rows() -> List[List[Any]]:
    conn = get_connection()
    cursor = conn.cursor()

    table: List[List[Any]] = []

    # ===== КВАРТАЛЬНЫЕ ЦЕЛИ =====
    cursor.execute("""
        SELECT pr.project_name, q.quarter_start, q.quarter_end, q.goal_text, q.status
        FROM quarter_goals q
        JOIN projects pr ON pr.id = q.project_id
        ORDER BY q.quarter_start DESC
    """)

    table.append(["=== ЦЕЛЬ НА КВАРТАЛ ==="])
    table.append(["Проект", "Начало", "Конец", "Цель", "Статус"])

    for project_name, start, end, goal_text, status in cursor.fetchall():
        status_map = {
            "achieved": "Достигнуто",
            "not_achieved": "Не достигнуто",
            None: "В процессе"
        }

        table.append([
            project_name,
            start,
            end,
            goal_text,
            status_map.get(status, "В процессе"),
        ])

    table.append([])
    table.append(["=== ЗАДАЧИ НА 30 ДНЕЙ ==="])
    table.append(["Проект", "Начало", "Конец", "Задача", "Статус", "Причина"])

    cursor.execute("""
    SELECT pr.project_name,
           COALESCE(p.start_date, (
               SELECT start_date FROM periods
               WHERE project_id = t.project_id
               ORDER BY id DESC LIMIT 1
           )) AS start_date,
           COALESCE(p.end_date, (
               SELECT end_date FROM periods
               WHERE project_id = t.project_id
               ORDER BY id DESC LIMIT 1
           )) AS end_date,
           t.id, t.task_text, t.status, t.fail_reason
    FROM tasks t
    JOIN projects pr ON pr.id = t.project_id
    LEFT JOIN periods p ON p.id = t.period_id
    ORDER BY start_date DESC
""")
    for project_name, start_date, end_date, task_id, task_text, status, fail_reason in cursor.fetchall():

        status_map = {
            "done": "Выполнено",
            "in_progress": "В процессе",
            "not_done": "Не выполнено",
            None: "Не выполнено"
        }

        table.append([
            project_name,
            start_date,
            end_date,
            task_text,
            status_map.get(status),
            fail_reason or ""
        ])

        if status == "in_progress":
            cursor.execute("UPDATE tasks SET period_id = NULL WHERE id = ?", (task_id,))

    conn.commit()
    conn.close()

    return table


def _sync_goals_sheet_once() -> None:
    gc = _get_gsheets_client()
    sh = gc.open_by_key(GSHEETS_SPREADSHEET_ID)

    try:
        ws = sh.worksheet("Трекшн")
    except Exception:
        ws = sh.add_worksheet(title="Трекшн", rows=2000, cols=20)
    body = _fetch_goals_and_tasks_rows()

    ws.clear()
    ws.update(values=body, range_name="A1", value_input_option="RAW")
    
def get_quarter_start(dt: datetime | None = None) -> datetime:
    d = dt or datetime.today()
    quarter = (d.month - 1) // 3 + 1
    start_month = (quarter - 1) * 3 + 1
    return datetime(d.year, start_month, 1)

def get_quarter_end(dt: datetime | None = None) -> datetime:
    d = dt or datetime.today()
    quarter = (d.month - 1) // 3 + 1
    end_month = quarter * 3
    if end_month < 12:
        last_day = (datetime(d.year, end_month + 1, 1) - timedelta(days=1)).day
    else:
        last_day = 31
    return datetime(d.year, end_month, last_day)


def get_previous_quarter_range(dt: datetime | None = None) -> tuple[datetime, datetime]:
    d = dt or datetime.today()
    current_q_start = get_quarter_start(d)
    prev_q_end = current_q_start - timedelta(days=1)
    prev_q_start = get_quarter_start(prev_q_end)
    prev_q_end = get_quarter_end(prev_q_end)
    return prev_q_start, prev_q_end


def is_quarter_check_day(dt: datetime | None = None) -> bool:
    d = dt or datetime.today()
    quarter_start_month = get_quarter_start(d).month
    return d.day == 5 and d.month == quarter_start_month


def is_user_busy(user_id: int) -> bool:
    return (
        user_id in user_state
        or user_id in reminder_state
        or user_id in income_reminder_state
        or user_id in task_check_state
        or user_id in new_tasks_state
        or user_id in report_files_state
        or user_id in quarter_goal_state
        or user_id in edit_state
    )


async def request_documents_if_needed_or_finish(message: Message, user_id: int, project_id: int, next_period_id: int | None) -> None:
    if project_requires_documents(project_id):
        report_files_state[user_id] = {
            "project_id": project_id,
            "period_id": next_period_id,
        }
        mark_documents_requested(project_id)
        save_chat_state_for_user(user_id)
        await message.answer(
            "🧾 Пожалуйста, отправьте выписку банка, ОДС и ОСВ в чат проекта.\n"
            "Подойдут фото (JPG, PNG) или PDF.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    schedule_next_period_from_now(next_period_id)
    clear_user_state(user_id)
    await message.answer(
        "✅ Данные сохранены. Ожидайте следующего уведомления от бота.",
        reply_markup=ReplyKeyboardRemove(),
    )
    schedule_quarter_test_check(user_id, project_id)


async def start_new_tasks_creation(
    message: Message,
    user_id: int,
    project_id: int,
    period_id: int,
    closed_period_id: int | None,
) -> None:
    new_tasks_state[user_id] = {
        "step": "awaiting_new_tasks",
        "tasks": [],
        "project_id": project_id,
        "period_id": period_id,
        "closed_period_id": closed_period_id,
    }
    touch_user_activity(user_id)
    save_chat_state_for_user(user_id)

    await message.answer(
        "📆 Добавьте задачи на ближайшие 30 дней.\n"
        "Каждую задачу вводите отдельным сообщением.\n"
        "❗️Когда закончите - напишите «Готово».",
        reply_markup=ReplyKeyboardRemove(),
    )


async def save_task_results_and_continue(message: Message, user_id: int) -> None:
    state = task_check_state.get(user_id, {})

    conn = get_connection()
    cursor = conn.cursor()

    for result in state.get("results", []):
        if result.get("status") == "not_done":
            cursor.execute(
                """
                UPDATE tasks
                SET status = ?, fail_reason = ?
                WHERE id = ?
                """,
                (result["status"], result.get("fail_reason", ""), result["task_id"]),
            )
        else:
            cursor.execute(
                """
                UPDATE tasks
                SET status = ?
                WHERE id = ?
                """,
                (result["status"], result["task_id"]),
            )

    conn.commit()
    conn.close()

    await message.answer("✅ Статусы задач сохранены!", reply_markup=ReplyKeyboardRemove())

    await start_new_tasks_creation(
        message,
        user_id,
        state["project_id"],
        state["new_period_id"],
        state.get("period_id"),
    )

    task_check_state.pop(user_id, None)
    save_chat_state_for_user(user_id)

async def prompt_expense_step(target: Message | None, user_id: int) -> None:
    state = reminder_state.get(user_id, {})
    categories = state.get("categories", [])
    index = state.get("current_index", 0)

    if index < len(categories):
        text = f"💸 Введите сумму расходов по категории: {categories[index]}"
        if target is not None:
            await target.answer(text, reply_markup=ReplyKeyboardRemove())
        else:
            await safe_send_message(user_id, text, reply_markup=ReplyKeyboardRemove())
    else:
        await finalize_expenses(target, user_id, state)


async def prompt_income_step(target: Message | None, user_id: int) -> None:
    state = income_reminder_state.get(user_id, {})
    categories = state.get("categories", [])
    index = state.get("current_index", 0)

    if index < len(categories):
        text = f"💳 Введите сумму дохода по категории: {categories[index]}"
        if target is not None:
            await target.answer(text, reply_markup=ReplyKeyboardRemove())
        else:
            await safe_send_message(user_id, text, reply_markup=ReplyKeyboardRemove())
    else:
        await finalize_incomes(target, user_id, state)


def schedule_next_period_from_now(period_id: int | None) -> None:
    if not period_id:
        return
    conn = get_connection()
    cursor = conn.cursor()
    start_date = datetime.now()
    end_date = start_date + (timedelta(seconds=PERIOD_SECONDS) if TEST_MODE else timedelta(days=REPORT_INTERVAL_DAYS))
    cursor.execute(
        """
        UPDATE periods
        SET start_date = ?, end_date = ?, reminder_sent = 0
        WHERE id = ?
        """,
        (start_date.strftime("%Y-%m-%d %H:%M:%S"), end_date.strftime("%Y-%m-%d %H:%M:%S"), period_id),
    )
    conn.commit()
    conn.close()


def schedule_quarter_test_check(user_id: int, project_id: int) -> None:
    if not QUARTER_TEST_MODE:
        return

    async def _delayed_check(uid: int, pid: int) -> None:
        await asyncio.sleep(QUARTER_TEST_DELAY_SECONDS)
        await trigger_quarter_goal_check(uid, pid)

    asyncio.create_task(_delayed_check(user_id, project_id))


async def finalize_expenses(message: Message, user_id: int, state: dict) -> None:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM expenses WHERE period_id = ?", (state.get("period_id"),))
    for cat, amt in state.get("amounts", {}).items():
        cursor.execute(
            "INSERT INTO expenses (period_id, category, amount) VALUES (?, ?, ?)",
            (state.get("period_id"), cat, amt),
        )

    cursor.execute(
        """
        SELECT name FROM categories
        WHERE project_id = ? AND type='income'
        ORDER BY id
        """,
        (state.get("project_id"),),
    )
    income_categories = [row[0] for row in cursor.fetchall()]

    conn.commit()
    conn.close()

    reminder_state.pop(user_id, None)
    income_reminder_state[user_id] = {
        "step": "awaiting_income_amount",
        "categories": income_categories,
        "current_index": 0,
        "amounts": {},
        "period_id": state.get("period_id"),
        "project_id": state.get("project_id"),
    }
    save_chat_state_for_user(user_id)

    await message.answer("✅ Расходы сохранены.", reply_markup=ReplyKeyboardRemove())
    await prompt_income_step(message, user_id)


async def finalize_incomes(message: Message, user_id: int, state: dict) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM incomes WHERE period_id = ?", (state.get("period_id"),))
    for cat, amt in state.get("amounts", {}).items():
        cursor.execute(
            "INSERT INTO incomes (period_id, category, amount) VALUES (?, ?, ?)",
            (state.get("period_id"), cat, amt),
        )
    conn.commit()
    conn.close()

    income_reminder_state.pop(user_id, None)
    save_chat_state_for_user(user_id)
    await message.answer("✅ Доходы сохранены.", reply_markup=ReplyKeyboardRemove())
    await process_financial_result_and_check_tasks(message, user_id)


async def google_sheets_sync_task() -> None:
    if not GSHEETS_SYNC_ENABLED:
        return

    while True:
        try:
            await asyncio.to_thread(_sync_finance_sheet_once)
            await asyncio.to_thread(_sync_goals_sheet_once)

        except Exception as e:
            logging.exception(f"⛔️ Ошибка синхронизации Google Sheets: {e}")

        await asyncio.sleep(GSHEETS_SYNC_INTERVAL_SECONDS)
async def process_financial_result_and_check_tasks(message: Message, user_id: int) -> None:
    state = income_reminder_state.get(user_id, {})

    conn = get_connection()
    cursor = conn.cursor()

    for cat, amt in state.get("amounts", {}).items():
        cursor.execute(
            "INSERT INTO incomes (period_id, category, amount) VALUES (?, ?, ?)",
            (state.get("period_id"), cat, amt),
        )

    cursor.execute("SELECT previous_balance FROM periods WHERE id = ?", (state.get("period_id"),))
    row = cursor.fetchone()
    previous_balance = row[0] if row else 0

    cursor.execute("SELECT SUM(amount) FROM incomes WHERE period_id = ?", (state.get("period_id"),))
    row = cursor.fetchone()
    total_income = row[0] if row and row[0] is not None else 0

    cursor.execute("SELECT SUM(amount) FROM expenses WHERE period_id = ?", (state.get("period_id"),))
    row = cursor.fetchone()
    total_expense = row[0] if row and row[0] is not None else 0

    financial_result = previous_balance + total_income - total_expense

    new_period_start = datetime.now()
    new_period_end = new_period_start + timedelta(seconds=PERIOD_SECONDS) if TEST_MODE else new_period_start + timedelta(days=REPORT_INTERVAL_DAYS)

    cursor.execute(
        """
        INSERT INTO periods (
            project_id, start_date, end_date, previous_balance, reminder_sent, account_balance, deposit_balance
        )
        VALUES (?, ?, ?, ?, 0, ?, ?)
        """,
        (
            state["project_id"],
            new_period_start.strftime("%Y-%m-%d %H:%M:%S"),
            new_period_end.strftime("%Y-%m-%d %H:%M:%S"),
            financial_result,
            financial_result,
            0,
        ),
    )
    new_period_id = cursor.lastrowid

    cursor.execute(
        """
        SELECT id, task_text FROM tasks
        WHERE project_id = ? AND period_id = ?
        ORDER BY id
        """,
        (state["project_id"], state["period_id"]),
    )
    old_tasks = cursor.fetchall()

    conn.commit()
    conn.close()

    report = (
        f"📊 <b>Финансовый результат проекта</b>\n\n"
        f"📌 Предыдущий остаток: {previous_balance:,.2f}\n"
        f"💰 Доходы: +{total_income:,.2f}\n"
        f"💸 Расходы: -{total_expense:,.2f}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏁 <b>Итог: {financial_result:,.2f}</b>"
    )
    await message.answer(report, parse_mode="HTML")

    income_reminder_state.pop(user_id, None)
    save_chat_state_for_user(user_id)

    closed_period_id = state.get("period_id")

    if old_tasks:
        task_check_state[user_id] = {
            "step": "awaiting_task_status",
            "tasks": old_tasks,
            "current_index": 0,
            "results": [],
            "project_id": state["project_id"],
            "new_period_id": new_period_id,
            "closed_period_id": closed_period_id,
        }
        touch_user_activity(user_id)
        save_chat_state_for_user(user_id)

        await message.answer(
            f"🔎 Проверка по статусу задач.\n\n"
            f"Задача: {old_tasks[0][1]}\n\n"
            "Выберите, пожалуйста, статус задачи:",
            reply_markup=task_status_kb,
        )
    else:
        await start_new_tasks_creation(
            message,
            user_id,
            state["project_id"],
            new_period_id,
            closed_period_id,
        )


async def trigger_quarter_goal_check(user_id: int, project_id: int) -> None:
    conn = get_connection()
    cursor = conn.cursor()

    if QUARTER_TEST_MODE:
        cursor.execute(
            """
            SELECT id, goal_text, status, notified_at
            FROM quarter_goals
            WHERE project_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (project_id,),
        )
    else:
        prev_start, prev_end = get_previous_quarter_range(datetime.today())
        cursor.execute(
            """
            SELECT id, goal_text, status, notified_at
            FROM quarter_goals
            WHERE project_id = ?
              AND quarter_start = ?
              AND quarter_end = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (project_id, prev_start.date().isoformat(), prev_end.date().isoformat()),
        )

    row = cursor.fetchone()
    if not row:
        conn.close()
        return

    goal_id, goal_text, status, notified_at = row

    if status in ("achieved", "not_achieved") or notified_at is not None:
        conn.close()
        return

    cursor.execute(
        "UPDATE quarter_goals SET notified_at = ? WHERE id = ?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), goal_id),
    )
    conn.commit()
    conn.close()

    quarter_goal_state[user_id] = {
        "step": "awaiting_quarter_status",
        "goal_id": goal_id,
        "project_id": project_id,
    }
    touch_user_activity(user_id)
    save_chat_state_for_user(user_id)

    await bot.send_message(
        user_id,
        "Настало время проверить цель прошлого квартала. ⏰\n\n"
        f"Цель: {goal_text}\n\n"
        "Укажите, достигли ли вы её:",
        reply_markup=quarter_status_kb,
    )

async def _ensure_onboarded(message: Message, user_id: int) -> bool:
    if is_user_onboarded(user_id):
        return True
    await message.answer(ONBOARDING_TEXT, reply_markup=onboarding_kb)
    return False


@dp.message_handler(lambda m: (m.text or "").strip() in ("Ознакомлен", "Ознакомлена"))
async def onboarding_accept_handler(message: Message) -> None:
    if is_duplicate_message(message):
        return

    user_id = message.from_user.id
    text = (message.text or "").strip()

    accepted_as = "male" if text == "Ознакомлен" else "female"
    set_user_onboarded(user_id, accepted_as=accepted_as)

    clear_user_state(user_id)
    user_state[user_id] = {"step": "awaiting_project_name"}
    touch_user_activity(user_id)
    save_chat_state_for_user(user_id)

    await message.answer("Спасибо! Теперь можно продолжить.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Введите, пожалуйста, название проекта", reply_markup=ReplyKeyboardRemove())


@dp.message_handler(commands=["start"])
async def start_handler(message: Message) -> None:
    if is_duplicate_message(message):
        return

    user_id = message.from_user.id

    if not is_user_onboarded(user_id):
        await message.answer(ONBOARDING_TEXT, reply_markup=onboarding_kb)
        return

    clear_user_state(user_id)

    user_state[user_id] = {"step": "awaiting_project_name"}
    touch_user_activity(user_id)
    save_chat_state_for_user(user_id)

    await message.answer("Введите, пожалуйста, название проекта", reply_markup=ReplyKeyboardRemove())


@dp.message_handler(content_types=["photo", "document"])
async def handle_files(message: Message) -> None:
    if is_duplicate_message(message):
        return

    user_id = message.from_user.id
    if not await _ensure_onboarded(message, user_id):
        return

    if user_id in edit_state and edit_state[user_id].get("command") == "command13":
        state = edit_state[user_id]
        if state.get("step") == "awaiting_new_file":
            touch_user_activity(user_id)

            conn = get_connection()
            cursor = conn.cursor()

            file_id = message.document.file_id if message.document else message.photo[-1].file_id
            file_name = message.document.file_name if message.document else "photo.jpg"

            cursor.execute(
                "UPDATE files SET file_id = ?, file_name = ?, created_at = CURRENT_TIMESTAMP WHERE id = ?",
                (file_id, file_name, state["file_row_id"]),
            )
            cursor.execute(
                "UPDATE projects SET next_docs_request_at = ?, docs_request_sent = 0 WHERE id = ?",
                (get_next_docs_request_at(), state["project_id"]),
            )
            conn.commit()
            conn.close()

            edit_state.pop(user_id, None)
            save_chat_state_for_user(user_id)

            await message.answer(
                f"✅ Выписка банка обновлена.\nНовое значение: {file_name}",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

    if user_id in report_files_state:
        touch_user_activity(user_id)

        state = report_files_state[user_id]

        conn = get_connection()
        cursor = conn.cursor()

        file_id = message.document.file_id if message.document else message.photo[-1].file_id
        file_name = message.document.file_name if message.document else "photo.jpg"

        cursor.execute(
            "INSERT INTO files (project_id, file_id, file_name) VALUES (?, ?, ?)",
            (state["project_id"], file_id, file_name),
        )
        cursor.execute(
            """
            UPDATE projects
            SET next_docs_request_at = ?, docs_request_sent = 0
            WHERE id = ?
            """,
            (get_next_docs_request_at(), state["project_id"]),
        )

        conn.commit()
        conn.close()

        report_files_state.pop(user_id, None)
        save_chat_state_for_user(user_id)

        schedule_next_period_from_now(state.get("period_id"))

        await message.answer(
            "✅ Документы получены и сохранены.\n\n"
            "🛎️ Ожидайте следующих уведомлений от бота.",
            reply_markup=ReplyKeyboardRemove(),
        )

        conn_for_notify = get_connection()
        cursor_for_notify = conn_for_notify.cursor()
        cursor_for_notify.execute(
            "SELECT project_name FROM projects WHERE id = ?",
            (state["project_id"],),
        )
        row = cursor_for_notify.fetchone()
        project_name = row[0] if row else "Неизвестный проект"
        conn_for_notify.close()

        await notify_admin(
                    f"📊 Отчет заполнен!\n"
            f"Проект: {project_name}\n"
            "Отчетная информация предоставлена."
        )

        clear_user_state(user_id)
        schedule_quarter_test_check(user_id, state["project_id"])
        return

    if user_id not in user_state:
        return
    if user_state[user_id].get("step") != "awaiting_files":
        return

    touch_user_activity(user_id)

    conn = get_connection()
    cursor = conn.cursor()

    project_name = user_state[user_id]["project_name"]
    total = user_state[user_id]["total"]

    cursor.execute(
        """
        INSERT INTO projects (user_id, username, project_name, next_docs_request_at, docs_request_sent)
        VALUES (?, ?, ?, ?, 0)
        """,
        (user_id, message.from_user.username, project_name, get_next_docs_request_at()),
    )
    project_id = cursor.lastrowid

    for cat in income_flow.get(user_id, []):
        cursor.execute(
            "INSERT INTO categories (project_id, type, name) VALUES (?, 'income', ?)",
            (project_id, cat),
        )

    for cat in expense_flow.get(user_id, []):
        cursor.execute(
            "INSERT INTO categories (project_id, type, name) VALUES (?, 'expense', ?)",
            (project_id, cat),
        )

    start_date = datetime.now()
    end_date = start_date + timedelta(seconds=PERIOD_SECONDS) if TEST_MODE else start_date + timedelta(days=REPORT_INTERVAL_DAYS)

    cursor.execute(
        """
        INSERT INTO periods (
            project_id, start_date, end_date, previous_balance, reminder_sent, account_balance, deposit_balance
        )
        VALUES (?, ?, ?, ?, 0, ?, ?)
        """,
        (
            project_id,
            start_date.strftime("%Y-%m-%d %H:%M:%S"),
            end_date.strftime("%Y-%m-%d %H:%M:%S"),
            total,
            user_state[user_id]["amount"],
            user_state[user_id].get("deposit", 0),
        ),
    )
    period_id = cursor.lastrowid

    for task_text in user_state[user_id].get("tasks", []):
        cursor.execute(
            """
            INSERT INTO tasks (project_id, period_id, task_text, status)
            VALUES (?, ?, ?, 'not_done')
            """,
            (project_id, period_id, task_text),
        )

    file_id = message.document.file_id if message.document else message.photo[-1].file_id
    file_name = message.document.file_name if message.document else "photo.jpg"

    cursor.execute(
        "INSERT INTO files (project_id, file_id, file_name) VALUES (?, ?, ?)",
        (project_id, file_id, file_name),
    )

    goal_text = user_state[user_id].get("goal", "")
    q_start = get_quarter_start(datetime.today())
    q_end = get_quarter_end(datetime.today())

    cursor.execute(
        """
        INSERT INTO quarter_goals (project_id, quarter_start, quarter_end, goal_text)
        VALUES (?, ?, ?, ?)
        """,
        (
            project_id,
            q_start.date().isoformat(),
            q_end.date().isoformat(),
            goal_text,
        ),
    )

    conn.commit()
    conn.close()

    project_name = user_state[user_id]["project_name"]
    await notify_admin(
        "🆕 Новый проект зарегистрирован!\n"
        f"Название: {project_name}\n"
        "Пользователь указал основные данные проекта."
    )
    await notify_admin(
        "🎯 Заполнена цель проекта на квартал!\n"
        f"Название: {project_name}\n"
        f"Цель: {goal_text}"
    )

    user_state.pop(user_id, None)
    expense_flow.pop(user_id, None)
    income_flow.pop(user_id, None)
    save_chat_state_for_user(user_id)

    await message.answer(
        "Информация по проекту сохранена ✅\n\n"
        "Документы получены. Когда подойдет следующий отчетный период, бот пришлет уведомление ⏰",
        reply_markup=ReplyKeyboardRemove(),
    )

    clear_user_state(user_id)


@dp.message_handler(lambda message: False)
async def save_message_legacy_disabled(message: Message) -> None:
    if is_duplicate_message(message):
        return

    user_id = message.from_user.id
    text = message.text.strip() if message.text else ""
    step = user_state.get(user_id, {}).get("step")

    # Жесткий приоритет для старта онбординга: название проекта не должно
    # перехватываться оставшимися служебными состояниями или проверками.
    if step == "awaiting_project_name" and text and not text.startswith("/"):
        user_state[user_id]["project_name"] = text
        user_state[user_id]["step"] = "awaiting_accounts"
        touch_user_activity(user_id)
        save_chat_state_for_user(user_id)
        await message.answer("💵 Введите, пожалуйста, сумму денег на расчетных счетах")
        return

    if not await _ensure_onboarded(message, user_id):
        return

    if text.startswith("/"):
        return

    if is_user_busy(user_id):
        touch_user_activity(user_id)

    if user_id in edit_state:
        state = edit_state[user_id]
        command = state.get("command")
        step = state.get("step")

        if step == "awaiting_selection":
            if not text.isdigit():
                await message.answer("Пожалуйста, отправьте номер записи из списка.")
                return

            selected_index = int(text) - 1
            items = state.get("items", [])
            if selected_index < 0 or selected_index >= len(items):
                await message.answer("Такого номера нет. Выберите номер записи из списка.")
                return

            selected_item = items[selected_index]
            state["selected_item"] = selected_item
            state["step"] = "awaiting_new_value"
            save_chat_state_for_user(user_id)

            if command == "command9":
                await message.answer(
                    f"Текущий статус задачи «{selected_item['label']}»: {selected_item['value']}\n\n"
                    "Введите новый статус:\n✅ Сделано\n🔄 В процессе\n❌ Не сделано",
                    reply_markup=task_status_kb,
                )
            else:
                old_value = selected_item.get("value", selected_item["label"])
                prompt = "новое значение" if command in {"command10", "command11"} else "новое название"
                await message.answer(
                    f"Старое значение: {old_value}\n\nВведите {prompt}.",
                    reply_markup=ReplyKeyboardRemove(),
                )
            return

        if step == "awaiting_new_value":
            conn = get_connection()
            cursor = conn.cursor()

            if command == "command2":
                cursor.execute(
                    "UPDATE projects SET project_name = ? WHERE id = ?",
                    (text, state["project_id"]),
                )
                success_text = f"✅ Название проекта обновлено.\nНовое значение: {text}"
            elif command == "command3":
                new_value = parse_float_from_user(text)
                if new_value is None or new_value < 0:
                    conn.close()
                    await message.answer("❌ Введите корректное неотрицательное число.")
                    return
                latest_period = get_latest_period(state["project_id"])
                if not latest_period:
                    conn.close()
                    await message.answer("Не удалось найти период для обновления.")
                    return
                _, _, _, deposit_balance = latest_period
                cursor.execute(
                    """
                    UPDATE periods
                    SET account_balance = ?, previous_balance = ?
                    WHERE id = ?
                    """,
                    (new_value, new_value + deposit_balance, state["period_id"]),
                )
                success_text = f"✅ Сумма на расчетных счетах обновлена.\nНовое значение: {format_number(new_value)}"
            elif command == "command4":
                new_value = parse_float_from_user(text)
                if new_value is None or new_value < 0:
                    conn.close()
                    await message.answer("❌ Введите корректное неотрицательное число.")
                    return
                latest_period = get_latest_period(state["project_id"])
                if not latest_period:
                    conn.close()
                    await message.answer("Не удалось найти период для обновления.")
                    return
                _, _, account_balance, _ = latest_period
                cursor.execute(
                    """
                    UPDATE periods
                    SET deposit_balance = ?, previous_balance = ?
                    WHERE id = ?
                    """,
                    (new_value, account_balance + new_value, state["period_id"]),
                )
                success_text = f"✅ Сумма на депозитах обновлена.\nНовое значение: {format_number(new_value)}"
            elif command in {"command5", "command14"}:
                cursor.execute(
                    "UPDATE quarter_goals SET goal_text = ? WHERE id = ?",
                    (text, state["goal_id"]),
                )
                label = "Цель проекта" if command == "command5" else "Цель на квартал"
                success_text = f"✅ {label} обновлена.\nНовое значение: {text}"
            elif command == "command6":
                selected_item = state["selected_item"]
                cursor.execute("UPDATE categories SET name = ? WHERE id = ?", (text, selected_item["id"]))
                cursor.execute(
                    """
                    UPDATE incomes
                    SET category = ?
                    WHERE category = ?
                      AND period_id IN (SELECT id FROM periods WHERE project_id = ?)
                    """,
                    (text, selected_item["label"], state["project_id"]),
                )
                success_text = f"✅ Категория доходов обновлена.\nНовое значение: {text}"
            elif command == "command7":
                selected_item = state["selected_item"]
                cursor.execute("UPDATE categories SET name = ? WHERE id = ?", (text, selected_item["id"]))
                cursor.execute(
                    """
                    UPDATE expenses
                    SET category = ?
                    WHERE category = ?
                      AND period_id IN (SELECT id FROM periods WHERE project_id = ?)
                    """,
                    (text, selected_item["label"], state["project_id"]),
                )
                success_text = f"✅ Категория расходов обновлена.\nНовое значение: {text}"
            elif command in COMMAND_8_AND_12_SET:
                selected_item = state["selected_item"]
                cursor.execute("UPDATE tasks SET task_text = ? WHERE id = ?", (text, selected_item["id"]))
                success_text = f"✅ Задача обновлена.\nНовое значение: {text}"
            elif command == "command9":
                new_status = TASK_STATUS_INPUT_MAP.get(text)
                if not new_status:
                    conn.close()
                    await message.answer("Пожалуйста, выберите статус из кнопок.", reply_markup=task_status_kb)
                    return
                selected_item = state["selected_item"]
                cursor.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, selected_item["id"]))
                success_text = f"✅ Статус задачи обновлен.\nНовое значение: {TASK_STATUS_LABELS[new_status]}"
            elif command == "command10":
                new_value = parse_float_from_user(text)
                if new_value is None or new_value < 0:
                    conn.close()
                    await message.answer("❌ Введите корректное неотрицательное число.")
                    return
                selected_item = state["selected_item"]
                cursor.execute("UPDATE incomes SET amount = ? WHERE id = ?", (new_value, selected_item["id"]))
                success_text = f"✅ Сумма доходов обновлена.\nНовое значение: {format_number(new_value)}"
            elif command == "command11":
                new_value = parse_float_from_user(text)
                if new_value is None or new_value < 0:
                    conn.close()
                    await message.answer("❌ Введите корректное неотрицательное число.")
                    return
                selected_item = state["selected_item"]
                cursor.execute("UPDATE expenses SET amount = ? WHERE id = ?", (new_value, selected_item["id"]))
                success_text = f"✅ Сумма расходов обновлена.\nНовое значение: {format_number(new_value)}"
            else:
                conn.close()
                await message.answer("Неизвестная команда редактирования.")
                return

            conn.commit()
            conn.close()
            edit_state.pop(user_id, None)
            save_chat_state_for_user(user_id)
            await message.answer(success_text, reply_markup=ReplyKeyboardRemove())
            return

    if user_id in quarter_goal_state:
        state = quarter_goal_state[user_id]

        if state.get("step") == "awaiting_quarter_status":
            if text == "✅ Достигнуто":
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE quarter_goals
                    SET status = 'achieved', checked_at = ?
                    WHERE id = ?
                    """,
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), state["goal_id"]),
                )
                conn.commit()
                conn.close()

                conn_for_notify = get_connection()
                cursor_for_notify = conn_for_notify.cursor()
                cursor_for_notify.execute(
                    "SELECT project_name FROM projects WHERE id = ?",
                    (state["project_id"],),
                )
                row = cursor_for_notify.fetchone()
                project_name = row[0] if row else "Неизвестный проект"
                conn_for_notify.close()

                await notify_admin(
                    "📅 Отчет за квартал заполнен!\n"
                    f"Проект: {project_name}\n"
                    "Цель отмечена как достигнута."
                )

                quarter_goal_state.pop(user_id, None)
                save_chat_state_for_user(user_id)

                await message.answer(
                    "Отлично! Цель отмечена как достигнута ✅",
                    reply_markup=ReplyKeyboardRemove(),
                )
                await message.answer(
                    f"Пожалуйста, обновите 1-pager проекта:\n{GOOGLE_FORM_URL}",
                    reply_markup=ReplyKeyboardRemove(),
                )
                clear_user_state(user_id)
                return

            if text == "❌ Не достигнуто":
                state["step"] = "awaiting_quarter_fail_reason"
                save_chat_state_for_user(user_id)
                await message.answer(
                    "Укажите, пожалуйста, причину, по которой цель не была достигнута:",
                    reply_markup=ReplyKeyboardRemove(),
                )
                return

            await message.answer(
                "Пожалуйста, выберите один из вариантов:",
                reply_markup=quarter_status_kb,
            )
            return

        if state.get("step") == "awaiting_quarter_fail_reason":
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE quarter_goals
                SET status = 'not_achieved', fail_reason = ?, checked_at = ?
                WHERE id = ?
                """,
                (text, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), state["goal_id"]),
            )
            conn.commit()
            conn.close()

            conn_for_notify = get_connection()
            cursor_for_notify = conn_for_notify.cursor()
            cursor_for_notify.execute(
                "SELECT project_name FROM projects WHERE id = ?",
                (state["project_id"],),
            )
            row = cursor_for_notify.fetchone()
            project_name = row[0] if row else "Неизвестный проект"
            conn_for_notify.close()

            await notify_admin(
                f"📅 Отчет за квартал заполнен!\n"
                f"Проект: {project_name}\n"
                f"Цель отмечена как не достигнута."
            )

            quarter_goal_state.pop(user_id, None)
            save_chat_state_for_user(user_id)

            await message.answer(
                "Спасибо за пояснение. Цель отмечена как не достигнута ❌",
                reply_markup=ReplyKeyboardRemove(),
            )
            await message.answer(
                f"Пожалуйста, обновите 1-pager проекта:\n{GOOGLE_FORM_URL}",
                reply_markup=ReplyKeyboardRemove(),
            )
            clear_user_state(user_id)
            return

    if user_id in task_check_state:
        state = task_check_state.get(user_id, {})

        if state.get("step") == "awaiting_task_status":
            if text in ["✅ Сделано", "🔄 В процессе", "❌ Не сделано"]:
                status_map = {
                    "✅ Сделано": "done",
                    "🔄 В процессе": "in_progress",
                    "❌ Не сделано": "not_done",
                }
                status = status_map[text]

                current_index = state.get("current_index", 0)
                tasks = state.get("tasks", [])

                if current_index < len(tasks):
                    current_task = tasks[current_index]
                    state["results"].append(
                        {
                            "task_id": current_task[0],
                            "task_text": current_task[1],
                            "status": status,
                        }
                    )

                    if status == "not_done":
                        state["step"] = "awaiting_fail_reason"
                        save_chat_state_for_user(user_id)
                        await message.answer(
                            "Укажите причину, по которой задача не выполнена:",
                            reply_markup=ReplyKeyboardRemove(),
                        )
                        return

                    state["current_index"] = current_index + 1
                    save_chat_state_for_user(user_id)

                    if state["current_index"] < len(tasks):
                        next_task = tasks[state["current_index"]]
                        await message.answer(
                            f"Задача: {next_task[1]}\n\nВыберите, пожалуйста, статус задачи",
                            reply_markup=task_status_kb,
                        )
                    else:
                        await save_task_results_and_continue(message, user_id)
                    return
            else:
                await message.answer("Пожалуйста, выберите статус из кнопок:", reply_markup=task_status_kb)
                return

        if state.get("step") == "awaiting_fail_reason":
            if state["results"]:
                state["results"][-1]["fail_reason"] = text

            current_index = state.get("current_index", 0)
            tasks = state.get("tasks", [])

            state["current_index"] = current_index + 1

            if state["current_index"] < len(tasks):
                next_task = tasks[state["current_index"]]
                state["step"] = "awaiting_task_status"
                save_chat_state_for_user(user_id)
                await message.answer(
                    f"Задача: {next_task[1]}\n\nВыберите, пожалуйста, статус задачи",
                    reply_markup=task_status_kb,
                )
            else:
                save_chat_state_for_user(user_id)
                await save_task_results_and_continue(message, user_id)
            return

    if user_id in new_tasks_state:
        if normalize_text(text) in READY_WORDS:
            state = new_tasks_state.get(user_id, {})

            conn = get_connection()
            cursor = conn.cursor()

            for task_text in state.get("tasks", []):
                cursor.execute(
                    "INSERT INTO tasks (project_id, period_id, task_text, status) VALUES (?, ?, ?, 'not_done')",
                    (state.get("project_id"), state.get("period_id"), task_text),
                )

            conn.commit()
            conn.close()

            new_tasks_state.pop(user_id, None)
            save_chat_state_for_user(user_id)

            await message.answer("✅ Новые задачи сохранены!", reply_markup=ReplyKeyboardRemove())

            try:
                conn_notify = get_connection()
                cur_notify = conn_notify.cursor()
                cur_notify.execute("SELECT project_name FROM projects WHERE id = ?", (state.get("project_id"),))
                notify_row = cur_notify.fetchone()
                conn_notify.close()
                if notify_row:
                    await notify_admin(
                        "📌 Обновлены задачи на ближайшие 30 дней!\n"
                        f"Название: {notify_row[0]}"
                    )
            except Exception as e:
                logging.exception(f"⛔️ Ошибка уведомления по задачам: {e}")

            await request_documents_if_needed_or_finish(
                message,
                user_id,
                state.get("project_id"),
                state.get("period_id"),
            )
            return

        new_tasks_state[user_id].setdefault("tasks", []).append(text)
        save_chat_state_for_user(user_id)
        await message.answer("Задача добавлена ✅\n\n"
                             "Введите следующую или «Готово»")
        return

    if user_id in income_reminder_state:
        state = income_reminder_state.get(user_id, {})

        if normalize_text(text) in READY_WORDS:
            await message.answer("❌ На этом этапе нужно вводить суммы по категориям, а не «Готово».", reply_markup=ReplyKeyboardRemove())
            return

        amount = parse_float_from_user(text)
        if amount is None:
            await message.answer("❌ Пожалуйста, введите число без смайликов, букв и других символов.", reply_markup=ReplyKeyboardRemove())
            return

        if amount < 0:
            await message.answer("❌ Число не может быть отрицательным", reply_markup=ReplyKeyboardRemove())
            return

        index = state.get("current_index", 0)
        categories = state.get("categories", [])

        if index < len(categories):
            category = categories[index]
            state.setdefault("amounts", {})[category] = amount
            state["current_index"] = index + 1
            state["step"] = "awaiting_income_amount"
            save_chat_state_for_user(user_id)
            await prompt_income_step(message, user_id)
            return

        await finalize_incomes(message, user_id, state)
        return

    if user_id in reminder_state:
        state = reminder_state.get(user_id, {})

        if normalize_text(text) in READY_WORDS:
            await message.answer("❌ На этом этапе нужно вводить суммы по категориям, а не «Готово».", reply_markup=ReplyKeyboardRemove())
            return

        amount = parse_float_from_user(text)
        if amount is None:
            await message.answer("❌ Пожалуйста, введите число без смайликов, букв и других символов.", reply_markup=ReplyKeyboardRemove())
            return

        if amount < 0:
            await message.answer("❌ Число не может быть отрицательным", reply_markup=ReplyKeyboardRemove())
            return

        index = state.get("current_index", 0)
        categories = state.get("categories", [])

        if index < len(categories):
            category = categories[index]
            state.setdefault("amounts", {})[category] = amount
            state["current_index"] = index + 1
            state["step"] = "awaiting_expense_amount"
            save_chat_state_for_user(user_id)
            await prompt_expense_step(message, user_id)
            return

        await finalize_expenses(message, user_id, state)
        return

    if user_id not in user_state:
        return
    if user_state[user_id].get("step") != "awaiting_files":
        return

    touch_user_activity(user_id)

    conn = get_connection()
    cursor = conn.cursor()

    project_name = user_state[user_id]["project_name"]
    total = user_state[user_id]["total"]

    cursor.execute(
        """
        INSERT INTO projects (user_id, username, project_name, next_docs_request_at, docs_request_sent)
        VALUES (?, ?, ?, ?, 0)
        """,
        (user_id, message.from_user.username, project_name, get_next_docs_request_at()),
    )
    project_id = cursor.lastrowid

    for cat in income_flow.get(user_id, []):
        cursor.execute(
            "INSERT INTO categories (project_id, type, name) VALUES (?, 'income', ?)",
            (project_id, cat),
        )

    for cat in expense_flow.get(user_id, []):
        cursor.execute(
            "INSERT INTO categories (project_id, type, name) VALUES (?, 'expense', ?)",
            (project_id, cat),
        )

    start_date = datetime.now()
    end_date = start_date + timedelta(seconds=PERIOD_SECONDS) if TEST_MODE else start_date + timedelta(days=REPORT_INTERVAL_DAYS)

    cursor.execute(
        """
        INSERT INTO periods (
            project_id, start_date, end_date, previous_balance, reminder_sent, account_balance, deposit_balance
        )
        VALUES (?, ?, ?, ?, 0, ?, ?)
        """,
        (
            project_id,
            start_date.strftime("%Y-%m-%d %H:%M:%S"),
            end_date.strftime("%Y-%m-%d %H:%M:%S"),
            total,
            user_state[user_id]["amount"],
            user_state[user_id].get("deposit", 0),
        ),
    )
    period_id = cursor.lastrowid

    for task_text in user_state[user_id].get("tasks", []):
        cursor.execute(
            """
            INSERT INTO tasks (project_id, period_id, task_text, status)
            VALUES (?, ?, ?, 'not_done')
            """,
            (project_id, period_id, task_text),
        )

    file_id = message.document.file_id if message.document else message.photo[-1].file_id
    file_name = message.document.file_name if message.document else "photo.jpg"

    cursor.execute(
        "INSERT INTO files (project_id, file_id, file_name) VALUES (?, ?, ?)",
        (project_id, file_id, file_name),
    )

    goal_text = user_state[user_id].get("goal", "")
    q_start = get_quarter_start(datetime.today())
    q_end = get_quarter_end(datetime.today())

    cursor.execute(
        """
        INSERT INTO quarter_goals (project_id, quarter_start, quarter_end, goal_text)
        VALUES (?, ?, ?, ?)
        """,
        (
            project_id,
            q_start.date().isoformat(),
            q_end.date().isoformat(),
            goal_text,
        ),
    )

    conn.commit()
    conn.close()

    project_name = user_state[user_id]["project_name"]
    await notify_admin(
        "🆕 Новый проект зарегистрирован!\n"
        f"Название: {project_name}\n"
        "Пользователь указал основные данные проекта."
    )
    await notify_admin(
        "🎯 Заполнена цель проекта на квартал!\n"
        f"Название: {project_name}\n"
        f"Цель: {goal_text}"
    )

    user_state.pop(user_id, None)
    expense_flow.pop(user_id, None)
    income_flow.pop(user_id, None)
    save_chat_state_for_user(user_id)

    await message.answer(
        "Информация по проекту сохранена ✅\n\n"
        "Документы получены. Когда подойдет следующий отчетный период, бот пришлет уведомление ⏰",
        reply_markup=ReplyKeyboardRemove(),
    )

    clear_user_state(user_id)


@dp.message_handler()
async def save_message(message: Message) -> None:
    if is_duplicate_message(message):
        return

    user_id = message.from_user.id
    text = message.text.strip() if message.text else ""
    step = user_state.get(user_id, {}).get("step")

    # Жесткий приоритет для старта онбординга: название проекта не должно
    # перехватываться оставшимися служебными состояниями или проверками.
    if step == "awaiting_project_name" and text and not text.startswith("/"):
        user_state[user_id]["project_name"] = text
        user_state[user_id]["step"] = "awaiting_accounts"
        touch_user_activity(user_id)
        save_chat_state_for_user(user_id)
        await message.answer("💵 Введите, пожалуйста, сумму денег на расчетных счетах")
        return

    if not await _ensure_onboarded(message, user_id):
        return

    if text.startswith("/"):
        return

    if is_user_busy(user_id):
        touch_user_activity(user_id)

    if user_id in edit_state:
        state = edit_state[user_id]
        command = state.get("command")
        step = state.get("step")

        if step == "awaiting_selection":
            if not text.isdigit():
                await message.answer("Пожалуйста, отправьте номер записи из списка.")
                return

            selected_index = int(text) - 1
            items = state.get("items", [])
            if selected_index < 0 or selected_index >= len(items):
                await message.answer("Такого номера нет. Выберите номер записи из списка.")
                return

            selected_item = items[selected_index]
            state["selected_item"] = selected_item
            state["step"] = "awaiting_new_value"
            save_chat_state_for_user(user_id)

            if command == "command9":
                await message.answer(
                    f"Текущий статус задачи «{selected_item['label']}»: {selected_item['value']}\n\n"
                    "Введите новый статус:\n✅ Сделано\n🔄 В процессе\n❌ Не сделано",
                    reply_markup=task_status_kb,
                )
            else:
                old_value = selected_item.get("value", selected_item["label"])
                prompt = "новое значение" if command in {"command10", "command11"} else "новое название"
                await message.answer(
                    f"Старое значение: {old_value}\n\nВведите {prompt}.",
                    reply_markup=ReplyKeyboardRemove(),
                )
            return

        if step == "awaiting_new_value":
            conn = get_connection()
            cursor = conn.cursor()

            if command == "command2":
                cursor.execute(
                    "UPDATE projects SET project_name = ? WHERE id = ?",
                    (text, state["project_id"]),
                )
                success_text = f"✅ Название проекта обновлено.\nНовое значение: {text}"
            elif command == "command3":
                new_value = parse_float_from_user(text)
                if new_value is None or new_value < 0:
                    conn.close()
                    await message.answer("❌ Введите корректное неотрицательное число.")
                    return
                latest_period = get_latest_period(state["project_id"])
                if not latest_period:
                    conn.close()
                    await message.answer("Не удалось найти период для обновления.")
                    return
                _, _, _, deposit_balance = latest_period
                cursor.execute(
                    """
                    UPDATE periods
                    SET account_balance = ?, previous_balance = ?
                    WHERE id = ?
                    """,
                    (new_value, new_value + deposit_balance, state["period_id"]),
                )
                success_text = f"✅ Сумма на расчетных счетах обновлена.\nНовое значение: {format_number(new_value)}"
            elif command == "command4":
                new_value = parse_float_from_user(text)
                if new_value is None or new_value < 0:
                    conn.close()
                    await message.answer("❌ Введите корректное неотрицательное число.")
                    return
                latest_period = get_latest_period(state["project_id"])
                if not latest_period:
                    conn.close()
                    await message.answer("Не удалось найти период для обновления.")
                    return
                _, _, account_balance, _ = latest_period
                cursor.execute(
                    """
                    UPDATE periods
                    SET deposit_balance = ?, previous_balance = ?
                    WHERE id = ?
                    """,
                    (new_value, account_balance + new_value, state["period_id"]),
                )
                success_text = f"✅ Сумма на депозитах обновлена.\nНовое значение: {format_number(new_value)}"
            elif command in {"command5", "command14"}:
                cursor.execute(
                    "UPDATE quarter_goals SET goal_text = ? WHERE id = ?",
                    (text, state["goal_id"]),
                )
                label = "Цель проекта" if command == "command5" else "Цель на квартал"
                success_text = f"✅ {label} обновлена.\nНовое значение: {text}"
            elif command == "command6":
                selected_item = state["selected_item"]
                cursor.execute("UPDATE categories SET name = ? WHERE id = ?", (text, selected_item["id"]))
                cursor.execute(
                    """
                    UPDATE incomes
                    SET category = ?
                    WHERE category = ?
                      AND period_id IN (SELECT id FROM periods WHERE project_id = ?)
                    """,
                    (text, selected_item["label"], state["project_id"]),
                )
                success_text = f"✅ Категория доходов обновлена.\nНовое значение: {text}"
            elif command == "command7":
                selected_item = state["selected_item"]
                cursor.execute("UPDATE categories SET name = ? WHERE id = ?", (text, selected_item["id"]))
                cursor.execute(
                    """
                    UPDATE expenses
                    SET category = ?
                    WHERE category = ?
                      AND period_id IN (SELECT id FROM periods WHERE project_id = ?)
                    """,
                    (text, selected_item["label"], state["project_id"]),
                )
                success_text = f"✅ Категория расходов обновлена.\nНовое значение: {text}"
            elif command in COMMAND_8_AND_12_SET:
                selected_item = state["selected_item"]
                cursor.execute("UPDATE tasks SET task_text = ? WHERE id = ?", (text, selected_item["id"]))
                success_text = f"✅ Задача обновлена.\nНовое значение: {text}"
            elif command == "command9":
                new_status = TASK_STATUS_INPUT_MAP.get(text)
                if not new_status:
                    conn.close()
                    await message.answer("Пожалуйста, выберите статус из кнопок.", reply_markup=task_status_kb)
                    return
                selected_item = state["selected_item"]
                cursor.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, selected_item["id"]))
                success_text = f"✅ Статус задачи обновлен.\nНовое значение: {TASK_STATUS_LABELS[new_status]}"
            elif command == "command10":
                new_value = parse_float_from_user(text)
                if new_value is None or new_value < 0:
                    conn.close()
                    await message.answer("❌ Введите корректное неотрицательное число.")
                    return
                selected_item = state["selected_item"]
                cursor.execute("UPDATE incomes SET amount = ? WHERE id = ?", (new_value, selected_item["id"]))
                success_text = f"✅ Сумма доходов обновлена.\nНовое значение: {format_number(new_value)}"
            elif command == "command11":
                new_value = parse_float_from_user(text)
                if new_value is None or new_value < 0:
                    conn.close()
                    await message.answer("❌ Введите корректное неотрицательное число.")
                    return
                selected_item = state["selected_item"]
                cursor.execute("UPDATE expenses SET amount = ? WHERE id = ?", (new_value, selected_item["id"]))
                success_text = f"✅ Сумма расходов обновлена.\nНовое значение: {format_number(new_value)}"
            else:
                conn.close()
                await message.answer("Неизвестная команда редактирования.")
                return

            conn.commit()
            conn.close()
            edit_state.pop(user_id, None)
            save_chat_state_for_user(user_id)
            await message.answer(success_text, reply_markup=ReplyKeyboardRemove())
            return

    if user_id in quarter_goal_state:
        state = quarter_goal_state[user_id]

        if state.get("step") == "awaiting_quarter_status":
            if text == "✅ Достигнуто":
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE quarter_goals
                    SET status = 'achieved', checked_at = ?
                    WHERE id = ?
                    """,
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), state["goal_id"]),
                )
                conn.commit()
                conn.close()

                conn_for_notify = get_connection()
                cursor_for_notify = conn_for_notify.cursor()
                cursor_for_notify.execute(
                    "SELECT project_name FROM projects WHERE id = ?",
                    (state["project_id"],),
                )
                row = cursor_for_notify.fetchone()
                project_name = row[0] if row else "Неизвестный проект"
                conn_for_notify.close()

                await notify_admin(
                    "📅 Отчет за квартал заполнен!\n"
                    f"Проект: {project_name}\n"
                    "Цель отмечена как достигнута."
                )

                quarter_goal_state.pop(user_id, None)
                save_chat_state_for_user(user_id)

                await message.answer(
                    "Отлично! Цель отмечена как достигнута ✅",
                    reply_markup=ReplyKeyboardRemove(),
                )
                await message.answer(
                    f"Пожалуйста, обновите 1-pager проекта:\n{GOOGLE_FORM_URL}",
                    reply_markup=ReplyKeyboardRemove(),
                )
                clear_user_state(user_id)
                return

            if text == "❌ Не достигнуто":
                state["step"] = "awaiting_quarter_fail_reason"
                save_chat_state_for_user(user_id)
                await message.answer(
                    "Укажите, пожалуйста, причину, по которой цель не была достигнута:",
                    reply_markup=ReplyKeyboardRemove(),
                )
                return

            await message.answer(
                "Пожалуйста, выберите один из вариантов:",
                reply_markup=quarter_status_kb,
            )
            return

        if state.get("step") == "awaiting_quarter_fail_reason":
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE quarter_goals
                SET status = 'not_achieved', fail_reason = ?, checked_at = ?
                WHERE id = ?
                """,
                (text, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), state["goal_id"]),
            )
            conn.commit()
            conn.close()

            conn_for_notify = get_connection()
            cursor_for_notify = conn_for_notify.cursor()
            cursor_for_notify.execute(
                "SELECT project_name FROM projects WHERE id = ?",
                (state["project_id"],),
            )
            row = cursor_for_notify.fetchone()
            project_name = row[0] if row else "Неизвестный проект"
            conn_for_notify.close()

            await notify_admin(
                f"📅 Отчет за квартал заполнен!\n"
                f"Проект: {project_name}\n"
                f"Цель отмечена как не достигнута."
            )

            quarter_goal_state.pop(user_id, None)
            save_chat_state_for_user(user_id)

            await message.answer(
                "Спасибо за пояснение. Цель отмечена как не достигнута ❌",
                reply_markup=ReplyKeyboardRemove(),
            )
            await message.answer(
                f"Пожалуйста, обновите 1-pager проекта:\n{GOOGLE_FORM_URL}",
                reply_markup=ReplyKeyboardRemove(),
            )
            clear_user_state(user_id)
            return

    if user_id in task_check_state:
        state = task_check_state.get(user_id, {})

        if state.get("step") == "awaiting_task_status":
            if text in ["✅ Сделано", "🔄 В процессе", "❌ Не сделано"]:
                status_map = {
                    "✅ Сделано": "done",
                    "🔄 В процессе": "in_progress",
                    "❌ Не сделано": "not_done",
                }
                status = status_map[text]

                current_index = state.get("current_index", 0)
                tasks = state.get("tasks", [])

                if current_index < len(tasks):
                    current_task = tasks[current_index]
                    state["results"].append(
                        {
                            "task_id": current_task[0],
                            "task_text": current_task[1],
                            "status": status,
                        }
                    )

                    if status == "not_done":
                        state["step"] = "awaiting_fail_reason"
                        save_chat_state_for_user(user_id)
                        await message.answer(
                            "Укажите причину, по которой задача не выполнена:",
                            reply_markup=ReplyKeyboardRemove(),
                        )
                        return

                    state["current_index"] = current_index + 1
                    save_chat_state_for_user(user_id)

                    if state["current_index"] < len(tasks):
                        next_task = tasks[state["current_index"]]
                        await message.answer(
                            f"Задача: {next_task[1]}\n\nВыберите, пожалуйста, статус задачи",
                            reply_markup=task_status_kb,
                        )
                    else:
                        await save_task_results_and_continue(message, user_id)
                    return
            else:
                await message.answer("Пожалуйста, выберите статус из кнопок:", reply_markup=task_status_kb)
                return

        if state.get("step") == "awaiting_fail_reason":
            if state["results"]:
                state["results"][-1]["fail_reason"] = text

            current_index = state.get("current_index", 0)
            tasks = state.get("tasks", [])

            state["current_index"] = current_index + 1

            if state["current_index"] < len(tasks):
                next_task = tasks[state["current_index"]]
                state["step"] = "awaiting_task_status"
                save_chat_state_for_user(user_id)
                await message.answer(
                    f"Задача: {next_task[1]}\n\nВыберите, пожалуйста, статус задачи",
                    reply_markup=task_status_kb,
                )
            else:
                save_chat_state_for_user(user_id)
                await save_task_results_and_continue(message, user_id)
            return

    if user_id in new_tasks_state:
        if normalize_text(text) in READY_WORDS:
            state = new_tasks_state.get(user_id, {})

            conn = get_connection()
            cursor = conn.cursor()

            for task_text in state.get("tasks", []):
                cursor.execute(
                    "INSERT INTO tasks (project_id, period_id, task_text, status) VALUES (?, ?, ?, 'not_done')",
                    (state.get("project_id"), state.get("period_id"), task_text),
                )

            conn.commit()
            conn.close()

            new_tasks_state.pop(user_id, None)
            save_chat_state_for_user(user_id)

            await message.answer("✅ Новые задачи сохранены!", reply_markup=ReplyKeyboardRemove())

            try:
                conn_notify = get_connection()
                cur_notify = conn_notify.cursor()
                cur_notify.execute("SELECT project_name FROM projects WHERE id = ?", (state.get("project_id"),))
                notify_row = cur_notify.fetchone()
                conn_notify.close()
                if notify_row:
                    await notify_admin(
                        "📌 Обновлены задачи на ближайшие 30 дней!\n"
                        f"Название: {notify_row[0]}"
                    )
            except Exception as e:
                logging.exception(f"⛔️ Ошибка уведомления по задачам: {e}")

            await request_documents_if_needed_or_finish(
                message,
                user_id,
                state.get("project_id"),
                state.get("period_id"),
            )
            return

        new_tasks_state[user_id].setdefault("tasks", []).append(text)
        save_chat_state_for_user(user_id)
        await message.answer("Задача добавлена ✅\n\n"
                             "Введите следующую или «Готово»")
        return

    if user_id in income_reminder_state:
        amount = parse_float_from_user(text)
        if amount is None:
            await message.answer("❌ Пожалуйста, введите число без смайликов, букв и других символов.")
            return
        
        if amount < 0:
            await message.answer("❌ Число не может быть отрицательным")
            return

        state = income_reminder_state.get(user_id, {})
        index = state.get("current_index", 0)
        categories = state.get("categories", [])

        if index < len(categories):
            category = categories[index]
            state["amounts"][category] = amount
            state["current_index"] = index + 1
            save_chat_state_for_user(user_id)

            if state["current_index"] < len(categories):
                next_cat = categories[state["current_index"]]
                await message.answer(f"💳 Введите сумму доходов по категории: {next_cat}")
            else:
                await process_financial_result_and_check_tasks(message, user_id)
        return

    if user_id in reminder_state:
        amount = parse_float_from_user(text)
        if amount is None:
            await message.answer("❌ Пожалуйста, введите число без смайликов, букв и других символов.")
            return
        
        if amount < 0:
            await message.answer("❌ Число не может быть отрицательным")
            return

        state = reminder_state.get(user_id, {})
        index = state.get("current_index", 0)
        categories = state.get("categories", [])

        if index < len(categories):
            category = categories[index]
            state["amounts"][category] = amount
            state["current_index"] = index + 1
            save_chat_state_for_user(user_id)

            if state["current_index"] < len(categories):
                next_cat = categories[state["current_index"]]
                await message.answer(f"💸 Введите сумму расходов по категории: {next_cat}")
            else:
                conn = get_connection()
                cursor = conn.cursor()

                for cat, amt in state.get("amounts", {}).items():
                    cursor.execute(
                        "INSERT INTO expenses (period_id, category, amount) VALUES (?, ?, ?)",
                        (state.get("period_id"), cat, amt),
                    )

                cursor.execute(
                    """
                    SELECT name FROM categories
                    WHERE project_id = ? AND type='income'
                    """,
                    (state.get("project_id"),),
                )
                income_categories = [row[0] for row in cursor.fetchall()]

                conn.commit()
                conn.close()

                reminder_state.pop(user_id, None)
                save_chat_state_for_user(user_id)

                await message.answer("✅ Все расходы сохранены.")

                if income_categories:
                    income_reminder_state[user_id] = {
                        "step": "awaiting_income_amount",
                        "categories": income_categories,
                        "current_index": 0,
                        "amounts": {},
                        "period_id": state.get("period_id"),
                        "project_id": state.get("project_id"),
                    }
                    save_chat_state_for_user(user_id)
                    await message.answer(f"💳 Введите сумму доходов по категории: {income_categories[0]}")
                else:
                    income_reminder_state[user_id] = {
                        "step": "awaiting_income_amount",
                        "categories": [],
                        "current_index": 0,
                        "amounts": {},
                        "period_id": state.get("period_id"),
                        "project_id": state.get("project_id"),
                    }
                    save_chat_state_for_user(user_id)
                    await process_financial_result_and_check_tasks(message, user_id)
        return

    if user_id not in user_state:
        await message.answer("Используйте /start для создания проекта")
        return

    step = user_state.get(user_id, {}).get("step")

    if step == "awaiting_project_name":
        user_state[user_id]["project_name"] = text
        user_state[user_id]["step"] = "awaiting_accounts"
        save_chat_state_for_user(user_id)
        await message.answer("💵 Введите, пожалуйста, сумму денег на расчетных счетах")
        return

    if step == "awaiting_accounts":
        amount = parse_float_from_user(text)
        if amount is None:
            await message.answer("❌ Пожалуйста, введите число без смайликов, букв и других символов.")
            return
        
        if amount < 0:
            await message.answer("❌ Число не может быть отрицательным")
            return
        
        user_state[user_id]["amount"] = amount
        user_state[user_id]["step"] = "awaiting_deposit"
        save_chat_state_for_user(user_id)
        await message.answer("💵 Введите, пожалуйста, сумму денег на депозитах")
        return

    if step == "awaiting_deposit":
        deposit = parse_float_from_user(text)
        if deposit is None:
            await message.answer("❌ Пожалуйста, введите число без смайликов, букв и других символов.")
            return
        
        if deposit < 0:
            await message.answer("❌ Число не может быть отрицательным")
            return

        total = user_state[user_id]["amount"] + deposit
        user_state[user_id]["deposit"] = deposit
        user_state[user_id]["total"] = total
        user_state[user_id]["step"] = "awaiting_goal"
        save_chat_state_for_user(user_id)

        quarter_end = get_quarter_end()

        await message.answer(
            f"Финансовый результат проекта: {total} ✅\n\n"
            "Сформулируйте цель проекта на текущий квартал.\n" 
            f"Цель которую Вы реализуете (до {quarter_end.strftime('%d.%m.%Y')})"
        )
        return

    if step == "awaiting_goal":
        user_state[user_id]["goal"] = text
        user_state[user_id]["step"] = "awaiting_income_categories"
        income_flow[user_id] = []
        save_chat_state_for_user(user_id)
        await message.answer(
            "📊 Введите, пожалуйста, категории <u>доходов</u> согласно финансовой модели.\n\n"
            "Например: Подписка. Разовые продажи. Гранты и т.п.\n\n"
            "Каждую категорию вводите отдельным сообщением.\n"
            "❗️Как введете все имеющиеся категории - напишите «Готово»\n",
            parse_mode="HTML"
        )
        return

    if step == "awaiting_income_categories":
        if normalize_text(text) in READY_WORDS:
            user_state[user_id]["step"] = "awaiting_expense_categories_names"
            expense_flow[user_id] = []
            save_chat_state_for_user(user_id)
            await message.answer(
                "📊 Введите, пожалуйста, категории <u>расходов</u> согласно финансовой модели.\n\n"
                "Например: Хостинг. Серверы. Бухгалтерия и т.п.\n\n"
                "Каждую категорию вводите отдельным сообщением.\n" 
                "❗️Как введете все имеющиеся категории - напишите «Готово»",
                parse_mode="HTML"
            )
            return

        income_flow[user_id].append(text)
        save_chat_state_for_user(user_id)
        await message.answer("Категория добавлена ✅")
        return

    if step == "awaiting_expense_categories_names":
        if normalize_text(text) in READY_WORDS:
            user_state[user_id]["step"] = "awaiting_tasks"
            save_chat_state_for_user(user_id)
            await message.answer(
                "📆 Внесите, пожалуйста, задачи на ближайшие 30 дней.\n\n"
                "Каждую задачу вводите отдельным сообщением.\n"
                "❗️Когда закончите - напишите «Готово».\n"
            )
            return

        expense_flow[user_id].append(text)
        save_chat_state_for_user(user_id)
        await message.answer("Категория добавлена ✅")
        return

    if step == "awaiting_tasks":
        if normalize_text(text) in READY_WORDS:
            user_state[user_id]["step"] = "awaiting_files"
            save_chat_state_for_user(user_id)
            await message.answer("🧾 Пожалуйста, отправьте выписку банка, ОДС и ОСВ в чат проекта.\n"
            "Подойдут фото (JPG, PNG) или PDF.")
            return

        user_state[user_id].setdefault("tasks", []).append(text)
        save_chat_state_for_user(user_id)
        await message.answer("Задача добавлена ✅")
        return


async def reminder_task() -> None:
    while True:
        await asyncio.sleep(2 if TEST_MODE else 60)

        conn = get_connection()
        cursor = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute(
            """
            SELECT p.id, pr.user_id, pr.project_name, pr.id
            FROM periods p
            JOIN projects pr ON pr.id = p.project_id
            WHERE p.reminder_sent = 0 AND p.end_date <= ?
            """,
            (now_str,),
        )
        rows = cursor.fetchall()

        for period_id, user_id, project_name, project_id in rows:
            if is_user_busy(user_id):
                continue

            sent = await safe_send_message(
                user_id,
                f"⏰ Пора внести данные за последние 30 дней по проекту «{project_name}».\n\n"
                f"Начнем с расходов 💸"
            )
            if not sent:
                cursor.execute("UPDATE periods SET reminder_sent = 1 WHERE id = ?", (period_id,))
                conn.commit()
                continue

            cursor.execute(
                """
                SELECT name FROM categories
                WHERE project_id = ? AND type='expense'
                """,
                (project_id,),
            )
            categories = [row[0] for row in cursor.fetchall()]

            reminder_state[user_id] = {
                "step": "awaiting_expense_amount",
                "categories": categories,
                "current_index": 0,
                "amounts": {},
                "project_id": project_id,
                "period_id": period_id,
            }


            touch_user_activity(user_id)
            save_chat_state_for_user(user_id)

            await prompt_expense_step(None, user_id)

            cursor.execute("UPDATE periods SET reminder_sent = 1 WHERE id = ?", (period_id,))
            conn.commit()

        conn.close()


async def documents_request_task() -> None:
    while True:
        await asyncio.sleep(60 if TEST_MODE else 300)
        # Отдельный таймер документов отключен: документы запрашиваются
        # только внутри сценария после сохранения новых задач.
        continue


async def inactivity_reminder_task() -> None:
    while True:
        await asyncio.sleep(2 if TEST_MODE else 60)

        now_ts = int(datetime.now().timestamp())

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, last_activity_ts, r24_sent, r48_sent FROM chat_state")
        rows = cursor.fetchall()
        conn.close()

        for user_id, last_activity_ts, r24_sent, r48_sent in rows:
            if not is_user_busy(user_id):
                continue

            last_ts = int(last_activity_ts or now_ts)
            delta = now_ts - last_ts

            # 24-часовое напоминание пользователю
            if delta >= REMINDER_24H_SECONDS and not bool(r24_sent):
                await safe_send_message(
                    user_id,
                    "❗️ Напоминаю Вам, что сегодня необходимо заполнить информацию",
                )

                conn2 = get_connection()
                cur2 = conn2.cursor()
                cur2.execute(
                    "UPDATE chat_state SET r24_sent = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (user_id,),
                )
                conn2.commit()
                conn2.close()

            # 48-часовое уведомление пользователю и админу
            if delta >= REMINDER_48H_SECONDS and not bool(r48_sent):
                # 1) Сообщение пользователю
                await safe_send_message(
                    user_id,
                    "Уведомление о несдаче отчёта направляется администратору 📨",
                )

                # 2) Сообщение администратору
                try:
                    conn3 = get_connection()
                    cur3 = conn3.cursor()
                    cur3.execute(
                        """
                        SELECT project_name, username
                        FROM projects
                        WHERE user_id = ?
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (user_id,),
                    )
                    project_row = cur3.fetchone()
                    conn3.close()

                    if project_row:
                        project_name = project_row[0] or "Неизвестный проект"
                        username = project_row[1] or f"id{user_id}"
                    else:
                        project_name = "Неизвестный проект"
                        username = f"id{user_id}"

                    admin_message = (
                        "⚠️ Уведомление о несдаче отчёта.\n\n"
                        f"Пользователь: {username}\n"
                        f"Проект (стартап): «{project_name}»\n"
                        "Информация не была заполнена вовремя.⏰"
                    )

                    await notify_admin(admin_message)

                except Exception as e:
                    logging.exception(f"⛔️ Ошибка при отправке уведомления администратору: {e}")

                conn2 = get_connection()
                cur2 = conn2.cursor()
                cur2.execute(
                    "UPDATE chat_state SET r48_sent = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (user_id,),
                )
                conn2.commit()
                conn2.close()


async def quarter_check_task() -> None:
    while True:
        await asyncio.sleep(5 if QUARTER_TEST_MODE else 60)

        if QUARTER_TEST_MODE:
            continue

        if not is_quarter_check_day(datetime.today()):
            continue

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id FROM projects")
        projects = cursor.fetchall()
        conn.close()

        for project_id, user_id in projects:
            try:
                if is_user_busy(user_id):
                    continue
                await trigger_quarter_goal_check(user_id, project_id)
            except Exception as e:
                logging.exception(f"⛔️ Ошибка при запуске квартальной проверки: {e}")
                continue

from aiogram import executor

async def on_startup(dp):
    await bot.delete_webhook(drop_pending_updates=True)
    await setup_bot_commands_on_startup(dp)
    asyncio.create_task(reminder_task())
    asyncio.create_task(documents_request_task())
    asyncio.create_task(inactivity_reminder_task())
    asyncio.create_task(quarter_check_task())
    asyncio.create_task(google_sheets_sync_task())

if __name__ == "__main__":
    logging.info("🤖 Bot started (30-day cycle)")
    load_chat_state_all()
    acquire_single_instance_lock()
    try:
        executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
    except TerminatedByOtherGetUpdates:
        logging.error("⛔️ Бот остановлен: этот токен уже используется другим экземпляром или внешним сервисом.")

