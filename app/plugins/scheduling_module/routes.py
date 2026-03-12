import os
import json
from datetime import date, timedelta
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
from .ai_chat import chat as ai_chat, is_ai_available

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


@public_bp.get("/ai")
@_staff_required
def ai_chat_page():
    """Contractor can chat with AI about availability, shifts, and coverage."""
    return render_template(
        "scheduling_module/public/ai_chat.html",
        ai_available=is_ai_available(),
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


@public_bp.post("/api/ai/chat")
@_staff_required
def api_ai_chat():
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Unauthorized"}), 401
    if not is_ai_available():
        return jsonify({"error": "AI assistant is not configured.", "reply": None}), 503
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message is required.", "reply": None}), 400
    history = data.get("history") or []
    if not isinstance(history, list):
        history = []
    messages = []
    for h in history[-20:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})
    reply = ai_chat(cid, messages)
    if reply is None:
        return jsonify({"error": "The assistant is unavailable. Please try again.", "reply": None}), 503
    return jsonify({"reply": reply})


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


@public_bp.post("/time-off/<int:tid>/cancel")
@_staff_required
def cancel_time_off(tid):
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.my_requests"))
    if ScheduleService.cancel_time_off(tid, cid):
        flash("Request cancelled.", "success")
    else:
        flash("Could not cancel (only pending requests can be cancelled).", "warning")
    return redirect(url_for("public_scheduling.my_requests"))


# ---------- Shift swap (contractor) ----------


