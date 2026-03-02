# Inventory Control plugin routes: admin UI, JSON APIs, mobile API, audit, Socket.IO
import hashlib
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, date
from functools import wraps
from io import StringIO
import csv as csv_module

from flask import Blueprint, request, jsonify, render_template, current_app, g, redirect, url_for, flash
from flask_login import login_required, current_user

from app.objects import get_db_connection
from app import socketio
from app.auth_jwt import decode_session_token
from app.openapi_utils import register_path

from .objects import get_inventory_service
from .ocr import InventoryInvoiceService, TesseractOCRProvider, AmazonInvoiceParser

BLUEPRINT_NAME = "inventory_control_internal"
BASE_PATH = "/plugin/inventory_control"

logger = logging.getLogger("inventory_control")
logger.setLevel(logging.INFO)

# Admin/internal blueprint (all admin + API + mobile under one blueprint)
internal = Blueprint(
    "inventory_control_internal",
    __name__,
    url_prefix="/plugin/inventory_control",
    template_folder="templates",
)


@internal.before_request
def _inventory_token_auth():
    """Set g.inventory_api_user when Authorization: Bearer <token> is valid."""
    g.inventory_api_user = None
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return
    token = auth[7:].strip()
    if not token:
        return
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, role, supplier_id, customer_id, scopes FROM inventory_api_tokens WHERE token_hash = %s",
            (token_hash,),
        )
        row = cur.fetchone()
        if row:
            g.inventory_api_user = {
                "id": row["id"],
                "role": row["role"],
                "supplier_id": row.get("supplier_id"),
                "customer_id": row.get("customer_id"),
                "scopes": json.loads(row["scopes"]) if isinstance(row.get("scopes"), str) else (row.get("scopes") or []),
            }
            cur.execute("UPDATE inventory_api_tokens SET last_used_at = NOW() WHERE id = %s", (row["id"],))
            conn.commit()
    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


@internal.before_request
def _session_token_auth():
    """Set g.token_user when Authorization: Bearer <session JWT> is valid (any role)."""
    g.token_user = None
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return
    token = auth[7:].strip()
    if not token:
        return
    if getattr(g, "inventory_api_user", None):
        return
    payload = decode_session_token(token)
    if payload:
        g.token_user = {
            "id": payload["sub"],
            "username": payload["username"],
            "role": payload.get("role") or "",
        }


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
    if hasattr(value, "__float__") and hasattr(value, "as_integer_ratio"):  # Decimal
        return float(value)
    return value


def _jsonify_safe(payload, status=200):
    return jsonify(_json_compatible(payload)), status


def _emit_inventory_event(event_type: str, payload: dict):
    try:
        socketio.emit("inventory_event", {"type": event_type, **payload}, broadcast=True)
    except Exception as e:
        logger.warning("Socket.IO emit failed: %s", e)


def _coerce_int_user_id(value):
    """
    inventory_audit.user_id is INT in current schema. Core users often have UUID ids.
    Coerce only numeric ids; otherwise return None (UUID can be stored in details).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return None
    return None


def log_audit(
    user,
    action: str,
    item_id=None,
    location_id=None,
    batch_id=None,
    transaction_id=None,
    details=None,
):
    try:
        extra = {"user": str(user), "action": action, "module": "inventory_control"}
        if item_id is not None:
            extra["item_id"] = item_id
        if location_id is not None:
            extra["location_id"] = location_id
        if batch_id is not None:
            extra["batch_id"] = batch_id
        if transaction_id is not None:
            extra["transaction_id"] = transaction_id
        if details is not None:
            extra["details"] = details
        audit_logger = getattr(current_app, "audit_logger", None)
        if audit_logger:
            audit_logger.info(action, extra={"extra": extra})
        else:
            logger.info("AUDIT: %s", json.dumps(extra, default=str))

        # Optional DB-level audit trail. Safe with UUID users: store numeric user_id when possible
        # and always include the raw id string in details for traceability.
        if os.environ.get("INVENTORY_AUDIT_TO_DB", "false").lower() == "true":
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                try:
                    details_payload = extra.get("details")
                    if isinstance(details_payload, dict):
                        details_payload = {**details_payload, "user": str(user)}
                    else:
                        details_payload = {"details": details_payload, "user": str(user)}
                    cur.execute(
                        """
                        INSERT INTO inventory_audit (user_id, action, item_id, location_id, batch_id, transaction_id, details)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            _coerce_int_user_id(user),
                            action,
                            item_id,
                            location_id,
                            batch_id,
                            transaction_id,
                            json.dumps(details_payload, default=str),
                        ),
                    )
                    conn.commit()
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception:
                # Never break the main request due to audit insert failure
                logger.exception("DB audit insert failed")
    except Exception:
        logger.exception("Audit logging failed")


def _is_admin():
    """True if current user or Bearer token user has admin role (session or g.token_user)."""
    token_user = getattr(g, "token_user", None)
    if token_user and token_user.get("role") in ("admin", "superuser"):
        return True
    return getattr(current_user, "role", None) in ("admin", "superuser")


def _permission_inventory():
    """Allow access to inventory module (admin role only for now)."""
    return _is_admin()


def _require_admin():
    """Require admin role for write operations."""
    return _is_admin()


def _allow_token_role(role: str) -> bool:
    """True if request is authenticated via session (admin) or Bearer token with given role."""
    if getattr(current_user, "is_authenticated", False) and _is_admin():
        return True
    api_user = getattr(g, "inventory_api_user", None)
    return api_user and api_user.get("role") == role


