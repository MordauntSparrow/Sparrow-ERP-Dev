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
        TimesheetService._refresh_week_totals(cur, user_id, week_pk)
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
