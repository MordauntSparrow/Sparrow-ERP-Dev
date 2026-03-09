# Module Status: Universal vs Specialist, Integration & Security

**Goal:** Universal core (employee portal, timesheets, scheduling, work, HR, compliance, training) used by everyone; specialist clusters (e.g. Ventus for emergency services) bundle their own modules but link back to the core. All secure and seamlessly integrated.

**Two apps:** Admin (port 82) = core users, Flask-Login, plugin **internal** routes. Contractor (port 80) = `tb_contractors`, session `tb_user`, plugin **public** routes. See `TWO_APP_ARCHITECTURE.md` for details.

---

## 1. Universal modules (everyone uses)

| Module | Purpose | Public (staff) | Admin (internal) | DB / install | Auth |
|--------|---------|----------------|------------------|--------------|------|
| **employee_portal_module** | Hub: login, dashboard, messages, todos, module links | ✅ Login, dashboard, messages, todos | Redirect only | ep_* tables, install.py | session `tb_user` |
| **time_billing_module** | Timesheets, runsheets, contractors, rates, pay | ✅ Login, dashboard, week view | ✅ Full (contractors, runsheets, rates, policies) | 001 + 002–004, install.py | `tb_user`; role from DB; **admin_required_tb enforced** ✅ |
| **hr_module** | Profile, document requests, uploads | ✅ Profile, requests, upload | Redirect only | hr_* tables, install.py | `tb_user` |
| **compliance_module** | Policies, view & acknowledge | ✅ List policies, view, sign | Redirect only | compliance_* tables, install.py | `tb_user` |
| **scheduling_module** | Shifts, time off, sickness | ✅ My day, request time off, report sickness | ✅ Shifts list + API | schedule_* tables, install.py | Public: `tb_user`; **admin: _admin_required_scheduling** ✅ |
| **work_module** | My day, record times/notes/photos → timesheet | ✅ Stops, record, photos | None | work_photos, install.py | `tb_user` |
| **training_module** | Training & mandatory completion | Stub page only | Redirect only | **No schema** | `tb_user` |

---

## 2. Specialist / cluster modules

| Module | Cluster | Links to universal |
|--------|---------|--------------------|
| **ventus_response_module** | Emergency / CAD (dispatch, units, jobs) | Sign-on/off → time_billing (runsheets, schedule_shifts) via ventus_integration; contractor_ventus_mapping |
| **medical_records_module** | VITA patient records | Dependency for Ventus; own auth (Flask-Login + PIN) |
| **inventory_control** | Stock, costing, mobile API | No direct link to portal/timesheets in code |
| **website_module** | Public site, pages, builder | Hosts plugin public routes; Flask-Login; **hardcoded default secret_key** ⚠️ |
| **news_blog_module** | Articles, sitemap | Content only |
| **event_manager_module** | Events (public + admin) | Content only |

---

## 3. Integration map

- **Portal** reads `ep_messages`, `ep_todos`; calls compliance (pending policies count), HR (pending requests count).
- **Work** reads/writes `schedule_shifts` (via Scheduling); writes `runsheet_assignments`, `tb_timesheet_entries` (via Time Billing); creates runsheet when shift has none (autofill timesheet).
- **Scheduling** reads `tb_contractors`, `clients`, `sites`, `job_types` (Time Billing).
- **Time Billing** writes `runsheets`, `runsheet_assignments`; links to `schedule_shifts`; used by Work and Ventus integration.
- **Ventus** calls `time_billing_module.ventus_integration` on sign-on/off; reads `contractor_ventus_mapping`, `ventus_integration_defaults`.
- **ep_todos / ep_messages:** Contract for other modules to push items; no plugin currently writes to them in code (manual or future).

---

## 4. Security status

| Area | Status | Notes |
|------|--------|--------|
| Staff auth (portal, TB, HR, compliance, scheduling, work) | ✅ | session `tb_user`; redirect to portal login when missing |
| Time Billing **admin** routes | ✅ **Enforced** | **Admin app (port 82):** `admin_required_tb` uses Flask-Login `current_user` + `@login_required`; requires `current_user.role` in (`admin`, `superuser`) |
| Scheduling **admin** routes | ✅ **Enforced** | **Admin app (port 82):** `_admin_required_scheduling` uses Flask-Login `current_user` + `@login_required`; requires admin/superuser |
| HR / Compliance / Portal admin | N/A or redirect | No admin UI yet |
| Website app secret_key | ⚠️ | Default `'your_secret_key_here'` in website __init__ |
| Ventus / Medical admin | ✅ | Flask-Login + role checks; admin PIN in-memory only |

---

## 5. Gaps (prioritised for impact)

1. **Critical – Admin route protection (universal)**  
   ~~Time Billing and Scheduling admin routes were open.~~ **Done:** Role is loaded from DB on login (`tb_contractor_roles` + `role_id` → `roles.name`); `admin_required_tb` and `_admin_required_scheduling` require role in (`admin`, `superuser`).

2. **High – Training module is stub**  
   No schema, no install, no “my training” or completions. Blocks universal “everyone uses” story.

3. **Medium – Admin UIs for universal modules**  
   Compliance (create policies, view acknowledgements), HR (create document requests, view uploads), Portal (manage messages/todos) have no admin screens.

4. **Medium – Single source of truth for “is admin”**  
   Unify how admin is determined (e.g. main app user vs `tb_contractors` role) so all plugin admin routes use the same check.

---

## 6. Recommended next focus

**Single highest impact (done):** **Secure universal admin routes (Time Billing + Scheduling).**

- Role is resolved from DB on login (portal + time_billing): `_contractor_effective_role(contractor_id)` from `tb_contractor_roles` and `roles` (and fallback to `tb_contractors.role_id`).
- `admin_required_tb` now requires `current_tb_user()` and `role` in (`admin`, `superuser`); else redirect with flash.
- Scheduling internal routes use `_admin_required_scheduling` (same role check).

**Granting admin (port 82):** Admin and scheduling plugin routes run on the **admin app** and use **core users** (Flask-Login). Grant access by creating a core user in the `users` table with `role` = `'admin'` or `'superuser'` and logging in on the admin app (port 82). Contractors (`tb_contractors`) do not log in there; they use the website app (port 80).

**Next focus (pick one):** **Training module (schema + minimal flow)** for universal completeness, or **Admin UIs for Compliance/HR** for operational use.