def admin_required(f):
    """Decorator: require admin role (aligns with core admin_only)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _is_admin():
            flash("Access denied: Admins only.", "danger")
            return redirect(url_for("routes.dashboard"))
        return f(*args, **kwargs)
    return wrapper


def api_authenticated_required(f):
    """Decorator for /api/* routes: allow any valid Bearer token or session. Return 401 JSON when unauthenticated."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if getattr(g, "token_user", None):
            return f(*args, **kwargs)
        if getattr(current_user, "is_authenticated", False):
            return f(*args, **kwargs)
        return _jsonify_safe({"error": "Authentication required"}, 401)
    return wrapper


def api_admin_required(f):
    """Decorator for /api/* routes: allow Bearer token (admin/superuser) or session (admin). Return 401 JSON when unauthenticated."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token_user = getattr(g, "token_user", None)
        if token_user and token_user.get("role") in ("admin", "superuser"):
            return f(*args, **kwargs)
        if getattr(current_user, "is_authenticated", False) and _is_admin():
            return f(*args, **kwargs)
        return _jsonify_safe({"error": "Authentication required"}, 401)
    return wrapper


def api_authenticated_required(f):
    """Decorator for /api/* routes: allow any valid Bearer token or session. Return 401 JSON when unauthenticated."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if getattr(g, "token_user", None):
            return f(*args, **kwargs)
        if getattr(current_user, "is_authenticated", False):
            return f(*args, **kwargs)
        return _jsonify_safe({"error": "Authentication required"}, 401)
    return wrapper


def _effective_user_for_audit():
    """Username or id for audit log: session user or token user."""
    if getattr(g, "token_user", None):
        return g.token_user.get("username") or f"token:{g.token_user.get('id')}"
    if getattr(current_user, "is_authenticated", False):
        return getattr(current_user, "username", None) or str(getattr(current_user, "id", ""))
    return "anonymous"


def _effective_user_id():
    """User id for audit/DB when using session or Bearer token."""
    if getattr(g, "token_user", None):
        return g.token_user.get("id")
    if getattr(current_user, "is_authenticated", False):
        return getattr(current_user, "id", None)
    return None


# ---------------------------------------------------------------------------
# Dashboard & UI pages
# ---------------------------------------------------------------------------

@internal.route("/")
@internal.route("/index")
@login_required
def dashboard():
    """Plugin index/landing: admin role required (aligned with rest of system)."""
    if not _permission_inventory():
        flash("Access denied: You do not have permission to access Inventory Control.", "danger")
        return redirect(url_for("routes.dashboard"))
    svc = get_inventory_service()
    health = svc.health_check()
    metrics = svc.get_dashboard_metrics()
    return render_template(
        "admin/inventory_dashboard.html",
        health=health,
        metrics=metrics,
    )


@internal.route("/items")
@login_required
@admin_required
def items_page():
    return render_template("admin/items.html")


@internal.route("/categories")
@login_required
@admin_required
def categories_page():
    return render_template("admin/categories.html")


@internal.route("/locations")
@login_required
@admin_required
def locations_page():
    return render_template("admin/locations.html")


@internal.route("/batches")
@login_required
@admin_required
def batches_page():
    return render_template("admin/batches.html")


@internal.route("/repack")
@login_required
@admin_required
def repack_page():
    return render_template("admin/repack.html")


@internal.route("/transactions")
@login_required
@admin_required
def transactions_page():
    return render_template("admin/transactions.html")


@internal.route("/invoices")
@login_required
@admin_required
def invoices_page():
    return render_template("admin/invoices.html")


@internal.route("/analytics")
@login_required
@admin_required
def analytics_page():
    return render_template("admin/analytics.html")


# ---------------------------------------------------------------------------
# API: health & dashboard
# ---------------------------------------------------------------------------

@internal.route("/api/health")
@api_authenticated_required
def api_health():
    svc = get_inventory_service()
    out = svc.health_check()
    if not isinstance(out, dict):
        out = {"status": "ok"} if out else {"status": "error"}
    token_user = getattr(g, "token_user", None)
    if token_user:
        out["user"] = {"id": token_user.get("id"), "username": token_user.get("username"), "role": token_user.get("role")}
    elif getattr(current_user, "is_authenticated", False):
        out["user"] = {"id": getattr(current_user, "id", None), "username": getattr(current_user, "username", ""), "role": getattr(current_user, "role", "")}
    return _jsonify_safe(out)


@internal.route("/api/dashboard")
@api_admin_required
def api_dashboard():
    svc = get_inventory_service()
    metrics = svc.get_dashboard_metrics()
    metrics["movement_summary"] = svc.get_movement_summary()
    return _jsonify_safe(metrics)


# ---------------------------------------------------------------------------
# API: categories CRUD
# ---------------------------------------------------------------------------

@internal.route("/api/categories", methods=["GET"])
@api_admin_required
def api_categories_list():
    svc = get_inventory_service()
    parent_id = request.args.get("parent_id", type=int)
    categories = svc.list_categories(parent_id=parent_id)
    return _jsonify_safe({"categories": categories})


@internal.route("/api/categories", methods=["POST"])
@api_admin_required
def api_categories_create():
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    if not data.get("name"):
        return _jsonify_safe({"error": "name required"}, 400)
    svc = get_inventory_service()
    try:
        cat_id = svc.create_category(data)
        return _jsonify_safe({"id": cat_id}, 201)
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


@internal.route("/api/categories/<int:category_id>", methods=["GET"])
@api_admin_required
def api_categories_get(category_id):
    svc = get_inventory_service()
    cat = svc.get_category(category_id)
    if not cat:
        return _jsonify_safe({"error": "Not found"}, 404)
    return _jsonify_safe(cat)


@internal.route("/api/categories/<int:category_id>", methods=["PUT", "PATCH"])
@api_admin_required
def api_categories_update(category_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    svc = get_inventory_service()
    if not svc.get_category(category_id):
        return _jsonify_safe({"error": "Not found"}, 404)
    try:
        svc.update_category(category_id, data)
        return _jsonify_safe({"ok": True})
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


@internal.route("/api/categories/<int:category_id>", methods=["DELETE"])
@api_admin_required
def api_categories_delete(category_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    svc = get_inventory_service()
    if not svc.get_category(category_id):
        return _jsonify_safe({"error": "Not found"}, 404)
    svc.delete_category(category_id)
    return _jsonify_safe({"ok": True})


# ---------------------------------------------------------------------------
# API: items CRUD
# ---------------------------------------------------------------------------

@internal.route("/api/items", methods=["GET"])
@api_authenticated_required
def api_items_list():
    svc = get_inventory_service()
    skip = request.args.get("skip", type=int) or 0
    limit = min(request.args.get("limit", type=int) or 50, 200)
    search = request.args.get("search", "").strip() or None
    category = request.args.get("category", "").strip() or None
    category_id = request.args.get("category_id", type=int)
    is_active = request.args.get("is_active")
    if is_active is not None:
        is_active = str(is_active).lower() in ("1", "true", "yes")
    items = svc.list_items(skip=skip, limit=limit, search=search, category=category, category_id=category_id, is_active=is_active)
    return _jsonify_safe({"items": items})


@internal.route("/api/items", methods=["POST"])
@api_admin_required
def api_items_create():
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    if not data.get("sku") or not data.get("name"):
        return _jsonify_safe({"error": "sku and name required"}, 400)
    svc = get_inventory_service()
    try:
        item_id = svc.create_item(data)
        log_audit(_effective_user_id(), "inventory_item_create", item_id=item_id, details=data)
        return _jsonify_safe({"id": item_id}, 201)
    except Exception as e:
        logger.exception("create item")
        return _jsonify_safe({"error": str(e)}, 400)


@internal.route("/api/items/<int:item_id>", methods=["GET"])
@api_authenticated_required
def api_items_get(item_id):
    svc = get_inventory_service()
    item = svc.get_item(item_id)
    if not item:
        return _jsonify_safe({"error": "Not found"}, 404)
    return _jsonify_safe(item)


@internal.route("/api/items/<int:item_id>", methods=["PUT", "PATCH"])
@api_admin_required
def api_items_update(item_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    svc = get_inventory_service()
    if not svc.get_item(item_id):
        return _jsonify_safe({"error": "Not found"}, 404)
    try:
        svc.update_item(item_id, data)
        log_audit(_effective_user_id(), "inventory_item_update", item_id=item_id, details=data)
        return _jsonify_safe({"ok": True})
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


@internal.route("/api/items/<int:item_id>", methods=["DELETE"])
@api_admin_required
def api_items_archive(item_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    svc = get_inventory_service()
    if not svc.get_item(item_id):
        return _jsonify_safe({"error": "Not found"}, 404)
    svc.archive_item(item_id)
    log_audit(_effective_user_id(), "inventory_item_archive", item_id=item_id)
    return _jsonify_safe({"ok": True})


# ---------------------------------------------------------------------------
# API: locations CRUD
# ---------------------------------------------------------------------------

@internal.route("/api/locations", methods=["GET"])
@api_admin_required
def api_locations_list():
    svc = get_inventory_service()
    parent_id = request.args.get("parent_id", type=int)
    locations = svc.list_locations(parent_id=parent_id)
    return _jsonify_safe({"locations": locations})


@internal.route("/api/locations", methods=["POST"])
@api_admin_required
def api_locations_create():
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    if not data.get("name") or not data.get("code"):
        return _jsonify_safe({"error": "name and code required"}, 400)
    svc = get_inventory_service()
    try:
        loc_id = svc.create_location(data)
        log_audit(_effective_user_id(), "inventory_location_create", location_id=loc_id, details=data)
        return _jsonify_safe({"id": loc_id}, 201)
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


@internal.route("/api/locations/<int:location_id>", methods=["GET"])
@api_admin_required
def api_locations_get(location_id):
    svc = get_inventory_service()
    loc = svc.get_location(location_id)
    if not loc:
        return _jsonify_safe({"error": "Not found"}, 404)
    return _jsonify_safe(loc)


@internal.route("/api/locations/<int:location_id>", methods=["PUT", "PATCH"])
@api_admin_required
def api_locations_update(location_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    svc = get_inventory_service()
    if not svc.get_location(location_id):
        return _jsonify_safe({"error": "Not found"}, 404)
    svc.update_location(location_id, data)
    log_audit(_effective_user_id(), "inventory_location_update", location_id=location_id, details=data)
    return _jsonify_safe({"ok": True})


# ---------------------------------------------------------------------------
# API: batches CRUD
# ---------------------------------------------------------------------------

@internal.route("/api/batches", methods=["GET"])
@api_admin_required
def api_batches_list():
    svc = get_inventory_service()
    item_id = request.args.get("item_id", type=int)
    limit = request.args.get("limit", type=int) or 100
    batches = svc.list_batches(item_id=item_id, limit=limit)
    return _jsonify_safe({"batches": batches})


@internal.route("/api/batches", methods=["POST"])
@api_admin_required
def api_batches_create():
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    if not data.get("item_id"):
        return _jsonify_safe({"error": "item_id required"}, 400)
    svc = get_inventory_service()
    try:
        batch_id = svc.create_batch(data)
        log_audit(_effective_user_id(), "inventory_batch_create", batch_id=batch_id, details=data)
        return _jsonify_safe({"id": batch_id}, 201)
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


@internal.route("/api/batches/<int:batch_id>", methods=["GET"])
@api_admin_required
def api_batches_get(batch_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM inventory_batches WHERE id = %s", (batch_id,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        return _jsonify_safe({"error": "Not found"}, 404)
    return _jsonify_safe(row)


@internal.route("/api/batches/<int:batch_id>", methods=["PUT", "PATCH"])
@api_admin_required
def api_batches_update(batch_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    svc = get_inventory_service()
    try:
        svc.update_batch(batch_id, data)
        log_audit(_effective_user_id(), "inventory_batch_update", batch_id=batch_id, details=data)
        return _jsonify_safe({"ok": True})
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


# ---------------------------------------------------------------------------
# API: transactions
# ---------------------------------------------------------------------------

@internal.route("/api/transactions", methods=["GET"])
@api_admin_required
def api_transactions_list():
    svc = get_inventory_service()
    item_id = request.args.get("item_id", type=int)
    location_id = request.args.get("location_id", type=int)
    transaction_type = request.args.get("transaction_type", "").strip() or None
    from_date = request.args.get("from_date", "").strip() or None
    to_date = request.args.get("to_date", "").strip() or None
    skip = request.args.get("skip", type=int) or 0
    limit = min(request.args.get("limit", type=int) or 100, 500)
    rows = svc.list_transactions(
        item_id=item_id,
        location_id=location_id,
        transaction_type=transaction_type,
        from_date=from_date,
        to_date=to_date,
        skip=skip,
        limit=limit,
    )
    return _jsonify_safe({"transactions": rows})


@internal.route("/api/transactions", methods=["POST"])
@api_admin_required
def api_transactions_create():
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    item_id = data.get("item_id")
    location_id = data.get("location_id")
    quantity = data.get("quantity")
    transaction_type = data.get("transaction_type", "in")
    if item_id is None or location_id is None or quantity is None:
        return _jsonify_safe({"error": "item_id, location_id, quantity required"}, 400)
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        return _jsonify_safe({"error": "quantity must be numeric"}, 400)
    svc = get_inventory_service()
    try:
        result = svc.record_transaction(
            item_id=int(item_id),
            location_id=int(location_id),
            quantity=quantity,
            transaction_type=transaction_type,
            batch_id=data.get("batch_id"),
            unit_cost=data.get("unit_cost"),
            reference_type=data.get("reference_type"),
            reference_id=data.get("reference_id"),
            performed_by_user_id=_effective_user_id(),
            metadata=data.get("metadata"),
            client_action_id=data.get("client_action_id"),
            weight=data.get("weight"),
            weight_uom=data.get("weight_uom"),
            uom=data.get("uom"),
        )
        log_audit(
            _effective_user_id(),
            "inventory_transaction",
            item_id=item_id,
            location_id=location_id,
            transaction_id=result.get("transaction_id"),
            details=result,
        )
        _emit_inventory_event(
            "stock_changed",
            {
                "item_id": item_id,
                "location_id": location_id,
                "transaction_type": transaction_type,
                "quantity": quantity,
                "stock": result.get("stock"),
            },
        )
        return _jsonify_safe(result, 201)
    except ValueError as e:
        return _jsonify_safe({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("record_transaction")
        return _jsonify_safe({"error": str(e)}, 500)


@internal.route("/api/repack", methods=["POST"])
@api_admin_required
def api_repack():
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    source_batch_id = data.get("source_batch_id")
    location_id = data.get("location_id")
    outputs = data.get("outputs") or []
    if not source_batch_id or not location_id or not outputs:
        return _jsonify_safe({"error": "source_batch_id, location_id, outputs required"}, 400)
    svc = get_inventory_service()
    try:
        result = svc.repack(
            source_batch_id=int(source_batch_id),
            location_id=int(location_id),
            outputs=outputs,
            performed_by_user_id=_effective_user_id(),
        )
        log_audit(
            _effective_user_id(),
            "inventory_repack",
            batch_id=source_batch_id,
            location_id=location_id,
            details=result,
        )
        _emit_inventory_event("stock_changed", {"repack_id": result.get("repack_id")})
        return _jsonify_safe(result, 201)
    except ValueError as e:
        return _jsonify_safe({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("repack")
        return _jsonify_safe({"error": str(e)}, 500)


@internal.route("/api/picking/suggest")
@api_admin_required
def api_picking_suggest():
    """FEFO: suggest batches to pick for outbound, ordered by expiry soonest first."""
    item_id = request.args.get("item_id", type=int)
    location_id = request.args.get("location_id", type=int)
    quantity = request.args.get("quantity", type=float)
    if not item_id or not location_id or quantity is None or quantity <= 0:
        return _jsonify_safe({"error": "item_id, location_id, quantity (positive) required"}, 400)
    svc = get_inventory_service()
    suggestions = svc.get_picking_suggestions_fefo(item_id=item_id, location_id=location_id, quantity=quantity)
    return _jsonify_safe({"suggestions": suggestions})


@internal.route("/api/transactions/<int:tx_id>/rollback", methods=["POST"])
@api_admin_required
def api_transactions_rollback(tx_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    svc = get_inventory_service()
    try:
        svc.rollback_transaction(tx_id)
        log_audit(_effective_user_id(), "inventory_transaction_rollback", transaction_id=tx_id)
        _emit_inventory_event("stock_changed", {"transaction_id": tx_id, "rollback": True})
        return _jsonify_safe({"ok": True})
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


# ---------------------------------------------------------------------------
# API: analytics (reports + activity)
# ---------------------------------------------------------------------------

@internal.route("/api/analytics/stock_levels")
@api_admin_required
def api_analytics_stock_levels():
    """Stock levels report with item/location names. Optional item_id, location_id, limit."""
    item_id = request.args.get("item_id", type=int)
    location_id = request.args.get("location_id", type=int)
    limit = min(request.args.get("limit", type=int) or 500, 5000)
    skip = request.args.get("skip", type=int) or 0
    svc = get_inventory_service()
    report = svc.list_stock_levels_report(item_id=item_id, location_id=location_id, limit=limit, skip=skip)
    return _jsonify_safe({"report": _json_compatible(report), "count": len(report)})


@internal.route("/api/analytics/movers")
@api_admin_required
def api_analytics_movers():
    """Fast and slow movers by transaction count and quantity in the last N days."""
    days = min(request.args.get("days", type=int) or 30, 365)
    top_n = min(request.args.get("top", type=int) or 20, 100)
    svc = get_inventory_service()
    data = svc.get_analytics_movers(days=days, top_n=top_n)
    return _jsonify_safe(data)


@internal.route("/api/analytics/activity")
@api_admin_required
def api_analytics_activity():
    """Recent transaction activity with item and location names."""
    days = min(request.args.get("days", type=int) or 7, 90)
    limit = min(request.args.get("limit", type=int) or 200, 1000)
    svc = get_inventory_service()
    rows = svc.get_analytics_activity(days=days, limit=limit)
    return _jsonify_safe({"activity": rows})


@internal.route("/api/analytics/suppliers")
@api_admin_required
def api_analytics_suppliers():
    """Supplier list for reporting (optional performance metrics later)."""
    svc = get_inventory_service()
    suppliers = svc.list_suppliers(limit=500)
    return _jsonify_safe({"suppliers": _json_compatible(suppliers)})


# ---------------------------------------------------------------------------
# API: invoices (upload, get, apply)
# ---------------------------------------------------------------------------

def _invoice_upload_dir():
    base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "data", "invoices")
    os.makedirs(d, exist_ok=True)
    return d


@internal.route("/api/invoices/upload", methods=["POST"])
@api_admin_required
def api_invoices_upload():
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    f = request.files.get("file")
    if not f:
        return _jsonify_safe({"error": "file required"}, 400)
    ext = (os.path.splitext(f.filename or "")[1] or "").lower()
    allowed = (".png", ".jpg", ".jpeg", ".gif", ".tiff", ".bmp", ".pdf", ".docx")
    if ext not in allowed:
        return _jsonify_safe({"error": "File type not allowed. Use image (png, jpg, etc.), PDF, or DOCX."}, 400)
    path = os.path.join(_invoice_upload_dir(), f"{uuid.uuid4().hex}{ext}")
    f.save(path)
    source = (request.form.get("source") or "amazon").strip() or "amazon"
    supplier_id = request.form.get("supplier_id", type=int)
    try:
        invoice_svc = InventoryInvoiceService(
            ocr_provider=TesseractOCRProvider(),
            parser=AmazonInvoiceParser(),
        )
        parsed = invoice_svc.parse_file(path, source=source)
    except Exception as e:
        logger.exception("invoice parse")
        return _jsonify_safe({"error": f"OCR/parse failed: {e}"}, 400)
    svc = get_inventory_service()
    invoice_id = svc.create_invoice_record(
        supplier_id=supplier_id or None,
        external_source=source,
        invoice_number=(parsed.invoice_number or "").strip() or None,
        invoice_date=parsed.invoice_date,
        currency=parsed.currency,
        status="parsed",
        raw_file_path=path,
        parsed_payload={
            "invoice_number": parsed.invoice_number,
            "invoice_date": parsed.invoice_date,
            "supplier_name": parsed.supplier_name,
            "external_source": source,
            "lines_count": len(parsed.lines),
        },
    )
    for line in parsed.lines:
        svc.add_invoice_line(
            invoice_id=invoice_id,
            sku=line.sku,
            description=line.description,
            quantity=line.quantity,
            unit_price=line.unit_price,
            line_total=line.line_total,
            external_item_ref=line.external_item_ref,
            match_status="unmapped",
        )
    log_audit(_effective_user_id(), "inventory_invoice_upload", details={"invoice_id": invoice_id})
    _emit_inventory_event("invoice_parsed", {"invoice_id": invoice_id})
    return _jsonify_safe({"invoice_id": invoice_id, "lines": len(parsed.lines)}, 201)


@internal.route("/api/invoices", methods=["GET"])
@api_admin_required
def api_invoices_list():
    """List invoices for admin UI."""
    svc = get_inventory_service()
    limit = min(int(request.args.get("limit", 100)), 500)
    skip = int(request.args.get("skip", 0))
    status = request.args.get("status")
    invoices = svc.list_invoices(limit=limit, skip=skip, status=status)
    return _jsonify_safe({"invoices": _json_compatible(invoices)})


@internal.route("/api/invoices/<int:invoice_id>", methods=["GET"])
@api_admin_required
def api_invoices_get(invoice_id):
    svc = get_inventory_service()
    inv = svc.get_invoice(invoice_id)
    if not inv:
        return _jsonify_safe({"error": "Not found"}, 404)
    lines = svc.get_invoice_lines(invoice_id)
    inv["lines"] = _json_compatible(lines)
    if isinstance(inv.get("parsed_payload"), str):
        try:
            inv["parsed_payload"] = json.loads(inv["parsed_payload"])
        except Exception:
            pass
    return _jsonify_safe(inv)


@internal.route("/api/invoices/<int:invoice_id>", methods=["PUT", "PATCH"])
@api_admin_required
def api_invoices_update(invoice_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    svc = get_inventory_service()
    if not svc.get_invoice(invoice_id):
        return _jsonify_safe({"error": "Not found"}, 404)
    updates = {}
    for key in ("supplier_id", "external_source", "invoice_number", "invoice_date", "total_amount", "currency"):
        if key in data:
            updates[key] = data[key]
    if updates:
        svc.update_invoice(invoice_id, **updates)
    return _jsonify_safe({"ok": True})


@internal.route("/api/invoices/<int:invoice_id>/lines/<int:line_id>", methods=["PUT", "PATCH"])
@api_admin_required
def api_invoices_line_update(invoice_id, line_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    svc = get_inventory_service()
    lines = svc.get_invoice_lines(invoice_id)
    if not any(l.get("id") == line_id for l in lines):
        return _jsonify_safe({"error": "Line not found"}, 404)
    updates = {}
    for key in ("sku", "description", "quantity", "unit_price", "line_total"):
        if key in data:
            updates[key] = data[key]
    if updates:
        svc.update_invoice_line(line_id, **updates)
    return _jsonify_safe({"ok": True})


@internal.route("/api/invoices/<int:invoice_id>/lines/<int:line_id>/match", methods=["PUT", "PATCH"])
@api_admin_required
def api_invoices_line_match(invoice_id, line_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    item_id = data.get("item_id")
    svc = get_inventory_service()
    svc.update_invoice_line_item(line_id, item_id)
    return _jsonify_safe({"ok": True})


@internal.route("/api/invoices/<int:invoice_id>/apply", methods=["POST"])
@api_admin_required
def api_invoices_apply(invoice_id):
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    location_id = data.get("location_id")
    if not location_id:
        return _jsonify_safe({"error": "location_id required"}, 400)
    svc = get_inventory_service()
    if not svc.get_invoice(invoice_id):
        return _jsonify_safe({"error": "Not found"}, 404)
    result = svc.apply_invoice_to_stock(
        invoice_id,
        int(location_id),
        performed_by_user_id=_effective_user_id(),
    )
    log_audit(_effective_user_id(), "inventory_invoice_apply", details={"invoice_id": invoice_id, **result})
    _emit_inventory_event("stock_changed", {"invoice_id": invoice_id})
    return _jsonify_safe(result)


# ---------------------------------------------------------------------------
# Mobile API: scan in/out/adjust, search, stock
# ---------------------------------------------------------------------------

@internal.route("/api/mobile/scan/in", methods=["POST"])
@api_authenticated_required
def api_mobile_scan_in():
    data = request.get_json() or {}
    barcode_or_sku = data.get("barcode") or data.get("sku")
    location_id = data.get("location_id")
    quantity = data.get("quantity", 1)
    if not barcode_or_sku or not location_id:
        return _jsonify_safe({"error": "barcode/sku and location_id required"}, 400)
    svc = get_inventory_service()
    item = svc.find_item_by_sku_or_barcode(str(barcode_or_sku))
    if not item:
        return _jsonify_safe({"error": "item not found"}, 404)
    try:
        result = svc.record_transaction(
            item_id=item["id"],
            location_id=int(location_id),
            quantity=float(quantity),
            transaction_type="in",
            performed_by_user_id=_effective_user_id(),
            client_action_id=data.get("client_action_id"),
            weight=data.get("weight"),
            weight_uom=data.get("weight_uom"),
            uom=data.get("uom"),
        )
        _emit_inventory_event("stock_changed", {"item_id": item["id"], "location_id": location_id, "quantity": quantity})
        return _jsonify_safe(result, 201)
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


@internal.route("/api/mobile/scan/out", methods=["POST"])
@api_authenticated_required
def api_mobile_scan_out():
    data = request.get_json() or {}
    barcode_or_sku = data.get("barcode") or data.get("sku")
    location_id = data.get("location_id")
    quantity = data.get("quantity", 1)
    if not barcode_or_sku or not location_id:
        return _jsonify_safe({"error": "barcode/sku and location_id required"}, 400)
    svc = get_inventory_service()
    item = svc.find_item_by_sku_or_barcode(str(barcode_or_sku))
    if not item:
        return _jsonify_safe({"error": "item not found"}, 404)
    try:
        result = svc.record_transaction(
            item_id=item["id"],
            location_id=int(location_id),
            quantity=float(quantity),
            transaction_type="out",
            performed_by_user_id=_effective_user_id(),
            client_action_id=data.get("client_action_id"),
            weight=data.get("weight"),
            weight_uom=data.get("weight_uom"),
            uom=data.get("uom"),
        )
        _emit_inventory_event("stock_changed", {"item_id": item["id"], "location_id": location_id, "quantity": -float(quantity)})
        return _jsonify_safe(result, 201)
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


@internal.route("/api/mobile/scan/adjust", methods=["POST"])
@api_authenticated_required
def api_mobile_scan_adjust():
    if not _require_admin():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    barcode_or_sku = data.get("barcode") or data.get("sku")
    location_id = data.get("location_id")
    quantity = data.get("quantity")
    if not barcode_or_sku or not location_id or quantity is None:
        return _jsonify_safe({"error": "barcode/sku, location_id, quantity required"}, 400)
    svc = get_inventory_service()
    item = svc.find_item_by_sku_or_barcode(str(barcode_or_sku))
    if not item:
        return _jsonify_safe({"error": "item not found"}, 404)
    try:
        result = svc.record_transaction(
            item_id=item["id"],
            location_id=int(location_id),
            quantity=float(quantity),
            transaction_type="adjustment",
            performed_by_user_id=_effective_user_id(),
            client_action_id=data.get("client_action_id"),
            weight=data.get("weight"),
            weight_uom=data.get("weight_uom"),
            uom=data.get("uom"),
        )
        _emit_inventory_event("stock_changed", {"item_id": item["id"], "location_id": location_id})
        return _jsonify_safe(result, 201)
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


@internal.route("/api/mobile/items/search")
@api_authenticated_required
def api_mobile_items_search():
    q = request.args.get("q", "").strip()
    if not q:
        return _jsonify_safe({"items": []})
    svc = get_inventory_service()
    items = svc.list_items(limit=20, search=q, is_active=True)
    return _jsonify_safe({"items": items})


@internal.route("/api/mobile/items/<int:item_id>/stock")
@api_authenticated_required
def api_mobile_items_stock(item_id):
    svc = get_inventory_service()
    levels = svc.list_stock_levels(item_id)
    return _jsonify_safe({"item_id": item_id, "stock_levels": levels})


@internal.route("/api/mobile/bulk/actions", methods=["POST"])
@api_authenticated_required
def api_mobile_bulk_actions():
    data = request.get_json() or {}
    actions = data.get("actions") or []
    results = []
    svc = get_inventory_service()
    for a in actions:
        client_action_id = a.get("client_action_id")
        action_type = a.get("type")
        try:
            if action_type == "in":
                item = svc.find_item_by_sku_or_barcode(a.get("barcode") or a.get("sku") or "")
                if not item:
                    results.append({"client_action_id": client_action_id, "error": "item not found"})
                    continue
                r = svc.record_transaction(
                    item_id=item["id"],
                    location_id=int(a["location_id"]),
                    quantity=float(a.get("quantity", 1)),
                    transaction_type="in",
                    performed_by_user_id=_effective_user_id(),
                    client_action_id=client_action_id,
                    weight=a.get("weight"),
                    weight_uom=a.get("weight_uom"),
                    uom=a.get("uom"),
                )
                results.append({"client_action_id": client_action_id, "transaction_id": r.get("transaction_id")})
            elif action_type == "out":
                item = svc.find_item_by_sku_or_barcode(a.get("barcode") or a.get("sku") or "")
                if not item:
                    results.append({"client_action_id": client_action_id, "error": "item not found"})
                    continue
                r = svc.record_transaction(
                    item_id=item["id"],
                    location_id=int(a["location_id"]),
                    quantity=float(a.get("quantity", 1)),
                    transaction_type="out",
                    performed_by_user_id=_effective_user_id(),
                    client_action_id=client_action_id,
                    weight=a.get("weight"),
                    weight_uom=a.get("weight_uom"),
                    uom=a.get("uom"),
                )
                results.append({"client_action_id": client_action_id, "transaction_id": r.get("transaction_id")})
            else:
                results.append({"client_action_id": client_action_id, "error": f"unknown type {action_type}"})
        except Exception as e:
            results.append({"client_action_id": client_action_id, "error": str(e)})
    return _jsonify_safe({"results": results})


# ---------------------------------------------------------------------------
# API: Token-based auth (create/list/revoke) — admin only
# ---------------------------------------------------------------------------

@internal.route("/api/tokens", methods=["POST"])
@api_admin_required
def api_tokens_create():
    """Create an API token for external supplier access. Raw token returned only once."""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip() or None
    role = (data.get("role") or "supplier").strip().lower()
    if role != "supplier":
        return _jsonify_safe({"error": "role must be 'supplier' (customer-facing features are in Sales module)"}, 400)
    supplier_id = data.get("supplier_id")
    customer_id = None  # reserved for future Sales module; not used in Inventory
    scopes = data.get("scopes")
    if isinstance(scopes, list):
        scopes = json.dumps(scopes)
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO inventory_api_tokens (token_hash, name, role, supplier_id, customer_id, scopes)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (token_hash, name, role, supplier_id, customer_id, scopes),
        )
        conn.commit()
        token_id = cur.lastrowid
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()
    log_audit(_effective_user_id(), "inventory_token_create", details={"token_id": token_id, "role": role})
    return _jsonify_safe({
        "id": token_id,
        "name": name,
        "role": role,
        "supplier_id": supplier_id,
        "customer_id": customer_id,
        "token": raw_token,
        "message": "Store the token securely; it will not be shown again.",
    }, 201)


@internal.route("/api/tokens", methods=["GET"])
@api_admin_required
def api_tokens_list():
    """List API tokens (no secret value)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, name, role, supplier_id, customer_id, scopes, created_at, last_used_at FROM inventory_api_tokens ORDER BY id DESC"
        )
        rows = cur.fetchall() or []
        for r in rows:
            if isinstance(r.get("scopes"), str):
                try:
                    r["scopes"] = json.loads(r["scopes"]) if r["scopes"] else []
                except Exception:
                    r["scopes"] = []
        return _jsonify_safe({"tokens": rows})
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


@internal.route("/api/tokens/<int:token_id>", methods=["DELETE"])
@api_admin_required
def api_tokens_revoke(token_id):
    """Revoke an API token."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM inventory_api_tokens WHERE id = %s", (token_id,))
        conn.commit()
        if cur.rowcount == 0:
            return _jsonify_safe({"error": "Not found"}, 404)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()
    log_audit(_effective_user_id(), "inventory_token_revoke", details={"token_id": token_id})
    return _jsonify_safe({"ok": True})


# ---------------------------------------------------------------------------
# API: Supplier-scoped (Bearer token role=supplier or session with inventory)
# ---------------------------------------------------------------------------

@internal.route("/api/supplier/items", methods=["GET"])
def api_supplier_items():
    """Limited item list for suppliers (id, sku, name, uom)."""
    if not _allow_token_role("supplier"):
        return _jsonify_safe({"error": "Forbidden"}, 403)
    svc = get_inventory_service()
    limit = min(int(request.args.get("limit", 50)), 200)
    skip = int(request.args.get("skip", 0))
    items = svc.list_items(skip=skip, limit=limit, is_active=True)
    out = []
    for row in items:
        out.append({
            "id": row["id"],
            "sku": row.get("sku"),
            "name": row.get("name"),
            "uom": row.get("uom"),
        })
    return _jsonify_safe({"items": out})


@internal.route("/api/supplier/invoices", methods=["GET"])
def api_supplier_invoices():
    """List invoices for the supplier (when token has supplier_id)."""
    if not _allow_token_role("supplier"):
        return _jsonify_safe({"error": "Forbidden"}, 403)
    api_user = getattr(g, "inventory_api_user", None)
    supplier_id = api_user.get("supplier_id") if api_user else None
    if not supplier_id and getattr(current_user, "is_authenticated", False):
        supplier_id = request.args.get("supplier_id", type=int)
    svc = get_inventory_service()
    limit = min(int(request.args.get("limit", 50)), 100)
    skip = int(request.args.get("skip", 0))
    invoices = svc.list_invoices(supplier_id=supplier_id, limit=limit, skip=skip)
    return _jsonify_safe({"invoices": _json_compatible(invoices)})


@internal.route("/api/supplier/receipts/confirm", methods=["POST"])
def api_supplier_receipts_confirm():
    """Confirm receipt: apply invoice to stock (supplier confirms)."""
    if not _allow_token_role("supplier"):
        return _jsonify_safe({"error": "Forbidden"}, 403)
    data = request.get_json() or {}
    invoice_id = data.get("invoice_id")
    location_id = data.get("location_id")
    if not invoice_id or not location_id:
        return _jsonify_safe({"error": "invoice_id and location_id required"}, 400)
    svc = get_inventory_service()
    inv = svc.get_invoice(int(invoice_id))
    if not inv:
        return _jsonify_safe({"error": "Invoice not found"}, 404)
    api_user = getattr(g, "inventory_api_user", None)
    if api_user and api_user.get("supplier_id") is not None and inv.get("supplier_id") != api_user.get("supplier_id"):
        return _jsonify_safe({"error": "Forbidden: invoice not for this supplier"}, 403)
    performed_by = _effective_user_id()
    try:
        result = svc.apply_invoice_to_stock(
            int(invoice_id), int(location_id), performed_by_user_id=performed_by
        )
        return _jsonify_safe(result)
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


@internal.route("/api/supplier/batches", methods=["GET"])
def api_supplier_batches():
    """List batches/lots supplied by this supplier (token must have supplier_id)."""
    if not _allow_token_role("supplier"):
        return _jsonify_safe({"error": "Forbidden"}, 403)
    api_user = getattr(g, "inventory_api_user", None)
    supplier_id = api_user.get("supplier_id") if api_user else None
    if not supplier_id and getattr(current_user, "is_authenticated", False):
        supplier_id = request.args.get("supplier_id", type=int)
    if not supplier_id:
        return _jsonify_safe({"error": "supplier_id required (set on token or query for session users)"}, 400)
    svc = get_inventory_service()
    limit = min(int(request.args.get("limit", 100)), 500)
    skip = int(request.args.get("skip", 0))
    batches = svc.list_batches(supplier_id=supplier_id, limit=limit, skip=skip)
    return _jsonify_safe({"batches": _json_compatible(batches)})


@internal.route("/api/supplier/pos", methods=["GET"])
def api_supplier_pos():
    """View PO status for this supplier (token must have supplier_id)."""
    if not _allow_token_role("supplier"):
        return _jsonify_safe({"error": "Forbidden"}, 403)
    api_user = getattr(g, "inventory_api_user", None)
    supplier_id = api_user.get("supplier_id") if api_user else None
    if not supplier_id and getattr(current_user, "is_authenticated", False):
        supplier_id = request.args.get("supplier_id", type=int)
    if not supplier_id:
        return _jsonify_safe({"error": "supplier_id required (set on token or query for session users)"}, 400)
    svc = get_inventory_service()
    status = request.args.get("status")
    limit = min(int(request.args.get("limit", 100)), 500)
    skip = int(request.args.get("skip", 0))
    pos = svc.list_purchase_orders(supplier_id=supplier_id, status=status, limit=limit, skip=skip)
    return _jsonify_safe({"purchase_orders": _json_compatible(pos)})


def _supplier_docs_upload_dir():
    base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "data", "supplier_documents")
    os.makedirs(d, exist_ok=True)
    return d


@internal.route("/api/supplier/compliance", methods=["GET"])
def api_supplier_compliance_list():
    """List compliance (and other) documents for this supplier."""
    if not _allow_token_role("supplier"):
        return _jsonify_safe({"error": "Forbidden"}, 403)
    api_user = getattr(g, "inventory_api_user", None)
    supplier_id = api_user.get("supplier_id") if api_user else None
    if not supplier_id and getattr(current_user, "is_authenticated", False):
        supplier_id = request.args.get("supplier_id", type=int)
    if not supplier_id:
        return _jsonify_safe({"error": "supplier_id required (set on token or query for session users)"}, 400)
    svc = get_inventory_service()
    document_type = request.args.get("document_type")
    limit = min(int(request.args.get("limit", 100)), 500)
    skip = int(request.args.get("skip", 0))
    docs = svc.list_supplier_documents(supplier_id=supplier_id, document_type=document_type, limit=limit, skip=skip)
    return _jsonify_safe({"documents": _json_compatible(docs)})


@internal.route("/api/supplier/compliance/upload", methods=["POST"])
def api_supplier_compliance_upload():
    """Upload a compliance (or other) document for this supplier."""
    if not _allow_token_role("supplier"):
        return _jsonify_safe({"error": "Forbidden"}, 403)
    api_user = getattr(g, "inventory_api_user", None)
    supplier_id = api_user.get("supplier_id") if api_user else None
    if not supplier_id and getattr(current_user, "is_authenticated", False):
        supplier_id = request.form.get("supplier_id", type=int)
    if not supplier_id:
        return _jsonify_safe({"error": "supplier_id required (set on token or form for session users)"}, 400)
    f = request.files.get("file")
    if not f or not f.filename:
        return _jsonify_safe({"error": "file required"}, 400)
    name = request.form.get("name") or f.filename
    document_type = (request.form.get("document_type") or "compliance").strip() or "compliance"
    ext = os.path.splitext(f.filename)[1] or ".bin"
    upload_dir = _supplier_docs_upload_dir()
    subdir = os.path.join(upload_dir, str(supplier_id))
    os.makedirs(subdir, exist_ok=True)
    path = os.path.join(subdir, f"{uuid.uuid4().hex}{ext}")
    f.save(path)
    token_id = api_user.get("id") if api_user else None
    svc = get_inventory_service()
    try:
        doc_id = svc.create_supplier_document(
            supplier_id=supplier_id,
            name=name,
            file_path=path,
            document_type=document_type,
            uploaded_by_token_id=token_id,
        )
        return _jsonify_safe({"id": doc_id, "name": name, "document_type": document_type}, 201)
    except Exception as e:
        return _jsonify_safe({"error": str(e)}, 400)


# ---------------------------------------------------------------------------
# API: CSV export (items, transactions, stock levels, batches)
# ---------------------------------------------------------------------------

def _csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "__float__") and hasattr(value, "as_integer_ratio"):
        return str(float(value))
    return str(value)


def _csv_response(rows, filename: str, fieldnames: list):
    if not fieldnames and rows:
        fieldnames = list(rows[0].keys()) if rows else []
    buf = StringIO()
    w = csv_module.writer(buf)
    w.writerow(fieldnames)
    for r in rows:
        row = [_csv_cell(r.get(f)) for f in fieldnames]
        w.writerow(row)
    from flask import Response
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@internal.route("/api/export/items", methods=["GET"])
@login_required
def api_export_items():
    if not _permission_inventory():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    fmt = (request.args.get("format") or "json").strip().lower()
    svc = get_inventory_service()
    limit = min(int(request.args.get("limit", 10000)), 50000)
    items = svc.list_items(limit=limit, skip=0)
    if fmt == "csv":
        fieldnames = ["id", "sku", "name", "barcode", "uom", "category", "standard_cost", "reorder_point", "is_active"]
        return _csv_response(items, "inventory_items.csv", fieldnames)
    return _jsonify_safe({"items": _json_compatible(items)})


@internal.route("/api/export/transactions", methods=["GET"])
@login_required
def api_export_transactions():
    if not _permission_inventory():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    fmt = (request.args.get("format") or "json").strip().lower()
    svc = get_inventory_service()
    item_id = request.args.get("item_id", type=int)
    location_id = request.args.get("location_id", type=int)
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    limit = min(int(request.args.get("limit", 10000)), 50000)
    tx = svc.list_transactions(item_id=item_id, location_id=location_id, from_date=from_date, to_date=to_date, limit=limit, skip=0)
    if fmt == "csv":
        fieldnames = ["id", "item_id", "location_id", "batch_id", "quantity", "transaction_type", "unit_cost", "weight", "weight_uom", "performed_at", "reference_type", "reference_id"]
        return _csv_response(tx, "inventory_transactions.csv", fieldnames)
    return _jsonify_safe({"transactions": _json_compatible(tx)})


@internal.route("/api/export/stock_levels", methods=["GET"])
@login_required
def api_export_stock_levels():
    if not _permission_inventory():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    fmt = (request.args.get("format") or "json").strip().lower()
    svc = get_inventory_service()
    item_id = request.args.get("item_id", type=int)
    location_id = request.args.get("location_id", type=int)
    limit = min(int(request.args.get("limit", 10000)), 50000)
    levels = svc.list_stock_levels_all(item_id=item_id, location_id=location_id, limit=limit, skip=0)
    if fmt == "csv":
        fieldnames = ["id", "item_id", "location_id", "batch_id", "quantity_on_hand", "quantity_reserved", "quantity_available"]
        return _csv_response(levels, "inventory_stock_levels.csv", fieldnames)
    return _jsonify_safe({"stock_levels": _json_compatible(levels)})


@internal.route("/api/export/batches", methods=["GET"])
@login_required
def api_export_batches():
    if not _permission_inventory():
        return _jsonify_safe({"error": "Forbidden"}, 403)
    fmt = (request.args.get("format") or "json").strip().lower()
    svc = get_inventory_service()
    item_id = request.args.get("item_id", type=int)
    limit = min(int(request.args.get("limit", 10000)), 50000)
    batches = svc.list_batches(item_id=item_id, limit=limit)
    if fmt == "csv":
        fieldnames = ["id", "item_id", "batch_number", "lot_number", "quantity", "weight", "weight_uom", "unit_weight", "unit_weight_uom", "expiry_date", "created_at"]
        return _csv_response(batches, "inventory_batches.csv", fieldnames)
    return _jsonify_safe({"batches": _json_compatible(batches)})


@internal.route("/openapi.json")
@login_required
@admin_required
def openapi_spec():
    """Return OpenAPI 3 spec for this plugin's APIs."""
    from app.openapi_utils import generate_openapi_spec
    server_url = request.url_root.rstrip("/") + BASE_PATH
    spec = generate_openapi_spec(
        blueprint_name=BLUEPRINT_NAME,
        server_url=server_url,
        title="Inventory Control API",
        version="0.0.1",
    )
    return jsonify(spec)


def get_blueprint():
    return internal


# --- OpenAPI registration (for AI/docs integration) ---
def _register_openapi():
    for path, method, op_id, summary in [
        (BASE_PATH + "/api/health", "GET", "inventory_health", "Health check"),
        (BASE_PATH + "/api/dashboard", "GET", "inventory_dashboard", "Dashboard metrics"),
        (BASE_PATH + "/api/analytics/stock_levels", "GET", "inventory_analytics_stock_levels", "Analytics: stock levels report"),
        (BASE_PATH + "/api/analytics/movers", "GET", "inventory_analytics_movers", "Analytics: fast/slow movers"),
        (BASE_PATH + "/api/analytics/activity", "GET", "inventory_analytics_activity", "Analytics: transaction activity"),
        (BASE_PATH + "/api/analytics/suppliers", "GET", "inventory_analytics_suppliers", "Analytics: suppliers list"),
        (BASE_PATH + "/api/categories", "GET", "inventory_categories_list", "List categories"),
        (BASE_PATH + "/api/categories", "POST", "inventory_categories_create", "Create category"),
        (BASE_PATH + "/api/categories/{category_id}", "GET", "inventory_categories_get", "Get category"),
        (BASE_PATH + "/api/categories/{category_id}", "PUT", "inventory_categories_update", "Update category"),
        (BASE_PATH + "/api/categories/{category_id}", "DELETE", "inventory_categories_delete", "Delete category"),
        (BASE_PATH + "/api/items", "GET", "inventory_items_list", "List items"),
        (BASE_PATH + "/api/items", "POST", "inventory_items_create", "Create item"),
        (BASE_PATH + "/api/items/{item_id}", "GET", "inventory_items_get", "Get item"),
        (BASE_PATH + "/api/items/{item_id}", "PUT", "inventory_items_update", "Update item"),
        (BASE_PATH + "/api/items/{item_id}", "DELETE", "inventory_items_archive", "Archive item"),
        (BASE_PATH + "/api/locations", "GET", "inventory_locations_list", "List locations"),
        (BASE_PATH + "/api/locations", "POST", "inventory_locations_create", "Create location"),
        (BASE_PATH + "/api/locations/{location_id}", "GET", "inventory_locations_get", "Get location"),
        (BASE_PATH + "/api/batches", "GET", "inventory_batches_list", "List batches"),
        (BASE_PATH + "/api/batches", "POST", "inventory_batches_create", "Create batch"),
        (BASE_PATH + "/api/transactions", "GET", "inventory_transactions_list", "List transactions"),
        (BASE_PATH + "/api/transactions", "POST", "inventory_transactions_create", "Record transaction"),
        (BASE_PATH + "/api/transactions/{tx_id}/rollback", "POST", "inventory_transactions_rollback", "Rollback transaction"),
        (BASE_PATH + "/api/invoices/upload", "POST", "inventory_invoices_upload", "Upload invoice"),
        (BASE_PATH + "/api/invoices/{invoice_id}", "GET", "inventory_invoices_get", "Get invoice"),
        (BASE_PATH + "/api/invoices/{invoice_id}/apply", "POST", "inventory_invoices_apply", "Apply invoice to stock"),
        (BASE_PATH + "/api/mobile/scan/in", "POST", "inventory_mobile_scan_in", "Mobile scan in"),
        (BASE_PATH + "/api/mobile/scan/out", "POST", "inventory_mobile_scan_out", "Mobile scan out"),
        (BASE_PATH + "/api/mobile/scan/adjust", "POST", "inventory_mobile_scan_adjust", "Mobile adjust"),
        (BASE_PATH + "/api/mobile/items/search", "GET", "inventory_mobile_items_search", "Mobile item search"),
        (BASE_PATH + "/api/mobile/items/{item_id}/stock", "GET", "inventory_mobile_items_stock", "Mobile item stock"),
        (BASE_PATH + "/api/mobile/bulk/actions", "POST", "inventory_mobile_bulk_actions", "Mobile bulk actions"),
        (BASE_PATH + "/api/repack", "POST", "inventory_repack", "Repack/split batch"),
        (BASE_PATH + "/api/picking/suggest", "GET", "inventory_picking_suggest", "FEFO picking suggestions"),
        (BASE_PATH + "/api/tokens", "POST", "inventory_tokens_create", "Create API token"),
        (BASE_PATH + "/api/tokens", "GET", "inventory_tokens_list", "List API tokens"),
        (BASE_PATH + "/api/tokens/{token_id}", "DELETE", "inventory_tokens_revoke", "Revoke API token"),
        (BASE_PATH + "/api/supplier/items", "GET", "inventory_supplier_items", "Supplier: list items"),
        (BASE_PATH + "/api/supplier/invoices", "GET", "inventory_supplier_invoices", "Supplier: list invoices"),
        (BASE_PATH + "/api/supplier/receipts/confirm", "POST", "inventory_supplier_confirm_receipt", "Supplier: confirm receipt"),
        (BASE_PATH + "/api/supplier/batches", "GET", "inventory_supplier_batches", "Supplier: view supplied batches/lots"),
        (BASE_PATH + "/api/supplier/pos", "GET", "inventory_supplier_pos", "Supplier: view PO status"),
        (BASE_PATH + "/api/supplier/compliance", "GET", "inventory_supplier_compliance_list", "Supplier: list compliance docs"),
        (BASE_PATH + "/api/supplier/compliance/upload", "POST", "inventory_supplier_compliance_upload", "Supplier: upload compliance doc"),
        (BASE_PATH + "/api/export/items", "GET", "inventory_export_items", "Export items CSV/JSON"),
        (BASE_PATH + "/api/export/transactions", "GET", "inventory_export_transactions", "Export transactions CSV/JSON"),
        (BASE_PATH + "/api/export/stock_levels", "GET", "inventory_export_stock_levels", "Export stock levels CSV/JSON"),
        (BASE_PATH + "/api/export/batches", "GET", "inventory_export_batches", "Export batches CSV/JSON"),
    ]:
        register_path(BLUEPRINT_NAME, path, method, op_id, summary, tags=["inventory_control"])


_register_openapi()
