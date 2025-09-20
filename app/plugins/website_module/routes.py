from flask import (
    Blueprint,
    render_template,
    request,
    jsonify,
    make_response,
    url_for,
    render_template_string
)
from jinja2 import TemplateNotFound
import os
import importlib.util
from app.objects import PluginManager, EmailManager
import json
from .objects import *
from pathlib import Path

from flask import Blueprint, render_template, Response

# Define the blueprint for website module's public routes
website_public_routes = Blueprint(
    'website_public',
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), 'templates', 'public'),
    url_prefix='/',
    static_folder=os.path.join(os.path.dirname(__file__), 'static'),
    static_url_path='/website_module_static'
)
# Define the blueprint for website module's public routes
website_public_added_routes = Blueprint(
    'website_public_added',
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), 'templates', 'public'),
    url_prefix='/',
    static_folder=os.path.join(os.path.dirname(__file__), 'static'),
    static_url_path='/website_module_static'
)
templates_dir = os.path.join(os.path.dirname(__file__), 'templates/public')
# Setup Analytics Manager for the website module
module_dir = os.path.dirname(os.path.abspath(__file__))
# Initialise analytics safely
analytics = None
try:
    module_dir = os.path.dirname(os.path.abspath(__file__))
    from .objects import ensure_data_folder
    data_dir = ensure_data_folder(module_dir)

    analytics = AnalyticsManager(data_dir)
except Exception as e:
    print(f"[Website] Analytics disabled: {e}")
    analytics = None


def get_core_manifest():
    print(os.path.abspath('app/plugins'))
    plugin_manager = PluginManager(os.path.abspath('app/plugins'))
    core_manifest = plugin_manager.get_core_manifest()
    return core_manifest


# Route for root page ('/')
@website_public_routes.route('/')
def root_page():
    """
    Serve the root page (index.html), or return a 404 if it doesn't exist.
    """
    pages_file = os.path.join(os.path.dirname(__file__), 'pages.json')
    templates_dir_local = os.path.join(os.path.dirname(__file__), 'templates', 'public')

    # Load pages.json
    if os.path.exists(pages_file):
        with open(pages_file, 'r') as f:
            pages = json.load(f)
    else:
        pages = []

    # Look for the page with route '/'
    page_data = next((p for p in pages if p['route'] == '/'), None)

    # Record the page view with analytics (safe)
    if analytics:
        try:
            analytics.record_page_view(
                page=request.path,
                ip_address=request.remote_addr,
                user_agent=request.user_agent.string,
                referrer=request.referrer
            )
        except Exception as e:
            print(f"[Website] Analytics error: {e}")

    if page_data:
        template_file = 'index.html'
        template_path = os.path.join(templates_dir_local, template_file)
        if os.path.exists(template_path):
            return render_template(template_file, page_data=page_data, config=get_core_manifest(), pages=pages)
        else:
            return "Home page file is missing.", 404
    else:
        return "Home page not found.", 404


@website_public_routes.route('/sitemap')
@website_public_routes.route('/sitemap.xml')
def sitemap():
    """
    Dynamically generate an XML sitemap based on pages.json.
    """
    # Locate the pages.json file
    pages_file = os.path.join(os.path.dirname(__file__), 'pages.json')

    if os.path.exists(pages_file):
        with open(pages_file, 'r') as f:
            pages = json.load(f)
    else:
        # If pages.json is missing, return a 404 or an empty sitemap
        return "pages.json not found.", 404

    # Build XML header
    sitemap_xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    sitemap_xml.append(
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    )

    # Base URL (e.g. https://www.example.com/)
    base_url = request.url_root.rstrip('/')

    for p in pages:
        route = p.get('route', '').strip()
        if not route:
            continue

        # Compute the full <loc> for this page
        if route == '/':
            loc = base_url + '/'
        else:
            # Ensure leading slash
            if not route.startswith('/'):
                route = '/' + route
            loc = base_url + route

        sitemap_xml.append('  <url>')
        sitemap_xml.append(f'    <loc>{loc}</loc>')
        sitemap_xml.append('  </url>')

    sitemap_xml.append('</urlset>')
    xml_str = "\n".join(sitemap_xml)

    return Response(xml_str, mimetype='application/xml')


@website_public_routes.route('/<path:page_route>')
def custom_page(page_route):
    pages_file = os.path.join(os.path.dirname(__file__), 'pages.json')
    if os.path.exists(pages_file):
        with open(pages_file, 'r') as f:
            pages = json.load(f)
    else:
        pages = []

    page_data = next((p for p in pages if p['route'].strip('/') == page_route), None)

    # Record the page view with analytics (safe)
    if analytics:
        try:
            analytics.record_page_view(
                page=request.path,
                ip_address=request.remote_addr,
                user_agent=request.user_agent.string,
                referrer=request.referrer
            )
        except Exception as e:
            print(f"[Website] Analytics error: {e}")

    if page_data:
        # Here, we do NOT prepend "public"
        # because the blueprint is configured to look in templates/public
        template_file = f"{page_route.strip('/')}.html"
        return render_template(template_file, pages=pages, page_data=page_data, config=get_core_manifest())
    else:
        return "Page Not Found", 404


