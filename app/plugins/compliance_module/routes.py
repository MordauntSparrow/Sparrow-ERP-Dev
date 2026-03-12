import os
import re
import uuid
from functools import wraps
from flask import Blueprint, flash, redirect, render_template, request, session, url_for, send_file
from flask_login import current_user, login_required
from app.objects import PluginManager
from . import services as compliance_services

_plugin_manager = PluginManager(os.path.abspath("app/plugins"))
_core_manifest = _plugin_manager.get_core_manifest() or {}


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


def _staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("tb_user"):
            return redirect("/employee-portal/login?next=" + request.path)
        return view(*args, **kwargs)
    return wrapped


def _contractor_id():
    u = session.get("tb_user")
    return int(u["id"]) if u and u.get("id") is not None else None


_template = os.path.join(os.path.dirname(__file__), "templates")
_uploads_dir = os.path.join(os.path.dirname(__file__), "uploads")
internal_bp = Blueprint("internal_compliance", __name__, url_prefix="/plugin/compliance_module", template_folder=_template)
public_bp = Blueprint("public_compliance", __name__, url_prefix="/compliance", template_folder=_template)


def _safe_filename(name: str) -> str:
    if not name or not name.strip():
        return "attachment"
    base = os.path.basename(name).strip()
    base = re.sub(r"[^\w.\-]", "_", base)
    return base[:200] or "attachment"


