import sys
import os
from pathlib import Path

# Bootstrap import paths (project/app/plugin)
HERE = Path(__file__).resolve()
PLUGIN_DIR = HERE.parent
PLUGINS_DIR = PLUGIN_DIR.parent
APP_ROOT = PLUGINS_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for p in (str(PROJECT_ROOT), str(APP_ROOT), str(PLUGIN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.objects import get_db_connection  # noqa: E402

MIGRATIONS_TABLE = "ventus_response_migrations"

BASE_TRIAGE_FORM_SEEDS = [
    (
        "urgent_care",
        "Private Urgent Care",
        "Primary urgent care pathway with exclusion screening.",
        '{"show_exclusions": true, "questions": [{"key":"is_stable","label":"Patient clinically stable?","type":"select","options":["unknown","yes","no"],"required":true},{"key":"primary_symptom","label":"Primary symptom","type":"text","required":true},{"key":"pain_score","label":"Pain score (0-10)","type":"number","min":0,"max":10},{"key":"red_flags","label":"Observed red flags","type":"textarea"}]}',
        1,
        1
    ),
    (
        "emergency_999",
        "999 Emergency",
        "Emergency dispatch workflow with critical incident prompts.",
        '{"show_exclusions": false, "questions": [{"key":"conscious","label":"Conscious?","type":"select","options":["unknown","yes","no"],"required":true},{"key":"breathing","label":"Breathing normally?","type":"select","options":["unknown","yes","no"],"required":true},{"key":"major_bleeding","label":"Major bleeding?","type":"select","options":["unknown","yes","no"],"required":true},{"key":"immediate_danger","label":"Immediate scene danger","type":"textarea"}]}',
        1,
        0
    ),
    (
        "event_medical",
        "Event Medical",
        "Event-specific intake with location and welfare context.",
        '{"show_exclusions": false, "questions": [{"key":"event_name","label":"Event name","type":"text","required":true},{"key":"event_zone","label":"Event zone / stand","type":"text"},{"key":"security_required","label":"Security required?","type":"select","options":["unknown","yes","no"]},{"key":"crowd_density","label":"Crowd density","type":"select","options":["low","medium","high","unknown"]}]}',
        1,
        0
    ),
    (
        "security_response",
        "Security Response",
        "Security team dispatch for incidents and welfare escalations.",
        '{"show_exclusions": false, "questions": [{"key":"incident_type","label":"Incident type","type":"select","options":["theft","violence","trespass","welfare","other"],"required":true},{"key":"threat_level","label":"Threat level","type":"select","options":["low","medium","high","critical"],"required":true},{"key":"suspect_description","label":"Suspect description","type":"textarea"},{"key":"police_notified","label":"Police already notified?","type":"select","options":["unknown","yes","no"]}]}',
        1,
        0
    ),
    (
        "private_police",
        "Private Police Support",
        "Evidence and scene-control workflow for private policing teams.",
        '{"show_exclusions": false, "questions": [{"key":"offence_category","label":"Offence category","type":"select","options":["public_order","assault","criminal_damage","traffic","other"],"required":true},{"key":"evidence_required","label":"Evidence capture required?","type":"select","options":["unknown","yes","no"]},{"key":"scene_contained","label":"Scene contained?","type":"select","options":["unknown","yes","no"]},{"key":"units_requested","label":"Units requested","type":"number","min":1,"max":20}]}',
        1,
        0
    ),
    (
        "vehicle_recovery",
        "Vehicle Recovery",
        "Roadside recovery, tow allocation, and safety assessment.",
        '{"show_exclusions": false, "questions": [{"key":"vehicle_type","label":"Vehicle type","type":"select","options":["car","van","truck","motorcycle","other"],"required":true},{"key":"driveable","label":"Vehicle driveable?","type":"select","options":["unknown","yes","no"],"required":true},{"key":"hazard_present","label":"Fuel/leak/fire hazard?","type":"select","options":["unknown","yes","no"]},{"key":"recovery_priority","label":"Recovery priority","type":"select","options":["routine","urgent","critical"]}]}',
        1,
        0
    ),
    (
        "welfare_check",
        "Welfare Check",
        "Safeguarding and welfare follow-up workflow.",
        '{"show_exclusions": false, "questions": [{"key":"welfare_trigger","label":"Welfare trigger","type":"text","required":true},{"key":"contact_made","label":"Contact made with person?","type":"select","options":["unknown","yes","no"],"required":true},{"key":"safeguarding_risk","label":"Safeguarding risk","type":"select","options":["low","medium","high","unknown"]},{"key":"next_of_kin_notified","label":"Next of kin notified?","type":"select","options":["unknown","yes","no"]}]}',
        1,
        0
    ),
    (
        "patient_transport",
        "Patient Transport",
        "Non-emergency and scheduled patient transport dispatch.",
        '{"show_exclusions": false, "questions": [{"key":"pickup_type","label":"Pickup type","type":"select","options":["hospital","home","care_home","event"],"required":true},{"key":"mobility_support","label":"Mobility support","type":"select","options":["none","wheelchair","stretcher","bariatric"],"required":true},{"key":"escort_required","label":"Escort required?","type":"select","options":["unknown","yes","no"]},{"key":"ready_time","label":"Ready time notes","type":"text"}]}',
        1,
        0
    ),
    (
        "mental_health_support",
        "Mental Health Support",
        "Crisis de-escalation and specialist welfare deployment.",
        '{"show_exclusions": false, "questions": [{"key":"immediate_risk","label":"Immediate self-harm risk?","type":"select","options":["unknown","yes","no"],"required":true},{"key":"agitation_level","label":"Agitation level","type":"select","options":["low","medium","high","critical"]},{"key":"known_history","label":"Known mental health history","type":"textarea"},{"key":"safe_place_available","label":"Safe place available?","type":"select","options":["unknown","yes","no"]}]}',
        1,
        0
    ),
    (
        "fire_support",
        "Fire Support",
        "Support dispatch for fire standby and incident support teams.",
        '{"show_exclusions": false, "questions": [{"key":"fire_type","label":"Fire type","type":"select","options":["structural","vehicle","wildland","electrical","unknown"],"required":true},{"key":"casualties_reported","label":"Casualties reported?","type":"select","options":["unknown","yes","no"]},{"key":"access_blocked","label":"Access blocked?","type":"select","options":["unknown","yes","no"]},{"key":"water_supply_issue","label":"Water supply issue?","type":"select","options":["unknown","yes","no"]}]}',
        1,
        0
    ),
    (
        "search_and_rescue",
        "Search and Rescue",
        "Search planning and rescue deployment workflow.",
        '{"show_exclusions": false, "questions": [{"key":"missing_person_age","label":"Missing person age", "type":"number","min":0,"max":120},{"key":"last_seen_location","label":"Last seen location","type":"text","required":true},{"key":"terrain_type","label":"Terrain type","type":"select","options":["urban","rural","coastal","mountain","woodland"],"required":true},{"key":"time_missing_hours","label":"Time missing (hours)","type":"number","min":0,"max":240}]}',
        1,
        0
    ),
]

DEMO_TRIAGE_FORM_SEEDS = [
    (
        "training_simulation",
        "Training Simulation",
        "Demo/training profile for onboarding and drills.",
        '{"show_exclusions": false, "questions": [{"key":"scenario_name","label":"Scenario name","type":"text","required":true},{"key":"complexity","label":"Complexity","type":"select","options":["low","medium","high"]},{"key":"observer_notes","label":"Observer notes","type":"textarea"}]}',
        1,
        0
    ),
    (
        "multi_agency_coordination",
        "Multi-Agency Coordination",
        "Demo profile for multi-service command exercises.",
        '{"show_exclusions": false, "questions": [{"key":"lead_agency","label":"Lead agency","type":"text","required":true},{"key":"agencies_involved","label":"Agencies involved","type":"textarea"},{"key":"comms_channel","label":"Primary comms channel","type":"text"},{"key":"command_level","label":"Command level","type":"select","options":["bronze","silver","gold"]}]}',
        1,
        0
    ),
]


def table_exists(conn, name):
    cur = conn.cursor()
    try:
        cur.execute("SHOW TABLES LIKE %s", (name,))
        return bool(cur.fetchone())
    finally:
        try:
            cur.close()
        except Exception:
            pass


def create_migrations_table(conn):
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{MIGRATIONS_TABLE}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                filename VARCHAR(255) NOT NULL UNIQUE,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        conn.commit()
        print(f"Ensured table exists: {MIGRATIONS_TABLE}")
    finally:
        try:
            cur.close()
        except Exception:
            pass


def create_table(conn, name, columns_sql):
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{name}` (
                {columns_sql}
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        conn.commit()
        print(f"Created or ensured table: {name}")
    finally:
        try:
            cur.close()
        except Exception:
            pass


def install(seed_demo: bool = False):
    conn = get_db_connection()
    try:
        create_migrations_table(conn)

        # response_triage: store original triage payloads and resolved coordinates
        create_table(
            conn,
            "response_triage",
            """
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_by VARCHAR(120),
            vita_record_id VARCHAR(120),
            first_name VARCHAR(120),
            middle_name VARCHAR(120),
            last_name VARCHAR(120),
            patient_dob DATE,
            phone_number VARCHAR(80),
            address VARCHAR(512),
            postcode VARCHAR(64),
            entry_requirements JSON,
            reason_for_call VARCHAR(255),
            onset_datetime DATETIME,
            patient_alone TINYINT(1) DEFAULT 0,
            exclusion_data JSON,
            risk_flags JSON,
            decision VARCHAR(64),
            coordinates JSON,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            """,
        )

        # Ensure backward-compat columns exist if table pre-exists
        try:
            cur = conn.cursor()
            try:
                cur.execute("ALTER TABLE mdt_jobs ADD COLUMN data JSON")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_jobs ADD COLUMN updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE mdt_jobs ADD COLUMN claimedBy VARCHAR(80)")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE mdt_jobs ADD COLUMN claimedAt DATETIME")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE mdt_jobs ADD COLUMN completedAt DATETIME")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE mdt_jobs ADD COLUMN chief_complaint VARCHAR(255)")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_jobs ADD COLUMN private_notes LONGTEXT")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_jobs ADD COLUMN public_notes LONGTEXT")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_jobs ADD COLUMN final_status VARCHAR(128)")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_jobs ADD COLUMN outcome VARCHAR(255)")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_jobs ADD COLUMN lastStatusTime DATETIME")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_jobs ADD COLUMN created_by VARCHAR(120)")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_jobs ADD COLUMN division VARCHAR(64) NOT NULL DEFAULT 'general'")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_jobs ADD INDEX idx_mdt_jobs_division_status_created (division, status, created_at)")
            except Exception:
                pass
            conn.commit()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        # mdt_jobs: CAD jobs table
        create_table(
            conn,
            "mdt_jobs",
            """
            cad INT AUTO_INCREMENT PRIMARY KEY,
            status VARCHAR(32) NOT NULL DEFAULT 'queued',
            data JSON,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_by VARCHAR(120),
            division VARCHAR(64) NOT NULL DEFAULT 'general',
            claimedBy VARCHAR(80),
            claimedAt DATETIME,
            completedAt DATETIME,
            chief_complaint VARCHAR(255),
            outcome VARCHAR(255),
            lastStatusTime DATETIME,
            INDEX idx_mdt_jobs_status_created_at (status, created_at),
            INDEX idx_mdt_jobs_division_status_created (division, status, created_at)
            """,
        )

        # Ensure additional columns exist for sign-on compatibility
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "ALTER TABLE mdts_signed_on ADD COLUMN assignedIncident INT")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdts_signed_on ADD COLUMN ipAddress VARCHAR(120)")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE mdts_signed_on ADD COLUMN crew JSON")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdts_signed_on ADD COLUMN lastLat DOUBLE")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdts_signed_on ADD COLUMN lastLon DOUBLE")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdts_signed_on ADD COLUMN updatedAt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdts_signed_on ADD COLUMN division VARCHAR(64) NOT NULL DEFAULT 'general'")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdts_signed_on ADD INDEX idx_mdts_division_status (division, status)")
            except Exception:
                pass
            conn.commit()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        # mdts_signed_on: units signed on
        create_table(
            conn,
            "mdts_signed_on",
            """
            callSign VARCHAR(64) PRIMARY KEY,
            signOnTime DATETIME,
            status VARCHAR(64),
            assignedIncident INT,
            ipAddress VARCHAR(120),
            crew JSON,
            lastLat DOUBLE,
            lastLon DOUBLE,
            updatedAt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            division VARCHAR(64) NOT NULL DEFAULT 'general',
            INDEX idx_mdts_division_status (division, status)
            """,
        )

        # mdt_locations: unit locations
        create_table(
            conn,
            "mdt_locations",
            """
            id INT AUTO_INCREMENT PRIMARY KEY,
            callSign VARCHAR(64),
            latitude DOUBLE,
            longitude DOUBLE,
            timestamp DATETIME,
            status VARCHAR(64),
            INDEX idx_mdt_locations_callSign (callSign)
            """,
        )

        # mdt_dispatch_settings: dispatch assignment mode (auto/manual)
        create_table(
            conn,
            "mdt_dispatch_settings",
            """
            id TINYINT PRIMARY KEY,
            mode VARCHAR(16) NOT NULL DEFAULT 'auto',
            motd_text TEXT,
            motd_updated_by VARCHAR(120),
            motd_updated_at TIMESTAMP NULL DEFAULT NULL,
            default_division VARCHAR(64) NOT NULL DEFAULT 'general',
            updated_by VARCHAR(120),
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            """,
        )
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "ALTER TABLE mdt_dispatch_settings ADD COLUMN motd_text TEXT")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_dispatch_settings ADD COLUMN motd_updated_by VARCHAR(120)")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_dispatch_settings ADD COLUMN motd_updated_at TIMESTAMP NULL DEFAULT NULL")
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE mdt_dispatch_settings ADD COLUMN default_division VARCHAR(64) NOT NULL DEFAULT 'general'")
            except Exception:
                pass
            cur.execute("""
                INSERT INTO mdt_dispatch_settings (id, mode, default_division, updated_by)
                VALUES (1, 'auto', 'general', 'installer')
                ON DUPLICATE KEY UPDATE id = id
            """)
            conn.commit()
        finally:
            try:
                cur.close()
            except Exception:
                pass

        # mdt_job_units: many-to-one CAD-unit assignments
        create_table(
            conn,
            "mdt_job_units",
            """
            id INT AUTO_INCREMENT PRIMARY KEY,
            job_cad INT NOT NULL,
            callsign VARCHAR(64) NOT NULL,
            assigned_by VARCHAR(120),
            assigned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_job_callsign (job_cad, callsign),
            INDEX idx_job_cad (job_cad),
            INDEX idx_callsign (callsign)
            """,
        )

        # mdt_job_comms: call-taker <-> dispatcher incident communications
        create_table(
            conn,
            "mdt_job_comms",
            """
            id INT AUTO_INCREMENT PRIMARY KEY,
            cad INT NOT NULL,
            message_type VARCHAR(24) NOT NULL DEFAULT 'message',
            sender_role VARCHAR(64),
            sender_user VARCHAR(120),
            message_text LONGTEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_job_comms_cad (cad),
            INDEX idx_job_comms_created_at (created_at)
            """,
        )

        # mdt_dispatch_divisions: configured dispatch divisions and visual tags
        create_table(
            conn,
            "mdt_dispatch_divisions",
            """
            id INT AUTO_INCREMENT PRIMARY KEY,
            slug VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(120) NOT NULL,
            color VARCHAR(16) NOT NULL DEFAULT '#64748b',
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            is_default TINYINT(1) NOT NULL DEFAULT 0,
            created_by VARCHAR(120),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_divisions_active (is_active)
            """,
        )
        try:
            cur = conn.cursor()
            seeds = [
                ("general", "General", "#64748b", 1),
                ("emergency", "Emergency", "#ef4444", 0),
                ("urgent_care", "Urgent Care", "#f59e0b", 0),
                ("events", "Events", "#22c55e", 0),
            ]
            for slug, name, color, is_default in seeds:
                cur.execute(
                    """
                    INSERT INTO mdt_dispatch_divisions (slug, name, color, is_active, is_default, created_by)
                    VALUES (%s, %s, %s, 1, %s, 'installer')
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        color = VALUES(color)
                    """,
                    (slug, name, color, is_default),
                )
            conn.commit()
        finally:
            try:
                cur.close()
            except Exception:
                pass

        # mdt_dispatch_assist_requests: explicit cross-division unit request workflow
        create_table(
            conn,
            "mdt_dispatch_assist_requests",
            """
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            request_type VARCHAR(32) NOT NULL DEFAULT 'unit_assist',
            from_division VARCHAR(64) NOT NULL,
            to_division VARCHAR(64) NOT NULL,
            callsign VARCHAR(64) NOT NULL,
            cad INT NULL,
            note TEXT,
            requested_by VARCHAR(120),
            status VARCHAR(24) NOT NULL DEFAULT 'pending',
            resolved_by VARCHAR(120),
            resolved_note TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP NULL DEFAULT NULL,
            INDEX idx_assist_status_to_division (status, to_division, created_at),
            INDEX idx_assist_callsign (callsign),
            INDEX idx_assist_cad (cad)
            """,
        )

        # mdt_dispatch_user_settings: per-user dispatch access flags
        create_table(
            conn,
            "mdt_dispatch_user_settings",
            """
            username VARCHAR(120) PRIMARY KEY,
            can_override_all TINYINT(1) NOT NULL DEFAULT 0,
            updated_by VARCHAR(120),
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            """,
        )

        # mdt_dispatch_user_divisions: per-user owned divisions
        create_table(
            conn,
            "mdt_dispatch_user_divisions",
            """
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(120) NOT NULL,
            division VARCHAR(64) NOT NULL,
            created_by VARCHAR(120),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_dispatch_user_division (username, division),
            INDEX idx_dispatch_user (username),
            INDEX idx_dispatch_division (division)
            """,
        )

        # mdt_triage_forms: configurable intake forms
        create_table(
            conn,
            "mdt_triage_forms",
            """
            id INT AUTO_INCREMENT PRIMARY KEY,
            slug VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(120) NOT NULL,
            description VARCHAR(255),
            schema_json JSON NOT NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            is_default TINYINT(1) NOT NULL DEFAULT 0,
            created_by VARCHAR(120),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            """,
        )
        try:
            cur = conn.cursor()
            seeds = list(BASE_TRIAGE_FORM_SEEDS)
            if seed_demo:
                seeds.extend(DEMO_TRIAGE_FORM_SEEDS)
            for slug, name, desc, schema_json, is_active, is_default in seeds:
                cur.execute(
                    """
                    INSERT INTO mdt_triage_forms (slug, name, description, schema_json, is_active, is_default, created_by)
                    VALUES (%s, %s, %s, CAST(%s AS JSON), %s, %s, 'installer')
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        description = VALUES(description),
                        schema_json = VALUES(schema_json),
                        is_active = VALUES(is_active),
                        is_default = VALUES(is_default)
                    """,
                    (slug, name, desc, schema_json, is_active, is_default),
                )
            conn.commit()
            print(
                f"Seeded triage form profiles: {len(seeds)}"
                + (" (including demo profiles)" if seed_demo else "")
            )
        finally:
            try:
                cur.close()
            except Exception:
                pass

        # messages: messages to/from units
        create_table(
            conn,
            "messages",
            """
            id INT AUTO_INCREMENT PRIMARY KEY,
            `from` VARCHAR(120),
            recipient VARCHAR(120),
            text LONGTEXT,
            timestamp DATETIME,
            `read` TINYINT(1) DEFAULT 0,
            INDEX idx_messages_recipient (recipient)
            """,
        )

        # standby_locations: saved standby points per unit
        create_table(
            conn,
            "standby_locations",
            """
            id INT AUTO_INCREMENT PRIMARY KEY,
            callSign VARCHAR(64),
            name VARCHAR(255),
            lat DOUBLE,
            lng DOUBLE,
            isNew TINYINT(1) DEFAULT 0,
            updatedAt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_standby_callSign (callSign)
            """,
        )

        print("Ventus response module: install complete.")
    finally:
        conn.close()


def upgrade(seed_demo: bool = True):
    """Idempotent schema upgrade entrypoint."""
    print("Ventus response module: running upgrade...")
    install(seed_demo=seed_demo)
    print("Ventus response module: upgrade complete.")


def uninstall(drop_data: bool = False):
    if not drop_data:
        print('uninstall called without --drop-data; nothing to do')
        return
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            tables = [
                "standby_locations",
                "messages",
                "mdt_locations",
                "mdt_dispatch_settings",
                "mdt_dispatch_divisions",
                "mdt_dispatch_assist_requests",
                "mdt_dispatch_user_divisions",
                "mdt_dispatch_user_settings",
                "mdt_job_units",
                "mdt_job_comms",
                "mdt_triage_forms",
                "mdts_signed_on",
                "mdt_jobs",
                "response_triage",
                MIGRATIONS_TABLE,
            ]
            for t in tables:
                cur.execute(f"DROP TABLE IF EXISTS `{t}`")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
            conn.commit()
            print('Dropped ventus response module tables')
        finally:
            try:
                cur.close()
            except Exception:
                pass
    finally:
        conn.close()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Ventus Response Module installer')
    parser.add_argument('command', choices=[
                        'install', 'upgrade', 'uninstall'], help='Action')
    parser.add_argument('--drop-data', action='store_true',
                        help='Drop module tables on uninstall')
    parser.add_argument('--seed-demo', action='store_true',
                        help='Seed extra demo form profiles (install command)')
    args = parser.parse_args()

    if args.command == 'install':
        print('[INSTALL] Running install...')
        install(seed_demo=args.seed_demo)
        print('[INSTALL] Complete')
    elif args.command == 'upgrade':
        print('[UPGRADE] Running upgrade...')
        upgrade(seed_demo=True)
        print('[UPGRADE] Complete')
    elif args.command == 'uninstall':
        print('[UNINSTALL] Running uninstall...')
        uninstall(drop_data=args.drop_data)
        print('[UNINSTALL] Complete')