@website_public_routes.route('/submit_form', methods=['POST'])
def form_submit():
    """
    A generic endpoint for processing dynamic form submissions.
    This route collects all form data, applies centralized spam protection (honeypot and Turnstile),
    records the submission using the ContactFormSubmissionManager, and then processes it.
    """
    plugin_manager = PluginManager(os.path.abspath('app/plugins'))
    core_manifest = plugin_manager.get_core_manifest()

    spam_protector = SpamProtection(core_manifest)
    is_spam, reason = spam_protector.is_spam(request.form)
    if is_spam:
        flash("Spam detected: " + reason, "danger")
        return redirect(request.referrer or url_for('website_public.root_page'))

    # Collect form data
    submission_data = request.form.to_dict()
    submission_data['remote_ip'] = request.remote_addr
    submission_data['timestamp'] = PluginManager.get_current_timestamp() if hasattr(PluginManager, "get_current_timestamp") else None

    module_dir = os.path.dirname(os.path.abspath(__file__))
    from .objects import ensure_data_folder
    data_dir = ensure_data_folder(module_dir)
    submission_manager = ContactFormSubmissionManager(data_dir)

    # Instead of record_submission, call process_submission
    success = submission_manager.process_submission(submission_data)
    if success:
        flash("Your submission has been received.", "success")
    else:
        flash("Submission saved, but processing was unsuccessful. Check logs for details.", "warning")

    return redirect(request.referrer or url_for('website_public.root_page'))


from flask import Blueprint, render_template, request, redirect, url_for, flash
import os
import json
from .objects import ContactFormConfigManager

# Define the paths for configuration files
PAGES_JSON_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'pages.json'))

# Resolve the absolute path to the admin templates folder
admin_template_folder = os.path.join(os.path.dirname(__file__), 'templates')
print(f"[Website Admin] Admin template folder: {admin_template_folder}")

website_admin_routes = Blueprint(
    'website_admin_routes',
    __name__,
    url_prefix='/plugin/website_module',
    template_folder=admin_template_folder  # Absolute path to the admin templates
)

def get_blueprint():
    return website_admin_routes


# ------------------------
# Pages persistence
# ------------------------
PAGES_JSON_PATH = os.path.join(os.path.dirname(__file__), "pages.json")

