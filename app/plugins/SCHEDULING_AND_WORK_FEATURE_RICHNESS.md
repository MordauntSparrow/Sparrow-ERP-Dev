# Scheduling & Work: Feature Richness & Gaps

This doc compares what’s **in place** vs what would make the stack **as feature-rich as** tools like Deputy, When I Work, Sling, and TSheets.

---

## What’s already there

### Scheduling (contractor)
- My day, my shifts (today / date / range), month view link
- Request time off, report sickness, cancel pending request
- My availability (view, add, edit, delete windows)
- AI assistant (availability, “who can take my shift”, time off, sickness)
- API: my-shifts, shifts patch, my-time-off, time-off create/cancel, report-sickness, availability CRUD

### Scheduling (admin)
- Week + month calendar, shift list, create/edit shift
- Conflict check, suggest staff, copy previous week
- Time-off list, approve/reject, create on behalf
- API: shifts CRUD, time-off list/patch, contractors/clients/sites/job-types, suggest-contractors, check-conflicts

### Work (contractor)
- Work planner: today’s schedule with client/site/postcode, notes preview, photo count
- Stop page: client & site details, record actual start/end, notes, upload photos with caption
- Sync to Time Billing (times, runsheet)

### Work (admin)
- Landing only (link to Scheduling); **no** recorded-stops list, gaps, or photo gallery yet

### Data (tables exist but little or no UI)
- **shift_swap_requests** – no contractor “offer/claim swap” or admin “approve swap” UI
- **schedule_templates** / **schedule_template_slots** – no template list, apply template, or “repeat shift” UI

---

## Gaps: what would make it “as feature rich as it can be”

### High impact (common in competitor apps)

| Area | Missing | Why it matters |
|------|--------|----------------|
| **Work – Admin** | Recorded stops list (filter by contractor, client, date); stop detail (times, notes, photos); **Gaps report** (no clock-in/out) | Visibility and payroll; catch missed punches |
| **Work – Admin** | Photo gallery (by date/contractor/shift), lightbox, optional flag/delete | Evidence and compliance |
| **Work – Admin** | Edit recorded times on behalf (override) + re-sync to Time Billing; optional audit log | Corrections without contractor re-entry |
| **Scheduling – Admin** | **Templates**: list/create/edit templates, “Apply template” to generate shifts for a week/range | Cuts data entry for recurring patterns |
| **Scheduling – Admin** | **Repeat shift**: “Repeat this shift” for N weeks (draft shifts) | Quick rollout of recurring shifts |
| **Scheduling – Contractor** | **Shift swap**: offer my shift / claim someone else’s; admin approve | Reduces admin load; staff self-serve |
| **Work – Contractor** | **Quick clock**: one-tap “Start” / “End” (set actual to now) | Faster on mobile |
| **Both** | **Notifications**: “Shift published”, “Time off approved”, “Please record your times” (portal message or in-app) | Engagement and compliance |

### Medium impact (strong differentiators)

| Area | Missing | Why it matters |
|------|--------|----------------|
| **Scheduling – Admin** | Time-off on calendar (overlay on week/month so blocked days are visible) | Avoid scheduling on leave |
| **Scheduling – Admin** | Availability vs shift conflict warning when creating/publishing (optional strict rule) | Fewer “outside availability” shifts |
| **Work – Admin** | **Reporting**: hours worked by contractor/client, export CSV; photo count by shift/contractor | Payroll and evidence reporting |
| **Work** | **Require photo** per job type or client (block “Save” until ≥1 photo) | Compliance / proof of attendance |
| **Work – Contractor** | **My recent stops** (past 7 days, read-only) | Quick reference |
| **Scheduling** | **Leave balance** (e.g. X days annual remaining) – may live in HR/config | Transparency; needs allowance data |
| **Clients/Sites** | Address, contact phone, site instructions (Time Billing schema + show in Work planner) | Richer “run sheet” for field |

### Lower priority / polish

| Area | Missing | Why it matters |
|------|--------|----------------|
| **Work** | Cut-off rule (no contractor edit after N hours unless admin) | Policy; optional |
| **Work – Admin** | Photo flag (e.g. “needs review”) and delete with audit reason | Quality/compliance |
| **Scheduling** | Contractor “my week” calendar (read-only) | Nice to have |
| **Scheduling** | Bulk approve time-off (e.g. all annual in range) | Saves clicks |
| **Work** | Offline/draft (save locally, submit when online) | Complex; lower ROI initially |
| **Scheduling** | Drag-and-drop move shift on calendar | UX polish |

---

## Suggested order to reach “feature rich”

1. **Work admin**: Recorded stops list + stop detail (times, notes, photos) + Gaps report.
2. **Work admin**: Photo gallery (filters, lightbox) + admin override times (with re-sync).
3. **Scheduling admin**: Templates (list, create, edit, apply to week/range) + “Repeat this shift” for N weeks.
4. **Scheduling**: Shift swap (contractor offer/claim, admin approve) using `shift_swap_requests`.
5. **Work contractor**: Quick clock (Start/End = now).
6. **Notifications**: Shift published, time off approved/rejected, “Record your times” (e.g. via Employee Portal messages).
7. **Work admin**: Reporting (hours by contractor/client, export CSV).
8. **Scheduling admin**: Time-off overlay on week/month calendar.
9. **Work**: Optional “require photo” per job type/client; optional cut-off rule.

---

## Summary

- **Already strong**: Contractor scheduling (my day, availability, time off, sickness, AI, API), admin calendar + smart tools (conflict, suggest, copy week), work planner with client details and notes/photos, Time Billing sync.
- **Biggest gaps**: Work admin (stops list, gaps, photos, override), scheduling templates and repeat, shift swap, quick clock, and notifications. Filling those would bring the stack close to “as feature rich as it can be” for typical workforce/scheduling use cases.
