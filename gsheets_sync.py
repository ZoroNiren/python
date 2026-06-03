import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials


@dataclass(frozen=True)
class SheetsConfig:
    sqlite_path: str
    service_account_json: str
    spreadsheet_id: str
    finance_sheet: str = "Финансы"
    traction_sheet: str = "Трекшн"
    state_path: str = "sync_state.json"


# === ВАЖНО: Заголовки, которые должны быть в 1-й строке листа (A1:...).
# Если у тебя в Google Sheets другие названия колонок — поменяй тут.
FINANCE_HEADERS = [
    "project",
    "period_start",
    "period_end",
    "section",      # opening_balance / income / expense / result
    "category",
    "amount",
    "note",
    "source_table",
    "source_id",
]

TRACTION_HEADERS = [
    "project",
    "period_start",
    "period_end",
    "type",         # quarter_goal / task
    "text",
    "status",
    "fail_reason",
    "source_table",
    "source_id",
]


def _load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(path: str, state: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _db_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return r is not None


def _table_cols(conn: sqlite3.Connection, table: str) -> List[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise RuntimeError(f"Таблица '{table}' не найдена.")
    return [r["name"] for r in rows]


def _pk_or_rowid(conn: sqlite3.Connection, table: str) -> str:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    for r in rows:
        if r["pk"] == 1:
            return r["name"]
    cols = [r["name"] for r in rows]
    if "id" in cols:
        return "id"
    return "rowid"


def _gs_client(service_account_json: str) -> gspread.Client:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(service_account_json, scopes=scopes)
    return gspread.authorize(creds)


def _get_header(ws) -> List[str]:
    values = ws.get_all_values()
    if not values:
        raise RuntimeError(f"Лист '{ws.title}' пустой. Нужна шапка в первой строке.")
    return [c.strip() for c in values[0]]


def _ensure_header(ws, expected: List[str]) -> None:
    header = _get_header(ws)
    if header != expected:
        raise RuntimeError(
            f"Шапка листа '{ws.title}' не совпадает с ожидаемой.\n"
            f"Ожидаю: {expected}\n"
            f"В таблице: {header}\n"
            f"Либо приведи шапку к ожидаемой, либо поменяй FINANCE_HEADERS/TRACTION_HEADERS в коде."
        )


def _append_rows(ws, rows: List[List[Any]]) -> None:
    if not rows:
        return

    rows = _sanitize_rows(rows)

    ws.append_rows(rows, value_input_option="USER_ENTERED")
    


def _projects_map(conn: sqlite3.Connection) -> Dict[int, str]:
    rows = conn.execute("SELECT id, project_name FROM projects").fetchall()
    return {int(r["id"]): str(r["project_name"]) for r in rows}


def sync_once(cfg: SheetsConfig) -> Dict[str, int]:
    state = _load_state(cfg.state_path)

    conn = _db_connect(cfg.sqlite_path)
    try:
        projects = _projects_map(conn)

        gc = _gs_client(cfg.service_account_json)
        sh = gc.open_by_key(cfg.spreadsheet_id)
        ws_fin = sh.worksheet(cfg.finance_sheet)
        ws_tr = sh.worksheet(cfg.traction_sheet)

        _ensure_header(ws_fin, FINANCE_HEADERS)
        _ensure_header(ws_tr, TRACTION_HEADERS)

        stats = {
            "finance_rows": 0,
            "traction_rows": 0,
        }

        stats["finance_rows"] += _sync_periods(conn, ws_fin, state, projects)
        stats["finance_rows"] += _sync_incomes_agg(conn, ws_fin, state, projects)
        stats["finance_rows"] += _sync_expenses_agg(conn, ws_fin, state, projects)
        stats["finance_rows"] += _sync_period_results(conn, ws_fin, state, projects)

        stats["traction_rows"] += _sync_tasks(conn, ws_tr, state, projects)
        stats["traction_rows"] += _sync_quarter_goals(conn, ws_tr, state, projects)

        _save_state(cfg.state_path, state)
        return stats
    finally:
        conn.close()


def _sync_periods(conn, ws_fin, state, projects) -> int:
    if not _table_exists(conn, "periods"):
        return 0

    pk = _pk_or_rowid(conn, "periods")
    last_id = int(state.get("periods.last_id", 0))

    rows = conn.execute(
        f"""
        SELECT {pk} AS id, project_id, start_date, end_date, previous_balance
        FROM periods
        WHERE {pk} > ?
        ORDER BY {pk} ASC
        """,
        (last_id,),
    ).fetchall()

    out = []
    max_id = last_id

    for r in rows:
        rid = int(r["id"])
        max_id = max(max_id, rid)
        project = projects.get(int(r["project_id"]), str(r["project_id"]))
        out.append([
            project,
            r["start_date"],
            r["end_date"],
            "opening_balance",
            "",
            r["previous_balance"] if r["previous_balance"] is not None else 0,
            "Входящий остаток периода",
            "periods",
            rid,
        ])

    _append_rows(ws_fin, out)
    if max_id != last_id:
        state["periods.last_id"] = max_id
    return len(out)

import math

def _sanitize_rows(rows):
    clean = []

    for row in rows:
        new_row = []

        for v in row:
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                new_row.append("")
            else:
                new_row.append(v)

        clean.append(new_row)

    return clean

def _sync_incomes_agg(conn, ws_fin, state, projects) -> int:
    if not _table_exists(conn, "incomes"):
        return 0

    pk = _pk_or_rowid(conn, "incomes")
    last_id = int(state.get("incomes.last_id", 0))

    rows = conn.execute(
        f"""
        SELECT
            i.{pk} AS id,
            p.project_id AS project_id,
            p.start_date AS period_start,
            p.end_date AS period_end,
            i.category AS category,
            i.amount AS amount
        FROM incomes i
        JOIN periods p ON p.id = i.period_id
        WHERE i.{pk} > ?
        ORDER BY i.{pk} ASC
        """,
        (last_id,),
    ).fetchall()

    out = []
    max_id = last_id
    for r in rows:
        rid = int(r["id"])
        max_id = max(max_id, rid)
        project = projects.get(int(r["project_id"]), str(r["project_id"]))
        out.append([
            project,
            r["period_start"],
            r["period_end"],
            "income",
            r["category"] or "",
            r["amount"] if r["amount"] is not None else 0,
            "",
            "incomes",
            rid,
        ])

    _append_rows(ws_fin, out)
    if max_id != last_id:
        state["incomes.last_id"] = max_id
    return len(out)


def _sync_expenses_agg(conn, ws_fin, state, projects) -> int:
    if not _table_exists(conn, "expenses"):
        return 0

    pk = _pk_or_rowid(conn, "expenses")
    last_id = int(state.get("expenses.last_id", 0))

    rows = conn.execute(
        f"""
        SELECT
            e.{pk} AS id,
            p.project_id AS project_id,
            p.start_date AS period_start,
            p.end_date AS period_end,
            e.category AS category,
            e.amount AS amount
        FROM expenses e
        JOIN periods p ON p.id = e.period_id
        WHERE e.{pk} > ?
        ORDER BY e.{pk} ASC
        """,
        (last_id,),
    ).fetchall()

    out = []
    max_id = last_id
    for r in rows:
        rid = int(r["id"])
        max_id = max(max_id, rid)
        project = projects.get(int(r["project_id"]), str(r["project_id"]))
        out.append([
            project,
            r["period_start"],
            r["period_end"],
            "expense",
            r["category"] or "",
            r["amount"] if r["amount"] is not None else 0,
            "",
            "expenses",
            rid,
        ])

    _append_rows(ws_fin, out)
    if max_id != last_id:
        state["expenses.last_id"] = max_id
    return len(out)


def _sync_period_results(conn, ws_fin, state, projects) -> int:
    if not _table_exists(conn, "periods"):
        return 0

    # Результат периода считаем как: previous_balance + sum(incomes) - sum(expenses)
    # И пишем 1 строкой на каждый новый период (по id periods.last_result_id)
    pk = _pk_or_rowid(conn, "periods")
    last_id = int(state.get("periods.last_result_id", 0))

    rows = conn.execute(
        f"""
        SELECT
            p.{pk} AS id,
            p.project_id,
            p.start_date,
            p.end_date,
            COALESCE(p.previous_balance, 0) AS previous_balance,
            COALESCE((SELECT SUM(amount) FROM incomes i WHERE i.period_id = p.id), 0) AS total_income,
            COALESCE((SELECT SUM(amount) FROM expenses e WHERE e.period_id = p.id), 0) AS total_expense
        FROM periods p
        WHERE p.{pk} > ?
        ORDER BY p.{pk} ASC
        """,
        (last_id,),
    ).fetchall()

    out = []
    max_id = last_id
    for r in rows:
        rid = int(r["id"])
        max_id = max(max_id, rid)

        result = float(r["previous_balance"]) + float(r["total_income"]) - float(r["total_expense"])
        project = projects.get(int(r["project_id"]), str(r["project_id"]))

        out.append([
            project,
            r["start_date"],
            r["end_date"],
            "result",
            "",
            result,
            f"prev={r['previous_balance']}, income={r['total_income']}, expense={r['total_expense']}",
            "periods",
            rid,
        ])

    _append_rows(ws_fin, out)
    if max_id != last_id:
        state["periods.last_result_id"] = max_id
    return len(out)


def _sync_tasks(conn, ws_tr, state, projects) -> int:
    if not _table_exists(conn, "tasks"):
        return 0

    pk = _pk_or_rowid(conn, "tasks")
    last_id = int(state.get("tasks.last_id", 0))

    cols = set(_table_cols(conn, "tasks"))
    fail_col = "fail_reason" if "fail_reason" in cols else None

    rows = conn.execute(
        f"""
        SELECT
            t.{pk} AS id,
            t.project_id,
            p.start_date AS period_start,
            p.end_date AS period_end,
            t.task_text,
            t.status
            {", t.fail_reason AS fail_reason" if fail_col else ""}
        FROM tasks t
        LEFT JOIN periods p ON p.id = t.period_id
        WHERE t.{pk} > ?
        ORDER BY t.{pk} ASC
        """,
        (last_id,),
    ).fetchall()

    out = []
    max_id = last_id
    for r in rows:
        rid = int(r["id"])
        max_id = max(max_id, rid)
        project = projects.get(int(r["project_id"]), str(r["project_id"]))
        out.append([
            project,
            r["period_start"] or "",
            r["period_end"] or "",
            "task",
            r["task_text"] or "",
            r["status"] or "",
            r["fail_reason"] if fail_col else "",
            "tasks",
            rid,
        ])

    _append_rows(ws_tr, out)
    if max_id != last_id:
        state["tasks.last_id"] = max_id
    return len(out)


def _sync_quarter_goals(conn, ws_tr, state, projects) -> int:
    if not _table_exists(conn, "quarter_goals"):
        return 0

    pk = _pk_or_rowid(conn, "quarter_goals")
    last_id = int(state.get("quarter_goals.last_id", 0))

    cols = set(_table_cols(conn, "quarter_goals"))
    status_col = "status" if "status" in cols else None
    fail_col = "fail_reason" if "fail_reason" in cols else None

    rows = conn.execute(
        f"""
        SELECT
            q.{pk} AS id,
            q.project_id,
            q.quarter_start,
            q.quarter_end,
            q.goal_text
            {", q.status AS status" if status_col else ""}
            {", q.fail_reason AS fail_reason" if fail_col else ""}
        FROM quarter_goals q
        WHERE q.{pk} > ?
        ORDER BY q.{pk} ASC
        """,
        (last_id,),
    ).fetchall()

    out = []
    max_id = last_id
    for r in rows:
        rid = int(r["id"])
        max_id = max(max_id, rid)
        project = projects.get(int(r["project_id"]), str(r["project_id"]))
        out.append([
            project,
            r["quarter_start"] or "",
            r["quarter_end"] or "",
            "quarter_goal",
            r["goal_text"] or "",
            r["status"] if status_col else "",
            r["fail_reason"] if fail_col else "",
            "quarter_goals",
            rid,
        ])

    _append_rows(ws_tr, out)
    if max_id != last_id:
        state["quarter_goals.last_id"] = max_id
    return len(out)