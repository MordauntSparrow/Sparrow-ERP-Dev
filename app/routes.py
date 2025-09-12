"""
app/routes.py

Core routes for Sparrow ERP.

Sections:
  1. Authentication & Password Reset
      - /login
      - /reset-password and /reset-password/<token>
  2. Dashboard & Logout
      - / (dashboard)
      - /logout
  3. User Management (Admin Only)
      - /users           -> Combined management page (live search + modals for add/edit/delete)
      - /users/search    -> AJAX endpoint for live search
      - /users/add       -> Process adding a new user (includes first_name and last_name)
      - /users/edit/<user_id>  -> Process editing a user (updates email, first_name, last_name, role, permissions, and optionally password)
      - /users/delete/<user_id> -> Delete a user
  4. Version Management (Admin Only)
      - /version         -> View/apply updates
  5. Core Module Settings (Admin Only)
      - /core/settings   -> Update site settings (theme, branding, etc.)
  6. SMTP/Email Configuration (Admin Only)
      - /smtp-config     -> Update email configuration
  7. Plugin Management (Admin Only)
      - /plugins         -> Manage plugins (listing, enabling/disabling, etc.)
      - /plugin/<plugin_system_name>/settings
      - /plugin/<plugin_system_name>/enable
      - /plugin/<plugin_system_name>/disable
      - /plugin/<plugin_system_name>/install
      - /plugin/<plugin_system_name>/uninstall
  8. Static File Serving for Plugins
      - /plugins/<plugin_name>/<filename>

All admin-only routes are protected using the helper function admin_only().
The core manifest is passed as "config" to all templates.
"""

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, jsonify, send_from_directory
)
import uuid
import sys, os, json, threading
from datetime import datetime
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from werkzeug.utils import secure_filename
from app.objects import *  # Ensure User, AuthManager, PluginManager, UpdateManager, EmailConfigManager, has_permission, etc. are imported
from flask_login import login_user, logout_user, current_user, login_required
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from app.create_app import update_env_var

# Constants and folder paths
PLUGINS_FOLDER = os.path.join(os.path.dirname(__file__), "plugins")
STATIC_UPLOAD_FOLDER = os.path.join('app', 'static', 'uploads')
ALLOWED_LOGO_EXTENSIONS = {'png', 'jpg', 'jpeg', 'svg'}
ALLOWED_CSS_EXTENSIONS = {'css'}

os.makedirs(STATIC_UPLOAD_FOLDER, exist_ok=True)

# Create blueprint for core routes
routes = Blueprint('routes', __name__)

##############################################################################
# SECTION 1: Authentication & Password Reset
##############################################################################

@routes.route('/login', methods=['GET', 'POST'])
def login():
    """
    Authenticate users via username and password.
    Loads site settings from the core manifest.
    On success, stores user data (including first_name and last_name) in session,
    updates the last_login timestamp, and redirects to the dashboard.
    """
    if current_user.is_authenticated:
        return redirect(url_for('routes.dashboard'))
    
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    core_manifest = plugin_manager.get_core_manifest()
    site_settings = core_manifest.get('site_settings', {
        'company_name': 'Sparrow ERP',
        'branding': 'name',
        'logo_path': ''
    })
    session['site_settings'] = site_settings
    session['core_manifest'] = core_manifest

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user_data = User.get_user_by_username_raw(username)
        if user_data and AuthManager.verify_password(user_data["password_hash"], password):
            # Parse permissions
            permissions = []
            if user_data.get('permissions'):
                try:
                    permissions = json.loads(user_data['permissions'])
                except Exception:
                    permissions = []
            user = User(user_data['id'], user_data['username'], user_data['email'],
                        user_data['role'], permissions)
            login_user(user)
            # Store first and last name in session
            session['first_name'] = user_data.get('first_name', '')
            session['last_name'] = user_data.get('last_name', '')
            session['theme'] = user_data.get('theme', 'default')
            # Update last_login timestamp in the database
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET last_login = %s WHERE id = %s", (datetime.now(), user_data['id']))
            conn.commit()
            cursor.close()
            conn.close()
            # Update site settings with user's name if available
            if user_data.get('first_name') and user_data.get('last_name'):
                site_settings['user_name'] = f"{user_data['first_name']} {user_data['last_name']}"
            flash(f"Welcome back, {user_data.get('first_name', user_data['username'])}!", 'success')

            if user_data['role'] == "crew":
                return redirect('/plugin/ventus_response_module/response')

            return redirect(url_for('routes.dashboard'))
        else:
            flash('Invalid credentials.', 'error')
    return render_template('login.html', site_settings=site_settings, config=core_manifest)

