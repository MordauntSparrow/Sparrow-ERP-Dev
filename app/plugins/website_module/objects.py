import os
import json
from datetime import datetime, timedelta
import requests

def ensure_data_folder(module_dir):
    """
    Ensure that a 'data' folder exists inside the given module directory.
    Returns the absolute path to the data folder.
    """
    data_folder = os.path.join(module_dir, 'data')
    if not os.path.exists(data_folder):
        os.makedirs(data_folder)
        print(f"Data folder created at: {data_folder}")
    return data_folder

# -------------------- AnalyticsManager --------------------

class AnalyticsManager:
    """
    Records page view data with detailed metadata.
    Data is stored in a JSON file (analytics.json) within the website module's data folder.
    """
    def __init__(self, data_dir):
        self.data_file = os.path.join(data_dir, "analytics.json")
        if not os.path.exists(self.data_file):
            with open(self.data_file, 'w') as f:
                json.dump({"page_views": []}, f, indent=4)

    def record_page_view(self, page, ip_address, user_agent, referrer=None, extra_fields=None):
        view = {
            "timestamp": datetime.utcnow().isoformat(),
            "page": page,
            "ip": ip_address,
            "user_agent": user_agent,
            "referrer": referrer
        }
        if extra_fields and isinstance(extra_fields, dict):
            view.update(extra_fields)
        data = self._load_data()
        data["page_views"].append(view)
        self._save_data(data)
        print(f"Recorded page view: {view}")

    def _load_data(self):
        with open(self.data_file, 'r') as f:
            return json.load(f)

    def _save_data(self, data):
        with open(self.data_file, 'w') as f:
            json.dump(data, f, indent=4)

    def get_page_views(self):
        return self._load_data().get("page_views", [])

    def get_views_by_hour(self):
        hourly = {str(i): 0 for i in range(24)}
        for view in self.get_page_views():
            dt = datetime.fromisoformat(view["timestamp"])
            hourly[str(dt.hour)] += 1
        return hourly

    def get_views_by_weekday(self):
        weekdays = {str(i): 0 for i in range(7)}
        for view in self.get_page_views():
            dt = datetime.fromisoformat(view["timestamp"])
            weekdays[str(dt.weekday())] += 1
        return weekdays

    def get_country_from_ip(self, ip):
        """
        Uses ip-api.com to retrieve the country for the given IP.
        For local IPs (e.g. 127.*, 192.*, 10.*) returns "United Kingdom" for testing.
        """
        if ip.startswith("127.") or ip.startswith("192.") or ip.startswith("10."):
            return "United Kingdom"
        try:
            response = requests.get(f"http://ip-api.com/json/{ip}?fields=country", timeout=2)
            if response.status_code == 200:
                data = response.json()
                return data.get("country", "Unknown")
            else:
                print(f"GeoIP API error for IP {ip}: status code {response.status_code}")
                return "Unknown"
        except Exception as e:
            print(f"GeoIP API lookup error for IP {ip}: {e}")
            return "Unknown"

    def get_requests_by_country(self):
        counts = {}
        for view in self.get_page_views():
            ip = view.get("ip", "")
            country = self.get_country_from_ip(ip)
            counts[country] = counts.get(country, 0) + 1
        return counts

    def get_popular_pages(self, period="alltime"):
        views = self.get_page_views()
        now = datetime.utcnow()
        if period == "today":
            start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "weekly":
            start_time = now - timedelta(days=now.weekday())
            start_time = start_time.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "monthly":
            start_time = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "year":
            start_time = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            start_time = None

        counts = {}
        for view in views:
            ts = datetime.fromisoformat(view["timestamp"])
            if start_time is None or ts >= start_time:
                page = view["page"]
                counts[page] = counts.get(page, 0) + 1
        popular = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return popular

# -------------------- ContactFormConfigManager --------------------

class ContactFormConfigManager:
    """
    Manages contact form configuration.
    Reads and writes settings to a JSON file (contact_form_config.json) in the website module's data folder.
    Each configuration maps a form identifier to its settings (e.g., recipient and subject).
    """
    def __init__(self, module_dir):
        self.data_folder = ensure_data_folder(module_dir)
        self.config_file = os.path.join(self.data_folder, 'contact_form_config.json')
        self.config = self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading contact form configuration: {e}")
        empty_config = {}
        self.save_config(empty_config)
        return empty_config

    def save_config(self, config):
        try:
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=4)
            self.config = config
        except Exception as e:
            print(f"Error saving contact form configuration: {e}")

    def update_configuration(self, form_id, recipient, subject):
        self.config[form_id] = {"recipient": recipient, "subject": subject}
        self.save_config(self.config)
        print(f"Updated configuration for form '{form_id}'.")

    def get_configuration(self):
        return self.config

