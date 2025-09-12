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
from app.objects import PluginManager, AuthManager, User, get_db_connection  # Adjust as needed
from .objects import ResponseTriage
import logging

logger = logging.getLogger('ventus_response_module')
logger.setLevel(logging.INFO)

# In-memory storage for one-time admin PINs.
admin_pin_store = {}  # Example: {"pin": "123456", "expires_at": datetime_object, "generated_by": "ClinicalLeadUser"}

def calculate_age(born, today=None):
    if today is None:
        today = datetime.utcnow().date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


# For audit logging, use our raw method.
def log_audit(user, action, patient_id=None):
    logger.info("Audit log created: user=%s, action=%s", user, action)


# Instantiate PluginManager and load core manifest.
plugin_manager = PluginManager(os.path.abspath('app/plugins'))
core_manifest = plugin_manager.get_core_manifest()

# =============================================================================
# INTERNAL BLUEPRINT (for admin side)
# =============================================================================
internal_template_folder = os.path.join(os.path.dirname(__file__), 'templates')
internal = Blueprint(
    'medical_response_internal',
    __name__,
    url_prefix='/plugin/ventus_response_module',
    template_folder=internal_template_folder
)

@internal.route('/')
def landing():
    """Landing page (router) for Medical response Module."""
    return render_template("response_routing.html", config=core_manifest)