def generate_reset_token(email):
    """
    Generate a secure, time-limited token for password reset.
    """
    secret_key = os.environ.get('SECRET_KEY')
    serializer = URLSafeTimedSerializer(secret_key)
    return serializer.dumps(email, salt='password-reset-salt')

def verify_reset_token(token, expiration=3600):
    """
    Verify the password reset token.
    Returns the email if valid; otherwise, returns None.
    """
    secret_key = os.environ.get('SECRET_KEY')
    serializer = URLSafeTimedSerializer(secret_key)
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=expiration)
    except (SignatureExpired, BadSignature):
        return None
    return email

@routes.route('/reset-password', methods=['GET', 'POST'])
def reset_password_request():
    """
    Password reset request: user submits email to receive a reset link.
    """
    if request.method == 'POST':
        email = request.form['email']
        user_data = User.get_user_by_email(email)
        if user_data:
            token = generate_reset_token(email)
            reset_link = url_for('routes.reset_password', token=token, _external=True)
            flash(f"A password reset link has been sent to {email}. (Link: {reset_link})", 'info')
        else:
            flash("Email not found.", "error")
        return redirect(url_for('routes.login'))
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    core_manifest = plugin_manager.get_core_manifest()
    return render_template('reset_password_request.html', config=core_manifest)

@routes.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """
    Password reset: validate token and allow user to set a new password.
    """
    email = verify_reset_token(token)
    if not email:
        flash("The password reset link is invalid or has expired.", "danger")
        return redirect(url_for('routes.reset_password_request'))
    if request.method == 'POST':
        new_password = request.form['password']
        confirm_password = request.form['confirm_password']
        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template('reset_password.html', token=token, config=core_manifest)
        user_data = User.get_user_by_email(email)
        if user_data:
            new_hash = AuthManager.hash_password(new_password)
            User.update_password(user_data['id'], new_hash)
            flash("Your password has been updated successfully!", "success")
            return redirect(url_for('routes.login'))
        else:
            flash("User not found.", "danger")
            return redirect(url_for('routes.reset_password_request'))
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    core_manifest = plugin_manager.get_core_manifest()
    return render_template('reset_password.html', token=token, config=core_manifest)

##############################################################################
# SECTION 2: Dashboard & Logout
##############################################################################

@routes.route('/')
@login_required
def dashboard():
    """
    Dashboard: displays navigation based on user permissions.
    Admin users see all plugins; non-admins see only those they are allowed to access.
    """
    user_info = {
        'username': current_user.username,
        'first_name': session.get('first_name', 'Guest'),
        'last_name': session.get('last_name', 'User'),
        'theme': session.get('theme', 'default')
    }
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    plugins = plugin_manager.get_enabled_plugins()
    if current_user.role != 'admin':
        plugins = [plugin for plugin in plugins if not plugin.get('permission_required') or has_permission(plugin.get('permission_required'))]
    core_manifest = plugin_manager.get_core_manifest()
    core_version = core_manifest.get('version', '0.0.1') if core_manifest else '0.0.1'
    return render_template('dashboard.html', plugins=plugins, config=core_manifest, user=user_info, core_version=core_version)

@routes.route('/logout')
@login_required
def logout():
    """
    Logout: logs the user out and redirects to the login page.
    """
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('routes.login'))

##############################################################################
# SECTION 3: User Management (Admin Only)
##############################################################################

@routes.route('/users/search', methods=['GET'])
@login_required
def search_users():
    """
    Live search endpoint for user management.
    Returns a JSON list of users matching the query (by username or email).
    Accessible only by admin users.
    """
    if not admin_only():
        return jsonify({"error": "Access denied"}), 403

    query = request.args.get('q', '').strip()
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    if query:
        search_param = f"%{query}%"
        cursor.execute("""
            SELECT id, username, email, role, permissions, first_name, last_name, created_at, last_login 
            FROM users
            WHERE username LIKE %s OR email LIKE %s
        """, (search_param, search_param))
    else:
        cursor.execute("""
            SELECT id, username, email, role, permissions, first_name, last_name, created_at, last_login 
            FROM users
        """)
    users_list = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(users_list)