def load_pages():
    """Loads pages from pages.json."""
    if not os.path.exists(PAGES_JSON_PATH):
        return []
    with open(PAGES_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_pages(pages):
    """Saves pages to pages.json."""
    with open(PAGES_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(pages, f, indent=4)


# ------------------------
# Helpers
# ------------------------
def _module_dir():
    """Returns the absolute path to this module directory."""
    return os.path.dirname(os.path.abspath(__file__))

def _data_dir():
    """Returns the data folder path, creating it if necessary."""
    return ensure_data_folder(_module_dir())

def _normalize_route(route_str: str) -> str:
    """Ensure a leading slash, collapse whitespace, and strip trailing spaces."""
    route_str = (route_str or '').strip()
    return '/' + route_str.lstrip('/')

def _load_page_by_route(store, route_str: str):
    """Scan builder store for a page with the given route. Return page dict or None."""
    pages_dir = os.path.join(store.pages_dir)
    if not os.path.isdir(pages_dir):
        return None
    norm = _normalize_route(route_str)
    for fname in os.listdir(pages_dir):
        if not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(pages_dir, fname), 'r', encoding='utf-8') as f:
                pj = json.load(f)
        except Exception:
            continue
        if pj.get('route') == norm:
            return pj
    return None

def _get_or_create_page(store, route_str: str):
    """Return existing builder page for route or create a new one with sensible defaults."""
    page = _load_page_by_route(store, route_str)
    if page:
        return page
    norm = _normalize_route(route_str)
    title = 'Home' if norm == '/' else norm.strip('/').replace('-', ' ').replace('_', ' ').title()
    return store.create(title=title, route=norm)


# ------------------------
# Builder UI
# ------------------------
@website_admin_routes.route('/builder', methods=['GET'], endpoint='builder_ui_root')
def builder_ui_root():
    """Open builder for the home route ('/'). Auto-creates if missing."""
    app_root = os.path.abspath(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..')
    )
    plugins_dir = os.path.abspath(os.path.join(app_root, 'plugins'))
    plugin_manager = PluginManager(plugins_dir)
    core_manifest = plugin_manager.get_core_manifest()

    store = BuilderPageStore(_data_dir())
    registry = BlocksRegistry()
    page = _get_or_create_page(store, '/')
    return render_template(
        'builder/builder.html',
        title=f"Website Builder — {page.get('title', '/')}",
        page=page,
        blocks_registry=registry.safe_registry(),
        config=core_manifest
    )


@website_admin_routes.route('/builder/<path:page_route>', methods=['GET'], endpoint='builder_ui')
def builder_ui(page_route):
    """Website Builder UI for a given route. Auto-creates if missing."""
    app_root = os.path.abspath(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..')
    )
    plugins_dir = os.path.abspath(os.path.join(app_root, 'plugins'))
    plugin_manager = PluginManager(plugins_dir)
    core_manifest = plugin_manager.get_core_manifest()

    store = BuilderPageStore(_data_dir())
    registry = BlocksRegistry()
    page = _get_or_create_page(store, page_route)
    return render_template(
        'builder/builder.html',
        title=f"Website Builder — {page.get('title', page_route)}",
        page=page,
        blocks_registry=registry.safe_registry(),
        config=core_manifest
    )


# ------------------------
# Live preview (iframe)
# ------------------------
@website_admin_routes.route('/builder/preview', methods=['GET'], endpoint='builder_preview_root')
@website_admin_routes.route('/builder/<path:page_route>/preview', methods=['GET'], endpoint='builder_preview')
def builder_preview(page_route: str = ''):
    """Preview live merged builder output with public HTML templates."""
    effective_route = '/' if not page_route else '/' + page_route.lstrip('/')

    app_root = os.path.abspath(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..')
    )
    plugins_dir = os.path.abspath(os.path.join(app_root, 'plugins'))
    plugin_manager = PluginManager(plugins_dir)
    core_manifest = plugin_manager.get_core_manifest()

    store = BuilderPageStore(_data_dir())
    renderer = BuilderRenderer()

    # Builder page/state
    page = _get_or_create_page(store, effective_route)
    builder_html = renderer.render_page(page) or ''

    # Manual HTML render for public template
    pages_json_path = os.path.join(os.path.dirname(__file__), 'pages.json')
    try:
        with open(pages_json_path, 'r', encoding='utf-8') as f:
            pages = json.load(f)
    except Exception as e:
        print(f"[Preview] Failed to load public pages.json at {pages_json_path}: {e}")
        pages = []

    page_data = next((p for p in pages if p.get('route') == effective_route), None)

    manual_html = ''
    if page_data:
        public_dir = os.path.join(os.path.dirname(__file__), 'templates', 'public')
        tpl_file = 'index.html' if effective_route == '/' else f"{effective_route.strip('/')}.html"
        candidate = os.path.join(public_dir, tpl_file)

        # Debug diagnostics
        print(f"[Preview] effective_route={effective_route}")
        print(f"[Preview] public_dir={public_dir}")
        print(f"[Preview] tpl_file={tpl_file}")
        print(f"[Preview] candidate_path={candidate}")
        print(f"[Preview] candidate_exists={os.path.exists(candidate)}")

        try:
            files = [f for f in os.listdir(public_dir) if f.endswith('.html')]
            print(f"[Preview] public templates list={files}")
        except Exception as e:
            print(f"[Preview] listdir error for {public_dir}: {e}")

        try:
            manual_html = render_template(
                tpl_file,
                page_data=page_data,
                pages=pages,
                config=core_manifest
            )
            print(f"[Preview] render_template OK for {tpl_file}")
        except Exception as e:
            print(f"[Preview] Manual template render failed for {tpl_file}: {e}")
            # Fallback: read file and render with Flask's environment so url_for works
            try:
                with open(candidate, 'r', encoding='utf-8') as f:
                    tpl_source = f.read()
                # Hint Flask about template name to keep extends/includes relative
                manual_html = render_template_string(
                    tpl_source,
                    page_data=page_data,
                    pages=pages,
                    config=core_manifest
                )
                print(f"[Preview] Fallback render_template_string OK for {candidate}")
            except Exception as ee:
                print(f"[Preview] Fallback render_template_string error for {candidate}: {ee}")

    # Merge policy
    merge_mode = (page.get('settings') or {}).get('merge_mode', 'augment')
    if not builder_html:
        final_render = manual_html
    else:
        if merge_mode == 'replace':
            final_render = builder_html
        elif merge_mode == 'prepend':
            final_render = (builder_html or '') + (manual_html or '')
        else:
            final_render = (manual_html or '') + (builder_html or '')

    html = render_template(
        'builder/preview_base.html',
        title=page.get('seo', {}).get('title') or page.get('title') or effective_route,
        page=page,
        rendered=final_render,
        config=core_manifest,
        pages=pages
    )
    resp = make_response(html)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp
# ------------------------
# Save draft (by route)
# ------------------------
@website_admin_routes.route('/builder/<path:page_route>/save', methods=['POST'], endpoint='builder_save')
def builder_save(page_route):
    """Saves a builder page draft including nested blocks and validates them."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': 'Invalid JSON payload'}), 400

    data_dir = _data_dir()
    store = BuilderPageStore(data_dir)
    registry = BlocksRegistry()
    page = _get_or_create_page(store, page_route)

    def clean_block(blk):
        btype = blk.get('type')
        if not btype or not registry.exists(btype):
            return None

        merged = registry.normalize(btype, blk.get('props'))
        ok, err = registry.validate(btype, merged)
        if not ok:
            raise ValueError(f"{btype}: {err}")

        if btype == 'section':
            nested = []
            for child in blk.get('props', {}).get('blocks', []):
                c = clean_block(child)
                if c:
                    nested.append(c)
            return {'type': btype, 'props': {**merged, 'blocks': nested}}

        if btype == 'columns':
            cols = []
            for col in blk.get('props', {}).get('columns', []):
                cleaned_col = []
                for child in col:
                    c = clean_block(child)
                    if c:
                        cleaned_col.append(c)
                cols.append(cleaned_col)
            return {'type': btype, 'props': {**merged, 'columns': cols}}

        return {'type': btype, 'props': merged}

    try:
        cleaned_blocks = []
        for blk in payload.get('blocks', []):
            c = clean_block(blk)
            if c:
                cleaned_blocks.append(c)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    page['blocks'] = cleaned_blocks
    page['vars'] = payload.get('vars', page.get('vars', {}))
    page['seo'] = payload.get('seo', page.get('seo', {}))
    page['draft'] = True
    page['version'] = int(page.get('version', 0)) + 1
    page['updated_at'] = datetime.utcnow().isoformat()
    store.save(page)
    return jsonify({'ok': True, 'version': page['version']})

# ------------------------
# Publish (by route)
# ------------------------
@website_admin_routes.route('/builder/<path:page_route>/publish', methods=['POST'], endpoint='builder_publish')
def builder_publish(page_route):
    """Publishes the current draft. Auto-creates page if needed."""
    data_dir = _data_dir()
    store = BuilderPageStore(data_dir)
    page = _get_or_create_page(store, page_route)
    page['draft'] = False
    page['published_at'] = datetime.utcnow().isoformat()
    page['updated_at'] = datetime.utcnow().isoformat()
    store.save(page)
    return jsonify({'ok': True})

# ------------------------
# Create page (explicit route)
# ------------------------
@website_admin_routes.route('/builder/create', methods=['POST'], endpoint='builder_create_page')
def builder_create_page():
    """Creates a new builder-managed page and returns canonical builder URL."""
    data = request.get_json(silent=True) or request.form
    title = (data.get('title') or '').strip()
    route = (data.get('route') or '').strip()
    if not title or not route or not route.startswith('/'):
        return jsonify({'ok': False, 'error': "Provide title and a route starting with '/'" }), 400

    data_dir = _data_dir()
    store = BuilderPageStore(data_dir)

    existing = _load_page_by_route(store, route)
    if existing:
        return jsonify({
            'ok': True,
            'builder_url': url_for('website_admin_routes.builder_ui', page_route=route.lstrip('/'))
        })

    new_page = store.create(title=title, route=route)
    return jsonify({
        'ok': True,
        'builder_url': url_for('website_admin_routes.builder_ui', page_route=route.lstrip('/'))
    })

# ------------------------
# Blocks registry (client-safe)
# ------------------------
@website_admin_routes.route('/builder/blocks/registry', methods=['GET'], endpoint='builder_blocks_registry')
def builder_blocks_registry():
    """Returns safe block metadata (label, icon, defaults, schema)."""
    return jsonify(BlocksRegistry().safe_registry())



@website_admin_routes.route('/', methods=['GET'])
def admin_index():
    """
    Website Admin Dashboard with time-range filtering, deltas, ordered charts,
    popular pages with real % change, and country breakdown.
    """

    # Initialize analytics
    module_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = ensure_data_folder(module_dir)
    analytics_manager = AnalyticsManager(data_dir)

    # Valid periods
    valid_periods = {
        "today": "Today",
        "weekly": "Last 7 days",
        "monthly": "Last 30 days",
        "year": "Last 12 months",
        "alltime": "All time",
    }

    # Period handling
    period = request.args.get("period", "alltime").lower()
    if period not in valid_periods:
        period = "alltime"

    # Time ranges
    current_range = analytics_manager.get_timerange(period)
    prev_range = analytics_manager.get_previous_timerange(period)

    # Totals and delta
    if current_range != (None, None):
        total_views_current = sum(1 for _ in analytics_manager._iter_views(current_range))
    else:
        total_views_current = len(analytics_manager.get_page_views())

    if prev_range != (None, None):
        total_views_prev = sum(1 for _ in analytics_manager._iter_views(prev_range))
    else:
        total_views_prev = 0

    views_delta = None
    if total_views_prev:
        views_delta = round(
            (total_views_current - total_views_prev) * 100.0 / total_views_prev, 1
        )

    # Charts (stable ordering)
    views_by_hour = analytics_manager.get_views_by_hour(current_range)        # dict int 0..23
    views_by_weekday = analytics_manager.get_views_by_weekday(current_range)  # dict int 0..6

    # Popular pages with real delta vs previous range
    popular_now = analytics_manager.get_popular_pages(time_range=current_range)
    prev_dict = (
        dict(analytics_manager.get_popular_pages(time_range=prev_range))
        if prev_range != (None, None)
        else {}
    )

    popular_pages_detailed = []
    for page, views in popular_now:
        prev = prev_dict.get(page, 0)
        change = round(((views - prev) * 100.0 / prev), 1) if prev else None
        popular_pages_detailed.append({
            "page": page,
            "views": views,
            "change": change,
        })

    # Countries
    requests_by_country = analytics_manager.get_requests_by_country(current_range)
    top_countries = sorted(
        requests_by_country.items(), key=lambda x: x[1], reverse=True
    )[:10]
    total_country_views = sum(requests_by_country.values()) or 0

    # Plugin manifest (path-safe)
    app_root = os.path.abspath(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..', 'app')
    )
    plugins_dir = os.path.abspath(os.path.join(app_root, 'plugins'))
    plugin_manager = PluginManager(plugins_dir)
    core_manifest = plugin_manager.get_core_manifest()

    # Analytics data for template
    analytics_data = {
        "total_views": total_views_current,
        "views_delta": views_delta,
        "views_by_hour": views_by_hour,
        "views_by_weekday": views_by_weekday,
        "popular_pages_detailed": popular_pages_detailed,
        "requests_by_country": requests_by_country,
        "top_countries": top_countries,
        "total_country_views": total_country_views,
        "current_period": period,
        "period_label": valid_periods[period],
    }

    return render_template(
        "admin/index.html",
        config=core_manifest,
        title="Website Admin Dashboard",
        analytics=analytics_data,
    )

@website_admin_routes.route('/contact-config', methods=['GET', 'POST'])
def contact_config():
    from .objects import ContactFormConfigManager
    MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_manager = ContactFormConfigManager(MODULE_DIR)
    
    # Handle deletion via AJAX: expects a DELETE request parameter "delete_form_id"
    if request.method == 'POST' and request.form.get("action") == "delete":
        form_id = request.form.get("delete_form_id", "").strip()
        if form_id:
            current_config = config_manager.get_configuration()
            if form_id in current_config:
                del current_config[form_id]
                config_manager.save_config(current_config)
                return "Deleted", 200
            else:
                return "Not Found", 404
        else:
            return "Missing Form ID", 400

    # Handle inline update via AJAX
    if request.method == 'POST' and request.headers.get("X-Requested-With") == "XMLHttpRequest":
        form_id = request.form.get("form_id", "").strip()
        recipient = request.form.get("recipient", "").strip()
        subject = request.form.get("subject", "").strip()
        if not form_id or not recipient or not subject:
            return "Missing fields", 400
        else:
            config_manager.update_configuration(form_id, recipient, subject)
            return "Success", 200

    # Handle non-AJAX POST: for adding a new configuration
    if request.method == 'POST':
        form_id = request.form.get("form_id", "").strip()
        recipient = request.form.get("recipient", "").strip()
        subject = request.form.get("subject", "").strip()
        if not form_id or not recipient or not subject:
            flash("All fields are required.", "danger")
        else:
            current_config = config_manager.get_configuration()
            if form_id in current_config:
                flash(f"Configuration for form '{form_id}' already exists. Use inline editing to modify it.", "warning")
            else:
                current_config[form_id] = {"recipient": recipient, "subject": subject}
                config_manager.save_config(current_config)
                flash(f"Configuration for form '{form_id}' added successfully.", "success")
        return redirect(url_for('website_admin_routes.contact_config'))
    
    # Load core manifest for base template (from PluginManager)
    from ...objects import PluginManager
    plugin_manager = PluginManager(os.path.abspath('app/plugins'))
    core_manifest = plugin_manager.get_core_manifest()
    
    current_config = config_manager.get_configuration()
    # Pass core manifest as "config" and the contact settings as "contact_config"
    return render_template("admin/contact_config.html", config=core_manifest, contact_config=current_config)

PAGES_FILE = os.path.join(os.path.dirname(__file__), 'pages.json')
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates', 'public')


def write_html_file(file_path, page_data):
    """
    Generate a basic HTML file with meta information.
    - Creates parent directories if they don't exist.
    - Writes either the "index.html" content or a default fallback
      for other pages.
    """

    # Convert to a Path object to normalize any back/forward slashes
    path_obj = Path(file_path).resolve()

    # Ensure the parent directory exists (create if needed)
    path_obj.parent.mkdir(parents=True, exist_ok=True)

    # If filename is "index.html", write the "sales page" style content
    if path_obj.name == "index.html":
        content = f"""{{% extends "base.html" %}}

{{% block title %}}{page_data['meta']['title']}{{% endblock %}}

{{% block content %}}
<!-- Main Content Section -->
<section class="container text-center mt-5">
    <h1 class="display-4">{page_data['title']}</h1>
    <p class="lead">{page_data['meta']['description']}</p>
    <!-- Get Started Button -->
    <button type="button" class="btn btn-primary btn-lg" data-mdb-toggle="modal" data-mdb-target="#getStartedModal">
        Get Started
    </button>
</section>

<!-- Features Section -->
<section class="features container text-center mt-5">
    <div class="row">
        <div class="col-md-4 mb-4">
            <div class="feature-icon mb-3">
                <i class="bi bi-gear" style="font-size: 2rem;"></i>
            </div>
            <h5>Modular Design</h5>
            <p>Extend functionality seamlessly with plug-and-play modules.</p>
        </div>
        <div class="col-md-4 mb-4">
            <div class="feature-icon mb-3">
                <i class="bi bi-lightning" style="font-size: 2rem;"></i>
            </div>
            <h5>Fast and Flexible</h5>
            <p>Built on Flask for rapid, lightweight web development.</p>
        </div>
        <div class="col-md-4 mb-4">
            <div class="feature-icon mb-3">
                <i class="bi bi-code-slash" style="font-size: 2rem;"></i>
            </div>
            <h5>Developer-Friendly</h5>
            <p>Write clean, extendable code with easy integration.</p>
        </div>
    </div>
</section>

<!-- Modal for Get Started -->
<div class="modal fade" id="getStartedModal" tabindex="-1" aria-labelledby="getStartedModalLabel" aria-hidden="true">
    <div class="modal-dialog">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title" id="getStartedModalLabel">Get Started with Sparrow ERP</h5>
                <button type="button" class="btn-close" data-mdb-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body">
                <p>To create and edit your website, log in to the admin portal at:</p>
                <a href="http://localhost:82/"><p><strong>http://localhost:82/</strong></p></a>
                <p>Enhance your frontend by installing and activating additional modules.</p>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" data-mdb-dismiss="modal">Close</button>
            </div>
        </div>
    </div>
</div>
{{% endblock %}}
"""
    else:
        # Default fallback content for other pages
        # (Note: Using triple quotes with raw { } might require doubling braces or removing them; 
        # adjust as needed if you see Jinja parse issues.)
        content = """
{% extends "base.html" %}

{% block title %}Home - Sparrow ERP{% endblock %}

{% block content %}
<!-- Hero Section -->
<section class="text-center py-5">
    <div class="container">
        <h1 class="display-3 fw-bold">{{ page_data.meta.title }}</h1>
        <p class="lead">{{ page_data.meta.description or "Start building your website with Sparrow ERP's modular framework." }}</p>
        <!-- Get Started Button -->
        <button type="button" class="btn btn-primary btn-lg" data-mdb-toggle="modal" data-mdb-target="#getStartedModal">
            Get Started
        </button>
    </div>
</section>

<!-- Modal for Get Started -->
<div class="modal fade" id="getStartedModal" tabindex="-1" aria-labelledby="getStartedModalLabel" aria-hidden="true">
    <div class="modal-dialog">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title" id="getStartedModalLabel">Get Started with Sparrow ERP</h5>
                <button type="button" class="btn-close" data-mdb-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body">
                <p>Get into your Page Manager and start editing to create your perfect website!</p>
                <p>Access the admin portal at:</p>
                <a href="http://localhost:82/"><strong>http://localhost:82/</strong></a>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" data-mdb-dismiss="modal">Close</button>
            </div>
        </div>
    </div>
</div>
{% endblock %}
"""

    # Write the content (overwrites if file already exists)
    path_obj.write_text(content, encoding='utf-8')
    print(f"HTML file created/updated: {path_obj}")

@website_admin_routes.route('/edit_base', methods=['GET', 'POST'])
def edit_base_html():
    """
    Allows editing of the public base.html file.
    """
    # Path to the public base.html file
    base_html_path = os.path.join(os.path.dirname(__file__), 'templates', 'public', 'base.html')
    print(f"Loading base.html from: {base_html_path}")  # Debugging path

    base_content = ""
    if os.path.exists(base_html_path):
        print("base.html file found!")  # Confirm file exists
        with open(base_html_path, 'r') as f:
            base_content = f.read()
    else:
        print("base.html file not found!")  # Debugging output

    if request.method == 'POST':
        # Save updated content to base.html
        updated_content = request.form['base_content']
        with open(base_html_path, 'w') as f:
            f.write(updated_content)
        flash('Base.html updated successfully!', 'success')
        return redirect(url_for('website_admin_routes.page_manager'))

    # Load pages for Page Manager
    pages = load_pages()

    return render_template('admin/page_manager.html', base_content=base_content, pages=pages)


@website_admin_routes.route('/pages', methods=['GET', 'POST'])
def page_manager():
    pages_path = os.path.join(os.path.dirname(__file__), 'pages.json')
    templates_dir = os.path.join(os.path.dirname(__file__), 'templates', 'public')
    base_html_path = os.path.join(templates_dir, 'base.html')  # Path to base.html

    plugin_manager = PluginManager(os.path.abspath('plugins'))
    core_manifest = plugin_manager.get_core_manifest()

    # Ensure base.html exists
    if not os.path.exists(base_html_path):
        with open(base_html_path, 'w') as f:
            f.write("""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ site_settings.company_name or 'Sparrow ERP' }}</title>
    <meta name="description" content="{{ page_data.meta.description if page_data.meta else '' }}">
    <meta name="keywords" content="{{ ', '.join(page_data.meta.keywords) if page_data.meta else '' }}">

    <!-- Material Bootstrap CSS -->
    <link
        href="https://cdnjs.cloudflare.com/ajax/libs/mdb-ui-kit/6.4.0/mdb.min.css"
        rel="stylesheet"
    >
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css">

    <!-- Custom CSS (Theme and User Uploaded) -->
    {% if theme_settings.custom_css_path %}
        <link rel="stylesheet" href="{{ url_for('static', filename=theme_settings.custom_css_path) }}">
    {% else %}
        <link rel="stylesheet" href="{{ url_for('static', filename='css/' + theme_settings.theme + '.css') }}">
    {% endif %}
