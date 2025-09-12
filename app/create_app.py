import os
import json
import subprocess
import sys
from flask import Flask
from flask_login import LoginManager
from app.objects import User  # Your user model with get_user_by_id
from flask_cors import CORS

# Load environment variables from a .env file in the app/config folder
from dotenv import load_dotenv, set_key


# Set the path to your .env file inside app/config
env_path = os.path.join(os.path.dirname(__file__), "config", ".env")
load_dotenv(dotenv_path=env_path)

def update_env_var(key, value):
    """
    Updates the given key in .env and reloads environment variables
    so that os.environ reflects the change in this process.
    """
    # set_key expects a string path
    set_key(str(env_path), key, value)
    # Reload .env so that os.environ is updated
    load_dotenv(env_path, override=True)

def restart_application():
    """Restart the application."""
    print("Restarting application to apply changes...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

def create_app():
    """Create and configure the Flask app."""
    # Define paths
    CONFIG_FOLDER = os.path.join("app", "config")
    CORE_MANIFEST_PATH = os.path.join(CONFIG_FOLDER, "manifest.json")

    # Ensure config directory exists
    os.makedirs(CONFIG_FOLDER, exist_ok=True)

    # Check and create core manifest if not exists
    if not os.path.exists(CORE_MANIFEST_PATH):
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
        with open(CORE_MANIFEST_PATH, 'w') as f:
            json.dump(core_manifest, f, indent=4)
        print(f"Core manifest created at {CORE_MANIFEST_PATH}")

    # Path to dependency_handler.py
    dependency_handler_path = os.path.join("app", "dependency_handler.py")

    if not os.path.exists(dependency_handler_path):
        print(f"Error: Dependency handler not found at {dependency_handler_path}")
        sys.exit(1)

    # Run the dependency handler during startup
    try:
        print("Running dependency handler during application startup...")
        subprocess.check_call([sys.executable, dependency_handler_path])
    except subprocess.CalledProcessError as e:
        print(f"Dependency handler failed: {e}")
        sys.exit(1)

    # Attempt to import required modules
    try:
        from app.objects import PluginManager
        from app.plugins.website_module import WebsiteServer
    except ImportError as e:
        print(f"Error importing module: {e}. Attempting to install missing dependency...")
        missing_package = str(e).split("'")[1]  # Extract the missing package name
        subprocess.check_call([sys.executable, "-m", "pip", "install", missing_package])
        print(f"Installed missing dependency: {missing_package}. Restarting application...")
        restart_application()

    # Create the Flask admin app
    app = Flask(__name__)

    import re

    def sort_keys(d):
        """Sort dictionary items so that keys with 'time' or 'date' come first."""
        def keyfunc(item):
            k, v = item
            if 'time' in k.lower() or 'date' in k.lower():
                return (0, k.lower())
            return (1, k.lower())
        return sorted(d.items(), key=keyfunc)
    app.jinja_env.filters['sort_keys'] = sort_keys

    def format_timestamp(ts):
        if isinstance(ts, str):
            return ts.replace('T', ' ')
        return ts
    app.jinja_env.filters['format_timestamp'] = format_timestamp

    def regex_replace(value, pattern, repl):
        return re.sub(pattern, repl, value)

    app.jinja_env.filters['regex_replace'] = regex_replace


    # Set configuration using environment variables
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'defaultsecretkey')
    app.config['PUBLIC_SERVER_URL'] = os.environ.get('PUBLIC_SERVER_URL', 'http://localhost:80')
 
    # Initialize and configure the LoginManager (from flask_login)
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "routes.login"  # The endpoint name for your login view

    # Define the user loader callback
    @login_manager.user_loader
    def load_user(user_id):
        """
        This callback is used by Flask-Login to reload the user object from the user ID stored in the session.
        It should return a user object if found, or None otherwise.
        """
        return User.get_user_by_id(user_id)

    @app.template_filter('fromjson')
    def fromjson_filter(s):
        import json
        return json.loads(s)

    # Register the admin routes (which include login/logout)
    from app.routes import routes, api_bp
    app.register_blueprint(routes)

    
    app.register_blueprint(api_bp)
    # Initialize the plugin manager
    plugin_manager = PluginManager(plugins_dir=os.path.abspath(os.path.join("app", "plugins")))
    plugin_manager.register_admin_routes(app)
    CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)
    # if plugin_manager.is_plugin_enabled('website_module'):
    #     website_server = WebsiteServer(port=80)
    #     website_server.start()
    
    return app