@routes.route('/users', methods=['GET'])
@login_required
def users():
    """
    Combined User Management Page (Admin Only):
      - Displays a live-search enabled interface with modals for adding and editing users.
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
      SELECT id, username, email, role, permissions, first_name, last_name, created_at, last_login 
      FROM users
    """)
    users_list = cursor.fetchall()
    cursor.close()
    conn.close()
    
    # Instantiate PluginManager to load configuration.
    plugin_manager = PluginManager()
    available_permissions = plugin_manager.get_available_permissions()
    core_manifest = plugin_manager.get_core_manifest()
    
    return render_template('user_management.html',
                           users=users_list,
                           query="",
                           available_permissions=available_permissions,
                           config=core_manifest)

@routes.route('/users/add', methods=['POST'])
@login_required
def add_user():
    """
    Add a new user (Admin Only).
    Processes form submission from the Add User modal.
    Integrates personal PIN field.
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    
    username = request.form.get('username')
    email = request.form.get('email')
    role = request.form.get('role')
    password = request.form.get('password')
    confirm_password = request.form.get('confirm_password')
    first_name = request.form.get('first_name')
    last_name = request.form.get('last_name')
    # Personal PIN field (may be blank)
    personal_pin = request.form.get('personal_pin')
    
    if password != confirm_password:
        flash("Passwords do not match.", "danger")
        return redirect(url_for("routes.users"))
    
    if User.get_user_by_username_raw(username):
        flash("Username already exists.", "danger")
        return redirect(url_for("routes.users"))
    if User.get_user_by_email(email):
        flash("Email already exists.", "danger")
        return redirect(url_for("routes.users"))
    
    new_hash = AuthManager.hash_password(password)
    new_permissions = request.form.getlist('permissions')
    new_permissions_json = json.dumps(new_permissions)
    user_id = str(uuid.uuid4())
    
    # If a personal PIN is provided, hash it; otherwise, leave it null.
    personal_pin_hash = None
    if personal_pin and personal_pin.strip():
        personal_pin_hash = AuthManager.hash_password(personal_pin.strip())
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (id, username, email, password_hash, role, permissions, first_name, last_name, personal_pin_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (user_id, username, email, new_hash, role, new_permissions_json, first_name, last_name, personal_pin_hash))
    conn.commit()
    cursor.close()
    conn.close()
    
    flash("New user added successfully.", "success")
    return redirect(url_for("routes.users"))


@routes.route('/users/edit/<user_id>', methods=['POST'])
@login_required
def edit_user(user_id):
    """
    Process user updates (email, first_name, last_name, role, permissions,
    and optionally password and personal PIN) from the Edit User modal.
    This route applies to all users regardless of role.
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    
    new_email = request.form.get("email")
    new_role = request.form.get("role")
    new_permissions = request.form.getlist("permissions")
    new_first_name = request.form.get("first_name")
    new_last_name = request.form.get("last_name")
    permissions_json = json.dumps(new_permissions)
    
    new_password = request.form.get("new_password")
    confirm_new_password = request.form.get("confirm_new_password")
    # Field for updating personal PIN for all users.
    new_personal_pin = request.form.get("personal_pin")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if new_password:
        if new_password != confirm_new_password:
            flash("New passwords do not match.", "danger")
            return redirect(url_for("routes.users"))
        new_hash = AuthManager.hash_password(new_password)
        if new_personal_pin and new_personal_pin.strip():
            new_personal_pin_hash = AuthManager.hash_password(new_personal_pin.strip())
            cursor.execute("""
                UPDATE users
                SET email = %s, role = %s, permissions = %s, password_hash = %s, first_name = %s, last_name = %s, personal_pin_hash = %s
                WHERE id = %s
            """, (new_email, new_role, permissions_json, new_hash, new_first_name, new_last_name, new_personal_pin_hash, user_id))
        else:
            cursor.execute("""
                UPDATE users
                SET email = %s, role = %s, permissions = %s, password_hash = %s, first_name = %s, last_name = %s
                WHERE id = %s
            """, (new_email, new_role, permissions_json, new_hash, new_first_name, new_last_name, user_id))
    else:
        if new_personal_pin and new_personal_pin.strip():
            new_personal_pin_hash = AuthManager.hash_password(new_personal_pin.strip())
            cursor.execute("""
                UPDATE users
                SET email = %s, role = %s, permissions = %s, first_name = %s, last_name = %s, personal_pin_hash = %s
                WHERE id = %s
            """, (new_email, new_role, permissions_json, new_first_name, new_last_name, new_personal_pin_hash, user_id))
        else:
            cursor.execute("""
                UPDATE users
                SET email = %s, role = %s, permissions = %s, first_name = %s, last_name = %s
                WHERE id = %s
            """, (new_email, new_role, permissions_json, new_first_name, new_last_name, user_id))
    conn.commit()
    cursor.close()
    conn.close()
    flash("User updated successfully.", "success")
    return redirect(url_for("routes.users"))



