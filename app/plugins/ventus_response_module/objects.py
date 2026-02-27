import os
import json
from datetime import datetime
from app.objects import get_db_connection
import requests
from geopy.distance import geodesic

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
W3W_API_KEY = os.environ.get("W3W_API_KEY")


class TriageValidationError(Exception):
    pass


class UnitSignOnError(Exception):
    pass


class UnitSignOffError(Exception):
    pass


class JobClaimError(Exception):
    pass


class ResponseTriage:
    # --- Original Geocoding Utilities ---
    @staticmethod
    def get_lat_lng_from_google(address, city="Crawley, UK"):
        if not GOOGLE_MAPS_API_KEY:
            return {"error": "Google Maps API key not configured"}
        formatted_address = f"{address}, {city}"
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={formatted_address}&key={GOOGLE_MAPS_API_KEY}"
        response = requests.get(url)
        data = response.json()
        if data["status"] == "OK":
            location = data["results"][0]["geometry"]["location"]
            return {"lat": location["lat"], "lng": location["lng"]}
        return {"error": "Google Maps could not find address"}

    @staticmethod
    def get_lat_lng_from_osm(address):
        if not address:
            return {"error": "No address provided"}
        url = f"https://nominatim.openstreetmap.org/search?q={address}&format=json&limit=1"
        try:
            response = requests.get(url, timeout=5)
            if response.status_code != 200 or not response.text.strip():
                return {"error": f"OSM API Error {response.status_code}"}
            data = response.json()
            if not data:
                return {"error": "OSM could not find address"}
            return {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"])}
        except Exception as e:
            return {"error": f"OSM lookup failed: {e}"}

    @staticmethod
    def get_lat_lng_from_postcode(postcode):
        if not postcode:
            return {"error": "No postcode provided"}
        url = f"https://api.postcodes.io/postcodes/{postcode}"
        response = requests.get(url)
        data = response.json()
        if data.get("status") == 200 and data.get("result"):
            return {"lat": data["result"]["latitude"], "lng": data["result"]["longitude"]}
        return {"error": "Postcode not found"}

    @staticmethod
    def get_lat_lng_from_w3w(what3words):
        if not W3W_API_KEY or not what3words:
            return {"error": "What3Words API key not configured or no input"}
        url = f"https://api.what3words.com/v3/convert-to-coordinates?words={what3words}&key={W3W_API_KEY}"
        response = requests.get(url)
        data = response.json()
        if "coordinates" in data:
            return {"lat": data["coordinates"]["lat"], "lng": data["coordinates"]["lng"]}
        return {"error": "What3Words location not found"}

    @staticmethod
    def is_within_range(coord1, coord2, max_distance=0.5):
        return geodesic((coord1["lat"], coord1["lng"]), (coord2["lat"], coord2["lng"])).km <= max_distance

    @staticmethod
    def get_best_lat_lng(address=None, postcode=None, what3words=None):
        if what3words:
            w3w_result = ResponseTriage.get_lat_lng_from_w3w(what3words)
            if "lat" in w3w_result:
                return w3w_result
        postcode_result = ResponseTriage.get_lat_lng_from_postcode(
            postcode) if postcode else None
        postcode_coords = postcode_result if postcode_result and "lat" in postcode_result else None
        google_result = ResponseTriage.get_lat_lng_from_google(
            address) if address else None
        google_coords = google_result if google_result and "lat" in google_result else None
        osm_result = ResponseTriage.get_lat_lng_from_osm(
            address) if address else None
        osm_coords = osm_result if osm_result and "lat" in osm_result else None
        if google_coords and postcode_coords:
            if ResponseTriage.is_within_range(google_coords, postcode_coords):
                return google_coords
            else:
                return postcode_coords
        elif osm_coords and postcode_coords:
            if ResponseTriage.is_within_range(osm_coords, postcode_coords):
                return osm_coords
            else:
                return postcode_coords
        elif postcode_coords:
            return postcode_coords
        elif google_coords:
            return google_coords
        elif osm_coords:
            return osm_coords
        return {"error": "No location found"}

    # --- Original Triage & BroadNet Logic ---
    @staticmethod
    def create(**triage_data):
        conn = get_db_connection()
        try:
            best_coordinates = ResponseTriage.get_best_lat_lng(
                address=triage_data.get("address"),
                postcode=triage_data.get("postcode"),
                what3words=triage_data.get("what3words")
            )
            triage_data["coordinates"] = best_coordinates
            query = """
                INSERT INTO response_triage 
                (created_by, vita_record_id, first_name, middle_name, last_name, 
                 patient_dob, phone_number, address, postcode, entry_requirements, reason_for_call, 
                 onset_datetime, patient_alone, exclusion_data, risk_flags, decision, coordinates)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), %s, %s, %s, 
                        CAST(%s AS JSON), CAST(%s AS JSON), %s, CAST(%s AS JSON))
            """
            patient_alone_raw = triage_data.get("patient_alone")
            if isinstance(patient_alone_raw, str):
                p = patient_alone_raw.strip().lower()
                if p in ("yes", "y", "true", "1"):
                    patient_alone_val = 1
                elif p in ("no", "n", "false", "0"):
                    patient_alone_val = 0
                else:
                    patient_alone_val = None
            elif patient_alone_raw in (0, 1, None):
                patient_alone_val = patient_alone_raw
            else:
                patient_alone_val = None
            with conn.cursor() as cursor:
                cursor.execute(query, (
                    triage_data.get("created_by"),
                    triage_data.get("vita_record_id"),
                    triage_data.get("first_name"),
                    triage_data.get("middle_name"),
                    triage_data.get("last_name"),
                    triage_data.get("patient_dob"),
                    triage_data.get("phone_number"),
                    triage_data.get("address"),
                    triage_data.get("postcode"),
                    json.dumps(triage_data.get("entry_requirements") or []),
                    triage_data.get("reason_for_call"),
                    triage_data.get("onset_datetime"),
                    patient_alone_val,
                    json.dumps(triage_data.get("exclusion_data") or {}),
                    json.dumps(triage_data.get("risk_flags") or []),
                    triage_data.get("decision"),
                    json.dumps(triage_data.get("coordinates") or {})
                ))
                conn.commit()
                new_id = cursor.lastrowid
            return new_id
        finally:
            conn.close()

    @staticmethod
    def post_triage_to_broadnet(triage_data):
        # [Your full BroadNet posting logic here, as in your original file]
        # Not omitted for brevity—paste your full method
        pass

    @staticmethod
    def get_by_id(record_id):
        conn = get_db_connection()
        try:
            query = "SELECT * FROM response_triage WHERE id = %s"
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute(query, (record_id,))
                row = cursor.fetchone()
            if row and row.get('selected_conditions'):
                row['selected_conditions'] = json.loads(
                    row['selected_conditions'])
            return row
        finally:
            conn.close()

    @staticmethod
    def get_all():
        conn = get_db_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            query = """
                SELECT id, created_by, created_at, postcode, decision, reason_for_call, exclusion_data
                FROM response_triage
                ORDER BY created_at DESC
            """
            cursor.execute(query)
            rows = cursor.fetchall()
            return rows
        finally:
            conn.close()

    # --- MDT API Methods (New Additions) ---
    @staticmethod
    def health_check():
        return {"status": "ok"}

    @staticmethod
    def sign_on(callsign, timestamp):
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO mdts_signed_on (callSign, signOnTime, status)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE signOnTime = VALUES(signOnTime), status='on_standby'
                """, (callsign, timestamp, 'on_standby'))
                conn.commit()
            return {"message": "Signed on"}
        finally:
            conn.close()

    @staticmethod
    def sign_off(callsign):
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM mdts_signed_on WHERE callSign = %s", (callsign,))
                conn.commit()
            return {"message": "Signed off"}
        finally:
            conn.close()

    @staticmethod
    def get_next_job(callsign):
        conn = get_db_connection()
        try:
            with conn.cursor(dictionary=True) as cur:
                cur.execute("""
                    SELECT cad FROM mdt_jobs
                    WHERE status = 'queued'
                    ORDER BY created_at ASC LIMIT 1
                """)
                job = cur.fetchone()
                return job  # None if no job
        finally:
            conn.close()

    @staticmethod
    def claim_job(cad, callsign):
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS mdt_job_units (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        job_cad INT NOT NULL,
                        callsign VARCHAR(64) NOT NULL,
                        assigned_by VARCHAR(120),
                        assigned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uq_job_callsign (job_cad, callsign),
                        INDEX idx_job_cad (job_cad),
                        INDEX idx_callsign (callsign)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    UPDATE mdt_jobs SET status='claimed', claimedAt=NOW()
                    WHERE cad=%s AND status='queued'
                """, (cad,))
                if cur.rowcount != 1:
                    conn.rollback()
                    raise JobClaimError("Job already claimed or not found")
                cur.execute("""
                    INSERT INTO mdt_job_units (job_cad, callsign, assigned_by)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE assigned_by=VALUES(assigned_by), assigned_at=CURRENT_TIMESTAMP
                """, (cad, callsign, "mdt_claim"))
                cur.execute("""
                    UPDATE mdt_jobs j
                    JOIN (
                      SELECT job_cad, GROUP_CONCAT(callsign ORDER BY assigned_at SEPARATOR ',') AS claimed
                      FROM mdt_job_units
                      WHERE job_cad = %s
                      GROUP BY job_cad
                    ) x ON x.job_cad = j.cad
                    SET j.claimedBy = x.claimed
                    WHERE j.cad = %s
                """, (cad, cad))
                cur.execute("""
                    UPDATE mdts_signed_on SET assignedIncident=%s, status='assigned'
                    WHERE callSign=%s
                """, (cad, callsign))
                conn.commit()
            return {"message": "Job claimed"}
        finally:
            conn.close()

    @staticmethod
    def get_job_details(cad):
        conn = get_db_connection()
        try:
            with conn.cursor(dictionary=True) as cur:
                cur.execute("""
                    SELECT cad, status, triage_data
                    FROM mdt_jobs WHERE cad=%s
                """, (cad,))
                job = cur.fetchone()
                if not job:
                    return None
                if isinstance(job["triage_data"], str):
                    try:
                        job["triage_data"] = json.loads(job["triage_data"])
                    except Exception:
                        pass
                return job
        finally:
            conn.close()

    @staticmethod
    def update_status(cad, callsign, status, time):
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE mdt_jobs SET status=%s, lastStatusTime=%s
                    WHERE cad=%s
                """, (status, time, cad))
                cur.execute("""
                    UPDATE mdts_signed_on SET status=%s
                    WHERE callSign=%s
                """, (status, callsign))
                conn.commit()
            return {"message": "Status updated"}
        finally:
            conn.close()

    @staticmethod
    def update_location(callsign, latitude, longitude, timestamp, status):
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO mdt_locations (callSign, latitude, longitude, timestamp, status)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        latitude=VALUES(latitude),
                        longitude=VALUES(longitude),
                        timestamp=VALUES(timestamp),
                        status=VALUES(status)
                """, (callsign, latitude, longitude, timestamp, status))
                conn.commit()
            return {"message": "Location updated"}
        finally:
            conn.close()

    @staticmethod
    def get_all_locations():
        conn = get_db_connection()
        try:
            with conn.cursor(dictionary=True) as cur:
                cur.execute("""
                    SELECT callSign, latitude, longitude, timestamp, status
                    FROM mdt_locations
                """)
                return cur.fetchall()
        finally:
            conn.close()

    @staticmethod
    def get_unread_message_count(callsign):
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM messages
                    WHERE recipient=%s AND `read`=0
                """, (callsign,))
                row = cur.fetchone()
                return {"count": row[0] if row else 0}
        finally:
            conn.close()

    @staticmethod
    def get_messages(callsign):
        conn = get_db_connection()
        try:
            with conn.cursor(dictionary=True) as cur:
                cur.execute("""
                    SELECT id, `from`, text, timestamp, `read`
                    FROM messages
                    WHERE recipient=%s
                    ORDER BY timestamp ASC
                """, (callsign,))
                return cur.fetchall()
        finally:
            conn.close()

    @staticmethod
    def post_message(callsign, text, sender="mdt"):
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO messages (`from`, recipient, text, timestamp, `read`)
                    VALUES (%s, %s, %s, %s, 0)
                """, (sender, callsign, text, datetime.utcnow()))
                conn.commit()
            return {"message": "Message sent"}
        finally:
            conn.close()

    @staticmethod
    def mark_message_read(callsign, message_id):
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE messages SET `read`=1
                    WHERE id=%s AND recipient=%s
                """, (message_id, callsign))
                conn.commit()
            return {"message": "Message marked as read"}
        finally:
            conn.close()

    @staticmethod
    def get_standby_location(callsign):
        conn = get_db_connection()
        try:
            with conn.cursor(dictionary=True) as cur:
                cur.execute("""
                    SELECT name, lat, lng, isNew
                    FROM standby_locations
                    WHERE callSign=%s
                    ORDER BY updatedAt DESC LIMIT 1
                """, (callsign,))
                row = cur.fetchone()
                if not row:
                    return {"standbyLocation": None, "isNew": False}
                return {
                    "standbyLocation": {
                        "name": row["name"],
                        "lat": row["lat"],
                        "lng": row["lng"]
                    },
                    "isNew": bool(row["isNew"])
                }
        finally:
            conn.close()

    @staticmethod
    def get_history(callsign):
        conn = get_db_connection()
        try:
            with conn.cursor(dictionary=True) as cur:
                cur.execute("SHOW TABLES LIKE 'mdt_job_units'")
                if cur.fetchone() is None:
                    return []
                cur.execute("""
                    SELECT j.cad, j.completedAt, j.chief_complaint, j.outcome
                    FROM mdt_jobs j
                    JOIN mdt_job_units u ON u.job_cad = j.cad
                    WHERE u.callsign=%s AND j.status='cleared'
                    ORDER BY j.completedAt DESC
                """, (callsign,))
                return cur.fetchall()
        finally:
            conn.close()
