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

import os
import json
import requests
from datetime import datetime, timedelta


class AnalyticsManager:
    """
    Records page view data with detailed metadata.
    Data is stored in a JSON file (analytics.json) within the website module's data folder.
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.data_file = os.path.join(data_dir, "analytics.json")
        self.geo_cache_file = os.path.join(data_dir, "geo_cache.json")

        if not os.path.exists(self.data_file):
            with open(self.data_file, 'w') as f:
                json.dump({"page_views": []}, f, indent=4)

        # lazy-load geo cache
        if not os.path.exists(self.geo_cache_file):
            with open(self.geo_cache_file, 'w') as f:
                json.dump({}, f)

    # -----------------------
    # Recording and I/O
    # -----------------------
    def record_page_view(self, page, ip_address, user_agent, referrer=None, extra_fields=None):
        view = {
            "timestamp": datetime.utcnow().isoformat(),
            "page": page,
            "ip": ip_address,
            "user_agent": user_agent,
            "referrer": referrer,
        }
        if extra_fields and isinstance(extra_fields, dict):
            view.update(extra_fields)

        data = self._load_data()
        data["page_views"].append(view)
        self._save_data(data)

    def _load_data(self):
        with open(self.data_file, 'r') as f:
            return json.load(f)

    def _save_data(self, data):
        with open(self.data_file, 'w') as f:
            json.dump(data, f, indent=4)

    # -----------------------
    # Time range helpers
    # -----------------------
    def get_timerange(self, period):
        """
        Returns (start, end) in UTC for the current period.
        end is exclusive (<= end is false; < end is true).
        """
        now = datetime.utcnow()

        if period == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "weekly":
            # last 7 days window
            start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "monthly":
            # last 30 days window
            start = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "year":
            # last 12 months window (365 days rolling)
            start = (now - timedelta(days=364)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            # all time
            return None, None

        end = now
        return start, end

    def get_previous_timerange(self, period):
        """
        Returns the range immediately preceding the current get_timerange(period).
        Mirrors the same window length.
        """
        start, end = self.get_timerange(period)
        if start is None and end is None:
            return None, None

        delta = end - start
        prev_end = start
        prev_start = prev_end - delta
        return prev_start, prev_end

    # -----------------------
    # Filtering utilities
    # -----------------------
    def _iter_views(self, time_range=None):
        """
        Yields views optionally filtered within [start, end) UTC.
        """
        views = self.get_page_views()
        if not time_range or time_range == (None, None):
            yield from views
            return

        start, end = time_range
        for v in views:
            ts = self._parse_ts(v.get("timestamp"))
            if (start is None or ts >= start) and (end is None or ts < end):
                yield v

    def _parse_ts(self, ts_str):
        """Handle potential microseconds or no-T suffix."""
        try:
            return datetime.fromisoformat(ts_str)
        except Exception:
            try:
                return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S.%f")
            except Exception:
                return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")

    # -----------------------
    # Public getters (range-aware)
    # -----------------------
    def get_page_views(self):
        return self._load_data().get("page_views", [])

    def get_views_by_hour(self, time_range=None):
        """Return a dict of page views grouped by hour (0–23)."""
        hourly = {i: 0 for i in range(24)}
        for view in self._iter_views(time_range):
            dt = self._parse_ts(view["timestamp"])
            hourly[dt.hour] += 1
        return hourly

    def get_views_by_weekday(self, time_range=None):
        """Return a dict of page views grouped by weekday (0=Mon..6=Sun)."""
        weekdays = {i: 0 for i in range(7)}
        for view in self._iter_views(time_range):
            dt = self._parse_ts(view["timestamp"])
            weekdays[dt.weekday()] += 1
        return weekdays

    def get_requests_by_country(self, time_range=None):
        counts = {}
        for view in self._iter_views(time_range):
            ip = view.get("ip", "")
            country = self.get_country_from_ip(ip)
            counts[country] = counts.get(country, 0) + 1
        return counts

    def get_popular_pages(self, period="alltime", time_range=None):
        """
        Backwards compatible:
        - If time_range provided, uses it.
        - Else if period provided, derives current range for known periods.
        - Else returns all time.

        Returns list of tuples [(page, count), ...] sorted desc.
        """
        if time_range is None and period != "alltime":
            time_range = self.get_timerange(period)

        counts = {}
        for view in self._iter_views(time_range):
            page = view.get("page")
            counts[page] = counts.get(page, 0) + 1

        return sorted(counts.items(), key=lambda x: x[1], reverse=True)

    # -----------------------
    # Geo lookup with caching
    # -----------------------
    def _load_geo_cache(self):
        try:
            with open(self.geo_cache_file, 'r') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_geo_cache(self, cache):
        with open(self.geo_cache_file, 'w') as f:
            json.dump(cache, f)

    def get_country_from_ip(self, ip):
        """
        Uses ip-api.com to retrieve the country for the given IP.
        For local IPs, returns "United Kingdom" for testing.
        Adds a simple cache to avoid repeated HTTP calls.
        """
        if not ip:
            return "Unknown"

        if ip.startswith(("127.", "192.", "10.")):
            return "United Kingdom"

        cache = self._load_geo_cache()
        if ip in cache:
            return cache[ip]

        try:
            resp = requests.get(f"http://ip-api.com/json/{ip}?fields=country", timeout=2)
            if resp.status_code == 200:
                country = resp.json().get("country", "Unknown") or "Unknown"
            else:
                country = "Unknown"
        except Exception:
            country = "Unknown"

        cache[ip] = country
        self._save_geo_cache(cache)
        return country

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

import os
import json
import uuid
from datetime import datetime
from flask import render_template

# ------------------------
# Data folder helper
# ------------------------
def ensure_data_folder(module_dir: str) -> str:
    """
    Ensures a writable data directory under this module.
    Returns the absolute path, e.g., <module>/data
    """
    data_dir = os.path.join(module_dir, 'data')
    os.makedirs(data_dir, exist_ok=True)
    return data_dir

# ------------------------
# Page Store (JSON)
# ------------------------
class BuilderPageStore:
    """
    JSON file store for builder pages: data/pages/<page_id>.json
    """
    def __init__(self, data_dir: str):
        self.pages_dir = os.path.join(data_dir, 'pages')
        os.makedirs(self.pages_dir, exist_ok=True)

    def _path(self, page_id: str) -> str:
        return os.path.join(self.pages_dir, f"{page_id}.json")

    def load(self, page_id: str) -> dict | None:
        p = self._path(page_id)
        if not os.path.exists(p):
            return None
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)

    def save(self, page_json: dict) -> None:
        if 'id' not in page_json:
            raise ValueError("page_json missing id")
        page_json.setdefault('updated_at', datetime.utcnow().isoformat())
        with open(self._path(page_json['id']), 'w', encoding='utf-8') as f:
            json.dump(page_json, f, indent=2, ensure_ascii=False)

    def create(self, title: str, route: str) -> dict:
        page_id = str(uuid.uuid4())[:8]
        page = {
            'id': page_id,
            'title': title,
            'route': route,
            'seo': {'title': title, 'description': ''},
            'vars': {},
            'blocks': [],
            'draft': True,
            'version': 1,
            'updated_at': datetime.utcnow().isoformat(),
        }
        self.save(page)
        return page

# ------------------------
# Blocks Registry (with layout primitives)
# ------------------------
class BlocksRegistry:
    """
    Registry: block metadata, defaults, schema, templates, and layout primitives.
    safe_registry() strips server-only fields.
    """
    def __init__(self):
        self._blocks = {
            # Content blocks
            'hero': {
                'label': 'Hero',
                'icon': 'bi-lightning',
                'defaults': {'headline': 'Headline', 'sub': 'Subheadline', 'cta_text': 'Get Started', 'bg': 'light'},
                'schema': [
                    {'name': 'headline', 'type': 'text', 'label': 'Headline', 'required': True},
                    {'name': 'sub', 'type': 'text', 'label': 'Subheadline'},
                    {'name': 'cta_text', 'type': 'text', 'label': 'CTA Text'},
                    {'name': 'bg', 'type': 'select', 'label': 'Background', 'options': ['light', 'dark', 'image']},
                ],
                'template': 'blocks/hero.html',
            },
            'text': {
                'label': 'Text',
                'icon': 'bi-fonts',
                'defaults': {'html': '<p>Write something…</p>'},
                'schema': [{'name': 'html', 'type': 'richtext', 'label': 'Content', 'required': True}],
                'template': 'blocks/text.html',
            },
            'image': {
                'label': 'Image',
                'icon': 'bi-image',
                'defaults': {'src': '', 'alt': '', 'rounded': True},
                'schema': [
                    {'name': 'src', 'type': 'text', 'label': 'Image URL', 'required': True},
                    {'name': 'alt', 'type': 'text', 'label': 'Alt text'},
                    {'name': 'rounded', 'type': 'switch', 'label': 'Rounded corners'},
                ],
                'template': 'blocks/image.html',
            },
            # Layout primitives (nested)
            'section': {
                'label': 'Section',
                'icon': 'bi-layout-text-sidebar',
                'defaults': {'blocks': [], 'bg': 'light'},
                'schema': [
                    {'name': 'bg', 'type': 'select', 'label': 'Background', 'options': ['light', 'dark', 'image']},
                    {'name': 'blocks', 'type': 'blocks', 'label': 'Nested blocks'},
                ],
                'template': 'blocks/section.html',
            },
            'columns': {
                'label': 'Columns',
                'icon': 'bi-columns',
                'defaults': {'columns': [[], []]},
                'schema': [{'name': 'columns', 'type': 'columns', 'label': 'Column content', 'required': True}],
                'template': 'blocks/columns.html',
            },
            'button': {
                'label': 'Button',
                'icon': 'bi-hand-index',
                'defaults': {'text': 'Click Me', 'href': '#', 'style': 'primary'},
                'schema': [
                    {'name': 'text', 'type': 'text', 'label': 'Button text', 'required': True},
                    {'name': 'href', 'type': 'text', 'label': 'Link', 'required': True},
                    {'name': 'style', 'type': 'select', 'label': 'Style',
                     'options': ['primary', 'secondary', 'success', 'info', 'warning', 'danger', 'link']},
                ],
                'template': 'blocks/button.html',
            },
            'spacer': {
                'label': 'Spacer',
                'icon': 'bi-arrows-expand',
                'defaults': {'height': 20},
                'schema': [{'name': 'height', 'type': 'number', 'label': 'Height (px)', 'required': True}],
                'template': 'blocks/spacer.html',
            },
        }

    # --- Introspection helpers ---
    def exists(self, btype: str) -> bool:
        return btype in self._blocks

    def defaults(self, btype: str) -> dict:
        return self._blocks[btype]['defaults']

    def template(self, btype: str) -> str:
        return self._blocks[btype]['template']

    def schema(self, btype: str) -> list:
        return self._blocks[btype]['schema']

    # --- Normalization / validation ---
    def normalize(self, btype: str, props: dict | None) -> dict:
        base = dict(self._blocks[btype]['defaults'])
        props = dict(props or {})

        # Drop unknown keys early
        for k in list(props.keys()):
            if k not in base:
                props.pop(k, None)

        # Nested structures
        if btype == 'section':
            base['blocks'] = []
            for blk in props.get('blocks', []):
                if isinstance(blk, dict) and self.exists(blk.get('type')):
                    child_props = self.normalize(blk['type'], blk.get('props'))
                    base['blocks'].append({'type': blk['type'], 'props': child_props})
        elif btype == 'columns':
            base['columns'] = []
            for col in props.get('columns', []):
                cleaned_col = []
                for blk in col if isinstance(col, list) else []:
                    if isinstance(blk, dict) and self.exists(blk.get('type')):
                        child_props = self.normalize(blk['type'], blk.get('props'))
                        cleaned_col.append({'type': blk['type'], 'props': child_props})
                base['columns'].append(cleaned_col or [])
        # Non-nested overwrite defaults
        for k, v in props.items():
            if k not in ('blocks', 'columns'):
                base[k] = v
        return base

    def validate(self, btype: str, props: dict) -> tuple[bool, str | None]:
        for field in self._blocks[btype]['schema']:
            name = field['name']
            ftype = field.get('type')
            req = field.get('required', False)
            val = props.get(name)

            if ftype in ('blocks', 'columns'):
                if req and not isinstance(val, list):
                    return False, f"{field.get('label') or name} must be a list"
            else:
                if req and not str(val or '').strip():
                    return False, f"{field.get('label') or name} is required"
        return True, None

    # --- Client-safe registry ---
    def safe_registry(self) -> dict:
        safe = {}
        for k, v in self._blocks.items():
            safe[k] = {
                'label': v['label'],
                'icon': v['icon'],
                'defaults': v['defaults'],
                'schema': v['schema'],
            }
        return safe

# ------------------------
# Server Renderer (nested)
# ------------------------
class BuilderRenderer:
    """
    Server-side renderer supporting nested blocks (section, columns).
    Renders using Jinja partials under templates/blocks/.
    """
    def __init__(self):
        self._registry = BlocksRegistry()

    def render_block(self, block: dict) -> str:
        btype = block.get('type')
        props = dict(block.get('props') or {})
        if not self._registry.exists(btype):
            return ''

        tpl = self._registry.template(btype)

        # Nested preprocessing
        if btype == 'section':
            children = props.get('blocks', [])
            html = [self.render_block(child) for child in children]
            props['blocks_html'] = '\n'.join(html)
        elif btype == 'columns':
            cols_html = []
            for col in props.get('columns', []):
                html = [self.render_block(child) for child in col]
                cols_html.append('\n'.join(html))
            props['columns_html'] = cols_html

        return render_template(tpl, **props)

    def render_page(self, page_json: dict) -> str:
        return '\n'.join(self.render_block(blk) for blk in page_json.get('blocks', []))
