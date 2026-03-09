# Integration Foundation: Linking Ventus, Time Billing, Scheduling & Work

This document describes how modules share data and automate flows (Odoo/Sling-level).

## Shared identity: Contractors

- **tb_contractors** (time_billing_module) is the canonical employee/contractor record.
- **contractor_ventus_mapping** links contractor_id ↔ Ventus callSign (and optionally division), so sign-on/off in Ventus can drive runsheets and attendance.
- Scheduling and Work modules use **tb_contractors.id** as user_id/contractor_id.

## Data flow: Ventus → Time Billing & Scheduling

1. **Ventus sign-on** (MDT): callSign, crew (list), shiftStartAt, shiftEndAt, signOnTime.
2. **Mapping**: Each crew member or callSign is resolved to tb_contractors (via contractor_ventus_mapping or initials/email).
3. **Scheduling**: Create or update **schedule_shifts** for that day (one shift per contractor) so the schedule reflects who is on duty.
4. **Time Billing**: Create or update **runsheets** and **runsheet_assignments** for the shift (client/site can be generic “Response” or come from a default job type). On **publish**, timesheet entries are created and pay is computed.
5. **Ventus sign-off**: Update shift end time and runsheet assignment **actual_end**; optionally mark attendance in scheduling.

So: **Sign-on creates shifts and runsheet assignments; sign-off writes actual end times; publishing runsheets fills timesheets and pay.**

## Data flow: Scheduling → Work module

1. **Scheduling** owns shifts: who works when, at which location (client/site), job type.
2. **Work module** calls Scheduling (or reads schedule_shifts) to get **my shifts for today** for the logged-in contractor.
3. Work presents a simple “My day” list: each stop = one shift (client/site, scheduled start/end).
4. Contractor records **arrival**, **leaving**, **notes**, **photos** per stop in the Work app.
5. Work module writes:
   - **actual_start** / **actual_end** (and notes) into **runsheet_assignments** and/or **tb_timesheet_entries** (source `scheduler` or `work_module`).
   - Photos into **work_photos** (or shared storage), linked to the shift/assignment.

So: **Scheduling defines the plan; Work records what actually happened and pushes it to Time Billing.**

## Data flow: Work → Time Billing

- Work updates **runsheet_assignments** (actual_start, actual_end, notes) when the run was created from a runsheet.
- If the shift came only from Scheduling (no runsheet yet), Work can create a **runsheet** + **runsheet_assignments** and then trigger the same publish logic, or create **tb_timesheet_entries** directly with source `scheduler`/`work_module`.
- Time Billing’s existing **TimesheetService** and **RateResolver** compute pay; week totals and approval flow unchanged.

## Tables and ownership

| Area            | Tables / Concepts |
|-----------------|-------------------|
| Identity       | tb_contractors, contractor_ventus_mapping |
| Time Billing    | clients, sites, job_types, runsheets, runsheet_assignments, tb_timesheet_*, wage/bill rates, calendar_policies |
| Scheduling      | schedule_locations (or use clients/sites), schedule_shifts, schedule_availability, schedule_time_off, shift_swap_requests |
| Work            | work_photos (and/or use runsheet_assignments.notes + file store) |
| Ventus          | mdts_signed_on, mdt_jobs, etc. (unchanged); integration via hooks that read/write Time Billing + Scheduling |

## Implemented pieces

- **contractor_ventus_mapping** (time_billing): Links tb_contractors to Ventus callSign.
- **ventus_integration_defaults** (time_billing): Default client_id, job_type_id, site_id for runsheets created from Ventus.
- **ventus_integration.on_ventus_sign_on / on_ventus_sign_off**: Called from Ventus routes; creates runsheet + assignment + schedule_shift on sign-on, sets actual_end on sign-off.
- **Scheduling module**: schedule_shifts, availability, time_off, swap_requests, templates; admin shifts list; public My day and API for Work.
- **Work module**: My day from scheduling; record arrival/leaving/notes per stop; work_photos; sync to runsheet_assignments and tb_timesheet_entries when linked.
- **Publishing**: Ventus-created runsheets are draft. In Time Billing admin, Publish runsheet to create timesheet entries and pay. Work then keeps actual times in sync.

## Competitor alignment

- **Odoo**: Linked CRM, projects, timesheets, HR. We link Ventus (response), Time Billing (timesheets, pay), Scheduling (shifts), Work (field capture).
- **Sling**: Shifts, availability, swap requests, labour cost, templates. Scheduling module implements these; labour cost uses Time Billing wage rates.
- **Work module**: Fast, mobile-first “my day” and record times + notes + pictures; one-touch sync to timesheets and pay.
