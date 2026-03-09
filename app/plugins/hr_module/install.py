"""
HR module install/upgrade: all table definitions and logic in this file.
Ensures hr_migrations, hr_staff_details, hr_document_requests, hr_document_uploads. No external db/*.sql.
Run from repo root: python app/plugins/hr_module/install.py install
Or from plugin dir: python install.py install
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PLUGIN_DIR = HERE.parent
PLUGINS_DIR = PLUGIN_DIR.parent
APP_ROOT = PLUGINS_DIR.parent
PROJECT_ROOT = APP_ROOT.parent
for p in (str(PROJECT_ROOT), str(APP_ROOT), str(PLUGIN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)
from app.objects import get_db_connection  # noqa: E402

MIGRATIONS_TABLE = "hr_migrations"
MODULE_TABLES = [MIGRATIONS_TABLE, "hr_staff_details", "hr_document_requests", "hr_document_uploads"]

SQL_CREATE_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS hr_migrations (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  filename VARCHAR(255) NOT NULL UNIQUE,
  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_HR_STAFF_DETAILS = """
CREATE TABLE IF NOT EXISTS hr_staff_details (
  contractor_id INT NOT NULL PRIMARY KEY,
  phone VARCHAR(64) DEFAULT NULL,
  address_line1 VARCHAR(255) DEFAULT NULL,
  address_line2 VARCHAR(255) DEFAULT NULL,
  postcode VARCHAR(32) DEFAULT NULL,
  emergency_contact_name VARCHAR(255) DEFAULT NULL,
  emergency_contact_phone VARCHAR(64) DEFAULT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_hrsd_contractor FOREIGN KEY (contractor_id) REFERENCES tb_contractors(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_HR_DOCUMENT_REQUESTS = """
CREATE TABLE IF NOT EXISTS hr_document_requests (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  contractor_id INT NOT NULL,
  title VARCHAR(255) NOT NULL,
  description TEXT,
  required_by_date DATE DEFAULT NULL,
  status ENUM('pending','uploaded','approved','overdue') NOT NULL DEFAULT 'pending',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_hrdr_contractor (contractor_id),
  KEY idx_hrdr_status (contractor_id, status),
  CONSTRAINT fk_hrdr_contractor FOREIGN KEY (contractor_id) REFERENCES tb_contractors(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_HR_DOCUMENT_UPLOADS = """
CREATE TABLE IF NOT EXISTS hr_document_uploads (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  request_id INT NOT NULL,
  file_path VARCHAR(512) NOT NULL,
  file_name VARCHAR(255) DEFAULT NULL,
  uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_hrdu_request (request_id),
  CONSTRAINT fk_hrdu_request FOREIGN KEY (request_id) REFERENCES hr_document_requests(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

CREATES = [
    SQL_CREATE_MIGRATIONS,
    SQL_CREATE_HR_STAFF_DETAILS,
    SQL_CREATE_HR_DOCUMENT_REQUESTS,
    SQL_CREATE_HR_DOCUMENT_UPLOADS,
]


def _run_sql(conn, sql):
    for stmt in [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]:
        cur = conn.cursor()
        try:
            cur.execute(stmt)
        finally:
            cur.close()
    conn.commit()


def ensure_tables(conn):
    for sql in CREATES:
        _run_sql(conn, sql)


def install():
    """Ensure all MODULE_TABLES exist (idempotent)."""
    conn = get_db_connection()
    try:
        ensure_tables(conn)
    finally:
        conn.close()


def upgrade():
    """Ensure all MODULE_TABLES exist (same as install, idempotent)."""
    install()


def uninstall(drop_data: bool = False):
    if not drop_data:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in reversed(MODULE_TABLES):
            cur.execute(f"DROP TABLE IF EXISTS `{t}`")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="HR Module Installer")
    parser.add_argument("command", choices=["install", "upgrade", "uninstall"])
    parser.add_argument("--drop-data", action="store_true")
    args = parser.parse_args()
    if args.command == "install":
        install()
    elif args.command == "upgrade":
        upgrade()
    else:
        uninstall(drop_data=args.drop_data)