</head>
<body>
    <!-- Navbar -->
    <nav class="navbar navbar-expand-lg navbar-light bg-light">
      <div class="container">
        {% if site_settings.branding == 'logo' and site_settings.logo_path %}
            <a class="navbar-brand" href="/">
                <img src="{{ url_for('static', filename=site_settings.logo_path) }}" alt="Logo" height="40">
            </a>
        {% else %}
            <a class="navbar-brand" href="/">{{ site_settings.company_name or 'Sparrow ERP' }}</a>
        {% endif %}
        <button
          class="navbar-toggler"
          type="button"
          data-mdb-toggle="collapse"
          data-mdb-target="#navbarNav"
          aria-controls="navbarNav"
          aria-expanded="false"
          aria-label="Toggle navigation"
        >
          <i class="fas fa-bars"></i>
        </button>
        <div class="collapse navbar-collapse" id="navbarNav">
          <ul class="navbar-nav ms-auto">
            {% for page in pages %}
                {% if page.header %}
                    <li class="nav-item">
                        <a class="nav-link" href="{{ url_for('website_public.custom_page', page_route=page.route.strip('/')) }}">
                            {{ page.title }}
                        </a>
                    </li>
                {% endif %}
            {% endfor %}
            <li class="nav-item">
                <a class="nav-link" href="{{ url_for('routes.logout') }}">Logout</a>
            </li>
          </ul>
        </div>
      </div>
    </nav>

    <!-- Page Content -->
    <div class="container mt-5">
        {% block content %}
        <!-- Content will be injected here -->
        {% endblock %}
    </div>

    <!-- Footer -->
    <footer class="bg-light text-center text-lg-start mt-5">
      <div class="container p-4">
        <div class="row">
          <div class="col-lg-6 col-md-12 mb-4 mb-md-0">
            <h5 class="text-uppercase">Powered by Sparrow ERP</h5>
            <p>
              Sparrow ERP offers powerful website and e-commerce capabilities, seamlessly integrated with your business.
            </p>
          </div>
          <div class="col-lg-6 col-md-12">
            <h5 class="text-uppercase">Quick Links</h5>
            <ul class="list-unstyled mb-0">
                {% for page in pages %}
                    {% if page.footer %}
                        <li>
                            <a href="{{ url_for('website_public.custom_page', page_route=page.route.strip('/')) }}" class="text-dark">
                                {{ page.title }}
                            </a>
                        </li>
                    {% endif %}
                {% endfor %}
            </ul>
          </div>
        </div>
      </div>
      <div class="text-center p-3 bg-dark text-white">
        CopyRight 2025 <strong>{{ site_settings.company_name or 'Sparrow ERP' }}</strong>. All Rights Reserved. | Powered by Sparrow ERP
      </div>
    </footer>

    <!-- Material Bootstrap JS -->
    <script
      type="text/javascript"
      src="https://cdnjs.cloudflare.com/ajax/libs/mdb-ui-kit/6.4.0/mdb.min.js"
    ></script>