def _admin_required_compliance(view):
    """For admin app: require core user with role admin/superuser (Flask-Login)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("routes.login"))
        role = (getattr(current_user, "role", None) or "").lower()
        if role not in ("admin", "superuser"):
            flash("Admin access required.", "error")
            return redirect(url_for("routes.dashboard"))
        return view(*args, **kwargs)
    return wrapped


def _slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "policy"


@internal_bp.get("/")
@login_required
@_admin_required_compliance
def admin_index():
    return render_template(
        "admin/index.html",
        module_name="Compliance & Policies",
        module_description="Manage policies and view acknowledgements. Staff view and sign from the Employee Portal.",
        plugin_system_name="compliance_module",
        config=_core_manifest,
    )


# ---------- Admin: Policies ----------


@internal_bp.get("/policies")
@login_required
@_admin_required_compliance
def admin_policies():
    active_only = request.args.get("active") == "1"
    policies = compliance_services.list_policies_admin(active_only=active_only)
    return render_template(
        "admin/policies.html",
        policies=policies,
        active_only=active_only,
        config=_core_manifest,
    )


@internal_bp.get("/policies/new")
@login_required
@_admin_required_compliance
def admin_policy_new():
    return render_template(
        "admin/policy_form.html",
        policy=None,
        config=_core_manifest,
    )


@internal_bp.post("/policies/new")
@login_required
@_admin_required_compliance
def admin_policy_create():
    from datetime import date
    title = (request.form.get("title") or "").strip()
    slug = (request.form.get("slug") or "").strip() or _slugify(title)
    summary = (request.form.get("summary") or "").strip() or None
    body = (request.form.get("body") or "").strip() or None
    try:
        version = int(request.form.get("version") or 1)
    except ValueError:
        version = 1
    effective_from_s = (request.form.get("effective_from") or "").strip()
    effective_to_s = (request.form.get("effective_to") or "").strip()
    effective_from = date.fromisoformat(effective_from_s) if effective_from_s else date.today()
    effective_to = date.fromisoformat(effective_to_s) if effective_to_s else None
    required_ack = request.form.get("required_acknowledgement") == "1"
    active = request.form.get("active") == "1"
    if not title:
        flash("Title required.", "error")
        return redirect(url_for("internal_compliance.admin_policy_new"))
    try:
        policy_id = compliance_services.create_policy(
            title=title, slug=slug, summary=summary, body=body,
            version=version, effective_from=effective_from, effective_to=effective_to,
            required_acknowledgement=required_ack, active=active,
        )
        f = request.files.get("file")
        if f and f.filename:
            os.makedirs(os.path.join(_uploads_dir, str(policy_id)), exist_ok=True)
            safe = _safe_filename(f.filename)
            unique = f"{uuid.uuid4().hex[:8]}_{safe}"
            path = os.path.join(_uploads_dir, str(policy_id), unique)
            f.save(path)
            compliance_services.update_policy(
                policy_id, file_path=f"{policy_id}/{unique}", file_name=f.filename.strip()[:255]
            )
        flash("Policy created.", "success")
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("internal_compliance.admin_policy_new"))
    return redirect(url_for("internal_compliance.admin_policies"))


@internal_bp.get("/policies/<int:policy_id>/edit")
@login_required
@_admin_required_compliance
def admin_policy_edit(policy_id):
    policy = compliance_services.get_policy_by_id(policy_id)
    if not policy:
        flash("Policy not found.", "error")
        return redirect(url_for("internal_compliance.admin_policies"))
    return render_template(
        "admin/policy_form.html",
        policy=policy,
        config=_core_manifest,
    )


@internal_bp.post("/policies/<int:policy_id>/edit")
@login_required
@_admin_required_compliance
def admin_policy_update(policy_id):
    from datetime import date
    policy = compliance_services.get_policy_by_id(policy_id)
    if not policy:
        flash("Policy not found.", "error")
        return redirect(url_for("internal_compliance.admin_policies"))
    title = (request.form.get("title") or "").strip()
    slug = (request.form.get("slug") or "").strip()
    summary = (request.form.get("summary") or "").strip() or None
    body = (request.form.get("body") or "").strip() or None
    try:
        version = int(request.form.get("version") or 1)
    except ValueError:
        version = policy.get("version") or 1
    effective_from_s = (request.form.get("effective_from") or "").strip()
    effective_to_s = (request.form.get("effective_to") or "").strip()
    effective_from = date.fromisoformat(effective_from_s) if effective_from_s else None
    effective_to = date.fromisoformat(effective_to_s) if effective_to_s else None
    required_ack = request.form.get("required_acknowledgement") == "1" if "required_acknowledgement" in request.form else None
    active = request.form.get("active") == "1" if "active" in request.form else None
    if title:
        compliance_services.update_policy(
            policy_id,
            title=title, slug=slug or None, summary=summary, body=body,
            version=version, effective_from=effective_from, effective_to=effective_to,
            required_acknowledgement=required_ack, active=active,
        )
    if request.form.get("remove_file") == "1":
        compliance_services.update_policy(policy_id, file_path="", file_name="")
    f = request.files.get("file")
    if f and f.filename:
        os.makedirs(os.path.join(_uploads_dir, str(policy_id)), exist_ok=True)
        safe = _safe_filename(f.filename)
        unique = f"{uuid.uuid4().hex[:8]}_{safe}"
        path = os.path.join(_uploads_dir, str(policy_id), unique)
        f.save(path)
        compliance_services.update_policy(
            policy_id, file_path=f"{policy_id}/{unique}", file_name=f.filename.strip()[:255]
        )
    if title or request.form.get("remove_file") == "1" or (f and f.filename):
        flash("Policy updated.", "success")
    return redirect(url_for("internal_compliance.admin_policies"))


# ---------- Admin: Acknowledgements ----------


@internal_bp.get("/acknowledgements")
@login_required
@_admin_required_compliance
def admin_acknowledgements():
    policy_id = request.args.get("policy_id", type=int)
    contractor_id = request.args.get("contractor_id", type=int)
    rows = compliance_services.list_acknowledgements(policy_id=policy_id, contractor_id=contractor_id)
    policies = compliance_services.list_policies_admin(active_only=False)
    contractors = compliance_services.list_contractors_for_select()
    return render_template(
        "admin/acknowledgements.html",
        rows=rows,
        policies=policies,
        contractors=contractors,
        policy_id=policy_id,
        contractor_id=contractor_id,
        config=_core_manifest,
    )


@public_bp.get("/")
@_staff_required
def public_index():
    cid = _contractor_id()
    policies = compliance_services.list_policies_for_staff(cid) if cid else []
    return render_template(
        "compliance_module/public/index.html",
        module_name="Compliance & Policies",
        module_description="View and sign company policies.",
        policies=policies,
        config=_core_manifest,
        website_settings=_get_website_settings(),
    )


@public_bp.get("/policy/<slug>")
@_staff_required
def view_policy(slug):
    policy = compliance_services.get_policy(slug)
    if not policy:
        return redirect(url_for("public_compliance.public_index"))
    cid = _contractor_id()
    acknowledged = compliance_services.is_acknowledged(policy["id"], cid) if cid else False
    return render_template(
        "compliance_module/public/policy.html",
        policy=policy,
        acknowledged=acknowledged,
        config=_core_manifest,
        website_settings=_get_website_settings(),
    )


@public_bp.get("/file/<int:policy_id>")
@_staff_required
def public_policy_file(policy_id):
    policy = compliance_services.get_policy_by_id(policy_id)
    if not policy or not policy.get("file_path") or not policy.get("file_name"):
        flash("File not found.", "error")
        return redirect(url_for("public_compliance.public_index"))
    full_path = os.path.join(_uploads_dir, policy["file_path"])
    if not os.path.isfile(full_path):
        flash("File not found.", "error")
        return redirect(url_for("public_compliance.public_index"))
    return send_file(
        full_path,
        as_attachment=True,
        download_name=policy["file_name"],
        mimetype="application/octet-stream",
    )


@public_bp.post("/policy/<slug>/acknowledge")
@_staff_required
def acknowledge(slug):
    policy = compliance_services.get_policy(slug)
    if not policy or not policy.get("required_acknowledgement"):
        return redirect(url_for("public_compliance.public_index"))
    cid = _contractor_id()
    if not cid:
        return redirect("/employee-portal/login")
    compliance_services.acknowledge_policy(
        policy["id"],
        cid,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
    )
    return redirect(url_for("public_compliance.view_policy", slug=slug))


def get_blueprint():
    return internal_bp


def get_public_blueprint():
    return public_bp
