import csv
import os
import uuid
from datetime import datetime
from functools import wraps
from io import StringIO

from flask import Blueprint, current_app, flash, redirect, render_template, request, response, session, url_for
from flask_login import current_user, login_required
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


def _admin_required_hr(view):
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


@internal_bp.get("/")
@login_required
@_admin_required_hr
def admin_index():
    return render_template(
        "admin/index.html",
        module_name="HR",
        module_description="Employee details, document uploads, and HR requests.",
        plugin_system_name="hr_module",
        config=_core_manifest,
    )


# -----------------------------------------------------------------------------
# Admin: Contractor search and profile
# -----------------------------------------------------------------------------


@internal_bp.get("/contractors")
@login_required
@_admin_required_hr
def admin_contractors():
    q = (request.args.get("q") or "").strip()
    contractors = hr_services.admin_search_contractors(q, limit=50) if q else []
    return render_template(
        "admin/contractors.html",
        q=q,
        contractors=contractors,
        config=_core_manifest,
    )


@internal_bp.get("/contractors/<int:cid>")
@login_required
@_admin_required_hr
def admin_contractor_profile(cid):
    profile = hr_services.admin_get_staff_profile(cid)
    if not profile:
        flash("Contractor not found.", "error")
        return redirect(url_for("internal_hr.admin_contractors"))
    requests_list, _ = hr_services.admin_list_document_requests(contractor_id=cid, limit=100, offset=0)
    return render_template(
        "admin/contractor_profile.html",
        profile=profile,
        requests_list=requests_list,
        config=_core_manifest,
    )


@internal_bp.get("/contractors/<int:cid>/edit")
@login_required
@_admin_required_hr
def admin_contractor_edit_form(cid):
    profile = hr_services.admin_get_staff_profile(cid)
    if not profile:
        flash("Contractor not found.", "error")
        return redirect(url_for("internal_hr.admin_contractors"))
    return render_template(
        "admin/contractor_edit.html",
        profile=profile,
        request_types=hr_services.REQUEST_TYPES,
        config=_core_manifest,
    )


@internal_bp.post("/contractors/<int:cid>/edit")
@login_required
@_admin_required_hr
def admin_contractor_edit_save(cid):
    profile = hr_services.admin_get_staff_profile(cid)
    if not profile:
        flash("Contractor not found.", "error")
        return redirect(url_for("internal_hr.admin_contractors"))
    data = {
        "phone": request.form.get("phone"),
        "address_line1": request.form.get("address_line1"),
        "address_line2": request.form.get("address_line2"),
        "postcode": request.form.get("postcode"),
        "emergency_contact_name": request.form.get("emergency_contact_name"),
        "emergency_contact_phone": request.form.get("emergency_contact_phone"),
        "driving_licence_number": request.form.get("driving_licence_number"),
        "driving_licence_expiry": request.form.get("driving_licence_expiry"),
        "driving_licence_document_path": request.form.get("driving_licence_document_path"),
        "right_to_work_type": request.form.get("right_to_work_type"),
        "right_to_work_expiry": request.form.get("right_to_work_expiry"),
        "right_to_work_document_path": request.form.get("right_to_work_document_path"),
        "dbs_level": request.form.get("dbs_level"),
        "dbs_number": request.form.get("dbs_number"),
        "dbs_expiry": request.form.get("dbs_expiry"),
        "dbs_document_path": request.form.get("dbs_document_path"),
        "contract_type": request.form.get("contract_type"),
        "contract_start": request.form.get("contract_start"),
        "contract_end": request.form.get("contract_end"),
        "contract_document_path": request.form.get("contract_document_path"),
    }
    if hr_services.admin_update_staff_profile(cid, data):
        flash("Profile saved.", "success")
    else:
        flash("Failed to save profile.", "error")
    return redirect(url_for("internal_hr.admin_contractor_profile", cid=cid))


# -----------------------------------------------------------------------------
# Admin: Document requests
# -----------------------------------------------------------------------------


