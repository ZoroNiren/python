# database.py
import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "bot.db")


def get_connection():
    return sqlite3.connect(DB_NAME)


def create_tables():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_onboarding (
        user_id INTEGER PRIMARY KEY,
        accepted INTEGER NOT NULL DEFAULT 0,
        accepted_as TEXT,
        accepted_at DATETIME
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        project_name TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        start_date DATETIME,
        end_date DATETIME,
        previous_balance REAL DEFAULT 0,
        reminder_sent INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        type TEXT CHECK(type IN ('income', 'expense')),
        name TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS incomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period_id INTEGER,
        category TEXT,
        amount REAL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period_id INTEGER,
        category TEXT,
        amount REAL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        period_id INTEGER,
        task_text TEXT,
        status TEXT CHECK(status IN ('done', 'in_progress', 'not_done')),
        fail_reason TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        project_id INTEGER,
        period_id INTEGER,
        remind_at DATETIME,
        type TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        file_id TEXT,
        file_name TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS quarter_goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        quarter_start DATE,
        quarter_end DATE,
        goal_text TEXT,
        status TEXT CHECK(status IN ('pending', 'achieved', 'not_achieved')) DEFAULT 'pending',
        fail_reason TEXT,
        checked_at DATETIME,
        notified_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_state (
        user_id INTEGER PRIMARY KEY,
        state_json TEXT NOT NULL,
        last_activity_ts INTEGER,
        r24_sent INTEGER DEFAULT 0,
        r48_sent INTEGER DEFAULT 0,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()


def add_reminder_column():
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE projects ADD COLUMN reminder_sent INTEGER DEFAULT 0")
        conn.commit()
        print("[DB] Column reminder_sent added")
    except sqlite3.OperationalError:
        pass
    conn.close()


def add_balance_columns():
    conn = get_connection()
    cursor = conn.cursor()
    for column_name in ("account_balance", "deposit_balance"):
        try:
            cursor.execute(f"ALTER TABLE periods ADD COLUMN {column_name} REAL DEFAULT 0")
            conn.commit()
            print(f"[DB] Column {column_name} added")
        except sqlite3.OperationalError:
            pass
    cursor.execute(
        """
        UPDATE periods
        SET account_balance = previous_balance
        WHERE COALESCE(account_balance, 0) = 0
          AND COALESCE(deposit_balance, 0) = 0
          AND COALESCE(previous_balance, 0) != 0
        """
    )
    conn.commit()
    conn.close()


def add_project_schedule_columns():
    conn = get_connection()
    cursor = conn.cursor()
    migrations = {
        "next_docs_request_at": "DATETIME",
        "docs_request_sent": "INTEGER DEFAULT 0",
    }
    for column_name, column_type in migrations.items():
        try:
            cursor.execute(f"ALTER TABLE projects ADD COLUMN {column_name} {column_type}")
            conn.commit()
            print(f"[DB] Column {column_name} added")
        except sqlite3.OperationalError:
            pass
    conn.close()


def is_user_onboarded(user_id: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT accepted FROM user_onboarding WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row and int(row[0]) == 1)


def set_user_onboarded(user_id: int, accepted_as: str | None = None) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO user_onboarding (user_id, accepted, accepted_as, accepted_at)
        VALUES (?, 1, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            accepted=1,
            accepted_as=excluded.accepted_as,
            accepted_at=CURRENT_TIMESTAMP
        """,
        (user_id, accepted_as),
    )
    conn.commit()
    conn.close()
