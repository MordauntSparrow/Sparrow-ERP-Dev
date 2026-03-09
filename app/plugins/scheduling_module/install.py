"""
Scheduling module install/upgrade: all table definitions and logic in this file.
Ensures schedule_migrations, schedule_shifts, schedule_availability, schedule_time_off,
shift_swap_requests, schedule_templates, schedule_template_slots. Depends on time_billing (tb_contractors, clients, sites, job_types). No external db/*.sql.
Run from repo root: python app/plugins/scheduling_module/install.py install
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

MIGRATIONS_TABLE = "schedule_migrations"
MODULE_TABLES = [
    MIGRATIONS_TABLE,
    "schedule_shifts",
    "schedule_availability",
    "schedule_time_off",
    "shift_swap_requests",
    "schedule_templates",
    "schedule_template_slots",
]

SQL_CREATE_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schedule_migrations (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  filename VARCHAR(255) NOT NULL UNIQUE,
  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_SCHEDULE_SHIFTS = """
CREATE TABLE IF NOT EXISTS schedule_shifts (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  contractor_id INT NOT NULL,
  client_id INT NOT NULL,
  site_id INT DEFAULT NULL,
  job_type_id INT NOT NULL,
  work_date DATE NOT NULL,
  scheduled_start TIME NOT NULL,
  scheduled_end TIME NOT NULL,
  actual_start TIME DEFAULT NULL,
  actual_end TIME DEFAULT NULL,
  break_mins INT NOT NULL DEFAULT 0,
  notes TEXT,
  status ENUM('draft','published','in_progress','completed','cancelled','no_show') NOT NULL DEFAULT 'draft',
  source ENUM('manual','ventus','scheduler','work_module') NOT NULL DEFAULT 'manual',
  external_id VARCHAR(255) DEFAULT NULL,
  runsheet_id BIGINT DEFAULT NULL,
  runsheet_assignment_id BIGINT DEFAULT NULL,
  labour_cost DECIMAL(10,2) DEFAULT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_ss_contractor_date (contractor_id, work_date),
  KEY idx_ss_date_status (work_date, status),
  KEY idx_ss_client_date (client_id, work_date),
  KEY idx_ss_source (source),
  KEY idx_ss_runsheet (runsheet_id),
  CONSTRAINT fk_ss_contractor FOREIGN KEY (contractor_id) REFERENCES tb_contractors(id) ON DELETE CASCADE,
  CONSTRAINT fk_ss_client FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
  CONSTRAINT fk_ss_site FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE SET NULL,
  CONSTRAINT fk_ss_jobtype FOREIGN KEY (job_type_id) REFERENCES job_types(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_SCHEDULE_AVAILABILITY = """
CREATE TABLE IF NOT EXISTS schedule_availability (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  contractor_id INT NOT NULL,
  day_of_week TINYINT NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  effective_from DATE NOT NULL,
  effective_to DATE DEFAULT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sa_contractor (contractor_id),
  KEY idx_sa_dow (contractor_id, day_of_week),
  CONSTRAINT fk_sa_contractor FOREIGN KEY (contractor_id) REFERENCES tb_contractors(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_SCHEDULE_TIME_OFF = """
CREATE TABLE IF NOT EXISTS schedule_time_off (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  contractor_id INT NOT NULL,
  type ENUM('annual','sickness','other') NOT NULL DEFAULT 'annual',
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  reason VARCHAR(255) DEFAULT NULL,
  status ENUM('requested','approved','rejected') NOT NULL DEFAULT 'requested',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sto_contractor (contractor_id),
  KEY idx_sto_dates (start_date, end_date),
  CONSTRAINT fk_sto_contractor FOREIGN KEY (contractor_id) REFERENCES tb_contractors(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_SHIFT_SWAP_REQUESTS = """
CREATE TABLE IF NOT EXISTS shift_swap_requests (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  shift_id BIGINT NOT NULL,
  requester_contractor_id INT NOT NULL,
  requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status ENUM('open','claimed','approved','rejected','cancelled') NOT NULL DEFAULT 'open',
  claimer_contractor_id INT DEFAULT NULL,
  claimed_at DATETIME DEFAULT NULL,
  resolved_at DATETIME DEFAULT NULL,
  resolved_by INT DEFAULT NULL,
  notes TEXT,
  KEY idx_ssr_shift (shift_id),
  KEY idx_ssr_requester (requester_contractor_id),
  KEY idx_ssr_status (status),
  CONSTRAINT fk_ssr_shift FOREIGN KEY (shift_id) REFERENCES schedule_shifts(id) ON DELETE CASCADE,
  CONSTRAINT fk_ssr_requester FOREIGN KEY (requester_contractor_id) REFERENCES tb_contractors(id) ON DELETE CASCADE,
  CONSTRAINT fk_ssr_claimer FOREIGN KEY (claimer_contractor_id) REFERENCES tb_contractors(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_SCHEDULE_TEMPLATES = """
CREATE TABLE IF NOT EXISTS schedule_templates (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(150) NOT NULL,
  client_id INT DEFAULT NULL,
  site_id INT DEFAULT NULL,
  job_type_id INT DEFAULT NULL,
  active TINYINT(1) NOT NULL DEFAULT 1,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY fk_st_client (client_id),
  KEY fk_st_site (site_id),
  KEY fk_st_jobtype (job_type_id),
  CONSTRAINT fk_st_client FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL,
  CONSTRAINT fk_st_site FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE SET NULL,
  CONSTRAINT fk_st_jobtype FOREIGN KEY (job_type_id) REFERENCES job_types(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQL_CREATE_SCHEDULE_TEMPLATE_SLOTS = """
CREATE TABLE IF NOT EXISTS schedule_template_slots (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  template_id INT NOT NULL,
  day_of_week TINYINT NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  position_label VARCHAR(100) DEFAULT NULL,
  KEY fk_sts_template (template_id),
  CONSTRAINT fk_sts_template FOREIGN KEY (template_id) REFERENCES schedule_templates(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

CREATES = [
    SQL_CREATE_MIGRATIONS,
    SQL_CREATE_SCHEDULE_SHIFTS,
    SQL_CREATE_SCHEDULE_AVAILABILITY,
    SQL_CREATE_SCHEDULE_TIME_OFF,
    SQL_CREATE_SHIFT_SWAP_REQUESTS,
    SQL_CREATE_SCHEDULE_TEMPLATES,
    SQL_CREATE_SCHEDULE_TEMPLATE_SLOTS,
]


def _run_sql(conn, sql):
    for stmt in [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]:
        cur = conn.cursor()
        try:
            cur.execute(stmt)
        finally:
            cur.close()
    conn.commit()


def _column_exists(conn, table, column):
    cur = conn.cursor()
    try:
        cur.execute("SHOW COLUMNS FROM `{}` LIKE %s".format(table), (column,))
        return bool(cur.fetchone())
    finally:
        cur.close()


def ensure_tables(conn):
    for sql in CREATES:
        _run_sql(conn, sql)
    # Backfill type on schedule_time_off if table existed from before we added the column (all in this file)
    cur = conn.cursor()
    try:
        cur.execute("SHOW TABLES LIKE 'schedule_time_off'")
        if cur.fetchone() and not _column_exists(conn, "schedule_time_off", "type"):
            cur.execute(
                "ALTER TABLE schedule_time_off ADD COLUMN type ENUM('annual','sickness','other') NOT NULL DEFAULT 'annual' AFTER contractor_id"
            )
            conn.commit()
    finally:
        cur.close()


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
    parser = argparse.ArgumentParser(description="Scheduling Module Installer")
    parser.add_argument("command", choices=["install", "upgrade", "uninstall"])
    parser.add_argument("--drop-data", action="store_true")
    args = parser.parse_args()
    if args.command == "install":
        install()
    elif args.command == "upgrade":
        upgrade()
    else:
        uninstall(drop_data=args.drop_data)