</body>
</html>""")

    # Ensure pages.json exists with a default Home page
    if not os.path.exists(pages_path):
        default_pages = [
            {
                "title": "Home",
                "route": "/",
                "header": True,
                "footer": True,
                "meta": {
                    "title": "Sparrow ERP - Build with Ease",
                    "description": "Discover the power of Sparrow ERP's modular, Flask-based architecture.",
                    "keywords": ["Sparrow ERP", "modular development", "Flask", "ERP system"]
                }
            }
        ]
        with open(pages_path, 'w') as f:
            json.dump(default_pages, f, indent=4)
        print("Default pages.json created.")

    # Check if index.html exists; create it only if missing
    index_html_path = os.path.join(templates_dir, "index.html")
    if not os.path.exists(index_html_path):
        # Load page data from pages.json
        with open(pages_path, 'r') as f:
            pages = json.load(f)
        # Find the home page data
        home_page = next((p for p in pages if p['route'] == '/'), None)
        if home_page:
            write_html_file(index_html_path, home_page)
            print("Default index.html created.")

    # Handle POST requests
    if request.method == 'POST':
        if 'add_page' in request.form:
            # Add new page
            title = request.form['title']
            route = request.form['route'].strip('/')
            file_name = 'index.html' if route == '' else f"{route}.html"
            html_path = os.path.join(templates_dir, file_name)

            new_page = {
                "title": title,
                "route": f"/{route}" if route else "/",
                "header": 'header' in request.form,
                "footer": 'footer' in request.form,
                "meta": {"title": title, "description": "", "keywords": []}
            }

            # Append to pages.json
            with open(pages_path, 'r+') as f:
                pages = json.load(f)
                pages.append(new_page)
                f.seek(0)
                json.dump(pages, f, indent=4)

            write_html_file(html_path, new_page)

        elif 'edit_page' in request.form:
            # Edit page details
            index = int(request.form['index'])
            with open(pages_path, 'r+') as f:
                pages = json.load(f)
                old_route = pages[index]['route'].strip('/')
                old_file_name = 'index.html' if old_route == '' else f"{old_route}.html"
                old_html_path = os.path.join(templates_dir, old_file_name)

                # Update details
                route = request.form['route'].strip('/')
                new_file_name = 'index.html' if route == '' else f"{route}.html"
                new_html_path = os.path.join(templates_dir, new_file_name)

                pages[index]['title'] = request.form['title']
                pages[index]['route'] = f"/{route}" if route else "/"
                pages[index]['header'] = 'header' in request.form
                pages[index]['footer'] = 'footer' in request.form
                pages[index]['meta'] = {
                    'title': request.form['meta_title'],
                    'description': request.form['meta_description'],
                    'keywords': [k.strip() for k in request.form['meta_keywords'].split(',')]
                }

                # Rename HTML file if route changes
                if old_html_path != new_html_path:
                    if os.path.exists(old_html_path):
                        os.rename(old_html_path, new_html_path)
                    else:
                        write_html_file(new_html_path, pages[index])

                f.seek(0)
                f.truncate()
                json.dump(pages, f, indent=4)

        elif 'edit_content' in request.form:
            # Edit content of an HTML page
            index = int(request.form['index'])
            content = request.form['content']
            with open(pages_path, 'r') as f:
                pages = json.load(f)
            route = pages[index]['route'].strip('/')
            file_name = 'index.html' if route == '' else f"{route}.html"
            html_path = os.path.join(templates_dir, file_name)
            with open(html_path, 'w') as f:
                f.write(content)

        elif 'delete_page' in request.form:
            # Delete a page
            index = int(request.form['index'])
            with open(pages_path, 'r+') as f:
                pages = json.load(f)
                deleted_page = pages.pop(index)
                f.seek(0)
                f.truncate()
                json.dump(pages, f, indent=4)

            route = deleted_page['route'].strip('/')
            file_name = 'index.html' if route == '' else f"{route}.html"
            html_path = os.path.join(templates_dir, file_name)
            if os.path.exists(html_path):
                os.remove(html_path)

        elif 'edit_base' in request.form:
            # Edit base.html content
            updated_content = request.form['base_content']
            with open(base_html_path, 'w') as f:
                f.write(updated_content)

    # Load pages and base.html content
    base_content = ""
    if os.path.exists(base_html_path):
        with open(base_html_path, 'r') as f:
            base_content = f.read()

    if os.path.exists(pages_path):
        with open(pages_path, 'r') as f:
            pages = json.load(f)
            for page in pages:
                route = page['route'].strip('/')
                file_name = 'index.html' if route == '' else f"{route}.html"
                html_path = os.path.join(templates_dir, file_name)
                if os.path.exists(html_path):
                    with open(html_path, 'r') as html_file:
                        page['content'] = html_file.read()
                else:
                    page['content'] = ""
    else:
        pages = []
  
    return render_template('admin/page_manager.html', pages=pages, config=core_manifest, base_content=base_content, public_url="http://localhost:80")


@website_admin_routes.route('/pages/meta/<int:page_index>', methods=['POST'])
def edit_meta(page_index):
    """
    Edit meta information for a specific page.
    """
    pages_path = os.path.join(os.path.dirname(__file__), 'pages.json')
    templates_dir = os.path.join(os.path.dirname(__file__), '../templates/public')

    # Load existing pages
    with open(pages_path, 'r') as f:
        pages = json.load(f)

    # Update meta information
    meta_title = request.form.get('meta_title', '').strip()
    meta_description = request.form.get('meta_description', '').strip()
    meta_keywords = request.form.get('meta_keywords', '').split(',')

    pages[page_index]['meta'] = {
        'title': meta_title,
        'description': meta_description,
        'keywords': [k.strip() for k in meta_keywords]
    }

    # Rewrite the HTML file
    page = pages[page_index]
    html_path = os.path.join(templates_dir, f"{page['route'].strip('/')}.html")
    write_html_file(html_path, page)

    # Save updated pages.json
    with open(pages_path, 'w') as f:
        json.dump(pages, f, indent=4)

    flash('Meta content updated successfully!', 'success')
    return redirect(url_for('website_admin_routes.page_manager'))


# Admin route registration function
def register_admin_routes(app):
    """
    Function to register the admin routes for the Website Module.
    This will be called dynamically by the Core Module.
    """
    app.register_blueprint(website_admin_routes)