@internal_bp.get("/requests")
@login_required
@_admin_required_hr
def admin_requests():
    contractor_id = request.args.get("contractor_id", type=int)
    status = (request.args.get("status") or "").strip() or None
    date_from_s = request.args.get("date_from") or ""
    date_to_s = request.args.get("date_to") or ""
    date_from = None
    date_to = None
    if date_from_s:
        try:
            date_from = datetime.strptime(date_from_s, "%Y-%m-%d").date()
        except ValueError:
            pass
    if date_to_s:
        try:
            date_to = datetime.strptime(date_to_s, "%Y-%m-%d").date()
        except ValueError:
            pass
    page = max(1, request.args.get("page", type=int) or 1)
    per_page = 50
    rows, total = hr_services.admin_list_document_requests(
        contractor_id=contractor_id,
        status=status,
        date_from=date_from,
        date_to=date_to,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    total_pages = (total + per_page - 1) // per_page if total else 1
    return render_template(
        "admin/requests.html",
        requests_list=rows,
        total=total,
        page=page,
        total_pages=total_pages,
        contractor_id=contractor_id,
        status=status,
        date_from=date_from_s,
        date_to=date_to_s,
        config=_core_manifest,
    )


@internal_bp.get("/requests/new")
@login_required
@_admin_required_hr
def admin_request_new_form():
    contractors = hr_services.admin_list_contractors_for_select()
    return render_template(
        "admin/request_new.html",
        contractors=contractors,
        request_types=hr_services.REQUEST_TYPES,
        config=_core_manifest,
    )


@internal_bp.post("/requests/new")
@login_required
@_admin_required_hr
def admin_request_new():
    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    required_by_s = (request.form.get("required_by_date") or "").strip() or None
    request_type = (request.form.get("request_type") or "other").strip()
    contractor_ids = []
    for part in request.form.getlist("contractor_ids"):
        if str(part).strip().isdigit():
            contractor_ids.append(int(part))
    if request.form.get("all_contractors") == "on":
        contractor_ids = [c["id"] for c in hr_services.admin_list_contractors_for_select()]
    if not title:
        flash("Title is required.", "error")
        return redirect(url_for("internal_hr.admin_request_new_form"))
    if not contractor_ids:
        flash("Select at least one contractor (or All staff).", "error")
        return redirect(url_for("internal_hr.admin_request_new_form"))
    required_by = None
    if required_by_s:
        try:
            required_by = datetime.strptime(required_by_s, "%Y-%m-%d").date()
        except ValueError:
            pass
    count = hr_services.admin_create_document_request(
        contractor_ids, title, description=description, required_by_date=required_by, request_type=request_type
    )
    flash(f"Request created for {count} contractor(s).", "success")
    return redirect(url_for("internal_hr.admin_requests"))


@internal_bp.get("/requests/<int:rid>")
@login_required
@_admin_required_hr
def admin_request_detail(rid):
    req = hr_services.admin_get_request(rid)
    if not req:
        flash("Request not found.", "error")
        return redirect(url_for("internal_hr.admin_requests"))
    return render_template(
        "admin/request_detail.html",
        req=req,
        config=_core_manifest,
    )


@internal_bp.post("/requests/<int:rid>/approve")
@login_required
@_admin_required_hr
def admin_request_approve(rid):
    if request.form.get("_csrf_token") and request.form.get("_csrf_token") != session.get("_csrf"):
        pass  # CSRF checked by app
    admin_notes = (request.form.get("admin_notes") or "").strip() or None
    user_id = getattr(current_user, "id", None)
    if hr_services.admin_approve_request(rid, user_id, admin_notes=admin_notes):
        flash("Request approved.", "success")
    else:
        flash("Request not found.", "error")
    return redirect(url_for("internal_hr.admin_request_detail", rid=rid))


@internal_bp.post("/requests/<int:rid>/reject")
@login_required
@_admin_required_hr
def admin_request_reject(rid):
    admin_notes = (request.form.get("admin_notes") or "").strip() or None
    user_id = getattr(current_user, "id", None)
    if hr_services.admin_reject_request(rid, user_id, admin_notes=admin_notes):
        flash("Request rejected.", "success")
    else:
        flash("Request not found.", "error")
    return redirect(url_for("internal_hr.admin_request_detail", rid=rid))


# -----------------------------------------------------------------------------
# Admin: Expiry dashboard and reports
# -----------------------------------------------------------------------------


@internal_bp.get("/expiry")
@login_required
@_admin_required_hr
def admin_expiry():
    days = min(365, max(7, request.args.get("days", type=int) or 90))
    expiring = hr_services.get_expiring_documents(days=days)
    return render_template(
        "admin/expiry.html",
        expiring=expiring,
        days=days,
        config=_core_manifest,
    )


@internal_bp.get("/reports")
@login_required
@_admin_required_hr
def admin_reports():
    overview = hr_services.hr_compliance_overview()
    return render_template(
        "admin/reports.html",
        overview=overview,
        config=_core_manifest,
    )


@internal_bp.get("/reports/export")
@login_required
@_admin_required_hr
def admin_reports_export():
    """CSV export of staff + key dates and statuses."""
    contractors = hr_services.admin_list_contractors_for_select()
    profiles = []
    for c in contractors:
        p = hr_services.admin_get_staff_profile(c["id"])
        if p:
            profiles.append(p)
    out = StringIO()
    w = csv.writer(out)
    w.writerow([
        "id", "name", "email", "phone", "address_line1", "postcode",
        "driving_licence_expiry", "right_to_work_expiry", "dbs_expiry", "contract_end",
    ])
    for p in profiles:
        w.writerow([
            p.get("id"),
            p.get("name") or "",
            p.get("email") or "",
            p.get("phone") or "",
            p.get("address_line1") or "",
            p.get("postcode") or "",
            p.get("driving_licence_expiry") or "",
            p.get("right_to_work_expiry") or "",
            p.get("dbs_expiry") or "",
            p.get("contract_end") or "",
        ])
    resp = response.make_response(out.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = "attachment; filename=hr_export.csv"
    return resp


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
    document_type = request.form.get("document_type") or "primary"
    try:
        hr_services.add_upload(req_id, cid, rel_path, file.filename, document_type=document_type)
    except ValueError:
        pass
    return redirect(url_for("public_hr.request_detail", req_id=req_id))


def get_blueprint():
    return internal_bp


def get_public_blueprint():
    return public_bp
