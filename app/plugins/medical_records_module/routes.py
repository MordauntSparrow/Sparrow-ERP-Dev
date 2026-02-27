import os
import random
import string
import uuid
import json
from datetime import datetime, timedelta
from flask import (
    Blueprint, request, jsonify, render_template, current_app,
    redirect, url_for, flash, session
)
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.security import check_password_hash
from flask_mail import Message, Mail
from .objects import Patient, AuditLog, Prescription, CareCompanyUser, EmailManager
from app.objects import PluginManager, AuthManager, User, get_db_connection  # Adjust as needed
import logging

logger = logging.getLogger('medical_records_module')
logger.setLevel(logging.INFO)

# In-memory storage for one-time admin PINs.
admin_pin_store = {}  # Example: {"pin": "123456", "expires_at": datetime_object, "generated_by": "ClinicalLeadUser"}

def calculate_age(born, today=None):
    if today is None:
        today = datetime.utcnow().date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

def generate_pin():
    """Generates a secure 6-digit PIN."""
    return ''.join(random.choices(string.digits, k=6))

# For audit logging, use our raw method.
def log_audit(user, action, patient_id=None):
    AuditLog.insert_log(user, action, patient_id)
    logger.info("Audit log created: user=%s, action=%s", user, action)

def send_reset_email(user, token):
    """Sends a password reset email to a care company user."""
    subject = "Password Reset for Care Company Portal"
    sender = current_app.config.get("MAIL_DEFAULT_SENDER")
    recipients = [user.email]
    reset_url = url_for('care_company.reset_password', token=token, _external=True)
    text_body = f"""Dear {user.company_name},

To reset your password, please click the following link:
{reset_url}

If you did not request a password reset, please ignore this email.

Kind regards,
The Support Team
"""
    html_body = f"""<p>Dear {user.company_name},</p>
<p>To reset your password, please click the following link:</p>
<p><a href="{reset_url}">{reset_url}</a></p>
<p>If you did not request a password reset, please ignore this email.</p>
<p>Kind regards,<br>The Support Team</p>"""
    mail = Mail(current_app)
    msg = Message(subject, sender=sender, recipients=recipients)
    msg.body = text_body
    msg.html = html_body
    mail.send(msg)
    logger.info("Password reset email sent to %s", user.email)

# Instantiate PluginManager and load core manifest.
plugin_manager = PluginManager(os.path.abspath('app/plugins'))
core_manifest = plugin_manager.get_core_manifest()

# =============================================================================
# INTERNAL BLUEPRINT (for admin side)
# =============================================================================
internal_template_folder = os.path.join(os.path.dirname(__file__), 'templates')
internal_bp = Blueprint(
    'medical_records_internal',
    __name__,
    url_prefix='/plugin/medical_records_module',
    template_folder=internal_template_folder
)

@internal_bp.route('/')
def landing():
    """Landing page (router) for Medical Records Module."""
    return render_template("router.html", config=core_manifest)

@internal_bp.route('/crew', methods=['GET'])
@login_required
def crew_view():
    """
    Crew view route.
    When accessed without AJAX parameters, renders the full crew page (crew/crew_home.html)
    that includes the search form, results table, and a persistent modal for PIN entry.
    Allowed roles: "crew", "admin", "superuser", "clinical_lead".
    """
    allowed_roles = ["crew", "admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({"error": "Unauthorised access"}), 403
    return render_template("crew/crew_home.html", config=core_manifest)

@internal_bp.route('/crew/verify_pin', methods=['POST'])
@login_required
def verify_pin():
    """
    AJAX endpoint to verify the crew member's personal PIN.
    Expects JSON with the key 'personal_pin'.
    Allowed roles: "crew", "admin", "superuser", "clinical_lead".
    """
    allowed_roles = ["crew", "admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({"error": "Unauthorised access"}), 403
    data = request.get_json()
    if not data or 'personal_pin' not in data:
        return jsonify({"error": "Missing personal PIN"}), 400
    personal_pin = data.get('personal_pin').strip()
    if not personal_pin:
        return jsonify({"error": "Personal PIN cannot be empty"}), 403
    if not current_user.personal_pin_hash or current_user.personal_pin_hash.strip() == "":
        return jsonify({"error": "Personal PIN not set. Please contact your administrator."}), 403
    if not AuthManager.verify_password(current_user.personal_pin_hash, personal_pin):
        logger.warning("User %s provided an invalid personal PIN.", current_user.username)
        return jsonify({"error": "Invalid personal PIN"}), 403
    log_audit(current_user.username, "Crew verified personal PIN")
    return jsonify({"message": "PIN verified"}), 200

@internal_bp.route('/crew/search', methods=['GET'])
@login_required
def crew_search():
    """
    AJAX endpoint for patient search using raw MySQL.
    Expects query parameters: 'date_of_birth' and 'postcode'.
    Allowed roles: "crew", "admin", "superuser", "clinical_lead".
    """
    allowed_roles = ["crew", "admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({"error": "Unauthorised access"}), 403
    dob_str = request.args.get('date_of_birth')
    postcode = request.args.get('postcode')
    if not dob_str or not postcode:
        logger.warning("Crew search missing date_of_birth or postcode.")
        return jsonify({"error": "Missing date_of_birth or postcode"}), 400
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date_of_birth format; please use YYYY-MM-DD"}), 400
    patients = Patient.search_by_dob_and_postcode(dob, postcode)
    if not patients:
        logger.warning("No patient found for DOB: %s and postcode: %s", dob_str, postcode)
        return jsonify({"error": "No matching patient found"}), 404
    AuditLog.insert_log(current_user.username, "Crew performed patient search", patient_id=patients[0].get("id"))
    current_date = datetime.utcnow().date()
    patients_list = []
    for p in patients:
        age = calculate_age(p.get('date_of_birth'), current_date) if p.get('date_of_birth') else "N/A"
        patients_list.append({
            "id": p.get("id"),
            "first_name": p.get("first_name"),
            "middle_name": p.get("middle_name"),
            "last_name": p.get("last_name"),
            "age": age,
            "gender": p.get("gender"),
            "address": p.get("address"),
            "postcode": p.get("postcode"),
            "date_of_birth": p.get("date_of_birth"),
            "package_type": p.get("package_type"),
            "care_company_id": p.get("care_company_id"),
            "access_requirements": p.get("access_requirements"),
            "risk_flags": p.get("notes"),
            "contact_number": p.get("contact_number")
        })
  
    return jsonify({"message": "Patient search successful.", "patients": patients_list}), 200

@internal_bp.route('/crew/view_record/<id>', methods=['GET'])
@login_required
def view_patient_record(id):
    patient = Patient.get_by_id(id)
    if not patient:
        return render_template(
            "crew/crew_home.html",
            error="Patient record not found",
            config=core_manifest
        )

    # For fields that should be dictionaries:
    dict_keys = [
        'gp_details',
        'resuscitation_directive',
        'payment_details',
        'weight'
    ]
    for key in dict_keys:
        value = patient.get(key)
        if isinstance(value, dict):
            continue
        elif isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value.strip())
                patient[key] = parsed if isinstance(parsed, dict) else {}
            except Exception as e:
                print(f"Error parsing {key}: {e}")
                patient[key] = {}
        else:
            patient[key] = {}

    # For fields that should be lists:
    list_keys = [
        'medical_conditions',
        'allergies',
        'medications',
        'previous_visit_records',
        'access_requirements',
        'notes',
        'message_log',
        'next_of_kin_details',
        'lpa_details'
    ]
    for key in list_keys:
        value = patient.get(key)
        if isinstance(value, list):
            continue
        elif isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value.strip())
                patient[key] = parsed if isinstance(parsed, list) else []
            except Exception as e:
                print(f"Error parsing {key}: {e}")
                patient[key] = []
        else:
            patient[key] = []

    age = calculate_age(patient.get("date_of_birth")) if patient.get("date_of_birth") else "N/A"
    log_audit(current_user.username, "Crew viewed patient record", patient_id=id)

    return render_template(
        "crew/view_patient.html",
        patient=patient,
        age=age,
        config=core_manifest
    )

@internal_bp.route('/crew/edit_record/<id>', methods=['GET', 'POST'])
@login_required
def crew_edit_patient_record(id):
    # --- Authorization ---
    allowed_roles = {"admin", "superuser", "clinical_lead", "crew"}
    if not getattr(current_user, 'role', '').lower() in allowed_roles:
        flash("Unauthorised access", "danger")
        return redirect(url_for('medical_records_internal.landing'))

    # --- POST: process updates ---
    if request.method == 'POST':
        patient_existing = Patient.get_by_id(id)
        if not patient_existing:
            flash("Patient record not found.", "danger")
            return redirect(url_for('medical_records_internal.crew_search'))

        # -- Simple scalar fields --
        first_name   = request.form.get('first_name', '').strip() or patient_existing.get('first_name')
        middle_name  = request.form.get('middle_name', '').strip() or patient_existing.get('middle_name')
        last_name    = request.form.get('last_name', '').strip() or patient_existing.get('last_name')
        address      = request.form.get('address', '').strip() or patient_existing.get('address')
        postcode     = request.form.get('postcode', '').strip() or patient_existing.get('postcode')
        gender       = request.form.get('gender', '').strip() or patient_existing.get('gender')
        package_type = request.form.get('package_type', '').strip() or patient_existing.get('package_type', '')
        contact_num  = request.form.get('contact_number', '').strip() or patient_existing.get('contact_number', '')

        dob_str = request.form.get('date_of_birth', '').strip()
        if dob_str:
            try:
                dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
            except ValueError:
                dob = patient_existing.get("date_of_birth")
        else:
            dob = patient_existing.get("date_of_birth")

        # -- JSON list fields --
        def load_list(field, default_list=None):
            raw = request.form.get(field, '').strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    return parsed if isinstance(parsed, list) else (default_list or [])
                except Exception:
                    return default_list or []
            stored = patient_existing.get(field, '[]')
            try:
                parsed = json.loads(stored)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []

        medical_conditions      = load_list('medical_conditions', default_list=[])
        allergies               = load_list('allergies', default_list=[])
        medications             = load_list('medications', default_list=[])
        access_requirements     = load_list('access_requirements', default_list=[])
        previous_visit_records  = load_list('previous_visit_records', default_list=[])
        notes                   = load_list('notes', default_list=[])
        message_log             = load_list('message_log', default_list=[])

        # -- Next‑of‑Kin and LPA arrays from hidden inputs --
        next_of_kin_details = load_list('next_of_kin_details', default_list=[])
        lpa_details         = load_list('lpa_details',          default_list=[])

        # -- Nested JSON / dict fields --
        def load_dict(field):
            raw = patient_existing.get(field, '')
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = json.loads(raw)
                    return parsed if isinstance(parsed, dict) else {}
                except Exception:
                    return {}
            return raw if isinstance(raw, dict) else {}

        gp_existing     = load_dict('gp_details')
        weight_existing = load_dict('weight')

        gp_details = {
            "name":    request.form.get('gp_name', '').strip()    or gp_existing.get("name", ""),
            "address": request.form.get('gp_address', '').strip() or gp_existing.get("address", ""),
            "contact": request.form.get('gp_contact', '').strip() or gp_existing.get("contact", ""),
            "email":   request.form.get('gp_email', '').strip()   or gp_existing.get("email", "")
        }

        weight = {
            "weight":       request.form.get('weight_value', '').strip() or weight_existing.get("weight", ""),
            "date_weighed": request.form.get('date_weighed', '').strip() or weight_existing.get("date_weighed", ""),
            "source":       request.form.get('weight_source', '').strip() or weight_existing.get("source", "")
        }

        # -- Resuscitation directive --
        resus_existing = load_dict('resuscitation_directive')
        docs = []
        for field, label in [
            ('doc_dnar',     "DNAR"),
            ('doc_respect',  "Respect Form"),
            ('doc_advanced', "Advanced Directive"),
            ('doc_living',   "Living Will"),
            ('doc_lpa',      "LPA"),
            ('doc_care',     "Care Plan"),
        ]:
            if request.form.get(field, '').strip():
                docs.append(label)
        if not docs:
            docs = resus_existing.get("documents", [])

        resuscitation_directive = {
            "for_resuscitation": request.form.get('resus_option', '').strip() or resus_existing.get("for_resuscitation", ""),
            "documents":         docs
        }

        documents = request.form.get('documents', '').strip() or patient_existing.get('documents', "")

        # --- Assemble updates ---
        update_fields = {
            "first_name":             first_name,
            "middle_name":            middle_name,
            "last_name":              last_name,
            "address":                address,
            "date_of_birth":          dob,
            "gender":                 gender,
            "postcode":               postcode,
            "package_type":           package_type,
            "contact_number":         contact_num,
            "gp_details":             json.dumps(gp_details),
            "weight":                 json.dumps(weight),
            "medical_conditions":     json.dumps(medical_conditions),
            "allergies":              json.dumps(allergies),
            "medications":            json.dumps(medications),
            "previous_visit_records": json.dumps(previous_visit_records),
            "access_requirements":    json.dumps(access_requirements),
            "notes":                  json.dumps(notes),
            "message_log":            json.dumps(message_log),
            "next_of_kin_details":    json.dumps(next_of_kin_details),
            "lpa_details":            json.dumps(lpa_details),
            "resuscitation_directive":json.dumps(resuscitation_directive),
            "payment_details":        json.dumps(load_dict('payment_details')),
            "documents":              documents,
        }

        try:
            Patient.update_patient(id, **update_fields)
            log_audit(current_user.username, f"Edited patient record: {id}", patient_id=id)
            flash("Patient record updated successfully.", "success")
        except Exception as e:
            logger.error("Error updating patient record: %s", e)
            flash("Error updating patient record", "danger")
            return redirect(url_for('medical_records_internal.view_patient_record', id=id))

        return redirect(url_for('medical_records_internal.view_patient_record', id=id))

    # --- GET: render the edit form ---
    patient = Patient.get_by_id(id)
    if not patient:
        flash("Patient record not found.", "danger")
        return redirect(url_for('medical_records_internal.admin_patients'))

    log_audit(current_user.username, f"Accessed edit view for patient record: {id}", patient_id=id)

    # --- Parse simple JSON‑dict fields (single objects only) ---
    dict_keys = ['gp_details', 'resuscitation_directive', 'payment_details', 'weight']
    for key in dict_keys:
        raw = patient.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw.strip())
                patient[key] = parsed if isinstance(parsed, dict) else {}
            except Exception:
                patient[key] = {}
        else:
            patient[key] = raw if isinstance(raw, dict) else {}

    # --- Parse JSON‑list fields (arrays of dicts) ---
    list_keys = [
        'medical_conditions', 'allergies', 'medications',
        'previous_visit_records', 'access_requirements',
        'next_of_kin_details', 'lpa_details',
    ]
    for key in list_keys:
        raw = patient.get(key)
        if isinstance(raw, list):
            continue
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw.strip())
                if isinstance(parsed, list):
                    patient[key] = parsed
                elif isinstance(parsed, dict):
                    patient[key] = [parsed]
                else:
                    patient[key] = []
            except Exception:
                patient[key] = []
        else:
            patient[key] = []

    age = calculate_age(patient.get("date_of_birth")) if patient.get("date_of_birth") else "N/A"
    return render_template(
        "crew/edit_patient.html",
        patient=patient,
        age=age,
        config=core_manifest
    )



