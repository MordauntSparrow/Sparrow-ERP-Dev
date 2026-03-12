"""
Work module: my day from scheduling, record times/notes, sync to time billing.
"""
from datetime import date, time
from typing import Any, Dict, List, Optional
from app.objects import get_db_connection

# Lazy import to avoid circular dependency; only needed when syncing to TB
def _get_schedule_service():
    from app.plugins.scheduling_module.services import ScheduleService
    return ScheduleService


def list_stops_admin(
    contractor_id: Optional[int] = None,
    client_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """List shifts (recorded stops) for admin with photo count."""
    shifts = _get_schedule_service().list_shifts(
        contractor_id=contractor_id,
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        status=None,
    )
    for s in shifts:
        s["photo_count"] = _photo_count_for_shift(s["id"])
    return shifts


def _photo_count_for_shift(shift_id: int) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM work_photos WHERE shift_id = %s", (shift_id,))
        return cur.fetchone()[0] or 0
    finally:
        cur.close()
        conn.close()


def list_gaps(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    contractor_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Shifts that are published but have no actual_start or no actual_end (missing clock)."""
    shifts = _get_schedule_service().list_shifts(
        contractor_id=contractor_id,
        date_from=date_from,
        date_to=date_to,
        status="published",
    )
    gaps = []
    for s in shifts:
        if s.get("actual_start") is None or s.get("actual_end") is None:
            s["photo_count"] = _photo_count_for_shift(s["id"])
            gaps.append(s)
    return gaps


def get_shift_for_admin(shift_id: int) -> Optional[Dict[str, Any]]:
    """Get any shift by id (admin)."""
    return _get_schedule_service().get_shift(shift_id)


def update_shift_times_admin(
    shift_id: int,
    actual_start: Optional[time] = None,
    actual_end: Optional[time] = None,
    notes: Optional[str] = None,
) -> bool:
    """Admin override: set actual times/notes and re-sync to Time Billing."""
    shift = _get_schedule_service().get_shift(shift_id)
    if not shift:
        return False
    updates = {}
    if actual_start is not None:
        updates["actual_start"] = actual_start
    if actual_end is not None:
        updates["actual_end"] = actual_end
    if notes is not None:
        updates["notes"] = notes
    if not updates:
        return True
    updates["status"] = "completed" if (actual_end or shift.get("actual_end")) else "in_progress"
    _get_schedule_service().update_shift(shift_id, updates)
    sync_shift_to_time_billing(shift_id)
    return True


def list_photos_admin(
    contractor_id: Optional[int] = None,
    shift_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """List photos with optional filters; join shift for client/date."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        where = ["1=1"]
        params: List[Any] = []
        if contractor_id is not None:
            where.append("p.contractor_id = %s")
            params.append(contractor_id)
        if shift_id is not None:
            where.append("p.shift_id = %s")
            params.append(shift_id)
        if date_from is not None:
            where.append("s.work_date >= %s")
            params.append(date_from)
        if date_to is not None:
            where.append("s.work_date <= %s")
            params.append(date_to)
        cur.execute(f"""
            SELECT p.id, p.shift_id, p.contractor_id, p.file_path, p.file_name, p.caption, p.created_at,
                   s.work_date, s.client_id, c.name AS client_name, u.name AS contractor_name
            FROM work_photos p
            JOIN schedule_shifts s ON s.id = p.shift_id
            JOIN clients c ON c.id = s.client_id
            JOIN tb_contractors u ON u.id = p.contractor_id
            WHERE {" AND ".join(where)}
            ORDER BY p.created_at DESC
        """, params)
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()


def report_hours(
    date_from: date,
    date_to: date,
    contractor_id: Optional[int] = None,
    client_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Hours worked (from actual_start/actual_end) by shift; for CSV/reporting."""
    shifts = _get_schedule_service().list_shifts(
        contractor_id=contractor_id,
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        status=None,
    )
    rows = []
    for s in shifts:
        if s.get("actual_start") is None and s.get("actual_end") is None:
            continue
        start = s.get("actual_start")
        end = s.get("actual_end")
        hours = None
        if start and end and hasattr(start, "hour") and hasattr(end, "hour"):
            from datetime import datetime as dt
            d = date.today()
            hours = (dt.combine(d, end) - dt.combine(d, start)).total_seconds() / 3600.0
            if hours < 0:
                hours += 24
        start_str = start.strftime("%H:%M") if start and hasattr(start, "strftime") else (str(start)[:5] if start else "")
        end_str = end.strftime("%H:%M") if end and hasattr(end, "strftime") else (str(end)[:5] if end else "")
        rows.append({
            "shift_id": s["id"],
            "work_date": s.get("work_date"),
            "contractor_name": s.get("contractor_name"),
            "client_name": s.get("client_name"),
            "site_name": s.get("site_name"),
            "actual_start": start,
            "actual_end": end,
            "actual_start_str": start_str,
            "actual_end_str": end_str,
            "hours": round(hours, 2) if hours is not None else None,
            "notes": s.get("notes"),
        })
    return rows


def get_my_stops_for_today(contractor_id: int) -> List[Dict[str, Any]]:
    today = date.today()
    return _get_schedule_service().get_my_shifts_for_date(contractor_id, today)


def get_shift_for_stop(shift_id: int, contractor_id: int) -> Optional[Dict[str, Any]]:
    shift = _get_schedule_service().get_shift(shift_id)
    if not shift or shift["contractor_id"] != contractor_id:
        return None
    return shift


def record_stop(shift_id: int, contractor_id: int, actual_start: Optional[time] = None, actual_end: Optional[time] = None, notes: Optional[str] = None) -> bool:
    shift = get_shift_for_stop(shift_id, contractor_id)
    if not shift:
        return False
    _get_schedule_service().record_actual_times(shift_id, actual_start=actual_start, actual_end=actual_end, notes=notes)
    sync_shift_to_time_billing(shift_id)
    return True


def sync_shift_to_time_billing(shift_id: int) -> None:
    """
    If the shift is linked to a runsheet assignment, update that assignment and
    the corresponding timesheet entry. If the shift has no runsheet yet (scheduler-only),
    create a runsheet + assignment and publish so the timesheet is autofilled.
    """
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, contractor_id, client_id, site_id, job_type_id, work_date,
                   actual_start, actual_end, notes, runsheet_id, runsheet_assignment_id
            FROM schedule_shifts WHERE id = %s
        """, (shift_id,))
        shift = cur.fetchone()
        if not shift:
            return
        if not shift.get("runsheet_id"):
            cur.close()
            conn.close()
            from app.plugins.time_billing_module.services import RunsheetService
            RunsheetService.create_and_publish_runsheet_for_shift(shift_id)
            return
        if not shift.get("runsheet_assignment_id"):
            return
        ra_id = shift["runsheet_assignment_id"]
        rs_id = shift["runsheet_id"]
        user_id = shift["contractor_id"]
        work_date = shift["work_date"]
        actual_start = shift.get("actual_start")
        actual_end = shift.get("actual_end")
        notes = shift.get("notes")
        cur.execute("""
            UPDATE runsheet_assignments
            SET actual_start = %s, actual_end = %s, notes = %s
            WHERE id = %s
        """, (actual_start, actual_end, notes, ra_id))
        iso_year, iso_week, _ = work_date.isocalendar()
        week_id_str = f"{iso_year}{iso_week:02d}"
        cur.execute("SELECT id FROM tb_timesheet_weeks WHERE user_id = %s AND week_id = %s", (user_id, week_id_str))
        wk = cur.fetchone()
        if not wk:
            conn.commit()
            return
        week_pk = wk["id"]
        cur.execute("""
            UPDATE tb_timesheet_entries
            SET actual_start = COALESCE(%s, actual_start),
                actual_end = COALESCE(%s, actual_end),
                notes = COALESCE(%s, notes)
            WHERE week_id = %s AND user_id = %s AND work_date = %s AND source = 'runsheet' AND runsheet_id = %s
        """, (actual_start, actual_end, notes, week_pk, user_id, work_date, rs_id))
        from app.plugins.time_billing_module.services import TimesheetService
        TimesheetService.refresh_entries_actuals(cur, conn, week_pk, user_id, work_date, rs_id)
        conn.commit()
    finally:
        cur.close()
        conn.close()


def add_photo(shift_id: int, contractor_id: int, file_path: str, file_name: Optional[str] = None, mime_type: Optional[str] = None, caption: Optional[str] = None) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO work_photos (shift_id, contractor_id, file_path, file_name, mime_type, caption)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (shift_id, contractor_id, file_path, file_name, mime_type, caption))
        conn.commit()
        return cur.lastrowid
    finally:
        cur.close()
        conn.close()


def list_photos_for_shift(shift_id: int) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT id, file_path, file_name, caption, created_at FROM work_photos WHERE shift_id = %s ORDER BY created_at", (shift_id,))
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()
