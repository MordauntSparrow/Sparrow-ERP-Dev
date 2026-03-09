from functools import wraps
import logging
import os
from flask import (
    Blueprint, request, jsonify,
    render_template, redirect, url_for, flash, session, current_app
)
from app.objects import get_db_connection, AuthManager, PluginManager

from .services import (
    get_messages,
    get_todos,
    get_module_links,
    get_pending_counts,
    is_scheduling_enabled,
    safe_next_url,
    safe_profile_picture_path,
)

logger = logging.getLogger(__name__)


def _get_website_settings():
    """Return website_settings for templates (website_public_base.html). From website_module or safe default."""
    try:
        from app.plugins.website_module.routes import get_website_settings
        return get_website_settings()
    except Exception:
        pass
    _keys = (
        "favicon_path", "default_og_image", "schema_json", "cookie_bar_colors", "cookie_bar_text",
        "cookie_bar_accept_text", "cookie_bar_decline_text", "cookie_policy", "analytics_code",
        "facebook_url", "instagram_url", "linkedin_url", "twitter_url", "youtube_url", "tiktok_url",
        "pinterest_url", "whatsapp_url", "threads_url", "reddit_url", "snapchat_url", "telegram_url",
        "discord_url", "tumblr_url", "github_url", "medium_url", "vimeo_url", "dribbble_url",
        "behance_url", "soundcloud_url", "slack_url", "mastodon_url",
    )
    return {k: None for k in _keys}

# =============================================================================
# Blueprints
# =============================================================================
_template_folder = os.path.join(os.path.dirname(__file__), "templates")
internal_bp = Blueprint(
    "internal_employee_portal",
    __name__,
    url_prefix="/plugin/employee_portal_module",
    template_folder=_template_folder,
)
public_bp = Blueprint(
    "public_employee_portal",
    __name__,
    url_prefix="/employee-portal",
    template_folder=_template_folder,
)

plugin_manager = PluginManager(os.path.abspath("app/plugins"))
core_manifest = plugin_manager.get_core_manifest()

# =============================================================================
# Auth (reuse tb_contractors / session tb_user)
# =============================================================================


def current_ep_user():
    return session.get("tb_user") or None


def current_ep_user_id():
    u = current_ep_user()
    return int(u["id"]) if u and u.get("id") is not None else None


