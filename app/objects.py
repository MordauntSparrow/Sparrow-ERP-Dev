import os
import json
import importlib
import requests
import shutil
import zipfile
import subprocess
import sys
from datetime import datetime, timedelta
from packaging.version import parse
from urllib.parse import urlparse, quote
from apscheduler.schedulers.background import BackgroundScheduler

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
import os
import mysql.connector
import bcrypt
from flask_login import UserMixin

from functools import wraps
from flask import flash, redirect, url_for

def get_db_connection():
    """
    Establish and return a new database connection using environment variables.
    """
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", "rootpassword"),
        database=os.getenv("DB_NAME", "sparrow_erp")
    )

class User(UserMixin):
    def __init__(self, id, username, email, role, permissions=None, personal_pin_hash=None):
        self.id = id
        self.username = username
        self.email = email
        self.role = role
        self.permissions = permissions or []
        self.personal_pin_hash = personal_pin_hash

    def get_id(self):
        return self.id

    @staticmethod
    def get_user_by_username_raw(username):
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        user_data = cursor.fetchone()
        cursor.close()
        conn.close()
        return user_data

    @staticmethod
    def get_user_by_id(user_id):
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user_data = cursor.fetchone()
        cursor.close()
        conn.close()
        if user_data:
            permissions = []
            if user_data.get('permissions'):
                try:
                    permissions = json.loads(user_data['permissions'])
                except Exception:
                    permissions = []
            # Ensure that if the personal_pin_hash is empty, we convert it to None.
            personal_pin_hash = user_data.get('personal_pin_hash')
            if personal_pin_hash is not None and personal_pin_hash.strip() == "":
                personal_pin_hash = None

            return User(
                user_data['id'],
                user_data['username'],
                user_data['email'],
                user_data['role'],
                permissions,
                personal_pin_hash=personal_pin_hash
            )
        return None


    @staticmethod
    def get_user_by_email(email):
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user_data = cursor.fetchone()
        cursor.close()
        conn.close()
        return user_data

    @staticmethod
    def update_password(user_id, new_hash):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password_hash = %s WHERE id = %s", (new_hash, user_id))
        conn.commit()
        cursor.close()
        conn.close()

    @staticmethod
    def update_permissions(user_id, permissions_list):
        conn = get_db_connection()
        cursor = conn.cursor()
        json_permissions = json.dumps(permissions_list)
        cursor.execute("UPDATE users SET permissions = %s WHERE id = %s", (json_permissions, user_id))
        conn.commit()
        cursor.close()
        conn.close()

class AuthManager:
    @staticmethod
    def hash_password(password):
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    @staticmethod
    def verify_password(stored_password, provided_password):
        return bcrypt.checkpw(provided_password.encode('utf-8'), stored_password.encode('utf-8'))

def has_permission(permission):
    """
    Check if the current user has the given permission.
    Admin users automatically have all permissions.
    """
    if current_user.role == 'admin':
        return True
    return permission in current_user.permissions

def permission_required(permission):
    """
    Decorator to require a specific permission for a route.
    Admin users automatically bypass this check.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if current_user.role == 'admin':
                return f(*args, **kwargs)
            if permission in current_user.permissions:
                return f(*args, **kwargs)
            flash("Access denied: You do not have the required permission.", "danger")
            return redirect(url_for('routes.dashboard'))
        return wrapper
    return decorator

def ensure_core_data_folder():
    """Ensure that a 'data' folder exists in the core module directory (/app/data)."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_folder = os.path.join(base_dir, "data")
    if not os.path.exists(data_folder):
        os.makedirs(data_folder)
        print(f"Core data folder created at: {data_folder}")
    return data_folder

class EmailManager:
    """
    Handles system email sending using SMTP.
    SMTP configuration is loaded from environment variables.
    """
    def __init__(self):
        # Load SMTP configuration from environment variables
        self.smtp_config = {
            "host": os.environ.get("SMTP_HOST"),
            "port": int(os.environ.get("SMTP_PORT")),
            "username": os.environ.get("SMTP_USERNAME"),
            "password": os.environ.get("SMTP_PASSWORD"),
            "use_tls": os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
        }
        print(self.smtp_config)
        # Validate required configuration
        required_keys = ["host", "port", "username", "password"]
        for key in required_keys:
            if not self.smtp_config[key]:
                raise Exception(f"Email configuration missing required key: {key}")

    def send_email(self, subject, body, recipients, sender=None, html_body=None):
        if sender is None:
            sender = self.smtp_config["username"]

        msg = MIMEMultipart("alternative")
        msg['From']    = sender
        msg['To']      = ", ".join(recipients)
        msg['Subject'] = Header(subject, 'utf-8')

        # UTF‑8 parts
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            server = smtplib.SMTP(self.smtp_config["host"],
                                self.smtp_config["port"],
                                timeout=10)
            if self.smtp_config["use_tls"]:
                server.starttls()
            server.login(self.smtp_config["username"],
                        self.smtp_config["password"])

            # THIS handles headers + body encoding correctly:
            server.send_message(msg, from_addr=sender, to_addrs=recipients)
            server.quit()
            print(f"Email sent successfully to {recipients}")
        except Exception as e:
            print(f"Failed to send email: {e}")

