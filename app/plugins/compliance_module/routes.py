import os
from functools import wraps
from flask import Blueprint, redirect, render_template, request, session, url_for
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
internal_bp = Blueprint("internal_compliance", __name__, url_prefix="/plugin/compliance_module", template_folder=_template)
public_bp = Blueprint("public_compliance", __name__, url_prefix="/compliance", template_folder=_template)


@internal_bp.get("/")
def admin_index():
    return redirect("/")


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
