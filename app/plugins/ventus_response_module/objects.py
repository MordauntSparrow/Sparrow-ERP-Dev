# medical_records_module/objects.py

import json
from datetime import datetime
from app.objects import get_db_connection
import requests
from geopy.distance import geodesic

# API Keys (Replace with actual keys)
GOOGLE_MAPS_API_KEY = "AIzaSyCAgyalR3yx2HSVaBpUXu4KeJocmg_3MMI"
W3W_API_KEY = "O7Q6HEJO"

class ResponseTriage:
    @staticmethod
    def get_lat_lng_from_google(address, city="Crawley, UK"):
        """Convert address to latitude & longitude using Google Geocoding API."""
        formatted_address = f"{address}, {city}"  # Forces Google to prioritize Crawley
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={formatted_address}&key={GOOGLE_MAPS_API_KEY}"
        response = requests.get(url)
        data = response.json()

        if data["status"] == "OK":
            location = data["results"][0]["geometry"]["location"]
            return {"lat": location["lat"], "lng": location["lng"]}
        
        return {"error": "Google Maps could not find address"}

    @staticmethod
    def get_lat_lng_from_osm(address):
        """Convert address to latitude & longitude using OpenStreetMap (Nominatim API)."""
        if not address:
            return {"error": "No address provided"}

        url = f"https://nominatim.openstreetmap.org/search?q={address}&format=json&limit=1"
        
        try:
            response = requests.get(url, timeout=5)

            if response.status_code != 200:
                print(f"⚠️ OSM API Error: {response.status_code}")
                return {"error": f"OSM API Error {response.status_code}"}

            # 🚨 Check if response is empty BEFORE parsing JSON
            if not response.text.strip():
                print("⚠️ OSM returned an empty response.")
                return {"error": "OSM returned empty response"}

            data = response.json()

            if not data:
                print("⚠️ OSM returned an empty JSON response.")
                return {"error": "OSM could not find address"}

            return {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"])}
    
        except requests.exceptions.RequestException as e:
            print(f"⚠️ OSM Request Failed: {e}")
            return {"error": f"OSM request failed: {str(e)}"}

        except requests.exceptions.JSONDecodeError:
            print("⚠️ OSM Response was not valid JSON (Empty or Invalid).")
            return {"error": "OSM returned invalid JSON"}


    @staticmethod
    def get_lat_lng_from_postcode(postcode):
        """Convert postcode to latitude & longitude using UK Postcodes API."""
        url = f"https://api.postcodes.io/postcodes/{postcode}"
        response = requests.get(url)
        data = response.json()

        if data["status"] == 200:
            return {"lat": data["result"]["latitude"], "lng": data["result"]["longitude"]}
        
        return {"error": "Postcode not found"}

    @staticmethod
    def get_lat_lng_from_w3w(what3words):
        """Convert What3Words to latitude & longitude."""
        url = f"https://api.what3words.com/v3/convert-to-coordinates?words={what3words}&key={W3W_API_KEY}"
        response = requests.get(url)
        data = response.json()

        if "coordinates" in data:
            return {"lat": data["coordinates"]["lat"], "lng": data["coordinates"]["lng"]}
        
        return {"error": "What3Words location not found"}

    @staticmethod
    def is_within_range(coord1, coord2, max_distance=0.5):
        """Check if two coordinates are within a given distance (default 500m)."""
        return geodesic((coord1["lat"], coord1["lng"]), (coord2["lat"], coord2["lng"])).km <= max_distance

    @staticmethod
    def get_best_lat_lng(address=None, postcode=None, what3words=None):
        """Find the best latitude & longitude using multiple methods."""
        
        print(f"DEBUG: Address Input: {address}")
        print(f"DEBUG: Postcode Input: {postcode}")
        print(f"DEBUG: What3Words Input: {what3words}")

        # 1️⃣ Use What3Words if provided
        if what3words:
            w3w_result = ResponseTriage.get_lat_lng_from_w3w(what3words)
            print(f"DEBUG: What3Words Input: {w3w_result}")
            if "lat" in w3w_result:
                print(f"What3Words Location: {w3w_result}")
                return w3w_result

        # 2️⃣ Try Postcode Lookup FIRST
        postcode_result = ResponseTriage.get_lat_lng_from_postcode(postcode) if postcode else None
        if postcode_result and "lat" in postcode_result:
            postcode_coords = postcode_result
            print(f"Postcode Location: {postcode_coords}")
        else:
            postcode_coords = None

        # 3️⃣ Try Google Maps for exact address
        google_result = ResponseTriage.get_lat_lng_from_google(address) if address else None
        if google_result and "lat" in google_result:
            google_coords = google_result
            print(f"Google Address Location: {google_coords}")
        else:
            google_coords = None  

        # 4️⃣ Try OpenStreetMap (Backup for address)
        osm_result = ResponseTriage.get_lat_lng_from_osm(address) if address else None
        if osm_result and "lat" in osm_result:
            osm_coords = osm_result
            print(f"OSM Address Location: {osm_coords}")
        else:
            osm_coords = None

        # 5️⃣ Prioritize Postcode If Address is Too Far Away
        if google_coords and postcode_coords:
            distance = geodesic((google_coords["lat"], google_coords["lng"]),
                                (postcode_coords["lat"], postcode_coords["lng"])).km
            print(f"Distance Between Address & Postcode: {distance} km")

            if ResponseTriage.is_within_range(google_coords, postcode_coords):
                print("✅ Address and Postcode match, trusting Address coordinates.")
                return google_coords
            else:
                print("❌ Address and Postcode do NOT match, using Postcode coordinates.")
                return postcode_coords

        elif osm_coords and postcode_coords:
            distance = geodesic((osm_coords["lat"], osm_coords["lng"]),
                                (postcode_coords["lat"], postcode_coords["lng"])).km
            print(f"Distance Between OSM Address & Postcode: {distance} km")

            if ResponseTriage.is_within_range(osm_coords, postcode_coords):
                print("✅ OSM Address and Postcode match, trusting OSM coordinates.")
                return osm_coords
            else:
                print("❌ OSM Address and Postcode do NOT match, using Postcode coordinates.")
                return postcode_coords

        elif postcode_coords:
            print("⚠️ No address match, using Postcode coordinates.")
            return postcode_coords

        elif google_coords:
            print("⚠️ No postcode match, using Google Address coordinates.")
            return google_coords

        elif osm_coords:
            print("⚠️ No postcode match, using OSM Address coordinates.")
            return osm_coords

        return {"error": "No location found"}  # ❌ Everything failed

    @staticmethod
    def create(**triage_data):
        """Creates a new triage response record and determines coordinates before storing."""
        conn = get_db_connection()
        try:
            # Ensure location coordinates are resolved before storing
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
            with conn.cursor() as cursor:
                cursor.execute(query, (
                    triage_data["created_by"],
                    triage_data["vita_record_id"],
                    triage_data["first_name"],
                    triage_data["middle_name"],
                    triage_data["last_name"],
                    triage_data["patient_dob"],
                    triage_data["phone_number"],
                    triage_data["address"],
                    triage_data["postcode"],
                    json.dumps(triage_data["entry_requirements"]),
                    triage_data["reason_for_call"],
                    triage_data["onset_datetime"],
                    triage_data["patient_alone"],
                    json.dumps(triage_data["exclusion_data"]),
                    json.dumps(triage_data["risk_flags"]),
                    triage_data["decision"],
                    json.dumps(triage_data["coordinates"])
                ))
                conn.commit()
                new_id = cursor.lastrowid
            return new_id
        finally:
            conn.close()

    @staticmethod
    def post_triage_to_broadnet(triage_data):
        """
        Sends triage record to BroadNet.
        """
        # Ensure we have valid coordinates
        coordinates = triage_data.get("coordinates")
        if not coordinates or "lat" not in coordinates or "lng" not in coordinates:
            coordinates = ResponseTriage.get_best_lat_lng(
                address=triage_data.get("address"),
                postcode=triage_data.get("postcode"),
                what3words=triage_data.get("what3words")
            )

        if "lat" not in coordinates or "lng" not in coordinates:
            return {"success": False, "error": "Could not determine location for BroadNet dispatch."}

        print(coordinates)

        BROADNET_API_URL = "https://api-dispatch.broadnet.systems/api_dispatch/v1/organisations/rJV7bbXO8PBJzQxp8yZjLAtt/incidents"
        TEAM_UUID = "26b36159-35f0-427a-b1f4-1cc2a1336b45"  # UC Team UUID
        CHANNEL_UUID = "b0484bce-74f7-40fa-8ed5-75ee07437713"  # UCARE Channel UUID

        exclusion_data = triage_data.get("exclusion_data", "{}")
        risk_flags = triage_data.get("risk_flags", "[]")

        relevant_exclusions = []

        for key, value in exclusion_data.items():
            # Special handling for "exclusion_speech"
            if key == "exclusion_speech":
                # For speech, "no" indicates an exclusion (can't speak in full sentences).
                if value.lower() == "no":
                    relevant_exclusions.append("Exclusion - Full sentences")
            else:
                # For all other exclusion_ keys, "yes" indicates an exclusion.
                if value.lower() == "yes":
                    label = key.replace("exclusion_", "").capitalize()
                    relevant_exclusions.append(f"Exclusion - {label}")

            # Convert triage record to BroadNet's format
            print({
                            "lat": coordinates["lat"],
                            "lng": coordinates["lng"],
                            "label": "Pt Location"
                        })
            incident_payload = {
                "incident": {
                    "description": f"Call for {triage_data['first_name']} {triage_data['last_name']} - {triage_data['reason_for_call']}",
                    "grade": 1,  # Assign priority based on decision
                    "locations": [
                        {
                            "lat": coordinates["lat"],
                            "lng": coordinates["lng"],
                            "label": "Pt Location"
                        },
                    ],
                    "notes": [
                        {"content": f"Patient DOB: {triage_data['patient_dob']}"},
                        {"content": f"Phone: {triage_data['phone_number']}"},
                        {"content": f"Address: {triage_data['address']}, {triage_data['postcode']}"},
                        {"content": f"Entry Requirements: {triage_data['entry_requirements']}"},
                        {"content": f"Onset Datetime: {triage_data['onset_datetime']}"},
                        {"content": f"Patient Alone: {triage_data['patient_alone']}"},
                        {"content": f"Decision: {triage_data['decision']}"},
                        {"content": f"Created At: {datetime.now()}"},
                    ] + [{"content": exclusion} for exclusion in relevant_exclusions]  # Only add exclusions marked "yes"
                    + [{"content": f"Risk Flag: {flag['flag_type']} - {flag['description']} ({flag['timestamp']})"} for flag in risk_flags],

                    "team_uuid": TEAM_UUID,  # Assign to UC team
                    "allocation": {
                        "type": "channel",
                        "channel_uuid": CHANNEL_UUID  # Assign to UCARE channel
                    }
                }
            }
            headers = {
                "Content-Type": "application/json"
            }

            try:
                response = requests.post(BROADNET_API_URL, headers=headers, data=json.dumps(incident_payload))
                response.raise_for_status()  # 🔥 This forces an error on 404 or 500

                print(response.json())
                return {"success": True, "response": response.json()}

            except requests.exceptions.HTTPError as http_err:
                print(f"HTTP error occurred: {http_err}")  # Logs the full error
                return {"success": False, "error": str(http_err)}

            except requests.exceptions.RequestException as req_err:
                print(f"Request error: {req_err}")  # Captures any other errors
                return {"success": False, "error": str(req_err)}

    @staticmethod
    def get_by_id(record_id):
        conn = get_db_connection()
        try:
            query = "SELECT * FROM response_triage WHERE id = %s"
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute(query, (record_id,))
                row = cursor.fetchone()
            if row and row['selected_conditions']:
                import json
                row['selected_conditions'] = json.loads(row['selected_conditions'])
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
