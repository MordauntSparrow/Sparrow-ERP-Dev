import os
import json
import subprocess
import sys
import re

from flask import Flask
from flask_login import LoginManager
from flask_cors import CORS

from dotenv import load_dotenv, set_key

from app.objects import User  # Your user model with get_user_by_id

from flask_session import Session as FlaskSession
import redis as _redis

# Security and monitoring
try:
    from flask_seasurf import SeaSurf
except Exception:
    SeaSurf = None
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
except Exception:
    Limiter = None
    get_remote_address = None
try:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
except Exception:
    sentry_sdk = None
try:
    from prometheus_flask_exporter import PrometheusMetrics
except Exception:
    PrometheusMetrics = None


# ---------------------------------------------------------------------
# Environment (.env) loading
# ---------------------------------------------------------------------

ENV_PATH = os.path.join(os.path.dirname(__file__), "config", ".env")
load_dotenv(dotenv_path=ENV_PATH)


def update_env_var(key, value):
    """
    Updates the given key in .env and reloads environment variables
    so that os.environ reflects the change in this process.
    """
    set_key(str(ENV_PATH), key, value)
    load_dotenv(ENV_PATH, override=True)


def restart_application():
    """
    Hard restart the current process.
    NOTE: Prefer the restart.flag watcher in run.py for normal operations.
    """
    print("Restarting application to apply changes...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------

def _ensure_core_manifest(config_dir: str) -> str:
    """
    Ensures app/config/manifest.json exists. Returns its path.
    """
    core_manifest_path = os.path.join(config_dir, "manifest.json")
    os.makedirs(config_dir, exist_ok=True)

    if not os.path.exists(core_manifest_path):
        core_manifest = {
            "name": "Core Module",
            "system_name": "Sparrow_ERP_Core",
            "version": "1.0.0",
            "theme_settings": {
                "theme": "default",
                "custom_css_path": ""
            },
            "site_settings": {
                "company_name": "Sparrow ERP",
                "branding": "name",
                "logo_path": ""
            }
        }
        with open(core_manifest_path, 'w', encoding="utf-8") as f:
            json.dump(core_manifest, f, indent=4)
        print(f"Core manifest created at {core_manifest_path}")

    return core_manifest_path


def _run_dependency_handler(app_root: str) -> None:
    """
    Runs dependency_handler.py during startup.
    """
    dependency_handler_path = os.path.join(app_root, "dependency_handler.py")
    if not os.path.exists(dependency_handler_path):
        print(
            f"Error: Dependency handler not found at {dependency_handler_path}")
        sys.exit(1)

    try:
        print("Running dependency handler during application startup...")
        subprocess.check_call([sys.executable, dependency_handler_path])
    except subprocess.CalledProcessError as e:
        print(f"Dependency handler failed: {e}")
        sys.exit(1)


def _install_missing_dependency_from_import_error(e: ImportError) -> None:
    """
    Attempts to pip install the missing module from an ImportError, then restarts.
    """
    msg = str(e)
    # Typical ImportError: "No module named 'xyz'"
    missing = None
    if "'" in msg:
        try:
            missing = msg.split("'")[1]
        except Exception:
            missing = None

    if not missing:
        raise e

    print(
        f"Error importing module: {e}. Attempting to install missing dependency: {missing}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", missing])
    print(
        f"Installed missing dependency: {missing}. Restarting application...")
    restart_application()


def _register_jinja_filters(app: Flask) -> None:
    def sort_keys(d):
        """Sort dictionary items so that keys with 'time' or 'date' come first."""
        def keyfunc(item):
            k, _v = item
            if 'time' in k.lower() or 'date' in k.lower():
                return (0, k.lower())
            return (1, k.lower())
        return sorted(d.items(), key=keyfunc)

    def format_timestamp(ts):
        if isinstance(ts, str):
            return ts.replace('T', ' ')
        return ts

    def regex_replace(value, pattern, repl):
        return re.sub(pattern, repl, value)

    @app.template_filter('fromjson')
    def fromjson_filter(s):
        return json.loads(s)

    app.jinja_env.filters['sort_keys'] = sort_keys
    app.jinja_env.filters['format_timestamp'] = format_timestamp
    app.jinja_env.filters['regex_replace'] = regex_replace


# ---------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------

