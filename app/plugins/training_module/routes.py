import os
import re
from functools import wraps
from flask import (
    Blueprint,
    request,
    redirect,
    render_template,
    session,
    flash,
    url_for,
)
from flask_login import current_user, login_required
from app.objects import PluginManager
from . import services as training_services

_plugin_manager = PluginManager(os.path.abspath("app/plugins"))
_core_manifest = _plugin_manager.get_core_manifest() or {}

_template = os.path.join(os.path.dirname(__file__), "templates")
internal_bp = Blueprint("internal_training", __name__, url_prefix="/plugin/training_module", template_folder=_template)
public_bp = Blueprint("public_training", __name__, url_prefix="/training", template_folder=_template)


def _staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("tb_user"):
            return redirect("/employee-portal/login?next=" + request.path)
        return view(*args, **kwargs)
    return wrapped


def _current_contractor_id():
    u = session.get("tb_user")
    return int(u["id"]) if u and u.get("id") is not None else None


def _admin_required_training(view):
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
    return s.strip("-") or "item"


def _get_website_settings():
    try:
        from app.plugins.website_module.routes import get_website_settings
        return get_website_settings()
    except Exception:
        pass
    return {}


# ---------- Public (contractor) ----------


@public_bp.get("/")
@_staff_required
def public_index():
    cid = _current_contractor_id()
    if not cid:
        return redirect("/employee-portal/")
    assignments = training_services.TrainingService.list_assignments(contractor_id=cid, include_completed=True)
    return render_template(
        "training_module/public/index.html",
        module_name="Training",
        module_description="View and complete your assigned training.",
        assignments=assignments,
        config=_core_manifest,
        website_settings=_get_website_settings(),
    )


@public_bp.get("/item/<int:assignment_id>")
@_staff_required
def view_item(assignment_id):
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_training.public_index"))
    assignment = training_services.TrainingService.get_assignment(assignment_id, contractor_id=cid)
    if not assignment:
        flash("Not found.", "error")
        return redirect(url_for("public_training.public_index"))
    return render_template(
        "training_module/public/view.html",
        assignment=assignment,
        config=_core_manifest,
        website_settings=_get_website_settings(),
    )


@public_bp.post("/item/<int:assignment_id>/complete")
@_staff_required
def complete_item(assignment_id):
    cid = _current_contractor_id()
    if not cid:
        return redirect(url_for("public_training.public_index"))
    notes = (request.form.get("notes") or "").strip() or None
    if training_services.TrainingService.mark_complete(assignment_id, cid, notes=notes):
        flash("Training marked complete. Thank you.", "success")
    else:
        flash("Could not complete.", "warning")
    return redirect(url_for("public_training.public_index"))


# ---------- Admin ----------


@internal_bp.get("/")
@login_required
@_admin_required_training
def admin_index():
    return render_template(
        "training_module/admin/index.html",
        module_name="Training",
        module_description="Manage training items, assignments, and completions.",
        config=_core_manifest,
    )


@internal_bp.get("/items")
@login_required
@_admin_required_training
def admin_items():
    items = training_services.TrainingService.list_items(active_only=False)
    return render_template(
        "training_module/admin/items.html",
        items=items,
        config=_core_manifest,
    )


@internal_bp.get("/items/new")
@login_required
@_admin_required_training
def admin_item_new():
    return render_template(
        "training_module/admin/item_form.html",
        item=None,
        config=_core_manifest,
    )


@internal_bp.post("/items/new")
@login_required
@_admin_required_training
def admin_item_create():
    title = (request.form.get("title") or "").strip()
    slug = (request.form.get("slug") or "").strip() or _slugify(title)
    summary = (request.form.get("summary") or "").strip() or None
    content = (request.form.get("content") or "").strip() or None
    item_type = request.form.get("item_type") or "document"
    external_url = (request.form.get("external_url") or "").strip() or None
    if not title:
        flash("Title required.", "error")
        return redirect(url_for("internal_training.admin_item_new"))
    try:
        training_services.TrainingService.create_item(
            title=title, slug=slug, summary=summary, content=content,
            item_type=item_type, external_url=external_url,
        )
        flash("Training item created.", "success")
        return redirect(url_for("internal_training.admin_items"))
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("internal_training.admin_item_new"))