@internal_bp.route('/crew/view_record/<id>/add_message_log_entry', methods=['POST'])
@login_required
def crew_add_message_log_entry(id):
    category = request.form.get('category')
    custom_category = request.form.get('custom_category')
    message_text = request.form.get('message')
    if category == "Other" and custom_category:
        category = custom_category
    if not category or not message_text:
        return jsonify({'error': 'Category and message are required.'}), 400
    new_message = {
        'id': str(uuid.uuid4()),
        'author': current_user.username,
        'timestamp': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        'category': category,
        'text': message_text
    }
    patient = Patient.get_by_id(id)
    if not patient:
        return jsonify({'error': 'Patient record not found.'}), 404
    message_log = patient.get("message_log")
    if not message_log or message_log == "":
        message_log = []
    elif not isinstance(message_log, list):
        try:
            message_log = json.loads(message_log)
            if not isinstance(message_log, list):
                message_log = []
        except Exception:
            message_log = []
    message_log.append(new_message)
    try:
        Patient.update_patient(id, message_log=json.dumps(message_log))
        log_audit(current_user.username, "Crew added message log entry", patient_id=id)
    except Exception as e:
        logger.error("Error updating patient (crew message log): %s", str(e))
        return jsonify({'error': 'Failed to save message log entry.'}), 500
    return jsonify({'success': True}), 200

@internal_bp.route('/crew/view_record/<id>/delete_message_log_entry', methods=['POST'])
@login_required
def crew_delete_message_log_entry(id):
    data = request.get_json()
    message_id = data.get('message_id')
    if not message_id:
        return jsonify({'error': 'Message ID is required.'}), 400
    patient = Patient.get_by_id(id)
    if not patient:
        return jsonify({'error': 'Patient record not found.'}), 404
    message_log = patient.get("message_log")
    if not message_log or message_log == "":
        message_log = []
    elif not isinstance(message_log, list):
        try:
            message_log = json.loads(message_log)
            if not isinstance(message_log, list):
                message_log = []
        except Exception:
            message_log = []
    new_log = [msg for msg in message_log if str(msg.get('id')) != str(message_id)]
    if len(new_log) == len(message_log):
        return jsonify({'error': 'Message not found.'}), 404
    try:
        Patient.update_patient(id, message_log=json.dumps(new_log))
        log_audit(current_user.username, "Crew deleted message log entry", patient_id=id)
    except Exception as e:
        logger.error("Error updating patient (delete message): %s", str(e))
        return jsonify({'error': 'Failed to delete message log entry.'}), 500
    return jsonify({'success': True}), 200

@internal_bp.route('/crew/view_record/<id>/add_risk_flag', methods=['POST'])
@login_required
def crew_add_risk_flag(id):
    flag_type = request.form.get('flag_type')
    custom_flag_type = request.form.get('custom_flag_type')
    description = request.form.get('description')
    if flag_type == "Other" and custom_flag_type:
        flag_type = custom_flag_type
    if not flag_type or not description:
        return jsonify({'error': 'Risk category and description are required.'}), 400
    new_flag = {
        'id': str(uuid.uuid4()),
        'flag_type': flag_type,
        'timestamp': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        'description': description
    }
    patient = Patient.get_by_id(id)
    if not patient:
        return jsonify({'error': 'Patient record not found.'}), 404
    risk_flags = patient.get("notes")
    if not risk_flags or risk_flags == "":
        risk_flags = []
    elif not isinstance(risk_flags, list):
        try:
            risk_flags = json.loads(risk_flags)
            if not isinstance(risk_flags, list):
                risk_flags = []
        except Exception:
            risk_flags = []
    risk_flags.append(new_flag)
    try:
        Patient.update_patient(id, notes=json.dumps(risk_flags))
        log_audit(current_user.username, "Crew added risk flag", patient_id=id)
    except Exception as e:
        logger.error("Error updating patient (crew risk flags): %s", str(e))
        return jsonify({'error': 'Failed to save risk flag.'}), 500
    return jsonify({'success': True}), 200

@internal_bp.route('/crew/view_record/<id>/delete_risk_flag', methods=['POST'])
@login_required
def crew_delete_risk_flag(id):
    data = request.get_json()
    flag_id = data.get('flag_id')
    if not flag_id:
        return jsonify({'error': 'Flag ID is required.'}), 400
    patient = Patient.get_by_id(id)
    if not patient:
        return jsonify({'error': 'Patient record not found.'}), 404
    risk_flags = patient.get("notes")
    if not risk_flags or risk_flags == "":
        risk_flags = []
    elif not isinstance(risk_flags, list):
        try:
            risk_flags = json.loads(risk_flags)
            if not isinstance(risk_flags, list):
                risk_flags = []
        except Exception:
            risk_flags = []
    new_flags = [flag for flag in risk_flags if str(flag.get('id')) != str(flag_id)]
    if len(new_flags) == len(risk_flags):
        return jsonify({'error': 'Risk flag not found.'}), 404
    try:
        Patient.update_patient(id, notes=json.dumps(new_flags))
        log_audit(current_user.username, "Crew deleted risk flag", patient_id=id)
    except Exception as e:
        logger.error("Error updating patient (delete risk flag): %s", str(e))
        return jsonify({'error': 'Failed to delete risk flag.'}), 500
    return jsonify({'success': True}), 200

