import os
import json
from datetime import date
from functools import wraps
from flask import (
    Blueprint,
    request,
    jsonify,
    render_template,
    redirect,
    url_for,
    session,
    flash,
)
from flask_login import current_user, login_required
from app.objects import PluginManager
from .services import ScheduleService

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

_template = os.path.join(os.path.dirname(__file__), "templates")
internal_bp = Blueprint(
    "internal_scheduling",
    __name__,
    url_prefix="/plugin/scheduling_module",
    template_folder=_template,
)
public_bp = Blueprint(
    "public_scheduling",
    __name__,
    url_prefix="/scheduling",
    template_folder=_template,
)


def _staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("tb_user"):
            return redirect("/employee-portal/login?next=/scheduling/")
        return view(*args, **kwargs)
    return wrapped


def _current_contractor_id():
    u = session.get("tb_user")
    if not u or u.get("id") is None:
        return None
    return int(u["id"])


def _admin_required_scheduling(view):
    """For internal (admin app, port 82): require core user with role admin/superuser (Flask-Login)."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        role = (getattr(current_user, "role", None) or "").lower()
        if role not in ("admin", "superuser"):
            flash("Admin access required.", "error")
            return redirect(url_for("routes.dashboard"))
        return view(*args, **kwargs)
    return wrapped


# ---------- Public: My shifts (for Work module and staff view) ----------


@public_bp.get("/")
@_staff_required
def public_index():
    return render_template(
        "scheduling_module/public/index.html",
        module_name="Scheduling & Shifts",
        module_description="View your shifts and schedule.",
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


@public_bp.get("/api/my-shifts")
@_staff_required
def api_my_shifts():
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Not authenticated"}), 401
    work_date = request.args.get("date")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    if work_date:
        try:
            d = date.fromisoformat(work_date)
            shifts = ScheduleService.get_my_shifts_for_date(cid, d)
        except ValueError:
            shifts = []
    elif date_from and date_to:
        try:
            df = date.fromisoformat(date_from)
            dt = date.fromisoformat(date_to)
            shifts = ScheduleService.list_shifts(contractor_id=cid, date_from=df, date_to=dt)
        except ValueError:
            shifts = []
    else:
        today = date.today()
        shifts = ScheduleService.get_my_shifts_for_date(cid, today)
    return jsonify({"shifts": shifts})


@public_bp.get("/my-requests")
@_staff_required
def my_requests():
    cid = _current_contractor_id()
    if not cid:
        return redirect("/employee-portal/login?next=/scheduling/my-requests")
    time_off_list = ScheduleService.list_time_off(contractor_id=cid)
    return render_template(
        "scheduling_module/public/my_requests.html",
        time_off_list=time_off_list,
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


@public_bp.get("/request-time-off")
@_staff_required
def request_time_off_page():
    return render_template(
        "scheduling_module/public/request_time_off.html",
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


@public_bp.post("/request-time-off")
@_staff_required
def request_time_off_submit():
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.request_time_off_page"))
    start_date = request.form.get("start_date")
    end_date = request.form.get("end_date")
    reason = request.form.get("reason", "").strip() or None
    if not start_date or not end_date:
        return redirect(url_for("public_scheduling.request_time_off_page"))
    try:
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
        if ed < sd:
            ed = sd
        ScheduleService.create_time_off(cid, sd, ed, reason=reason, type="annual")
    except ValueError:
        pass
    return redirect(url_for("public_scheduling.my_requests"))


@public_bp.get("/report-sickness")
@_staff_required
def report_sickness_page():
    today = date.today().isoformat()
    return render_template(
        "scheduling_module/public/report_sickness.html",
        today=today,
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


@public_bp.post("/report-sickness")
@_staff_required
def report_sickness_submit():
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.report_sickness_page"))
    start_date = request.form.get("start_date")
    end_date = request.form.get("end_date")
    reason = request.form.get("reason", "").strip() or None
    if not start_date:
        start_date = end_date = date.today().isoformat()
    if not end_date:
        end_date = start_date
    try:
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
        if ed < sd:
            ed = sd
        ScheduleService.create_time_off(cid, sd, ed, reason=reason or "Sickness", type="sickness")
    except ValueError:
        pass
    return redirect(url_for("public_scheduling.my_requests"))


@public_bp.get("/my-day")
@_staff_required
def my_day():
    """Mobile-first 'My day' page: list of today's shifts."""
    cid = _current_contractor_id()
    if not cid:
        return redirect("/employee-portal/login?next=/scheduling/my-day")
    today = date.today()
    shifts = ScheduleService.get_my_shifts_for_date(cid, today)
    return render_template(
        "scheduling_module/public/my_day.html",
        shifts=shifts,
        today=today,
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


# ---------- Public: Single shift (for Work module to record times) ----------


@public_bp.get("/api/shifts/<int:shift_id>")
@_staff_required
def api_get_shift(shift_id):
    shift = ScheduleService.get_shift(shift_id)
    if not shift:
        return jsonify({"error": "Not found"}), 404
    cid = _current_contractor_id()
    if shift["contractor_id"] != cid:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(shift)


@public_bp.patch("/api/shifts/<int:shift_id>")
@_staff_required
def api_patch_shift(shift_id):
    shift = ScheduleService.get_shift(shift_id)
    if not shift:
        return jsonify({"error": "Not found"}), 404
    cid = _current_contractor_id()
    if shift["contractor_id"] != cid:
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    updates = {}
    if "actual_start" in data:
        updates["actual_start"] = data["actual_start"]
    if "actual_end" in data:
        updates["actual_end"] = data["actual_end"]
    if "notes" in data:
        updates["notes"] = data["notes"]
    if updates:
        ScheduleService.update_shift(shift_id, updates)
    return jsonify({"ok": True})


# ---------- Internal (admin) ----------


@internal_bp.get("/")
@_admin_required_scheduling
def admin_index():
    return redirect(url_for("internal_scheduling.admin_shifts"))


@internal_bp.get("/shifts")
@_admin_required_scheduling
def admin_shifts():
    clients, sites = ScheduleService.list_clients_and_sites()
    job_types = ScheduleService.list_job_types()
    contractors = ScheduleService.list_contractors()
    return render_template(
        "scheduling_module/admin/shifts.html",
        clients=clients,
        sites=sites,
        job_types=job_types,
        contractors=contractors,
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


@internal_bp.get("/api/shifts")
@_admin_required_scheduling
def api_list_shifts():
    contractor_id = request.args.get("contractor_id", type=int)
    client_id = request.args.get("client_id", type=int)
    work_date = request.args.get("date")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    status = request.args.get("status")
    kwargs = {}
    if contractor_id is not None:
        kwargs["contractor_id"] = contractor_id
    if client_id is not None:
        kwargs["client_id"] = client_id
    if work_date:
        try:
            kwargs["work_date"] = date.fromisoformat(work_date)
        except ValueError:
            pass
    if date_from:
        try:
            kwargs["date_from"] = date.fromisoformat(date_from)
        except ValueError:
            pass
    if date_to:
        try:
            kwargs["date_to"] = date.fromisoformat(date_to)
        except ValueError:
            pass
    if status:
        kwargs["status"] = status
    shifts = ScheduleService.list_shifts(**kwargs)
    return jsonify({"shifts": shifts})


@internal_bp.post("/api/shifts")
@_admin_required_scheduling
def api_create_shift():
    data = request.get_json() or {}
    required = ["contractor_id", "client_id", "job_type_id", "work_date", "scheduled_start", "scheduled_end"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400
    try:
        shift_id = ScheduleService.create_shift(data)
        return jsonify({"id": shift_id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@internal_bp.get("/api/shifts/<int:shift_id>")
@_admin_required_scheduling
def api_get_shift_admin(shift_id):
    shift = ScheduleService.get_shift(shift_id)
    if not shift:
        return jsonify({"error": "Not found"}), 404
    return jsonify(shift)


@internal_bp.put("/api/shifts/<int:shift_id>")
@_admin_required_scheduling
def api_update_shift(shift_id):
    shift = ScheduleService.get_shift(shift_id)
    if not shift:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    try:
        ScheduleService.update_shift(shift_id, data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def get_blueprint():
    return internal_bp


def get_public_blueprint():
    return public_bp