@internal_bp.get("/items/<int:item_id>/edit")
@login_required
@_admin_required_training
def admin_item_edit(item_id):
    item = training_services.TrainingService.get_item(item_id)
    if not item:
        flash("Not found.", "error")
        return redirect(url_for("internal_training.admin_items"))
    return render_template(
        "training_module/admin/item_form.html",
        item=item,
        config=_core_manifest,
    )


@internal_bp.post("/items/<int:item_id>/edit")
@login_required
@_admin_required_training
def admin_item_update(item_id):
    item = training_services.TrainingService.get_item(item_id)
    if not item:
        flash("Not found.", "error")
        return redirect(url_for("internal_training.admin_items"))
    title = (request.form.get("title") or "").strip()
    slug = (request.form.get("slug") or "").strip()
    summary = (request.form.get("summary") or "").strip() or None
    content = (request.form.get("content") or "").strip() or None
    item_type = request.form.get("item_type")
    external_url = (request.form.get("external_url") or "").strip() or None
    active = None
    if "active" in request.form:
        active = request.form.get("active") == "1"
    if title:
        training_services.TrainingService.update_item(
            item_id, title=title, slug=slug or None, summary=summary, content=content,
            item_type=item_type, external_url=external_url, active=active,
        )
        flash("Training item updated.", "success")
    return redirect(url_for("internal_training.admin_items"))


@internal_bp.get("/assignments")
@login_required
@_admin_required_training
def admin_assignments():
    contractor_id = request.args.get("contractor_id", type=int)
    item_id = request.args.get("item_id", type=int)
    assignments = training_services.TrainingService.list_assignments(
        contractor_id=contractor_id,
        training_item_id=item_id,
        include_completed=True,
    )
    contractors = training_services.TrainingService.list_contractors()
    items = training_services.TrainingService.list_items(active_only=False)
    return render_template(
        "training_module/admin/assignments.html",
        assignments=assignments,
        contractors=contractors,
        items=items,
        contractor_id=contractor_id,
        item_id=item_id,
        config=_core_manifest,
    )


@internal_bp.get("/assignments/new")
@login_required
@_admin_required_training
def admin_assignment_new():
    contractors = training_services.TrainingService.list_contractors()
    items = training_services.TrainingService.list_items(active_only=True)
    return render_template(
        "training_module/admin/assignment_form.html",
        contractors=contractors,
        items=items,
        config=_core_manifest,
    )


@internal_bp.post("/assignments/new")
@login_required
@_admin_required_training
def admin_assignment_create():
    item_id = request.form.get("training_item_id", type=int)
    contractor_id = request.form.get("contractor_id", type=int)
    due_s = request.form.get("due_date") or None
    mandatory = request.form.get("mandatory") == "1"
    if not item_id or not contractor_id:
        flash("Item and contractor required.", "error")
        return redirect(url_for("internal_training.admin_assignment_new"))
    from datetime import date
    due_date = date.fromisoformat(due_s) if due_s else None
    try:
        training_services.TrainingService.add_assignment(
            training_item_id=item_id,
            contractor_id=contractor_id,
            due_date=due_date,
            mandatory=mandatory,
            assigned_by_user_id=getattr(current_user, "id", None),
        )
        flash("Assignment created.", "success")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("internal_training.admin_assignments"))


@internal_bp.get("/completions")
@login_required
@_admin_required_training
def admin_completions():
    from datetime import date, timedelta
    item_id = request.args.get("item_id", type=int)
    contractor_id = request.args.get("contractor_id", type=int)
    date_from_s = request.args.get("date_from")
    date_to_s = request.args.get("date_to")
    date_from = date.fromisoformat(date_from_s) if date_from_s else (date.today() - timedelta(days=30))
    date_to = date.fromisoformat(date_to_s) if date_to_s else date.today()
    rows = training_services.TrainingService.list_completions(
        training_item_id=item_id,
        contractor_id=contractor_id,
        date_from=date_from,
        date_to=date_to,
    )
    items = training_services.TrainingService.list_items(active_only=False)
    contractors = training_services.TrainingService.list_contractors()
    return render_template(
        "training_module/admin/completions.html",
        rows=rows,
        items=items,
        contractors=contractors,
        item_id=item_id,
        contractor_id=contractor_id,
        date_from=date_from,
        date_to=date_to,
        config=_core_manifest,
    )


def get_blueprint():
    return internal_bp


def get_public_blueprint():
    return public_bp
