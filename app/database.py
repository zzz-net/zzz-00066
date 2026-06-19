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
                created_by TEXT,
                review_status TEXT NOT NULL DEFAULT '未开始',
                archived_at TEXT,
                archived_by TEXT
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                require_double_review INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_no TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '进行中',
                require_double_review INTEGER NOT NULL DEFAULT 0,
                initiated_by TEXT NOT NULL,
                initiated_role TEXT NOT NULL,
                initiated_at TEXT NOT NULL,
                handed_over_by TEXT,
                cancelled_at TEXT,
                cancelled_by TEXT,
                cancelled_reason TEXT,
                completed_at TEXT,
                UNIQUE(batch_no, status)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_review_boxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER NOT NULL,
                box_code TEXT NOT NULL,
                first_review_result TEXT,
                first_reviewer TEXT,
                first_review_role TEXT,
                first_review_reason TEXT,
                first_review_at TEXT,
                second_review_result TEXT,
                second_reviewer TEXT,
                second_review_role TEXT,
                second_review_reason TEXT,
                second_review_at TEXT,
                final_result TEXT,
                UNIQUE(review_id, box_code),
                FOREIGN KEY (review_id) REFERENCES batch_reviews(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                require_double_confirm INTEGER NOT NULL DEFAULT 0,
                allow_proxy_submit INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_no TEXT UNIQUE NOT NULL,
                batch_no TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '待确认',
                problem_type TEXT NOT NULL,
                evidence_desc TEXT,
                responsibility_judgment TEXT,
                deadline TEXT,
                conclusion TEXT,
                require_double_confirm INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL,
                created_role TEXT NOT NULL,
                submitted_by TEXT,
                proxy_submitted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                supervisor_confirmed INTEGER NOT NULL DEFAULT 0,
                supervisor_confirmed_by TEXT,
                supervisor_confirmed_at TEXT,
                qc_confirmed INTEGER NOT NULL DEFAULT 0,
                qc_confirmed_by TEXT,
                qc_confirmed_at TEXT,
                rejected_at TEXT,
                rejected_by TEXT,
                rejected_role TEXT,
                rejected_reason TEXT,
                withdrawn_at TEXT,
                withdrawn_by TEXT,
                withdrawn_reason TEXT,
                resubmitted_at TEXT,
                resubmitted_by TEXT,
                closed_at TEXT,
                closed_by TEXT,
                closed_role TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_ticket_boxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                box_code TEXT NOT NULL,
                UNIQUE(ticket_id, box_code),
                FOREIGN KEY (ticket_id) REFERENCES dispute_tickets(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                evidence_desc TEXT NOT NULL,
                added_by TEXT NOT NULL,
                added_role TEXT NOT NULL,
                added_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES dispute_tickets(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                role TEXT NOT NULL,
                operator TEXT NOT NULL,
                reason TEXT,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES dispute_tickets(id)
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
    try:
        conn.execute("ALTER TABLE batches ADD COLUMN review_status TEXT NOT NULL DEFAULT '未开始'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE batches ADD COLUMN archived_at TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE batches ADD COLUMN archived_by TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                require_double_review INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            )
            """
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_no TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '进行中',
                require_double_review INTEGER NOT NULL DEFAULT 0,
                initiated_by TEXT NOT NULL,
                initiated_role TEXT NOT NULL,
                initiated_at TEXT NOT NULL,
                handed_over_by TEXT,
                cancelled_at TEXT,
                cancelled_by TEXT,
                cancelled_reason TEXT,
                completed_at TEXT,
                UNIQUE(batch_no, status)
            )
            """
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_review_boxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER NOT NULL,
                box_code TEXT NOT NULL,
                first_review_result TEXT,
                first_reviewer TEXT,
                first_review_role TEXT,
                first_review_reason TEXT,
                first_review_at TEXT,
                second_review_result TEXT,
                second_reviewer TEXT,
                second_review_role TEXT,
                second_review_reason TEXT,
                second_review_at TEXT,
                final_result TEXT,
                UNIQUE(review_id, box_code),
                FOREIGN KEY (review_id) REFERENCES batch_reviews(id)
            )
            """
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                require_double_confirm INTEGER NOT NULL DEFAULT 0,
                allow_proxy_submit INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            )
            """
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_no TEXT UNIQUE NOT NULL,
                batch_no TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '待确认',
                problem_type TEXT NOT NULL,
                evidence_desc TEXT,
                responsibility_judgment TEXT,
                deadline TEXT,
                conclusion TEXT,
                require_double_confirm INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL,
                created_role TEXT NOT NULL,
                submitted_by TEXT,
                proxy_submitted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                supervisor_confirmed INTEGER NOT NULL DEFAULT 0,
                supervisor_confirmed_by TEXT,
                supervisor_confirmed_at TEXT,
                qc_confirmed INTEGER NOT NULL DEFAULT 0,
                qc_confirmed_by TEXT,
                qc_confirmed_at TEXT,
                rejected_at TEXT,
                rejected_by TEXT,
                rejected_role TEXT,
                rejected_reason TEXT,
                withdrawn_at TEXT,
                withdrawn_by TEXT,
                withdrawn_reason TEXT,
                resubmitted_at TEXT,
                resubmitted_by TEXT,
                closed_at TEXT,
                closed_by TEXT,
                closed_role TEXT
            )
            """
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_ticket_boxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                box_code TEXT NOT NULL,
                UNIQUE(ticket_id, box_code),
                FOREIGN KEY (ticket_id) REFERENCES dispute_tickets(id)
            )
            """
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                evidence_desc TEXT NOT NULL,
                added_by TEXT NOT NULL,
                added_role TEXT NOT NULL,
                added_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES dispute_tickets(id)
            )
            """
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispute_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                role TEXT NOT NULL,
                operator TEXT NOT NULL,
                reason TEXT,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES dispute_tickets(id)
            )
            """
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE dispute_config ADD COLUMN allow_proxy_submit INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE dispute_tickets ADD COLUMN submitted_by TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE dispute_tickets ADD COLUMN proxy_submitted INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