def create_app():
    """
    Create and configure the Flask admin app.
    """
    app_root = os.path.abspath(os.path.dirname(__file__))
    config_dir = os.path.join(app_root, "config")
    plugins_dir = os.path.abspath(os.path.join(app_root, "plugins"))

    # Ensure core config exists
    _ensure_core_manifest(config_dir)

    # Dependency handler (your existing behaviour)
    # _run_dependency_handler(app_root)

    # Import modules that may not exist until dependencies are installed
    try:
        from app.objects import PluginManager
    except ImportError as e:
        _install_missing_dependency_from_import_error(e)

    # Create Flask app
    app = Flask(__name__)

    # Jinja filters
    _register_jinja_filters(app)

    # Config
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'defaultsecretkey')
    app.config['PUBLIC_SERVER_URL'] = os.environ.get(
        'PUBLIC_SERVER_URL', 'http://localhost:80')

    # Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "routes.login"

    @login_manager.user_loader
    def load_user(user_id):
        return User.get_user_by_id(user_id)

    # Portal theme: light (default), dark, or auto (by time). Per-user in DB. Resolved theme used for CSS.
    @app.context_processor
    def inject_portal_theme():
        from flask import session, request
        from app.plugins.employee_portal_module.services import get_contractor_theme, resolve_theme_by_time
        preference = session.get("portal_theme")
        if not preference or preference not in ("light", "dark", "auto"):
            # Cookie fallback when session is missing (e.g. after redirect from set-theme on another path)
            cookie_theme = (request.cookies.get("portal_theme") or "").strip().lower()
            if cookie_theme in ("light", "dark", "auto"):
                preference = cookie_theme
                session["portal_theme"] = preference
                session.modified = True
        if not preference or preference not in ("light", "dark", "auto"):
            cid = (session.get("tb_user") or {}).get("id")
            if cid:
                try:
                    stored = get_contractor_theme(int(cid))
                    if stored in ("light", "dark", "auto"):
                        preference = stored
                        session["portal_theme"] = preference
                except Exception:
                    pass
            if not preference or preference not in ("light", "dark", "auto"):
                preference = request.cookies.get("portal_theme", "light").strip().lower()
                if preference not in ("light", "dark", "auto"):
                    preference = "light"
        resolved = resolve_theme_by_time() if preference == "auto" else preference
        return {"portal_theme": resolved, "portal_theme_preference": preference}

    # Register blueprints
    from app.routes import routes, api_bp
    app.register_blueprint(routes)
    app.register_blueprint(api_bp)

    # Plugins
    plugin_manager = PluginManager(plugins_dir=plugins_dir)
    plugin_manager.register_admin_routes(app)
    # Public plugin blueprints (e.g. recruitment /vacancies, employee portal paths) — required for url_for from admin templates
    plugin_manager.register_public_routes(app)

    # CORS (allow Authorization header for Bearer token from Lovable / other origins)
    CORS(
        app,
        resources={r"/*": {"origins": "*",
                           "allow_headers": ["Content-Type", "Authorization"]}},
        supports_credentials=True,
    )

    # ------------------------------------------------------------------
    # Session store (Redis) for multi-instance deployments
    # ------------------------------------------------------------------
    redis_url = os.environ.get('REDIS_URL') or os.environ.get('REDIS_URLS')
    if redis_url:
        try:
            app.config['SESSION_TYPE'] = 'redis'
            app.config['SESSION_REDIS'] = _redis.from_url(redis_url)
            app.config['SESSION_COOKIE_HTTPONLY'] = True
            app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
            app.config['SESSION_COOKIE_SECURE'] = os.environ.get(
                'SESSION_COOKIE_SECURE', 'false').lower() == 'true'
            FlaskSession(app)
        except Exception as e:
            print(f"[WARN] Redis session setup failed: {e}")

    # ------------------------------------------------------------------
    # CSRF protection (SeaSurf) and rate limiting
    # ------------------------------------------------------------------
    if SeaSurf:
        try:
            csrf = SeaSurf(app)
            # Exempt inventory plugin API routes so Bearer token auth works (no CSRF cookie from cross-origin clients)
            for rule in app.url_map.iter_rules():
                if rule.rule.startswith("/plugin/inventory_control/api/"):
                    view = app.view_functions.get(rule.endpoint)
                    if view and not getattr(view, "_csrf_exempt", False):
                        csrf.exempt(view)
                        view._csrf_exempt = True
            # Exempt all Ventus plugin routes: CAD, response centre, and dispatch use fetch() without CSRF token;
            # MDT uses JWT. Auth is session or Bearer. Covers job/, api/, messages/, dispatch/, unit/, motd, etc.
            for rule in app.url_map.iter_rules():
                if rule.rule.startswith("/plugin/ventus_response_module/"):
                    view = app.view_functions.get(rule.endpoint)
                    if view and not getattr(view, "_csrf_exempt", False):
                        csrf.exempt(view)
                        view._csrf_exempt = True
            # Exempt core /api/* (e.g. POST /api/login) so MDT, Lovable, and other API clients can authenticate without CSRF
            for rule in app.url_map.iter_rules():
                if rule.rule.startswith("/api/"):
                    view = app.view_functions.get(rule.endpoint)
                    if view and not getattr(view, "_csrf_exempt", False):
                        csrf.exempt(view)
                        view._csrf_exempt = True
        except Exception as e:
            print(f"[WARN] SeaSurf init failed: {e}")
    # Ensure csrf_token() exists in Jinja so templates never raise UndefinedError (e.g. when SeaSurf not installed or init failed)
    if "csrf_token" not in app.jinja_env.globals:
        def _csrf_token():
            return ""
        app.jinja_env.globals["csrf_token"] = _csrf_token

    # Basic rate limiting
    if Limiter:
        try:
            limiter = Limiter(key_func=get_remote_address, default_limits=[
                              os.environ.get('RATE_LIMIT', '200 per minute')])
            limiter.init_app(app)
        except Exception as e:
            print(f"[WARN] Limiter init failed: {e}")

    # ------------------------------------------------------------------
    # Sentry (optional)
    # ------------------------------------------------------------------
    try:
        sentry_dsn = os.environ.get('SENTRY_DSN')
        if sentry_sdk and sentry_dsn:
            sentry_sdk.init(dsn=sentry_dsn, integrations=[
                            FlaskIntegration()], traces_sample_rate=0.1)
    except Exception as e:
        print(f"[WARN] Sentry init failed: {e}")

    # ------------------------------------------------------------------
    # Prometheus metrics (optional)
    # ------------------------------------------------------------------
    try:
        if PrometheusMetrics:
            PrometheusMetrics(app)
    except Exception as e:
        print(f"[WARN] Prometheus metrics init failed: {e}")

    # ------------------------------------------------------------------
    # Structured audit logging (rotating JSON file)
    # ------------------------------------------------------------------
    try:
        import logging
        import json
        from logging.handlers import RotatingFileHandler

        logs_dir = os.path.join(app_root, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        audit_path = os.path.join(logs_dir, 'audit.log')

        class JsonFormatter(logging.Formatter):
            def format(self, record):
                payload = {
                    'time': self.formatTime(record, self.datefmt),
                    'level': record.levelname,
                    'logger': record.name,
                    'message': record.getMessage()
                }
                # include extra fields if present
                if hasattr(record, 'extra') and isinstance(record.extra, dict):
                    payload.update(record.extra)
                return json.dumps(payload, default=str)

        audit_handler = RotatingFileHandler(
            audit_path, maxBytes=10*1024*1024, backupCount=5)
        audit_handler.setLevel(logging.INFO)
        audit_handler.setFormatter(JsonFormatter())

        audit_logger = logging.getLogger('audit')
        audit_logger.setLevel(logging.INFO)
        audit_logger.addHandler(audit_handler)
        # make available on app for use
        app.audit_logger = audit_logger
    except Exception as e:
        print(f"[WARN] Audit logging setup failed: {e}")
    # ------------------------------------------------------------------
    # Socket.IO initialization (optional Redis message queue)
    # ------------------------------------------------------------------
    try:
        from . import socketio
        redis_url = os.environ.get('REDIS_URL') or os.environ.get('REDIS_URLS')
        socketio_opts = {}
        # If a Redis message queue is configured, provide it so SocketIO can
        # scale across processes/instances.
        if redis_url:
            socketio_opts['message_queue'] = redis_url
        # Preferred async mode may be set via env var (default to eventlet)
        async_mode = os.environ.get('SOCKETIO_ASYNC_MODE', 'eventlet')
        socketio.init_app(app, cors_allowed_origins='*',
                          async_mode=async_mode, **socketio_opts)
        # Enforce authentication on socket connect
        try:
            from flask_login import current_user
            from flask_socketio import disconnect

            @socketio.on('connect')
            def _on_connect():
                try:
                    if not getattr(current_user, 'is_authenticated', False):
                        # reject anonymous socket connections
                        disconnect()
                except Exception:
                    disconnect()
        except Exception:
            pass
        # Simple pass-through for panel messages coming from clients (popouts/main)
        try:
            @socketio.on('panel_message')
            def _on_panel_message(msg):
                try:
                    socketio.emit('panel_message', msg,
                                  broadcast=True, include_self=False)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception as e:
        # Non-fatal: if SocketIO imports fail, continue without realtime features.
        print(f"[WARN] SocketIO initialization failed: {e}")

    return app