def staff_required_ep(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_ep_user()
        if not u:
            target = url_for("public_employee_portal.login_page")
            next_path = safe_next_url(request.path if request else "", "")
            if next_path:
                from urllib.parse import quote
                target = target + "?next=" + quote(next_path, safe="/")
            return redirect(target)
        return view(*args, **kwargs)
    return wrapped


# =============================================================================
# Public: Login / Logout
# =============================================================================


@public_bp.get("/login")
def login_page():
    if current_ep_user():
        # Already logged in: always go to dashboard (do not use next - avoids redirect loop to /time-billing/)
        return redirect(url_for("public_employee_portal.dashboard"))
    site_settings = (core_manifest or {}).get("site_settings") or {}
    site_settings = {
        "company_name": site_settings.get("company_name") or "Employee Portal",
        "branding": site_settings.get("branding") or "name",
        "logo_path": site_settings.get("logo_path") or "",
    }
    return render_template(
        "employee_portal_module/public/login.html",
        config=core_manifest,
        site_settings=site_settings,
        website_settings=_get_website_settings(),
    )


@public_bp.post("/login")
def login_submit():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    if not email or not password:
        flash("Please enter your email and password.", "error")
        return redirect(url_for("public_employee_portal.login_page"))

    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT id, email, name, initials, status, password_hash,
                       profile_picture_path
                FROM tb_contractors
                WHERE email = %s
                LIMIT 1
            """, (email,))
            u = cur.fetchone()
        finally:
            cur.close()
            conn.close()

        if not u or not u.get("password_hash") or not AuthManager.verify_password(
            u["password_hash"], password
        ):
            logger.warning("Employee portal login failed for email=%s", email[:3] + "***" if len(email) > 3 else "***")
            flash("Invalid email or password.", "error")
            return redirect(url_for("public_employee_portal.login_page"))

        if str(u.get("status", "")).lower() not in ("active", "1", "true", "yes"):
            logger.info("Employee portal login rejected (inactive): contractor_id=%s", u.get("id"))
            flash("Your account is inactive. Please contact an administrator.", "error")
            return redirect(url_for("public_employee_portal.login_page"))

        if request.form.get("remember") == "on":
            session.permanent = True

        display = (u.get("name") or "").strip() or u.get("email") or ""
        try:
            from app.plugins.time_billing_module.routes import _contractor_effective_role
            role = _contractor_effective_role(int(u["id"]))
        except Exception:
            role = "staff"
        safe_avatar = safe_profile_picture_path(u.get("profile_picture_path"))
        session["tb_user"] = {
            "id": int(u["id"]),
            "email": u["email"],
            "name": display,
            "initials": (u.get("initials") or "").strip(),
            "profile_picture_path": safe_avatar,
            "role": role,
        }
        session.modified = True  # Ensure session is persisted so time_billing and other modules see tb_user

        default_next = url_for("public_employee_portal.dashboard")
        next_param = request.form.get("next") or request.args.get("next")
        next_url = safe_next_url(next_param, default_next, request)
        logger.info("Employee portal login success: contractor_id=%s", u["id"])
        resp = redirect(next_url)
        # Fallback cookie so time-billing can restore session when session cookie is not sent (e.g. path/port)
        try:
            from itsdangerous import URLSafeTimedSerializer
            s = URLSafeTimedSerializer(current_app.secret_key)
            token = s.dumps(int(u["id"]), salt="tb_cid")
            resp.set_cookie("tb_cid", token, path="/", max_age=60 * 60 * 24 * 7, httponly=True, samesite="Lax")
        except Exception:
            pass
        return resp
    except Exception as e:
        logger.exception("Employee portal login error: %s", e)
        flash("An unexpected error occurred. Please try again.", "error")
        return redirect(url_for("public_employee_portal.login_page"))


@public_bp.get("/logout")
def logout():
    session.pop("tb_user", None)
    resp = redirect(url_for("public_employee_portal.login_page"))
    resp.delete_cookie("tb_cid", path="/")
    flash("You have been logged out.", "success")
    return resp


# =============================================================================
# Public: Launch (one-time token so time-billing gets auth without relying on session cookie)
# =============================================================================


@public_bp.get("/go/<slug>")
@staff_required_ep
def go_to_module(slug):
    """Redirect to a module with a one-time launch token so the target app can restore session."""
    if slug != "time-billing":
        return redirect(url_for("public_employee_portal.dashboard"))
    uid = current_ep_user_id()
    if not uid:
        return redirect(url_for("public_employee_portal.login_page"))
    try:
        from itsdangerous import URLSafeTimedSerializer
        s = URLSafeTimedSerializer(current_app.secret_key)
        token = s.dumps(uid, salt="tb_launch")
        return redirect(f"/time-billing/?launch={token}")
    except Exception:
        return redirect("/time-billing/")


# =============================================================================
# Public: Dashboard (mobile-first)
# =============================================================================


@public_bp.get("/")
@staff_required_ep
def dashboard():
    uid = current_ep_user_id()
    user = current_ep_user()
    if not user:
        return redirect(url_for("public_employee_portal.login_page"))

    # Use safe profile path in context (may already be set in session; ensure no path traversal)
    user = dict(user)
    user["profile_picture_path"] = safe_profile_picture_path(user.get("profile_picture_path"))

    messages = get_messages(uid)
    todos = get_todos(uid)
    pending_policies, pending_hr_requests = get_pending_counts(uid)
    module_links = get_module_links(plugin_manager)
    # Use launch URL for time-billing so auth is passed via token (avoids session/cookie not sent)
    for mod in module_links:
        if mod.get("launch_slug") and mod.get("enabled"):
            mod["url"] = url_for("public_employee_portal.go_to_module", slug=mod["launch_slug"])
    scheduling_enabled = is_scheduling_enabled(plugin_manager)

    return render_template(
        "employee_portal_module/public/dashboard.html",
        config=core_manifest or {},
        user=user,
        messages=messages,
        todos=todos,
        module_links=module_links,
        pending_policies=pending_policies,
        pending_hr_requests=pending_hr_requests,
        scheduling_enabled=scheduling_enabled,
        website_settings=_get_website_settings(),
    )


# =============================================================================
# Public: Mark message read / todo complete (optional)
# =============================================================================


@public_bp.post("/api/messages/<int:msg_id>/read")
@staff_required_ep
def api_mark_message_read(msg_id):
    if msg_id <= 0:
        return jsonify({"error": "Invalid message id"}), 400
    uid = current_ep_user_id()
    if not uid:
        return jsonify({"error": "Not authenticated"}), 401
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE ep_messages SET read_at = NOW() WHERE id = %s AND contractor_id = %s",
            (msg_id, uid),
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Message not found"}), 404
    except Exception as e:
        logger.exception("api_mark_message_read: %s", e)
        return jsonify({"error": "Server error"}), 500
    finally:
        cur.close()
        conn.close()
    return jsonify({"ok": True})


@public_bp.post("/api/todos/<int:todo_id>/complete")
@staff_required_ep
def api_todo_complete(todo_id):
    if todo_id <= 0:
        return jsonify({"error": "Invalid todo id"}), 400
    uid = current_ep_user_id()
    if not uid:
        return jsonify({"error": "Not authenticated"}), 401
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE ep_todos SET completed_at = NOW() WHERE id = %s AND contractor_id = %s",
            (todo_id, uid),
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Todo not found"}), 404
    except Exception as e:
        logger.exception("api_todo_complete: %s", e)
        return jsonify({"error": "Server error"}), 500
    finally:
        cur.close()
        conn.close()
    return jsonify({"ok": True})


# =============================================================================
# Internal (admin) – placeholder
# =============================================================================


@internal_bp.get("/")
def admin_index():
    return redirect("/")


# =============================================================================
# Blueprint registration
# =============================================================================


def get_blueprint():
    return internal_bp


def get_public_blueprint():
    return public_bp
