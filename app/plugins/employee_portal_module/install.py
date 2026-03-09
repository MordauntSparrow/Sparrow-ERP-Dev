"""
Employee Portal install/upgrade: all table definitions and logic in this file.
Ensures ep_migrations, ep_messages, ep_todos. No external db/*.sql.
Run from repo root: python app/plugins/employee_portal_module/install.py install
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

MIGRATIONS_TABLE = "ep_migrations"
MODULE_TABLES = [MIGRATIONS_TABLE, "ep_messages", "ep_todos"]

# Full CREATE TABLE statements (all in this file)
SQL_CREATE_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS ep_migrations (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  filename VARCHAR(255) NOT NULL UNIQUE,
  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_EP_MESSAGES = """
CREATE TABLE IF NOT EXISTS ep_messages (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  contractor_id INT NOT NULL,
  source_module VARCHAR(64) NOT NULL,
  subject VARCHAR(255) NOT NULL,
  body TEXT,
  read_at DATETIME DEFAULT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_ep_messages_contractor (contractor_id),
  KEY idx_ep_messages_read (contractor_id, read_at),
  CONSTRAINT fk_ep_messages_contractor FOREIGN KEY (contractor_id)
    REFERENCES tb_contractors(id) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_EP_TODOS = """
CREATE TABLE IF NOT EXISTS ep_todos (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  contractor_id INT NOT NULL,
  source_module VARCHAR(64) NOT NULL,
  title VARCHAR(255) NOT NULL,
  link_url VARCHAR(512) DEFAULT NULL,
  due_date DATE DEFAULT NULL,
  completed_at DATETIME DEFAULT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_ep_todos_contractor (contractor_id),
  KEY idx_ep_todos_pending (contractor_id, completed_at),
  CONSTRAINT fk_ep_todos_contractor FOREIGN KEY (contractor_id)
    REFERENCES tb_contractors(id) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

CREATES = [SQL_CREATE_MIGRATIONS, SQL_CREATE_EP_MESSAGES, SQL_CREATE_EP_TODOS]


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
    parser = argparse.ArgumentParser(description="Employee Portal Module Installer")
    parser.add_argument("command", choices=["install", "upgrade", "uninstall"])
    parser.add_argument("--drop-data", action="store_true")
    args = parser.parse_args()
    if args.command == "install":
        install()
    elif args.command == "upgrade":
        upgrade()
    else:
        uninstall(drop_data=args.drop_data)