# -----------------------
# ADMIN ROUTES
# -----------------------
@internal_bp.route('/admin', methods=['GET'])
@login_required
def admin_patients():
    """
    Admin view route that displays a table of all patient records with a live search bar.
    Allowed roles: "admin", "superuser", "clinical_lead".
    """
    allowed_roles = ["admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        flash("Unauthorised access", "danger")
        return redirect(url_for('medical_records_internal.landing'))
    search = request.args.get('q')
    patients = Patient.get_all(search)
    current_date = datetime.utcnow().date()
    for p in patients:
        if p.get("date_of_birth"):
            p["age"] = calculate_age(p["date_of_birth"], current_date)
        else:
            p["age"] = "N/A"
    log_audit(current_user.username, "Admin accessed patient list")
    return render_template("admin/patients.html", patients=patients, config=core_manifest)

@internal_bp.route('/admin/search', methods=['GET'])
@login_required
def admin_search():
    """
    AJAX endpoint that returns JSON for patient records matching a search query.
    Allowed roles: "admin", "superuser", "clinical_lead".
    """
    allowed_roles = ["admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({"error": "Unauthorised access"}), 403
    search = request.args.get('q')
    patients = Patient.get_all(search)
    current_date = datetime.utcnow().date()
    for p in patients:
        if p.get("date_of_birth"):
            p["age"] = calculate_age(p["date_of_birth"], current_date)
        else:
            p["age"] = "N/A"
    return jsonify({"patients": patients})

@internal_bp.route('/admin/search_care_company', methods=['GET'])
@login_required
def search_care_company():
    """
    AJAX endpoint that returns JSON for care company records matching a search query.
    Allowed roles: "admin", "superuser", "clinical_lead".
    """
    allowed_roles = ["admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({"error": "Unauthorised access"}), 403

    search = request.args.get('q', '').strip()
    companies = CareCompanyUser.get_all(search)  # calls the static method we just created

    # Transform results to match the expected front-end structure
    # e.g. returning "name" instead of "company_name"
    result_data = []
    for c in companies:
        result_data.append({
            "id": c["id"],
            "name": c["company_name"]  # front-end expects "name"
        })

    return jsonify({"companies": result_data})

@internal_bp.route('/admin/view_record/<id>', methods=['GET'])
@login_required
def admin_view_record(id):
    # --- Authorization ---
    allowed_roles = {"admin", "superuser", "clinical_lead"}
    if getattr(current_user, 'role', '').lower() not in allowed_roles:
        return render_template("admin/view_patient.html",
                               error="Unauthorised access",
                               config=core_manifest)

    patient = Patient.get_by_id(id)
    if not patient:
        return render_template("admin/view_patient.html",
                               error="Patient record not found",
                               config=core_manifest)

    unlocked = session.get(f'unlocked_{id}', False)

    # --- parse single‐object JSON → dicts ---
    dict_keys = [
        'gp_details',
        'payment_details',
        'resuscitation_directive',
        'weight',
    ]
    for key in dict_keys:
        raw = patient.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw.strip())
                patient[key] = parsed if isinstance(parsed, dict) else {}
            except:
                patient[key] = {}
        else:
            patient[key] = raw if isinstance(raw, dict) else {}

    # --- parse JSON arrays → lists of dicts ---
    list_keys = [
        'medical_conditions',
        'allergies',
        'medications',
        'previous_visit_records',
        'access_requirements',
        'next_of_kin_details',
        'lpa_details',
        'notes',
        'message_log'
    ]
    for key in list_keys:
        raw = patient.get(key)
        if isinstance(raw, list):
            continue
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw.strip())
                if isinstance(parsed, list):
                    patient[key] = parsed
                elif isinstance(parsed, dict):
                    patient[key] = [parsed]
                else:
                    patient[key] = []
            except:
                patient[key] = []
        else:
            patient[key] = []

    # --- mask if still locked ---
    if not unlocked:
        for field in [
            'gp_details',
            'payment_details',
            'resuscitation_directive',
            'weight',
            'medical_conditions',
            'allergies',
            'medications',
            'previous_visit_records',
            'access_requirements',
            'next_of_kin_details',
            'lpa_details'
        ]:
            patient[field] = "*******"

    age = calculate_age(patient.get("date_of_birth")) if patient.get("date_of_birth") else "N/A"
    log_audit(current_user.username, "Admin viewed patient record", patient_id=id)

    return render_template(
        "admin/view_patient.html",
        patient=patient,
        age=age,
        unlocked=unlocked,
        config=core_manifest
    )

@internal_bp.route('/admin/view_record/<id>/add_risk_flag', methods=['POST'])
@login_required
def add_risk_flag(id):
    """
    AJAX endpoint to add a risk flag (note) to the patient's record.
    Expects 'flag_type', optionally 'custom_flag_type', and 'description' in the POST form data.
    """
    flag_type = request.form.get('flag_type')
    custom_flag_type = request.form.get('custom_flag_type')
    description = request.form.get('description')
    if flag_type == "Other" and custom_flag_type:
        flag_type = custom_flag_type
    if not flag_type or not description:
        return jsonify({'error': 'Risk category and description are required.'}), 400
    new_flag = {
        'id': str(uuid.uuid4()),
        'flag_type': flag_type,
        'timestamp': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        'description': description
    }
    patient = Patient.get_by_id(id)
    if not patient:
        return jsonify({'error': 'Patient record not found.'}), 404
    risk_flags = patient.get("notes")
    if not risk_flags or risk_flags == "":
        risk_flags = []
    elif not isinstance(risk_flags, list):
        try:
            risk_flags = json.loads(risk_flags)
            if not isinstance(risk_flags, list):
                risk_flags = []
        except Exception:
            risk_flags = []
    risk_flags.append(new_flag)
    try:
        Patient.update_patient(id, notes=json.dumps(risk_flags))
        log_audit(current_user.username, "Admin added risk flag", patient_id=id)
    except Exception as e:
        logger.error("Error updating patient (admin risk flag): %s", str(e))
        return jsonify({'error': 'Failed to save risk flag.'}), 500
    return jsonify({'success': True}), 200

@internal_bp.route('/admin/view_record/<id>/delete_risk_flag', methods=['POST'])
@login_required
def delete_risk_flag(id):
    """
    AJAX endpoint to delete a risk flag (note) from the patient's record.
    Expects a JSON payload with key 'flag_id'.
    """
    data = request.get_json()
    flag_id = data.get('flag_id')
    if not flag_id:
        return jsonify({'error': 'Flag ID is required.'}), 400
    patient = Patient.get_by_id(id)
    if not patient:
        return jsonify({'error': 'Patient record not found.'}), 404
    risk_flags = patient.get("notes")
    if not risk_flags or risk_flags == "":
        risk_flags = []
    elif not isinstance(risk_flags, list):
        try:
            risk_flags = json.loads(risk_flags)
            if not isinstance(risk_flags, list):
                risk_flags = []
        except Exception:
            risk_flags = []
    new_flags = [flag for flag in risk_flags if str(flag.get('id')) != str(flag_id)]
    if len(new_flags) == len(risk_flags):
        return jsonify({'error': 'Risk flag not found.'}), 404
    try:
        Patient.update_patient(id, notes=json.dumps(new_flags))
        log_audit(current_user.username, "Admin deleted risk flag", patient_id=id)
    except Exception as e:
        logger.error("Error updating patient (delete risk flag): %s", str(e))
        return jsonify({'error': 'Failed to delete risk flag.'}), 500
    return jsonify({'success': True}), 200

@internal_bp.route('/admin/view_record/<id>/add_message_log_entry', methods=['POST'])
@login_required
def add_message_log_entry(id):
    """
    AJAX endpoint to add a message log entry to the patient's record.
    Expects 'category' (optionally 'custom_category') and 'message' in the POST form data.
    """
    category = request.form.get('category')
    custom_category = request.form.get('custom_category')
    message_text = request.form.get('message')
    if category == "Other" and custom_category:
        category = custom_category
    if not category or not message_text:
        return jsonify({'error': 'Category and message are required.'}), 400
    new_message = {
        'id': str(uuid.uuid4()),
        'author': current_user.username,
        'timestamp': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        'category': category,
        'text': message_text
    }
    patient = Patient.get_by_id(id)
    if not patient:
        return jsonify({'error': 'Patient record not found.'}), 404
    message_log = patient.get("message_log")
    if not message_log or message_log == "":
        message_log = []
    elif not isinstance(message_log, list):
        try:
            message_log = json.loads(message_log)
            if not isinstance(message_log, list):
                message_log = []
        except Exception:
            message_log = []
    message_log.append(new_message)
    try:
        Patient.update_patient(id, message_log=json.dumps(message_log))
        log_audit(current_user.username, "Admin added message log entry", patient_id=id)
    except Exception as e:
        logger.error("Error updating patient (admin message log): %s", str(e))
        return jsonify({'error': 'Failed to save message log entry.'}), 500
    return jsonify({'success': True}), 200

@internal_bp.route('/admin/view_record/<id>/delete_message_log_entry', methods=['POST'])
@login_required
def admin_delete_message_log_entry(id):
    """
    AJAX endpoint to delete a message log entry from the patient's record.
    Expects a JSON payload with key 'message_id'.
    """
    data = request.get_json()
    message_id = data.get('message_id')
    if not message_id:
        return jsonify({'error': 'Message ID is required.'}), 400
    patient = Patient.get_by_id(id)
    if not patient:
        return jsonify({'error': 'Patient record not found.'}), 404
    message_log = patient.get("message_log")
    if not message_log or message_log == "":
        message_log = []
    elif not isinstance(message_log, list):
        try:
            message_log = json.loads(message_log)
            if not isinstance(message_log, list):
                message_log = []
        except Exception:
            message_log = []
    new_log = [msg for msg in message_log if str(msg.get('id')) != str(message_id)]
    if len(new_log) == len(message_log):
        return jsonify({'error': 'Message not found.'}), 404
    try:
        Patient.update_patient(id, message_log=json.dumps(new_log))
        log_audit(current_user.username, "Admin deleted message log entry", patient_id=id)
    except Exception as e:
        logger.error("Error updating patient (delete message): %s", str(e))
        return jsonify({'error': 'Failed to delete message log entry.'}), 500
    return jsonify({'success': True}), 200

@internal_bp.route('/admin/unlock_record', methods=['POST'])
@login_required
def unlock_record():
    """
    Unlocks a patient record.
    Expects form fields:
      - 'id'             : patient ID
      - 'pin'            : one‑time PIN
      - 'justification'  : access reason
    Allowed roles: admin, superuser, clinical_lead.
    Returns JSON with error or success + redirect URL.
    """
    allowed_roles = {"admin", "superuser", "clinical_lead"}
    patient_id = request.form.get('id') or request.form.get('patient_id')
    pin           = (request.form.get('pin') or "").strip()
    justification = (request.form.get('justification') or "").strip()

    # 1) authorization
    if current_user.role.lower() not in allowed_roles:
        return jsonify(error="Unauthorised access"), 403

    # 2) parameters
    if not patient_id or not pin or not justification:
        return jsonify(error="Missing parameters"), 400

    # 3) verify PIN
    stored     = admin_pin_store.get("pin")
    expires_at = admin_pin_store.get("expires_at")
    if not stored or datetime.utcnow() > expires_at or pin != stored:
        return jsonify(error="Invalid or expired PIN"), 403

    # 4) consume PIN
    admin_pin_store.clear()

    # 5) audit log the unlock with justification
    log_audit(
        current_user.username,
        f"Unlocked record {patient_id} with PIN {pin} — reason: {justification}",
        patient_id=patient_id
    )

    # 6) mark session and respond
    session[f'unlocked_{patient_id}'] = True
    return jsonify({
        "message":      "Record unlocked successfully",
        "redirect_url": url_for('medical_records_internal.admin_view_record', id=patient_id)
    }), 200

@internal_bp.route('/admin/delete_record/<id>', methods=['POST'])
@login_required
def delete_patient_record(id):
    """
    Deletes a patient record.
    Allowed roles: "admin", "superuser", "clinical_lead".
    """
    allowed_roles = ["admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        flash("Unauthorised access", "danger")
        return redirect(url_for('medical_records_internal.admin_patients'))
    try:
        Patient.delete_patient(id)
        log_audit(current_user.user, "Admin deleted patient record", patient_id=id)
    except Exception as e:
        logger.error("Error deleting patient record: %s", str(e))
        flash("Error deleting patient record", "danger")
        return redirect(url_for('medical_records_internal.admin_patients'))
    flash("Patient record deleted.", "success")
    return redirect(url_for('medical_records_internal.admin_patients'))

@internal_bp.route('/admin/add_record', methods=['GET', 'POST'])
@login_required
def add_patient_record():
    """
    Allows admin, superuser, or clinical_lead to add a new patient record.
    GET: Renders the add patient form.
    POST: Processes the form and inserts a new patient record.
    """
    allowed_roles = ["admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        flash("Unauthorised access", "danger")
        return redirect(url_for('medical_records_internal.admin_patients'))
    
    if request.method == 'POST':
        nhs_number = request.form.get('nhs_number')
        first_name = request.form.get('first_name')
        middle_name = request.form.get('middle_name')
        care_company_id = request.form.get("care_company_id")
        last_name = request.form.get('last_name')
        contact_number = request.form.get("contact_number")
        address = request.form.get('address')
        gp_details = request.form.get('gp_details')
        medical_conditions = request.form.get('medical_conditions')
        allergies = request.form.get('allergies')
        medications = request.form.get('medications')
        previous_visit_records = request.form.get('previous_visit_records')
        package_type = request.form.get('package_type')
        notes = request.form.get('notes')
        message_log = request.form.get('message_log')
        access_requirements = request.form.get('access_requirements')
        payment_details = request.form.get('payment_details')
        next_of_kin_details = request.form.get('next_of_kin_details')
        lpa_details = request.form.get('lpa_details')
        resuscitation_directive = request.form.get('resuscitation_directive')
        documents = request.form.get('documents')
        dob_str = request.form.get('date_of_birth')
        weight = request.form.get('weight')
        gender = request.form.get('gender')
        postcode = request.form.get('postcode')
        try:
            dob = datetime.strptime(dob_str, "%Y-%m-%d").date() if dob_str else None
        except ValueError:
            dob = None
        try:
            Patient.add_patient(
                nhs_number, first_name, middle_name, last_name, address,
                gp_details, medical_conditions, allergies, medications, previous_visit_records,
                package_type, notes, message_log, access_requirements, payment_details,
                next_of_kin_details, lpa_details, resuscitation_directive, documents,
                dob, weight, gender, postcode, care_company_id, contact_number
            )
        except Exception as e:
            logger.error("Error adding new patient record: %s", str(e))
            flash("Error adding new patient record", "danger")
            return redirect(url_for('medical_records_internal.admin_patients'))
        flash("New patient record added successfully.", "success")
        return redirect(url_for('medical_records_internal.admin_patients'))
    return render_template("admin/add_patient.html", config=core_manifest)


@internal_bp.route('/admin/edit_record/<id>', methods=['GET', 'POST'])
@login_required
def edit_patient_record(id):
    # --- Authorization ---
    allowed_roles = {"admin", "superuser", "clinical_lead"}
    if not getattr(current_user, 'role', '').lower() in allowed_roles:
        flash("Unauthorised access", "danger")
        return redirect(url_for('medical_records_internal.admin_patients'))

    # --- POST: process updates ---
    if request.method == 'POST':
        patient_existing = Patient.get_by_id(id)
        if not patient_existing:
            flash("Patient record not found.", "danger")
            return redirect(url_for('medical_records_internal.admin_patients'))

        # --- Scalar fields ---
        first_name  = request.form.get('first_name','').strip()  or patient_existing.get('first_name')
        middle_name = request.form.get('middle_name','').strip() or patient_existing.get('middle_name')
        last_name   = request.form.get('last_name','').strip()   or patient_existing.get('last_name')
        address     = request.form.get('address','').strip()     or patient_existing.get('address')
        dob_str     = request.form.get('date_of_birth','').strip()
        try:
            dob = datetime.strptime(dob_str, "%Y-%m-%d").date() if dob_str else patient_existing.get('date_of_birth')
        except ValueError:
            dob = patient_existing.get('date_of_birth')
        gender       = request.form.get('gender','').strip() or patient_existing.get('gender')
        postcode     = request.form.get('postcode','').strip() or patient_existing.get('postcode')
        package_type = request.form.get('package_type','').strip() or patient_existing.get('package_type','')
        contact_num  = request.form.get('contact_number','').strip() or patient_existing.get('contact_number','')

        # --- Helper to load JSON arrays ---
        def load_list(field):
            raw = request.form.get(field,'').strip()
            if raw:
                try:
                    arr = json.loads(raw)
                    return arr if isinstance(arr, list) else []
                except:
                    return []
            stored = patient_existing.get(field,'[]')
            try:
                arr = json.loads(stored)
                return arr if isinstance(arr, list) else []
            except:
                return []

        medical_conditions     = load_list('medical_conditions')
        allergies              = load_list('allergies')
        medications            = load_list('medications')
        access_requirements    = load_list('access_requirements')
        previous_visit_records = load_list('previous_visit_records')
        notes                  = load_list('notes')
        message_log            = load_list('message_log')
        next_of_kin_details    = load_list('next_of_kin_details')
        lpa_details            = load_list('lpa_details')

        # --- Helper to load JSON dicts ---
        def load_dict(field):
            raw = patient_existing.get(field,'')
            if isinstance(raw, str) and raw.strip():
                try:
                    obj = json.loads(raw)
                    return obj if isinstance(obj, dict) else {}
                except:
                    return {}
            return raw if isinstance(raw, dict) else {}

        unlocked = session.get(f'unlocked_{id}', False)

        if unlocked:
            # GP Details
            existing_gp = load_dict('gp_details')
            gp_details = {
                "name":    request.form.get('gp_name','').strip()    or existing_gp.get("name",""),
                "address": request.form.get('gp_address','').strip() or existing_gp.get("address",""),
                "contact": request.form.get('gp_contact','').strip() or existing_gp.get("contact",""),
                "email":   request.form.get('gp_email','').strip()   or existing_gp.get("email","")
            }
            # Weight
            existing_weight = load_dict('weight')
            weight = {
                "weight":       request.form.get('weight_value','').strip() or existing_weight.get("weight",""),
                "date_weighed": request.form.get('date_weighed','').strip() or existing_weight.get("date_weighed",""),
                "source":       request.form.get('weight_source','').strip() or existing_weight.get("source","")
            }
            # Payment
            existing_payment = load_dict('payment_details')
            payment_details = {
                "payment_method": request.form.get('payment_method','').strip() or existing_payment.get("payment_method",""),
                "billing_email":  request.form.get('billing_email','').strip()  or existing_payment.get("billing_email","")
            }
            # Resuscitation
            existing_resus = load_dict('resuscitation_directive')
            docs = []
            for fld,label in [
                ('doc_dnar','DNAR'),
                ('doc_respect','Respect Form'),
                ('doc_advanced','Advanced Directive'),
                ('doc_living','Living Will'),
                ('doc_lpa','LPA'),
                ('doc_care','Care Plan'),
            ]:
                if request.form.get(fld,'').strip():
                    docs.append(label)
            resuscitation_directive = {
                "for_resuscitation": request.form.get('resus_option','').strip() or existing_resus.get("for_resuscitation",""),
                "documents":         docs if docs else existing_resus.get("documents",[])
            }
        else:
            # preserve existing
            gp_details              = load_dict('gp_details')
            weight                  = load_dict('weight')
            payment_details         = load_dict('payment_details')
            resuscitation_directive = load_dict('resuscitation_directive')

        documents = request.form.get('documents','').strip() or patient_existing.get('documents',"")

        # --- Assemble updates ---
        update_fields = {
            "first_name":              first_name,
            "middle_name":             middle_name,
            "last_name":               last_name,
            "address":                 address,
            "date_of_birth":           dob,
            "gender":                  gender,
            "postcode":                postcode,
            "package_type":            package_type,
            "contact_number":          contact_num,
            "gp_details":              json.dumps(gp_details),
            "weight":                  json.dumps(weight),
            "payment_details":         json.dumps(payment_details),
            "resuscitation_directive": json.dumps(resuscitation_directive),
            "medical_conditions":      json.dumps(medical_conditions),
            "allergies":               json.dumps(allergies),
            "medications":             json.dumps(medications),
            "previous_visit_records":  json.dumps(previous_visit_records),
            "access_requirements":     json.dumps(access_requirements),
            "notes":                   json.dumps(notes),
            "message_log":             json.dumps(message_log),
            "next_of_kin_details":     json.dumps(next_of_kin_details),
            "lpa_details":             json.dumps(lpa_details),
            "documents":               documents,
        }

        try:
            Patient.update_patient(id, **update_fields)
            log_audit(current_user.username, f"Edited patient record: {id}", patient_id=id)
            flash("Patient record updated successfully.", "success")
        except Exception as e:
            logger.error("Error updating patient record: %s", e)
            flash("Error updating patient record", "danger")
            return redirect(url_for('medical_records_internal.admin_view_record', id=id))

        return redirect(url_for('medical_records_internal.admin_view_record', id=id))

    # --- GET: render the edit form ---
    patient = Patient.get_by_id(id)
    if not patient:
        flash("Patient record not found.", "danger")
        return redirect(url_for('medical_records_internal.admin_patients'))

    log_audit(current_user.username, f"Accessed edit view for patient record: {id}", patient_id=id)
    unlocked = session.get(f'unlocked_{id}', False)

    # parse dicts
    dict_keys = ['gp_details','payment_details','resuscitation_directive','weight']
    for key in dict_keys:
        raw = patient.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw.strip())
                patient[key] = parsed if isinstance(parsed, dict) else {}
            except:
                patient[key] = {}
        else:
            patient[key] = raw if isinstance(raw, dict) else {}

    # parse lists
    list_keys = [
        'medical_conditions','allergies','medications',
        'previous_visit_records','access_requirements',
        'next_of_kin_details','lpa_details'
    ]
    for key in list_keys:
        raw = patient.get(key)
        if isinstance(raw, list):
            continue
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw.strip())
                patient[key] = parsed if isinstance(parsed, list) else []
            except:
                patient[key] = []
        else:
            patient[key] = []

    # mask if locked
    if not unlocked:
        for field in [
            'gp_details','payment_details','resuscitation_directive',
            'weight','next_of_kin_details','lpa_details'
        ]:
            patient[field] = "*******"

    age = calculate_age(patient.get("date_of_birth")) if patient.get("date_of_birth") else "N/A"
    return render_template(
        "admin/edit_patient.html",
        patient=patient,
        unlocked=unlocked,
        age=age,
        config=core_manifest
    )


# =============================================================================
# CLINICAL ROUTES
# =============================================================================
@internal_bp.route('/clinical', methods=['GET', 'POST'])
@login_required
def clinical_view():
    """
    Clinical lead view.
    Allowed roles: "clinical_lead", "superuser".
    - POST (AJAX): Generates a one-time admin PIN and returns JSON.
    - GET: Renders the clinical dashboard.
    """
    allowed_roles = ["clinical_lead", "superuser"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        flash("Unauthorised access", "danger")
        return redirect(url_for('medical_records_internal.landing'))
    if request.method == 'POST':
        new_pin = generate_pin()
        expires_at = datetime.utcnow() + timedelta(minutes=10)
        admin_pin_store["pin"] = new_pin
        admin_pin_store["expires_at"] = expires_at
        admin_pin_store["generated_by"] = current_user.username
        AuditLog.insert_log(current_user.username, "Generated new admin PIN")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"pin": new_pin, "expires_at": expires_at.isoformat()})
        return render_template("clinical/clinical_home.html", modal_pin=new_pin, pin_expires=expires_at.isoformat(), config=core_manifest)
    return render_template("clinical/clinical_home.html", config=core_manifest)