class UpdateManager:
    # Environment-based configuration
    GITLAB_USERNAME = os.environ.get("GITLAB_USERNAME")
    GITLAB_PASSWORD = os.environ.get("GITLAB_PASSWORD")
    GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "default_token")
    CORE_MANIFEST_REMOTE_URL = os.environ.get(
        "CORE_MANIFEST_REMOTE_URL",
        "https://gitlab.com/api/v4/projects/sparrow-erp%2FCore/repository/files/manifest.json/raw?ref=main"
    )
    PLUGIN_MANIFEST_REMOTE_URL_TEMPLATE = os.environ.get(
        "PLUGIN_MANIFEST_REMOTE_URL_TEMPLATE",
        "https://gitlab.com/api/v4/projects/sparrow-erp%2FCore/repository/files/plugins/%s/manifest.json/raw?ref=main"
    )

    UPDATE_DIR = "app/updates"
    BACKUP_DIR = "app/backups"
    LOG_PATH = "app/logs/update_history.json"
    CORE_PATH = "app/core"  # Not used directly for core updates; we apply to system root

    def __init__(self, plugins_dir='plugins'):
        self.plugin_manager = PluginManager(plugins_dir)
        self.scheduler = BackgroundScheduler()
        self.scheduler.start()
        self.ensure_directories()

        # Background jobs
        self.scheduler.add_job(
            self.check_and_download_new_plugins,
            'interval', hours=1, id='new_plugins_check', name='Check for new official plugins'
        )
        self.scheduler.add_job(
            self.check_for_forced_updates,
            'interval', hours=1, id='forced_update_check', name='Check for forced core updates'
        )
        self.scheduler.add_job(
            self.check_for_forced_plugin_updates,
            'interval', hours=1, id='forced_plugin_update_check', name='Check for forced plugin updates'
        )

    # ------------- Internal helpers -------------
    def _gitlab_session(self):
        s = requests.Session()
        if self.GITLAB_USERNAME and self.GITLAB_PASSWORD:
            s.auth = (self.GITLAB_USERNAME, self.GITLAB_PASSWORD)  # Basic auth for deploy token
        elif self.GITLAB_TOKEN and self.GITLAB_TOKEN != "default_token":
            s.headers.update({"PRIVATE-TOKEN": self.GITLAB_TOKEN})  # Fallback for PATs
        s.headers.update({"Accept": "application/json"})
        return s

    def ensure_directories(self):
        os.makedirs(self.UPDATE_DIR, exist_ok=True)
        os.makedirs(self.BACKUP_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(self.LOG_PATH), exist_ok=True)

    def _version_tuple(self, v):
        if not isinstance(v, str):
            return (0,)
        parts = []
        for p in v.split('.'):
            try:
                parts.append(int(p))
            except ValueError:
                num = ''.join(ch for ch in p if ch.isdigit())
                parts.append(int(num) if num else 0)
        return tuple(parts)

    # ------------- Manifest Fetching -------------
    def get_core_manifest_remote(self):
        with self._gitlab_session() as s:
            r = s.get(os.environ.get(
        "CORE_MANIFEST_REMOTE_URL",
        f"https://gitlab.com/api/v4/projects/65546585/repository/files/manifest.json/raw?ref=main"
    ))
            if r.status_code == 200:
                try:
                    return json.loads(r.text)  # raw endpoint returns text
                except json.JSONDecodeError as e:
                    raise Exception(f"Core manifest not valid JSON: {e}. Body: {r.text[:500]}")
            elif r.status_code == 401:
                raise Exception("Unauthorized fetching core manifest (401). Check GITLAB_USERNAME/GITLAB_PASSWORD.")
            else:
                raise Exception(f"Failed to fetch core manifest: {r.status_code} - {r.text[:500]}")

    def get_plugin_manifest_remote(self, plugin_name):
        if not plugin_name:
            raise ValueError("Plugin name must be provided.")

        factory_manifest = self.plugin_manager.get_factory_manifest_by_name(plugin_name)

        if factory_manifest.get('repository') == 'official':
            encoded_name = quote(plugin_name, safe='')
            plugin_manifest_url = f"https://gitlab.com/api/v4/projects/65546585/repository/files/plugins%2F{encoded_name}%2Fmanifest.json/raw?ref=main"
        else:
            base_repo = (factory_manifest.get('repository') or '').rstrip('/')
            if not base_repo:
                raise Exception(f"Repository URL for {plugin_name} is missing or invalid in the factory manifest.")
            plugin_manifest_url = f"{base_repo}/plugins/{plugin_name}/manifest.json"

        with self._gitlab_session() as s:
            r = s.get(plugin_manifest_url)
            if r.status_code == 200:
                try:
                    data = json.loads(r.text)
                except json.JSONDecodeError as e:
                    raise Exception(f"Plugin manifest for {plugin_name} not valid JSON: {e}. Body: {r.text[:500]}")
                # Normalize: if bundled under "plugins", extract the single plugin dict
                if isinstance(data, dict) and 'plugins' in data and isinstance(data['plugins'], dict):
                    if plugin_name in data['plugins']:
                        return data['plugins'][plugin_name]
                    else:
                        raise Exception(f"Plugin {plugin_name} not found in plugins bundle manifest.")
                return data  # already single plugin dict
            elif r.status_code == 401:
                raise Exception(f"Unauthorized fetching plugin manifest for {plugin_name} (401).")
            else:
                raise Exception(f"Failed to fetch plugin manifest for {plugin_name}: {r.status_code} - {r.text[:500]}")

    def get_plugin_factory_manifest(self, plugin_name):
        if not plugin_name:
            raise ValueError("Plugin name must be provided.")
        plugin_manifest = self.plugin_manager.get_factory_manifest(plugin_name)
        if plugin_manifest.get('repository') == 'official':
            url = "https://gitlab.com/api/v4/projects/sparrow-erp%2FCore/repository/files/manifest.json/raw?ref=main"
        else:
            repo = (plugin_manifest.get('repository') or '').rstrip('/')
            if not repo:
                raise Exception(f"Repository URL missing for third-party plugin {plugin_name}.")
            url = f"{repo}/plugins/{plugin_name}/factory_manifest.json"

        with self._gitlab_session() as s:
            r = s.get(url)
            if r.status_code == 200:
                try:
                    return json.loads(r.text)
                except json.JSONDecodeError as e:
                    raise Exception(f"Factory manifest JSON invalid for {plugin_name}: {e}. Body: {r.text[:500]}")
            elif r.status_code == 401:
                raise Exception(f"Unauthorized fetching factory manifest for {plugin_name}.")
            else:
                raise Exception(f"Failed to fetch factory manifest for {plugin_name}: {r.status_code} - {r.text[:500]}")

    # ------------- Version Checking -------------
    def get_current_version(self):
        core_manifest = self.plugin_manager.get_core_manifest()  # Local core manifest
        return core_manifest.get('version', 'Unknown')

    def get_latest_version(self):
        core_manifest = self.get_core_manifest_remote()
        return core_manifest.get('core', {}).get('current_version', 'Unknown')

    def get_plugin_latest_version(self, plugin_name):
        plugin = self.get_plugin_manifest_remote(plugin_name)
        return plugin.get('current_version', 'Unknown')

    def get_plugins_versions(self):
        plugins_versions = {}
        plugins = self.plugin_manager.get_all_plugins()
        for plugin in plugins:
            plugin_name = plugin["system_name"]
            plugin_versions = self.plugin_manager.get_plugin(plugin_name)
            plugins_versions[plugin_name] = plugin_versions.get("version", "Unknown")
        return plugins_versions

    def get_update_status(self):
        current_version = self.get_current_version()
        latest_version = self.get_latest_version()
        core_update_available = self._version_tuple(current_version) < self._version_tuple(latest_version)

        plugins_versions = self.get_plugins_versions()
        plugin_updates = []
        for plugin_name, plugin_version in plugins_versions.items():
            try:
                plugin_latest_version = self.get_plugin_latest_version(plugin_name)
                plugin_update_available = self._version_tuple(plugin_version) < self._version_tuple(plugin_latest_version)
            except Exception as e:
                plugin_latest_version = "Unknown"
                plugin_update_available = False
                print(f"Error determining latest version for plugin {plugin_name}: {e}")
            plugin_updates.append({
                "plugin_name": plugin_name,
                "current_version": plugin_version,
                "latest_version": plugin_latest_version,
                "update_available": plugin_update_available
            })

        return {
            "core": {
                "current_version": current_version,
                "latest_version": latest_version,
                "update_available": core_update_available
            },
            "plugins": plugin_updates
        }

    # ------------- URL Conversion -------------
    def convert_to_api_endpoint(self, download_url):
        api_prefix = "https://gitlab.com/api/v4/"
        if download_url.startswith(api_prefix):
            return download_url

        parsed = urlparse(download_url)
        path_parts = parsed.path.split('/')
        # Expected web raw form: /<namespace>/<project>/-/raw/<branch>/<file path...>
        if len(path_parts) < 7 or path_parts[3] != '-' or path_parts[4] != 'raw':
            return download_url

        namespace = path_parts[1]
        project = path_parts[2]
        branch = path_parts[5]
        file_path = "/".join(path_parts[6:])
        project_identifier = quote(f"{namespace}/{project}", safe='')
        encoded_file_path = quote(file_path, safe='')
        return f"https://gitlab.com/api/v4/projects/{project_identifier}/repository/files/{encoded_file_path}/raw?ref={branch}"

    # ------------- Backup and Restore -------------
    def backup(self, backup_name="whole_system_backup"):
        import zipfile
        system_root = os.path.dirname(self.BACKUP_DIR) # typically "app"
        os.makedirs(self.BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"{backup_name}_{timestamp}.zip"
        backup_path = os.path.join(self.BACKUP_DIR, backup_filename)
        # Exclude heavy or generated directories
        exclude_dirs = {
            os.path.normpath(self.BACKUP_DIR),                          # app/backups
            os.path.normpath(self.UPDATE_DIR),                          # app/updates
            os.path.normpath(os.path.join(system_root, "logs")),        # app/logs
            os.path.normpath(os.path.join(system_root, "tmp")),         # app/tmp
            os.path.normpath(os.path.join(system_root, "node_modules")),
            os.path.normpath(os.path.join(system_root, "venv")),
            os.path.normpath(os.path.join(system_root, ".venv")),
            os.path.normpath(os.path.join(system_root, "__pycache__")),
            os.path.normpath(os.path.join(system_root, "media")),
            os.path.normpath(os.path.join(system_root, "uploads")),
        }

        def is_excluded(path):
            p = os.path.normpath(path)
            for ex in exclude_dirs:
                if p == ex or p.startswith(ex + os.sep):
                    return True
            return False

        # Optional: print some progress every N files
        file_counter = 0
        progress_step = 1000

        with zipfile.ZipFile(backup_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(system_root):
                # prune excluded dirs to speed up traversal
                dirs[:] = [d for d in dirs if not is_excluded(os.path.join(root, d))]
                for fn in files:
                    fp = os.path.join(root, fn)
                    if is_excluded(fp):
                        continue
                    arcname = os.path.relpath(fp, system_root)
                    try:
                        zf.write(fp, arcname)
                    except FileNotFoundError:
                        # File changed/removed during walk — skip
                        continue
                    file_counter += 1
                    if file_counter % progress_step == 0:
                        print(f"Backup progress: {file_counter} files zipped...")

        print(f"Backup completed: {backup_path} ({file_counter} files)")
        return backup_path

    def restore_backup(self, backup_name, restore_to):
        backup_zip = os.path.join(self.BACKUP_DIR, f"{backup_name}.zip")
        if not os.path.exists(backup_zip):
            raise Exception(f"Backup {backup_zip} not found.")
        os.makedirs(restore_to, exist_ok=True)
        with zipfile.ZipFile(backup_zip, 'r') as zip_ref:
            zip_ref.extractall(restore_to)

    # ------------- Update Installation -------------
    def download_update(self, url, save_path):
        with self._gitlab_session() as s:
            s.headers.update({"Accept": "*/*"})  # binary
            r = s.get(url, stream=True)
            if r.status_code == 200:
                with open(save_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            elif r.status_code == 401:
                raise Exception(f"Unauthorized downloading update from {url} (401).")
            else:
                snippet = ""
                try:
                    snippet = r.text[:500]
                except Exception:
                    pass
                raise Exception(f"Failed to download update from {url}: {r.status_code} - {snippet}")

    def apply_zip(self, zip_path, target_path):
        if os.path.exists(zip_path):
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(target_path)
        else:
            raise Exception(f"Update file not found: {zip_path}")

    def run_update_instructions(self, script_path):
        try:
            print(f"Executing update instructions from {script_path}...")
            subprocess.run(["python", script_path], check=True)
            print("Update instructions executed successfully.")
        except subprocess.CalledProcessError as e:
            raise Exception(f"Running update instructions failed: {e}")

    # ------------- Detailed Logging -------------
    def log_update(self, update_type, name, update_mode, old_version, new_version, status, details):
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "update_type": update_type,
            "name": name,
            "update_mode": update_mode,
            "old_version": old_version,
            "new_version": new_version,
            "status": status,
            "details": details
        }
        os.makedirs(os.path.dirname(self.LOG_PATH), exist_ok=True)
        with open(self.LOG_PATH, 'a') as log_file:
            json.dump(log_entry, log_file)
            log_file.write("\n")

    # ------------- Update Application -------------
    def apply_update(self, update_type, plugin_name=None, update_mode="manual"):
        backup_archive = None
        system_root = os.path.dirname(self.BACKUP_DIR) # already defined earlier as "app"
        plugin_path = os.path.join(system_root, "plugins", plugin_name)
        old_version = None
        try:
            # Determine old version
            if update_type == "core":
                old_version = self.get_current_version()
            elif update_type == "plugin" and plugin_name:
                old_version = self.plugin_manager.get_plugin(plugin_name).get("version", "Unknown")

            print("Creating whole system backup...")
            backup_archive = self.backup()  # Whole system backup
            print(f"Whole system backup created: {backup_archive}")

            if update_type == "core":
                print("Fetching core manifest...")
                core_manifest = self.get_core_manifest_remote()

                print("Downloading core update...")
                download_url = core_manifest.get('core', {}).get('download_url')
                if not download_url:
                    raise Exception("No download URL for core update.")
                download_url = self.convert_to_api_endpoint(download_url)
                zip_path = os.path.join(self.UPDATE_DIR, "core_update.zip")
                self.download_update(download_url, zip_path)

                print("Applying core update...")
                self.apply_zip(zip_path, system_root)

                # Update local manifest version (app/config/manifest.json)
                manifest_path = os.path.join(system_root, "config", "manifest.json")
                new_version = core_manifest.get('core', {}).get('current_version')
                if os.path.exists(manifest_path):
                    with open(manifest_path, 'r') as f:
                        try:
                            manifest_data = json.load(f)
                        except json.JSONDecodeError:
                            manifest_data = {}
                else:
                    manifest_data = {}
                manifest_data['version'] = new_version
                os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
                with open(manifest_path, 'w') as f:
                    json.dump(manifest_data, f, indent=4)
                print(f"Manifest version updated to {new_version}.")

                update_script = os.path.join(system_root, "update_instructions.py")
                if os.path.exists(update_script):
                    self.run_update_instructions(update_script)
                print("Core update completed.")

                self.log_update("core", "core", update_mode, old_version, new_version, "success", "Core update applied successfully.")

            elif update_type == "plugin" and plugin_name:
                print(f"Fetching plugin manifest for {plugin_name}...")
                plugin_manifest = self.get_plugin_manifest_remote(plugin_name)

                print(f"Downloading plugin update for {plugin_name}...")
                download_url = plugin_manifest.get('download_url')
                if not download_url:
                    raise Exception(f"No download URL for plugin {plugin_name} update.")
                download_url = self.convert_to_api_endpoint(download_url)
                zip_path = os.path.join(self.UPDATE_DIR, f"{plugin_name}_update.zip")
                self.download_update(download_url, zip_path)

                plugin_path = os.path.join(plugin_path, plugin_name)
                print(f"Applying plugin update for {plugin_name}...")
                os.makedirs(plugin_path, exist_ok=True)
                self.apply_zip(zip_path, plugin_path)

                update_script = os.path.join(plugin_path, "update_instructions.py")
                if os.path.exists(update_script):
                    self.run_update_instructions(update_script)
                print(f"{plugin_name} update completed.")

                new_version = plugin_manifest.get('current_version')
                self.log_update("plugin", plugin_name, update_mode, old_version, new_version, "success", f"Plugin {plugin_name} update applied successfully.")

            else:
                raise Exception("Invalid update type.")

            print("Update applied successfully.")

        except Exception as e:
            error_message = str(e)
            print(f"Update failed: {error_message}")
            try:
                self.log_update(update_type, plugin_name if plugin_name else "core", update_mode, old_version, "Unknown", "error", error_message)
            except Exception as le:
                print(f"Failed to write update log: {le}")
            if backup_archive:
                try:
                    print("Rolling back to the previous whole system backup...")
                    backup_name = os.path.basename(backup_archive).replace('.zip', '')
                    self.restore_backup(backup_name, system_root)
                    print("System restored from backup.")
                except Exception as re:
                    print(f"Rollback failed: {re}")
            raise

    def schedule_update(self, update_type, scheduled_time, plugin_name=None):
        run_date = datetime.strptime(scheduled_time, "%Y-%m-%d %H:%M:%S")
        if update_type == "core":
            self.scheduler.add_job(self.apply_update, 'date', run_date=run_date, args=['core', None, "scheduled"])
        elif update_type == "plugin" and plugin_name:
            self.scheduler.add_job(self.apply_update, 'date', run_date=run_date, args=['plugin', plugin_name, "scheduled"])
        else:
            raise Exception(f"Invalid update type: {update_type}")
        print(f"Update scheduled for {update_type} at {run_date}")

    def get_changelog_for_plugin(self, plugin_name):
        try:
            data = self.get_plugin_manifest_remote(plugin_name)
            return data.get('changelog', 'No changelog available.')
        except Exception as e:
            print(f"Failed to fetch changelog for plugin {plugin_name}: {e}")
            return 'No changelog available.'
    def get_changelog_for_core(self):
        try:
            core_manifest = self.get_core_manifest_remote()
            return core_manifest.get('core', {}).get('changelog', 'No changelog available.')
        except Exception as e:
            print(f"Failed to fetch changelog for core module: {e}")
            return 'No changelog available.'
    
    def get_remote_plugin_list(self):
        url = "https://gitlab.com/api/v4/projects/sparrow-erp%2FCore/repository/tree?path=plugins&ref=main&per_page=100"
        with self._gitlab_session() as s:
            r = s.get(url)
            if r.status_code == 200:
                try:
                    nodes = r.json()
                except Exception:
                    raise Exception(f"Invalid JSON from plugin list. Body: {r.text[:500]}")
                # Return only folder names under plugins
                return [n['name'] for n in nodes if n.get('type') == 'tree']
            elif r.status_code == 401:
                raise Exception("Unauthorized fetching plugin list (401).")
            else:
                raise Exception(f"Failed to fetch plugin list: {r.status_code} - {r.text[:500]}")

    def install_plugin(self, plugin_name):
        plugin_manifest = self.get_plugin_manifest_remote(plugin_name)
        download_url = plugin_manifest.get('download_url')
        if not download_url:
            raise Exception(f"No download URL found for plugin {plugin_name}")
        download_url = self.convert_to_api_endpoint(download_url)
        os.makedirs(self.PLUGIN_PATH, exist_ok=True)
        zip_path = os.path.join(self.PLUGIN_PATH, f"{plugin_name}_update.zip")
        self.download_update(download_url, zip_path)
        plugin_target = os.path.join(self.PLUGIN_PATH, plugin_name)
        os.makedirs(plugin_target, exist_ok=True)
        self.apply_zip(zip_path, plugin_target)
        update_script = os.path.join(plugin_target, "update_instructions.py")
        if os.path.exists(update_script):
            self.run_update_instructions(update_script)
        print(f"Plugin {plugin_name} installed successfully.")

    def check_and_download_new_plugins(self):
        print("Checking for new plugins...")
        remote_plugins = self.get_remote_plugin_list()
        installed_plugins = [plugin["system_name"] for plugin in self.plugin_manager.get_all_plugins()]
        for plugin_name in remote_plugins:
            if plugin_name not in installed_plugins:
                print(f"New plugin found: {plugin_name}. Downloading and installing...")
                try:
                    self.install_plugin(plugin_name)
                except Exception as e:
                    print(f"Failed to install plugin {plugin_name}: {e}")
            else:
                print(f"Plugin {plugin_name} is already installed.")

    # --- Forced Update Check for Core ---
    def check_for_forced_updates(self):
        try:
            core_manifest = self.get_core_manifest_remote()
            force_update = core_manifest.get('core', {}).get('force_update', False)
            if force_update:
                if not self.scheduler.get_job("forced_core_update"):
                    now = datetime.now()
                    next_3am = now.replace(hour=3, minute=0, second=0, microsecond=0)
                    if next_3am <= now:
                        next_3am += timedelta(days=1)
                    print(f"Force update flag detected in core manifest. Scheduling core update at {next_3am}")
                    self.scheduler.add_job(
                        self.apply_update, 'date', run_date=next_3am, args=['core', None, "forced"],
                        id="forced_core_update", name="Forced Core Update"
                    )
            else:
                if self.scheduler.get_job("forced_core_update"):
                    self.scheduler.remove_job("forced_core_update")
        except Exception as e:
            print(f"Error checking forced updates: {e}")

    # --- Forced Update Check for Plugins ---
    def check_for_forced_plugin_updates(self):
        try:
            installed_plugins = [plugin["system_name"] for plugin in self.plugin_manager.get_all_plugins()]
            for plugin_name in installed_plugins:
                try:
                    data = self.get_plugin_manifest_remote(plugin_name)
                    force_update = data.get('force_update', False)
                    job_id = f"forced_plugin_update_{plugin_name}"
                    if force_update:
                        if not self.scheduler.get_job(job_id):
                            now = datetime.now()
                            next_3am = now.replace(hour=3, minute=0, second=0, microsecond=0)
                            if next_3am <= now:
                                next_3am += timedelta(days=1)
                            print(f"Force update flag detected for plugin {plugin_name}. Scheduling update at {next_3am}")
                            self.scheduler.add_job(
                                self.apply_update, 'date', run_date=next_3am, args=['plugin', plugin_name, "forced"],
                                id=job_id, name=f"Forced Plugin Update {plugin_name}"
                            )
                    else:
                        if self.scheduler.get_job(job_id):
                            self.scheduler.remove_job(job_id)
                except Exception as e:
                    print(f"Error checking forced update for plugin {plugin_name}: {e}")
        except Exception as e:
            print(f"Error checking forced plugin updates: {e}")    

import os
import json

class Plugin:
    def __init__(self, system_name, plugins_dir='app/plugins'):
        self.system_name = system_name
        self.plugins_dir = plugins_dir
        self.plugin_path = os.path.join(self.plugins_dir, self.system_name)
        self.manifest_path = os.path.join(self.plugin_path, 'manifest.json')
        print(f"[DEBUG] Initialising Plugin object for '{self.system_name}'")
        self.manifest = self.get_manifest()
        if self.manifest:
            print(f"[DEBUG] Manifest loaded for '{self.system_name}': {self.manifest}")
        else:
            print(f"[DEBUG] No manifest found for '{self.system_name}'")

    def get_manifest(self):
        """Load the plugin's manifest file."""
        manifest_path = os.path.join(self.plugins_dir, self.system_name, 'manifest.json')
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, 'r') as f:
                    data = json.load(f)
                    return data
            except json.JSONDecodeError as e:
                print(f"[ERROR] Error decoding JSON for plugin '{self.system_name}': {e}")
        else:
            print(f"[DEBUG] Manifest file does not exist for '{self.system_name}'")
        return None

    def save_manifest(self):
        """Save the current state of the plugin's manifest."""
        manifest_path = os.path.join(self.plugins_dir, self.system_name, 'manifest.json')
        print(f"[DEBUG] Saving manifest for '{self.system_name}' at '{manifest_path}'")
        try:
            with open(manifest_path, 'w') as f:
                json.dump(self.manifest, f, indent=4)
            print(f"[DEBUG] Manifest saved successfully for '{self.system_name}'")
        except Exception as e:
            print(f"[ERROR] Failed to save manifest for '{self.system_name}': {e}")

    def install(self):
        """Install the plugin using the factory manifest located in the plugin folder."""
        print(f"[DEBUG] Starting installation for plugin '{self.system_name}'")
        plugin_path = os.path.join(self.plugins_dir, self.system_name)
        
        # Ensure the plugin directory exists.
        if not os.path.exists(plugin_path):
            print(f"[DEBUG] Plugin directory '{plugin_path}' not found. Creating it.")
            os.makedirs(plugin_path)
        else:
            print(f"[DEBUG] Plugin directory '{plugin_path}' exists.")

        # If a manifest already exists, remove it to allow a fresh install.
        if os.path.exists(self.manifest_path):
            print(f"[DEBUG] Plugin manifest exists for '{self.system_name}'; re-installing by removing existing manifest.")
            try:
                os.remove(self.manifest_path)
                print(f"[DEBUG] Existing manifest removed for '{self.system_name}'.")
            except Exception as e:
                error_msg = f"Failed to remove existing manifest: {e}"
                print(f"[ERROR] {error_msg}")
                return False, error_msg

        # Load the factory manifest from the plugin folder.
        factory_manifest_path = os.path.join(plugin_path, 'factory_manifest.json')
        print(f"[DEBUG] Looking for factory manifest at '{factory_manifest_path}'")
        if not os.path.exists(factory_manifest_path):
            error_msg = "Factory manifest not found."
            print(f"[ERROR] {error_msg}")
            return False, error_msg

        try:
            with open(factory_manifest_path, 'r') as f:
                factory_manifest = json.load(f)
            print(f"[DEBUG] Factory manifest loaded for '{self.system_name}': {factory_manifest}")
        except json.JSONDecodeError as e:
            error_msg = f"Error reading factory manifest: {e}"
            print(f"[ERROR] {error_msg}")
            return False, error_msg

        # Initialize the plugin manifest from the factory manifest.
        self.manifest = factory_manifest
        self.manifest['enabled'] = False  # Ensure plugin is installed disabled.
        print(f"[DEBUG] Setting 'enabled' to False for plugin '{self.system_name}'.")

        # Save the plugin manifest.
        self.save_manifest()
        print(f"[DEBUG] Plugin '{self.system_name}' installed successfully.")
        return True, f"{self.system_name} installed successfully."

    def uninstall(self):
        """Uninstall the plugin by removing its manifest file."""
        print(f"[DEBUG] Uninstalling plugin '{self.system_name}'")
        if not os.path.exists(self.plugin_path):
            error_msg = "Plugin directory not found."
            print(f"[ERROR] {error_msg}")
            return False, error_msg
        try:
            if os.path.exists(self.manifest_path):
                os.remove(self.manifest_path)
                print(f"[DEBUG] Removed manifest for plugin '{self.system_name}'")
            return True, f"{self.system_name} uninstalled successfully."
        except Exception as e:
            error_msg = f"Error uninstalling plugin: {e}"
            print(f"[ERROR] {error_msg}")
            return False, error_msg

    def enable(self):
        """Enable the plugin by setting 'enabled' to True and saving the manifest."""
        print(f"[DEBUG] Enabling plugin '{self.system_name}'")
        if not self.manifest:
            error_msg = "Manifest not found."
            print(f"[ERROR] {error_msg}")
            return False, error_msg
        self.manifest['enabled'] = True
        self.save_manifest()
        return True, f"{self.system_name} enabled successfully."

    def disable(self):
        """Disable the plugin by setting 'enabled' to False and saving the manifest."""
        print(f"[DEBUG] Disabling plugin '{self.system_name}'")
        if not self.manifest:
            error_msg = "Manifest not found."
            print(f"[ERROR] {error_msg}")
            return False, error_msg
        self.manifest['enabled'] = False
        self.save_manifest()
        return True, f"{self.system_name} disabled successfully."

    def get_settings(self):
        """Return the settings of the plugin, or an empty dictionary if not defined."""
        if not self.manifest or 'settings' not in self.manifest:
            print(f"[DEBUG] No settings found in manifest for '{self.system_name}'")
            return {}
        return self.manifest['settings']

    def save_settings(self, settings):
        """Save the provided settings to the plugin's manifest."""
        print(f"[DEBUG] Saving settings for plugin '{self.system_name}'")
        if not self.manifest:
            error_msg = "Manifest not found."
            print(f"[ERROR] {error_msg}")
            return False, error_msg
        self.manifest['settings'] = settings
        self.save_manifest()
        return True, "Settings saved successfully."

    def update_setting(self, setting_key, setting_value):
        """Update a setting if it's editable."""
        print(f"[DEBUG] Updating setting '{setting_key}' for plugin '{self.system_name}'")
        if not self.manifest or 'settings' not in self.manifest:
            error_msg = "Settings not found in the manifest."
            print(f"[ERROR] {error_msg}")
            return False, error_msg

        settings = self.manifest['settings']
        if setting_key not in settings:
            error_msg = f"Setting {setting_key} not found."
            print(f"[ERROR] {error_msg}")
            return False, error_msg

        setting = settings[setting_key]
        if not setting.get('editable', False):
            error_msg = f"Setting {setting_key} is not editable."
            print(f"[ERROR] {error_msg}")
            return False, error_msg

        setting['value'] = setting_value
        self.save_manifest()
        print(f"[DEBUG] Setting '{setting_key}' updated successfully for plugin '{self.system_name}'")
        return True, f"Setting {setting_key} updated successfully."

    @staticmethod
    def get_factory_manifest(plugin_path):
        """Load the factory manifest data from factory_manifest.json."""
        factory_manifest_path = os.path.join(plugin_path, "factory_manifest.json")
        print(f"[DEBUG] Loading factory manifest from '{factory_manifest_path}'")
        if os.path.exists(factory_manifest_path):
            try:
                with open(factory_manifest_path, "r") as factory_manifest_file:
                    manifest_data = json.load(factory_manifest_file)
                    print(f"[DEBUG] Factory manifest data: {manifest_data}")
                    return manifest_data
            except (json.JSONDecodeError, IOError) as e:
                print(f"[ERROR] Error reading factory_manifest.json for {plugin_path}: {e}")
        else:
            print(f"[DEBUG] Factory manifest not found at '{factory_manifest_path}'")
        return None




import os
import json
import importlib
from flask_login import current_user

import os
import sys
import json
import subprocess
import importlib
from pathlib import Path

class PluginManager:
    def __init__(self, plugins_dir='plugins'):
        # Use the absolute path to avoid confusion.
        app_root = os.path.abspath(os.path.dirname(__file__))  # e.g. sparrow-erp/app
        self.plugins_dir = os.path.join(app_root, plugins_dir)
        self.config_dir = os.path.join(app_root, 'config')
        print(f"[DEBUG] PluginManager initialized with plugins_dir: {self.plugins_dir}")
        self.plugins = self.load_plugins()  # Loads manifest data for all plugins.
        print(f"[DEBUG] Loaded plugins (manifests): {list(self.plugins.keys())}")

    def get_factory_manifest_by_name(self, plugin_name):
        """
        Return a minimal 'factory' descriptor for a plugin by name.
        Priority:
        1) Local manifest.json (repository field if present)
        2) factory_manifest.json inside the plugin folder
        3) Default to 'official'
        """
        plugin_folder = os.path.join(self.plugins_dir, plugin_name)
        # Try local manifest.json
        manifest_path = os.path.join(plugin_folder, 'manifest.json')
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, 'r') as f:
                    man = json.load(f)
                repo = man.get('repository')
                if repo:
                    return {"repository": repo}
            except json.JSONDecodeError:
                pass
        # Try local factory_manifest.json
        factory_manifest = self.get_factory_manifest(plugin_folder)
        if factory_manifest and isinstance(factory_manifest, dict):
            repo = factory_manifest.get('repository')
            if repo:
                return {"repository": repo}
        # Fallback
        return {"repository": "official"}

    def get_repository_for_plugin(self, plugin_name):
        """
        Convenience wrapper to return the repository string for a plugin:
        - 'official' or a base URL (e.g., https://gitlab.com/yourgroup/yourrepo)
        """
        data = self.get_factory_manifest_by_name(plugin_name)
        return (data or {}).get('repository', 'official')

    def load_plugin_modules(self):
        """
        Dynamically import and return a list of plugin modules.
        Each plugin is assumed to be a folder (with __init__.py) under self.plugins_dir.
        """
        plugin_modules = []
        if not os.path.exists(self.plugins_dir):
            print(f"[ERROR] Plugins folder does not exist: {self.plugins_dir}")
            return plugin_modules
        for plugin_folder in os.listdir(self.plugins_dir):
            folder_path = os.path.join(self.plugins_dir, plugin_folder)
            if not os.path.isdir(folder_path) or plugin_folder.startswith("__"):
                continue
            try:
                module = importlib.import_module(f"app.plugins.{plugin_folder}")
                plugin_modules.append(module)
                print(f"[DEBUG] Imported plugin module: {plugin_folder}")
            except Exception as e:
                print(f"[ERROR] Failed to import plugin module '{plugin_folder}': {e}")
        return plugin_modules

    def load_plugins(self):
        """
        Load plugin data from the plugins directory.
        For each plugin folder, if a manifest.json exists, load it.
        Otherwise, if a factory_manifest.json exists, load that and set 'enabled': False.
        """
        plugins = {}
        if not os.path.exists(self.plugins_dir):
            print(f"[ERROR] Plugins folder does not exist: {self.plugins_dir}")
            return plugins
        for plugin_folder in os.listdir(self.plugins_dir):
            folder_path = os.path.join(self.plugins_dir, plugin_folder)
            if not os.path.isdir(folder_path) or plugin_folder.startswith("__"):
                continue
            manifest_path = os.path.join(folder_path, 'manifest.json')
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, 'r') as f:
                        manifest = json.load(f)
                    print(f"[DEBUG] Plugin '{plugin_folder}' loaded from manifest: {manifest}")
                except json.JSONDecodeError as e:
                    print(f"[ERROR] Error decoding manifest for plugin '{plugin_folder}': {e}")
                    continue
            else:
                factory_manifest = self.get_factory_manifest(folder_path)
                if factory_manifest:
                    manifest = factory_manifest
                    manifest['enabled'] = False
                    print(f"[DEBUG] Plugin '{plugin_folder}' loaded from factory manifest: {manifest}")
                else:
                    print(f"[DEBUG] No manifest found for plugin '{plugin_folder}'")
                    continue
            if 'allowed_roles' not in manifest:
                manifest['allowed_roles'] = []
            plugins[plugin_folder] = manifest
        return plugins

    def get_factory_manifest(self, plugin_path):
        """Retrieve the factory manifest for a plugin."""
        factory_manifest_path = os.path.join(plugin_path, 'factory_manifest.json')
        print(f"[DEBUG] Loading factory manifest from: {factory_manifest_path}")
        if os.path.exists(factory_manifest_path):
            try:
                with open(factory_manifest_path, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError as e:
                print(f"[ERROR] Error decoding factory_manifest.json for {plugin_path}: {e}")
        return None

    def get_plugin_manifest(self, plugin_path):
        """Retrieve the manifest for a plugin."""
        manifest_path = os.path.join(plugin_path, 'manifest.json')
        print(f"[DEBUG] Loading plugin manifest from: {manifest_path}")
        if os.path.exists(manifest_path):
            try:
                return json.load(open(manifest_path, 'r'))
            except json.JSONDecodeError as e:
                print(f"[ERROR] Error decoding manifest.json for {plugin_path}: {e}")
        return None

    def get_all_plugins(self):
        """Returns a list of all available plugins with their details."""
        plugin_list = []
        if not os.path.exists(self.plugins_dir):
            print(f"[ERROR] Plugins folder does not exist: {self.plugins_dir}")
            return plugin_list

        print(f"[DEBUG] Scanning plugins in: {self.plugins_dir}")
        for plugin_name, manifest in self.plugins.items():
            data = {
                "name": manifest.get("name", plugin_name),
                "description": manifest.get("description", "No description available."),
                "icon": manifest.get("icon", "default-icon.png"),
                "system_name": manifest.get("system_name", plugin_name.lower()),
                "version": manifest.get("version", "Unknown"),
                "update_available": manifest.get("update_available", False),
                "installed": os.path.exists(os.path.join(self.plugins_dir, plugin_name, 'manifest.json')),
                "enabled": manifest.get("enabled", False)
            }
            plugin_list.append(data)
            print(f"[DEBUG] Plugin data for '{plugin_name}': {data}")
        return plugin_list

    def get_enabled_plugins(self):
        """Retrieve all enabled plugins with their name and system_name."""
        plugins = []
        for plugin_folder in os.listdir(self.plugins_dir):
            plugin_path = os.path.join(self.plugins_dir, plugin_folder)
            if os.path.isdir(plugin_path):
                manifest_path = os.path.join(plugin_path, 'manifest.json')
                if os.path.exists(manifest_path):
                    try:
                        with open(manifest_path, 'r') as f:
                            manifest = json.load(f)
                        if manifest.get("enabled", False):
                            plugins.append({
                                "name": manifest.get("name", plugin_folder),
                                "system_name": plugin_folder
                            })
                    except json.JSONDecodeError as e:
                        print(f"[ERROR] Error decoding manifest for plugin '{plugin_folder}': {e}")
        print(f"[DEBUG] Enabled plugins: {plugins}")
        return plugins

    def is_plugin_enabled(self, system_name):
        """
        Checks if the plugin is enabled based on its manifest.
        Returns True if enabled, False otherwise.
        """
        print(f"[DEBUG] Checking if plugin '{system_name}' is enabled.")
        plugins = self.load_plugins()
        if system_name not in plugins:
            raise ValueError(f"Plugin {system_name} not found. Available: {list(plugins.keys())}")
        enabled = plugins[system_name].get('enabled', False)
        print(f"[DEBUG] Plugin '{system_name}' enabled status: {enabled}")
        return enabled

    def install_plugin(self, plugin_name):
        """Install a plugin (using its factory manifest if not installed) and handle dependencies."""
        print(f"[DEBUG] Attempting to install plugin '{plugin_name}'")
        plugin_folder = os.path.join(self.plugins_dir, plugin_name)
        if not os.path.isdir(plugin_folder):
            raise ValueError(f"Plugin folder '{plugin_name}' not found in {self.plugins_dir}.")
        self.plugins = self.load_plugins()  # Update plugin list.
        manifest_path = os.path.join(plugin_folder, 'manifest.json')
        if os.path.exists(manifest_path):
            print(f"[DEBUG] Plugin '{plugin_name}' is already installed; skipping installation.")
        else:
            from app.objects import Plugin  # Adjust the import path as needed.
            plugin = Plugin(plugin_name)
            install_status, install_message = plugin.install()
            if not install_status:
                raise Exception(f"Plugin installation failed: {install_message}")
            print(f"[DEBUG] Plugin '{plugin_name}' installed successfully.")
        dependency_handler_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "dependency_handler.py")
        print(f"[DEBUG] Running dependency handler at: {dependency_handler_path}")
        if os.path.exists(dependency_handler_path):
            try:
                subprocess.check_call([sys.executable, dependency_handler_path])
                print(f"[DEBUG] Dependency handler executed successfully.")
            except subprocess.CalledProcessError as e:
                print(f"[ERROR] Dependency handler failed: {e}")
                raise
        else:
            print(f"[WARNING] Dependency handler not found at: {dependency_handler_path}")
        self.plugins = self.load_plugins()  # Reload plugins.
        print(f"[DEBUG] Plugin '{plugin_name}' installation complete.")
        return True, f"Plugin {plugin_name} installed successfully."

    def check_dependencies(self, system_name):
        """
        Checks whether the specified plugin has any missing dependencies.
        Returns a tuple (can_enable, missing_dependency).
        """
        print(f"[DEBUG] Checking dependencies for plugin '{system_name}'")
        plugins = self.load_plugins()
        if system_name not in plugins:
            raise ValueError(f"Plugin {system_name} not found.")
        dependencies = plugins[system_name].get('dependencies', [])
        if not dependencies:
            print(f"[DEBUG] Plugin '{system_name}' has no dependencies.")
            return True, None
        missing = []
        for dependency in dependencies:
            dep_manifest_path = os.path.join(self.plugins_dir, dependency, 'manifest.json')
            if not os.path.exists(dep_manifest_path):
                print(f"[DEBUG] Dependency '{dependency}' is missing for plugin '{system_name}'.")
                missing.append(dependency)
            else:
                print(f"[DEBUG] Dependency '{dependency}' is present for plugin '{system_name}'. Enabling it.")
                self.enable_plugin(dependency)
        if missing:
            return False, missing
        return True, None

    def get_dependents(self, plugin_name):
        """Find which plugins depend on a given plugin."""
        print(f"[DEBUG] Looking for dependents of plugin '{plugin_name}'")
        dependents = []
        for key, manifest in self.plugins.items():
            if plugin_name in manifest.get('dependencies', []):
                dependents.append(key)
        print(f"[DEBUG] Dependents for plugin '{plugin_name}': {dependents}")
        return dependents

    def enable_plugin(self, system_name):
        """Enable a plugin."""
        print(f"[DEBUG] Enabling plugin '{system_name}'")
        plugin_folder = os.path.join(self.plugins_dir, system_name)
        manifest_path = os.path.join(plugin_folder, 'manifest.json')
        if not os.path.exists(manifest_path):
            return False, f"{system_name} is not installed."
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        if manifest.get('enabled', False):
            print(f"[DEBUG] Plugin '{system_name}' is already enabled.")
            return True, f"{system_name} is already enabled."
        can_enable, missing_dependency = self.check_dependencies(system_name)
        if not can_enable and missing_dependency:
            for dep in missing_dependency:
                print(f"[DEBUG] Installing missing dependency '{dep}' for plugin '{system_name}' during enabling.")
                install_status, message = self.install_plugin(dep)
                if not install_status:
                    return False, f"Cannot enable {system_name}: {message}"
                self.enable_plugin(dep)
        manifest['enabled'] = True
        manifest['update_available'] = False
        with open(manifest_path, 'w') as f:
            json.dump(manifest, f, indent=4)
        try:
            print(f"[DEBUG] Running dependency handler after enabling plugin '{system_name}'...")
            dependency_handler_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "dependency_handler.py")
            subprocess.check_call([sys.executable, dependency_handler_path])
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Dependency handler failed after enabling plugin '{system_name}': {e}")
            raise
        print(f"[DEBUG] Plugin '{system_name}' enabled successfully.")
        return True, f"{system_name} enabled successfully."

    def disable_plugin(self, system_name):
        """Disable a plugin and its dependents."""
        print(f"[DEBUG] Disabling plugin '{system_name}'")
        plugin_folder = os.path.join(self.plugins_dir, system_name)
        manifest_path = os.path.join(plugin_folder, 'manifest.json')
        if not os.path.exists(manifest_path):
            return False, f"{system_name} is not installed."
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        manifest['enabled'] = False
        manifest['update_available'] = True
        with open(manifest_path, 'w') as f:
            json.dump(manifest, f, indent=4)
        print(f"[DEBUG] Plugin '{system_name}' disabled.")
        dependents = self.get_dependents(system_name)
        for dep in dependents:
            dep_folder = os.path.join(self.plugins_dir, dep)
            dep_manifest_path = os.path.join(dep_folder, 'manifest.json')
            if os.path.exists(dep_manifest_path):
                with open(dep_manifest_path, 'r') as f:
                    dep_manifest = json.load(f)
                if dep_manifest.get('enabled', False):
                    dep_manifest['enabled'] = False
                    dep_manifest['update_available'] = True
                    with open(dep_manifest_path, 'w') as f:
                        json.dump(dep_manifest, f, indent=4)
                    print(f"[DEBUG] Disabled dependent plugin: {dep}")
        return True, f"{system_name} and its dependents have been disabled."

    def update_plugin_manifest(self, plugin_name, update_flag):
        """Update the 'update_available' flag in the plugin's manifest."""
        plugin_folder = os.path.join(self.plugins_dir, plugin_name)
        manifest = self.get_plugin_manifest(plugin_folder)
        if manifest:
            manifest['update_available'] = update_flag
            self.save_plugin_manifest(plugin_name, manifest)

    def save_plugin_manifest(self, plugin_name, plugin_manifest):
        manifest_path = os.path.join(self.plugins_dir, plugin_name, 'manifest.json')
        print(f"[DEBUG] Saving updated manifest for plugin '{plugin_name}' at '{manifest_path}'")
        with open(manifest_path, 'w') as f:
            json.dump(plugin_manifest, f, indent=4)

    def get_core_manifest(self):
        """Load the core module's manifest file (config/manifest.json)."""
        core_manifest_path = os.path.join(self.config_dir, 'manifest.json')
        print(f"[DEBUG] Loading core manifest from '{core_manifest_path}'")
        if os.path.exists(core_manifest_path):
            with open(core_manifest_path, 'r') as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    print(f"[ERROR] Error reading core manifest at {core_manifest_path}.")
        else:
            print(f"[ERROR] Core manifest not found at {core_manifest_path}.")
        return None

    def get_core_manifest_path(self):
        """Return the core manifest file path."""
        core_manifest_path = os.path.join(self.config_dir, 'manifest.json')
        if os.path.exists(core_manifest_path):
            return core_manifest_path
        else:
            print(f"[ERROR] Core manifest not found at {core_manifest_path}.")
        return core_manifest_path

    def update_plugin_settings(self, plugin_system_name, form_data):
        """Update the plugin settings from the form data."""
        plugin_folder = os.path.join(self.plugins_dir, plugin_system_name)
        manifest_path = os.path.join(plugin_folder, 'manifest.json')
        print(f"[DEBUG] Updating settings for plugin '{plugin_system_name}'")
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r') as f:
                manifest = json.load(f)
            for key, value in form_data.items():
                if key in manifest.get('settings', {}):
                    manifest['settings'][key]['value'] = value
                    print(f"[DEBUG] Updated setting '{key}' to '{value}'")
            with open(manifest_path, 'w') as f:
                json.dump(manifest, f, indent=4)
        else:
            print(f"[ERROR] Manifest not found for {plugin_system_name}")

    def get_plugin(self, system_name):
        """Get a specific plugin by its system name (UID)."""
        from app.objects import Plugin  # Adjust as needed.
        plugin = Plugin(system_name)
        return plugin.get_manifest()

    def get_available_permissions(self):
        perms = set()
        for manifest in self.plugins.values():
            perm = manifest.get("permission_required")
            if perm:
                perms.add(perm)
        return list(perms)

    def register_admin_routes(self, app):
        """
        Dynamically register admin routes for all plugins.
        For each plugin, if its manifest includes a "permission_required" key,
        attach a blueprint-level before_request handler that requires the specified permission.
        """
        for plugin_name, manifest in self.plugins.items():
            try:
                module = importlib.import_module(f"app.plugins.{plugin_name}.routes")
                if hasattr(module, "get_blueprint"):
                    blueprint = module.get_blueprint()
                    required_permission = manifest.get("permission_required")
                    # @blueprint.before_request
                    # def check_plugin_access():
                    #     from flask import flash, redirect, url_for
                    #     if not current_user.is_authenticated:
                    #         flash("Please log in to access this module.", "warning")
                    #         return redirect(url_for('routes.login'))
                    #     if current_user.role == 'admin':
                    #         return
                    #     if not required_permission:
                    #         return
                    #     # Assume has_permission() is defined elsewhere.
                    #     if not has_permission(required_permission):
                    #         flash("Access denied: You do not have the required permission to access this module.", "danger")
                    #         return redirect(url_for('routes.dashboard'))
                    app.register_blueprint(blueprint)
                    print(f"[DEBUG] Admin routes registered for plugin: {plugin_name}")
                else:
                    print(f"[DEBUG] Plugin {plugin_name} does not provide get_blueprint().")
            except Exception as e:
                print(f"[ERROR] Error registering plugin {plugin_name}: {e}")

    def register_public_routes(self, app):
        """
        Dynamically register public routes for all plugins.
        For each plugin, if its manifest includes a "permission_required" key,
        attach a blueprint-level before_request handler that requires the specified permission.
        """
        for plugin_name, manifest in self.plugins.items():
            try:
                module = importlib.import_module(f"app.plugins.{plugin_name}.routes")
                if hasattr(module, "get_public_blueprint"):
                    blueprint = module.get_public_blueprint()
                    app.register_blueprint(blueprint)
                    print(f"[DEBUG] Public routes registered for plugin: {plugin_name}")
                else:
                    print(f"[DEBUG] Plugin {plugin_name} does not provide get_public_blueprint().")
            except Exception as e:
                print(f"[ERROR] Error registering plugin {plugin_name}: {e}")