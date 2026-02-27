import os
import random
import string
import uuid
import json
import math
from datetime import datetime, timedelta, date
from flask import (
    Blueprint, request, jsonify, render_template, current_app,
    redirect, url_for, flash, session
)
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.security import check_password_hash
from flask_mail import Message, Mail
# Adjust as needed
from app.objects import PluginManager, AuthManager, User, get_db_connection
from .objects import ResponseTriage, GOOGLE_MAPS_API_KEY
import logging
from app import socketio

logger = logging.getLogger('ventus_response_module')
logger.setLevel(logging.INFO)

# In-memory storage for one-time admin PINs.
# Example: {"pin": "123456", "expires_at": datetime_object, "generated_by": "ClinicalLeadUser"}
admin_pin_store = {}


def _json_compatible(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_compatible(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_compatible(v) for v in value]
    if isinstance(value, tuple):
        return [_json_compatible(v) for v in value]
    return value


def _jsonify_safe(payload, status=200):
    return jsonify(_json_compatible(payload)), status


def _sender_label_from_portal(sender_hint, username):
    hint = _role_key(sender_hint)
    user = str(username or '').strip()
    if hint in ('dispatch', 'dispatcher', 'cad_dispatch', 'controller'):
        return f"Dispatcher ({user})" if user else "Dispatcher"
    if hint in (
        'response_centre', 'response_center', 'response', 'ro', 'response_officer',
        'call_centre', 'call_center', 'call_taker', 'calltaker', 'call_handler', 'callhandler'
    ):
        return f"RO ({user})" if user else "RO"
    raw = str(sender_hint or '').strip()
    return user or raw or 'dispatcher'


def calculate_age(born, today=None):
    if today is None:
        today = datetime.utcnow().date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


# For audit logging, use our raw method.
def log_audit(user, action, patient_id=None, details=None):
    """Write a structured audit entry and add a Sentry breadcrumb when available."""
    try:
        extra = {'user': user, 'action': action}
        if patient_id is not None:
            extra['patient_id'] = patient_id
        if details is not None:
            extra['details'] = details

        # Try to use app-level audit logger
        try:
            from flask import current_app
            audit_logger = getattr(current_app, 'audit_logger', None)
            if audit_logger:
                audit_logger.info(action, extra={'extra': extra})
            else:
                logger.info('AUDIT: %s', json.dumps(extra, default=str))
        except Exception:
            logger.info('AUDIT: %s', json.dumps(extra, default=str))

        # Add Sentry breadcrumb if available
        try:
            import sentry_sdk
            sentry_sdk.add_breadcrumb(
                category='audit', message=action, data=extra, level='info')
        except Exception:
            pass
    except Exception:
        # Best-effort audit; swallow errors to avoid breaking flows
        try:
            logger.exception('Audit logging failed')
        except Exception:
            pass


def _normalize_division(value, fallback='general'):
    raw = str(value or '').strip().lower()
    if raw in ('', 'any', 'all', '*'):
        return str(fallback or '')
    safe = ''.join(ch for ch in raw if ch.isalnum() or ch in ('_', '-', '.'))
    return safe or str(fallback or '')


def _request_division_scope():
    division = _normalize_division(request.args.get('division'), fallback='')
    include_external = str(request.args.get('include_external') or '').strip().lower() in (
        '1', 'true', 'yes', 'on'
    )
    return division, include_external


def _extract_job_division(payload, fallback='general'):
    try:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode('utf-8', errors='ignore')
        if isinstance(payload, str):
            payload = json.loads(payload) if payload else {}
        if not isinstance(payload, dict):
            return _normalize_division(fallback, fallback='general')
        for key in ('division', 'dispatch_division', 'operational_division', 'service_division'):
            candidate = _normalize_division(payload.get(key), fallback='')
            if candidate:
                return candidate
    except Exception:
        pass
    return _normalize_division(fallback, fallback='general')


def _normalize_hex_color(value, fallback='#64748b'):
    s = str(value or '').strip()
    if not s:
        return fallback
    if len(s) == 4 and s.startswith('#'):
        try:
            int(s[1:], 16)
            return "#" + "".join(ch * 2 for ch in s[1:]).lower()
        except Exception:
            return fallback
    if len(s) == 7 and s.startswith('#'):
        try:
            int(s[1:], 16)
            return s.lower()
        except Exception:
            return fallback
    return fallback


def _ensure_dispatch_settings_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mdt_dispatch_settings (
            id TINYINT PRIMARY KEY,
            mode VARCHAR(16) NOT NULL DEFAULT 'auto',
            motd_text TEXT,
            motd_updated_by VARCHAR(120),
            motd_updated_at TIMESTAMP NULL DEFAULT NULL,
            default_division VARCHAR(64) NOT NULL DEFAULT 'general',
            updated_by VARCHAR(120),
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    # Best-effort compatibility for older schemas.
    try:
        cur.execute("ALTER TABLE mdt_dispatch_settings ADD COLUMN motd_text TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE mdt_dispatch_settings ADD COLUMN motd_updated_by VARCHAR(120)")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE mdt_dispatch_settings ADD COLUMN motd_updated_at TIMESTAMP NULL DEFAULT NULL")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE mdt_dispatch_settings ADD COLUMN default_division VARCHAR(64) NOT NULL DEFAULT 'general'")
    except Exception:
        pass


def _ensure_mdts_signed_on_schema(cur):
    """Repair legacy constraints that block real multi-unit dispatch behavior."""
    try:
        cur.execute("SHOW INDEX FROM mdts_signed_on")
        rows = cur.fetchall() or []
    except Exception:
        return

    bad_unique_indexes = set()
    for row in rows:
        if isinstance(row, dict):
            name = str(row.get('Key_name') or '')
        else:
            # SHOW INDEX tuple: Table, Non_unique, Key_name, ...
            name = str(row[2] if len(row) > 2 else '')
        if name in ('status_UNIQUE', 'ipAddress_UNIQUE'):
            bad_unique_indexes.add(name)

    for idx in bad_unique_indexes:
        try:
            cur.execute(f"ALTER TABLE mdts_signed_on DROP INDEX `{idx}`")
        except Exception:
            pass

    try:
        cur.execute("CREATE INDEX idx_mdts_status ON mdts_signed_on (status)")
    except Exception:
        pass
    try:
        cur.execute("CREATE INDEX idx_mdts_seen ON mdts_signed_on (lastSeenAt)")
    except Exception:
        pass


def _ensure_dispatch_divisions_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mdt_dispatch_divisions (
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    seeds = [
        ('general', 'General', '#64748b', 1),
        ('emergency', 'Emergency', '#ef4444', 0),
        ('urgent_care', 'Urgent Care', '#f59e0b', 0),
        ('events', 'Events', '#22c55e', 0)
    ]
    for slug, name, color, is_default in seeds:
        cur.execute("""
            INSERT INTO mdt_dispatch_divisions (slug, name, color, is_active, is_default, created_by)
            VALUES (%s, %s, %s, 1, %s, 'system')
            ON DUPLICATE KEY UPDATE
                name = COALESCE(name, VALUES(name)),
                color = COALESCE(color, VALUES(color))
        """, (slug, name, color, is_default))


def _ensure_assist_requests_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mdt_dispatch_assist_requests (
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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def _ensure_dispatch_user_access_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mdt_dispatch_user_settings (
            username VARCHAR(120) PRIMARY KEY,
            can_override_all TINYINT(1) NOT NULL DEFAULT 0,
            updated_by VARCHAR(120),
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mdt_dispatch_user_divisions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(120) NOT NULL,
            division VARCHAR(64) NOT NULL,
            created_by VARCHAR(120),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_dispatch_user_division (username, division),
            INDEX idx_dispatch_user (username),
            INDEX idx_dispatch_division (division)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def _ensure_standby_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS standby_locations (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            callSign VARCHAR(64) NOT NULL,
            name VARCHAR(180) NOT NULL,
            lat DECIMAL(10,7) NOT NULL,
            lng DECIMAL(10,7) NOT NULL,
            updatedBy VARCHAR(120),
            updatedAt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_standby_callsign (callSign),
            INDEX idx_standby_updated (updatedAt)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mdt_standby_presets (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(180) NOT NULL,
            lat DECIMAL(10,7) NOT NULL,
            lng DECIMAL(10,7) NOT NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            created_by VARCHAR(120),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_standby_preset_name (name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    for name, lat, lng in [
        ('HQ', 51.5074000, -0.1278000),
        ('North Standby', 51.5400000, -0.1100000),
        ('South Standby', 51.4700000, -0.1200000),
    ]:
        try:
            cur.execute("""
                INSERT INTO mdt_standby_presets (name, lat, lng, is_active, created_by)
                VALUES (%s, %s, %s, 1, 'system')
                ON DUPLICATE KEY UPDATE
                    lat = VALUES(lat),
                    lng = VALUES(lng)
            """, (name, lat, lng))
        except Exception:
            pass


def _ensure_meal_break_columns(cur):
    try:
        cur.execute("ALTER TABLE mdts_signed_on ADD COLUMN mealBreakStartedAt DATETIME NULL DEFAULT NULL")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE mdts_signed_on ADD COLUMN mealBreakUntil DATETIME NULL DEFAULT NULL")
    except Exception:
        pass


def _get_dispatch_default_division(cur):
    default_division = 'general'
    try:
        _ensure_dispatch_settings_table(cur)
        cur.execute("SELECT default_division FROM mdt_dispatch_settings WHERE id = 1 LIMIT 1")
        row = cur.fetchone()
        if isinstance(row, dict):
            default_division = _normalize_division(row.get('default_division'), fallback='general')
        elif row:
            default_division = _normalize_division(row[0], fallback='general')
    except Exception:
        pass
    return default_division or 'general'


def _set_dispatch_default_division(cur, slug, updated_by='system'):
    _ensure_dispatch_settings_table(cur)
    cur.execute("""
        INSERT INTO mdt_dispatch_settings (id, mode, default_division, updated_by)
        VALUES (1, 'auto', %s, %s)
        ON DUPLICATE KEY UPDATE
            default_division = VALUES(default_division),
            updated_by = VALUES(updated_by),
        updated_at = CURRENT_TIMESTAMP
    """, (_normalize_division(slug, fallback='general'), str(updated_by or 'system')))


def _list_dispatch_divisions(cur, include_inactive=False):
    _ensure_dispatch_divisions_table(cur)
    sql = """
        SELECT slug, name, color, is_active, is_default
        FROM mdt_dispatch_divisions
    """
    if not include_inactive:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY is_default DESC, name ASC, slug ASC"
    cur.execute(sql)
    rows = cur.fetchall() or []
    out = []
    seen = set()
    for row in rows:
        slug = _normalize_division((row or {}).get('slug'), fallback='')
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append({
            'slug': slug,
            'name': str((row or {}).get('name') or slug).strip() or slug,
            'color': _normalize_hex_color((row or {}).get('color'), '#64748b'),
            'is_active': bool((row or {}).get('is_active', 1)),
            'is_default': bool((row or {}).get('is_default', 0))
        })
    if 'general' not in seen:
        out.insert(0, {
            'slug': 'general',
            'name': 'General',
            'color': '#64748b',
            'is_active': True,
            'is_default': not any(d.get('is_default') for d in out)
        })
    return out


def _get_dispatch_user_division_access(cur, username=None):
    uname = str(username or getattr(current_user, 'username', '') or '').strip()
    uname_key = uname.lower()
    role = str(getattr(current_user, 'role', '') or '').strip().lower()
    privileged_roles = {'admin', 'superuser', 'clinical_lead'}
    if role in privileged_roles:
        return {'username': uname, 'restricted': False, 'divisions': [], 'can_override_all': True}
    if not uname:
        return {'username': uname, 'restricted': False, 'divisions': [], 'can_override_all': False}

    try:
        _ensure_dispatch_user_access_tables(cur)
    except Exception:
        return {'username': uname, 'restricted': False, 'divisions': [], 'can_override_all': False}

    can_override = False
    try:
        cur.execute("SELECT can_override_all FROM mdt_dispatch_user_settings WHERE LOWER(username) = %s LIMIT 1", (uname_key,))
        row = cur.fetchone()
        if isinstance(row, dict):
            can_override = bool(row.get('can_override_all'))
        elif row:
            can_override = bool(row[0])
    except Exception:
        can_override = False

    divisions = []
    try:
        cur.execute("""
            SELECT division
            FROM mdt_dispatch_user_divisions
            WHERE LOWER(username) = %s
            ORDER BY division ASC
        """, (uname_key,))
        rows = cur.fetchall() or []
        if rows and isinstance(rows[0], dict):
            divisions = [_normalize_division(r.get('division'), fallback='') for r in rows]
        else:
            divisions = [_normalize_division((r[0] if r else ''), fallback='') for r in rows]
        divisions = [d for d in divisions if d]
    except Exception:
        divisions = []

    restricted = len(divisions) > 0
    return {
        'username': uname,
        'restricted': restricted,
        'divisions': divisions,
        'can_override_all': can_override
    }


def _enforce_dispatch_scope(cur, selected_division, include_external):
    selected = _normalize_division(selected_division, fallback='')
    include_ext = bool(include_external)
    access = _get_dispatch_user_division_access(cur)
    if not access.get('restricted'):
        return selected, include_ext, access
    allowed = [d for d in (access.get('divisions') or []) if d]
    if not allowed:
        return 'general', False, access
    if selected not in allowed:
        selected = allowed[0]
    include_ext = bool(include_ext and access.get('can_override_all'))
    return selected, include_ext, access


# Instantiate PluginManager and load core manifest.
plugin_manager = PluginManager(os.path.abspath('app/plugins'))
core_manifest = plugin_manager.get_core_manifest()

DEFAULT_TRIAGE_FORMS = [
    {
        "slug": "urgent_care",
        "name": "Private Urgent Care",
        "description": "Primary urgent care pathway with exclusion screening.",
        "is_default": True,
        "show_exclusions": True,
        "questions": [
            {"key": "is_stable", "label": "Patient clinically stable?", "type": "select", "options": ["unknown", "yes", "no"], "required": True},
            {"key": "primary_symptom", "label": "Primary symptom", "type": "text", "required": True},
            {"key": "pain_score", "label": "Pain score (0-10)", "type": "number", "min": 0, "max": 10},
            {"key": "red_flags", "label": "Observed red flags", "type": "textarea"}
        ]
    },
    {
        "slug": "emergency_999",
        "name": "999 Emergency",
        "description": "Emergency dispatch workflow with critical incident prompts.",
        "is_default": False,
        "show_exclusions": False,
        "questions": [
            {"key": "conscious", "label": "Conscious?", "type": "select", "options": ["unknown", "yes", "no"], "required": True},
            {"key": "breathing", "label": "Breathing normally?", "type": "select", "options": ["unknown", "yes", "no"], "required": True},
            {"key": "major_bleeding", "label": "Major bleeding?", "type": "select", "options": ["unknown", "yes", "no"], "required": True},
            {"key": "immediate_danger", "label": "Immediate scene danger", "type": "textarea"}
        ]
    },
    {
        "slug": "event_medical",
        "name": "Event Medical",
        "description": "Event-specific intake with location and welfare context.",
        "is_default": False,
        "show_exclusions": False,
        "questions": [
            {"key": "event_name", "label": "Event name", "type": "text", "required": True},
            {"key": "event_zone", "label": "Event zone / stand", "type": "text"},
            {"key": "security_required", "label": "Security required?", "type": "select", "options": ["unknown", "yes", "no"]},
            {"key": "crowd_density", "label": "Crowd density", "type": "select", "options": ["low", "medium", "high", "unknown"]}
        ]
    }
]


def _default_triage_forms():
    # Deep-copy via JSON to avoid accidental mutation of global defaults.
    return json.loads(json.dumps(DEFAULT_TRIAGE_FORMS))


def _normalize_triage_form(raw):
    slug = str(raw.get("slug") or "").strip().lower().replace(" ", "_")
    if not slug:
        return None
    name = str(raw.get("name") or slug.replace("_", " ").title()).strip()
    description = str(raw.get("description") or "").strip()
    dispatch_division = _normalize_division(raw.get("dispatch_division") or raw.get("division"), fallback='general')
    show_exclusions = bool(raw.get("show_exclusions", False))
    priority_config = _normalize_priority_config(raw.get("priority_config"))
    questions = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    normalized_questions = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        key = str(q.get("key") or "").strip().lower().replace(" ", "_")
        if not key:
            continue
        q_type = str(q.get("type") or "text").strip().lower()
        if q_type not in ("text", "textarea", "number", "select"):
            q_type = "text"
        options = []
        if q_type == "select":
            options = [str(x) for x in (q.get("options") or []) if str(x).strip()]
            if not options:
                options = ["unknown", "yes", "no"]
        normalized_questions.append({
            "key": key,
            "label": str(q.get("label") or key.replace("_", " ").title()),
            "type": q_type,
            "required": bool(q.get("required", False)),
            "options": options,
            "min": q.get("min"),
            "max": q.get("max")
        })
    return {
        "slug": slug,
        "name": name,
        "description": description,
        "dispatch_division": dispatch_division,
        "is_default": bool(raw.get("is_default", False)),
        "show_exclusions": show_exclusions,
        "questions": normalized_questions,
        "priority_config": priority_config
    }


def _load_triage_forms(cur=None):
    forms = []
    if cur is not None:
        try:
            cur.execute("SHOW TABLES LIKE 'mdt_triage_forms'")
            if cur.fetchone() is not None:
                cur.execute("""
                    SELECT slug, name, description, schema_json, is_default
                    FROM mdt_triage_forms
                    WHERE is_active = 1
                    ORDER BY is_default DESC, name ASC
                """)
                rows = cur.fetchall() or []
                for row in rows:
                    schema = row.get("schema_json")
                    if isinstance(schema, (bytes, bytearray)):
                        schema = schema.decode("utf-8", errors="ignore")
                    if isinstance(schema, str):
                        try:
                            schema = json.loads(schema)
                        except Exception:
                            schema = {}
                    schema = schema if isinstance(schema, dict) else {}
                    schema["slug"] = row.get("slug")
                    schema["name"] = row.get("name")
                    schema["description"] = row.get("description")
                    schema["is_default"] = bool(row.get("is_default", False))
                    normalized = _normalize_triage_form(schema)
                    if normalized:
                        forms.append(normalized)
        except Exception:
            forms = []
    if not forms:
        forms = [_normalize_triage_form(f) for f in _default_triage_forms()]
        forms = [f for f in forms if f]
    return forms


def _pick_triage_form(forms, slug):
    wanted = str(slug or "").strip().lower()
    if not forms:
        forms = [_normalize_triage_form(f) for f in _default_triage_forms()]
    for form in forms:
        if form and form["slug"] == wanted:
            return form
    return forms[0] if forms else _normalize_triage_form(_default_triage_forms()[0])


def _default_priority_config():
    return {
        "levels": [
            {"code": "P1", "label": "P1 - Immediate"},
            {"code": "P2", "label": "P2 - Urgent"},
            {"code": "P3", "label": "P3 - Soon"},
            {"code": "P4", "label": "P4 - Routine"},
        ],
        "fallback": "P4",
        "rules": []
    }


def _normalize_priority_level_code(value):
    code = str(value or "").strip().upper().replace(" ", "_")
    code = "".join(ch for ch in code if ch.isalnum() or ch == "_")
    return code[:24] if code else ""


def _normalize_priority_config(raw):
    cfg = raw if isinstance(raw, dict) else {}
    levels_in = cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
    levels = []
    for lvl in levels_in:
        if not isinstance(lvl, dict):
            continue
        code = _normalize_priority_level_code(lvl.get("code") or lvl.get("value"))
        label = str(lvl.get("label") or code).strip()
        if not code:
            continue
        if any(x["code"] == code for x in levels):
            continue
        levels.append({"code": code, "label": label or code})
    if not levels:
        levels = _default_priority_config()["levels"]

    valid_codes = {x["code"] for x in levels}
    fallback = _normalize_priority_level_code(cfg.get("fallback"))
    if fallback not in valid_codes:
        fallback = levels[-1]["code"]

    rules = []
    raw_rules = cfg.get("rules") if isinstance(cfg.get("rules"), list) else []
    for r in raw_rules:
        if not isinstance(r, dict):
            continue
        field = str(r.get("field") or "").strip()
        op = str(r.get("op") or "equals").strip().lower()
        value = r.get("value")
        target = _normalize_priority_level_code(r.get("target") or r.get("then"))
        if not field or target not in valid_codes:
            continue
        if op not in ("equals", "not_equals", "contains", "contains_any", "in", "gte", "gt", "lte", "lt", "is_true", "is_false"):
            op = "equals"
        rules.append({
            "field": field,
            "op": op,
            "value": value,
            "target": target
        })
    return {
        "levels": levels,
        "fallback": fallback,
        "rules": rules
    }


def _priority_levels_for_form(selected_form):
    cfg = _normalize_priority_config((selected_form or {}).get("priority_config"))
    return cfg.get("levels") or _default_priority_config()["levels"]


def _priority_label_for_form(code, selected_form):
    normalized = _normalize_priority_for_form(code, selected_form)
    levels = _priority_levels_for_form(selected_form)
    for lvl in levels:
        if lvl.get("code") == normalized:
            return str(lvl.get("label") or normalized)
    return normalized


def _normalize_priority_for_form(value, selected_form):
    raw = str(value or "").strip()
    if not raw:
        return None
    needle = _normalize_priority_level_code(raw)
    levels = _priority_levels_for_form(selected_form)
    for lvl in levels:
        code = _normalize_priority_level_code(lvl.get("code"))
        label = str(lvl.get("label") or "").strip().lower()
        if needle == code or raw.lower() == label:
            return code
    return None


def _legacy_normalize_priority(value):
    raw = str(value or "").strip().lower().replace(" ", "_")
    mapping = {
        "p1": "P1",
        "critical": "P1",
        "immediate": "P1",
        "p2": "P2",
        "urgent": "P2",
        "high": "P2",
        "p3": "P3",
        "routine": "P3",
        "normal": "P3",
        "low": "P3",
        "p4": "P4",
        "non_urgent": "P4",
        "nonurgent": "P4",
    }
    return mapping.get(raw)


def _normalize_patient_alone(value):
    v = str(value or "").strip().lower()
    if v in ("yes", "y", "true", "1"):
        return 1
    if v in ("no", "n", "false", "0"):
        return 0
    return None


def _role_key(value):
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _current_role_key():
    return _role_key(getattr(current_user, "role", ""))


def _user_has_role(*roles):
    if not getattr(current_user, "is_authenticated", False):
        return False
    current = _current_role_key()
    allowed = {_role_key(r) for r in roles if str(r or "").strip()}
    return current in allowed


def _can_access_call_centre():
    return _user_has_role(
        "crew",
        "dispatcher",
        "admin",
        "superuser",
        "clinical_lead",
        "call_taker",
        "calltaker",
        "controller",
        "call_handler",
        "callhandler",
    )


def _compute_system_priority_legacy(reason_for_call, selected_form, decision, exclusion_data, form_answers):
    score = 0
    reason = str(reason_for_call or "").lower()
    slug = str((selected_form or {}).get("slug") or "").lower()
    answers = form_answers if isinstance(form_answers, dict) else {}
    exclusions = exclusion_data if isinstance(exclusion_data, dict) else {}

    critical_terms = [
        "unconscious", "cardiac", "not breathing", "arrest", "seizure",
        "stroke", "major bleed", "severe bleeding", "anaphylaxis"
    ]
    urgent_terms = [
        "chest pain", "collapse", "shortness of breath", "breathing",
        "severe pain", "head injury", "trauma", "violent", "weapon"
    ]
    if any(t in reason for t in critical_terms):
        score += 6
    if any(t in reason for t in urgent_terms):
        score += 3

    if slug in ("emergency_999",):
        score += 3
    if slug in ("security_response", "private_police"):
        score += 2

    if str(decision or "").upper() == "ACCEPT_WITH_EXCLUSION":
        score += 3
    if any(str(v or "").strip().lower() == "yes" for v in exclusions.values()):
        score += 3

    if str(answers.get("conscious") or "").lower() == "no":
        score += 5
    if str(answers.get("breathing") or "").lower() == "no":
        score += 5
    if str(answers.get("major_bleeding") or "").lower() == "yes":
        score += 5
    if str(answers.get("immediate_risk") or "").lower() == "yes":
        score += 3
    if str(answers.get("threat_level") or "").lower() in ("high", "critical"):
        score += 3
    if str(answers.get("agitation_level") or "").lower() in ("high", "critical"):
        score += 2
    if str(answers.get("casualties_reported") or "").lower() == "yes":
        score += 2

    if score >= 9:
        return "P1"
    if score >= 5:
        return "P2"
    if score >= 2:
        return "P3"
    return "P4"


def _evaluate_priority_rule_condition(field, op, value, reason_for_call, decision, exclusion_data, form_answers):
    field_key = str(field or "").strip()
    answers = form_answers if isinstance(form_answers, dict) else {}
    exclusions = exclusion_data if isinstance(exclusion_data, dict) else {}

    if field_key == "reason_for_call":
        actual = str(reason_for_call or "")
    elif field_key == "decision":
        actual = str(decision or "")
    elif field_key == "exclusion_any":
        actual = any(str(v or "").strip().lower() == "yes" for v in exclusions.values())
    elif field_key.startswith("question:"):
        actual = answers.get(field_key.split(":", 1)[1], "")
    else:
        actual = answers.get(field_key, "")

    op = str(op or "equals").strip().lower()
    if op == "is_true":
        return str(actual).strip().lower() in ("1", "true", "yes", "y")
    if op == "is_false":
        return str(actual).strip().lower() in ("0", "false", "no", "n", "")
    if op == "contains":
        return str(value or "").strip().lower() in str(actual or "").lower()
    if op == "contains_any":
        terms = [t.strip().lower() for t in str(value or "").split(",") if t.strip()]
        actual_l = str(actual or "").lower()
        return any(t in actual_l for t in terms)
    if op == "in":
        allowed = [t.strip().lower() for t in str(value or "").split(",") if t.strip()]
        return str(actual or "").strip().lower() in allowed
    if op in ("gte", "gt", "lte", "lt"):
        try:
            a = float(actual)
            b = float(value)
            if op == "gte":
                return a >= b
            if op == "gt":
                return a > b
            if op == "lte":
                return a <= b
            return a < b
        except Exception:
            return False
    if op == "not_equals":
        return str(actual or "").strip().lower() != str(value or "").strip().lower()
    return str(actual or "").strip().lower() == str(value or "").strip().lower()


def _compute_system_priority(reason_for_call, selected_form, decision, exclusion_data, form_answers):
    cfg = _normalize_priority_config((selected_form or {}).get("priority_config"))
    rules = cfg.get("rules") or []
    valid_codes = {x["code"] for x in (cfg.get("levels") or [])}
    for r in rules:
        try:
            matched = _evaluate_priority_rule_condition(
                field=r.get("field"),
                op=r.get("op"),
                value=r.get("value"),
                reason_for_call=reason_for_call,
                decision=decision,
                exclusion_data=exclusion_data,
                form_answers=form_answers
            )
        except Exception:
            matched = False
        if matched:
            target = _normalize_priority_level_code(r.get("target"))
            if target in valid_codes:
                return target

    legacy_priority = _compute_system_priority_legacy(
        reason_for_call=reason_for_call,
        selected_form=selected_form,
        decision=decision,
        exclusion_data=exclusion_data,
        form_answers=form_answers
    )
    legacy_mapped = _normalize_priority_for_form(legacy_priority, selected_form)
    if legacy_mapped:
        return legacy_mapped
    return cfg.get("fallback") or "P4"

# =============================================================================
# INTERNAL BLUEPRINT (for admin side)
# =============================================================================
internal_template_folder = os.path.join(os.path.dirname(__file__), 'templates')
internal = Blueprint(
    'medical_response_internal',
    __name__,
    url_prefix='/plugin/ventus_response_module',
    template_folder=internal_template_folder
)

# Root-level compatibility blueprint for MDT clients that still call /api/*
# directly instead of /plugin/ventus_response_module/api/*.
api_compat = Blueprint(
    'ventus_response_api_compat',
    __name__,
    url_prefix='',
    template_folder=internal_template_folder
)


@internal.route('/')
def landing():
    """Landing page (router) for Medical response Module."""
    return render_template("response_routing.html", config=core_manifest)

# --- CAD/DISPATCHER INTERNAL API ROUTES ---


@internal.route('/jobs', methods=['GET'])
@login_required
def jobs():
    """Return all jobs/incidents (queued, active, etc.) for the job queue and map."""
    selected_division, include_external = _request_division_scope()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        selected_division, include_external, _ = _enforce_dispatch_scope(cur, selected_division, include_external)
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'updated_at'")
        has_updated_at = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'claimedBy'")
        has_claimed_by = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
        has_division = cur.fetchone() is not None
        updated_at_sql = "updated_at" if has_updated_at else "NULL AS updated_at"
        claimed_by_sql = "claimedBy" if has_claimed_by else "NULL AS claimedBy"
        division_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_division else "'general' AS division"
        sql = """
            SELECT cad,
                   TRIM(COALESCE(status, '')) AS status,
                   data,
                   created_at,
                   {updated_at_sql},
                   {claimed_by_sql},
                   {division_sql}
            FROM mdt_jobs
            WHERE LOWER(TRIM(COALESCE(status, ''))) NOT IN ('cleared', 'stood_down')
        """.format(updated_at_sql=updated_at_sql, claimed_by_sql=claimed_by_sql, division_sql=division_sql)
        args = []
        if selected_division and not include_external:
            if has_division:
                sql += " AND LOWER(TRIM(COALESCE(division, 'general'))) = %s"
                args.append(selected_division)
            else:
                if selected_division != 'general':
                    return jsonify([])
        sql += " ORDER BY cad DESC"
        cur.execute(sql, tuple(args))
        jobs = cur.fetchall() or []
        # Parse triage payload and convert coordinates if present
        for job in jobs:
            reason_for_call = None
            lat = None
            lng = None
            address = None
            postcode = None
            what3words = None
            priority = None
            payload = job.get('data')
            try:
                if isinstance(payload, (bytes, bytearray)):
                    payload = payload.decode('utf-8', errors='ignore')
                if isinstance(payload, str):
                    payload = json.loads(payload) if payload else {}
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            try:
                reason_for_call = payload.get('reason_for_call')
                address = payload.get('address')
                postcode = payload.get('postcode')
                what3words = payload.get('what3words')
                priority = payload.get('call_priority') or payload.get('priority') or payload.get('acuity')
                coords = payload.get('coordinates') or {}
                if isinstance(coords, dict):
                    lat = coords.get('lat')
                    lng = coords.get('lng')
            except Exception:
                pass
            try:
                lat = float(lat) if lat is not None else None
                lng = float(lng) if lng is not None else None
            except Exception:
                lat = lng = None
            job['reason_for_call'] = reason_for_call
            job['address'] = address
            job['postcode'] = postcode
            job['what3words'] = what3words
            job['priority'] = priority
            job['lat'] = lat
            job['lng'] = lng
            job_division = _extract_job_division(payload, fallback=job.get('division') or 'general')
            job['division'] = job_division
            job['is_external'] = bool(selected_division and job_division != selected_division)
            job.pop('data', None)
        return jsonify(jobs)
    finally:
        cur.close()
        conn.close()


@internal.route('/jobs/history', methods=['GET'])
@login_required
def jobs_history():
    """Return all cleared/past jobs for the history panel."""
    selected_division, include_external = _request_division_scope()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        selected_division, include_external, _ = _enforce_dispatch_scope(cur, selected_division, include_external)
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'claimedBy'")
        has_claimed_by = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'updated_at'")
        has_updated_at = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'data'")
        has_data = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
        has_division = cur.fetchone() is not None

        claimed_by_sql = "claimedBy" if has_claimed_by else "NULL AS claimedBy"
        updated_at_sql = "updated_at" if has_updated_at else "NULL AS updated_at"
        data_sql = "data" if has_data else "NULL AS data"
        division_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_division else "'general' AS division"
        order_by_sql = "updated_at DESC" if has_updated_at else "created_at DESC"

        sql = f"""
            SELECT cad,
                   TRIM(COALESCE(status, '')) AS status,
                   {data_sql},
                   created_at,
                   {updated_at_sql},
                   {claimed_by_sql},
                   {division_sql}
            FROM mdt_jobs
            WHERE LOWER(TRIM(COALESCE(status, ''))) = 'cleared'
        """
        args = []
        if selected_division and not include_external:
            if has_division:
                sql += " AND LOWER(TRIM(COALESCE(division, 'general'))) = %s"
                args.append(selected_division)
            else:
                if selected_division != 'general':
                    return jsonify([])
        sql += f" ORDER BY {order_by_sql} LIMIT 500"
        cur.execute(sql, tuple(args))
        jobs = cur.fetchall()
        # Parse triage payload and convert coordinates if present
        for job in jobs:
            reason_for_call = None
            lat = None
            lng = None
            payload = job.get('data')
            try:
                if isinstance(payload, (bytes, bytearray)):
                    payload = payload.decode('utf-8', errors='ignore')
                if isinstance(payload, str):
                    payload = json.loads(payload) if payload else {}
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            try:
                reason_for_call = payload.get('reason_for_call')
                coords = payload.get('coordinates') or {}
                if isinstance(coords, dict):
                    lat = coords.get('lat')
                    lng = coords.get('lng')
            except Exception:
                pass
            try:
                lat = float(lat) if lat is not None else None
                lng = float(lng) if lng is not None else None
            except Exception:
                lat = lng = None
            job['reason_for_call'] = reason_for_call
            job['lat'] = lat
            job['lng'] = lng
            job_division = _extract_job_division(payload, fallback=job.get('division') or 'general')
            job['division'] = job_division
            job['is_external'] = bool(selected_division and job_division != selected_division)
            job.pop('data', None)
        return jsonify(jobs)
    except Exception:
        logger.exception("jobs_history failed")
        # Keep UI usable even when legacy schema/data is inconsistent.
        return jsonify([])
    finally:
        cur.close()
        conn.close()


@internal.route('/jobs/timings', methods=['GET'])
@login_required
def jobs_timings():
    """Return status timestamps per CAD for timing/duration display."""
    cad_args = request.args.getlist('cad')
    cads = []
    for c in cad_args:
        try:
            cads.append(int(c))
        except Exception:
            continue
    # De-dup while preserving order
    cads = list(dict.fromkeys(cads))
    if not cads:
        return jsonify([])

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SHOW TABLES LIKE 'mdt_response_log'")
        if cur.fetchone() is None:
            return jsonify([])

        placeholders = ",".join(["%s"] * len(cads))
        sql = f"""
            SELECT
                cad,
                MAX(CASE WHEN status='received'    THEN event_time END) AS received_time,
                MAX(CASE WHEN status='assigned'    THEN event_time END) AS assigned_time,
                MAX(CASE WHEN status='mobile'      THEN event_time END) AS mobile_time,
                MAX(CASE WHEN status='on_scene'    THEN event_time END) AS on_scene_time,
                MAX(CASE WHEN status='leave_scene' THEN event_time END) AS leave_scene_time,
                MAX(CASE WHEN status='at_hospital' THEN event_time END) AS at_hospital_time,
                MAX(CASE WHEN status='cleared'     THEN event_time END) AS cleared_time,
                MAX(CASE WHEN status='stood_down'  THEN event_time END) AS stood_down_time
            FROM mdt_response_log
            WHERE cad IN ({placeholders})
            GROUP BY cad
        """
        cur.execute(sql, cads)
        return jsonify(cur.fetchall())
    except Exception:
        logger.exception("jobs_timings failed")
        return jsonify([])
    finally:
        cur.close()
        conn.close()


@internal.route('/job/<int:cad>', methods=['GET'])
@login_required
def job_detail(cad):
    """Return full job/incident details for the detail panel."""
    selected_division, include_external = _request_division_scope()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        selected_division, include_external, access = _enforce_dispatch_scope(cur, selected_division, include_external)
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'private_notes'")
        has_private_notes = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'public_notes'")
        has_public_notes = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'final_status'")
        has_final_status = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'outcome'")
        has_outcome = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'updated_at'")
        has_updated_at = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'created_at'")
        has_created_at = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'claimedBy'")
        has_claimed_by = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
        has_division = cur.fetchone() is not None

        private_notes_sql = "private_notes" if has_private_notes else "NULL AS private_notes"
        public_notes_sql = "public_notes" if has_public_notes else "NULL AS public_notes"
        final_status_sql = "final_status" if has_final_status else "NULL AS final_status"
        outcome_sql = "outcome" if has_outcome else "NULL AS outcome"
        updated_at_sql = "updated_at" if has_updated_at else "NULL AS updated_at"
        created_at_sql = "created_at" if has_created_at else "NULL AS created_at"
        claimed_by_sql = "claimedBy" if has_claimed_by else "NULL AS claimedBy"
        division_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_division else "'general' AS division"

        cur.execute(f"""
            SELECT cad, status, data, {claimed_by_sql},
                   {private_notes_sql},
                   {public_notes_sql},
                   {final_status_sql},
                   {outcome_sql},
                   {updated_at_sql},
                   {created_at_sql},
                   {division_sql}
            FROM mdt_jobs
            WHERE cad = %s
        """, (cad,))
        job = cur.fetchone()
        if not job:
            return jsonify({'error': 'Not found'}), 404
        # Parse triage_data if JSON
        try:
            job['triage_data'] = json.loads(job['data']) if job['data'] else {}
        except Exception:
            job['triage_data'] = {}
        del job['data']
        job['assigned_units'] = []
        try:
            cur.execute("SHOW TABLES LIKE 'mdt_job_units'")
            if cur.fetchone() is not None:
                job['assigned_units'] = _get_job_unit_callsigns(cur, cad)
        except Exception:
            pass
        job['division'] = _normalize_division(job.get('division'), fallback='general')
        if access.get('restricted'):
            allowed = set(access.get('divisions') or [])
            if job['division'] not in allowed and not access.get('can_override_all'):
                return jsonify({'error': 'Not permitted for this division'}), 403
        if selected_division and job['division'] != selected_division and not include_external:
            return jsonify({'error': 'Job not in selected division'}), 404
        return jsonify(job)
    finally:
        cur.close()
        conn.close()


@internal.route('/units', methods=['GET'])
@login_required
def units():
    """Return all active units for the units panel and live map."""
    selected_division, include_external = _request_division_scope()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_meal_break_columns(cur)
        try:
            cur.execute("""
                UPDATE mdts_signed_on
                   SET status = 'on_standby',
                       mealBreakStartedAt = NULL,
                       mealBreakUntil = NULL
                 WHERE LOWER(TRIM(COALESCE(status, ''))) = 'meal_break'
                   AND mealBreakUntil IS NOT NULL
                   AND mealBreakUntil <= NOW()
            """)
            conn.commit()
        except Exception:
            conn.rollback()
        selected_division, include_external, _ = _enforce_dispatch_scope(cur, selected_division, include_external)
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
        has_division = cur.fetchone() is not None
        division_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_division else "'general' AS division"
        sql = """
            SELECT callSign, status, 
                   COALESCE(lastLat, NULL) AS latitude, 
                   COALESCE(lastLon, NULL) AS longitude,
                   {division_sql}
            FROM mdts_signed_on
            WHERE status IS NOT NULL
              AND COALESCE(lastSeenAt, signOnTime) >= DATE_SUB(NOW(), INTERVAL 120 MINUTE)
        """.format(division_sql=division_sql)
        args = []
        if selected_division and not include_external:
            if has_division:
                sql += " AND LOWER(TRIM(COALESCE(division, 'general'))) = %s"
                args.append(selected_division)
            else:
                if selected_division != 'general':
                    return jsonify([])
        sql += " ORDER BY callSign ASC"
        cur.execute(sql, tuple(args))
        units = cur.fetchall() or []
        # Convert lat/lon to float if present
        for unit in units:
            try:
                unit['latitude'] = float(
                    unit['latitude']) if unit['latitude'] is not None else None
                unit['longitude'] = float(
                    unit['longitude']) if unit['longitude'] is not None else None
            except Exception:
                unit['latitude'] = unit['longitude'] = None
            unit_division = _normalize_division(unit.get('division'), fallback='general')
            unit['division'] = unit_division
            unit['is_external'] = bool(selected_division and unit_division != selected_division)
        return jsonify(units)
    finally:
        cur.close()
        conn.close()


@internal.route('/job/<int:cad>/update-details', methods=['POST'])
@login_required
def update_job_details(cad):
    """Update editable incident details for an existing CAD job."""
    if not _user_has_role("crew", "dispatcher", "admin", "superuser", "clinical_lead", "call_taker", "calltaker", "controller", "call_handler", "callhandler"):
        return jsonify({'error': 'Unauthorised'}), 403

    payload = request.get_json() or {}
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT data FROM mdt_jobs WHERE cad = %s", (cad,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Job not found'}), 404

        raw = row.get('data')
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode('utf-8', errors='ignore')
            data = json.loads(raw) if isinstance(raw, str) and raw else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}

        editable_fields = [
            'reason_for_call', 'address', 'postcode', 'what3words',
            'first_name', 'middle_name', 'last_name', 'patient_dob',
            'phone_number', 'patient_gender', 'caller_name', 'caller_phone',
            'additional_details', 'onset_datetime', 'patient_alone',
            'call_priority', 'priority_source', 'division'
        ]
        changed = {}
        for key in editable_fields:
            if key in payload:
                val = payload.get(key)
                if isinstance(val, str):
                    val = val.strip()
                data[key] = val
                changed[key] = val

        # Optional patch for form-specific answers.
        answers = payload.get('form_answers')
        if isinstance(answers, dict):
            existing = data.get('form_answers')
            if not isinstance(existing, dict):
                existing = {}
            for k, v in answers.items():
                kk = str(k or '').strip()
                if not kk:
                    continue
                existing[kk] = v.strip() if isinstance(v, str) else v
            data['form_answers'] = existing
            changed['form_answers'] = existing

        sender_user_raw = str(getattr(current_user, 'username', 'unknown') or 'unknown').strip()
        sender_hint = payload.get('sender_portal') or payload.get('from') or _current_role_key()
        sender_label = _sender_label_from_portal(sender_hint, sender_user_raw)
        incident_update = str(payload.get('incident_update') or '').strip()
        assigned_units_for_push = []
        if incident_update:
            history = data.get('incident_updates')
            if not isinstance(history, list):
                history = []
            history.append({
                'time': datetime.utcnow().isoformat(),
                'by': sender_label,
                'text': incident_update
            })
            data['incident_updates'] = history
            changed['incident_update'] = incident_update
            try:
                assigned_units_for_push = _get_job_unit_callsigns(cur, cad)
            except Exception:
                assigned_units_for_push = []

        if not changed:
            return jsonify({'message': 'No changes submitted', 'cad': cad, 'updated': False})

        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'chief_complaint'")
        has_chief = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'updated_at'")
        has_updated_at = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
        has_division = cur.fetchone() is not None
        reason = data.get('reason_for_call')
        division_value = _normalize_division(data.get('division'), fallback='general') if 'division' in changed else None
        if division_value:
            data['division'] = division_value
        if has_chief:
            if has_updated_at:
                if has_division and division_value:
                    cur.execute("""
                        UPDATE mdt_jobs
                        SET data = %s, chief_complaint = %s, division = %s, updated_at = NOW()
                        WHERE cad = %s
                    """, (json.dumps(data, default=str), reason, division_value, cad))
                else:
                    cur.execute("""
                        UPDATE mdt_jobs
                        SET data = %s, chief_complaint = %s, updated_at = NOW()
                        WHERE cad = %s
                    """, (json.dumps(data, default=str), reason, cad))
            else:
                if has_division and division_value:
                    cur.execute("""
                        UPDATE mdt_jobs
                        SET data = %s, chief_complaint = %s, division = %s
                        WHERE cad = %s
                    """, (json.dumps(data, default=str), reason, division_value, cad))
                else:
                    cur.execute("""
                        UPDATE mdt_jobs
                        SET data = %s, chief_complaint = %s
                        WHERE cad = %s
                    """, (json.dumps(data, default=str), reason, cad))
        else:
            if has_updated_at:
                if has_division and division_value:
                    cur.execute("""
                        UPDATE mdt_jobs
                        SET data = %s, division = %s, updated_at = NOW()
                        WHERE cad = %s
                    """, (json.dumps(data, default=str), division_value, cad))
                else:
                    cur.execute("""
                        UPDATE mdt_jobs
                        SET data = %s, updated_at = NOW()
                        WHERE cad = %s
                    """, (json.dumps(data, default=str), cad))
            else:
                if has_division and division_value:
                    cur.execute("""
                        UPDATE mdt_jobs
                        SET data = %s, division = %s
                        WHERE cad = %s
                    """, (json.dumps(data, default=str), division_value, cad))
                else:
                    cur.execute("""
                        UPDATE mdt_jobs
                        SET data = %s
                        WHERE cad = %s
                    """, (json.dumps(data, default=str), cad))
        conn.commit()

        # Push incident updates to assigned MDT units as messages so they can see them in-device.
        if incident_update and assigned_units_for_push:
            try:
                for callsign in assigned_units_for_push:
                    cur.execute("""
                        INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                        VALUES (%s, %s, %s, NOW(), 0)
                    """, (
                        sender_label,
                        callsign,
                        f"CAD #{cad} UPDATE: {incident_update}"
                    ))
                conn.commit()
            except Exception:
                conn.rollback()

        try:
            socketio.emit('mdt_event', {'type': 'jobs_updated', 'cad': cad}, broadcast=True)
            if incident_update:
                socketio.emit('mdt_event', {
                    'type': 'job_update',
                    'cad': cad,
                    'text': incident_update,
                    'units': assigned_units_for_push
                }, broadcast=True)
        except Exception:
            pass
        try:
            log_audit(
                getattr(current_user, 'username', 'unknown'),
                'job_details_update',
                details={'cad': cad, 'fields': list(changed.keys())}
            )
        except Exception:
            pass
        return jsonify({'message': 'Job details updated', 'cad': cad, 'updated': True, 'changed': list(changed.keys())})
    finally:
        cur.close()
        conn.close()


@internal.route('/job/<int:cad>/comms', methods=['GET', 'POST'])
@login_required
def job_comms(cad):
    """Job-level communications between call-taker and dispatcher/controller."""
    if not _user_has_role("crew", "dispatcher", "admin", "superuser", "clinical_lead", "call_taker", "calltaker", "controller", "call_handler", "callhandler"):
        return jsonify({'error': 'Unauthorised'}), 403

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_job_comms_table(cur)
        cur.execute("SELECT cad FROM mdt_jobs WHERE cad = %s LIMIT 1", (cad,))
        if cur.fetchone() is None:
            return jsonify({'error': 'Job not found'}), 404

        if request.method == 'GET':
            cur.execute("""
                SELECT id, cad, message_type, sender_role, sender_user, message_text, created_at
                FROM mdt_job_comms
                WHERE cad = %s
                ORDER BY created_at ASC, id ASC
                LIMIT 800
            """, (cad,))
            return jsonify(cur.fetchall() or [])

        payload = request.get_json() or {}
        msg_text = str(payload.get('text') or '').strip()
        msg_type = str(payload.get('type') or 'message').strip().lower()
        if msg_type not in ('message', 'update'):
            msg_type = 'message'
        if not msg_text:
            return jsonify({'error': 'text is required'}), 400

        sender_role = _current_role_key()
        sender_user_raw = str(getattr(current_user, 'username', 'unknown') or 'unknown').strip()
        sender_portal = payload.get('sender_portal') or payload.get('from') or sender_role
        sender_user = _sender_label_from_portal(sender_portal, sender_user_raw)

        cur.execute("""
            INSERT INTO mdt_job_comms (cad, message_type, sender_role, sender_user, message_text)
            VALUES (%s, %s, %s, %s, %s)
        """, (cad, msg_type, sender_role, sender_user, msg_text))

        assigned_units_for_push = []
        if msg_type == 'update':
            cur.execute("SELECT data FROM mdt_jobs WHERE cad = %s", (cad,))
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return jsonify({'error': 'Job not found'}), 404

            raw = row.get('data')
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode('utf-8', errors='ignore')
                data = json.loads(raw) if isinstance(raw, str) and raw else {}
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}

            history = data.get('incident_updates')
            if not isinstance(history, list):
                history = []
            history.append({
                'time': datetime.utcnow().isoformat(),
                'by': sender_user,
                'text': msg_text
            })
            data['incident_updates'] = history
            cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'updated_at'")
            has_updated_at = cur.fetchone() is not None
            if has_updated_at:
                cur.execute("UPDATE mdt_jobs SET data = %s, updated_at = NOW() WHERE cad = %s",
                            (json.dumps(data, default=str), cad))
            else:
                cur.execute("UPDATE mdt_jobs SET data = %s WHERE cad = %s",
                            (json.dumps(data, default=str), cad))
            try:
                assigned_units_for_push = _get_job_unit_callsigns(cur, cad)
            except Exception:
                assigned_units_for_push = []

            if assigned_units_for_push:
                try:
                    cur.execute("SHOW TABLES LIKE 'messages'")
                    has_messages = cur.fetchone() is not None
                    if has_messages:
                        for callsign in assigned_units_for_push:
                            cur.execute("""
                                INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                                VALUES (%s, %s, %s, NOW(), 0)
                            """, (sender_user, callsign, f"CAD #{cad} UPDATE: {msg_text}"))
                except Exception:
                    pass

        conn.commit()

        try:
            socketio.emit('mdt_event', {'type': 'job_comm', 'cad': cad, 'message_type': msg_type, 'text': msg_text, 'by': sender_user}, broadcast=True)
            if msg_type == 'update':
                socketio.emit('mdt_event', {'type': 'jobs_updated', 'cad': cad}, broadcast=True)
                socketio.emit('mdt_event', {'type': 'job_update', 'cad': cad, 'text': msg_text, 'units': assigned_units_for_push}, broadcast=True)
        except Exception:
            pass

        return jsonify({'message': 'sent', 'cad': cad, 'type': msg_type}), 200
    finally:
        cur.close()
        conn.close()


@internal.route('/job-comms/recent', methods=['GET'])
@login_required
def recent_job_comms():
    """Recent CAD comms feed for dispatcher notifications fallback (polling)."""
    if not _user_has_role("dispatcher", "admin", "superuser", "clinical_lead", "controller"):
        return jsonify({"error": "Unauthorised access"}), 403

    limit = request.args.get('limit', default=40, type=int) or 40
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_job_comms_table(cur)
        cur.execute("""
            SELECT c.id, c.cad, c.message_type, c.sender_role, c.sender_user, c.message_text, c.created_at
            FROM mdt_job_comms c
            INNER JOIN mdt_jobs j ON j.cad = c.cad
            WHERE LOWER(TRIM(COALESCE(j.status, ''))) NOT IN ('cleared', 'stood_down')
            ORDER BY c.id DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall() or []
        rows.reverse()
        return jsonify(rows)
    finally:
        cur.close()
        conn.close()


@internal.route('/dispatch/mode', methods=['GET', 'POST'])
@login_required
def dispatch_mode():
    """Get or update dispatch mode ('auto' or 'manual')."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_dispatch_settings_table(cur)
        if request.method == 'GET':
            return jsonify({'mode': _get_dispatch_mode(cur)})

        allowed_roles = ["admin", "superuser", "clinical_lead"]
        if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
            return jsonify({'error': 'Unauthorised'}), 403

        payload = request.get_json() or {}
        mode = str(payload.get('mode') or '').strip().lower()
        if mode not in ('auto', 'manual'):
            return jsonify({'error': 'Invalid mode'}), 400

        cur.execute("""
            INSERT INTO mdt_dispatch_settings (id, mode, updated_by)
            VALUES (1, %s, %s)
            ON DUPLICATE KEY UPDATE
                mode = VALUES(mode),
                updated_by = VALUES(updated_by),
                updated_at = CURRENT_TIMESTAMP
        """, (mode, getattr(current_user, 'username', 'unknown')))
        conn.commit()
        return jsonify({'mode': mode, 'message': 'Dispatch mode updated'})
    finally:
        cur.close()
        conn.close()


@internal.route('/dispatch/motd', methods=['GET', 'POST'])
@login_required
def dispatch_motd():
    """Get or update dispatcher note / message of the day."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_dispatch_settings_table(cur)
        if request.method == 'GET':
            return jsonify(_get_dispatch_motd(cur))

        allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead"]
        if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
            return jsonify({'error': 'Unauthorised'}), 403

        payload = request.get_json() or {}
        text = str(payload.get('text') or '').strip()
        if len(text) > 4000:
            return jsonify({'error': 'Message too long (max 4000 chars)'}), 400

        cur.execute("""
            INSERT INTO mdt_dispatch_settings (id, mode, motd_text, motd_updated_by, motd_updated_at, updated_by)
            VALUES (1, 'auto', %s, %s, NOW(), %s)
            ON DUPLICATE KEY UPDATE
                motd_text = VALUES(motd_text),
                motd_updated_by = VALUES(motd_updated_by),
                motd_updated_at = NOW(),
                updated_by = VALUES(updated_by),
                updated_at = CURRENT_TIMESTAMP
        """, (
            text or None,
            getattr(current_user, 'username', 'unknown'),
            getattr(current_user, 'username', 'unknown')
        ))
        conn.commit()
        return jsonify({
            'message': 'Dispatch note updated',
            **_get_dispatch_motd(cur)
        })
    finally:
        cur.close()
        conn.close()


@internal.route('/dispatch/divisions', methods=['GET'])
@login_required
def dispatch_divisions():
    """List configured and observed operational divisions for dispatcher filtering."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        items = _list_dispatch_divisions(cur, include_inactive=False)
        _, _, access = _enforce_dispatch_scope(cur, '', False)
        out = {d['slug'] for d in items}
        try:
            cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
            has_job_div = cur.fetchone() is not None
            if has_job_div:
                cur.execute("""
                    SELECT DISTINCT LOWER(TRIM(COALESCE(division, 'general'))) AS division
                    FROM mdt_jobs
                    WHERE division IS NOT NULL AND TRIM(division) <> ''
                """)
                for row in (cur.fetchall() or []):
                    d = _normalize_division(row.get('division'), fallback='')
                    if d:
                        out.add(d)
        except Exception:
            pass
        try:
            cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
            has_unit_div = cur.fetchone() is not None
            if has_unit_div:
                cur.execute("""
                    SELECT DISTINCT LOWER(TRIM(COALESCE(division, 'general'))) AS division
                    FROM mdts_signed_on
                    WHERE division IS NOT NULL AND TRIM(division) <> ''
                """)
                for row in (cur.fetchall() or []):
                    d = _normalize_division(row.get('division'), fallback='')
                    if d:
                        out.add(d)
        except Exception:
            pass
        missing = [slug for slug in sorted(out) if slug not in {x['slug'] for x in items}]
        for slug in missing:
            items.append({
                'slug': slug,
                'name': slug.replace('_', ' ').title(),
                'color': '#64748b',
                'is_active': True,
                'is_default': False
            })
        default_division = _get_dispatch_default_division(cur)
        if default_division not in {x['slug'] for x in items}:
            default_division = 'general'
        if access.get('restricted'):
            allowed = set(access.get('divisions') or [])
            catalog_by_slug = {str(x.get('slug') or ''): x for x in _list_dispatch_divisions(cur, include_inactive=True)}
            items = [x for x in items if x.get('slug') in allowed]
            # Keep dispatcher-owned divisions visible even if not currently active/configured.
            missing_allowed = [slug for slug in sorted(allowed) if slug and slug not in {x.get('slug') for x in items}]
            for slug in missing_allowed:
                from_catalog = catalog_by_slug.get(slug)
                if from_catalog:
                    items.append({
                        'slug': slug,
                        'name': str(from_catalog.get('name') or slug).strip() or slug,
                        'color': _normalize_hex_color(from_catalog.get('color'), '#64748b'),
                        'is_active': bool(from_catalog.get('is_active', 1)),
                        'is_default': bool(from_catalog.get('is_default', 0)),
                    })
                else:
                    items.append({
                        'slug': slug,
                        'name': slug.replace('_', ' ').title(),
                        'color': '#64748b',
                        'is_active': True,
                        'is_default': False,
                    })
            if not items:
                items = [{
                    'slug': 'general',
                    'name': 'General',
                    'color': '#64748b',
                    'is_active': True,
                    'is_default': True
                }]
                allowed = {'general'}
            if default_division not in allowed:
                default_division = items[0]['slug']
        for item in items:
            item['is_default'] = (item['slug'] == default_division)
        items.sort(key=lambda x: (0 if x['slug'] == default_division else 1, x['name'].lower(), x['slug']))
        return jsonify({
            'divisions': [x['slug'] for x in items],
            'items': items,
            'default': default_division,
            'access': access,
            'can_show_external': bool(access.get('can_override_all', False) or not access.get('restricted'))
        })
    finally:
        cur.close()
        conn.close()


@internal.route('/unit/<callsign>/transfer-division', methods=['POST'])
@login_required
def transfer_unit_division(callsign):
    """Transfer a callsign into another division (cross-division support)."""
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    payload = request.get_json() or {}
    target_division = _normalize_division(payload.get('division'), fallback='')
    if not target_division:
        return jsonify({'error': 'division required'}), 400

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _, _, access = _enforce_dispatch_scope(cur, target_division, False)
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
        has_division = cur.fetchone() is not None
        if not has_division:
            return jsonify({'error': 'division column missing; run plugin upgrade'}), 409
        if access.get('restricted'):
            allowed = set(access.get('divisions') or [])
            if target_division not in allowed and not access.get('can_override_all'):
                return jsonify({'error': 'Target division not permitted'}), 403

        cur.execute("SELECT callSign, division FROM mdts_signed_on WHERE callSign = %s LIMIT 1", (callsign,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Unit not found'}), 404
        prev = _normalize_division((row or {}).get('division'), fallback='general')
        cur.execute("""
            UPDATE mdts_signed_on
            SET division = %s
            WHERE callSign = %s
        """, (target_division, callsign))
        conn.commit()
        try:
            socketio.emit('mdt_event', {
                'type': 'unit_division_transferred',
                'callsign': callsign,
                'from_division': prev,
                'to_division': target_division
            }, broadcast=True)
        except Exception:
            pass
        return jsonify({
            'message': 'Unit division updated',
            'callsign': callsign,
            'from_division': prev,
            'to_division': target_division
        })
    finally:
        cur.close()
        conn.close()


@internal.route('/unit/<callsign>/detail', methods=['GET'])
@login_required
def unit_detail(callsign):
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead", "controller", "crew", "call_taker", "call_handler"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    callsign = str(callsign or '').strip().upper()
    if not callsign:
        return jsonify({'error': 'callsign required'}), 400

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_meal_break_columns(cur)
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
        has_division = cur.fetchone() is not None
        div_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_division else "'general' AS division"
        cur.execute(f"""
            SELECT callSign, ipAddress, status, assignedIncident, signOnTime,
                   crew, lastLat, lastLon, lastSeenAt, updatedAt, mealBreakStartedAt, mealBreakUntil, {div_sql}
            FROM mdts_signed_on
            WHERE callSign = %s
            LIMIT 1
        """, (callsign,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Unit not found'}), 404

        crew = []
        try:
            raw_crew = row.get('crew')
            if isinstance(raw_crew, str):
                crew = json.loads(raw_crew) if raw_crew else []
            elif isinstance(raw_crew, list):
                crew = raw_crew
        except Exception:
            crew = []

        current_job = None
        cad = row.get('assignedIncident')
        if cad:
            cur.execute("""
                SELECT cad, status, data, created_at, updated_at
                FROM mdt_jobs
                WHERE cad = %s
                LIMIT 1
            """, (cad,))
            j = cur.fetchone()
            if j:
                reason = ''
                try:
                    payload = j.get('data')
                    if isinstance(payload, (bytes, bytearray)):
                        payload = payload.decode('utf-8', errors='ignore')
                    parsed = json.loads(payload) if isinstance(payload, str) and payload else {}
                    if isinstance(parsed, dict):
                        reason = str(parsed.get('reason_for_call') or '').strip()
                except Exception:
                    pass
                current_job = {
                    'cad': j.get('cad'),
                    'status': j.get('status'),
                    'reason_for_call': reason,
                    'created_at': j.get('created_at'),
                    'updated_at': j.get('updated_at')
                }

        cur.execute("""
            SELECT COUNT(DISTINCT cad) AS total
            FROM mdt_response_log
            WHERE callSign = %s
              AND event_time >= COALESCE(%s, DATE_SUB(NOW(), INTERVAL 7 DAY))
        """, (callsign, row.get('signOnTime')))
        total_row = cur.fetchone() or {}
        jobs_since_sign_on = int(total_row.get('total') or 0)

        cur.execute("""
            SELECT l.cad, MAX(l.event_time) AS last_event, j.status
            FROM mdt_response_log l
            LEFT JOIN mdt_jobs j ON j.cad = l.cad
            WHERE l.callSign = %s
              AND l.event_time >= COALESCE(%s, DATE_SUB(NOW(), INTERVAL 7 DAY))
            GROUP BY l.cad, j.status
            ORDER BY last_event DESC
            LIMIT 20
        """, (callsign, row.get('signOnTime')))
        recent_jobs = cur.fetchall() or []

        ping_seconds = None
        try:
            cur.execute("SELECT TIMESTAMPDIFF(SECOND, COALESCE(lastSeenAt, signOnTime), NOW()) AS ping_seconds FROM mdts_signed_on WHERE callSign=%s", (callsign,))
            p = cur.fetchone() or {}
            ping_seconds = int(p.get('ping_seconds')) if p.get('ping_seconds') is not None else None
        except Exception:
            ping_seconds = None

        return _jsonify_safe({
            'callsign': callsign,
            'status': row.get('status'),
            'division': _normalize_division(row.get('division'), fallback='general'),
            'ip_address': row.get('ipAddress'),
            'last_seen_at': row.get('lastSeenAt'),
            'last_ping_seconds': ping_seconds,
            'last_lat': float(row.get('lastLat')) if row.get('lastLat') is not None else None,
            'last_lng': float(row.get('lastLon')) if row.get('lastLon') is not None else None,
            'sign_on_time': row.get('signOnTime'),
            'updated_at': row.get('updatedAt'),
            'crew': crew,
            'meal_break_started_at': row.get('mealBreakStartedAt'),
            'meal_break_until': row.get('mealBreakUntil'),
            'current_job': current_job,
            'jobs_since_sign_on': jobs_since_sign_on,
            'recent_jobs': recent_jobs
        }, 200)
    finally:
        cur.close()
        conn.close()


@internal.route('/standby-locations', methods=['GET'])
@login_required
def standby_locations_list():
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead", "controller", "crew", "call_taker", "call_handler"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_standby_tables(cur)
        conn.commit()
        cur.execute("""
            SELECT id, name, lat, lng
            FROM mdt_standby_presets
            WHERE is_active = 1
            ORDER BY name ASC
        """)
        return _jsonify_safe(cur.fetchall() or [], 200)
    finally:
        cur.close()
        conn.close()


@internal.route('/unit/<callsign>/standby', methods=['POST'])
@login_required
def unit_set_standby(callsign):
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead", "controller"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    callsign = str(callsign or '').strip().upper()
    payload = request.get_json() or {}
    name = str(payload.get('name') or payload.get('location_name') or '').strip()
    lat = payload.get('lat', payload.get('latitude'))
    lng = payload.get('lng', payload.get('longitude'))
    if not name:
        name = 'Standby'
    try:
        lat = float(lat)
        lng = float(lng)
    except Exception:
        return jsonify({'error': 'lat/lng required'}), 400

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_standby_tables(cur)
        cur.execute("SELECT callSign FROM mdts_signed_on WHERE callSign = %s LIMIT 1", (callsign,))
        if cur.fetchone() is None:
            return jsonify({'error': 'Unit not found'}), 404
        cur.execute("""
            INSERT INTO standby_locations (callSign, name, lat, lng, updatedBy)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                lat = VALUES(lat),
                lng = VALUES(lng),
                updatedBy = VALUES(updatedBy),
                updatedAt = CURRENT_TIMESTAMP
        """, (callsign, name, lat, lng, getattr(current_user, 'username', 'unknown')))
        conn.commit()
        try:
            socketio.emit('mdt_event', {'type': 'unit_standby_updated', 'callsign': callsign, 'name': name, 'lat': lat, 'lng': lng}, broadcast=True)
            socketio.emit('mdt_event', {'type': 'units_updated', 'callsign': callsign}, broadcast=True)
        except Exception:
            pass
        return jsonify({'message': 'Standby location set', 'callsign': callsign, 'name': name, 'lat': lat, 'lng': lng}), 200
    finally:
        cur.close()
        conn.close()


@internal.route('/unit/<callsign>/meal-break', methods=['POST'])
@login_required
def unit_meal_break(callsign):
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead", "controller"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    callsign = str(callsign or '').strip().upper()
    payload = request.get_json() or {}
    action = str(payload.get('action') or 'start').strip().lower()
    minutes = int(payload.get('minutes') or 30)
    if minutes < 5:
        minutes = 5
    if minutes > 180:
        minutes = 180

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        operator = str(getattr(current_user, 'username', '') or '').strip()
        sender_name = f"Dispatcher ({operator})" if operator else "dispatcher"
        _ensure_meal_break_columns(cur)
        cur.execute("SELECT callSign FROM mdts_signed_on WHERE callSign = %s LIMIT 1", (callsign,))
        if cur.fetchone() is None:
            return jsonify({'error': 'Unit not found'}), 404

        if action == 'stop':
            cur.execute("""
                UPDATE mdts_signed_on
                   SET status = 'on_standby',
                       mealBreakStartedAt = NULL,
                       mealBreakUntil = NULL
                 WHERE callSign = %s
            """, (callsign,))
            cur.execute("""
                INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                VALUES (%s, %s, %s, NOW(), 0)
            """, (sender_name, callsign, 'Meal break ended. Return to job-ready status.'))
            msg = 'Meal break ended'
        else:
            cur.execute("""
                UPDATE mdts_signed_on
                   SET status = 'meal_break',
                       mealBreakStartedAt = NOW(),
                       mealBreakUntil = DATE_ADD(NOW(), INTERVAL %s MINUTE)
                 WHERE callSign = %s
            """, (minutes, callsign))
            cur.execute("""
                INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                VALUES (%s, %s, %s, NOW(), 0)
            """, (sender_name, callsign, f'Meal break started for {minutes} minute(s). Status set to meal_break.'))
            msg = 'Meal break started'
        conn.commit()
        try:
            socketio.emit('mdt_event', {'type': 'unit_meal_break', 'callsign': callsign, 'action': action, 'minutes': minutes}, broadcast=True)
            socketio.emit('mdt_event', {'type': 'status_update', 'callsign': callsign, 'status': ('on_standby' if action == 'stop' else 'meal_break')}, broadcast=True)
            socketio.emit('mdt_event', {'type': 'message_posted', 'from': sender_name, 'to': callsign, 'text': ('Meal break ended. Return to job-ready status.' if action == 'stop' else f'Meal break started for {minutes} minute(s). Status set to meal_break.')}, broadcast=True)
            socketio.emit('mdt_event', {'type': 'units_updated', 'callsign': callsign}, broadcast=True)
        except Exception:
            pass
        return jsonify({'message': msg, 'callsign': callsign, 'action': action, 'minutes': minutes}), 200
    finally:
        cur.close()
        conn.close()


@internal.route('/unit/<callsign>/force-signoff', methods=['POST'])
@login_required
def unit_force_signoff(callsign):
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead", "controller"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403
    callsign = str(callsign or '').strip().upper()
    if not callsign:
        return jsonify({'error': 'callsign required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM mdts_signed_on WHERE callSign = %s", (callsign,))
        if cur.rowcount == 0:
            return jsonify({'error': 'Unit not found'}), 404
        conn.commit()
        try:
            socketio.emit('mdt_event', {'type': 'unit_signoff', 'callsign': callsign}, broadcast=True)
            socketio.emit('mdt_event', {'type': 'units_updated', 'callsign': callsign}, broadcast=True)
        except Exception:
            pass
        return jsonify({'message': 'Unit force signed off', 'callsign': callsign}), 200
    finally:
        cur.close()
        conn.close()


@internal.route('/dispatch/divisions/manage', methods=['GET', 'POST'])
@login_required
def dispatch_divisions_manage():
    """Admin management for division catalog (create/update/archive/default)."""
    edit_roles = ["admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in edit_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        if request.method == 'GET':
            items = _list_dispatch_divisions(cur, include_inactive=True)
            default_division = _get_dispatch_default_division(cur)
            for item in items:
                item['is_default'] = (item['slug'] == default_division)
            items.sort(key=lambda x: (0 if x['is_default'] else 1, 0 if x['is_active'] else 1, x['name'].lower(), x['slug']))
            return jsonify({'items': items, 'default': default_division})

        payload = request.get_json() or {}
        slug = _normalize_division(payload.get('slug'), fallback='')
        if not slug:
            return jsonify({'error': 'slug required'}), 400
        name = str(payload.get('name') or '').strip() or slug.replace('_', ' ').title()
        color = _normalize_hex_color(payload.get('color'), '#64748b')
        is_active = bool(payload.get('is_active', True))
        is_default = bool(payload.get('is_default', False))
        if slug == 'general':
            name = name or 'General'
            is_active = True
        if is_default:
            is_active = True

        _ensure_dispatch_divisions_table(cur)
        if is_default:
            cur.execute("UPDATE mdt_dispatch_divisions SET is_default = 0")
        cur.execute("""
            INSERT INTO mdt_dispatch_divisions (slug, name, color, is_active, is_default, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                color = VALUES(color),
                is_active = VALUES(is_active),
                is_default = VALUES(is_default),
                updated_at = CURRENT_TIMESTAMP
        """, (
            slug, name, color, 1 if is_active else 0, 1 if is_default else 0,
            getattr(current_user, 'username', 'unknown')
        ))
        if is_default:
            _set_dispatch_default_division(cur, slug, updated_by=getattr(current_user, 'username', 'unknown'))
        conn.commit()
        return jsonify({'message': 'Division saved', 'division': {'slug': slug, 'name': name, 'color': color, 'is_active': is_active, 'is_default': is_default}})
    finally:
        cur.close()
        conn.close()


@internal.route('/dispatch/divisions/default', methods=['POST'])
@login_required
def dispatch_set_default_division():
    """Set default focused dispatch division for all dashboards."""
    edit_roles = ["admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in edit_roles:
        return jsonify({'error': 'Unauthorised'}), 403
    payload = request.get_json() or {}
    slug = _normalize_division(payload.get('slug'), fallback='')
    if not slug:
        return jsonify({'error': 'slug required'}), 400
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_dispatch_divisions_table(cur)
        cur.execute("UPDATE mdt_dispatch_divisions SET is_default = CASE WHEN slug = %s THEN 1 ELSE 0 END", (slug,))
        _set_dispatch_default_division(cur, slug, updated_by=getattr(current_user, 'username', 'unknown'))
        conn.commit()
        return jsonify({'message': 'Default division updated', 'default': slug})
    finally:
        cur.close()
        conn.close()


@internal.route('/dispatch/user-divisions', methods=['GET', 'POST'])
@login_required
def dispatch_user_divisions():
    """Admin management of dispatcher/controller division ownership."""
    edit_roles = ["admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in edit_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_dispatch_user_access_tables(cur)
        if request.method == 'GET':
            username = str(request.args.get('username') or '').strip()
            username_key = username.lower()
            if username:
                cur.execute("""
                    SELECT division
                    FROM mdt_dispatch_user_divisions
                    WHERE LOWER(username) = %s
                    ORDER BY division ASC
                """, (username_key,))
                rows = cur.fetchall() or []
                divisions = [_normalize_division(r.get('division'), fallback='') for r in rows if _normalize_division(r.get('division'), fallback='')]
                cur.execute("SELECT can_override_all FROM mdt_dispatch_user_settings WHERE LOWER(username) = %s LIMIT 1", (username_key,))
                s = cur.fetchone() or {}
                can_override_all = bool(s.get('can_override_all'))
                return jsonify({
                    'username': username,
                    'divisions': divisions,
                    'can_override_all': can_override_all
                })

            cur.execute("""
                SELECT d.username, d.division, COALESCE(s.can_override_all, 0) AS can_override_all
                FROM mdt_dispatch_user_divisions d
                LEFT JOIN mdt_dispatch_user_settings s ON s.username = d.username
                ORDER BY d.username ASC, d.division ASC
            """)
            rows = cur.fetchall() or []
            grouped = {}
            for row in rows:
                uname = str((row or {}).get('username') or '').strip()
                if not uname:
                    continue
                if uname not in grouped:
                    grouped[uname] = {'username': uname, 'divisions': [], 'can_override_all': bool((row or {}).get('can_override_all'))}
                d = _normalize_division((row or {}).get('division'), fallback='')
                if d and d not in grouped[uname]['divisions']:
                    grouped[uname]['divisions'].append(d)

            cur.execute("""
                SELECT username, can_override_all
                FROM mdt_dispatch_user_settings
                WHERE username NOT IN (SELECT DISTINCT username FROM mdt_dispatch_user_divisions)
                ORDER BY username ASC
            """)
            for row in (cur.fetchall() or []):
                uname = str((row or {}).get('username') or '').strip()
                if uname and uname not in grouped:
                    grouped[uname] = {'username': uname, 'divisions': [], 'can_override_all': bool((row or {}).get('can_override_all'))}
            return jsonify(sorted(grouped.values(), key=lambda x: x['username'].lower()))

        payload = request.get_json() or {}
        username = str(payload.get('username') or '').strip()
        username_key = username.lower()
        if not username:
            return jsonify({'error': 'username required'}), 400
        divisions = payload.get('divisions') if isinstance(payload.get('divisions'), list) else []
        divisions = sorted(list(dict.fromkeys([_normalize_division(x, fallback='') for x in divisions if _normalize_division(x, fallback='')])))
        can_override_all = bool(payload.get('can_override_all', False))

        cur.execute("""
            INSERT INTO mdt_dispatch_user_settings (username, can_override_all, updated_by)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                can_override_all = VALUES(can_override_all),
                updated_by = VALUES(updated_by),
                updated_at = CURRENT_TIMESTAMP
        """, (username_key, 1 if can_override_all else 0, getattr(current_user, 'username', 'unknown')))

        cur.execute("DELETE FROM mdt_dispatch_user_divisions WHERE LOWER(username) = %s", (username_key,))
        for d in divisions:
            cur.execute("""
                INSERT INTO mdt_dispatch_user_divisions (username, division, created_by)
                VALUES (%s, %s, %s)
            """, (username_key, d, getattr(current_user, 'username', 'unknown')))
        conn.commit()
        return jsonify({'message': 'User division access updated', 'username': username_key, 'divisions': divisions, 'can_override_all': can_override_all})
    finally:
        cur.close()
        conn.close()


@internal.route('/dispatch/my-division-access', methods=['GET'])
@login_required
def dispatch_my_division_access():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        access = _get_dispatch_user_division_access(cur)
        return jsonify(access)
    finally:
        cur.close()
        conn.close()


@internal.route('/dispatch/assist-requests', methods=['GET', 'POST'])
@login_required
def dispatch_assist_requests():
    """Create/list dispatcher cross-division assistance requests."""
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead", "controller"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_assist_requests_table(cur)
        req_division = _normalize_division(request.args.get('division'), fallback='')
        req_include_external = str(request.args.get('include_external') or '').strip().lower() in ('1', 'true', 'yes', 'on')
        req_division, req_include_external, access = _enforce_dispatch_scope(cur, req_division, req_include_external)
        if request.method == 'GET':
            status = str(request.args.get('status') or 'pending').strip().lower()
            division = req_division
            limit = request.args.get('limit', default=100, type=int) or 100
            if limit < 1:
                limit = 1
            if limit > 400:
                limit = 400

            where = []
            args = []
            if status and status != 'all':
                where.append("status = %s")
                args.append(status)
            if division:
                where.append("(from_division = %s OR to_division = %s)")
                args.extend([division, division])
            elif access.get('restricted'):
                allowed = [d for d in (access.get('divisions') or []) if d]
                if allowed:
                    placeholders = ",".join(["%s"] * len(allowed))
                    where.append(f"(from_division IN ({placeholders}) OR to_division IN ({placeholders}))")
                    args.extend(allowed + allowed)
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""
            cur.execute(f"""
                SELECT id, request_type, from_division, to_division, callsign, cad, note,
                       requested_by, status, resolved_by, resolved_note, created_at, resolved_at
                FROM mdt_dispatch_assist_requests
                {where_sql}
                ORDER BY id DESC
                LIMIT %s
            """, tuple(args + [limit]))
            rows = cur.fetchall() or []
            return jsonify(rows)

        payload = request.get_json() or {}
        callsign = str(payload.get('callsign') or '').strip()
        if not callsign:
            return jsonify({'error': 'callsign required'}), 400
        from_division = _normalize_division(payload.get('from_division'), fallback='')
        to_division = _normalize_division(payload.get('to_division'), fallback='')
        note = str(payload.get('note') or '').strip()
        cad = payload.get('cad')
        try:
            cad = int(cad) if cad not in (None, '') else None
        except Exception:
            cad = None
        if not to_division:
            return jsonify({'error': 'to_division required'}), 400
        if access.get('restricted'):
            allowed = set(access.get('divisions') or [])
            if to_division not in allowed and not access.get('can_override_all'):
                return jsonify({'error': 'to_division not permitted'}), 403
        if not from_division:
            # derive from unit record if not supplied
            cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
            has_unit_div = cur.fetchone() is not None
            if has_unit_div:
                cur.execute("SELECT LOWER(TRIM(COALESCE(division, 'general'))) AS division FROM mdts_signed_on WHERE callSign = %s LIMIT 1", (callsign,))
                row = cur.fetchone()
                from_division = _normalize_division((row or {}).get('division'), fallback='general')
            else:
                from_division = 'general'

        cur.execute("""
            INSERT INTO mdt_dispatch_assist_requests
                (request_type, from_division, to_division, callsign, cad, note, requested_by, status)
            VALUES ('unit_assist', %s, %s, %s, %s, %s, %s, 'pending')
        """, (
            from_division, to_division, callsign, cad, note or None,
            getattr(current_user, 'username', 'unknown')
        ))
        req_id = cur.lastrowid
        conn.commit()
        try:
            socketio.emit('mdt_event', {
                'type': 'assist_request_created',
                'id': req_id,
                'from_division': from_division,
                'to_division': to_division,
                'callsign': callsign,
                'cad': cad
            }, broadcast=True)
        except Exception:
            pass
        return jsonify({'message': 'Assist request created', 'id': req_id})
    finally:
        cur.close()
        conn.close()


@internal.route('/dispatch/assist-requests/<int:req_id>/resolve', methods=['POST'])
@login_required
def dispatch_assist_request_resolve(req_id):
    """Approve/reject assist requests and optionally transfer/assign units."""
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead", "controller"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    payload = request.get_json() or {}
    decision = str(payload.get('decision') or '').strip().lower()
    if decision not in ('approve', 'reject', 'cancel'):
        return jsonify({'error': 'decision must be approve/reject/cancel'}), 400
    transfer_division = bool(payload.get('transfer_division', True))
    assign_cad = payload.get('cad')
    try:
        assign_cad = int(assign_cad) if assign_cad not in (None, '') else None
    except Exception:
        assign_cad = None
    note = str(payload.get('note') or '').strip()

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_assist_requests_table(cur)
        _, _, access = _enforce_dispatch_scope(cur, '', False)
        cur.execute("""
            SELECT id, status, from_division, to_division, callsign, cad
            FROM mdt_dispatch_assist_requests
            WHERE id = %s
            LIMIT 1
        """, (req_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Request not found'}), 404
        if str(row.get('status') or '').lower() != 'pending':
            return jsonify({'error': 'Request already resolved'}), 409

        callsign = str(row.get('callsign') or '').strip()
        to_division = _normalize_division(row.get('to_division'), fallback='general')
        if access.get('restricted'):
            allowed = set(access.get('divisions') or [])
            if to_division not in allowed and not access.get('can_override_all'):
                return jsonify({'error': 'Not permitted to resolve this division request'}), 403
        cad = assign_cad if assign_cad is not None else row.get('cad')
        try:
            cad = int(cad) if cad not in (None, '') else None
        except Exception:
            cad = None

        if decision == 'approve':
            cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
            has_unit_div = cur.fetchone() is not None
            if has_unit_div and transfer_division:
                cur.execute("UPDATE mdts_signed_on SET division = %s WHERE callSign = %s", (to_division, callsign))

            if cad is not None:
                _ensure_job_units_table(cur)
                cur.execute("SELECT status FROM mdt_jobs WHERE cad = %s LIMIT 1", (cad,))
                job = cur.fetchone()
                if not job:
                    conn.rollback()
                    return jsonify({'error': 'CAD job not found'}), 404
                status = str((job or {}).get('status') or '').strip().lower()
                if status in ('cleared', 'stood_down'):
                    conn.rollback()
                    return jsonify({'error': 'Cannot assign to closed CAD'}), 409
                next_status = 'assigned' if status in ('queued', 'claimed', '', 'received', 'stood_down') else status
                cur.execute("UPDATE mdt_jobs SET status = %s WHERE cad = %s", (next_status, cad))
                cur.execute("""
                    INSERT INTO mdt_job_units (job_cad, callsign, assigned_by)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        assigned_by = VALUES(assigned_by),
                        assigned_at = CURRENT_TIMESTAMP
                """, (cad, callsign, getattr(current_user, 'username', 'unknown')))
                _sync_claimed_by_from_job_units(cur, cad)
                cur.execute("""
                    UPDATE mdts_signed_on
                    SET assignedIncident = %s, status = 'assigned'
                    WHERE callSign = %s
                """, (cad, callsign))

        new_status = 'approved' if decision == 'approve' else ('rejected' if decision == 'reject' else 'cancelled')
        cur.execute("""
            UPDATE mdt_dispatch_assist_requests
            SET status = %s,
                resolved_by = %s,
                resolved_note = %s,
                resolved_at = NOW()
            WHERE id = %s
        """, (
            new_status,
            getattr(current_user, 'username', 'unknown'),
            note or None,
            req_id
        ))
        conn.commit()
        try:
            socketio.emit('mdt_event', {
                'type': 'assist_request_resolved',
                'id': req_id,
                'decision': new_status,
                'callsign': callsign,
                'to_division': to_division,
                'cad': cad
            }, broadcast=True)
            if cad is not None:
                socketio.emit('mdt_event', {'type': 'jobs_updated', 'cad': cad}, broadcast=True)
        except Exception:
            pass
        return jsonify({'message': 'Assist request resolved', 'status': new_status, 'id': req_id})
    finally:
        cur.close()
        conn.close()


@internal.route('/jobs/eligibility', methods=['GET'])
@login_required
def jobs_eligibility():
    """Return ranked unit recommendations for each CAD job."""
    selected_division, include_external = _request_division_scope()
    cad_args = request.args.getlist('cad')
    cads = []
    for c in cad_args:
        try:
            cads.append(int(c))
        except Exception:
            continue
    cads = list(dict.fromkeys(cads))
    if not cads:
        return jsonify([])

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        selected_division, include_external, _ = _enforce_dispatch_scope(cur, selected_division, include_external)
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
        has_job_div = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
        has_unit_div = cur.fetchone() is not None
        placeholders = ",".join(["%s"] * len(cads))
        job_div_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_job_div else "'general' AS division"
        cur.execute(f"""
            SELECT cad, status, data, created_at, {job_div_sql}
            FROM mdt_jobs
            WHERE cad IN ({placeholders})
        """, cads)
        jobs = cur.fetchall() or []
        job_map = {int(j['cad']): j for j in jobs if j.get('cad') is not None}

        unit_div_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_unit_div else "'general' AS division"
        cur.execute("""
            SELECT callSign,
                   LOWER(TRIM(COALESCE(status, ''))) AS status,
                   lastLat,
                   lastLon,
                   crew,
                   {unit_div_sql}
            FROM mdts_signed_on
            WHERE status IS NOT NULL
            ORDER BY callSign ASC
        """.format(unit_div_sql=unit_div_sql))
        units = cur.fetchall() or []

        avail_states = {'on_standby', 'on_station', 'at_station', 'available', 'cleared', 'stood_down'}
        results = []

        for cad in cads:
            job = job_map.get(cad)
            if not job:
                results.append({'cad': cad, 'recommended': None, 'candidates': []})
                continue

            job_lat, job_lng, payload = _extract_coords_from_job_data(job.get('data'))
            job_division = _normalize_division(job.get('division') or _extract_job_division(payload), fallback='general')
            required_skills = _extract_required_skills(payload)

            ranked = []
            for unit in units:
                callsign = unit.get('callSign')
                if not callsign:
                    continue
                status = (unit.get('status') or '').strip().lower()
                is_available = status in avail_states
                unit_division = _normalize_division(unit.get('division'), fallback='general')
                is_external = bool(job_division and unit_division != job_division)
                if selected_division and not include_external and unit_division != selected_division:
                    continue
                unit_skills = _extract_unit_skills(unit.get('crew'))
                missing = sorted(list(required_skills - unit_skills))
                skill_match = (len(missing) == 0)

                try:
                    ulat = float(unit['lastLat']) if unit.get('lastLat') is not None else None
                    ulon = float(unit['lastLon']) if unit.get('lastLon') is not None else None
                except Exception:
                    ulat = ulon = None

                if ulat is not None and ulon is not None and job_lat is not None and job_lng is not None:
                    distance_km = _haversine_km(ulat, ulon, job_lat, job_lng)
                    distance_missing = 0
                else:
                    distance_km = None
                    distance_missing = 1

                rank = (
                    1 if is_external else 0,
                    0 if is_available else 1,
                    0 if skill_match else 1,
                    distance_missing,
                    distance_km if distance_km is not None else 10**9,
                    str(callsign)
                )
                ranked.append({
                    'rank': rank,
                    'callsign': callsign,
                    'status': status,
                    'division': unit_division,
                    'external': is_external,
                    'available': is_available,
                    'skill_match': skill_match,
                    'missing_skills': missing,
                    'distance_km': round(distance_km, 2) if distance_km is not None else None
                })

            ranked.sort(key=lambda x: x['rank'])
            top = ranked[:3]
            recommended = top[0] if top else None
            for item in top:
                item.pop('rank', None)
            if recommended:
                recommended = {k: v for k, v in recommended.items() if k != 'rank'}
            results.append({
                'cad': cad,
                'division': job_division,
                'recommended': recommended,
                'candidates': top
            })

        return jsonify(results)
    finally:
        cur.close()
        conn.close()


@internal.route('/dispatch/repair_assignments', methods=['POST'])
@login_required
def dispatch_repair_assignments():
    """Backfill/sync mdt_job_units and claimedBy for legacy incidents."""
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_job_units_table(cur)

        cur.execute("SELECT cad, claimedBy FROM mdt_jobs")
        rows = cur.fetchall() or []

        inserted_links = 0
        touched_jobs = set()

        # Backfill mappings from legacy claimedBy CSV values (one-off repair purpose only).
        for row in rows:
            cad = row.get('cad')
            claimed = str(row.get('claimedBy') or '').strip()
            if not cad or not claimed:
                continue
            callsigns = [c.strip() for c in claimed.split(',') if c.strip()]
            for cs in callsigns:
                cur.execute("""
                    INSERT INTO mdt_job_units (job_cad, callsign, assigned_by)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE callsign = callsign
                """, (cad, cs, 'repair_tool'))
                inserted_links += int(cur.rowcount == 1)
                touched_jobs.add(int(cad))

        # Sync claimedBy from mdt_job_units for all jobs with links.
        cur.execute("SELECT DISTINCT job_cad FROM mdt_job_units")
        linked = [int(r['job_cad']) for r in (cur.fetchall() or []) if r.get('job_cad') is not None]
        synced = 0
        for cad in linked:
            _sync_claimed_by_from_job_units(cur, cad)
            synced += 1

        conn.commit()
        return jsonify({
            'message': 'Assignment repair complete',
            'inserted_links': inserted_links,
            'jobs_touched': len(touched_jobs),
            'jobs_synced': synced
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@internal.route('/messages/<callsign>', methods=['GET', 'POST'])
@login_required
def messages(callsign):
    """Get or send messages to a unit/dispatcher."""
    callsign = str(callsign or '').strip().upper()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    if request.method == 'GET':
        try:
            cur.execute("""
                SELECT id, `from`, text, timestamp
                FROM messages
                WHERE LOWER(TRIM(recipient)) = LOWER(TRIM(%s))
                   OR LOWER(TRIM(`from`)) = LOWER(TRIM(%s))
                ORDER BY timestamp ASC
            """, (callsign, callsign))
            messages = cur.fetchall()
            return jsonify(messages)
        finally:
            cur.close()
            conn.close()
    else:
        data = request.get_json() or {}
        text = data.get('text', '').strip()
        sender_hint = str(data.get('sender_portal') or data.get('from') or 'dispatcher').strip()
        # Basic validation
        if not text:
            cur.close()
            conn.close()
            return jsonify({'error': 'Message text required'}), 400
        if len(text) > 2000:
            cur.close()
            conn.close()
            return jsonify({'error': 'Message too long'}), 400
        # Authorization: allow dispatch/crew/admin to send messages
        allowed_roles = ["dispatcher", "admin",
                         "superuser", "crew", "clinical_lead"]
        if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
            cur.close()
            conn.close()
            return jsonify({'error': 'Unauthorised'}), 403
        username = str(getattr(current_user, 'username', '') or '').strip()
        sender = _sender_label_from_portal(sender_hint, username)
        try:
            cur.execute("""
                INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                VALUES (%s, %s, %s, NOW(), 0)
            """, (sender, callsign, text))
            conn.commit()
            try:
                socketio.emit('mdt_event', {
                    'type': 'message_posted',
                    'from': str(sender or 'dispatcher'),
                    'to': callsign,
                    'text': text
                }, broadcast=True)
            except Exception:
                pass
            try:
                logger.info('Message posted: from=%s to=%s by=%s len=%s', sender, callsign, getattr(
                    current_user, 'username', 'unknown'), len(text))
            except Exception:
                pass
            try:
                log_audit(getattr(current_user, 'username', 'unknown'),
                          'post_message', details={'to': callsign, 'from': sender, 'len': len(text)})
            except Exception:
                pass
            return jsonify({'message': 'Message sent'})
        finally:
            cur.close()
            conn.close()


@internal.route('/kpis', methods=['GET'])
@login_required
def kpis():
    """Return analytics: active jobs, cleared today, avg response time, units available."""
    selected_division, include_external = _request_division_scope()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        selected_division, include_external, _ = _enforce_dispatch_scope(cur, selected_division, include_external)
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
        has_job_div = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
        has_unit_div = cur.fetchone() is not None

        job_div_where = ""
        unit_div_where = ""
        job_div_args = []
        unit_div_args = []
        if selected_division and not include_external:
            if has_job_div:
                job_div_where = " AND LOWER(TRIM(COALESCE(division, 'general'))) = %s"
                job_div_args.append(selected_division)
            elif selected_division != 'general':
                return jsonify({
                    "active_jobs": 0,
                    "units_available": 0,
                    "cleared_today": 0,
                    "avg_response_time": "--",
                    "stage_averages": {
                        "wait_to_assigned": "--",
                        "mobile_to_scene": "--",
                        "on_scene": "--",
                        "leave_to_hospital": "--",
                        "at_hospital": "--"
                    }
                })
            if has_unit_div:
                unit_div_where = " AND LOWER(TRIM(COALESCE(division, 'general'))) = %s"
                unit_div_args.append(selected_division)
            elif selected_division != 'general':
                return jsonify({
                    "active_jobs": 0,
                    "units_available": 0,
                    "cleared_today": 0,
                    "avg_response_time": "--",
                    "stage_averages": {
                        "wait_to_assigned": "--",
                        "mobile_to_scene": "--",
                        "on_scene": "--",
                        "leave_to_hospital": "--",
                        "at_hospital": "--"
                    }
                })

        # Active jobs
        cur.execute(
            "SELECT COUNT(*) AS count FROM mdt_jobs WHERE LOWER(TRIM(COALESCE(status, ''))) IN ('queued','claimed','assigned','mobile','on_scene')" + job_div_where,
            tuple(job_div_args)
        )
        active_jobs = cur.fetchone()['count']

        # Units available
        cur.execute(
            "SELECT COUNT(*) AS count FROM mdts_signed_on WHERE LOWER(TRIM(COALESCE(status, ''))) IN ('on_standby','on_station','at_station','available')" + unit_div_where,
            tuple(unit_div_args)
        )
        units_available = cur.fetchone()['count']

        # Cleared today
        cur.execute(
            "SELECT COUNT(*) AS count FROM mdt_jobs WHERE LOWER(TRIM(COALESCE(status, ''))) = 'cleared' AND DATE(updated_at) = CURDATE()" + job_div_where,
            tuple(job_div_args)
        )
        cleared_today = cur.fetchone()['count']

        avg_response_time = "--"

        def _parse_dt(value):
            if not value:
                return None
            if isinstance(value, datetime):
                return value
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace('Z', '+00:00'))
                except Exception:
                    try:
                        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        return None
            return None

        def _duration_seconds(start, end):
            s = _parse_dt(start)
            e = _parse_dt(end)
            if not s or not e:
                return None
            sec = int((e - s).total_seconds())
            return sec if sec >= 0 else None

        def _avg_duration(values):
            clean = [v for v in values if isinstance(v, int) and v >= 0]
            if not clean:
                return None
            return int(round(sum(clean) / len(clean)))

        def _fmt_duration(seconds):
            if seconds is None:
                return "--"
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            if hours > 0:
                return f"{hours}h {minutes}m"
            return f"{minutes}m"

        stage_averages = {
            "wait_to_assigned": "--",
            "mobile_to_scene": "--",
            "on_scene": "--",
            "leave_to_hospital": "--",
            "at_hospital": "--"
        }

        # Today's status-stage averages across CADs with response-log activity today.
        cur.execute("SHOW TABLES LIKE 'mdt_response_log'")
        has_response_log = cur.fetchone() is not None
        if has_response_log:
            if selected_division and not include_external and has_job_div:
                cur.execute("""
                    SELECT
                        l.cad,
                        MAX(CASE WHEN l.status='received'    THEN l.event_time END) AS received_time,
                        MAX(CASE WHEN l.status='assigned'    THEN l.event_time END) AS assigned_time,
                        MAX(CASE WHEN l.status='mobile'      THEN l.event_time END) AS mobile_time,
                        MAX(CASE WHEN l.status='on_scene'    THEN l.event_time END) AS on_scene_time,
                        MAX(CASE WHEN l.status='leave_scene' THEN l.event_time END) AS leave_scene_time,
                        MAX(CASE WHEN l.status='at_hospital' THEN l.event_time END) AS at_hospital_time,
                        MAX(CASE WHEN l.status='cleared'     THEN l.event_time END) AS cleared_time,
                        MAX(CASE WHEN l.status='stood_down'  THEN l.event_time END) AS stood_down_time
                    FROM mdt_response_log l
                    INNER JOIN mdt_jobs j ON j.cad = l.cad
                    INNER JOIN (
                        SELECT DISTINCT cad
                        FROM mdt_response_log
                        WHERE DATE(event_time) = CURDATE()
                    ) t ON t.cad = l.cad
                    WHERE LOWER(TRIM(COALESCE(j.division, 'general'))) = %s
                    GROUP BY l.cad
                """, (selected_division,))
            else:
                cur.execute("""
                SELECT
                    l.cad,
                    MAX(CASE WHEN l.status='received'    THEN l.event_time END) AS received_time,
                    MAX(CASE WHEN l.status='assigned'    THEN l.event_time END) AS assigned_time,
                    MAX(CASE WHEN l.status='mobile'      THEN l.event_time END) AS mobile_time,
                    MAX(CASE WHEN l.status='on_scene'    THEN l.event_time END) AS on_scene_time,
                    MAX(CASE WHEN l.status='leave_scene' THEN l.event_time END) AS leave_scene_time,
                    MAX(CASE WHEN l.status='at_hospital' THEN l.event_time END) AS at_hospital_time,
                    MAX(CASE WHEN l.status='cleared'     THEN l.event_time END) AS cleared_time,
                    MAX(CASE WHEN l.status='stood_down'  THEN l.event_time END) AS stood_down_time
                FROM mdt_response_log l
                INNER JOIN (
                    SELECT DISTINCT cad
                    FROM mdt_response_log
                    WHERE DATE(event_time) = CURDATE()
                ) t ON t.cad = l.cad
                GROUP BY l.cad
                """)
            timing_rows = cur.fetchall() or []

            wait_to_assigned = []
            mobile_to_scene = []
            on_scene = []
            leave_to_hospital = []
            at_hospital = []
            cycle_complete = []

            for row in timing_rows:
                wait_sec = _duration_seconds(
                    row.get('received_time'),
                    row.get('assigned_time') or row.get('mobile_time')
                )
                if wait_sec is not None:
                    wait_to_assigned.append(wait_sec)

                mobile_scene_sec = _duration_seconds(
                    row.get('mobile_time'),
                    row.get('on_scene_time')
                )
                if mobile_scene_sec is not None:
                    mobile_to_scene.append(mobile_scene_sec)

                on_scene_sec = _duration_seconds(
                    row.get('on_scene_time'),
                    row.get('leave_scene_time')
                )
                if on_scene_sec is not None:
                    on_scene.append(on_scene_sec)

                leave_hosp_sec = _duration_seconds(
                    row.get('leave_scene_time'),
                    row.get('at_hospital_time')
                )
                if leave_hosp_sec is not None:
                    leave_to_hospital.append(leave_hosp_sec)

                at_hosp_sec = _duration_seconds(
                    row.get('at_hospital_time'),
                    row.get('cleared_time') or row.get('stood_down_time')
                )
                if at_hosp_sec is not None:
                    at_hospital.append(at_hosp_sec)

                cycle_sec = _duration_seconds(
                    row.get('received_time'),
                    row.get('cleared_time') or row.get('stood_down_time')
                )
                if cycle_sec is not None:
                    cycle_complete.append(cycle_sec)

            stage_averages = {
                "wait_to_assigned": _fmt_duration(_avg_duration(wait_to_assigned)),
                "mobile_to_scene": _fmt_duration(_avg_duration(mobile_to_scene)),
                "on_scene": _fmt_duration(_avg_duration(on_scene)),
                "leave_to_hospital": _fmt_duration(_avg_duration(leave_to_hospital)),
                "at_hospital": _fmt_duration(_avg_duration(at_hospital))
            }
            avg_response_time = _fmt_duration(_avg_duration(cycle_complete))

        # Fallback when response log has no complete cycles today.
        if avg_response_time == "--":
            cur.execute("""
                SELECT AVG(TIMESTAMPDIFF(SECOND, created_at, updated_at)) AS avg_seconds
                FROM mdt_jobs
                WHERE LOWER(TRIM(COALESCE(status, ''))) = 'cleared'
                  AND DATE(created_at) = CURDATE()
                  AND DATE(updated_at) = CURDATE()
            """ + job_div_where, tuple(job_div_args))
            row = cur.fetchone() or {}
            avg_response_time = _fmt_duration(
                _avg_duration([int(row.get('avg_seconds'))]) if row.get('avg_seconds') is not None else None
            )

        return jsonify({
            "active_jobs": active_jobs,
            "units_available": units_available,
            "cleared_today": cleared_today,
            "avg_response_time": avg_response_time,
            "stage_averages": stage_averages
        })
    finally:
        cur.close()
        conn.close()


@internal.route('/history', methods=['GET'])
@login_required
def history():
    """Return all completed/cleared jobs/incidents for the history table."""
    selected_division, include_external = _request_division_scope()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        selected_division, include_external, _ = _enforce_dispatch_scope(cur, selected_division, include_external)
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'claimedBy'")
        has_claimed_by = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'updated_at'")
        has_updated_at = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'data'")
        has_data = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
        has_division = cur.fetchone() is not None

        claimed_by_sql = "claimedBy" if has_claimed_by else "NULL AS claimedBy"
        completed_sql = "updated_at AS completedAt" if has_updated_at else "created_at AS completedAt"
        data_sql = "data" if has_data else "NULL AS data"
        division_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_division else "'general' AS division"
        order_by_sql = "updated_at DESC" if has_updated_at else "created_at DESC"

        sql = f"""
            SELECT cad, {completed_sql}, TRIM(COALESCE(status, '')) AS status, {claimed_by_sql},
                   {data_sql}, {division_sql}
            FROM mdt_jobs
            WHERE LOWER(TRIM(COALESCE(status, ''))) = 'cleared'
        """
        args = []
        if selected_division and not include_external:
            if has_division:
                sql += " AND LOWER(TRIM(COALESCE(division, 'general'))) = %s"
                args.append(selected_division)
            elif selected_division != 'general':
                return jsonify([])
        sql += f" ORDER BY {order_by_sql} LIMIT 100"
        cur.execute(sql, tuple(args))
        jobs = cur.fetchall()
        for job in jobs:
            payload = job.get('data')
            try:
                if isinstance(payload, (bytes, bytearray)):
                    payload = payload.decode('utf-8', errors='ignore')
                if isinstance(payload, str):
                    payload = json.loads(payload) if payload else {}
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            job['chief_complaint'] = payload.get('chief_complaint')
            job['outcome'] = payload.get('outcome')
            job['division'] = _extract_job_division(payload, fallback=job.get('division') or 'general')
            job['is_external'] = bool(selected_division and job.get('division') != selected_division)
            job.pop('data', None)
        return jsonify(jobs)
    except Exception:
        logger.exception("history failed")
        return jsonify([])
    finally:
        cur.close()
        conn.close()


@internal.route('/job/<int:cad>/assign', methods=['POST'])
@login_required
def assign_job(cad):
    """Assign a job to one or many units (callsign(s) in POST data)."""
    data = request.get_json() or {}
    callsigns = data.get('callsigns')
    if not isinstance(callsigns, list):
        single = data.get('callsign')
        callsigns = [single] if single else []
    callsigns = [str(c).strip() for c in callsigns if str(c).strip()]
    callsigns = list(dict.fromkeys(callsigns))
    transfer_division = str(data.get('transfer_division', '1')).strip().lower() in ('1', 'true', 'yes', 'on')
    selected_division = _normalize_division(data.get('division'), fallback='')
    if not callsigns:
        return jsonify({'error': 'callsign(s) required'}), 400
    # Authorization: only dispatchers/admins/clinical lead may assign jobs
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        selected_division, _, access = _enforce_dispatch_scope(cur, selected_division, False)
        # Ensure assignment mapping table exists for multi-unit incidents.
        _ensure_job_units_table(cur)
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
        has_job_div = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
        has_unit_div = cur.fetchone() is not None

        job_div_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_job_div else "'general' AS division"
        cur.execute(
            f"SELECT status, {job_div_sql} FROM mdt_jobs WHERE cad = %s LIMIT 1", (cad,))
        job = cur.fetchone()
        if not job:
            conn.rollback()
            return jsonify({'error': 'Job not found'}), 404

        status = str(job.get('status') or '').strip().lower()
        job_division = _normalize_division(job.get('division') or selected_division, fallback='general')
        if access.get('restricted'):
            allowed = set(access.get('divisions') or [])
            if job_division not in allowed and not access.get('can_override_all'):
                conn.rollback()
                return jsonify({'error': 'Not permitted for this division'}), 403
        if status == 'cleared':
            conn.rollback()
            return jsonify({'error': 'Cannot assign closed job'}), 409

        existing_units = _get_job_unit_callsigns(cur, cad)
        merged_units = list(dict.fromkeys(existing_units + callsigns))
        # Re-assignment must reopen previously stood-down jobs as active.
        next_status = 'assigned' if status in ('queued', 'claimed', '', 'received', 'stood_down') else status

        cur.execute("""
            UPDATE mdt_jobs SET status = %s WHERE cad = %s
        """, (next_status, cad))

        assigned = []
        missing = []
        reassigned_from = set()
        sender_name = _sender_label_from_portal('dispatch', getattr(current_user, 'username', ''))
        cur.execute("SHOW TABLES LIKE 'messages'")
        has_messages = cur.fetchone() is not None
        for cs in callsigns:
            unit_div_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_unit_div else "'general' AS division"
            cur.execute(
                f"SELECT callSign, crew, {unit_div_sql} FROM mdts_signed_on WHERE callSign = %s LIMIT 1", (cs,))
            unit = cur.fetchone()
            if not unit:
                missing.append(cs)
                continue
            cur.execute("SELECT assignedIncident, crew FROM mdts_signed_on WHERE callSign = %s LIMIT 1", (cs,))
            unit_state = cur.fetchone() or {}
            old_cad = unit_state.get('assignedIncident')
            crew_json = unit_state.get('crew') or '[]'
            if old_cad is not None:
                try:
                    old_cad = int(old_cad)
                except Exception:
                    old_cad = None
            # Reassign flow: stand down unit from previous CAD before assigning this CAD.
            if old_cad and old_cad != cad:
                reassigned_from.add(old_cad)
                cur.execute("DELETE FROM mdt_job_units WHERE job_cad = %s AND callsign = %s", (old_cad, cs))
                _sync_claimed_by_from_job_units(cur, old_cad)
                cur.execute("SELECT status FROM mdt_jobs WHERE cad = %s LIMIT 1", (old_cad,))
                old_row = cur.fetchone() or {}
                old_status = str(old_row.get('status') or '').strip().lower()
                remaining_old_units = _get_job_unit_callsigns(cur, old_cad)
                # Superseded incident: once no units remain, return it to dispatch queue
                # (unallocated) unless it has been explicitly cleared.
                if len(remaining_old_units) == 0 and old_status != 'cleared':
                    cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'updated_at'")
                    has_updated_at = cur.fetchone() is not None
                    if has_updated_at:
                        cur.execute(
                            "UPDATE mdt_jobs SET status = 'queued', updated_at = NOW() WHERE cad = %s AND LOWER(TRIM(COALESCE(status, ''))) <> 'cleared'",
                            (old_cad,)
                        )
                    else:
                        cur.execute(
                            "UPDATE mdt_jobs SET status = 'queued' WHERE cad = %s AND LOWER(TRIM(COALESCE(status, ''))) <> 'cleared'",
                            (old_cad,)
                        )
                try:
                    cur.execute("""
                        INSERT INTO mdt_response_log (callSign, cad, status, event_time, crew)
                        VALUES (%s, %s, %s, NOW(), %s)
                    """, (cs, old_cad, 'stood_down', crew_json))
                except Exception:
                    pass
                if has_messages:
                    cur.execute("""
                        INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                        VALUES (%s, %s, %s, NOW(), 0)
                    """, (sender_name, cs, f"Stand down from CAD #{old_cad}. Reassigned to CAD #{cad}."))
            unit_division = _normalize_division(unit.get('division'), fallback='general')
            if access.get('restricted') and unit_division not in set(access.get('divisions') or []) and not access.get('can_override_all'):
                missing.append(cs)
                continue
            should_transfer = bool(has_unit_div and transfer_division and unit_division != job_division)
            if should_transfer and access.get('restricted') and not access.get('can_override_all'):
                # restricted dispatchers can only transfer units into their own allowed divisions
                if job_division not in set(access.get('divisions') or []):
                    should_transfer = False
            cur.execute("""
                UPDATE mdts_signed_on
                   SET assignedIncident = %s, status = 'assigned'{division_set}
                 WHERE callSign = %s
            """.format(division_set=", division = %s" if should_transfer else ""), tuple(
                [cad] + ([job_division] if should_transfer else []) + [cs]
            ))
            cur.execute("""
                INSERT INTO mdt_job_units (job_cad, callsign, assigned_by)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    assigned_by = VALUES(assigned_by),
                    assigned_at = CURRENT_TIMESTAMP
            """, (cad, cs, getattr(current_user, 'username', 'unknown')))
            try:
                cur.execute("""
                    INSERT INTO mdt_response_log (callSign, cad, status, event_time, crew)
                    VALUES (%s, %s, %s, NOW(), %s)
                """, (cs, cad, 'assigned', unit.get('crew') or '[]'))
            except Exception:
                pass
            assigned.append(cs)

        if not assigned:
            conn.rollback()
            return jsonify({'error': 'No valid units selected', 'missing': missing}), 409

        _sync_claimed_by_from_job_units(cur, cad)

        conn.commit()
        # Notify connected realtime clients to refresh job lists/maps
        try:
            socketio.emit(
                'mdt_event', {'type': 'jobs_updated', 'cad': cad}, broadcast=True)
            for old_cad in sorted(reassigned_from):
                socketio.emit('mdt_event', {'type': 'jobs_updated', 'cad': old_cad}, broadcast=True)
            for cs in assigned:
                socketio.emit('mdt_event', {'type': 'units_updated', 'callsign': cs}, broadcast=True)
        except Exception:
            pass
        try:
            logger.info('Job assigned: cad=%s by=%s to=%s missing=%s', cad, getattr(
                current_user, 'username', 'unknown'), ",".join(assigned), ",".join(missing))
            try:
                log_audit(getattr(current_user, 'username', 'unknown'),
                          'assign_job', details={'cad': cad, 'to': assigned, 'missing': missing})
            except Exception:
                pass
        except Exception:
            pass
        return jsonify({'message': 'Job assigned', 'cad': cad, 'assigned': assigned, 'missing': missing, 'division': job_division})
    finally:
        cur.close()
        conn.close()


@internal.route('/job/<int:cad>/unassign', methods=['POST'])
@login_required
def unassign_job_unit(cad):
    data = request.get_json() or {}
    callsigns = data.get('callsigns')
    if not isinstance(callsigns, list):
        single = data.get('callsign')
        callsigns = [single] if single else []
    callsigns = [str(c).strip() for c in callsigns if str(c).strip()]
    callsigns = list(dict.fromkeys(callsigns))
    if not callsigns:
        return jsonify({'error': 'callsign(s) required'}), 400

    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead", "controller"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_job_units_table(cur)
        sender_name = _sender_label_from_portal('dispatch', getattr(current_user, 'username', ''))
        cur.execute("SHOW TABLES LIKE 'messages'")
        has_messages = cur.fetchone() is not None
        released = []
        for cs in callsigns:
            cur.execute("SELECT crew FROM mdts_signed_on WHERE callSign = %s LIMIT 1", (cs,))
            row = cur.fetchone() or {}
            crew_json = row.get('crew') or '[]'
            cur.execute("""
                UPDATE mdts_signed_on
                   SET assignedIncident = NULL, status = 'on_standby'
                 WHERE callSign = %s AND assignedIncident = %s
            """, (cs, cad))
            cur.execute("DELETE FROM mdt_job_units WHERE job_cad = %s AND callsign = %s", (cad, cs))
            if cur.rowcount >= 0:
                released.append(cs)
            try:
                cur.execute("""
                    INSERT INTO mdt_response_log (callSign, cad, status, event_time, crew)
                    VALUES (%s, %s, %s, NOW(), %s)
                """, (cs, cad, 'stood_down', crew_json))
            except Exception:
                pass
            if has_messages:
                cur.execute("""
                    INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                    VALUES (%s, %s, %s, NOW(), 0)
                """, (sender_name, cs, f"Stood down from CAD #{cad}. Await further assignment."))

        remaining = _sync_claimed_by_from_job_units(cur, cad)
        cur.execute("SELECT status FROM mdt_jobs WHERE cad = %s LIMIT 1", (cad,))
        job = cur.fetchone() or {}
        st = str(job.get('status') or '').strip().lower()
        if st in ('assigned', 'claimed') and len(remaining) == 0:
            cur.execute("UPDATE mdt_jobs SET status = 'queued' WHERE cad = %s AND status IN ('assigned','claimed')", (cad,))

        conn.commit()
        try:
            socketio.emit('mdt_event', {'type': 'jobs_updated', 'cad': cad}, broadcast=True)
            for cs in released:
                socketio.emit('mdt_event', {'type': 'units_updated', 'callsign': cs}, broadcast=True)
        except Exception:
            pass
        return jsonify({'message': 'Units stood down', 'cad': cad, 'released': released, 'remaining': remaining}), 200
    finally:
        cur.close()
        conn.close()


@internal.route('/job/<int:cad>/standdown', methods=['POST'])
@login_required
def standdown_job(cad):
    data = request.get_json() or {}
    reason = str(data.get('reason') or data.get('outcome') or 'stood_down').strip()
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead", "controller", "call_taker", "calltaker", "call_handler", "callhandler"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({'error': 'Unauthorised'}), 403

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_job_units_table(cur)
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'updated_at'")
        has_updated_at = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'final_status'")
        has_final_status = cur.fetchone() is not None
        if has_final_status and has_updated_at:
            cur.execute("UPDATE mdt_jobs SET status='stood_down', final_status=%s, updated_at=NOW() WHERE cad=%s", (reason, cad))
        elif has_final_status:
            cur.execute("UPDATE mdt_jobs SET status='stood_down', final_status=%s WHERE cad=%s", (reason, cad))
        elif has_updated_at:
            cur.execute("UPDATE mdt_jobs SET status='stood_down', updated_at=NOW() WHERE cad=%s", (cad,))
        else:
            cur.execute("UPDATE mdt_jobs SET status='stood_down' WHERE cad=%s", (cad,))
        if cur.rowcount == 0:
            conn.rollback()
            return jsonify({'error': 'Job not found'}), 404

        sender_name = _sender_label_from_portal('dispatch', getattr(current_user, 'username', ''))
        cur.execute("SHOW TABLES LIKE 'messages'")
        has_messages = cur.fetchone() is not None
        callsigns = _collect_job_callsigns(cur, cad)
        released = []
        for cs in callsigns:
            cur.execute("SELECT crew FROM mdts_signed_on WHERE callSign = %s LIMIT 1", (cs,))
            row = cur.fetchone() or {}
            crew_json = row.get('crew') or '[]'
            cur.execute("""
                UPDATE mdts_signed_on
                   SET assignedIncident = NULL, status = 'on_standby'
                 WHERE callSign = %s AND assignedIncident = %s
            """, (cs, cad))
            released.append(cs)
            try:
                cur.execute("""
                    INSERT INTO mdt_response_log (callSign, cad, status, event_time, crew)
                    VALUES (%s, %s, %s, NOW(), %s)
                """, (cs, cad, 'stood_down', crew_json))
            except Exception:
                pass
            if has_messages:
                cur.execute("""
                    INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                    VALUES (%s, %s, %s, NOW(), 0)
                """, (sender_name, cs, f"CAD #{cad} stood down. Reason: {reason}"))

        cur.execute("DELETE FROM mdt_job_units WHERE job_cad = %s", (cad,))
        _sync_claimed_by_from_job_units(cur, cad)
        conn.commit()
        try:
            socketio.emit('mdt_event', {'type': 'jobs_updated', 'cad': cad}, broadcast=True)
            for cs in released:
                socketio.emit('mdt_event', {'type': 'units_updated', 'callsign': cs}, broadcast=True)
        except Exception:
            pass
        return jsonify({'message': 'Job stood down', 'cad': cad, 'reason': reason, 'released': released}), 200
    finally:
        cur.close()
        conn.close()


@internal.route('/cad', methods=['GET'])
@login_required
def cad_dashboard():
    allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({"error": "Unauthorised access"}), 403
    return render_template("cad/dashboard.html", config=core_manifest)


@internal.route('/job/<int:cad>/close', methods=['POST'])
@login_required
def close_job(cad):
    """Close/clear a job/incident with notes and outcome. Preserves MDT status unless overridden."""
    data = request.get_json() or {}
    private_notes = data.get('private_notes', '').strip()
    public_notes = data.get('public_notes', '').strip()
    # outcome: completed, cancelled, transferred, etc.
    outcome = data.get('outcome', 'completed').strip()
    # Only set if dispatcher is forcing due to issue
    force_status = data.get('force_status', None)

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        # Authorization: only dispatchers/admins/clinical lead may close jobs
        allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead", "controller", "call_taker", "calltaker", "call_handler", "callhandler"]
        if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
            return jsonify({'error': 'Unauthorised'}), 403

        # If force_status provided, dispatcher is overriding due to comms issue, etc.
        if force_status:
            cur.execute("""
                UPDATE mdt_jobs
                   SET status = %s,
                       final_status = %s,
                       private_notes = %s,
                       public_notes = %s,
                       updated_at = NOW()
                 WHERE cad = %s AND status != 'cleared'
            """, (force_status, outcome, private_notes, public_notes, cad))
        else:
            # Normal close: preserve MDT's status, just record outcome
            cur.execute("""
                UPDATE mdt_jobs
                   SET final_status = %s,
                       private_notes = %s,
                       public_notes = %s,
                       status = 'cleared',
                       updated_at = NOW()
                 WHERE cad = %s AND status != 'cleared'
            """, (outcome, private_notes, public_notes, cad))

        if cur.rowcount == 0:
            conn.rollback()
            return jsonify({'error': 'Job cannot be closed'}), 409

        # Release any linked units and return them to polling state.
        _ensure_job_units_table(cur)
        sender_name = _sender_label_from_portal('dispatch', getattr(current_user, 'username', ''))
        cur.execute("SHOW TABLES LIKE 'messages'")
        has_messages = cur.fetchone() is not None
        callsigns = _collect_job_callsigns(cur, cad)
        for cs in callsigns:
            cur.execute("SELECT crew FROM mdts_signed_on WHERE callSign = %s LIMIT 1", (cs,))
            row = cur.fetchone() or {}
            crew_json = row.get('crew') or '[]'
            cur.execute("""
                UPDATE mdts_signed_on
                   SET assignedIncident = NULL, status = 'on_standby'
                 WHERE callSign = %s AND assignedIncident = %s
            """, (cs, cad))
            try:
                cur.execute("""
                    INSERT INTO mdt_response_log (callSign, cad, status, event_time, crew)
                    VALUES (%s, %s, %s, NOW(), %s)
                """, (cs, cad, 'cleared', crew_json))
            except Exception:
                pass
            if has_messages:
                cur.execute("""
                    INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                    VALUES (%s, %s, %s, NOW(), 0)
                """, (sender_name, cs, f"CAD #{cad} closed ({outcome}). Stand down and return to polling."))

        cur.execute("DELETE FROM mdt_job_units WHERE job_cad = %s", (cad,))
        _sync_claimed_by_from_job_units(cur, cad)
        conn.commit()

        try:
            emit_msg = {'type': 'jobs_updated', 'cad': cad}
            if public_notes:
                emit_msg['public_notes'] = public_notes
            if force_status:
                emit_msg['forced_status'] = force_status
            socketio.emit('mdt_event', emit_msg, broadcast=True)
        except Exception:
            pass

        try:
            logger.info('Job closed: cad=%s outcome=%s forced=%s by=%s', cad, outcome, bool(force_status), getattr(
                current_user, 'username', 'unknown'))
            try:
                log_audit(getattr(current_user, 'username', 'unknown'),
                          'close_job', details={'cad': cad, 'outcome': outcome, 'forced': bool(force_status)})
            except Exception:
                pass
        except Exception:
            pass

        return jsonify({'message': 'Job closed', 'cad': cad, 'outcome': outcome, 'forced': bool(force_status)})
    finally:
        cur.close()
        conn.close()


@internal.route('/job/<int:cad>/force-status', methods=['POST'])
@login_required
def force_job_status(cad):
    """Dispatcher force-set job status due to comms issue or error. Use sparingly."""
    data = request.get_json() or {}
    new_status = data.get('status', '').strip()
    reason = data.get('reason', 'Unknown').strip()

    if not new_status:
        return jsonify({'error': 'status required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Authorization: only dispatchers/admins/clinical lead
        allowed_roles = ["dispatcher", "admin", "superuser", "clinical_lead"]
        if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
            return jsonify({'error': 'Unauthorised'}), 403

        cur.execute("""
            UPDATE mdt_jobs
               SET status = %s,
                   private_notes = CONCAT(IFNULL(private_notes, ''), 
                     '\n[Forced by ', %s, ': ', %s, ' at ', DATE_FORMAT(NOW(), '%Y-%m-%d %H:%i:%S'), ']')
             WHERE cad = %s
        """, (new_status, getattr(current_user, 'username', 'dispatcher'), reason, cad))

        if cur.rowcount == 0:
            conn.rollback()
            return jsonify({'error': 'Job not found'}), 404
        conn.commit()

        try:
            socketio.emit('mdt_event',
                          {'type': 'status_forced', 'cad': cad, 'status': new_status,
                              'by': getattr(current_user, 'username', 'dispatcher')},
                          broadcast=True)
        except Exception:
            pass

        try:
            logger.warning('Status forced: cad=%s new_status=%s reason=%s by=%s', cad, new_status, reason, getattr(
                current_user, 'username', 'unknown'))
            try:
                log_audit(getattr(current_user, 'username', 'unknown'),
                          'force_job_status', details={'cad': cad, 'status': new_status, 'reason': reason})
            except Exception:
                pass
        except Exception:
            pass

        return jsonify({'message': 'Status forced', 'cad': cad, 'status': new_status})
    finally:
        cur.close()
        conn.close()


@internal.route('/response', methods=['GET'])
@login_required
def response_dashboard():
    if not _user_has_role("crew", "dispatcher", "admin", "superuser", "clinical_lead", "call_taker", "calltaker", "controller"):
        return jsonify({"error": "Unauthorised access"}), 403
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        forms = _load_triage_forms(cur)
    finally:
        cur.close()
        conn.close()
    return render_template("response/dashboard.html", config=core_manifest, triage_forms=forms)


@internal.route('/response/forms', methods=['GET', 'POST'])
@login_required
def response_forms():
    if not _user_has_role("crew", "dispatcher", "admin", "superuser", "clinical_lead", "call_taker", "calltaker", "controller"):
        return jsonify({"error": "Unauthorised access"}), 403
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        if request.method == 'GET':
            return jsonify(_load_triage_forms(cur))

        edit_roles = ["admin", "superuser", "clinical_lead"]
        if current_user.role.lower() not in edit_roles:
            return jsonify({"error": "Unauthorised"}), 403
        payload = request.get_json() or {}
        slug = str(payload.get("slug") or "").strip().lower().replace(" ", "_")
        if not slug:
            return jsonify({"error": "slug is required"}), 400
        raw_form = {
            "slug": slug,
            "name": payload.get("name"),
            "description": payload.get("description"),
            "show_exclusions": bool(payload.get("show_exclusions", False)),
            "questions": payload.get("questions") or [],
            "priority_config": payload.get("priority_config") or {}
        }
        is_default = bool(payload.get("is_default", False))
        normalized = _normalize_triage_form(raw_form)
        if not normalized:
            return jsonify({"error": "invalid form payload"}), 400
        schema = {
            "dispatch_division": normalized.get("dispatch_division") or "general",
            "show_exclusions": normalized["show_exclusions"],
            "questions": normalized["questions"],
            "priority_config": normalized.get("priority_config") or _default_priority_config()
        }
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mdt_triage_forms (
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
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        if is_default:
            cur.execute("UPDATE mdt_triage_forms SET is_default = 0")
        cur.execute("""
            INSERT INTO mdt_triage_forms (slug, name, description, schema_json, is_active, is_default, created_by)
            VALUES (%s, %s, %s, CAST(%s AS JSON), 1, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                description = VALUES(description),
                schema_json = VALUES(schema_json),
                is_active = 1,
                is_default = VALUES(is_default),
                updated_at = CURRENT_TIMESTAMP
        """, (
            normalized["slug"],
            normalized["name"],
            normalized["description"],
            json.dumps(schema),
            1 if is_default else 0,
            getattr(current_user, 'username', 'unknown')
        ))
        conn.commit()
        return jsonify({"message": "Form saved", "form": normalized})
    finally:
        cur.close()
        conn.close()


@internal.route('/response/patient-lookup', methods=['GET'])
@login_required
def response_patient_lookup():
    """Smart patient lookup using local triage history (name/address/postcode/phone)."""
    if not _user_has_role("crew", "dispatcher", "admin", "superuser", "clinical_lead", "call_taker", "calltaker", "controller"):
        return jsonify({"error": "Unauthorised access"}), 403

    q = str(request.args.get("q") or "").strip()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        if q:
            like = f"%{q}%"
            cur.execute("""
                SELECT id, vita_record_id, first_name, middle_name, last_name, patient_dob,
                       phone_number, address, postcode, created_at
                FROM response_triage
                WHERE first_name LIKE %s
                   OR middle_name LIKE %s
                   OR last_name LIKE %s
                   OR phone_number LIKE %s
                   OR postcode LIKE %s
                   OR address LIKE %s
                ORDER BY created_at DESC
                LIMIT 100
            """, (like, like, like, like, like, like))
        else:
            cur.execute("""
                SELECT id, vita_record_id, first_name, middle_name, last_name, patient_dob,
                       phone_number, address, postcode, created_at
                FROM response_triage
                ORDER BY created_at DESC
                LIMIT 30
            """)
        rows = cur.fetchall() or []

        scored = []
        ql = q.lower()
        for r in rows:
            score = 0
            if q:
                for key, weight in (
                    ("postcode", 5),
                    ("phone_number", 4),
                    ("address", 3),
                    ("last_name", 3),
                    ("first_name", 2),
                    ("middle_name", 1),
                ):
                    val = str(r.get(key) or "").lower()
                    if not val:
                        continue
                    if ql == val:
                        score += weight * 2
                    elif ql in val:
                        score += weight
            scored.append((score, r))

        scored.sort(key=lambda x: (x[0], x[1].get("created_at") or datetime.min), reverse=True)
        dedup = {}
        for score, r in scored:
            key = r.get("vita_record_id") or f"triage-{r.get('id')}"
            if key not in dedup:
                full_name = " ".join([str(r.get("first_name") or "").strip(), str(r.get("middle_name") or "").strip(), str(r.get("last_name") or "").strip()]).strip()
                dedup[key] = {
                    "id": r.get("id"),
                    "vita_record_id": r.get("vita_record_id"),
                    "full_name": full_name,
                    "first_name": r.get("first_name"),
                    "middle_name": r.get("middle_name"),
                    "last_name": r.get("last_name"),
                    "patient_dob": str(r.get("patient_dob") or ""),
                    "phone_number": r.get("phone_number"),
                    "address": r.get("address"),
                    "postcode": r.get("postcode"),
                    "score": score,
                    "source": "response_triage"
                }
            if len(dedup) >= 25:
                break
        return jsonify({"patients": list(dedup.values())})
    except Exception as e:
        logger.exception("response_patient_lookup failed")
        return jsonify({"patients": [], "error": str(e)}), 200
    finally:
        cur.close()
        conn.close()


@internal.route('/response/triage', methods=['GET', 'POST'])
@login_required
def triage_form():
    if not _user_has_role("crew", "dispatcher", "admin", "superuser", "clinical_lead", "call_taker", "calltaker", "controller"):
        return jsonify({"error": "Unauthorized access"}), 403
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        forms = _load_triage_forms(cur)
    finally:
        cur.close()
        conn.close()

    selected_slug = request.args.get('form') or request.form.get('form_slug') or ''
    selected_form = _pick_triage_form(forms, selected_slug)
    triage_template_ctx = {
        "config": core_manifest,
        "triage_forms": forms,
        "selected_form": selected_form,
        "google_maps_api_key": (GOOGLE_MAPS_API_KEY or "")
    }

    if request.method == 'POST':
        # Vita record ID
        vita_record_str = request.form.get('vita_record_id', '')
        vita_record_id = int(
            vita_record_str) if vita_record_str.isdigit() else None

        # Patient details
        first_name = request.form.get('first_name', '').strip()
        middle_name = request.form.get('middle_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        dob_str = request.form.get('patient_dob', '')
        phone_number = request.form.get('phone_number', '').strip()
        address = request.form.get('address', '').strip()
        postcode = request.form.get('postcode', '').strip()
        what3words = request.form.get('what3words', '').strip()
        caller_name = request.form.get('caller_name', '').strip()
        caller_phone = request.form.get('caller_phone', '').strip()
        patient_gender = request.form.get('patient_gender', '').strip()
        additional_details = ''

        access_requirements_str = request.form.get('access_requirements', '')
        try:
            access_requirements = json.loads(
                access_requirements_str) if access_requirements_str.strip() else []
        except Exception:
            access_requirements = []
        if not isinstance(access_requirements, list):
            access_requirements = []

        # Triage information
        reason_for_call = request.form.get('reason_for_call', '').strip()
        onset_datetime = request.form.get('onset_datetime', '').strip()
        patient_alone = _normalize_patient_alone(request.form.get('patient_alone', ''))
        decision = str(request.form.get('decision', 'ACCEPT')).strip().upper()
        if decision not in ('ACCEPT', 'REJECT', 'ACCEPT_WITH_EXCLUSION', 'PENDING'):
            decision = 'ACCEPT'

        risk_flags_str = request.form.get('risk_flags', '')
        try:
            risk_flags = json.loads(
                risk_flags_str) if risk_flags_str.strip() else []
        except Exception:
            risk_flags = []
        if not isinstance(risk_flags, list):
            risk_flags = []

        # Convert date of birth
        patient_dob = None
        if dob_str:
            try:
                patient_dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid date format for Date of Birth.", "danger")
                return render_template("response/triage_form.html", **triage_template_ctx)

        onset_datetime_db = None
        if onset_datetime:
            parsed = None
            for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    parsed = datetime.strptime(onset_datetime, fmt)
                    break
                except Exception:
                    continue
            if parsed is None:
                flash("Invalid onset date/time format.", "danger")
                return render_template("response/triage_form.html", **triage_template_ctx)
            onset_datetime_db = parsed.strftime("%Y-%m-%d %H:%M:%S")

        # Gather exclusion responses only for forms that use them
        exclusion_data = {}
        if selected_form.get("show_exclusions"):
            exclusion_data = {key: request.form.get(
                key) for key in request.form if key.startswith("exclusion_")}

        # Dynamic per-form questions
        form_answers = {}
        for q in selected_form.get("questions") or []:
            key = str(q.get("key") or "")
            if not key:
                continue
            form_answers[key] = request.form.get(f"extra_{key}", "").strip()

        if not reason_for_call:
            reason_for_call = str(
                form_answers.get("primary_symptom")
                or form_answers.get("event_name")
                or form_answers.get("immediate_danger")
                or ""
            ).strip()

        priority_override = _normalize_priority_for_form(
            request.form.get('call_priority_override', ''),
            selected_form
        )
        if not priority_override:
            priority_override = _legacy_normalize_priority(request.form.get('call_priority_override', ''))
            priority_override = _normalize_priority_for_form(priority_override, selected_form)
        computed_priority = _compute_system_priority(
            reason_for_call=reason_for_call,
            selected_form=selected_form,
            decision=decision,
            exclusion_data=exclusion_data,
            form_answers=form_answers
        )
        call_priority = priority_override or computed_priority
        priority_source = 'manual' if priority_override else 'system'
        requested_division = _normalize_division(request.form.get('division'), fallback='')
        if not requested_division:
            requested_division = _normalize_division(selected_form.get('dispatch_division') or selected_form.get('division'), fallback='')
        if not requested_division:
            slug = str(selected_form.get('slug') or '').strip().lower()
            if slug == 'emergency_999':
                requested_division = 'emergency'
            elif slug == 'urgent_care':
                requested_division = 'urgent_care'
            elif slug == 'event_medical':
                requested_division = 'events'
            else:
                requested_division = 'general'

        validation_errors = []
        if not reason_for_call:
            validation_errors.append("Reason for call is required.")
        if not address:
            validation_errors.append("Address is required.")
        if not postcode and not what3words:
            validation_errors.append("Provide at least postcode or what3words.")
        for q in selected_form.get("questions") or []:
            if q.get("required") and not str(form_answers.get(q.get("key"), "")).strip():
                validation_errors.append(f"{q.get('label') or q.get('key')} is required.")
        if validation_errors:
            for msg in validation_errors:
                flash(msg, "danger")
            return render_template("response/triage_form.html", **triage_template_ctx)

        # 🌍 **NEW**: Ignore frontend lat/lng & determine location in backend
        best_coordinates = ResponseTriage.get_best_lat_lng(
            address=address,
            postcode=postcode,
            what3words=what3words
        )

        if "error" in best_coordinates:
            flash(
                "Warning: Unable to determine exact location. Defaulting to postcode if available.", "warning")

        # 1) Save the triage record
        try:
            new_id = ResponseTriage.create(
                created_by=current_user.username,
                vita_record_id=vita_record_id,
                first_name=first_name,
                middle_name=middle_name,
                last_name=last_name,
                patient_dob=patient_dob,
                phone_number=phone_number,
                address=address,
                postcode=postcode,
                entry_requirements=access_requirements,
                reason_for_call=reason_for_call,
                onset_datetime=onset_datetime_db,
                patient_alone=patient_alone,
                exclusion_data=exclusion_data,
                risk_flags=risk_flags,
                decision=decision,
                coordinates=best_coordinates
            )
        except Exception as e:
            logger.exception("triage_form create failed")
            flash(f"Unable to save triage record: {e}", "danger")
            return render_template("response/triage_form.html", **triage_template_ctx)

        # 2) Build payload for BroadNet & MDT
        triage_data = {
            "cad": new_id,
            "vita_record_id": vita_record_id,
            "first_name": first_name,
            "middle_name": middle_name,
            "last_name": last_name,
            "patient_dob": patient_dob,
            "phone_number": phone_number,
            "address": address,
            "postcode": postcode,
            "what3words": what3words,
            "caller_name": caller_name,
            "caller_phone": caller_phone,
            "patient_gender": patient_gender,
            "additional_details": additional_details,
            "entry_requirements": access_requirements,
            "reason_for_call": reason_for_call,
            "onset_datetime": onset_datetime_db,
            "patient_alone": patient_alone,
            "exclusion_data": exclusion_data,
            "risk_flags": risk_flags,
            "decision": decision,
            "call_priority": call_priority,
            "call_priority_label": _priority_label_for_form(call_priority, selected_form),
            "priority_source": priority_source,
            "form_slug": selected_form.get("slug"),
            "form_name": selected_form.get("name"),
            "form_answers": form_answers,
            "division": requested_division,
            "coordinates": best_coordinates
        }

        # 3) Send to BroadNet
        # ResponseTriage.post_triage_to_broadnet(triage_data)

        # 4) **ENQUEUE** into internal MDT queue
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'created_by'")
            has_created_by = cur.fetchone() is not None
            cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
            has_division = cur.fetchone() is not None
            if has_created_by:
                if has_division:
                    cur.execute("""
                      INSERT INTO mdt_jobs (cad, created_by, status, data, division)
                      VALUES (%s, %s, 'queued', %s, %s)
                    """, (
                        new_id,
                        current_user.username,
                        json.dumps(triage_data, default=str),
                        requested_division
                    ))
                else:
                    cur.execute("""
                      INSERT INTO mdt_jobs (cad, created_by, status, data)
                      VALUES (%s, %s, 'queued', %s)
                    """, (
                        new_id,
                        current_user.username,
                        json.dumps(triage_data, default=str)
                    ))
            else:
                if has_division:
                    cur.execute("""
                      INSERT INTO mdt_jobs (cad, status, data, division)
                      VALUES (%s, 'queued', %s, %s)
                    """, (
                        new_id,
                        json.dumps(triage_data, default=str),
                        requested_division
                    ))
                else:
                    cur.execute("""
                      INSERT INTO mdt_jobs (cad, status, data)
                      VALUES (%s, 'queued', %s)
                    """, (
                        new_id,
                        json.dumps(triage_data, default=str)
                    ))
            conn.commit()
            try:
                socketio.emit(
                    'mdt_event', {'type': 'jobs_updated', 'cad': new_id}, broadcast=True)
            except Exception:
                pass
            try:
                log_audit(getattr(current_user, 'username', 'unknown'),
                          'triage_create', details={'cad': new_id})
            except Exception:
                pass
        except Exception as e:
            conn.rollback()
            flash(f"Warning: MDT enqueue failed: {e}", "warning")
        finally:
            cur.close()
            conn.close()

        flash("Triage form submitted successfully!", "success")
        # Intake workflow: always hand off to the dedicated call-taker incident workspace.
        if _can_access_call_centre():
            return redirect(url_for('medical_response_internal.call_centre_job', cad=new_id))
        if _user_has_role("dispatcher", "admin", "superuser", "clinical_lead", "controller"):
            return redirect(url_for('medical_response_internal.cad_dashboard', panel='jobs', cad=new_id))
        return redirect(url_for('medical_response_internal.triage_list'))

    return render_template("response/triage_form.html", **triage_template_ctx)


@internal.route('/response/list')
@login_required
def triage_list():
    # Get all triage responses from the ResponseTriage class
    triage_list = ResponseTriage.get_all()
    return render_template("response/triage_list.html", triage_list=triage_list, config=core_manifest)


@internal.route('/call-centre', methods=['GET'])
@login_required
def call_centre_dashboard():
    """Dedicated call-taker CAD stack workspace."""
    if not _can_access_call_centre():
        return jsonify({"error": "Unauthorised access"}), 403
    return render_template("response/call_centre_dashboard.html", config=core_manifest)


@internal.route('/call-centre/wallboard', methods=['GET'])
@login_required
def call_centre_wallboard():
    """Large-screen TV wallboard for live CAD stack monitoring."""
    if not _can_access_call_centre():
        return jsonify({"error": "Unauthorised access"}), 403
    return render_template("response/call_centre_wallboard.html", config=core_manifest)


@internal.route('/call-centre/job/<int:cad>', methods=['GET'])
@login_required
def call_centre_job(cad):
    """Full-screen single-incident call-taker workspace."""
    if not _can_access_call_centre():
        return jsonify({"error": "Unauthorised access"}), 403
    return render_template("response/call_centre_job.html", config=core_manifest, cad=cad)


@internal.route('/call-centre/stack', methods=['GET'])
@login_required
def call_centre_stack():
    """Expanded CAD stack data optimized for call-centre and wallboard views."""
    if not _can_access_call_centre():
        return jsonify({"error": "Unauthorised access"}), 403
    selected_division, include_external = _request_division_scope()

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        selected_division, include_external, _ = _enforce_dispatch_scope(cur, selected_division, include_external)
        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
        has_division = cur.fetchone() is not None
        division_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_division else "'general' AS division"
        sql = """
            SELECT cad,
                   TRIM(COALESCE(status, '')) AS status,
                   data,
                   created_at,
                   updated_at,
                   {division_sql}
            FROM mdt_jobs
            WHERE LOWER(TRIM(COALESCE(status, ''))) NOT IN ('cleared', 'stood_down')
        """.format(division_sql=division_sql)
        args = []
        if selected_division and not include_external:
            if has_division:
                sql += " AND LOWER(TRIM(COALESCE(division, 'general'))) = %s"
                args.append(selected_division)
            elif selected_division != 'general':
                return jsonify([])
        sql += " ORDER BY cad DESC LIMIT 600"
        cur.execute(sql, tuple(args))
        rows = cur.fetchall() or []
        cads = [int(r.get('cad')) for r in rows if r.get('cad') is not None]
        assignments = {}
        if cads:
            try:
                _ensure_job_units_table(cur)
                placeholders = ",".join(["%s"] * len(cads))
                cur.execute(
                    f"SELECT job_cad, callsign FROM mdt_job_units WHERE job_cad IN ({placeholders}) ORDER BY assigned_at ASC",
                    cads
                )
                for row in (cur.fetchall() or []):
                    cad = int(row.get('job_cad'))
                    assignments.setdefault(cad, []).append(row.get('callsign'))
            except Exception:
                assignments = {}

        out = []
        for row in rows:
            payload = row.get('data')
            try:
                if isinstance(payload, (bytes, bytearray)):
                    payload = payload.decode('utf-8', errors='ignore')
                if isinstance(payload, str):
                    payload = json.loads(payload) if payload else {}
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}

            cad = int(row.get('cad'))
            out.append({
                'cad': cad,
                'status': row.get('status'),
                'division': _normalize_division(row.get('division'), fallback='general'),
                'created_at': row.get('created_at'),
                'updated_at': row.get('updated_at'),
                'reason_for_call': payload.get('reason_for_call'),
                'priority': payload.get('call_priority') or payload.get('priority') or payload.get('acuity'),
                'address': payload.get('address'),
                'postcode': payload.get('postcode'),
                'what3words': payload.get('what3words'),
                'caller_name': payload.get('caller_name'),
                'caller_phone': payload.get('caller_phone'),
                'assigned_units': assignments.get(cad, []),
            })
        return jsonify(out)
    finally:
        cur.close()
        conn.close()

# -----------------------
# ADMIN ROUTES
# -----------------------


@internal.route('/admin', methods=['GET'])
@login_required
def admin_dashboard():
    allowed_roles = ["admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        flash("Unauthorised access", "danger")
        return redirect(url_for('medical_response_internal.landing'))

    return render_template("admin/dashboard.html", config=core_manifest)


# =============================================================================
# CLINICAL ROUTES (retired)
# =============================================================================
@internal.route('/clinical', methods=['GET', 'POST'])
@login_required
def clinical_dashboard():
    return redirect(url_for('medical_response_internal.landing'))


# Add CORS headers to all responses
@internal.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    return response

# =============================================================================
# MDT ROUTES
# =============================================================================

# Helper to normalize callsign


def _normalize_callsign(payload=None, args=None):
    """
    Accept either 'callSign' or 'callsign' from JSON body or query params.
    """
    cs = None
    if payload:
        cs = payload.get('callSign') or payload.get('callsign')
    if not cs and args:
        cs = args.get('callSign') or args.get('callsign')
    return str(cs or '').strip().upper()


def _get_dispatch_mode(cur):
    """Return dispatch mode: 'auto' or 'manual'. Defaults to 'auto'."""
    mode = 'auto'
    try:
        _ensure_dispatch_settings_table(cur)
        cur.execute(
            "SELECT mode FROM mdt_dispatch_settings WHERE id = 1 LIMIT 1")
        row = cur.fetchone()
        if isinstance(row, dict):
            candidate = str(row.get('mode') or '').strip().lower()
        else:
            candidate = str(row[0] if row else '').strip().lower()
        if candidate in ('auto', 'manual'):
            mode = candidate
    except Exception:
        pass
    return mode


def _get_dispatch_motd(cur):
    """Return active dispatch message of the day metadata."""
    out = {
        'text': '',
        'updated_by': None,
        'updated_at': None
    }
    try:
        _ensure_dispatch_settings_table(cur)
        cur.execute("""
            SELECT motd_text, motd_updated_by, motd_updated_at
            FROM mdt_dispatch_settings
            WHERE id = 1
            LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return out
        if isinstance(row, dict):
            out['text'] = row.get('motd_text') or ''
            out['updated_by'] = row.get('motd_updated_by')
            out['updated_at'] = row.get('motd_updated_at')
        else:
            out['text'] = row[0] or ''
            out['updated_by'] = row[1] if len(row) > 1 else None
            out['updated_at'] = row[2] if len(row) > 2 else None
    except Exception:
        pass
    return out


def _ensure_job_units_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mdt_job_units (
            id INT AUTO_INCREMENT PRIMARY KEY,
            job_cad INT NOT NULL,
            callsign VARCHAR(64) NOT NULL,
            assigned_by VARCHAR(120),
            assigned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_job_callsign (job_cad, callsign),
            INDEX idx_job_cad (job_cad),
            INDEX idx_callsign (callsign)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def _ensure_job_comms_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mdt_job_comms (
            id INT AUTO_INCREMENT PRIMARY KEY,
            cad INT NOT NULL,
            message_type VARCHAR(24) NOT NULL DEFAULT 'message',
            sender_role VARCHAR(64),
            sender_user VARCHAR(120),
            message_text LONGTEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_job_comms_cad (cad),
            INDEX idx_job_comms_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def _get_job_unit_callsigns(cur, cad):
    cur.execute(
        "SELECT callsign FROM mdt_job_units WHERE job_cad = %s ORDER BY assigned_at ASC",
        (cad,)
    )
    rows = cur.fetchall() or []
    if rows and isinstance(rows[0], dict):
        return [r['callsign'] for r in rows if r.get('callsign')]
    return [r[0] for r in rows if r and r[0]]


def _sync_claimed_by_from_job_units(cur, cad):
    units = _get_job_unit_callsigns(cur, cad)
    claimed_by = ",".join(units) if units else None
    cur.execute(
        "UPDATE mdt_jobs SET claimedBy = %s WHERE cad = %s",
        (claimed_by, cad)
    )
    return units


def _collect_job_callsigns(cur, cad):
    callsigns = list(_get_job_unit_callsigns(cur, cad))
    cur.execute("SELECT callSign FROM mdts_signed_on WHERE assignedIncident = %s", (cad,))
    rows = cur.fetchall() or []
    if rows and isinstance(rows[0], dict):
        callsigns.extend([str(r.get('callSign') or '').strip() for r in rows if r.get('callSign')])
    else:
        callsigns.extend([str(r[0]).strip() for r in rows if r and r[0]])
    return list(dict.fromkeys([c for c in callsigns if c]))


def _extract_coords_from_job_data(payload):
    """Best-effort coordinates extraction from job JSON payload."""
    try:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode('utf-8', errors='ignore')
        if isinstance(payload, str):
            payload = json.loads(payload) if payload else {}
        if not isinstance(payload, dict):
            return (None, None, {})
        coords = payload.get('coordinates') or {}
        lat = coords.get('lat') if isinstance(coords, dict) else None
        lng = coords.get('lng') if isinstance(coords, dict) else None
        try:
            lat = float(lat) if lat is not None else None
            lng = float(lng) if lng is not None else None
        except Exception:
            lat = lng = None
        return (lat, lng, payload)
    except Exception:
        return (None, None, {})


def _extract_required_skills(job_payload):
    keys = ['required_skills', 'requiredSkills',
            'skill_requirements', 'skills_required']
    for key in keys:
        val = job_payload.get(key)
        if isinstance(val, list):
            return {str(x).strip().lower() for x in val if str(x).strip()}
        if isinstance(val, str) and val.strip():
            return {v.strip().lower() for v in val.split(',') if v.strip()}
    return set()


def _extract_unit_skills(crew_payload):
    skills = set()
    try:
        crew = crew_payload
        if isinstance(crew_payload, (bytes, bytearray)):
            crew = crew_payload.decode('utf-8', errors='ignore')
        if isinstance(crew, str):
            crew = json.loads(crew) if crew else []
        if not isinstance(crew, list):
            return skills
        for member in crew:
            if isinstance(member, str):
                # legacy list of crew names only; no skills to infer safely
                continue
            if not isinstance(member, dict):
                continue
            for key in ('skills', 'quals', 'qualifications', 'capabilities'):
                raw = member.get(key)
                if isinstance(raw, list):
                    skills.update(str(s).strip().lower()
                                  for s in raw if str(s).strip())
                elif isinstance(raw, str):
                    skills.update(s.strip().lower()
                                  for s in raw.split(',') if s.strip())
    except Exception:
        pass
    return skills


def _haversine_km(lat1, lon1, lat2, lon2):
    """Distance between 2 lat/lon points in kilometers."""
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

# 1) Sign-On


@internal.route('/api/mdt/signOn', methods=['POST', 'OPTIONS'])
def mdt_sign_on():
    if request.method == 'OPTIONS':
        return '', 200

    payload = request.get_json() or {}
    callsign = str(payload.get('callSign') or payload.get('callsign') or '').strip().upper()
    crew_raw = payload.get('crew')  # may be str or list
    status = str(payload.get('status') or 'on_standby').strip().lower()
    if status == 'available':
        status = 'on_standby'
    division = _normalize_division(payload.get('division'), fallback='general')

    # normalize crew into a list
    crew = []
    if isinstance(crew_raw, str):
        crew = [crew_raw]
    elif isinstance(crew_raw, list):
        crew = crew_raw

    if not callsign or not crew:
        return jsonify({'error': 'callSign (or callsign) and crew required'}), 400

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_mdts_signed_on_schema(cur)
        try:
            cur.execute("""
                DELETE FROM mdts_signed_on
                WHERE COALESCE(lastSeenAt, signOnTime) < DATE_SUB(NOW(), INTERVAL 120 MINUTE)
                  AND assignedIncident IS NULL
            """)
        except Exception:
            pass
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
        has_division = cur.fetchone() is not None
        if has_division:
            cur.execute(
                """
                INSERT INTO mdts_signed_on
                  (callSign, ipAddress, status, crew, division, lastSeenAt)
                VALUES
                  (%s, %s, %s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                  status           = VALUES(status),
                  crew             = VALUES(crew),
                  division         = VALUES(division),
                  lastSeenAt       = NOW(),
                  signOnTime       = CURRENT_TIMESTAMP,
                  assignedIncident = NULL
                """,
                (
                    callsign,
                    request.headers.get('X-Forwarded-For',
                                        request.remote_addr or ''),
                    status,
                    json.dumps(crew),
                    division
                )
            )
        else:
            cur.execute(
                """
                INSERT INTO mdts_signed_on
                  (callSign, ipAddress, status, crew, lastSeenAt)
                VALUES
                  (%s, %s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                  status           = VALUES(status),
                  crew             = VALUES(crew),
                  lastSeenAt       = NOW(),
                  signOnTime       = CURRENT_TIMESTAMP,
                  assignedIncident = NULL
                """,
                (
                    callsign,
                    request.headers.get('X-Forwarded-For',
                                        request.remote_addr or ''),
                    status,
                    json.dumps(crew)
                )
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

    try:
        socketio.emit('mdt_event', {
            'type': 'unit_signon',
            'callsign': callsign,
            'status': status,
            'division': division
        }, broadcast=True)
        socketio.emit('mdt_event', {'type': 'units_updated', 'callsign': callsign}, broadcast=True)
    except Exception:
        pass

    return jsonify({'message': 'Signed on'}), 200

# 2) Sign-Off


@internal.route('/api/mdt/signOff', methods=['POST', 'OPTIONS'])
def mdt_sign_off():
    if request.method == 'OPTIONS':
        return '', 200

    payload = request.get_json() or {}
    callsign = str(payload.get('callSign') or payload.get('callsign') or '').strip().upper()
    if not callsign:
        return jsonify({'error': 'callSign required'}), 400

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    affected_cads = set()
    try:
        _ensure_job_units_table(cur)
        # Capture affected CADs before removing unit row.
        cur.execute("SELECT assignedIncident FROM mdts_signed_on WHERE callSign = %s", (callsign,))
        for row in (cur.fetchall() or []):
            cad = row.get('assignedIncident')
            try:
                if cad is not None:
                    affected_cads.add(int(cad))
            except Exception:
                pass

        cur.execute("SELECT job_cad FROM mdt_job_units WHERE callsign = %s", (callsign,))
        for row in (cur.fetchall() or []):
            cad = row.get('job_cad')
            try:
                if cad is not None:
                    affected_cads.add(int(cad))
            except Exception:
                pass

        # Remove unit from all incident assignment links.
        cur.execute("DELETE FROM mdt_job_units WHERE callsign = %s", (callsign,))

        cur.execute(
            "DELETE FROM mdts_signed_on WHERE callSign = %s", (callsign,)
        )

        # Recompute per-CAD assignment state after sign-off cleanup.
        for cad in sorted(affected_cads):
            remaining = _sync_claimed_by_from_job_units(cur, cad)
            cur.execute("SELECT LOWER(TRIM(COALESCE(status, ''))) AS status FROM mdt_jobs WHERE cad = %s LIMIT 1", (cad,))
            jr = cur.fetchone() or {}
            st = str(jr.get('status') or '').strip().lower()
            if st != 'cleared' and len(remaining) == 0:
                cur.execute("UPDATE mdt_jobs SET status = 'queued' WHERE cad = %s AND LOWER(TRIM(COALESCE(status, ''))) <> 'cleared'", (cad,))

        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

    try:
        socketio.emit('mdt_event', {'type': 'unit_signoff', 'callsign': callsign}, broadcast=True)
        socketio.emit('mdt_event', {'type': 'units_updated', 'callsign': callsign}, broadcast=True)
        for cad in sorted(affected_cads):
            socketio.emit('mdt_event', {'type': 'jobs_updated', 'cad': cad}, broadcast=True)
    except Exception:
        pass

    return jsonify({'message': 'Signed off'}), 200

# 3) Next job


@internal.route('/api/mdt/next', methods=['GET', 'OPTIONS'])
def mdt_next():
    if request.method == 'OPTIONS':
        return '', 200

    callsign = _normalize_callsign(args=request.args)
    if not callsign:
        return '', 204

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        mode = _get_dispatch_mode(cur)

        # Return existing assignment if present, but self-heal stale pointers.
        cur.execute(
            "SELECT assignedIncident FROM mdts_signed_on WHERE callSign = %s ORDER BY signOnTime DESC LIMIT 1",
            (callsign,)
        )
        row = cur.fetchone() or {}
        assigned_cad = row.get('assignedIncident')
        valid_current = False
        if assigned_cad:
            try:
                assigned_cad = int(assigned_cad)
            except Exception:
                assigned_cad = None
        if assigned_cad:
            cur.execute(
                "SELECT LOWER(TRIM(COALESCE(status, ''))) AS status FROM mdt_jobs WHERE cad = %s LIMIT 1",
                (assigned_cad,)
            )
            jrow = cur.fetchone() or {}
            jstatus = str(jrow.get('status') or '').strip().lower()
            if jstatus and jstatus not in ('cleared', 'stood_down'):
                cur.execute("SHOW TABLES LIKE 'mdt_job_units'")
                has_links = cur.fetchone() is not None
                if has_links:
                    cur.execute(
                        "SELECT 1 FROM mdt_job_units WHERE job_cad = %s AND callsign = %s LIMIT 1",
                        (assigned_cad, callsign)
                    )
                    valid_current = cur.fetchone() is not None
                else:
                    valid_current = True
        if valid_current and assigned_cad:
            return jsonify({'cad': assigned_cad}), 200

        # Attempt to recover latest active assignment from job-unit links.
        recovered_cad = None
        cur.execute("SHOW TABLES LIKE 'mdt_job_units'")
        if cur.fetchone() is not None:
            cur.execute("""
                SELECT ju.job_cad
                FROM mdt_job_units ju
                JOIN mdt_jobs j ON j.cad = ju.job_cad
                WHERE ju.callsign = %s
                  AND LOWER(TRIM(COALESCE(j.status, ''))) NOT IN ('cleared', 'stood_down')
                ORDER BY ju.assigned_at DESC, ju.id DESC
                LIMIT 1
            """, (callsign,))
            rec = cur.fetchone() or {}
            recovered_cad = rec.get('job_cad')
            if recovered_cad:
                try:
                    recovered_cad = int(recovered_cad)
                except Exception:
                    recovered_cad = None
        if recovered_cad:
            cur.execute(
                "UPDATE mdts_signed_on SET assignedIncident = %s WHERE callSign = %s",
                (recovered_cad, callsign)
            )
            return jsonify({'cad': recovered_cad}), 200

        # No active assignment remains; clear stale pointer if present.
        if assigned_cad:
            cur.execute(
                "UPDATE mdts_signed_on SET assignedIncident = NULL WHERE callSign = %s",
                (callsign,)
            )

        # Manual mode: dispatcher assigns explicitly; do not advertise general queue.
        if mode == 'manual':
            return '', 204

        # Auto mode: pick best queued job by skill match and distance.
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
        has_unit_div = cur.fetchone() is not None
        unit_div_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_unit_div else "'general' AS division"
        cur.execute("""
            SELECT callSign,
                   LOWER(TRIM(COALESCE(status, ''))) AS status,
                   lastLat,
                   lastLon,
                   crew,
                   {unit_div_sql}
            FROM mdts_signed_on
            WHERE callSign = %s
            ORDER BY signOnTime DESC
            LIMIT 1
        """.format(unit_div_sql=unit_div_sql), (callsign,))
        unit = cur.fetchone()
        if not unit:
            return '', 204

        unit_status = (unit.get('status') or '').strip().lower()
        if unit_status not in ('on_standby', 'on_station', 'at_station', 'available', 'cleared', 'stood_down'):
            return '', 204

        unit_lat = unit.get('lastLat')
        unit_lon = unit.get('lastLon')
        try:
            unit_lat = float(unit_lat) if unit_lat is not None else None
            unit_lon = float(unit_lon) if unit_lon is not None else None
        except Exception:
            unit_lat = unit_lon = None
        unit_skills = _extract_unit_skills(unit.get('crew'))
        unit_division = _normalize_division(unit.get('division'), fallback='general')

        cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'division'")
        has_job_div = cur.fetchone() is not None
        if has_job_div:
            cur.execute(
                "SELECT cad, data, created_at, LOWER(TRIM(COALESCE(division, 'general'))) AS division FROM mdt_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 500"
            )
        else:
            cur.execute(
                "SELECT cad, data, created_at, 'general' AS division FROM mdt_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 500"
            )
        jobs = cur.fetchall() or []
        if not jobs:
            return '', 204

        candidates = []
        for job in jobs:
            job_division = _normalize_division(job.get('division'), fallback='general')
            if unit_division and job_division != unit_division:
                continue
            lat, lng, payload = _extract_coords_from_job_data(job.get('data'))
            required_skills = _extract_required_skills(payload)
            if required_skills and not required_skills.issubset(unit_skills):
                continue
            if unit_lat is not None and unit_lon is not None and lat is not None and lng is not None:
                dist_km = _haversine_km(unit_lat, unit_lon, lat, lng)
                has_dist = 0
            else:
                dist_km = 10**9
                has_dist = 1
            candidates.append(
                (has_dist, dist_km, job.get('created_at') or datetime.utcnow(), int(job['cad'])))

        if not candidates:
            return '', 204

        candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
        return jsonify({'cad': candidates[0][3]}), 200
    finally:
        cur.close()
        conn.close()

# 4) Claim


@internal.route('/api/mdt/<int:cad>/claim', methods=['POST', 'OPTIONS'])
def mdt_claim(cad):
    if request.method == 'OPTIONS':
        return '', 200

    # Only read callsign from query parameters
    callsign = (request.args.get('callSign')
                or request.args.get('callsign') or '').strip()
    if not callsign:
        return jsonify({'error': 'callSign is required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _ensure_job_units_table(cur)

        # Fast path: claim direct from queued for auto-dispatch scenarios.
        cur.execute(
            """
            UPDATE mdt_jobs
               SET status = 'claimed'
             WHERE cad = %s AND status = 'queued'
            """,
            (cad,)
        )
        if cur.rowcount == 1:
            cur.execute("""
                INSERT INTO mdt_job_units (job_cad, callsign, assigned_by)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    assigned_by = VALUES(assigned_by),
                    assigned_at = CURRENT_TIMESTAMP
            """, (cad, callsign, 'mdt_claim'))
            _sync_claimed_by_from_job_units(cur, cad)
        else:
            # Manual-dispatch path: only assigned units may claim assigned incidents.
            cur.execute(
                """
                SELECT 1
                FROM mdt_jobs j
                JOIN mdt_job_units ju ON ju.job_cad = j.cad
                WHERE j.cad = %s
                  AND j.status = 'assigned'
                  AND ju.callsign = %s
                LIMIT 1
                """,
                (cad, callsign)
            )
            allowed = cur.fetchone()
            if not allowed:
                conn.rollback()
                return jsonify({'error': 'Job already claimed or not assigned to this unit'}), 409
            cur.execute(
                "UPDATE mdt_jobs SET status = 'claimed' WHERE cad = %s AND status = 'assigned'",
                (cad,)
            )
            _sync_claimed_by_from_job_units(cur, cad)

        # Assign to callsign only; MDT must explicitly ACK receipt by posting
        # status='received' when the crew device has shown the job.
        cur.execute(
            """
            UPDATE mdts_signed_on
               SET assignedIncident = %s,
                   status           = 'assigned'
             WHERE callSign = %s
            """,
            (cad, callsign)
        )

        # Deliver any existing incident updates to the MDT when they first claim.
        try:
            cur.execute("SHOW TABLES LIKE 'messages'")
            has_messages = cur.fetchone() is not None
            if has_messages:
                cur.execute("SELECT data FROM mdt_jobs WHERE cad = %s LIMIT 1", (cad,))
                row = cur.fetchone()
                raw_data = row[0] if row else None
                payload = {}
                if isinstance(raw_data, (bytes, bytearray)):
                    raw_data = raw_data.decode('utf-8', errors='ignore')
                if isinstance(raw_data, str) and raw_data:
                    try:
                        payload = json.loads(raw_data)
                    except Exception:
                        payload = {}
                if isinstance(payload, dict):
                    updates = payload.get('incident_updates')
                    if isinstance(updates, list):
                        for upd in updates[-12:]:
                            if not isinstance(upd, dict):
                                continue
                            text = str(upd.get('text') or '').strip()
                            if not text:
                                continue
                            sender = str(upd.get('by') or 'dispatcher').strip() or 'dispatcher'
                            cur.execute("""
                                INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                                VALUES (%s, %s, %s, NOW(), 0)
                            """, (sender, callsign, f"CAD #{cad} UPDATE: {text}"))
        except Exception:
            # Best-effort notification fanout; never block claim.
            pass

        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

    return jsonify({'message': 'Job claimed'}), 200


# 5) History
@internal.route('/api/mdt/history', methods=['GET', 'POST', 'OPTIONS'])
def mdt_history():
    if request.method == 'OPTIONS':
        return '', 200

    if request.method == 'GET':
        callsign = _normalize_callsign(args=request.args)
        cads = request.args.getlist('cad', type=int)
    else:
        body = request.get_json() or {}
        callsign = body.get('callSign') or body.get('callsign') or ''
        cads = body.get('cads', [])

    if not callsign:
        return jsonify({'error': 'callSign required'}), 400
    if not cads:
        return jsonify([]), 200

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        placeholders = ",".join(["%s"] * len(cads))
        sql = f"""
          SELECT cad, status, event_time
          FROM mdt_response_log
          WHERE callSign = %s
            AND cad IN ({placeholders})
          ORDER BY cad ASC, event_time ASC
        """
        cur.execute(sql, [callsign] + cads)
        raw_rows = cur.fetchall() or []
    finally:
        cur.close()
        conn.close()

    by_cad = {}
    for row in raw_rows:
        try:
            cad_val = int(row.get('cad'))
        except Exception:
            continue
        by_cad.setdefault(cad_val, []).append(row)

    result = []
    for cad in cads:
        try:
            cad_int = int(cad)
        except Exception:
            continue
        events = by_cad.get(cad_int, [])
        if not events:
            continue

        # New cycle starts at the latest explicit (re)assignment/receive marker.
        start_idx = 0
        for i, ev in enumerate(events):
            st = str(ev.get('status') or '').strip().lower()
            if st in ('assigned', 'received'):
                start_idx = i
        cycle = events[start_idx:] if events else []

        def _latest_time(status_key):
            t = None
            for ev in cycle:
                if str(ev.get('status') or '').strip().lower() == status_key:
                    t = ev.get('event_time')
            return t

        result.append({
            'cad': cad_int,
            'received_time': _latest_time('received'),
            'assigned_time': _latest_time('assigned'),
            'mobile_time': _latest_time('mobile'),
            'on_scene_time': _latest_time('on_scene'),
            'leave_scene_time': _latest_time('leave_scene'),
            'at_hospital_time': _latest_time('at_hospital'),
            'cleared_time': _latest_time('cleared'),
            'stood_down_time': _latest_time('stood_down')
        })

    return _jsonify_safe(result, 200)

# 6) Details


@internal.route('/api/mdt/<int:cad>', methods=['GET', 'OPTIONS'])
def mdt_details(cad):
    if request.method == 'OPTIONS':
        return '', 200

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT status, data FROM mdt_jobs WHERE cad = %s",
        (cad,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({'error': 'Not found'}), 404

    return _jsonify_safe({
        'cad': cad,
        'status': row['status'],
        'triage_data': row['data']
    }, 200)

# 6b) CAD comms/update stream for MDT clients (sessionless)


@internal.route('/api/mdt/<int:cad>/comms', methods=['GET', 'POST', 'OPTIONS'])
def mdt_job_comms_api(cad):
    if request.method == 'OPTIONS':
        return '', 200

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_job_comms_table(cur)
        cur.execute("SELECT cad FROM mdt_jobs WHERE cad = %s LIMIT 1", (cad,))
        if cur.fetchone() is None:
            return jsonify({'error': 'Job not found'}), 404

        if request.method == 'GET':
            cur.execute("""
                SELECT id, cad, message_type, sender_role, sender_user, message_text, created_at
                FROM mdt_job_comms
                WHERE cad = %s
                ORDER BY created_at ASC, id ASC
                LIMIT 800
            """, (cad,))
            return _jsonify_safe(cur.fetchall() or [], 200)

        payload = request.get_json(silent=True) or {}
        msg_text = str(payload.get('text') or '').strip()
        msg_type = str(payload.get('type') or 'message').strip().lower()
        if msg_type not in ('message', 'update'):
            msg_type = 'message'
        if not msg_text:
            return jsonify({'error': 'text is required'}), 400

        sender_user = str(
            payload.get('callSign')
            or payload.get('callsign')
            or payload.get('from')
            or 'mdt'
        ).strip()[:120]
        if not sender_user:
            sender_user = 'mdt'

        cur.execute("""
            INSERT INTO mdt_job_comms (cad, message_type, sender_role, sender_user, message_text)
            VALUES (%s, %s, %s, %s, %s)
        """, (cad, msg_type, 'crew', sender_user, msg_text))

        assigned_units_for_push = []
        if msg_type == 'update':
            cur.execute("SELECT data FROM mdt_jobs WHERE cad = %s", (cad,))
            row = cur.fetchone() or {}
            raw = row.get('data')
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode('utf-8', errors='ignore')
                data = json.loads(raw) if isinstance(raw, str) and raw else {}
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            history = data.get('incident_updates')
            if not isinstance(history, list):
                history = []
            history.append({
                'time': datetime.utcnow().isoformat(),
                'by': sender_user,
                'text': msg_text
            })
            data['incident_updates'] = history
            cur.execute("SHOW COLUMNS FROM mdt_jobs LIKE 'updated_at'")
            has_updated_at = cur.fetchone() is not None
            if has_updated_at:
                cur.execute("UPDATE mdt_jobs SET data = %s, updated_at = NOW() WHERE cad = %s",
                            (json.dumps(data, default=str), cad))
            else:
                cur.execute("UPDATE mdt_jobs SET data = %s WHERE cad = %s",
                            (json.dumps(data, default=str), cad))
            try:
                assigned_units_for_push = _get_job_unit_callsigns(cur, cad)
            except Exception:
                assigned_units_for_push = []

        # Mirror into dispatcher/MDT inbox feed.
        try:
            cur.execute("SHOW TABLES LIKE 'messages'")
            if cur.fetchone() is not None:
                cur.execute("""
                    INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                    VALUES (%s, %s, %s, NOW(), 0)
                """, (sender_user, 'dispatcher', f"CAD #{cad} {msg_type.upper()}: {msg_text}"))
                if msg_type == 'update':
                    for callsign in assigned_units_for_push:
                        cur.execute("""
                            INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                            VALUES (%s, %s, %s, NOW(), 0)
                        """, (sender_user, callsign, f"CAD #{cad} UPDATE: {msg_text}"))
        except Exception:
            pass

        conn.commit()

        try:
            socketio.emit('mdt_event', {'type': 'job_comm', 'cad': cad, 'message_type': msg_type, 'text': msg_text, 'by': sender_user}, broadcast=True)
            if msg_type == 'update':
                socketio.emit('mdt_event', {'type': 'jobs_updated', 'cad': cad}, broadcast=True)
                socketio.emit('mdt_event', {'type': 'job_update', 'cad': cad, 'text': msg_text, 'units': assigned_units_for_push}, broadcast=True)
        except Exception:
            pass

        return jsonify({'message': 'sent', 'cad': cad, 'type': msg_type}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

# 7) Status update (includes clear & stand-down)


@internal.route('/api/mdt/<int:cad>/status', methods=['POST', 'OPTIONS'])
def mdt_status(cad):
    if request.method == 'OPTIONS':
        return '', 200

    payload = request.get_json() or {}
    status = payload.get('status')
    callsign = payload.get('callSign') or payload.get('callsign') or ''

    valid = {
        'received', 'assigned', 'mobile', 'on_scene',
        'leave_scene', 'at_hospital', 'cleared', 'stood_down'
    }
    if status not in valid:
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        if not callsign:
            # Fallback for MDT clients that omit callsign in status payload:
            # prefer explicit job-unit links, then signed-on assigned incident.
            cur.execute("SHOW TABLES LIKE 'mdt_job_units'")
            if cur.fetchone() is not None:
                cur.execute(
                    "SELECT callsign FROM mdt_job_units WHERE job_cad = %s ORDER BY id DESC LIMIT 1",
                    (cad,)
                )
                linked = cur.fetchone() or {}
                callsign = str(linked.get('callsign') or '').strip()
            if not callsign:
                cur.execute(
                    "SELECT callSign FROM mdts_signed_on WHERE assignedIncident = %s ORDER BY signOnTime DESC LIMIT 1",
                    (cad,)
                )
                live = cur.fetchone() or {}
                callsign = str(live.get('callSign') or '').strip()
        if not callsign:
            return jsonify({'error': 'callSign required'}), 400

        # Reject stale status events for superseded/reassigned incidents.
        # Only the currently assigned CAD (or explicitly linked unit<->CAD) may
        # advance operational statuses.
        cur.execute(
            "SELECT assignedIncident FROM mdts_signed_on WHERE callSign = %s ORDER BY signOnTime DESC LIMIT 1",
            (callsign,)
        )
        live_row = cur.fetchone() or {}
        live_assigned = live_row.get('assignedIncident')
        is_live_assignment = False
        try:
            is_live_assignment = int(live_assigned) == int(cad)
        except Exception:
            is_live_assignment = False

        is_linked_assignment = False
        cur.execute("SHOW TABLES LIKE 'mdt_job_units'")
        if cur.fetchone() is not None:
            cur.execute(
                "SELECT 1 FROM mdt_job_units WHERE job_cad = %s AND callsign = %s LIMIT 1",
                (cad, callsign)
            )
            is_linked_assignment = cur.fetchone() is not None

        if status not in ('cleared', 'stood_down') and not (is_live_assignment or is_linked_assignment):
            return jsonify({
                'error': 'stale status update',
                'callsign': callsign,
                'cad': cad,
                'status': status
            }), 409

        # fetch current crew JSON
        cur.execute(
            "SELECT crew FROM mdts_signed_on WHERE callSign = %s ORDER BY signOnTime DESC LIMIT 1",
            (callsign,)
        )
        row = cur.fetchone()
        crew_json = row['crew'] if row and row.get('crew') else '[]'

        # Ensure CAD exists before applying status transitions.
        cur.execute("SELECT cad FROM mdt_jobs WHERE cad = %s LIMIT 1", (cad,))
        if cur.fetchone() is None:
            return jsonify({'error': 'CAD not found'}), 404

        # Unit stood-down is not automatically incident stood-down.
        # If this was the active assignment and no units remain linked to the CAD,
        # push incident back to queued so dispatcher can reassign.
        if status == 'stood_down':
            cur.execute("SHOW TABLES LIKE 'mdt_job_units'")
            has_job_units = cur.fetchone() is not None
            remaining_units = []
            if has_job_units:
                cur.execute(
                    "DELETE FROM mdt_job_units WHERE job_cad = %s AND callsign = %s",
                    (cad, callsign)
                )
                remaining_units = _sync_claimed_by_from_job_units(cur, cad)
            if is_live_assignment or is_linked_assignment:
                if len(remaining_units) == 0:
                    cur.execute(
                        "UPDATE mdt_jobs SET status = 'queued' WHERE cad = %s AND LOWER(TRIM(COALESCE(status, ''))) <> 'cleared'",
                        (cad,)
                    )
                else:
                    cur.execute(
                        "UPDATE mdt_jobs SET status = 'assigned' WHERE cad = %s",
                        (cad,)
                    )
            # stale stood_down from an old/superseded CAD should not override job state
        else:
            # Normal progression and explicit clear states.
            cur.execute(
                "UPDATE mdt_jobs SET status = %s WHERE cad = %s",
                (status, cad)
            )

        # log status change
        cur.execute(
            "INSERT INTO mdt_response_log (callSign, cad, status, event_time, crew) VALUES (%s, %s, %s, NOW(), %s)",
            (callsign, cad, status, crew_json)
        )

        # update live status, but never let an old CAD clear a newer assignment.
        # Only clear assignedIncident when the CAD being cleared/stood_down is
        # still the unit's current assignment.
        cur.execute(
            """
            UPDATE mdts_signed_on
               SET status = CASE
                              WHEN %s IN ('cleared','stood_down') THEN
                                CASE
                                  WHEN assignedIncident = %s OR assignedIncident IS NULL THEN 'on_standby'
                                  ELSE status
                                END
                              ELSE %s
                            END,
                   assignedIncident = CASE
                                        WHEN %s IN ('cleared','stood_down') AND assignedIncident = %s THEN NULL
                                        ELSE assignedIncident
                                      END
             WHERE callSign = %s
            """,
            (status, cad, status, status, cad, callsign)
        )

        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

    try:
        socketio.emit('mdt_event', {
            'type': 'status_update',
            'cad': cad,
            'status': status,
            'callsign': callsign
        }, broadcast=True)
        socketio.emit('mdt_event', {'type': 'jobs_updated', 'cad': cad}, broadcast=True)
    except Exception:
        pass

    return jsonify({'message': 'Status updated and logged'}), 200

# 8) Location update (real-time position reporting)


def _update_mdt_location(callsign, latitude, longitude):
    callsign = str(callsign or '').strip().upper()
    if not callsign:
        return {'error': 'callSign required'}, 400
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except Exception:
        return {'error': 'latitude and longitude must be numeric'}, 400

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        # Keep historic location table available across mixed schemas.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mdt_positions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                callSign VARCHAR(64) NOT NULL,
                latitude DECIMAL(10,7) NOT NULL,
                longitude DECIMAL(10,7) NOT NULL,
                recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_call_time (callSign, recorded_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # Only update columns that exist in mdts_signed_on.
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'lastLat'")
        has_last_lat = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'lastLon'")
        has_last_lon = cur.fetchone() is not None
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'lastSeenAt'")
        has_last_seen = cur.fetchone() is not None

        set_parts = []
        args = []
        if has_last_lat:
            set_parts.append("lastLat = %s")
            args.append(latitude)
        if has_last_lon:
            set_parts.append("lastLon = %s")
            args.append(longitude)
        if has_last_seen:
            set_parts.append("lastSeenAt = NOW()")
        if set_parts:
            args.append(callsign)
            cur.execute(
                f"UPDATE mdts_signed_on SET {', '.join(set_parts)} WHERE callSign = %s",
                tuple(args)
            )

        # Auto-ack delivery heuristic:
        # If a unit is still in "assigned" and we receive a successful ping
        # after dispatch, treat this as transport-level confirmation that the
        # MDT is online and mark current assignment as "received" once.
        cur.execute("""
            SELECT assignedIncident, status, crew
            FROM mdts_signed_on
            WHERE callSign = %s
            ORDER BY signOnTime DESC
            LIMIT 1
        """, (callsign,))
        live = cur.fetchone() or {}
        live_cad = live.get('assignedIncident')
        live_status = str(live.get('status') or '').strip().lower()
        crew_json = live.get('crew') or '[]'
        if live_cad and live_status == 'assigned':
            try:
                live_cad = int(live_cad)
            except Exception:
                live_cad = None
        if live_cad:
            cur.execute("""
                SELECT
                  MAX(CASE WHEN status = 'assigned' THEN event_time END) AS last_assigned,
                  MAX(CASE WHEN status = 'received' THEN event_time END) AS last_received
                FROM mdt_response_log
                WHERE callSign = %s AND cad = %s
            """, (callsign, live_cad))
            marker = cur.fetchone() or {}
            last_assigned = marker.get('last_assigned')
            last_received = marker.get('last_received')
            should_ack = bool(last_assigned) and (not last_received or last_received < last_assigned)
            if should_ack:
                cur.execute(
                    "UPDATE mdts_signed_on SET status = 'received' WHERE callSign = %s",
                    (callsign,)
                )
                cur.execute("""
                    INSERT INTO mdt_response_log (callSign, cad, status, event_time, crew)
                    VALUES (%s, %s, 'received', NOW(), %s)
                """, (callsign, live_cad, crew_json))

        cur.execute(
            "INSERT INTO mdt_positions (callSign, latitude, longitude, recorded_at) VALUES (%s, %s, %s, NOW())",
            (callsign, latitude, longitude)
        )
        conn.commit()
        return {'message': 'Location updated', 'callSign': callsign}, 200
    except Exception as e:
        conn.rollback()
        return {'error': str(e)}, 500
    finally:
        cur.close()
        conn.close()


@internal.route('/api/mdt/location', methods=['POST', 'OPTIONS'])
def mdt_update_location():
    if request.method == 'OPTIONS':
        return '', 200

    payload = request.get_json(silent=True) or {}
    callsign = payload.get('callSign') or payload.get('callsign') or ''
    latitude = payload.get('latitude', payload.get('lat'))
    longitude = payload.get('longitude', payload.get('lng'))
    body, code = _update_mdt_location(callsign, latitude, longitude)
    if code == 200:
        try:
            cs = str((body or {}).get('callSign') or callsign or '').strip().upper()
            if cs:
                socketio.emit('mdt_event', {'type': 'units_updated', 'callsign': cs}, broadcast=True)
        except Exception:
            pass
    return jsonify(body), code


# --- MDT compatibility aliases (legacy/mobile clients) ---
@internal.route('/api/mdt/<callsign>/location', methods=['POST', 'OPTIONS'])
def mdt_update_location_legacy(callsign):
    """Legacy alias: location endpoint with callsign in URL path."""
    if request.method == 'OPTIONS':
        return '', 200
    payload = request.get_json(silent=True) or {}
    latitude = payload.get('latitude', payload.get('lat'))
    longitude = payload.get('longitude', payload.get('lng'))
    body, code = _update_mdt_location(callsign, latitude, longitude)
    if code == 200:
        try:
            cs = str((body or {}).get('callSign') or callsign or '').strip().upper()
            if cs:
                socketio.emit('mdt_event', {'type': 'units_updated', 'callsign': cs}, broadcast=True)
        except Exception:
            pass
    return jsonify(body), code


@internal.route('/api/mdt/<callsign>/crew', methods=['POST', 'OPTIONS'])
def mdt_update_crew_legacy(callsign):
    """Legacy alias: update signed-on unit crew list."""
    if request.method == 'OPTIONS':
        return '', 200
    payload = request.get_json(silent=True) or {}
    crew_raw = payload.get('crew')
    crew = []
    if isinstance(crew_raw, str):
        crew = [crew_raw]
    elif isinstance(crew_raw, list):
        crew = crew_raw
    if not crew:
        return jsonify({'error': 'crew required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE mdts_signed_on SET crew = %s WHERE callSign = %s",
            (json.dumps(crew), callsign)
        )
        if cur.rowcount == 0:
            return jsonify({'error': 'callsign not signed on'}), 404
        conn.commit()
        return jsonify({'message': 'Crew updated', 'callSign': callsign, 'crew_count': len(crew)}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@internal.route('/api/mdt/<callsign>/standby', methods=['GET', 'OPTIONS'])
def mdt_standby_legacy(callsign):
    """Legacy alias: fetch latest standby assignment for a callsign."""
    if request.method == 'OPTIONS':
        return '', 200
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SHOW TABLES LIKE 'standby_locations'")
        if cur.fetchone() is None:
            return _jsonify_safe({'callsign': callsign, 'standby': None}, 200)
        cur.execute("""
            SELECT id, name, lat, lng, updatedAt
            FROM standby_locations
            WHERE callSign = %s
            ORDER BY updatedAt DESC, id DESC
            LIMIT 1
        """, (callsign,))
        row = cur.fetchone()
        if not row:
            return _jsonify_safe({'callsign': callsign, 'standby': None}, 200)
        return _jsonify_safe({
            'callsign': callsign,
            'standby': {
                'id': row.get('id'),
                'name': row.get('name'),
                'lat': row.get('lat'),
                'lng': row.get('lng'),
                'updated_at': row.get('updatedAt')
            }
        }, 200)
    finally:
        cur.close()
        conn.close()


@internal.route('/api/mdt/<callsign>/status', methods=['GET', 'OPTIONS'])
def mdt_unit_status_legacy(callsign):
    """Return current live unit status for MDT polling."""
    if request.method == 'OPTIONS':
        return '', 200
    cs = str(callsign or '').strip().upper()
    if not cs:
        return jsonify({'error': 'callsign required'}), 400
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        _ensure_meal_break_columns(cur)
        cur.execute("SHOW COLUMNS FROM mdts_signed_on LIKE 'division'")
        has_division = cur.fetchone() is not None
        div_sql = "LOWER(TRIM(COALESCE(division, 'general'))) AS division" if has_division else "'general' AS division"
        cur.execute(f"""
            SELECT callSign, status, assignedIncident, lastSeenAt, mealBreakUntil, {div_sql}
            FROM mdts_signed_on
            WHERE callSign = %s
            LIMIT 1
        """, (cs,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'not signed on', 'callsign': cs}), 404
        return _jsonify_safe({
            'callsign': cs,
            'status': str(row.get('status') or ''),
            'assigned_incident': row.get('assignedIncident'),
            'last_seen_at': row.get('lastSeenAt'),
            'meal_break_until': row.get('mealBreakUntil'),
            'division': _normalize_division(row.get('division'), fallback='general')
        }, 200)
    finally:
        cur.close()
        conn.close()


@internal.route('/api/messages/<callsign>', methods=['GET', 'POST', 'OPTIONS'])
def api_messages_legacy(callsign):
    """MDT/mobile API messages endpoint (sessionless)."""
    if request.method == 'OPTIONS':
        return '', 200
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cs = str(callsign or '').strip()
    if not cs:
        cur.close()
        conn.close()
        return jsonify({'error': 'callsign required'}), 400
    try:
        if request.method == 'GET':
            cur.execute("""
                SELECT id, `from`, recipient, text, timestamp, COALESCE(`read`, 0) AS `read`
                FROM messages
                WHERE LOWER(TRIM(recipient)) = LOWER(TRIM(%s))
                   OR LOWER(TRIM(`from`)) = LOWER(TRIM(%s))
                ORDER BY timestamp ASC
            """, (cs, cs))
            rows = cur.fetchall() or []
            return _jsonify_safe(rows, 200)

        data = request.get_json(silent=True) or {}
        text = str(data.get('text') or '').strip()
        if not text:
            return jsonify({'error': 'Message text required'}), 400
        if len(text) > 2000:
            return jsonify({'error': 'Message too long'}), 400

        sender = str(
            data.get('from')
            or data.get('sender')
            or data.get('callSign')
            or data.get('callsign')
            or 'mdt'
        ).strip()[:120]
        if not sender:
            sender = 'mdt'
        cur.execute("""
            INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
            VALUES (%s, %s, %s, NOW(), 0)
        """, (sender, cs, text))
        conn.commit()
        try:
            socketio.emit('mdt_event', {
                'type': 'message_posted',
                'from': sender,
                'to': cs,
                'text': text
            }, broadcast=True)
        except Exception:
            pass
        return jsonify({'message': 'Message sent'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@internal.route('/api/messages/<callsign>/unread', methods=['GET', 'OPTIONS'])
def api_messages_unread_legacy(callsign):
    """Legacy alias: unread count for callsign."""
    if request.method == 'OPTIONS':
        return '', 200
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SHOW TABLES LIKE 'messages'")
        if cur.fetchone() is None:
            return jsonify({'callsign': callsign, 'unread': 0}), 200
        cur.execute("""
            SELECT COUNT(*) AS unread
            FROM messages
            WHERE LOWER(TRIM(recipient)) = LOWER(TRIM(%s))
              AND COALESCE(`read`, 0) = 0
        """, (callsign,))
        row = cur.fetchone() or {}
        unread = int(row.get('unread') or 0)
        return jsonify({'callsign': callsign, 'unread': unread}), 200
    finally:
        cur.close()
        conn.close()


@internal.route('/api/messages/<callsign>/read', methods=['POST', 'OPTIONS'])
def api_messages_mark_read_legacy(callsign):
    """Legacy alias: mark all recipient messages read."""
    if request.method == 'OPTIONS':
        return '', 200
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SHOW TABLES LIKE 'messages'")
        if cur.fetchone() is None:
            return jsonify({'callsign': callsign, 'updated': 0}), 200
        cur.execute(
            "UPDATE messages SET `read` = 1 WHERE LOWER(TRIM(recipient)) = LOWER(TRIM(%s)) AND COALESCE(`read`, 0) = 0",
            (callsign,)
        )
        updated = cur.rowcount or 0
        conn.commit()
        return jsonify({'callsign': callsign, 'updated': int(updated)}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@internal.route('/api/ping', methods=['GET', 'OPTIONS'])
def api_ping_legacy():
    """Legacy health endpoint for MDT heartbeat."""
    if request.method == 'OPTIONS':
        return '', 200
    return jsonify({'ok': True, 'service': 'ventus_response_module', 'time': datetime.utcnow().isoformat() + 'Z'}), 200


# --- Root-level compatibility aliases (true /api/* paths) ---
@api_compat.route('/api/mdt/signOn', methods=['POST', 'OPTIONS'])
def mdt_sign_on_root_compat():
    return mdt_sign_on()


@api_compat.route('/api/mdt/signOff', methods=['POST', 'OPTIONS'])
def mdt_sign_off_root_compat():
    return mdt_sign_off()


@api_compat.route('/api/mdt/next', methods=['GET', 'OPTIONS'])
def mdt_next_root_compat():
    return mdt_next()


@api_compat.route('/api/mdt/history', methods=['GET', 'POST', 'OPTIONS'])
def mdt_history_root_compat():
    return mdt_history()


@api_compat.route('/api/mdt/<int:cad>', methods=['GET', 'OPTIONS'])
def mdt_details_root_compat(cad):
    return mdt_details(cad)


@api_compat.route('/api/mdt/<int:cad>/claim', methods=['POST', 'OPTIONS'])
def mdt_claim_root_compat(cad):
    return mdt_claim(cad)


@api_compat.route('/api/mdt/<int:cad>/status', methods=['POST', 'OPTIONS'])
def mdt_status_root_compat(cad):
    return mdt_status(cad)


@api_compat.route('/api/mdt/<int:cad>/comms', methods=['GET', 'POST', 'OPTIONS'])
def mdt_job_comms_root_compat(cad):
    return mdt_job_comms_api(cad)


@api_compat.route('/api/mdt/<callsign>/location', methods=['POST', 'OPTIONS'])
def mdt_update_location_root_compat(callsign):
    return mdt_update_location_legacy(callsign)


@api_compat.route('/api/mdt/<callsign>/crew', methods=['POST', 'OPTIONS'])
def mdt_update_crew_root_compat(callsign):
    return mdt_update_crew_legacy(callsign)


@api_compat.route('/api/mdt/<callsign>/standby', methods=['GET', 'OPTIONS'])
def mdt_standby_root_compat(callsign):
    return mdt_standby_legacy(callsign)


@api_compat.route('/api/mdt/<callsign>/status', methods=['GET', 'OPTIONS'])
def mdt_unit_status_root_compat(callsign):
    return mdt_unit_status_legacy(callsign)


@api_compat.route('/api/messages/<callsign>', methods=['GET', 'POST', 'OPTIONS'])
def api_messages_root_compat(callsign):
    return api_messages_legacy(callsign)


@api_compat.route('/api/messages/<callsign>/unread', methods=['GET', 'OPTIONS'])
def api_messages_unread_root_compat(callsign):
    return api_messages_unread_legacy(callsign)


@api_compat.route('/api/messages/<callsign>/read', methods=['POST', 'OPTIONS'])
def api_messages_mark_read_root_compat(callsign):
    return api_messages_mark_read_legacy(callsign)


@api_compat.route('/api/ping', methods=['GET', 'OPTIONS'])
def api_ping_root_compat():
    return api_ping_legacy()


# =============================================================================
# PUBLIC BLUEPRINT
# =============================================================================
public_template_folder = os.path.join(
    os.path.dirname(__file__), 'templates', 'public')
public = Blueprint(
    'ventus_response',
    __name__,
    url_prefix='/ventus',
    template_folder=public_template_folder
)


@public.before_request
def ensure_ventus_response_portal_user():
    # If the user isn't authenticated yet, let the login_required decorator handle it.
    if not current_user.is_authenticated:
        return
    # Now that the user is authenticated, ensure they are from the Vita-Care-Portal module.
    if not hasattr(current_user, 'role') or current_user.role != "Ventus-Response-Portal":
        return jsonify({"error": "Unauthorised access"}), 403

# =============================================================================
# Blueprint Registration Functions
# =============================================================================


def get_blueprint():
    # Keep the original blueprint name/endpoints so existing template url_for
    # calls (medical_response_internal.*) continue to resolve.
    return internal


def get_public_blueprint():
    return public


@internal.route('/panel/<panel_type>', methods=['GET'])
@login_required
def panel_popup(panel_type):
    """Serve a lightweight panel view usable as a popout window.

    This page subscribes to the BroadcastChannel "ventus_cad" for live updates.
    """
    # Normalize and validate panel type to avoid invalid popout routing.
    normalized = (panel_type or '').strip().lower()
    aliases = {
        'msgs': 'messages',
        'message': 'messages',
        'past': 'past_jobs',
        'pastjobs': 'past_jobs',
        'history': 'past_jobs'
    }
    normalized = aliases.get(normalized, normalized)
    allowed = {'jobs', 'units', 'messages', 'past_jobs', 'audit'}
    if normalized not in allowed:
        return jsonify({'error': 'Invalid panel type'}), 400
    # Popouts now use the full CAD dashboard in single-panel mode to guarantee
    # parity with the main in-page panel content.
    qs = request.args.to_dict(flat=True)
    panel_for_dashboard = normalized if normalized in {'jobs', 'units', 'messages'} else 'jobs'
    query = {
        'panel': panel_for_dashboard,
        'title': str(qs.get('title') or panel_for_dashboard).strip(),
        'popout': '1',
    }
    division = str(qs.get('division') or '').strip()
    include_external = str(qs.get('include_external') or '').strip()
    if division:
        query['division'] = division
    if include_external:
        query['include_external'] = include_external
    return redirect(url_for('medical_response_internal.cad_dashboard', **query))
