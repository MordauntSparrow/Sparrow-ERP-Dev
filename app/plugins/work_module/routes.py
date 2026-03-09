import os
import uuid
from datetime import datetime
from functools import wraps
from flask import (
    Blueprint,
    request,
    jsonify,
    render_template,
    redirect,
    session,
    current_app,
)
from app.objects import PluginManager
from . import services as work_services

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
    "internal_work",
    __name__,
    url_prefix="/plugin/work_module",
    template_folder=_template,
)
public_bp = Blueprint(
    "public_work",
    __name__,
    url_prefix="/work",
    template_folder=_template,
)


def _staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("tb_user"):
            return redirect("/employee-portal/login?next=" + request.path)
        return view(*args, **kwargs)
    return wrapped


def _current_contractor_id():
    u = session.get("tb_user")
    if not u or u.get("id") is None:
        return None
    return int(u["id"])


# ---------- Public: My day (list of stops from scheduling) ----------


@public_bp.get("/")
@_staff_required
def index():
    cid = _current_contractor_id()
    if not cid:
        return redirect("/employee-portal/login?next=/work/")
    stops = work_services.get_my_stops_for_today(cid)
    return render_template(
        "work_module/public/index.html",
        stops=stops,
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


# ---------- Public: Record stop (times, notes, photos) ----------


@public_bp.get("/stop/<int:shift_id>")
@_staff_required
def stop_page(shift_id):
    cid = _current_contractor_id()
    if not cid:
        return redirect("/employee-portal/login?next=/work/stop/" + str(shift_id))
    shift = work_services.get_shift_for_stop(shift_id, cid)
    if not shift:
        return redirect("/work/")
    photos = work_services.list_photos_for_shift(shift_id)
    return render_template(
        "work_module/public/stop.html",
        shift=shift,
        photos=photos,
        website_settings=_get_website_settings(),
        config=_core_manifest,
    )


@public_bp.post("/api/stop/<int:shift_id>/record")
@_staff_required
def api_record_stop(shift_id):
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or request.form
    actual_start = data.get("actual_start")
    actual_end = data.get("actual_end")
    notes = data.get("notes")
    if actual_start and isinstance(actual_start, str) and len(actual_start) <= 8:
        from datetime import time
        parts = actual_start.split(":")
        actual_start = time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0, int(parts[2]) if len(parts) > 2 else 0)
    else:
        actual_start = None
    if actual_end and isinstance(actual_end, str) and len(actual_end) <= 8:
        from datetime import time
        parts = actual_end.split(":")
        actual_end = time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0, int(parts[2]) if len(parts) > 2 else 0)
    else:
        actual_end = None
    ok = work_services.record_stop(shift_id, cid, actual_start=actual_start, actual_end=actual_end, notes=notes)
    if not ok:
        return jsonify({"error": "Forbidden or not found"}), 403
    return jsonify({"ok": True})


@public_bp.post("/api/stop/<int:shift_id>/photo")
@_staff_required
def api_upload_photo(shift_id):
    cid = _current_contractor_id()
    if not cid:
        return jsonify({"error": "Not authenticated"}), 401
    shift = work_services.get_shift_for_stop(shift_id, cid)
    if not shift:
        return jsonify({"error": "Not found"}), 404
    file = request.files.get("photo")
    if not file or not file.filename:
        return jsonify({"error": "No file"}), 400
    base = getattr(current_app, "root_path", None) or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    upload_dir = os.path.join(base, "static", "uploads", "work_photos")
    os.makedirs(upload_dir, exist_ok=True)
    ext = os.path.splitext(file.filename)[1] or ".jpg"
    safe_name = f"{shift_id}_{uuid.uuid4().hex[:12]}{ext}"
    rel_path = os.path.join("uploads", "work_photos", safe_name)
    full_path = os.path.join(base, "static", rel_path)
    file.save(full_path)
    caption = request.form.get("caption")
    pid = work_services.add_photo(shift_id, cid, rel_path, file_name=file.filename, mime_type=file.content_type, caption=caption)
    return jsonify({"ok": True, "id": pid, "path": "/static/" + rel_path})


def get_blueprint():
    return internal_bp


def get_public_blueprint():
    return public_bp