# -------------------- ContactFormSubmissionManager --------------------

class ContactFormSubmissionManager:
    """
    Manages contact form submissions by:
      1. Recording all submitted data into a JSON file.
      2. Processing the submission according to the contact form configuration.
         The submission is only processed if a configuration exists for the submitted form_id
         (comparison is case-insensitive). If the Sales module is installed (via the core manifest),
         the submission is forwarded; otherwise, it is emailed using the EmailManager from the core module.
    """
    def __init__(self, data_dir):
        self.submissions_file = os.path.join(data_dir, "contact_submissions.json")
        if not os.path.exists(self.submissions_file):
            with open(self.submissions_file, 'w') as f:
                json.dump([], f, indent=4)

    def record_submission(self, submission_data):
        submissions = self._load_submissions()
        submissions.append(submission_data)
        self._save_submissions(submissions)
        print(f"Recorded contact form submission: {submission_data}")

    def _load_submissions(self):
        with open(self.submissions_file, 'r') as f:
            return json.load(f)

    def _save_submissions(self, submissions):
        with open(self.submissions_file, 'w') as f:
            json.dump(submissions, f, indent=4)

    def process_submission(self, submission_data):
        """
        Processes the submission by:
          - Recording the submission.
          - Loading the contact form configuration.
          - Converting the submitted form_id to lowercase and checking if a configuration exists.
          - If no matching configuration is found or required fields are missing, the submission is not processed.
          - Otherwise, if the Sales module is installed (per core manifest), the submission is forwarded;
            else, it is emailed using the EmailManager (which now loads its config automatically).
        """
        self.record_submission(submission_data)
        
        from .objects import ContactFormConfigManager  # Using website module's objects
        module_dir = os.path.dirname(os.path.abspath(__file__))
        config_manager = ContactFormConfigManager(module_dir)
        current_config = config_manager.get_configuration()
        
        form_id_submitted = submission_data.get("form_id", "").strip().lower()
        if not form_id_submitted:
            print("No form_id provided. Submission not processed.")
            return False
        
        config_lower = { key.lower(): value for key, value in current_config.items() }
        if form_id_submitted not in config_lower:
            print(f"Configuration for form '{form_id_submitted}' not defined. Submission not processed.")
            return False
        
        matched_config = config_lower[form_id_submitted]
        email_recipient = matched_config.get("recipient", "").strip().lower()
        email_subject = matched_config.get("subject", "").strip().lower()
        
        if not email_recipient or not email_subject:
            print("Form configuration missing required fields. Submission not processed.")
            return False
        
        from ...objects import PluginManager  # From core module
        plugin_manager = PluginManager(os.path.abspath('app/plugins'))
        core_manifest = plugin_manager.get_core_manifest()
        
        sales_installed = False
        if "sales" in core_manifest and core_manifest["sales"].get("enabled", False):
            sales_installed = True
        
        if sales_installed:
            print("Forwarding submission to Sales module:", submission_data)
            # Insert your Sales module integration logic here.
        else:
            try:
                # Now, simply instantiate EmailManager with no parameters.
                from ...objects import EmailManager
                email_manager = EmailManager()  # Automatically loads SMTP config from default JSON file.
                email_body = "New contact form submission:\n\n" + json.dumps(submission_data, indent=4)
                email_manager.send_email(email_subject, email_body, [email_recipient])
                print("Email sent to", email_recipient)
            except Exception as e:
                print("Error sending email:", e)
                return False
        return True

# -------------------- SpamProtection --------------------

class SpamProtection:
    """
    Centralized spam protection for contact forms.
    Checks the honeypot field (named "website") and, if Turnstile keys are configured in the core manifest,
    verifies the Turnstile token via Cloudflare Turnstile.
    """
    def __init__(self, config):
        self.turnstile_site_key = config.get("turnstile", {}).get("site_key", "").strip()
        self.turnstile_secret_key = config.get("turnstile", {}).get("secret_key", "").strip()
    
    def is_spam(self, form):
        if form.get("website", "").strip():
            return True, "Honeypot field filled"
        if self.turnstile_site_key and self.turnstile_secret_key:
            token = form.get("cf-turnstile-response", "").strip()
            if not token:
                return False, "Turnstile token missing"
            try:
                url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
                payload = {
                    "secret": self.turnstile_secret_key,
                    "response": token,
                    "remoteip": form.get("remote_ip", "")
                }
                r = requests.post(url, data=payload, timeout=5)
                result = r.json()
                if not result.get("success", False):
                    return True, "Turnstile verification failed"
            except Exception as e:
                return True, f"Turnstile verification error: {e}"
        return False, ""