emailer     = EmailManager()

@internal_bp.route('/admin/request_unlock_pin', methods=['POST'])
@login_required
def request_unlock_pin():
    allowed = {'admin', 'superuser'}
    if current_user.role.lower() not in allowed:
        return jsonify(error="Unauthorised access"), 403

    data          = request.get_json() or {}
    patient_id    = data.get('id')
    justification = (data.get('justification') or "").strip()
    if not patient_id or not justification:
        return jsonify(error="Missing parameters"), 400

    # 1) generate & store PIN
    new_pin     = generate_pin()
    expires_at  = datetime.utcnow() + timedelta(minutes=10)
    admin_pin_store.clear()
    admin_pin_store['pin']          = new_pin
    admin_pin_store['expires_at']   = expires_at
    admin_pin_store['generated_by'] = (
        f"System: Requested for Approval By: {current_user.username}"
    )

    # 2) audit log
    log_audit(
        current_user.username,
        f"Requested unlock PIN for record {patient_id} — reason: {justification}",
        patient_id=patient_id
    )

    # 3) fetch recipients via get_db_connection()
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()
        query = """
            SELECT email, first_name
            FROM users
            WHERE LOWER(role) IN (LOWER(%s), LOWER(%s))
        """
        cursor.execute(query, ("clinical_lead", "superuser"))
        recipients = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        current_app.logger.error("Database error fetching PIN recipients: %s", e)
        return jsonify(error="Internal server error"), 500

    if not recipients:
        current_app.logger.error(
            "No clinical_lead or superuser accounts found when requesting PIN for %s",
            patient_id
        )
        return jsonify(error="No recipients configured"), 500

    # 4) send the email
    subject = f"PIN Request for patient #{patient_id}"
    body = (
        f"{current_user.username} has requested a one‑time PIN to unlock patient record {patient_id}.\n\n"
        f"Justification:\n{justification}\n\n"
        f"PIN (valid until {expires_at.isoformat()} UTC): {new_pin}\n\n"
        "If you approve, please call the requester to share this PIN; otherwise contact them for more info."
    )
    to_addrs = [row[0] for row in recipients]  # row = (email, first_name)

    try:
        emailer.send_email(subject=subject, body=body, recipients=to_addrs)
    except Exception as e:
        current_app.logger.error("Failed to send PIN emails: %s", e)
        return jsonify(error="Failed to send emails"), 500

    return jsonify(message="PIN request sent to clinical leads and superusers"), 200


@internal_bp.route('/audit_log', methods=['GET'])
@login_required
def view_audit_log():
    """
    Returns the audit log for clinical leads or superusers.
    Allowed roles: "clinical_lead", "superuser".
    """
    if not hasattr(current_user, 'role') or current_user.role.lower() not in ["clinical_lead", "superuser"]:
        return jsonify({"error": "Unauthorised access"}), 403
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM audit_logs ORDER BY timestamp DESC")
    logs = cursor.fetchall()
    cursor.close()
    conn.close()
    audit_entries = [{
        "user": log["user"],
        "action": log["action"],
        "patient_id": log["patient_id"],
        "timestamp": log["timestamp"].isoformat() if isinstance(log["timestamp"], datetime) else str(log["timestamp"])
    } for log in logs]
    logger.info("Audit log viewed by user %s.", current_user.username)
    return jsonify({"audit_logs": audit_entries}), 200

@internal_bp.route('/prescription', methods=['POST'])
@login_required
def add_prescription():
    """
    Adds a prescription record.
    Allowed roles: "clinical_lead", "superuser".
    Expects JSON with keys: 'patient_id', 'prescribed_by', and 'prescription'.
    """
    if not hasattr(current_user, 'role') or current_user.role.lower() not in ["clinical_lead", "superuser"]:
        return jsonify({"error": "Unauthorised access"}), 403
    data = request.get_json()
    required_fields = ['patient_id', 'prescribed_by', 'prescription']
    if not data or any(field not in data for field in required_fields):
        logger.error("Failed to add prescription: Missing required fields.")
        return jsonify({"error": "Missing required prescription fields"}), 400
    try:
        patient_id = int(data['patient_id'])
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid patient_id"}), 400
    try:
        Prescription.insert_prescription(patient_id, data['prescribed_by'], data['prescription'])
    except Exception as e:
        logger.error("Error inserting prescription: %s", str(e))
        return jsonify({"error": "Error inserting prescription"}), 500
    AuditLog.insert_log(current_user.username, "Added prescription", patient_id=patient_id)
    return jsonify({"message": f"Prescription added successfully for patient_id {patient_id}."}), 201

def get_assigned_patient_count(care_company_user_id):
    """Return the number of patients assigned to a given care company user.
       Assumes that your patients table has a column 'care_company_id'."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM patients WHERE care_company_id = %s", (care_company_user_id,))
    count = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    return count

@internal_bp.route('/clinical/epcr', methods=['GET'])
@login_required
def view_epcr():
    """
    Renders the Clinical Lead Dashboard page that includes:
      - A table listing EPCR cases along with a search bar.
      - An Actions column with View and Delete buttons.
    
    The case record includes:
      - id, status, created_at, updated_at, closed_at, 
      - data (the complete data object, including sections and assignedUsers)
    """
    # Redirect if not authorized.
    if not hasattr(current_user, 'role') or current_user.role.lower() not in ["clinical_lead", "superuser"]:
        return redirect(url_for("medical_records_module"))
    
    # Fetch cases from the database.
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, data, status, created_at, closed_at, updated_at 
        FROM cases 
        ORDER BY id DESC
    """)
    cases = cursor.fetchall()
    cursor.close()
    conn.close()

    epcr_cases = []
    for case in cases:
        # Parse the JSON data from the "data" column.
        try:
            data_obj = json.loads(case.get("data"))
        except (json.JSONDecodeError, TypeError):
            data_obj = case.get("data")
        
        # Extract assignedUsers from within the data object, if available.
        assigned_users = data_obj.get("assignedUsers") if isinstance(data_obj, dict) else []
        
        epcr_cases.append({
            "id": case.get("id"),
            "assignedUsers": assigned_users,
            "status": case.get("status"),
            "created_at": case.get("created_at"),
            "updated_at": case.get("updated_at"),
            "closed_at": case.get("closed_at"),
            "data": data_obj
        })

    logger.info("EPCR dashboard viewed by user %s.", current_user.username)
    return render_template('clinical/clinical_epcr.html', epcr_cases=epcr_cases, current_user=current_user, config=core_manifest)

