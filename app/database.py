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
                receive_at TEXT,
                batch_no TEXT
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
                created_at TEXT NOT NULL,
                batch_no TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batches (
                batch_no TEXT PRIMARY KEY,
                sample_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '待出库',
                scheduled_outbound_time TEXT,
                estimated_arrival_deadline TEXT,
                total_boxes INTEGER NOT NULL DEFAULT 0,
                received_boxes INTEGER NOT NULL DEFAULT 0,
                missing_boxes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_by TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_boxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_no TEXT NOT NULL,
                box_code TEXT NOT NULL,
                box_batch_status TEXT NOT NULL DEFAULT '正常',
                received_at TEXT,
                missing_reason TEXT,
                missing_registered_at TEXT,
                missing_registered_by TEXT,
                missing_cancelled_at TEXT,
                missing_cancelled_by TEXT,
                missing_cancel_reason TEXT,
                UNIQUE(batch_no, box_code)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_no TEXT NOT NULL,
                box_code TEXT,
                action TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                role TEXT NOT NULL,
                operator TEXT NOT NULL,
                reason TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _migrate_db(conn)


def _migrate_db(conn):
    try:
        conn.execute("ALTER TABLE boxes ADD COLUMN batch_no TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE audit_log ADD COLUMN batch_no TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE batch_boxes ADD COLUMN missing_cancelled_at TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE batch_boxes ADD COLUMN missing_cancelled_by TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE batch_boxes ADD COLUMN missing_cancel_reason TEXT")
    except sqlite3.OperationalError:
        pass
