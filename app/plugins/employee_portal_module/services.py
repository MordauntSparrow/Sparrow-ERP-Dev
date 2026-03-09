"""
Employee Portal services: dashboard data, module links, and safe helpers.
Production-ready: defensive loading, logging, no raw secrets in logs.
"""
import logging
import re

from app.objects import get_db_connection

logger = logging.getLogger(__name__)

# Limits for dashboard lists (avoid unbounded queries)
LIMIT_MESSAGES = 50
LIMIT_TODOS = 100

# Single source of truth for portal module links (name, url, icon, plugin system_name)
# use_launch=True: dashboard link goes via /employee-portal/go/<slug> so auth is passed by token (avoids session/cookie issues)
MODULE_LINKS_CONFIG = [
    {"name": "Time & Billing", "url": "/time-billing/", "icon": "bi-clock-history", "system_name": "time_billing_module", "launch_slug": "time-billing"},
    {"name": "Work", "url": "/work/", "icon": "bi-briefcase", "system_name": "work_module", "launch_slug": None},
    {"name": "HR", "url": "/hr/", "icon": "bi-person-badge", "system_name": "hr_module", "launch_slug": None},
    {"name": "Compliance & Policies", "url": "/compliance/", "icon": "bi-shield-check", "system_name": "compliance_module", "launch_slug": None},
    {"name": "Training", "url": "/training/", "icon": "bi-mortarboard", "system_name": "training_module", "launch_slug": None},
    {"name": "Scheduling & Shifts", "url": "/scheduling/", "icon": "bi-calendar-week", "system_name": "scheduling_module", "launch_slug": None},
]


def safe_profile_picture_path(path):
    """
    Return path only if it looks safe for static serving (no path traversal, no absolute).
    Otherwise return None so the UI falls back to initials.
    """
    if not path or not isinstance(path, str):
        return None
    cleaned = path.strip()
    if ".." in cleaned or cleaned.startswith("/") or re.match(r"^[a-zA-Z]:", cleaned):
        return None
    # Allow alphanumeric, slash, hyphen, underscore (e.g. uploads/contractors/123.jpg)
    if not re.match(r"^[\w/.\-]+$", cleaned):
        return None
    return cleaned


def safe_next_url(next_param, default, request=None):
    """
    Validate redirect target to prevent open redirects.
    Only allow relative paths (e.g. /employee-portal/ or /time-billing/).
    Reject //, protocol-relative, or URLs with scheme in first segment.
    """
    if not next_param or not isinstance(next_param, str):
        return default
    s = next_param.strip()
    if not s or not s.startswith("/") or s.startswith("//"):
        return default
    parts = s.split("/")
    if len(parts) < 2 or not parts[1]:
        return default
    if ":" in parts[1]:
        return default
    return s


def get_messages(contractor_id):
    """Load messages for the dashboard. Returns list; empty on error (logged)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT id, subject, body, read_at, created_at, source_module
                FROM ep_messages
                WHERE contractor_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (contractor_id, LIMIT_MESSAGES))
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.warning("Employee portal: messages unavailable for contractor %s (run install if needed): %s", contractor_id, e)
        return []


def get_todos(contractor_id):
    """Load todos for the dashboard. Returns list; empty on error (logged)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT id, source_module, title, link_url, due_date, completed_at, created_at
                FROM ep_todos
                WHERE contractor_id = %s
                ORDER BY completed_at IS NULL DESC, due_date IS NULL ASC, due_date ASC, created_at DESC
                LIMIT %s
            """, (contractor_id, LIMIT_TODOS))
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.warning("Employee portal: todos unavailable for contractor %s (run install if needed): %s", contractor_id, e)
        return []


def get_module_links(plugin_manager):
    """
    Return list of module link dicts with 'enabled' set from plugin manifest.
    Each item: name, url, icon, system_name, enabled.
    """
    result = []
    for mod in MODULE_LINKS_CONFIG:
        item = dict(mod)
        try:
            item["enabled"] = bool(plugin_manager.is_plugin_enabled(mod["system_name"]))
        except Exception:
            item["enabled"] = False
        result.append(item)
    return result


def get_pending_counts(contractor_id):
    """Return (pending_policies, pending_hr_requests). Uses 0 on import or runtime errors."""
    pending_policies = 0
    pending_hr_requests = 0
    try:
        from app.plugins.compliance_module.services import pending_policies_count
        pending_policies = pending_policies_count(contractor_id)
    except Exception as e:
        logger.debug("Employee portal: compliance pending count unavailable: %s", e)
    try:
        from app.plugins.hr_module.services import pending_requests_count
        pending_hr_requests = pending_requests_count(contractor_id)
    except Exception as e:
        logger.debug("Employee portal: HR pending count unavailable: %s", e)
    return pending_policies, pending_hr_requests


def is_scheduling_enabled(plugin_manager):
    """True if scheduling_module is enabled (for quick actions visibility)."""
    try:
        return bool(plugin_manager.is_plugin_enabled("scheduling_module"))
    except Exception:
        return False
