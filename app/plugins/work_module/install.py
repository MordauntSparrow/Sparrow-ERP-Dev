"""
Work module install/upgrade: all table definitions and logic in this file.
Ensures work_migrations, work_photos. Depends on schedule_shifts and tb_contractors. No external db/*.sql.
Run from repo root: python app/plugins/work_module/install.py install
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

MIGRATIONS_TABLE = "work_migrations"
MODULE_TABLES = [MIGRATIONS_TABLE, "work_photos"]

SQL_CREATE_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS work_migrations (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  filename VARCHAR(255) NOT NULL UNIQUE,
  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_WORK_PHOTOS = """
CREATE TABLE IF NOT EXISTS work_photos (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  shift_id BIGINT NOT NULL,
  contractor_id INT NOT NULL,
  file_path VARCHAR(512) NOT NULL,
  file_name VARCHAR(255) DEFAULT NULL,
  mime_type VARCHAR(128) DEFAULT NULL,
  caption VARCHAR(500) DEFAULT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_wp_shift (shift_id),
  KEY idx_wp_contractor (contractor_id),
  CONSTRAINT fk_wp_shift FOREIGN KEY (shift_id) REFERENCES schedule_shifts(id) ON DELETE CASCADE,
  CONSTRAINT fk_wp_contractor FOREIGN KEY (contractor_id) REFERENCES tb_contractors(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

CREATES = [SQL_CREATE_MIGRATIONS, SQL_CREATE_WORK_PHOTOS]


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
    parser = argparse.ArgumentParser(description="Work Module Installer")
    parser.add_argument("command", choices=["install", "upgrade", "uninstall"])
    parser.add_argument("--drop-data", action="store_true")
    args = parser.parse_args()
    if args.command == "install":
        install()
    elif args.command == "upgrade":
        upgrade()
    else:
        uninstall(drop_data=args.drop_data)