@internal_bp.route('/clinical/epcr/<int:case_id>', methods=['GET'])
@login_required
def view_epcr_case(case_id):
    """
    Detailed view for a single EPCR case.
    Fetches the raw JSON from MySQL, adds status/timestamps,
    and renders the detailed Jinja template which will handle
    ordering and conditional sections.
    """
    current_app.logger.debug("Entered /clinical/epcr route for case %s", case_id)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT data, status, created_at, closed_at, updated_at "
            "FROM cases WHERE id = %s",
            (case_id,)
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            current_app.logger.error("No case found for id %s", case_id)
            return redirect(url_for('medical_records_internal.view_epcr'))

        data_str, status, created_at, closed_at, updated_at = row
        case_data = json.loads(data_str)

        # Attach metadata for display
        case_data.update({
            'status': status,
            'createdAt': created_at.isoformat() if created_at else None,
            'closedAt':  closed_at.isoformat()  if closed_at  else None,
            'updatedAt': updated_at.isoformat() if updated_at else None
        })

    except Exception as e:
        current_app.logger.error("Database error loading case %s: %s", case_id, e)
        return redirect(url_for('medical_records_internal.view_epcr'))

    # Render template; all section ordering / existence checks live in Jinja now
    return render_template(
        'public/case_access_pdf.html',
        case_data=case_data
    )

@internal_bp.route('/clinical/epcr/delete/<int:case_id>', methods=['POST'])
@login_required
def delete_epcr(case_id):
    """
    Deletes the specified EPCR case.
    Only users with the "clinical_lead" or "superuser" roles are allowed.
    Returns a JSON response indicating success/failure.
    """
    if not hasattr(current_user, 'role') or current_user.role.lower() not in ["clinical_lead", "superuser"]:
        return jsonify({"error": "Unauthorised access"}), 403

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM cases WHERE id = %s", (case_id,))
        conn.commit()
        logger.info("User %s deleted case %s.", current_user.username, case_id)
        response = {"success": True, "case_id": case_id}
    except Exception as e:
        conn.rollback()
        logger.error("Error deleting case %s: %s", case_id, str(e))
        response = {"success": False, "error": str(e)}
    finally:
        cursor.close()
        conn.close()


    return jsonify(response)

@internal_bp.route('/care_company/list', methods=['GET'])
@login_required
def list_care_company_users():
    """
    Lists all care company users along with the number of patients assigned to each.
    Only allowed for admin and superuser roles.
    """
    if current_user.role.lower() not in ['admin', 'superuser', 'clinical_lead']:
        flash("Unauthorised access", "danger")
        return redirect(url_for('routes.dashboard'))
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM care_company_users")
    users = cursor.fetchall()
    cursor.close()
    conn.close()
    for user in users:
        user['assigned_patients'] = get_assigned_patient_count(user['id'])
    return render_template("care_company_users/list.html", users=users, config=core_manifest)