@public_bp.get("/swap")
@_staff_required
def my_swaps():
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.public_index"))
    my_list = ScheduleService.list_swap_requests(contractor_id=cid)
    available = ScheduleService.list_swap_requests(contractor_id=cid, for_claimer=True)
    my_shifts = ScheduleService.list_shifts(contractor_id=cid, date_from=date.today(), date_to=date.today() + timedelta(days=13))
    return render_template(
        "scheduling_module/public/swap.html",
        my_swaps=my_list,
        available_to_claim=available,
        my_shifts=my_shifts,
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


@public_bp.post("/swap/offer")
@_staff_required
def offer_shift():
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.my_swaps"))
    shift_id = request.form.get("shift_id", type=int)
    notes = (request.form.get("notes") or "").strip() or None
    if not shift_id:
        flash("Shift required.", "error")
        return redirect(url_for("public_scheduling.my_swaps"))
    sid = ScheduleService.create_swap_request(shift_id, cid, notes=notes)
    if sid:
        flash("Shift offered for swap. Others can claim it.", "success")
    else:
        flash("Could not offer (already offered or not your shift).", "warning")
    return redirect(url_for("public_scheduling.my_swaps"))


@public_bp.post("/swap/<int:swap_id>/claim")
@_staff_required
def claim_swap(swap_id):
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.my_swaps"))
    if ScheduleService.claim_swap(swap_id, cid):
        flash("You claimed this shift. Waiting for manager approval.", "success")
    else:
        flash("Could not claim.", "warning")
    return redirect(url_for("public_scheduling.my_swaps"))


@public_bp.post("/swap/<int:swap_id>/cancel")
@_staff_required
def cancel_swap(swap_id):
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.my_swaps"))
    if ScheduleService.cancel_swap(swap_id, cid):
        flash("Swap cancelled.", "success")
    return redirect(url_for("public_scheduling.my_swaps"))


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


# ---------- Public: My availability (contractor self-service) ----------


def _time_display(t):
    """Format time or timedelta for display (HH:MM)."""
    if t is None:
        return ""
    if hasattr(t, "strftime"):
        return t.strftime("%H:%M")
    if hasattr(t, "total_seconds"):
        s = int(t.total_seconds()) % (24 * 3600)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"
    return str(t)[:5]


@public_bp.get("/my-availability")
@_staff_required
def my_availability():
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.public_index"))
    availability = ScheduleService.list_availability(cid)
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for a in availability:
        a["start_time_display"] = _time_display(a.get("start_time"))
        a["end_time_display"] = _time_display(a.get("end_time"))
        a["day_name"] = day_names[a["day_of_week"]] if 0 <= a.get("day_of_week", -1) <= 6 else ""
        a["effective_from_display"] = a["effective_from"].strftime("%Y-%m-%d") if a.get("effective_from") and hasattr(a["effective_from"], "strftime") else str(a.get("effective_from", ""))[:10]
        a["effective_to_display"] = (a["effective_to"].strftime("%Y-%m-%d") if a["effective_to"] and hasattr(a["effective_to"], "strftime") else str(a.get("effective_to", ""))[:10]) if a.get("effective_to") else ""
    return render_template(
        "scheduling_module/public/my_availability.html",
        availability=availability,
        day_names=day_names,
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


def _parse_time(s):
    if not s:
        return None
    try:
        from datetime import datetime as dt
        return dt.strptime(s.strip()[:5], "%H:%M").time()
    except (ValueError, TypeError):
        return None


@public_bp.get("/availability/add")
@_staff_required
def availability_add_form():
    return render_template(
        "scheduling_module/public/availability_form.html",
        avail=None,
        config=_core_manifest,
    )


@public_bp.post("/availability/add")
@_staff_required
def availability_add():
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.my_availability"))
    day_of_week = request.form.get("day_of_week", type=int)
    start_time = _parse_time(request.form.get("start_time"))
    end_time = _parse_time(request.form.get("end_time"))
    effective_from_s = request.form.get("effective_from")
    effective_to_s = request.form.get("effective_to") or None
    if day_of_week is None or start_time is None or end_time is None or not effective_from_s:
        flash("Please fill in day, start time, end time, and effective from date.", "error")
        return redirect(url_for("public_scheduling.availability_add_form"))
    try:
        effective_from = date.fromisoformat(effective_from_s)
        effective_to = date.fromisoformat(effective_to_s) if effective_to_s else None
    except ValueError:
        flash("Invalid date.", "error")
        return redirect(url_for("public_scheduling.availability_add_form"))
    if day_of_week < 0 or day_of_week > 6:
        flash("Invalid day.", "error")
        return redirect(url_for("public_scheduling.availability_add_form"))
    ScheduleService.add_availability(cid, day_of_week, start_time, end_time, effective_from, effective_to)
    flash("Availability added.", "success")
    return redirect(url_for("public_scheduling.my_availability"))


@public_bp.get("/availability/<int:avail_id>/edit")
@_staff_required
def availability_edit_form(avail_id):
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.my_availability"))
    avail = ScheduleService.get_availability(avail_id, contractor_id=cid)
    if not avail:
        flash("Not found.", "error")
        return redirect(url_for("public_scheduling.my_availability"))
    avail["start_time_str"] = _time_display(avail.get("start_time"))
    avail["end_time_str"] = _time_display(avail.get("end_time"))
    if avail.get("effective_from"):
        avail["effective_from_iso"] = avail["effective_from"].isoformat() if hasattr(avail["effective_from"], "isoformat") else str(avail["effective_from"])[:10]
    else:
        avail["effective_from_iso"] = ""
    avail["effective_to_iso"] = ""
    if avail.get("effective_to"):
        avail["effective_to_iso"] = avail["effective_to"].isoformat() if hasattr(avail["effective_to"], "isoformat") else str(avail["effective_to"])[:10]
    return render_template(
        "scheduling_module/public/availability_form.html",
        avail=avail,
        config=_core_manifest,
    )


@public_bp.post("/availability/<int:avail_id>/edit")
@_staff_required
def availability_edit(avail_id):
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.my_availability"))
    avail = ScheduleService.get_availability(avail_id, contractor_id=cid)
    if not avail:
        flash("Not found.", "error")
        return redirect(url_for("public_scheduling.my_availability"))
    day_of_week = request.form.get("day_of_week", type=int)
    start_time = _parse_time(request.form.get("start_time"))
    end_time = _parse_time(request.form.get("end_time"))
    effective_from_s = request.form.get("effective_from")
    effective_to_s = request.form.get("effective_to") or None
    updates = {}
    if day_of_week is not None and 0 <= day_of_week <= 6:
        updates["day_of_week"] = day_of_week
    if start_time is not None:
        updates["start_time"] = start_time
    if end_time is not None:
        updates["end_time"] = end_time
    if effective_from_s:
        try:
            updates["effective_from"] = date.fromisoformat(effective_from_s)
        except ValueError:
            pass
    if effective_to_s:
        try:
            updates["effective_to"] = date.fromisoformat(effective_to_s)
        except ValueError:
            pass
    if updates:
        ScheduleService.update_availability(avail_id, cid, **updates)
        flash("Availability updated.", "success")
    return redirect(url_for("public_scheduling.my_availability"))


@public_bp.post("/availability/<int:avail_id>/delete")
@_staff_required
def availability_delete(avail_id):
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_scheduling.my_availability"))
    if ScheduleService.delete_availability(avail_id, cid):
        flash("Availability removed.", "success")
    else:
        flash("Could not remove.", "warning")
    return redirect(url_for("public_scheduling.my_availability"))


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


# ---------- Public API (contractor app) ----------


def _serialize_availability(a):
    out = dict(a)
    out["start_time"] = _time_display(a.get("start_time"))
    out["end_time"] = _time_display(a.get("end_time"))
    for k in ("effective_from", "effective_to"):
        if out.get(k) and hasattr(out[k], "isoformat"):
            out[k] = out[k].isoformat()
    return out


@public_bp.get("/api/my-time-off")
@_staff_required
def api_my_time_off():
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Unauthorized"}), 401
    date_from_s = request.args.get("from")
    date_to_s = request.args.get("to")
    date_from = date.fromisoformat(date_from_s) if date_from_s else None
    date_to = date.fromisoformat(date_to_s) if date_to_s else None
    rows = ScheduleService.list_time_off(contractor_id=cid, date_from=date_from, date_to=date_to)
    for r in rows:
        for k in ("start_date", "end_date"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return jsonify({"time_off": rows})


@public_bp.post("/api/time-off")
@_staff_required
def api_create_time_off():
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    start_s = data.get("start_date")
    end_s = data.get("end_date") or start_s
    reason = data.get("reason")
    type_val = data.get("type") or "annual"
    if not start_s:
        return jsonify({"error": "start_date required"}), 400
    try:
        start_date = date.fromisoformat(start_s)
        end_date = date.fromisoformat(end_s) if end_s else start_date
        if end_date < start_date:
            end_date = start_date
        tid = ScheduleService.create_time_off(cid, start_date, end_date, reason=reason, type=type_val)
        return jsonify({"id": tid}), 201
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400


@public_bp.post("/api/time-off/<int:tid>/cancel")
@_staff_required
def api_cancel_time_off(tid):
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Unauthorized"}), 401
    if ScheduleService.cancel_time_off(tid, cid):
        return jsonify({"ok": True})
    return jsonify({"error": "Not found or cannot cancel"}), 404


@public_bp.post("/api/report-sickness")
@_staff_required
def api_report_sickness():
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    start_s = data.get("start_date")
    end_s = data.get("end_date") or start_s
    reason = (data.get("reason") or "").strip() or "Sickness"
    if not start_s:
        return jsonify({"error": "start_date required"}), 400
    try:
        start_date = date.fromisoformat(start_s)
        end_date = date.fromisoformat(end_s) if end_s else start_date
        if end_date < start_date:
            end_date = start_date
        tid = ScheduleService.create_time_off(cid, start_date, end_date, reason=reason, type="sickness")
        return jsonify({"id": tid}), 201
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400


@public_bp.get("/api/availability")
@_staff_required
def api_list_availability():
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Unauthorized"}), 401
    rows = ScheduleService.list_availability(cid)
    return jsonify({"availability": [_serialize_availability(a) for a in rows]})


@public_bp.post("/api/availability")
@_staff_required
def api_create_availability():
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    day_of_week = data.get("day_of_week")
    start_time_s = data.get("start_time")
    end_time_s = data.get("end_time")
    effective_from_s = data.get("effective_from")
    effective_to_s = data.get("effective_to")
    if day_of_week is None or not start_time_s or not end_time_s or not effective_from_s:
        return jsonify({"error": "day_of_week, start_time, end_time, effective_from required"}), 400
    start_time = _parse_time(start_time_s) if isinstance(start_time_s, str) else None
    end_time = _parse_time(end_time_s) if isinstance(end_time_s, str) else None
    if start_time is None or end_time is None:
        return jsonify({"error": "Invalid start_time or end_time (use HH:MM)"}), 400
    try:
        effective_from = date.fromisoformat(effective_from_s)
        effective_to = date.fromisoformat(effective_to_s) if effective_to_s else None
    except ValueError:
        return jsonify({"error": "Invalid effective_from or effective_to"}), 400
    if not (0 <= day_of_week <= 6):
        return jsonify({"error": "day_of_week must be 0-6 (Mon-Sun)"}), 400
    ScheduleService.add_availability(cid, day_of_week, start_time, end_time, effective_from, effective_to)
    return jsonify({"ok": True}), 201


@public_bp.put("/api/availability/<int:avail_id>")
@_staff_required
def api_update_availability(avail_id):
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Unauthorized"}), 401
    avail = ScheduleService.get_availability(avail_id, contractor_id=cid)
    if not avail:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    updates = {}
    if "day_of_week" in data and 0 <= data["day_of_week"] <= 6:
        updates["day_of_week"] = data["day_of_week"]
    if data.get("start_time") is not None:
        t = _parse_time(data["start_time"]) if isinstance(data["start_time"], str) else None
        if t is not None:
            updates["start_time"] = t
    if data.get("end_time") is not None:
        t = _parse_time(data["end_time"]) if isinstance(data["end_time"], str) else None
        if t is not None:
            updates["end_time"] = t
    if data.get("effective_from"):
        try:
            updates["effective_from"] = date.fromisoformat(data["effective_from"])
        except ValueError:
            pass
    if data.get("effective_to") is not None:
        try:
            updates["effective_to"] = date.fromisoformat(data["effective_to"]) if data["effective_to"] else None
        except ValueError:
            pass
    if updates:
        ScheduleService.update_availability(avail_id, cid, **updates)
    return jsonify({"ok": True})


@public_bp.delete("/api/availability/<int:avail_id>")
@_staff_required
def api_delete_availability(avail_id):
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Unauthorized"}), 401
    if ScheduleService.delete_availability(avail_id, cid):
        return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404


# ---------- Internal (admin) ----------


@internal_bp.get("/")
@_admin_required_scheduling
def admin_index():
    return render_template(
        "scheduling_module/admin/index.html",
        config=_core_manifest,
    )


@internal_bp.get("/schedule/week")
@_admin_required_scheduling
def admin_schedule_week():
    """Week calendar view: pick week, see shifts by day. Sling-style."""
    from datetime import timedelta
    week_start_s = request.args.get("week")  # YYYY-MM-DD (Monday)
    if week_start_s:
        try:
            week_start = date.fromisoformat(week_start_s)
        except ValueError:
            week_start = date.today()
            while week_start.weekday() != 0:
                week_start -= timedelta(days=1)
    else:
        week_start = date.today()
        while week_start.weekday() != 0:
            week_start -= timedelta(days=1)
    week_end = week_start + timedelta(days=6)
    contractor_id = request.args.get("contractor_id", type=int)
    client_id = request.args.get("client_id", type=int)
    shifts = ScheduleService.list_shifts(
        date_from=week_start,
        date_to=week_end,
        contractor_id=contractor_id,
        client_id=client_id,
    )
    contractors = ScheduleService.list_contractors()
    clients, sites = ScheduleService.list_clients_and_sites()
    job_types = ScheduleService.list_job_types()
    week_days = [week_start + timedelta(days=i) for i in range(7)]
    # Key (contractor_id, date_iso) -> list of shifts for template
    from collections import defaultdict
    shifts_by = defaultdict(list)
    for s in shifts:
        wd = s.get("work_date")
        key = (s["contractor_id"], wd.isoformat() if hasattr(wd, "isoformat") else str(wd)) if wd else None
        if key:
            shifts_by[key].append(s)
    # Time-off overlay: (contractor_id, date_iso) -> list of time-off (type, etc.)
    time_off_list = ScheduleService.list_time_off(date_from=week_start, date_to=week_end, status=None)
    time_off_by = defaultdict(list)
    for to in time_off_list:
        cid = to.get("contractor_id")
        start = to.get("start_date")
        end = to.get("end_date")
        if cid is None or not start or not end:
            continue
        for i in range(7):
            d = week_start + timedelta(days=i)
            if start <= d <= end and to.get("status") in ("requested", "approved"):
                time_off_by[(cid, d.isoformat())].append(to)
    return render_template(
        "scheduling_module/admin/schedule_week.html",
        week_start=week_start,
        week_end=week_end,
        week_days=week_days,
        shifts=shifts,
        shifts_by=dict(shifts_by),
        time_off_by=dict(time_off_by),
        contractors=contractors,
        clients=clients,
        sites=sites,
        job_types=job_types,
        contractor_id=contractor_id,
        client_id=client_id,
        timedelta=timedelta,
        config=_core_manifest,
    )


@internal_bp.get("/schedule/month")
@_admin_required_scheduling
def admin_schedule_month():
    """Month calendar view: pick month, see shift counts per day, click to week or add shift."""
    from datetime import timedelta
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    if not year or not month:
        today = date.today()
        year, month = today.year, today.month
    try:
        first = date(year, month, 1)
    except (ValueError, TypeError):
        first = date.today().replace(day=1)
    # Last day of month
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    contractor_id = request.args.get("contractor_id", type=int)
    client_id = request.args.get("client_id", type=int)
    shifts = ScheduleService.list_shifts(
        date_from=first,
        date_to=last,
        contractor_id=contractor_id,
        client_id=client_id,
    )
    # Count per day: day_iso -> count
    from collections import defaultdict
    shifts_per_day = defaultdict(int)
    for s in shifts:
        wd = s.get("work_date")
        if wd:
            shifts_per_day[wd.isoformat() if hasattr(wd, "isoformat") else str(wd)] += 1
    # Calendar grid: weeks (each week Mon-Sun), first week may start in prev month
    week_start = first - timedelta(days=first.weekday())
    weeks = []
    current = week_start
    while current <= last or current.month == month or (current - week_start).days < 35:
        week_days = []
        for i in range(7):
            d = current + timedelta(days=i)
            week_days.append({
                "date": d,
                "iso": d.isoformat(),
                "count": shifts_per_day.get(d.isoformat(), 0),
                "in_month": d.month == month,
            })
        weeks.append(week_days)
        current += timedelta(days=7)
        if current > last and current.month != month:
            break
    contractors = ScheduleService.list_contractors()
    clients, _ = ScheduleService.list_clients_and_sites()
    return render_template(
        "scheduling_module/admin/schedule_month.html",
        year=year,
        month=month,
        first=first,
        last=last,
        weeks=weeks,
        shifts_per_day=dict(shifts_per_day),
        contractors=contractors,
        clients=clients,
        contractor_id=contractor_id,
        client_id=client_id,
        timedelta=timedelta,
        config=_core_manifest,
    )


@internal_bp.post("/schedule/copy-week")
@_admin_required_scheduling
def admin_copy_week():
    """Copy previous week's shifts into the current week (as drafts)."""
    from datetime import timedelta
    week_s = request.form.get("week") or request.args.get("week")
    if not week_s:
        flash("Week required.", "error")
        return redirect(url_for("internal_scheduling.admin_schedule_week"))
    try:
        to_monday = date.fromisoformat(week_s)
        if to_monday.weekday() != 0:
            to_monday = to_monday - timedelta(days=to_monday.weekday())
        from_monday = to_monday - timedelta(days=7)
    except ValueError:
        flash("Invalid week.", "error")
        return redirect(url_for("internal_scheduling.admin_schedule_week"))
    count = ScheduleService.copy_week_shifts(from_monday, to_monday)
    flash(f"Copied {count} shift(s) from previous week as drafts.", "success")
    return redirect(url_for("internal_scheduling.admin_schedule_week", week=to_monday.isoformat()))


# ---------- Templates ----------


@internal_bp.get("/templates")
@_admin_required_scheduling
def admin_templates():
    templates = ScheduleService.list_templates()
    for t in templates:
        t["slot_count"] = len(ScheduleService.list_template_slots(t["id"]))
    return render_template(
        "scheduling_module/admin/templates.html",
        templates=templates,
        config=_core_manifest,
    )


@internal_bp.get("/templates/new")
@_admin_required_scheduling
def admin_template_new():
    clients, sites = ScheduleService.list_clients_and_sites()
    job_types = ScheduleService.list_job_types()
    return render_template(
        "scheduling_module/admin/template_form.html",
        template=None,
        clients=clients,
        sites=sites,
        job_types=job_types,
        config=_core_manifest,
    )


@internal_bp.post("/templates/new")
@_admin_required_scheduling
def admin_template_create():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Name required.", "error")
        return redirect(url_for("internal_scheduling.admin_template_new"))
    client_id = request.form.get("client_id", type=int)
    site_id = request.form.get("site_id", type=int)
    job_type_id = request.form.get("job_type_id", type=int)
    tid = ScheduleService.create_template(name, client_id=client_id or None, site_id=site_id or None, job_type_id=job_type_id or None)
    flash("Template created. Add slots below.", "success")
    return redirect(url_for("internal_scheduling.admin_template_edit", template_id=tid))


def _parse_time_internal(s):
    if not s or not isinstance(s, str):
        return None
    try:
        from datetime import datetime as dt
        return dt.strptime(s.strip()[:5], "%H:%M").time()
    except (ValueError, TypeError):
        return None


@internal_bp.get("/templates/<int:template_id>/edit")
@_admin_required_scheduling
def admin_template_edit(template_id):
    template = ScheduleService.get_template(template_id)
    if not template:
        flash("Template not found.", "error")
        return redirect(url_for("internal_scheduling.admin_templates"))
    slots = ScheduleService.list_template_slots(template_id)
    for slot in slots:
        t = slot.get("start_time")
        slot["start_time_str"] = t.strftime("%H:%M") if t and hasattr(t, "strftime") else (str(t)[:5] if t else "")
        t = slot.get("end_time")
        slot["end_time_str"] = t.strftime("%H:%M") if t and hasattr(t, "strftime") else (str(t)[:5] if t else "")
    clients, sites = ScheduleService.list_clients_and_sites()
    job_types = ScheduleService.list_job_types()
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return render_template(
        "scheduling_module/admin/template_edit.html",
        template=template,
        slots=slots,
        clients=clients,
        sites=sites,
        job_types=job_types,
        day_names=day_names,
        config=_core_manifest,
    )


@internal_bp.post("/templates/<int:template_id>/edit")
@_admin_required_scheduling
def admin_template_update(template_id):
    template = ScheduleService.get_template(template_id)
    if not template:
        flash("Template not found.", "error")
        return redirect(url_for("internal_scheduling.admin_templates"))
    name = (request.form.get("name") or "").strip()
    client_id = request.form.get("client_id", type=int)
    site_id = request.form.get("site_id", type=int)
    job_type_id = request.form.get("job_type_id", type=int)
    ScheduleService.update_template(template_id, name=name or None, client_id=client_id or None, site_id=site_id or None, job_type_id=job_type_id or None)
    flash("Template updated.", "success")
    return redirect(url_for("internal_scheduling.admin_template_edit", template_id=template_id))


@internal_bp.post("/templates/<int:template_id>/slots/add")
@_admin_required_scheduling
def admin_template_slot_add(template_id):
    day_of_week = request.form.get("day_of_week", type=int)
    start_time = _parse_time_internal(request.form.get("start_time"))
    end_time = _parse_time_internal(request.form.get("end_time"))
    position_label = (request.form.get("position_label") or "").strip() or None
    if day_of_week is None or start_time is None or end_time is None:
        flash("Day, start time and end time required.", "error")
        return redirect(url_for("internal_scheduling.admin_template_edit", template_id=template_id))
    if not (0 <= day_of_week <= 6):
        flash("Invalid day.", "error")
        return redirect(url_for("internal_scheduling.admin_template_edit", template_id=template_id))
    ScheduleService.add_template_slot(template_id, day_of_week, start_time, end_time, position_label)
    flash("Slot added.", "success")
    return redirect(url_for("internal_scheduling.admin_template_edit", template_id=template_id))


@internal_bp.post("/templates/<int:template_id>/slots/<int:slot_id>/delete")
@_admin_required_scheduling
def admin_template_slot_delete(template_id, slot_id):
    ScheduleService.delete_template_slot(slot_id)
    flash("Slot removed.", "success")
    return redirect(url_for("internal_scheduling.admin_template_edit", template_id=template_id))


@internal_bp.get("/templates/<int:template_id>/apply")
@_admin_required_scheduling
def admin_template_apply_form(template_id):
    template = ScheduleService.get_template(template_id)
    if not template:
        flash("Template not found.", "error")
        return redirect(url_for("internal_scheduling.admin_templates"))
    contractors = ScheduleService.list_contractors()
    return render_template(
        "scheduling_module/admin/template_apply.html",
        template=template,
        contractors=contractors,
        config=_core_manifest,
    )


@internal_bp.post("/templates/<int:template_id>/apply")
@_admin_required_scheduling
def admin_template_apply(template_id):
    week_s = request.form.get("week_monday")
    contractor_id = request.form.get("contractor_id", type=int)
    if not week_s or not contractor_id:
        flash("Week start (Monday) and contractor required.", "error")
        return redirect(url_for("internal_scheduling.admin_template_apply_form", template_id=template_id))
    try:
        week_monday = date.fromisoformat(week_s)
        if week_monday.weekday() != 0:
            week_monday = week_monday - timedelta(days=week_monday.weekday())
    except ValueError:
        flash("Invalid date.", "error")
        return redirect(url_for("internal_scheduling.admin_template_apply_form", template_id=template_id))
    count = ScheduleService.apply_template_to_week(template_id, week_monday, contractor_id)
    flash(f"Created {count} draft shift(s) from template.", "success")
    return redirect(url_for("internal_scheduling.admin_schedule_week", week=week_monday.isoformat()))


@internal_bp.post("/shifts/<int:shift_id>/repeat")
@_admin_required_scheduling
def admin_shift_repeat(shift_id):
    num_weeks = request.form.get("num_weeks", type=int) or request.args.get("num_weeks", type=int) or 0
    if num_weeks < 1 or num_weeks > 52:
        flash("Number of weeks must be 1–52.", "error")
        return redirect(request.referrer or url_for("internal_scheduling.admin_shifts"))
    count = ScheduleService.repeat_shift(shift_id, num_weeks)
    flash(f"Created {count} repeat shift(s) as drafts.", "success")
    return redirect(request.referrer or url_for("internal_scheduling.admin_shifts"))


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


@internal_bp.get("/shifts/new")
@_admin_required_scheduling
def admin_shift_new():
    clients, sites = ScheduleService.list_clients_and_sites()
    job_types = ScheduleService.list_job_types()
    contractors = ScheduleService.list_contractors()
    default_date = request.args.get("date") or date.today().isoformat()
    return render_template(
        "scheduling_module/admin/shift_form.html",
        shift=None,
        clients=clients,
        sites=sites,
        job_types=job_types,
        contractors=contractors,
        default_date=default_date,
        config=_core_manifest,
    )


@internal_bp.post("/shifts/new")
@_admin_required_scheduling
def admin_shift_create():
    from flask import session
    data = {
        "contractor_id": request.form.get("contractor_id", type=int),
        "client_id": request.form.get("client_id", type=int),
        "site_id": request.form.get("site_id", type=int) or None,
        "job_type_id": request.form.get("job_type_id", type=int),
        "work_date": request.form.get("work_date"),
        "scheduled_start": request.form.get("scheduled_start"),
        "scheduled_end": request.form.get("scheduled_end"),
        "break_mins": request.form.get("break_mins", type=int) or 0,
        "notes": request.form.get("notes") or None,
        "status": request.form.get("status") or "draft",
    }
    if not data["contractor_id"] or not data["client_id"] or not data["job_type_id"] or not data["work_date"] or not data["scheduled_start"] or not data["scheduled_end"]:
        flash("Please fill in contractor, client, job type, date, and start/end times.", "error")
        return redirect(url_for("internal_scheduling.admin_shift_new"))
    try:
        data["work_date"] = date.fromisoformat(data["work_date"])
    except (TypeError, ValueError):
        flash("Invalid date.", "error")
        return redirect(url_for("internal_scheduling.admin_shift_new"))
    try:
        from datetime import timedelta
        shift_id = ScheduleService.create_shift(data)
        flash("Shift created.", "success")
        w = data["work_date"]
        monday = w - timedelta(days=w.weekday())
        return redirect(url_for("internal_scheduling.admin_schedule_week") + "?week=" + monday.strftime("%Y-%m-%d"))
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("internal_scheduling.admin_shift_new"))


@internal_bp.get("/shifts/<int:shift_id>/edit")
@_admin_required_scheduling
def admin_shift_edit(shift_id):
    shift = ScheduleService.get_shift(shift_id)
    if not shift:
        flash("Shift not found.", "error")
        return redirect(url_for("internal_scheduling.admin_shifts"))
    clients, sites = ScheduleService.list_clients_and_sites()
    job_types = ScheduleService.list_job_types()
    contractors = ScheduleService.list_contractors()
    return render_template(
        "scheduling_module/admin/shift_form.html",
        shift=shift,
        clients=clients,
        sites=sites,
        job_types=job_types,
        contractors=contractors,
        default_date=shift.get("work_date"),
        config=_core_manifest,
    )


@internal_bp.post("/shifts/<int:shift_id>/edit")
@_admin_required_scheduling
def admin_shift_update(shift_id):
    shift = ScheduleService.get_shift(shift_id)
    if not shift:
        flash("Shift not found.", "error")
        return redirect(url_for("internal_scheduling.admin_shifts"))
    data = {}
    for key in ("contractor_id", "client_id", "site_id", "job_type_id", "work_date", "scheduled_start", "scheduled_end", "break_mins", "notes", "status"):
        val = request.form.get(key)
        if val is not None:
            if key == "break_mins":
                data[key] = int(val) if val != "" else 0
            elif key == "work_date":
                try:
                    data[key] = date.fromisoformat(val) if val else None
                except (TypeError, ValueError):
                    pass
            else:
                data[key] = val
    if data:
        ScheduleService.update_shift(shift_id, data)
        flash("Shift updated.", "success")
    return redirect(request.referrer or url_for("internal_scheduling.admin_schedule_week"))


@internal_bp.get("/time-off")
@_admin_required_scheduling
def admin_time_off():
    contractor_id = request.args.get("contractor_id", type=int)
    status = request.args.get("status") or ""
    type_filter = request.args.get("type") or ""
    date_from_s = request.args.get("date_from") or ""
    date_to_s = request.args.get("date_to") or ""
    date_from = date.fromisoformat(date_from_s) if date_from_s else None
    date_to = date.fromisoformat(date_to_s) if date_to_s else None
    time_off_list = ScheduleService.list_time_off(
        contractor_id=contractor_id,
        date_from=date_from,
        date_to=date_to,
        status=status or None,
        type_filter=type_filter or None,
    )
    contractors = ScheduleService.list_contractors()
    return render_template(
        "scheduling_module/admin/time_off.html",
        time_off_list=time_off_list,
        contractors=contractors,
        contractor_id=contractor_id,
        status=status,
        type_filter=type_filter,
        date_from=date_from_s,
        date_to=date_to_s,
        config=_core_manifest,
    )


def _notify_contractor(contractor_id: int, subject: str, body: str = ""):
    """Send a portal message to the contractor (if employee_portal is available)."""
    try:
        from app.plugins.employee_portal_module.services import admin_send_message
        from flask_login import current_user
        admin_send_message(
            [contractor_id],
            subject[:255],
            body[:65535] if body else "",
            sent_by_user_id=getattr(current_user, "id", None),
            source_module="scheduling_module",
        )
    except Exception:
        pass


@internal_bp.post("/time-off/<int:tid>/approve")
@_admin_required_scheduling
def admin_time_off_approve(tid):
    from flask_login import current_user
    admin_notes = (request.form.get("admin_notes") or "").strip() or None
    user_id = getattr(current_user, "id", None)
    to = ScheduleService.get_time_off(tid)
    contractor_id = to.get("contractor_id") if to else None
    if ScheduleService.approve_time_off(tid, reviewed_by_user_id=user_id, admin_notes=admin_notes):
        if contractor_id:
            _notify_contractor(contractor_id, "Time off approved", "Your time off request has been approved.")
        flash("Time off approved.", "success")
    else:
        flash("Could not approve (maybe already processed).", "warning")
    return redirect(request.referrer or url_for("internal_scheduling.admin_time_off"))


@internal_bp.post("/time-off/<int:tid>/reject")
@_admin_required_scheduling
def admin_time_off_reject(tid):
    from flask_login import current_user
    admin_notes = (request.form.get("admin_notes") or "").strip() or None
    user_id = getattr(current_user, "id", None)
    to = ScheduleService.get_time_off(tid)
    contractor_id = to.get("contractor_id") if to else None
    if ScheduleService.reject_time_off(tid, reviewed_by_user_id=user_id, admin_notes=admin_notes):
        if contractor_id:
            _notify_contractor(contractor_id, "Time off request declined", admin_notes or "Your time off request was not approved.")
        flash("Time off rejected.", "success")
    else:
        flash("Could not reject (maybe already processed).", "warning")
    return redirect(request.referrer or url_for("internal_scheduling.admin_time_off"))


@internal_bp.get("/time-off/new")
@_admin_required_scheduling
def admin_time_off_new():
    contractors = ScheduleService.list_contractors()
    return render_template(
        "scheduling_module/admin/time_off_new.html",
        contractors=contractors,
        config=_core_manifest,
    )


@internal_bp.post("/time-off/new")
@_admin_required_scheduling
def admin_time_off_create():
    contractor_id = request.form.get("contractor_id", type=int)
    start_s = request.form.get("start_date")
    end_s = request.form.get("end_date")
    type_val = request.form.get("type") or "annual"
    reason = (request.form.get("reason") or "").strip() or None
    if not contractor_id or not start_s:
        flash("Contractor and start date required.", "error")
        return redirect(url_for("internal_scheduling.admin_time_off_new"))
    end_s = end_s or start_s
    try:
        start_date = date.fromisoformat(start_s)
        end_date = date.fromisoformat(end_s)
        if end_date < start_date:
            end_date = start_date
        ScheduleService.create_time_off_on_behalf(contractor_id, start_date, end_date, type=type_val, reason=reason, status="approved")
        flash("Time off added.", "success")
    except ValueError:
        flash("Invalid date.", "error")
    return redirect(url_for("internal_scheduling.admin_time_off"))


@internal_bp.get("/swap")
@_admin_required_scheduling
def admin_swaps():
    status = request.args.get("status")
    swaps = ScheduleService.list_swap_requests(status=status)
    return render_template(
        "scheduling_module/admin/swap.html",
        swaps=swaps,
        status=status,
        config=_core_manifest,
    )


@internal_bp.post("/swap/<int:swap_id>/approve")
@_admin_required_scheduling
def admin_swap_approve(swap_id):
    if ScheduleService.approve_swap(swap_id):
        flash("Swap approved. Shift reassigned.", "success")
    else:
        flash("Could not approve.", "warning")
    return redirect(url_for("internal_scheduling.admin_swaps"))


@internal_bp.post("/swap/<int:swap_id>/reject")
@_admin_required_scheduling
def admin_swap_reject(swap_id):
    if ScheduleService.reject_swap(swap_id):
        flash("Swap rejected.", "success")
    return redirect(url_for("internal_scheduling.admin_swaps"))


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


@internal_bp.get("/api/time-off")
@_admin_required_scheduling
def api_list_time_off():
    contractor_id = request.args.get("contractor_id", type=int)
    date_from_s = request.args.get("from")
    date_to_s = request.args.get("to")
    status = request.args.get("status")
    type_filter = request.args.get("type")
    date_from = date.fromisoformat(date_from_s) if date_from_s else None
    date_to = date.fromisoformat(date_to_s) if date_to_s else None
    rows = ScheduleService.list_time_off(
        contractor_id=contractor_id,
        date_from=date_from,
        date_to=date_to,
        status=status or None,
        type_filter=type_filter or None,
    )
    for r in rows:
        for k in ("start_date", "end_date"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return jsonify({"time_off": rows})


@internal_bp.patch("/api/time-off/<int:tid>")
@_admin_required_scheduling
def api_time_off_action(tid):
    from flask_login import current_user
    data = request.get_json() or {}
    action = data.get("action")
    admin_notes = data.get("admin_notes")
    user_id = getattr(current_user, "id", None)
    if action == "approve":
        if ScheduleService.approve_time_off(tid, reviewed_by_user_id=user_id, admin_notes=admin_notes):
            return jsonify({"ok": True})
    elif action == "reject":
        if ScheduleService.reject_time_off(tid, reviewed_by_user_id=user_id, admin_notes=admin_notes):
            return jsonify({"ok": True})
    return jsonify({"error": "Not found or invalid action"}), 400


@internal_bp.get("/api/contractors")
@_admin_required_scheduling
def api_list_contractors():
    contractors = ScheduleService.list_contractors()
    return jsonify({"contractors": contractors})


@internal_bp.get("/api/clients")
@_admin_required_scheduling
def api_list_clients():
    clients, _ = ScheduleService.list_clients_and_sites()
    return jsonify({"clients": clients})


@internal_bp.get("/api/sites")
@_admin_required_scheduling
def api_list_sites():
    _, sites = ScheduleService.list_clients_and_sites()
    return jsonify({"sites": sites})


@internal_bp.get("/api/job-types")
@_admin_required_scheduling
def api_list_job_types():
    job_types = ScheduleService.list_job_types()
    return jsonify({"job_types": job_types})


@internal_bp.get("/api/suggest-contractors")
@_admin_required_scheduling
def api_suggest_contractors():
    work_date_s = request.args.get("date")
    start_time_s = request.args.get("start")
    end_time_s = request.args.get("end")
    client_id = request.args.get("client_id", type=int)
    job_type_id = request.args.get("job_type_id", type=int)
    if not work_date_s or not start_time_s or not end_time_s:
        return jsonify({"error": "date, start, end required (e.g. date=2025-03-10&start=09:00&end=17:00)"}), 400
    try:
        work_date = date.fromisoformat(work_date_s)
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400
    start_time = _parse_time(start_time_s)
    end_time = _parse_time(end_time_s)
    if start_time is None or end_time is None:
        return jsonify({"error": "Invalid start or end time (use HH:MM)"}), 400
    contractors = ScheduleService.suggest_available_contractors(
        work_date=work_date,
        start_time=start_time,
        end_time=end_time,
        client_id=client_id,
        job_type_id=job_type_id,
    )
    return jsonify({"contractors": contractors})


@internal_bp.get("/api/check-conflicts")
@_admin_required_scheduling
def api_check_conflicts():
    contractor_id = request.args.get("contractor_id", type=int)
    work_date_s = request.args.get("date")
    start_time_s = request.args.get("start")
    end_time_s = request.args.get("end")
    exclude_shift_id = request.args.get("exclude_shift_id", type=int)
    if not contractor_id or not work_date_s or not start_time_s or not end_time_s:
        return jsonify({"error": "contractor_id, date, start, end required"}), 400
    try:
        work_date = date.fromisoformat(work_date_s)
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400
    start_time = _parse_time(start_time_s)
    end_time = _parse_time(end_time_s)
    if start_time is None or end_time is None:
        return jsonify({"error": "Invalid start or end time (use HH:MM)"}), 400
    conflicts = ScheduleService.check_shift_conflicts(
        contractor_id=contractor_id,
        work_date=work_date,
        scheduled_start=start_time,
        scheduled_end=end_time,
        exclude_shift_id=exclude_shift_id,
    )
    return jsonify({"conflicts": conflicts})


def get_blueprint():
    return internal_bp


def get_public_blueprint():
    return public_bp