@routes.route('/users/delete/<user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    """
    Delete a user (Admin Only).
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash("User deleted successfully.", "success")
    return redirect(url_for("routes.users"))

def admin_only():
    """
    Helper function to restrict access to admin users.
    Returns True if current_user is admin; otherwise, flashes an error and returns False.
    """
    if current_user.role != 'admin' and current_user.role != 'superuser':
        flash("Access denied: Admins only.", "danger")
        return False
    return True

##############################################################################
# SECTION 4: Version Management (Admin Only)
##############################################################################

@routes.route('/version', methods=['GET', 'POST'])
@login_required
def version():
    """
    Version management page (Admin Only):
      - Allows scheduling and applying updates.
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    
    update_manager = UpdateManager()
    plugin_manager = PluginManager()
        
    if request.method == 'POST':
        update_type = request.form['update_type']  # 'core' or 'plugin'
        plugin_name = request.form.get('plugin_name')
        scheduled_time = request.form.get('scheduled_time')
        if scheduled_time:
            update_manager.schedule_update(update_type, scheduled_time, plugin_name)
            flash(f"Update scheduled for {update_type} at {scheduled_time}", 'success')
        else:
            try:
                update_manager.apply_update(update_type, plugin_name)
                flash(f"{update_type.capitalize()} updated successfully.", 'success')
            except Exception as e:
                flash(f"Error during {update_type} update: {str(e)}", 'danger')
        return redirect(url_for('routes.version'))
    
    current_version = update_manager.get_current_version()
    latest_version = update_manager.get_latest_version()
    if current_version is None or latest_version is None:
        return jsonify({"error": "Current or latest version not found."}), 500
    core_update_available = current_version < latest_version
    update_status = update_manager.get_update_status()
    core_changelog = update_manager.get_changelog_for_core()
    plugin_changelogs = {plugin['plugin_name']: update_manager.get_changelog_for_plugin(plugin['plugin_name']) for plugin in update_status['plugins']}
    core_manifest = plugin_manager.get_core_manifest()
    return render_template('version_checker.html', config=core_manifest, current_version=current_version, latest_version=latest_version, update_available=core_update_available, plugins=update_status['plugins'], core_changelog=core_changelog, plugin_changelogs=plugin_changelogs)

##############################################################################
# SECTION 5: Core Module Settings (Admin Only)
##############################################################################

@routes.route('/core/settings', methods=['GET', 'POST'])
@login_required
def core_module_settings():
    """
    Core module settings (Admin Only):
      - Allows updating theme settings, site branding, and logo.
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))

    plugin_manager = PluginManager(PLUGINS_FOLDER)
    manifest_path = plugin_manager.get_core_manifest_path()
    default_config = {
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
    
    # Read current config if exists, otherwise use defaults
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r') as f:
            try:
                config_data = json.load(f)
                config_data = {**default_config, **config_data}
            except json.JSONDecodeError:
                config_data = default_config
    else:
        config_data = default_config

    if request.method == 'POST':
        config_data['site_settings']['company_name'] = request.form.get('company_name')
        config_data['site_settings']['branding'] = request.form.get('branding')
        config_data['theme_settings']['theme'] = request.form.get('theme')

        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename:
            # Directly check extension for allowed logos
            if '.' in logo_file.filename and \
               logo_file.filename.rsplit('.', 1)[1].lower() in ALLOWED_LOGO_EXTENSIONS:
                logo_filename = secure_filename(logo_file.filename)
                logo_path = os.path.join(STATIC_UPLOAD_FOLDER, logo_filename)
                logo_file.save(logo_path)
                config_data['site_settings']['logo_path'] = f"uploads/{logo_filename}"
            else:
                flash("Invalid file type for logo. Allowed: png, jpg, jpeg, svg", 'danger')

        css_file = request.files.get('custom_css')
        if css_file and css_file.filename:
            # Directly check extension for allowed CSS
            if '.' in css_file.filename and \
               css_file.filename.rsplit('.', 1)[1].lower() in ALLOWED_CSS_EXTENSIONS:
                css_filename = secure_filename(css_file.filename)
                css_path = os.path.join(STATIC_UPLOAD_FOLDER, css_filename)
                css_file.save(css_path)
                config_data['theme_settings']['custom_css_path'] = f"uploads/{css_filename}"
            else:
                flash("Invalid file type for custom CSS. Allowed: css", 'danger')

        # Write updated config to manifest
        with open(manifest_path, 'w') as f:
            json.dump(config_data, f, indent=4)

        # Refresh session settings
        core_manifest = plugin_manager.get_core_manifest()
        session['site_settings'] = core_manifest.get('site_settings', {
            'company_name': 'Sparrow ERP',
            'branding': 'name',
            'logo_path': ''
        })
        flash("Core module settings updated successfully!", 'success')

    core_manifest = plugin_manager.get_core_manifest()
    return render_template('core_module_settings.html', config=core_manifest)


##############################################################################
# SECTION 6: SMTP/Email Configuration (Admin Only)
##############################################################################

@routes.route('/smtp-config', methods=['GET', 'POST'])
def email_config():
    if request.method == 'POST':
        # Get the new values from the form
        host = request.form.get("host", "").strip()
        port = request.form.get("port", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        use_tls = request.form.get("use_tls", "false").lower() == "true"

        # Write them into .env
        update_env_var("SMTP_HOST", host)
        update_env_var("SMTP_PORT", port)
        update_env_var("SMTP_USERNAME", username)
        update_env_var("SMTP_PASSWORD", password)
        update_env_var("SMTP_USE_TLS", str(use_tls).lower())

        flash("SMTP configuration updated via .env", "success")
        return redirect(url_for('routes.email_config'))
    
    # On GET, read from os.environ (already loaded by .env)
    current_email_config = {
        "host": os.environ.get("SMTP_HOST", ""),
        "port": os.environ.get("SMTP_PORT", ""),
        "username": os.environ.get("SMTP_USERNAME", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "use_tls": os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
    }
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    core_manifest = plugin_manager.get_core_manifest()
    return render_template(
        "email_config.html",
        email_config=current_email_config,
        config=core_manifest
    )

##############################################################################
# SECTION 7: Plugin Management (Admin Only)
##############################################################################

@routes.route('/plugins', methods=['GET'])
@login_required
def plugins():
    """
    Plugin management page (Admin Only):
      - Displays all plugins as cards.
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    plugins_list = plugin_manager.get_all_plugins()
    core_manifest = plugin_manager.get_core_manifest()
    return render_template('plugins.html', plugins=plugins_list, config=core_manifest)

@routes.route('/plugin/<plugin_system_name>/settings', methods=['GET', 'POST'])
@login_required
def plugin_settings(plugin_system_name):
    """
    Manage settings for a specific plugin (Admin Only).
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    manifest = plugin_manager.get_plugin(plugin_system_name)
    if not manifest:
        flash(f'Plugin manifest for {plugin_system_name} not found.', 'error')
        return redirect(url_for('routes.plugins'))
    if request.method == 'POST':
        plugin_manager.update_plugin_settings(plugin_system_name, request.form)
        flash(f'{plugin_system_name} settings updated successfully!', 'success')
    core_manifest = plugin_manager.get_core_manifest()
    return render_template('plugin_settings.html', plugin_name=plugin_system_name, settings=manifest, config=core_manifest)

@routes.route('/plugin/<plugin_system_name>/enable', methods=['POST'])
@login_required
def enable_plugin(plugin_system_name):
    """
    Enable a plugin (Admin Only).
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    success = plugin_manager.enable_plugin(plugin_system_name)
    if success:
        flash(f'{plugin_system_name} has been enabled.', 'success')
    else:
        flash(f'{plugin_system_name} is not installed or manifest is missing.', 'error')
    return redirect(url_for('routes.plugins'))

@routes.route('/plugin/<plugin_system_name>/disable', methods=['POST'])
@login_required
def disable_plugin(plugin_system_name):
    """
    Disable a plugin (Admin Only).
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    success = plugin_manager.disable_plugin(plugin_system_name)
    if success:
        flash(f'{plugin_system_name} has been disabled.', 'success')
    else:
        flash(f'{plugin_system_name} is not installed or manifest is missing.', 'error')
    return redirect(url_for('routes.plugins'))

@routes.route('/plugin/<plugin_system_name>/install', methods=['POST'])
@login_required
def install_plugin(plugin_system_name):
    """
    Install a plugin (Admin Only).
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    success = plugin_manager.install_plugin(plugin_system_name)
    if success:
        flash(f'{plugin_system_name} has been installed successfully!', 'success')
    else:
        flash(f'Failed to install {plugin_system_name}. Please check the plugin files.', 'error')
    return redirect(url_for('routes.plugins'))

@routes.route('/plugin/<plugin_system_name>/uninstall', methods=['POST'])
@login_required
def uninstall_plugin(plugin_system_name):
    """
    Uninstall a plugin (Admin Only).
    """
    if not admin_only():
        return redirect(url_for('routes.dashboard'))
    plugin_manager = PluginManager(PLUGINS_FOLDER)
    success = plugin_manager.uninstall_plugin(plugin_system_name)
    if success:
        flash(f'{plugin_system_name} has been uninstalled.', 'success')
    else:
        flash(f'{plugin_system_name} is not installed or manifest is missing.', 'error')
    return redirect(url_for('routes.plugins'))

##############################################################################
# SECTION 8: Static File Serving for Plugins
##############################################################################

@routes.route('/plugins/<plugin_name>/<filename>')
def serve_plugin_icon(plugin_name, filename):
    """
    Serve static plugin icon files.
    """
    plugin_folder = os.path.join(os.getcwd(), 'app', 'plugins', plugin_name)
    return send_from_directory(plugin_folder, filename)

##############################################################################
# SECTION 9: API For PWA
##############################################################################

api_bp = Blueprint('api', __name__, url_prefix='/api')

@api_bp.route('/login', methods=['POST'])
def api_login():
    if current_user.is_authenticated:
        logout_user()
    
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "Invalid JSON payload."}), 400

    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        return jsonify({"status": "error", "message": "Username and password required."}), 400

    # Replace this with your actual user retrieval and authentication logic
    user_data = User.get_user_by_username_raw(username)
    if user_data and AuthManager.verify_password(user_data["password_hash"], password):
        permissions = []
        if user_data.get('permissions'):
            try:
                permissions = json.loads(user_data['permissions'])
            except Exception:
                permissions = []
        user = User(user_data['id'], user_data['username'], user_data['email'],
                    user_data['role'], permissions)
        login_user(user)
        session['first_name'] = user_data.get('first_name', '')
        session['last_name'] = user_data.get('last_name', '')
        session['theme'] = user_data.get('theme', 'default')
        
        # Update last login timestamp
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET last_login = %s WHERE id = %s", (datetime.now(), user_data['id']))
        conn.commit()
        cursor.close()
        conn.close()
        
        # Set additional session data as needed
        # ...
        
        response_data = {
            "status": "success",
            "message": f"Welcome back, {user_data.get('first_name', user_data['username'])}!",
            "user": {
                "id": user_data['id'],
                "username": user_data['username'],
                "email": user_data['email'],
                "role": user_data['role'],
                "first_name": user_data.get('first_name', ''),
                "last_name": user_data.get('last_name', ''),
                "theme": user_data.get('theme', 'default'),
                "permissions": permissions
            },
            "site_settings": session.get("site_settings", {}),
            "core_manifest": session.get("core_manifest", {})
        }

        return jsonify(response_data)
    
    else:
        return jsonify({"status": "error", "message": "Invalid credentials."}), 401

@api_bp.route('/logout', methods=['POST'])
def api_logout():
    """
    API endpoint for logout. Logs out the user and returns a JSON response.
    Note: @login_required is removed to prevent redirection.
    """
    logout_user()
    return jsonify({"status": "success", "message": "You have been logged out."})

@api_bp.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "success", "message": "Server is reachable"}), 200