@internal_bp.route('/care_company/add', methods=['GET', 'POST'])
@login_required
def add_care_company_user():
    """
    Adds a new care company user.
    Only allowed for admin and superuser.
    """
    if current_user.role.lower() not in ['admin', 'superuser', 'clinical_lead']:
        flash("Unauthorised access", "danger")
        return redirect(url_for('routes.dashboard'))
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        company_name = request.form.get('company_name')
        contact_phone = request.form.get('contact_phone')
        contact_address = request.form.get('contact_address')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        company_pin = request.form.get('company_pin')
        
        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for('medical_records_internal.add_care_company_user'))
        
        new_password_hash = AuthManager.hash_password(password)
        new_company_pin_hash = None
        if company_pin and company_pin.strip() != "":
            new_company_pin_hash = AuthManager.hash_password(company_pin)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        query = """
            INSERT INTO care_company_users 
            (username, email, password_hash, company_name, contact_phone, contact_address, company_pin_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(query, (username, email, new_password_hash, company_name, contact_phone, contact_address, new_company_pin_hash))
        conn.commit()
        cursor.close()
        conn.close()
        
        flash("Care company user added successfully.", "success")
        return redirect(url_for('medical_records_internal.list_care_company_users'))
    
    return render_template("care_company_users/add.html", config=core_manifest)

@internal_bp.route('/care_company/edit/<user_id>', methods=['GET', 'POST'])
@login_required
def edit_care_company_user(user_id):
    """
    Edits an existing care company user.
    Only allowed for admin and superuser.
    """
    if current_user.role.lower() not in ['admin', 'superuser', 'clinical_lead']:
        flash("Unauthorised access", "danger")
        return redirect(url_for('routes.dashboard'))
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM care_company_users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    if not user:
        cursor.close()
        conn.close()
        flash("User not found", "danger")
        return redirect(url_for('medical_records_internal.list_care_company_users'))
    
    if request.method == 'POST':
        email = request.form.get('email')
        company_name = request.form.get('company_name')
        contact_phone = request.form.get('contact_phone')
        contact_address = request.form.get('contact_address')
        new_password = request.form.get('new_password')
        confirm_new_password = request.form.get('confirm_new_password')
        new_company_pin = request.form.get('new_company_pin')
        
        update_fields = {
            "email": email,
            "company_name": company_name,
            "contact_phone": contact_phone,
            "contact_address": contact_address
        }
        
        if new_password:
            if new_password != confirm_new_password:
                flash("New passwords do not match.", "danger")
                return redirect(url_for('medical_records_internal.edit_care_company_user', user_id=user_id))
            update_fields["password_hash"] = AuthManager.hash_password(new_password)
        
        if new_company_pin and new_company_pin.strip() != "":
            update_fields["company_pin_hash"] = AuthManager.hash_password(new_company_pin)
        
        set_clause = ", ".join([f"{k} = %s" for k in update_fields.keys()])
        params = list(update_fields.values())
        params.append(user_id)
        cursor.execute(f"UPDATE care_company_users SET {set_clause} WHERE id = %s", tuple(params))
        conn.commit()
        cursor.close()
        conn.close()
        flash("User updated successfully.", "success")
        log_audit(current_user.username, "Admin edited care company user", patient_id=user_id)
        return redirect(url_for('medical_records_internal.list_care_company_users'))
    
    cursor.close()
    conn.close()
    return render_template("care_company_users/edit.html", user=user, config=core_manifest)

@internal_bp.route('/care_company/delete/<user_id>', methods=['POST'])
@login_required
def delete_care_company_user(user_id):
    """
    Deletes a care company user.
    Only allowed for admin and superuser.
    """
    if current_user.role.lower() not in ['admin', 'superuser', 'clinical_lead']:
        flash("Unauthorised access", "danger")
        return redirect(url_for('routes.dashboard'))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM care_company_users WHERE id = %s", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash("User deleted successfully.", "success")
    log_audit(current_user.username, "Admin deleted care company user", patient_id=user_id)
    return redirect(url_for('medical_records_internal.list_care_company_users'))

# =============================================================================
# PUBLIC BLUEPRINT (Care Company Interface)
# =============================================================================
public_template_folder = os.path.join(os.path.dirname(__file__), 'templates', 'public')
public_bp = Blueprint(
    'care_company',
    __name__,
    url_prefix='/care_company',
    template_folder=public_template_folder
)

@public_bp.route('/login', methods=['GET', 'POST'])
def care_company_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = CareCompanyUser.get_user_by_username(username)
        if not user or not user.check_password(password):
            flash("Invalid username or password", "error")
            return render_template("care_company_login.html", config=core_manifest)
        login_user(user)
        flash("Logged in successfully", "success")
        log_audit(user.id, "Care company user logged in")
        return redirect(url_for('care_company.dashboard'))
    return render_template("care_company_login.html", config=core_manifest)

@public_bp.route('/logout')
@login_required
def care_company_logout():

    flash("You have been logged out", "success")
    log_audit(current_user.username, "Care company user logged out")
    logout_user()
    session.clear()
    return redirect(url_for('care_company.care_company_login'))

@public_bp.route('/reset_password_request', methods=['GET', 'POST'])
def reset_password_request():
    if request.method == 'POST':
        email = request.form.get('email')
        user = CareCompanyUser.get_user_by_email(email)
        if user:
            token = user.generate_reset_token()
            send_reset_email(user, token)
        
        flash("If that account exists. An email has been sent with instructions to reset your password.", "info")

        return redirect(url_for('care_company.care_company_login'))
    return render_template("reset_password_request.html", config=core_manifest)

@public_bp.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    user = CareCompanyUser.verify_reset_token(token)
    if not user:
        flash("The reset token is invalid or has expired.", "error")
        return redirect(url_for('care_company.reset_password_request'))
    if request.method == 'POST':
        new_password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if new_password != confirm_password:
            flash("Passwords do not match", "error")
            return render_template("reset_password.html", token=token, config=core_manifest)
        new_hash = AuthManager.hash_password(new_password)
        CareCompanyUser.update_password(user.id, new_hash)
        log_audit(user.id, "Care company user reset password")
        flash("Your password has been reset.", "success")
        return redirect(url_for('care_company.care_company_login'))
    return render_template("reset_password.html", token=token, config=core_manifest)

@public_bp.before_request
def ensure_vita_care_portal_user():
    # If the user isn't authenticated yet, let the login_required decorator handle it.
    if not current_user.is_authenticated:
        return
    # Now that the user is authenticated, ensure they are from the Vita-Care-Portal module.
    if not hasattr(current_user, 'role') or current_user.role != "Vita-Care-Portal":
        return jsonify({"error": "Unauthorised access"}), 403

@public_bp.route('/')
@login_required
def dashboard():
    log_audit(current_user.username, "Care company dashboard accessed")
    search = request.args.get('q')
    patients = Patient.get_all(search, company_id=current_user.id)
    current_date = datetime.utcnow().date()
    for p in patients:
        if p.get("date_of_birth"):
            p["age"] = calculate_age(p["date_of_birth"], current_date)
        else:
            p["age"] = "N/A"
    return render_template("care_company_home.html", clients=patients, config=core_manifest)

@public_bp.route('/add_client_record', methods=['POST'])
@login_required
def add_client_record():
    nhs_number = request.form.get("nhs_number")
    first_name = request.form.get('first_name')
    middle_name = request.form.get('middle_name')
    last_name = request.form.get('last_name')
    address = request.form.get('address')
    contact_number = request.form.get("contact_number")
    gp_details = request.form.get('gp_details') or None
    medical_conditions = request.form.get('medical_conditions') or None
    allergies = request.form.get('allergies') or None
    medications = request.form.get('medications') or None
    previous_visit_records = request.form.get('previous_visit_records') or None
    package_type = "Bronze"
    notes = request.form.get('notes') or None
    message_log = request.form.get('message_log') or None
    access_requirements = request.form.get('access_requirements') or None
    payment_details = request.form.get('payment_details') or None
    next_of_kin_details = request.form.get('next_of_kin_details') or None
    lpa_details = request.form.get('lpa_details') or None
    resuscitation_directive = request.form.get('resuscitation_directive') or None
    documents = request.form.get('documents') or None
    dob_str = request.form.get('date_of_birth')
    weight = request.form.get('weight') or None
    gender = request.form.get('gender') or None
    postcode = request.form.get('postcode')
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d").date() if dob_str else None
    except ValueError:
        dob = None
    new_record = {
        "nhs_number": nhs_number,
        "first_name": first_name,
        "middle_name": middle_name,
        "last_name": last_name,
        "address": address,
        "contact_number": contact_number,
        "gp_details": gp_details,
        "medical_conditions": medical_conditions,
        "allergies": allergies,
        "medications": medications,
        "previous_visit_records": previous_visit_records,
        "package_type": package_type,
        "notes": notes,
        "message_log": message_log,
        "access_requirements": access_requirements,
        "payment_details": payment_details,
        "next_of_kin_details": next_of_kin_details,
        "lpa_details": lpa_details,
        "resuscitation_directive": resuscitation_directive,
        "documents": documents,
        "date_of_birth": dob,
        "weight": weight,
        "gender": gender,
        "postcode": postcode,
        "care_company_id": current_user.id  # Assign the current care company user id
    }
    try:
        Patient.add_patient(**new_record)
        log_audit(current_user.username, f"Added client record: {first_name} {last_name}")
    except Exception as e:
        logger.error("Error adding client record: %s", str(e))
        flash("Error adding client record", "danger")
        return redirect(url_for('care_company.dashboard'))
    flash("Client record added successfully.", "success")
    return redirect(url_for('care_company.dashboard'))

from flask import (
    render_template, request, redirect, url_for, flash, session
)
from flask_login import login_required, current_user
from datetime import datetime
import json

@public_bp.route('/edit_client_record/<id>', methods=['GET', 'POST'])
@login_required
def edit_client_record(id):
    # POST: save updates
    if request.method == 'POST':
        client_existing = Patient.get_by_id(id)
        if not client_existing:
            flash("Client record not found.", "danger")
            return redirect(url_for('care_company.dashboard'))

        # simple scalars
        first_name   = request.form.get('first_name', '').strip()   or client_existing.get('first_name')
        middle_name  = request.form.get('middle_name', '').strip()  or client_existing.get('middle_name')
        last_name    = request.form.get('last_name', '').strip()    or client_existing.get('last_name')
        address      = request.form.get('address', '').strip()      or client_existing.get('address')
        postcode     = request.form.get('postcode', '').strip()     or client_existing.get('postcode')
        gender       = request.form.get('gender', '').strip()       or client_existing.get('gender')
        dob_str      = request.form.get('date_of_birth', '').strip()
        try:
            dob = datetime.strptime(dob_str, "%Y-%m-%d").date() if dob_str else client_existing.get('date_of_birth')
        except ValueError:
            dob = client_existing.get('date_of_birth')

        # helper for list fields
        def load_list(field):
            raw = request.form.get(field, '').strip()
            if raw:
                try:
                    arr = json.loads(raw)
                    return arr if isinstance(arr, list) else []
                except:
                    return []
            stored = client_existing.get(field, '[]')
            try:
                arr = json.loads(stored)
                return arr if isinstance(arr, list) else []
            except:
                return []

        medical_conditions     = load_list('medical_conditions')
        allergies              = load_list('allergies')
        medications            = load_list('medications')
        access_requirements    = load_list('access_requirements')
        previous_visit_records = load_list('previous_visit_records')
        notes                  = load_list('notes')
        message_log            = load_list('message_log')
        next_of_kin_details    = load_list('next_of_kin_details')
        lpa_details            = load_list('lpa_details')

        # helper for dict fields
        def load_dict(field):
            raw = client_existing.get(field, '')
            if isinstance(raw, str) and raw.strip():
                try:
                    obj = json.loads(raw)
                    return obj if isinstance(obj, dict) else {}
                except:
                    return {}
            return raw if isinstance(raw, dict) else {}

        unlocked = session.get(f'unlocked_{id}', False)

        if unlocked:
            # user provided these in form
            gp_details = {
                "name":    request.form.get('gp_name','').strip(),
                "address": request.form.get('gp_address','').strip(),
                "contact": request.form.get('gp_contact','').strip(),
                "email":   request.form.get('gp_email','').strip()
            }
            weight = {
                "weight":       request.form.get('weight_value','').strip(),
                "date_weighed": request.form.get('date_weighed','').strip(),
                "source":       request.form.get('weight_source','').strip()
            }
            payment_details = {
                "payment_method": request.form.get('payment_method','').strip(),
                "billing_email":  request.form.get('billing_email','').strip()
            }
            # resuscitation docs
            docs = []
            for fld, label in [
                ('doc_dnar','DNAR'),
                ('doc_respect','Respect Form'),
                ('doc_advanced','Advanced Directive'),
                ('doc_living','Living Will'),
                ('doc_lpa','LPA'),
                ('doc_care','Care Plan')
            ]:
                if request.form.get(fld):
                    docs.append(label)
            resuscitation_directive = {
                "for_resuscitation": request.form.get('resus_option','').strip(),
                "documents":         docs
            }
        else:
            # preserve existing values
            gp_details               = load_dict('gp_details')
            weight                   = load_dict('weight')
            payment_details          = load_dict('payment_details')
            resuscitation_directive  = load_dict('resuscitation_directive')

        # assemble and persist
        update_fields = {
            "first_name":             first_name,
            "middle_name":            middle_name,
            "last_name":              last_name,
            "address":                address,
            "date_of_birth":          dob,
            "gender":                 gender,
            "postcode":               postcode,
            "gp_details":             json.dumps(gp_details),
            "weight":                 json.dumps(weight),
            "payment_details":        json.dumps(payment_details),
            "resuscitation_directive":json.dumps(resuscitation_directive),
            "medical_conditions":     json.dumps(medical_conditions),
            "allergies":              json.dumps(allergies),
            "medications":            json.dumps(medications),
            "previous_visit_records": json.dumps(previous_visit_records),
            "access_requirements":    json.dumps(access_requirements),
            "notes":                  json.dumps(notes),
            "message_log":            json.dumps(message_log),
            "next_of_kin_details":    json.dumps(next_of_kin_details),
            "lpa_details":            json.dumps(lpa_details),
        }

        try:
            Patient.update_patient(id, **update_fields)
            log_audit(current_user.username, f"Edited client record: {id}", patient_id=id)
            flash("Client record updated successfully.", "success")
        except Exception as e:
            logger.error("Error updating client record: %s", e)
            flash("Error updating client record", "danger")
            return redirect(url_for('care_company.view_client_record', id=id))

        return redirect(url_for('care_company.view_client_record', id=id))

    # GET: render edit form
    client = Patient.get_by_id(id)
    if not client or client.get("care_company_id") != current_user.id:
        flash("Client record not found or access denied.", "danger")
        return redirect(url_for('care_company.dashboard'))

    log_audit(current_user.username, f"Accessed edit view for client record: {id}", patient_id=id)
    unlocked = session.get(f'unlocked_{id}', False)

    # parse dict fields
    dict_keys = ['gp_details','payment_details','resuscitation_directive','weight']
    for key in dict_keys:
        raw = client.get(key)
        try:
            parsed = json.loads(raw.strip()) if isinstance(raw, str) else raw
            client[key] = parsed if isinstance(parsed, dict) else {}
        except:
            client[key] = {}

    # parse list fields
    list_keys = [
        'medical_conditions','allergies','medications',
        'previous_visit_records','access_requirements',
        'next_of_kin_details','lpa_details'
    ]
    for key in list_keys:
        raw = client.get(key)
        try:
            parsed = json.loads(raw.strip()) if isinstance(raw, str) else raw
            client[key] = parsed if isinstance(parsed, list) else []
        except:
            client[key] = []

    # mask if locked
    if not unlocked:
        for field in ['gp_details','payment_details','resuscitation_directive','weight','next_of_kin_details','lpa_details']:
            client[field] = "*******"

    age = calculate_age(client.get("date_of_birth")) if client.get("date_of_birth") else "N/A"
    return render_template(
        "edit_client.html",
        client=client,
        unlocked=unlocked,
        age=age,
        config=core_manifest
    )

@public_bp.route('/view_client_record/<id>')
@login_required
def view_client_record(id):
    client = Patient.get_by_id(id)
    if not client:
        return render_template("view_client.html",
                               error="Client record not found",
                               config=core_manifest)
    if client.get("care_company_id") != current_user.id:
        flash("You do not have access to this record", "danger")
        return redirect(url_for("care_company.dashboard"))

    unlocked = session.get(f'unlocked_{id}', False)

    # --- parse single‑object JSON → dicts only ---
    dict_keys = [
        'gp_details',
        'payment_details',
        'resuscitation_directive',
        'weight',
    ]
    for key in dict_keys:
        raw = client.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw.strip())
                client[key] = parsed if isinstance(parsed, dict) else {}
            except Exception:
                client[key] = {}
        else:
            client[key] = raw if isinstance(raw, dict) else {}

    # --- parse JSON arrays → lists of dicts ---
    list_keys = [
        'medical_conditions',
        'allergies',
        'medications',
        'previous_visit_records',
        'access_requirements',
        'next_of_kin_details',
        'lpa_details',
        'notes',
        'message_log'
    ]
    for key in list_keys:
        raw = client.get(key)
        if isinstance(raw, list):
            continue
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw.strip())
                if isinstance(parsed, list):
                    client[key] = parsed
                elif isinstance(parsed, dict):
                    client[key] = [parsed]
                else:
                    client[key] = []
            except Exception:
                client[key] = []
        else:
            client[key] = []

    # --- mask sensitive fields if locked ---
    if not unlocked:
        sensitive = [
            'gp_details',
            'payment_details',
            'resuscitation_directive',
            'weight',
            'medical_conditions',
            'allergies',
            'medications',
            'previous_visit_records',
            'access_requirements',
            'next_of_kin_details',
            'lpa_details',
        ]
        for field in sensitive:
            if field in client:
                client[field] = "*******"

    age = calculate_age(client.get("date_of_birth")) if client.get("date_of_birth") else "N/A"
    log_audit(current_user.username, "Care company viewed client record", patient_id=id)

    return render_template(
        "view_client.html",
        client=client,
        age=age,
        unlocked=unlocked,
        config=core_manifest
    )
@public_bp.route('/search')
@login_required
def search():
    search = request.args.get('q')
    patients = Patient.get_all(search)
    current_date = datetime.utcnow().date()
    for p in patients:
        if p.get("date_of_birth"):
            p["age"] = calculate_age(p["date_of_birth"], current_date)
        else:
            p["age"] = "N/A"
    return jsonify({"clients": patients})
@public_bp.route('/add_message_log_entry/<id>', methods=['POST'])
@login_required
def add_message_log_entry(id):
    """
    AJAX endpoint to add a message log entry to the patient's record.
    Expects 'category' (optionally 'custom_category') and 'message' in the POST form data.
    """
    category = request.form.get('category')
    custom_category = request.form.get('custom_category')
    message_text = request.form.get('message')
    if category == "Other" and custom_category:
        category = custom_category
    if not category or not message_text:
        return jsonify({'error': 'Category and message are required.'}), 400
    new_message = {
        'id': str(uuid.uuid4()),
        'author': current_user.username,
        'timestamp': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        'category': category,
        'text': message_text
    }
    patient = Patient.get_by_id(id)
    if not patient:
        return jsonify({'error': 'Patient record not found.'}), 404
    if patient.get("care_company_id") != current_user.id:
        flash("You do not have access to this record", "danger")
        return redirect(url_for("care_company.dashboard"))
    message_log = patient.get("message_log")
    if not message_log or message_log == "":
        message_log = []
    elif not isinstance(message_log, list):
        try:
            message_log = json.loads(message_log)
            if not isinstance(message_log, list):
                message_log = []
        except Exception:
            message_log = []
    message_log.append(new_message)
    try:
        Patient.update_patient(id, message_log=json.dumps(message_log))
        log_audit(current_user.username, "Care company added message log entry", patient_id=id)
    except Exception as e:
        logger.error("Error updating patient (message log): %s", str(e))
        return jsonify({'error': 'Failed to save message log entry.'}), 500
    return jsonify({'success': True}), 200

@public_bp.route('/add_risk_flag/<id>', methods=['POST'])
@login_required
def add_risk_flag(id):
    """
    AJAX endpoint to add a risk flag (note) to the patient's record.
    Expects 'flag_type', optionally 'custom_flag_type', and 'description' in the POST form data.
    """
    flag_type = request.form.get('flag_type')
    custom_flag_type = request.form.get('custom_flag_type')
    description = request.form.get('description')
    if flag_type == "Other" and custom_flag_type:
        flag_type = custom_flag_type
    if not flag_type or not description:
        return jsonify({'error': 'Risk category and description are required.'}), 400
    new_flag = {
        'id': str(uuid.uuid4()),
        'flag_type': flag_type,
        'timestamp': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        'description': description
    }
    patient = Patient.get_by_id(id)
    if not patient:
        return jsonify({'error': 'Patient record not found.'}), 404
    risk_flags = patient.get("notes")
    if not risk_flags or risk_flags == "":
        risk_flags = []
    elif not isinstance(risk_flags, list):
        try:
            risk_flags = json.loads(risk_flags)
            if not isinstance(risk_flags, list):
                risk_flags = []
        except Exception:
            risk_flags = []
    risk_flags.append(new_flag)
    try:
        Patient.update_patient(id, notes=json.dumps(risk_flags))
        log_audit(current_user.username, "Care company added risk flag", patient_id=id)
    except Exception as e:
        logger.error("Error updating patient (risk flag): %s", str(e))
        return jsonify({'error': 'Failed to save risk flag.'}), 500
    return jsonify({'success': True}), 200

@public_bp.route('/unlock_record', methods=['POST'])
@login_required
def unlock_record():
    id = request.form.get("id")
    pin = request.form.get("pin")
    access_reason = request.form.get('access_reason')

    if not AuthManager.verify_password(current_user.company_pin_hash, pin):
        return jsonify({"error": "Invalid PIN"}), 403
    session[f'unlocked_{id}'] = True
    log_audit(current_user.username, f"Care company unlocked record for the following reason: {access_reason}", patient_id=id)
    return jsonify({
        "message": "Record unlocked successfully",
        "redirect_url": url_for('care_company.view_client_record', id=id)
    })

@public_bp.route('/lock_record', methods=['POST'])
@login_required
def lock_record():
    id = request.form.get("id")
    session[f'unlocked_{id}'] = False
    log_audit(current_user.username, "Care company locked record", patient_id=id)
    return redirect(url_for('care_company.dashboard'))

@internal_bp.route('/api/cases', methods=['GET', 'POST', 'OPTIONS'])
def cases():
    # Handle OPTIONS requests for CORS pre-flight.
    if request.method == 'OPTIONS':
        return '', 200

    # GET: Return cases, optionally filtering by a username passed as a query parameter.
    if request.method == 'GET':
        # Read an optional "username" query parameter.
        username = request.args.get('username', None)

        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            query = "SELECT id, data, status, created_at, closed_at, updated_at FROM cases ORDER BY created_at DESC"
            cursor.execute(query)
            rows = cursor.fetchall()

            cases = []
            # Use local time here (change to datetime.utcnow().date() if using UTC)
            today = datetime.now().date()
            for row in rows:
                case_id, data_str, status, created_at, closed_at, updated_at = row
                try:
                    data = json.loads(data_str)
                except Exception:
                    data = {}

                # Merge the metadata from DB into the case data.
                data.update({
                    'id': case_id,
                    'status': status,
                    'created_at': created_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(created_at, "strftime") else created_at,
                    'updated_at': updated_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(updated_at, "strftime") else updated_at,
                })
                if closed_at:
                    data['closed_at'] = closed_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(closed_at, "strftime") else closed_at
                else:
                    data['closed_at'] = None
                    

                # For cases marked as closed, include them only if closed_at is today.
                if status.lower() == "closed" and closed_at:
                    if closed_at.date() != today:
                        continue
                
                # If a username filter is provided, include only cases with that user in 'assignedUsers'.
                if username:
                    assigned_users = data.get('assignedUsers', [])
                    if username in assigned_users:
                        cases.append(data)
                    else:
                        pass

                

            cursor.close()
            conn.close()
            return jsonify(cases), 200

        except Exception as e:
            conn.rollback()
            cursor.close()
            conn.close()
            print({'error': str(e)})
            return jsonify({'error': str(e)}), 500

    # POST: Create a new case.
    if request.method == 'POST':
        payload = request.get_json()
        if not payload:
            return jsonify({'error': 'No JSON payload provided'}), 400

        payload_str = json.dumps(payload)
        status = payload.get('status', 'in progress')
        created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        updated_at = created_at

        # Expect the client to provide an 'id'
        case_id = payload.get('id')
        if not case_id:
            return jsonify({'error': 'Case id not provided in payload'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            query = """
                INSERT INTO cases (id, data, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                        data = VALUES(data),
                        status = VALUES(status),
                        updated_at = VALUES(updated_at)
            """
            cursor.execute(query, (case_id, payload_str, status, created_at, updated_at))
            conn.commit()
            cursor.close()
            conn.close()
            return jsonify({'message': 'Case created successfully', 'case': payload}), 201
        except Exception as e:
            conn.rollback()
            cursor.close()
            conn.close()
            return jsonify({'error': str(e)}), 500

@internal_bp.route('/api/cases/<int:case_id>', methods=['GET', 'PUT', 'OPTIONS'])
def case_handler(case_id):
    if request.method == 'OPTIONS':
        return '', 200

    conn = get_db_connection()
    cursor = conn.cursor()
    if request.method == 'GET':
        try:
            query = "SELECT data, status, created_at, closed_at, updated_at FROM cases WHERE id = %s"
            cursor.execute(query, (case_id,))
            row = cursor.fetchone()
            if row:
                data_str, status, created_at, closed_at, updated_at = row
                case_data = json.loads(data_str)
                case_data['status'] = status
                case_data['createdAt'] = created_at.isoformat() if created_at else None
                case_data['closedAt'] = closed_at.isoformat() if closed_at else None
                case_data['updatedAt'] = updated_at.isoformat() if updated_at else None
                cursor.close()
                conn.close()
                return jsonify(case_data), 200
            else:
                cursor.close()
                conn.close()
                return jsonify({'error': 'Case not found'}), 404
        except Exception as e:
            conn.rollback()
            cursor.close()
            conn.close()
            return jsonify({'error': str(e)}), 500

    elif request.method == 'PUT':
        payload = request.get_json()
        if not payload:
            cursor.close()
            conn.close()
            return jsonify({'error': 'No JSON payload provided'}), 400

        payload_str = json.dumps(payload)
        status = payload.get('status', 'in progress')
        closed_at = payload.get('closedAt', None)
        updated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        # If no created_at is provided, use updated_at for new records.
        created_at = updated_at  

        closed_at_str = None
        if closed_at:
            try:
                # Replace trailing 'Z' with '+00:00' so we get a timezone-aware datetime:
                dt_utc = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                # Convert the UTC time to local time; this assumes your server's local time zone.
                dt_local = dt_utc.astimezone()
                closed_at_str = dt_local.strftime("%Y-%m-%d %H:%M:%S")
            except Exception as e:
                cursor.close()
                conn.close()
                return jsonify({'error': f"Invalid closedAt format: {str(e)}"}), 400

        try:
            if closed_at_str:
                query = """
                    INSERT INTO cases (id, data, status, created_at, updated_at, closed_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        data = VALUES(data),
                        status = VALUES(status),
                        updated_at = VALUES(updated_at),
                        closed_at = VALUES(closed_at)
                """
                cursor.execute(query, (case_id, payload_str, status, created_at, updated_at, closed_at_str))

                # map sections
                sec_map       = {sec['name']: sec['content'] for sec in payload.get('sections', [])}
                incident      = sec_map.get('Incident Log', {}).get('incident', {})
                response_type = (incident.get('responseType') or '').strip().lower()

                # only send finance email if not an "event"
                if response_type != 'event':
                    finance_recipient = os.environ.get('FINANCE_EMAIL')
                    if finance_recipient:
                        # gather data
                        billing   = sec_map.get('Billing', {}).get('billing', {})
                        ptinfo    = sec_map.get('PatientInfo', {}).get('ptInfo', {})
                        member    = ptinfo.get('urgentCareMember', {})
                        handover  = sec_map.get('Clinical Handover', {}).get('handoverData', {})
                        receiving = handover.get('receivingHospital', {})
                        drugs     = sec_map.get('Drugs Administered', {}).get('drugsAdministered', [])

                        # patient name
                        patient_name = " ".join(filter(None, [ptinfo.get('forename'), ptinfo.get('surname')]))

                        # compute drug cost summary
                        total_drug_cost = 0.0
                        for drug in drugs:
                            try:
                                total_drug_cost += float(drug.get('cost', 0))
                            except (TypeError, ValueError):
                                pass
                        overall_drug_cost = total_drug_cost

                        # email subject
                        subject = f"EPCR ({incident.get('responseType')}) - Case: {case_id}"

                        # build email body lines
                        lines = [
                            fmt("Patient Name", patient_name),
                            fmt("Case ID", case_id),
                            fmt("Closed At", closed_at_str),
                            ""
                        ]

                        # Billing
                        if any(billing.get(k) for k in ("payeeName","payeeEmail","payeeAddress","notes")):
                            lines += [
                                "-- Billing Information --",
                                fmt("Payee Name", billing.get("payeeName")),
                                fmt("Payee Email", billing.get("payeeEmail")),
                                fmt("Payee Address", billing.get("payeeAddress")),
                                fmt("Notes", billing.get("notes")),
                                ""
                            ]

                        # Membership
                        if any(member.get(k) for k in ("isMember","membershipNumber","primaryType","membershipLevel")):
                            lines += [
                                "-- Membership --",
                                fmt("Member?", member.get("isMember")),
                                fmt("Membership Number", member.get("membershipNumber")),
                                fmt("Membership Type", member.get("primaryType")),
                                fmt("Membership Level", member.get("membershipLevel")),
                                ""
                            ]

                        # Incident Details
                        lines += [
                            "-- Incident Details --",
                            fmt("Response Type", incident.get("responseType")),
                            fmt("Response Other", incident.get("responseOther")),
                        ]

                        # Conveyance Outcome
                        if handover.get("outcome"):
                            lines += [
                                "",
                                "-- Conveyance Outcome --",
                                fmt("Outcome Code", handover.get("outcome"))
                            ]
                            if handover.get("otherOutcomeDetails"):
                                lines.append(fmt("Details", handover.get("otherOutcomeDetails")))

                        # Receiving Hospital
                        if any(receiving.get(k) for k in ("hospital","ward","otherWard","otherHospitalDetails")):
                            lines += [
                                "",
                                "-- Receiving Hospital --",
                                fmt("Hospital", receiving.get("hospital")),
                                fmt("Ward", receiving.get("ward")),
                                fmt("Other Ward", receiving.get("otherWard")),
                                fmt("Other Hospital Details", receiving.get("otherHospitalDetails")),
                            ]

                        # Drugs Administered
                        if drugs:
                            lines += ["", "-- Drugs Administered --"]
                            for i, drug in enumerate(drugs, start=1):
                                lines += [
                                    fmt(f"Drug {i} Name",            drug.get("drugName")),
                                    fmt(f"Drug {i} Dosage",          drug.get("dosage")),
                                    fmt(f"Drug {i} Batch Number",    drug.get("batchNumber")),
                                    fmt(f"Drug {i} Expiry Date",     drug.get("expiryDate")),
                                    fmt(f"Drug {i} Administered By", drug.get("administeredBy")),
                                    fmt(f"Drug {i} Route",           drug.get("route")),
                                    fmt(f"Drug {i} Time Administered", drug.get("timeAdministered")),
                                    fmt(f"Drug {i} Notes",           drug.get("notes")),
                                    fmt(f"Drug {i} Cost Consent",    drug.get("costConsent")),
                                    fmt(f"Drug {i} Cost £",            drug.get("cost")),      # ← added
                                ]

                            # # Drug cost summary
                            # lines += [
                            #     "",
                            #     "-- Drug Cost Summary --",
                            #     fmt("Total Drug Cost", total_drug_cost),
                            #     fmt("Overall Drug Cost", overall_drug_cost),
                            # ]

                        # Timings
                        lines += [
                            "",
                            "-- Timings --",
                            fmt("Time Of Call", incident.get("timeOfCall")),
                            fmt("Time Mobile", incident.get("timeMobile")),
                            fmt("Time At Scene", incident.get("timeOfScene") or incident.get("timeAtScene")),
                            fmt("Time Leave Scene", incident.get("timeLeaveScene")),
                            fmt("Time At Hospital", incident.get("timeAtHospital")),
                            fmt("Time Handover", incident.get("timeHandover")),
                            fmt("Time Left Hospital", incident.get("timeLeftHospital")),
                            fmt("Time At Treatment Centre", incident.get("timeAtTreatmentCentre")),
                            fmt("Time Of Prealert", incident.get("timeOfPrealert")),
                        ]

                        # Support note
                        lines += [
                            "",
                            "If you have any issues, please contact support."
                        ]

                        body     = "\n".join(lines)
                        to_addrs = [finance_recipient]

                        try:
                            emailer.send_email(subject=subject, body=body, recipients=to_addrs)
                        except Exception as email_err:
                            current_app.logger.error(f"Failed to send finance email: {email_err}")
                    else:
                        current_app.logger.warning("FINANCE_EMAIL not configured; skipping finance notification")

            else:
                query = """
                    INSERT INTO cases (id, data, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        data = VALUES(data),
                        status = VALUES(status),
                        updated_at = VALUES(updated_at)
                """
                cursor.execute(query, (case_id, payload_str, status, created_at, updated_at))
            conn.commit()
            cursor.close()
            conn.close()
            return jsonify({'message': 'Case updated successfully', 'case': payload}), 200
        except Exception as e:
            conn.rollback()
            cursor.close()
            conn.close()
            return jsonify({'error': str(e)}), 500



def fmt(label: str, val) -> str:
    """Format a label/value pair, defaulting to 'N/A' if val is falsy."""
    return f"{label}: {val}" if val else f"{label}: N/A"

@internal_bp.route('/api/cases/<int:case_id>/close', methods=['PUT', 'OPTIONS'])
def close_case(case_id):
    if request.method == 'OPTIONS':
        return '', 200

    payload = request.get_json()
    if not payload:
        return jsonify({'error': 'No JSON payload provided'}), 400

    # parse closedAt
    closed_at = payload.get('closedAt')
    if closed_at:
        try:
            dt = datetime.fromisoformat(closed_at)
        except Exception as e:
            return jsonify({'error': f"Invalid closedAt format: {str(e)}"}), 400
    else:
        dt = datetime.utcnow()
    closed_at_str = dt.strftime("%Y-%m-%d %H:%M:%S")

    # Update DB
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE cases SET data=%s, status=%s, closed_at=%s, updated_at=%s WHERE id=%s",
            (json.dumps(payload), 'closed', closed_at_str,
             datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), case_id)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        cursor.close()
        conn.close()
        return jsonify({'error': str(e)}), 500

    # Prepare email only if responseType != "event"
    sec_map       = {sec['name']: sec['content'] for sec in payload.get('sections', [])}
    incident      = sec_map.get('Incident Log', {}).get('incident', {})
    response_type = (incident.get('responseType') or '').strip().lower()

    if response_type != 'event':
        finance_recipient = os.environ.get('FINANCE_EMAIL')
        if finance_recipient:
            # extract content
            billing    = sec_map.get('Billing', {}).get('billing', {})
            ptinfo     = sec_map.get('PatientInfo', {}).get('ptInfo', {})
            member     = ptinfo.get('urgentCareMember', {})
            handover   = sec_map.get('Clinical Handover', {}).get('handoverData', {})
            receiving  = handover.get('receivingHospital', {})

            # build patient full name
            patient_name = " ".join(filter(None, [ptinfo.get('forename'), ptinfo.get('surname')]))

            subject = f"EPCR ({incident.get('responseType')}) - Case: {case_id}"

            lines = [
                fmt("Patient Name", patient_name),
                fmt("Case ID", case_id),
                fmt("Closed At", closed_at_str),
                ""
            ]

            # Billing block
            if any(billing.get(k) for k in ("payeeName","payeeEmail","payeeAddress","notes")):
                lines += ["-- Billing Information --",
                          fmt("Payee Name", billing.get("payeeName")),
                          fmt("Payee Email", billing.get("payeeEmail")),
                          fmt("Payee Address", billing.get("payeeAddress")),
                          fmt("Notes", billing.get("notes")),
                          ""]

            # Membership block
            if any(member.get(k) for k in ("isMember","membershipNumber","primaryType","membershipLevel")):
                lines += ["-- Membership --",
                          fmt("Member?", member.get("isMember")),
                          fmt("Membership Number", member.get("membershipNumber")),
                          fmt("Membership Type", member.get("primaryType")),
                          fmt("Membership Level", member.get("membershipLevel")),
                          ""]

            # Incident Details
            lines += ["-- Incident Details --",
                      fmt("Response Type", incident.get("responseType")),
                      fmt("Response Other", incident.get("responseOther"))]

            # Conveyance Outcome
            if handover.get("outcome"):
                lines += ["", "-- Conveyance Outcome --",
                          fmt("Outcome Code", handover.get("outcome"))]
                if handover.get("otherOutcomeDetails"):
                    lines.append(fmt("Details", handover.get("otherOutcomeDetails")))

            # Receiving hospital
            if any(receiving.get(k) for k in ("hospital","ward","otherWard","otherHospitalDetails")):
                lines += ["", "-- Receiving Hospital --",
                          fmt("Hospital", receiving.get("hospital")),
                          fmt("Ward", receiving.get("ward")),
                          fmt("Other Ward", receiving.get("otherWard")),
                          fmt("Other Hospital Details", receiving.get("otherHospitalDetails"))]

            # Timings
            lines += ["", "-- Timings --",
                      fmt("Time Of Call", incident.get("timeOfCall")),
                      fmt("Time Mobile", incident.get("timeMobile")),
                      fmt("Time At Scene", incident.get("timeOfScene") or incident.get("timeAtScene")),
                      fmt("Time Leave Scene", incident.get("timeLeaveScene")),
                      fmt("Time At Hospital", incident.get("timeAtHospital")),
                      fmt("Time Handover", incident.get("timeHandover")),
                      fmt("Time Left Hospital", incident.get("timeLeftHospital")),
                      fmt("Time At Treatment Centre", incident.get("timeAtTreatmentCentre")),
                      fmt("Time Of Prealert", incident.get("timeOfPrealert"))]

            body = "\n".join(lines)
            to_addrs = [finance_recipient]

            try:
                emailer.send_email(subject=subject, body=body, recipients=to_addrs)
            except Exception as email_err:
                current_app.logger.error(f"Failed to send finance email: {email_err}")
        else:
            current_app.logger.warning("FINANCE_EMAIL not configured; skipping finance notification")

    cursor.close()
    conn.close()
    return jsonify({'message': 'Case closed successfully', 'case': payload}), 200


import os
import json
import base64
import tempfile
from io import BytesIO
from flask import render_template, request, send_file, current_app
# import weasyprint  # Optional: if you later decide to convert to PDF
import re

# Custom filter: convert camelCase to Title Case words.
def split_camel(value):
    # Use regex to insert spaces between lower and uppercase letters and then title-case the result.
    return re.sub(r'([a-z])([A-Z])', r'\1 \2', value).title()

@public_bp.app_template_filter('splitCamel')
def splitCamel_filter(value):
    return split_camel(value)

def process_images(obj, temp_dir):
    """
    Recursively process any base64-encoded image strings in the JSON object.
    Decode them, save to a file in temp_dir, and replace the value with the file path.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and value.startswith('data:image'):
                try:
                    header, encoded = value.split(',', 1)
                    ext = header.split('/')[1].split(';')[0]
                    filename = f"{key}.{ext}"
                    filepath = os.path.join(temp_dir, filename)
                    with open(filepath, "wb") as f:
                        f.write(base64.b64decode(encoded))
                    current_app.logger.debug("Processed image for key '%s', saved to: %s", key, filepath)
                    obj[key] = filepath
                except Exception as e:
                    current_app.logger.error("Failed to process image for key '%s': %s", key, e)
            else:
                process_images(value, temp_dir)
    elif isinstance(obj, list):
        for item in obj:
            process_images(item, temp_dir)

@public_bp.route('/case-access', methods=['GET', 'POST'])
def case_access():
    current_app.logger.debug("Entered /case-access route.")
    error = None
    case_data = None

    if request.method == 'POST':
        current_app.logger.debug("Processing POST request.")
        case_ref = request.form.get('case_ref')
        dob = request.form.get('dob')
        access_pin = request.form.get('access_pin')
        current_app.logger.debug("Received form data: case_ref=%s, dob=%s, access_pin=%s", case_ref, dob, access_pin)

        # Validate case_ref format.
        if not (case_ref and case_ref.isdigit() and len(case_ref) == 10):
            error = "Case Reference Number must be exactly 10 digits."
            current_app.logger.error("Validation error: %s", error)
            return render_template('case_access.html', error=error)

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            query = "SELECT data, status, created_at, closed_at, updated_at FROM cases WHERE id = %s"
            cursor.execute(query, (case_ref,))
            row = cursor.fetchone()
            if row:
                data_str, status, created_at, closed_at, updated_at = row
                case_data = json.loads(data_str)
                # Augment with metadata.
                case_data['status'] = status
                case_data['createdAt'] = created_at.isoformat() if created_at else None
                case_data['closedAt'] = closed_at.isoformat() if closed_at else None
                case_data['updatedAt'] = updated_at.isoformat() if updated_at else None
                current_app.logger.debug("Case data loaded for ref %s", case_ref)
            else:
                error = "Case not found."
                current_app.logger.error("No case found for ref %s", case_ref)
            cursor.close()
            conn.close()
        except Exception as e:
            error = str(e)
            current_app.logger.error("Database error: %s", error)
            return render_template('case_access.html', error=error)

        if not case_data:
            current_app.logger.error("Case data is empty.")
            return render_template('case_access.html', error=error)

        # Extract Patient Info from section "PatientInfo" (case-insensitive).
        ptInfo = None
        for section in case_data.get('sections', []):
            if section.get('name', '').lower() == 'patientinfo':
                ptInfo = section.get('content', {}).get('ptInfo')
                break
        if not ptInfo or not ptInfo.get('dob'):
            error = "Patient date of birth not found in the record."
            current_app.logger.error("Patient DOB missing in case_data sections: %s", case_data.get('sections'))
            return render_template('case_access.html', error=error)
        if dob != ptInfo.get('dob'):
            error = "The provided Date of Birth does not match our records."
            current_app.logger.error("DOB mismatch: provided %s vs record %s", dob, ptInfo.get('dob'))
            return render_template('case_access.html', error=error)
        case_data['ptInfo'] = ptInfo

        # Extract Incident Log from section "Incident Log" (case-insensitive).
        incident_log = None
        for section in case_data.get('sections', []):
            if section.get('name', '').lower() == 'incident log':
                incident_log = section.get('content', {}).get('incident', {})
                break
        if not incident_log:
            incident_log = {}
        case_data['incident'] = {key: (value if value else 'Not Provided') for key, value in incident_log.items()}
        if access_pin != case_data['incident'].get('pinCode', 'N/A'):
            error = "The Case Access Pin is incorrect."
            current_app.logger.error("Access PIN mismatch: provided %s vs record %s", access_pin, case_data['incident'].get('pinCode', 'N/A'))
            return render_template('case_access.html', error=error)

        # Process images (e.g., signatures).
        temp_dir = tempfile.mkdtemp()
        current_app.logger.debug("Temporary directory created: %s", temp_dir)
        process_images(case_data, temp_dir)
        current_app.logger.debug("Image processing complete.")

        return render_template('case_access_pdf.html', case_data=case_data)
    current_app.logger.debug("GET request; rendering case_access.html.")
    return render_template('case_access.html', error=error)

@internal_bp.route('/api/search', methods=['GET'])
def ecpr_search():
    """
    AJAX endpoint for patient search using raw MySQL.
    Expects query parameters: 'date_of_birth' and 'postcode'.
    Allowed roles: "crew", "admin", "superuser", "clinical_lead".
    """
    # allowed_roles = ["crew", "admin", "superuser", "clinical_lead"]
    # if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
    #     return jsonify({"error": "Unauthorised access"}), 403
    dob_str = request.args.get('date_of_birth')
    postcode = request.args.get('postcode')
    if not dob_str or not postcode:
        logger.warning("Crew search missing date_of_birth or postcode.")
        return jsonify({"error": "Missing date_of_birth or postcode"}), 400
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date_of_birth format; please use YYYY-MM-DD"}), 400
    patients = Patient.search_by_dob_and_postcode(dob, postcode)
    if not patients:
        logger.warning("No patient found for DOB: %s and postcode: %s", dob_str, postcode)
        return jsonify({"error": "No matching patient found"}), 404
    # AuditLog.insert_log(current_user.username, "Crew performed patient search", patient_id=patients[0].get("id"))
    print(patients)
    return jsonify({"message": "Patient search successful.", "patients": patients}), 200

from werkzeug.exceptions import HTTPException

# Catch all HTTP errors (404, 403, 500, etc.)
@public_bp.errorhandler(HTTPException)
def handle_http_exception(err):
    current_app.logger.warning(f"Public route HTTP error {err.code}: {err.description}")
    return redirect('/care_company/login')

# Catch any other exception
@public_bp.errorhandler(Exception)
def handle_exception(err):
    current_app.logger.error(f"Public route unexpected error: {err}", exc_info=True)
    return redirect(url_for('/care_company/login'))

# =============================================================================
# Blueprint Registration Functions
# =============================================================================
def get_blueprint():
    return internal_bp

def get_public_blueprint():
    return public_bp
