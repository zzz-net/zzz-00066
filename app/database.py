import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "cold_chain.db"


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thresholds (
                sample_type TEXT PRIMARY KEY,
                temp_min REAL NOT NULL,
                temp_max REAL NOT NULL,
                timeout_minutes INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS boxes (
                box_code TEXT PRIMARY KEY,
                sample_type TEXT NOT NULL,
                current_temp REAL,
                status TEXT NOT NULL DEFAULT '待出库',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                dispatch_at TEXT,
                receive_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                box_code TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                role TEXT NOT NULL,
                operator TEXT NOT NULL,
                reason TEXT,
                temp_at_action REAL,
                created_at TEXT NOT NULL
            )
            """
        )
