import os
import uuid
from functools import wraps
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from app.objects import PluginManager
from . import services as hr_services

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
internal_bp = Blueprint("internal_hr", __name__, url_prefix="/plugin/hr_module", template_folder=_template)
public_bp = Blueprint("public_hr", __name__, url_prefix="/hr", template_folder=_template)


@internal_bp.get("/")
def admin_index():
    return redirect("/")


@public_bp.get("/")
@_staff_required
def public_index():
    cid = _contractor_id()
    requests_list = hr_services.list_document_requests(cid) if cid else []
    return render_template(
        "hr_module/public/index.html",
        module_name="HR",
        module_description="Your details and document requests.",
        requests_list=requests_list,
        config=_core_manifest,
        website_settings=_get_website_settings(),
    )


@public_bp.get("/profile")
@_staff_required
def profile():
    cid = _contractor_id()
    if not cid:
        return redirect(url_for("public_hr.public_index"))
    profile_data = hr_services.get_staff_profile(cid)
    return render_template(
        "hr_module/public/profile.html",
        profile=profile_data,
        config=_core_manifest,
        website_settings=_get_website_settings(),
    )


@public_bp.post("/profile")
@_staff_required
def profile_save():
    cid = _contractor_id()
    if not cid:
        return redirect(url_for("public_hr.public_index"))
    data = request.form
    hr_services.update_staff_details(cid, {
        "phone": data.get("phone"),
        "address_line1": data.get("address_line1"),
        "address_line2": data.get("address_line2"),
        "postcode": data.get("postcode"),
        "emergency_contact_name": data.get("emergency_contact_name"),
        "emergency_contact_phone": data.get("emergency_contact_phone"),
    })
    return redirect(url_for("public_hr.profile"))


@public_bp.get("/request/<int:req_id>")
@_staff_required
def request_detail(req_id):
    from app.objects import get_db_connection
    cid = _contractor_id()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT r.*, (SELECT COUNT(*) FROM hr_document_uploads u WHERE u.request_id = r.id) AS upload_count
            FROM hr_document_requests r WHERE r.id = %s AND r.contractor_id = %s
        """, (req_id, cid))
        req = cur.fetchone()
        if not req:
            return redirect(url_for("public_hr.public_index"))
        cur.execute("SELECT id, file_path, file_name, uploaded_at FROM hr_document_uploads WHERE request_id = %s ORDER BY uploaded_at", (req_id,))
        req["uploads"] = cur.fetchall() or []
    finally:
        cur.close()
        conn.close()
    return render_template(
        "hr_module/public/request_detail.html",
        req=req,
        config=_core_manifest,
        website_settings=_get_website_settings(),
    )


@public_bp.post("/request/<int:req_id>/upload")
@_staff_required
def request_upload(req_id):
    cid = _contractor_id()
    if not cid:
        return redirect(url_for("public_hr.public_index"))
    file = request.files.get("document")
    if not file or not file.filename:
        return redirect(url_for("public_hr.request_detail", req_id=req_id))
    base = getattr(current_app, "root_path", None) or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    upload_dir = os.path.join(base, "static", "uploads", "hr_documents")
    os.makedirs(upload_dir, exist_ok=True)
    ext = os.path.splitext(file.filename)[1] or ".pdf"
    safe_name = f"{req_id}_{cid}_{uuid.uuid4().hex[:12]}{ext}"
    rel_path = os.path.join("uploads", "hr_documents", safe_name)
    full_path = os.path.join(base, "static", rel_path)
    file.save(full_path)
    try:
        hr_services.add_upload(req_id, cid, rel_path, file.filename)
    except ValueError:
        pass
    return redirect(url_for("public_hr.request_detail", req_id=req_id))


def get_blueprint():
    return internal_bp


def get_public_blueprint():
    return public_bp
