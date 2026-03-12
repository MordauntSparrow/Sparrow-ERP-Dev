import os
import re
import uuid
from functools import wraps
from flask import (
    Blueprint,
    request,
    redirect,
    render_template,
    session,
    flash,
    url_for,
    send_file,
    current_app,
)
from flask_login import current_user, login_required
from app.objects import PluginManager
from . import services as essentials_services

_plugin_manager = PluginManager(os.path.abspath("app/plugins"))
_core_manifest = _plugin_manager.get_core_manifest() or {}
_template = os.path.join(os.path.dirname(__file__), "templates")
_uploads_dir = os.path.join(os.path.dirname(__file__), "uploads")

internal_bp = Blueprint("internal_essentials", __name__, url_prefix="/plugin/essentials_module", template_folder=_template)
public_bp = Blueprint("public_essentials", __name__, url_prefix="/essentials", template_folder=_template)


def _staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("tb_user"):
            return redirect("/employee-portal/login?next=" + request.path)
        return view(*args, **kwargs)
    return wrapped


def _admin_required_essentials(view):
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
    return s.strip("-") or "document"


def _safe_filename(name: str) -> str:
    if not name or not name.strip():
        return "document"
    base = os.path.basename(name).strip()
    base = re.sub(r"[^\w.\-]", "_", base)
    return base[:200] or "document"


def _get_website_settings():
    try:
        from app.plugins.website_module.routes import get_website_settings
        return get_website_settings()
    except Exception:
        pass
    return {}


# ---------- Public (portal) ----------


@public_bp.get("/")
@_staff_required
def public_index():
    docs = essentials_services.list_documents(active_only=True)
    return render_template(
        "essentials_module/public/index.html",
        module_name="Essentials",
        module_description="Essential documents and reference material.",
        documents=docs,
        config=_core_manifest,
        website_settings=_get_website_settings(),
    )


@public_bp.get("/doc/<slug>")
@_staff_required
def public_view(slug):
    doc = essentials_services.get_document_by_slug(slug, active_only=True)
    if not doc:
        flash("Document not found.", "error")
        return redirect(url_for("public_essentials.public_index"))
    return render_template(
        "essentials_module/public/view.html",
        document=doc,
        config=_core_manifest,
        website_settings=_get_website_settings(),
    )


@public_bp.get("/file/<int:doc_id>")
@_staff_required
def public_file(doc_id):
    doc = essentials_services.get_document_by_id(doc_id)
    if not doc or not doc.get("file_path") or not doc.get("file_name"):
        flash("File not found.", "error")
        return redirect(url_for("public_essentials.public_index"))
    full_path = os.path.join(_uploads_dir, doc["file_path"])
    if not os.path.isfile(full_path):
        flash("File not found.", "error")
        return redirect(url_for("public_essentials.public_index"))
    return send_file(
        full_path,
        as_attachment=True,
        download_name=doc["file_name"],
        mimetype="application/octet-stream",
    )


# ---------- Admin ----------


@internal_bp.get("/")
@login_required
@_admin_required_essentials
def admin_index():
    return render_template(
        "essentials_module/admin/index.html",
        module_name="Essentials",
        module_description="Upload or paste essential documents (e.g. PRDs) for staff to view on the portal.",
        config=_core_manifest,
    )


@internal_bp.get("/documents")
@login_required
@_admin_required_essentials
def admin_documents():
    active_only = request.args.get("active") != "all"
    docs = essentials_services.list_documents(active_only=active_only)
    return render_template(
        "essentials_module/admin/documents.html",
        documents=docs,
        active_only=active_only,
        config=_core_manifest,
    )


@internal_bp.get("/documents/new")
@login_required
@_admin_required_essentials
def admin_document_new():
    return render_template(
        "essentials_module/admin/document_form.html",
        document=None,
        config=_core_manifest,
    )