@internal.route('/response', methods=['GET'])
@login_required
def response_dashboard():
    allowed_roles = ["crew", "admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({"error": "Unauthorised access"}), 403
    return render_template("response/dashboard.html", config=core_manifest)

@internal.route('/response/triage', methods=['GET', 'POST'])
@login_required
def triage_form():
    allowed_roles = ["crew", "admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        return jsonify({"error": "Unauthorized access"}), 403

    if request.method == 'POST':
        # Vita record ID
        vita_record_str = request.form.get('vita_record_id', '')
        vita_record_id = int(vita_record_str) if vita_record_str.isdigit() else None

        # Patient details
        first_name = request.form.get('first_name', '')
        middle_name = request.form.get('middle_name', '')
        last_name = request.form.get('last_name', '')
        dob_str = request.form.get('patient_dob', '')
        phone_number = request.form.get('phone_number', '')
        address = request.form.get('address', '')
        postcode = request.form.get('postcode', '')
        what3words = request.form.get('what3words', '')

        access_requirements_str = request.form.get('access_requirements', '')
        try:
            access_requirements = json.loads(access_requirements_str) if access_requirements_str.strip() else []
        except Exception:
            access_requirements = []

        # Triage information
        reason_for_call = request.form.get('reason_for_call', '')
        onset_datetime = request.form.get('onset_datetime', '')
        patient_alone = request.form.get('patient_alone', '')

        decision = request.form.get('decision', 'PENDING')

        risk_flags_str = request.form.get('risk_flags', '')
        try:
            risk_flags = json.loads(risk_flags_str) if risk_flags_str.strip() else []
        except Exception:
            risk_flags = []

        # Convert date of birth
        patient_dob = None
        if dob_str:
            try:
                patient_dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid date format for Date of Birth.", "danger")
                return render_template("response/triage_form.html", config=core_manifest)

        # Gather exclusion responses into a dictionary
        exclusion_data = {key: request.form.get(key) for key in request.form if key.startswith("exclusion_")}

        # 🌍 **NEW**: Ignore frontend lat/lng & determine location in backend
        best_coordinates = ResponseTriage.get_best_lat_lng(
            address=address,
            postcode=postcode,
            what3words=what3words
        )

        if "error" in best_coordinates:
            flash("Warning: Unable to determine exact location. Defaulting to postcode if available.", "warning")

        # 1) Save the triage record
        new_id = ResponseTriage.create(
            created_by=current_user.username,
            vita_record_id=vita_record_id,
            first_name=first_name,
            middle_name=middle_name,
            last_name=last_name,
            patient_dob=patient_dob,
            phone_number=phone_number,
            address=address,
            postcode=postcode,
            entry_requirements=access_requirements,
            reason_for_call=reason_for_call,
            onset_datetime=onset_datetime,
            patient_alone=patient_alone,
            exclusion_data=exclusion_data,
            risk_flags=risk_flags,
            decision=decision,
            coordinates=best_coordinates
        )

        # 2) Build payload for BroadNet & MDT
        triage_data = {
            "cad": new_id,
            "vita_record_id": vita_record_id,
            "first_name": first_name,
            "middle_name": middle_name,
            "last_name": last_name,
            "patient_dob": patient_dob,
            "phone_number": phone_number,
            "address": address,
            "postcode": postcode,
            "what3words": what3words,
            "entry_requirements": access_requirements,
            "reason_for_call": reason_for_call,
            "onset_datetime": onset_datetime,
            "patient_alone": patient_alone,
            "exclusion_data": exclusion_data,
            "risk_flags": risk_flags,
            "decision": decision,
            "coordinates": best_coordinates
        }

        # 3) Send to BroadNet
        # ResponseTriage.post_triage_to_broadnet(triage_data)

        # 4) **ENQUEUE** into internal MDT queue
        conn = get_db_connection()
        cur  = conn.cursor()
        try:
            cur.execute("""
              INSERT INTO mdt_jobs (cad, created_by, status, data)
              VALUES (%s, %s, 'queued', %s)
            """, (
              new_id,
              current_user.username,
              json.dumps(triage_data, default=str)
            ))
            conn.commit()
        except Exception as e:
            conn.rollback()
            flash(f"Warning: MDT enqueue failed: {e}", "warning")
        finally:
            cur.close()
            conn.close()

        flash("Triage form submitted successfully!", "success")
        return redirect(url_for('medical_response_internal.triage_list'))
    
    return render_template("response/triage_form.html", config=core_manifest)


@internal.route('/response/list')
@login_required
def triage_list():
    # Get all triage responses from the ResponseTriage class
    triage_list = ResponseTriage.get_all()
    return render_template("response/triage_list.html", triage_list=triage_list, config=core_manifest)

# -----------------------
# ADMIN ROUTES
# -----------------------
@internal.route('/admin', methods=['GET'])
@login_required
def admin_dashboard():
    allowed_roles = ["admin", "superuser", "clinical_lead"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        flash("Unauthorised access", "danger")
        return redirect(url_for('medical_response_internal.landing'))

    return render_template("admin/dashboard.html", config=core_manifest)


# =============================================================================
# CLINICAL ROUTES
# =============================================================================
@internal.route('/clinical', methods=['GET', 'POST'])
@login_required
def clinical_dashboard():
    allowed_roles = ["clinical_lead", "superuser"]
    if not hasattr(current_user, 'role') or current_user.role.lower() not in allowed_roles:
        flash("Unauthorised access", "danger")
        return redirect(url_for('medical_response_internal.landing'))

    return render_template("clinical/dashboard.html", config=core_manifest)


# Add CORS headers to all responses
@internal.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    return response

# =============================================================================
# MDT ROUTES
# =============================================================================

# Helper to normalize callsign
def _normalize_callsign(payload=None, args=None):
    """
    Accept either 'callSign' or 'callsign' from JSON body or query params.
    """
    cs = None
    if payload:
        cs = payload.get('callSign') or payload.get('callsign')
    if not cs and args:
        cs = args.get('callSign') or args.get('callsign')
    return cs or ''

# 1) Sign-On
@internal.route('/api/mdt/signOn', methods=['POST', 'OPTIONS'])
def mdt_sign_on():
    if request.method == 'OPTIONS':
        return '', 200

    payload = request.get_json() or {}
    callsign = payload.get('callSign') or payload.get('callsign')
    crew_raw = payload.get('crew')  # may be str or list
    status = payload.get('status', 'on_station')

    # normalize crew into a list
    crew = []
    if isinstance(crew_raw, str):
        crew = [crew_raw]
    elif isinstance(crew_raw, list):
        crew = crew_raw

    if not callsign or not crew:
        return jsonify({'error': 'callSign (or callsign) and crew required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO mdts_signed_on
              (callSign, ipAddress, status, crew)
            VALUES
              (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              status           = VALUES(status),
              crew             = VALUES(crew),
              signOnTime       = CURRENT_TIMESTAMP,
              assignedIncident = NULL
            """,
            (
                callsign,
                request.headers.get('X-Forwarded-For', request.remote_addr or ''),
                status,
                json.dumps(crew)
            )
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

    return jsonify({'message': 'Signed on'}), 200

# 2) Sign-Off
@internal.route('/api/mdt/signOff', methods=['POST', 'OPTIONS'])
def mdt_sign_off():
    if request.method == 'OPTIONS':
        return '', 200

    payload = request.get_json() or {}
    callsign = payload.get('callSign') or payload.get('callsign')
    if not callsign:
        return jsonify({'error': 'callSign required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM mdts_signed_on WHERE callSign = %s", (callsign,)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

    return jsonify({'message': 'Signed off'}), 200

# 3) Next job
@internal.route('/api/mdt/next', methods=['GET', 'OPTIONS'])
def mdt_next():
    if request.method == 'OPTIONS':
        return '', 200

    callsign = _normalize_callsign(args=request.args)
    if not callsign:
        return '', 204

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        # Return existing assignment if present
        cur.execute(
            "SELECT assignedIncident FROM mdts_signed_on WHERE callSign = %s AND assignedIncident IS NOT NULL ORDER BY signOnTime DESC LIMIT 1",
            (callsign,)
        )
        row = cur.fetchone()
        if row and row.get('assignedIncident'):
            return jsonify({'cad': row['assignedIncident']}), 200

        # Otherwise pull next queued job
        cur.execute(
            "SELECT cad FROM mdt_jobs WHERE status = 'queued' ORDER BY cad ASC LIMIT 1"
        )
        job = cur.fetchone()
        if not job:
            return '', 204
        return jsonify({'cad': job['cad']}), 200
    finally:
        cur.close()
        conn.close()

# 4) Claim
@internal.route('/api/mdt/<int:cad>/claim', methods=['POST', 'OPTIONS'])
def mdt_claim(cad):
    if request.method == 'OPTIONS':
        return '', 200

    # Only read callsign from query parameters
    callsign = (request.args.get('callSign') or request.args.get('callsign') or '').strip()
    if not callsign:
        return jsonify({'error': 'callSign is required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Atomically claim job if still queued
        cur.execute(
            """
            UPDATE mdt_jobs
               SET status = 'claimed', claimedBy = %s
             WHERE cad = %s AND status = 'queued'
            """,
            (callsign, cad)
        )
        if cur.rowcount != 1:
            conn.rollback()
            return jsonify({'error': 'Job already claimed or not found'}), 409

        # Assign to callsign and update live status
        cur.execute(
            """
            UPDATE mdts_signed_on
               SET assignedIncident = %s,
                   status           = 'assigned'
             WHERE callSign = %s
            """,
            (cad, callsign)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

    return jsonify({'message': 'Job claimed'}), 200


# 5) History
@internal.route('/api/mdt/history', methods=['GET', 'POST', 'OPTIONS'])
def mdt_history():
    if request.method == 'OPTIONS':
        return '', 200

    if request.method == 'GET':
        callsign = _normalize_callsign(args=request.args)
        cads = request.args.getlist('cad', type=int)
    else:
        body = request.get_json() or {}
        callsign = body.get('callSign') or body.get('callsign') or ''
        cads = body.get('cads', [])

    if not callsign:
        return jsonify({'error': 'callSign required'}), 400
    if not cads:
        return jsonify([]), 200

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        placeholders = ",".join(["%s"] * len(cads))
        sql = f"""
          SELECT
            cad,
            MAX(CASE WHEN status='received'    THEN event_time END) AS received_time,
            MAX(CASE WHEN status='mobile'      THEN event_time END) AS mobile_time,
            MAX(CASE WHEN status='on_scene'    THEN event_time END) AS on_scene_time,
            MAX(CASE WHEN status='leave_scene' THEN event_time END) AS leave_scene_time,
            MAX(CASE WHEN status='at_hospital' THEN event_time END) AS at_hospital_time,
            MAX(CASE WHEN status='cleared'     THEN event_time END) AS cleared_time
          FROM mdt_response_log
          WHERE callSign = %s
            AND cad IN ({placeholders})
          GROUP BY cad
          ORDER BY received_time DESC
        """
        cur.execute(sql, [callsign] + cads)
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    return jsonify(rows), 200

# 6) Details
@internal.route('/api/mdt/<int:cad>', methods=['GET', 'OPTIONS'])
def mdt_details(cad):
    if request.method == 'OPTIONS':
        return '', 200

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT status, data FROM mdt_jobs WHERE cad = %s",
        (cad,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({'error': 'Not found'}), 404

    return jsonify({
        'cad': cad,
        'status': row['status'],
        'triage_data': row['data']
    }), 200

# 7) Status update (includes clear & stand-down)
@internal.route('/api/mdt/<int:cad>/status', methods=['POST', 'OPTIONS'])
def mdt_status(cad):
    if request.method == 'OPTIONS':
        return '', 200

    payload = request.get_json() or {}
    status = payload.get('status')
    callsign = payload.get('callSign') or payload.get('callsign') or ''

    valid = {
        'received', 'assigned', 'mobile', 'on_scene',
        'leave_scene', 'at_hospital', 'cleared', 'stood_down'
    }
    if status not in valid:
        return jsonify({'error': 'Invalid status'}), 400
    if not callsign:
        return jsonify({'error': 'callSign required'}), 400

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        # fetch current crew JSON
        cur.execute(
            "SELECT crew FROM mdts_signed_on WHERE callSign = %s ORDER BY signOnTime DESC LIMIT 1",
            (callsign,)
        )
        row = cur.fetchone()
        crew_json = row['crew'] if row and row.get('crew') else '[]'

        # update mdt_jobs status
        cur.execute(
            "UPDATE mdt_jobs SET status = %s WHERE cad = %s",
            (status, cad)
        )

        # log status change
        cur.execute(
            "INSERT INTO mdt_response_log (callSign, cad, status, event_time, crew) VALUES (%s, %s, %s, NOW(), %s)",
            (callsign, cad, status, crew_json)
        )

        # update live status & clear assignment if done
        cur.execute(
            """UPDATE mdts_signed_on
             SET status = %s,
                 assignedIncident = CASE WHEN %s IN ('cleared','stood_down') THEN NULL ELSE assignedIncident END
             WHERE callSign = %s""",
            (status, status, callsign)
        )

        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

    return jsonify({'message': 'Status updated and logged'}), 200

# 8) Location update (real-time position reporting)
@internal.route('/api/mdt/location', methods=['POST', 'OPTIONS'])
def mdt_update_location():
    if request.method == 'OPTIONS':
        return '', 200

    payload = request.get_json() or {}
    callsign = payload.get('callSign') or payload.get('callsign') or ''
    latitude = payload.get('latitude')
    longitude = payload.get('longitude')

    if not callsign or latitude is None or longitude is None:
        return jsonify({'error': 'callSign, latitude and longitude required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Update live location in signed-on table
        cur.execute(
            """
             UPDATE mdts_signed_on
               SET lastLat    = %s,
                   lastLon    = %s,
                   lastSeenAt = NOW()
             WHERE callSign = %s
            """,
            (latitude, longitude, callsign)
        )
        # Archive position in history table
        cur.execute(
            "INSERT INTO mdt_positions (callSign, latitude, longitude, recorded_at) VALUES (%s, %s, %s, NOW())",
            (callsign, latitude, longitude)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

    return jsonify({'message': 'Location updated'}), 200


# =============================================================================
# PUBLIC BLUEPRINT
# =============================================================================
public_template_folder = os.path.join(os.path.dirname(__file__), 'templates', 'public')
public = Blueprint(
    'ventus_response',
    __name__,
    url_prefix='/ventus',
    template_folder=public_template_folder
)




@public.before_request
def ensure_ventus_response_portal_user():
    # If the user isn't authenticated yet, let the login_required decorator handle it.
    if not current_user.is_authenticated:
        return
    # Now that the user is authenticated, ensure they are from the Vita-Care-Portal module.
    if not hasattr(current_user, 'role') or current_user.role != "Ventus-Response-Portal":
        return jsonify({"error": "Unauthorised access"}), 403

# =============================================================================
# Blueprint Registration Functions
# =============================================================================
def get_blueprint():
    return internal

def get_public_blueprint():
    return public
