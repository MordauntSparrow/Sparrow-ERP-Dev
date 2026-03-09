# Two-App Architecture: Admin vs Contractor Portals

The system is designed around **two separate Flask apps** that are linked by data but have **separate authentication and entry points**.

---

## 1. Admin app (port 82)

- **Purpose:** Management and admin views. Managers and core staff use this to manage scheduling, HR, time billing, compliance, plugins, etc.
- **Flask app:** Built by `create_app()` in `app/create_app.py` (the main app).
- **Auth:** **Core users** from the `users` table. Login via **Flask-Login** (`routes.login`). Session holds the core user; `current_user` (Flask-Login) is the source of truth.
- **Who uses it:** Admins, superusers, managers – anyone with a **core user account** (not a contractor account).
- **Plugin registration:** **Internal (admin) blueprints** are registered on this app via `plugin_manager.register_admin_routes(app)`. So all routes under `/plugin/<module_name>/` (e.g. `/plugin/time_billing_module/`, `/plugin/scheduling_module/`) run on the **admin app**.
- **Access control:** Plugin admin routes must require **core login** and **admin/superuser role** (e.g. `@login_required` and `current_user.role in ('admin', 'superuser')`). This is the same model as core routes (e.g. `admin_only()` in `app/routes.py`).

**Examples of admin-side views:**

- Time Billing: contractors list/edit, runsheets, rates, policies, timesheet approval.
- Scheduling: manager shifts list, create/edit shifts, approve time off/sickness.
- HR: create document requests, review uploads, staff details (when implemented).
- Compliance: create/edit policies, view acknowledgements (when implemented).

---

## 2. Website / contractor app (port 80)

- **Purpose:** Contractor and staff self-service. Employees log in here to see their schedule, submit times, request time off, view policies, upload documents, etc.
- **Flask app:** Built by `create_website_app()` in `app/plugins/website_module/__init__.py`. Runs on its own port (e.g. 80).
- **Auth:** **Contractors** from the `tb_contractors` table. Login via the **employee portal** (or time-billing login that redirects to portal). Session holds `tb_user` (contractor id, name, email, role, etc.). There is **no** Flask-Login `current_user` from the core `users` table on this app.
- **Who uses it:** Contractors, field staff, cleaners, etc. – anyone with a **contractor account** in `tb_contractors`.
- **Plugin registration:** **Public blueprints** are registered on this app via `plugin_manager.register_public_routes(app)` in the website module. So routes like `/employee-portal/`, `/time-billing/`, `/scheduling/`, `/work/`, `/hr/`, `/compliance/` run on the **website app**.
- **Access control:** Plugin public routes require **contractor login** (session `tb_user`). Use decorators like `staff_required_tb`, `_staff_required`, etc. that redirect to `/employee-portal/login` if `tb_user` is missing.

**Examples of contractor-side views:**

- Employee portal dashboard, messages, todos.
- Time Billing: my timesheet, week view, submit.
- Scheduling: my day, request time off, report sickness.
- Work: my day, record stop (times/notes/photos).
- HR: my profile, document requests, upload documents.
- Compliance: list policies, view and acknowledge.

---

## 3. Summary table

| Aspect            | Admin app (port 82)              | Website app (port 80)           |
|------------------|-----------------------------------|---------------------------------|
| App factory      | `create_app()`                    | `create_website_app()`          |
| Users            | Core users (`users` table)       | Contractors (`tb_contractors`)  |
| Auth             | Flask-Login, `current_user`      | Session `tb_user`               |
| Login route      | `routes.login`                   | Employee portal login           |
| Plugin routes    | **Internal** (`/plugin/...`)      | **Public** (e.g. `/scheduling/`)|
| Protect with     | `@login_required` + admin role   | `tb_user` + staff decorator     |

---

## 4. Implementing both sides in a module

When a module has **both** admin and contractor views:

1. **Internal blueprint** (admin app, port 82):
   - Use `@login_required` (Flask-Login) and require `current_user.role in ('admin', 'superuser')` (or a shared helper like `admin_only()`).
   - Do **not** rely on `session['tb_user']` here; it is not set on the admin app.

2. **Public blueprint** (website app, port 80):
   - Use a staff decorator that checks `session.get('tb_user')` and redirects to the employee portal login if missing.
   - Do **not** rely on Flask-Login `current_user` here; core users do not log in on the website app.

3. **Data:** Both sides can read/write the same DB tables (contractors, shifts, runsheets, etc.). Access control is by **who is logged in on which app**, not by mixing the two auth models on one route.

This keeps admin and contractor access **completely separate but linked** through shared data and consistent decorators on each app.