@internal_bp.post("/documents/new")
@login_required
@_admin_required_essentials
def admin_document_create():
    title = (request.form.get("title") or "").strip()
    slug = (request.form.get("slug") or "").strip() or _slugify(title)
    summary = (request.form.get("summary") or "").strip() or None
    content = (request.form.get("content") or "").strip() or None
    try:
        display_order = int(request.form.get("display_order") or 0)
    except ValueError:
        display_order = 0
    active = request.form.get("active") == "1"
    if not title:
        flash("Title required.", "error")
        return redirect(url_for("internal_essentials.admin_document_new"))
    try:
        doc_id = essentials_services.create_document(
            title=title, slug=slug, summary=summary, content=content,
            file_path=None, file_name=None, active=active, display_order=display_order,
        )
        # Optional file upload (after we have an id)
        f = request.files.get("file")
        if f and f.filename:
            os.makedirs(os.path.join(_uploads_dir, str(doc_id)), exist_ok=True)
            safe = _safe_filename(f.filename)
            unique = f"{uuid.uuid4().hex[:8]}_{safe}"
            path = os.path.join(_uploads_dir, str(doc_id), unique)
            f.save(path)
            essentials_services.update_document(
                doc_id,
                file_path=f"{doc_id}/{unique}",
                file_name=f.filename.strip()[:255],
            )
        flash("Document created.", "success")
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("internal_essentials.admin_document_new"))
    return redirect(url_for("internal_essentials.admin_documents"))


@internal_bp.get("/documents/<int:doc_id>/edit")
@login_required
@_admin_required_essentials
def admin_document_edit(doc_id):
    doc = essentials_services.get_document_by_id(doc_id)
    if not doc:
        flash("Document not found.", "error")
        return redirect(url_for("internal_essentials.admin_documents"))
    return render_template(
        "essentials_module/admin/document_form.html",
        document=doc,
        config=_core_manifest,
    )


@internal_bp.post("/documents/<int:doc_id>/edit")
@login_required
@_admin_required_essentials
def admin_document_update(doc_id):
    doc = essentials_services.get_document_by_id(doc_id)
    if not doc:
        flash("Document not found.", "error")
        return redirect(url_for("internal_essentials.admin_documents"))
    title = (request.form.get("title") or "").strip()
    slug = (request.form.get("slug") or "").strip()
    summary = (request.form.get("summary") or "").strip() or None
    content = (request.form.get("content") or "").strip() or None
    try:
        display_order = int(request.form.get("display_order") or 0)
    except ValueError:
        display_order = doc.get("display_order", 0)
    active = request.form.get("active") == "1" if "active" in request.form else None
    if title:
        essentials_services.update_document(
            doc_id, title=title, slug=slug or None, summary=summary, content=content,
            active=active, display_order=display_order,
        )
        # Optional new file upload (replaces existing)
        f = request.files.get("file")
        if f and f.filename:
            os.makedirs(os.path.join(_uploads_dir, str(doc_id)), exist_ok=True)
            safe = _safe_filename(f.filename)
            unique = f"{uuid.uuid4().hex[:8]}_{safe}"
            path = os.path.join(_uploads_dir, str(doc_id), unique)
            f.save(path)
            essentials_services.update_document(
                doc_id,
                file_path=f"{doc_id}/{unique}",
                file_name=f.filename.strip()[:255],
            )
        flash("Document updated.", "success")
    return redirect(url_for("internal_essentials.admin_documents"))


@internal_bp.post("/documents/<int:doc_id>/delete")
@login_required
@_admin_required_essentials
def admin_document_delete(doc_id):
    doc = essentials_services.get_document_by_id(doc_id)
    if doc and doc.get("file_path"):
        import shutil
        dir_path = os.path.join(_uploads_dir, str(doc_id))
        if os.path.isdir(dir_path):
            try:
                shutil.rmtree(dir_path)
            except Exception:
                pass
    if essentials_services.delete_document(doc_id):
        flash("Document deleted.", "success")
    else:
        flash("Document not found.", "error")
    return redirect(url_for("internal_essentials.admin_documents"))


def get_blueprint():
    return internal_bp


def get_public_blueprint():
    return public_bp
