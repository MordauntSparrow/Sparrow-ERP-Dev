"""
Scheduling module services: shifts, availability, time off, swap requests.
Uses tb_contractors, clients, sites, job_types from time_billing_module.
"""
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional
from app.objects import get_db_connection

logger = logging.getLogger(__name__)


class ScheduleService:
    @staticmethod
    def list_shifts(
        contractor_id: Optional[int] = None,
        client_id: Optional[int] = None,
        work_date: Optional[date] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            where = ["1=1"]
            params: List[Any] = []
            if contractor_id is not None:
                where.append("s.contractor_id = %s")
                params.append(contractor_id)
            if client_id is not None:
                where.append("s.client_id = %s")
                params.append(client_id)
            if work_date is not None:
                where.append("s.work_date = %s")
                params.append(work_date)
            if date_from is not None:
                where.append("s.work_date >= %s")
                params.append(date_from)
            if date_to is not None:
                where.append("s.work_date <= %s")
                params.append(date_to)
            if status:
                where.append("s.status = %s")
                params.append(status)
            cur.execute(f"""
                SELECT s.*,
                       c.name AS client_name,
                       st.name AS site_name,
                       jt.name AS job_type_name,
                       u.name AS contractor_name,
                       u.initials AS contractor_initials
                FROM schedule_shifts s
                JOIN clients c ON c.id = s.client_id
                LEFT JOIN sites st ON st.id = s.site_id
                JOIN job_types jt ON jt.id = s.job_type_id
                JOIN tb_contractors u ON u.id = s.contractor_id
                WHERE {" AND ".join(where)}
                ORDER BY s.work_date, s.scheduled_start
            """, params)
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_my_shifts_for_date(contractor_id: int, work_date: date) -> List[Dict[str, Any]]:
        return ScheduleService.list_shifts(
            contractor_id=contractor_id,
            work_date=work_date,
            status=None,
        )

    @staticmethod
    def get_shift(shift_id: int) -> Optional[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT s.*,
                       c.name AS client_name,
                       st.name AS site_name,
                       jt.name AS job_type_name,
                       u.name AS contractor_name,
                       u.initials AS contractor_initials
                FROM schedule_shifts s
                JOIN clients c ON c.id = s.client_id
                LEFT JOIN sites st ON st.id = s.site_id
                JOIN job_types jt ON jt.id = s.job_type_id
                JOIN tb_contractors u ON u.id = s.contractor_id
                WHERE s.id = %s
            """, (shift_id,))
            return cur.fetchone()
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def create_shift(data: Dict[str, Any]) -> int:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO schedule_shifts
                (contractor_id, client_id, site_id, job_type_id, work_date,
                 scheduled_start, scheduled_end, break_mins, notes, status, source,
                 external_id, runsheet_id, runsheet_assignment_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                data["contractor_id"],
                data["client_id"],
                data.get("site_id"),
                data["job_type_id"],
                data["work_date"],
                data["scheduled_start"],
                data["scheduled_end"],
                int(data.get("break_mins") or 0),
                data.get("notes"),
                data.get("status") or "draft",
                data.get("source") or "manual",
                data.get("external_id"),
                data.get("runsheet_id"),
                data.get("runsheet_assignment_id"),
            ))
            conn.commit()
            return cur.lastrowid
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def update_shift(shift_id: int, data: Dict[str, Any]) -> None:
        allowed = {
            "scheduled_start", "scheduled_end", "actual_start", "actual_end",
            "break_mins", "notes", "status", "labour_cost",
        }
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            updates = []
            params: List[Any] = []
            for k in allowed:
                if k in data:
                    updates.append(f"{k} = %s")
                    params.append(data[k])
            if not updates:
                return
            params.append(shift_id)
            cur.execute(
                f"UPDATE schedule_shifts SET {', '.join(updates)} WHERE id = %s",
                params,
            )
            conn.commit()
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def record_actual_times(shift_id: int, actual_start: Optional[time] = None, actual_end: Optional[time] = None, notes: Optional[str] = None) -> None:
        updates: Dict[str, Any] = {}
        if actual_start is not None:
            updates["actual_start"] = actual_start
        if actual_end is not None:
            updates["actual_end"] = actual_end
        if notes is not None:
            updates["notes"] = notes
        if updates:
            updates["status"] = "completed" if actual_end else "in_progress"
            ScheduleService.update_shift(shift_id, updates)

    @staticmethod
    def list_availability(contractor_id: int) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT * FROM schedule_availability
                WHERE contractor_id = %s
                ORDER BY day_of_week, start_time
            """, (contractor_id,))
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def list_time_off(contractor_id: Optional[int] = None, date_from: Optional[date] = None, date_to: Optional[date] = None) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            where = ["1=1"]
            params: List[Any] = []
            if contractor_id is not None:
                where.append("contractor_id = %s")
                params.append(contractor_id)
            if date_from is not None:
                where.append("end_date >= %s")
                params.append(date_from)
            if date_to is not None:
                where.append("start_date <= %s")
                params.append(date_to)
            cur.execute(f"""
                SELECT t.*, u.name AS contractor_name
                FROM schedule_time_off t
                JOIN tb_contractors u ON u.id = t.contractor_id
                WHERE {" AND ".join(where)}
                ORDER BY start_date
            """, params)
            return cur.fetchall() or []
        except Exception as e:
            # Table may not exist if scheduling migrations not run (e.g. 1146 Table doesn't exist)
            logger.warning("list_time_off failed (run scheduling install/upgrade if needed): %s", e)
            return []
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def create_time_off(contractor_id: int, start_date: date, end_date: date, reason: Optional[str] = None, type: str = "annual") -> int:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO schedule_time_off (contractor_id, type, start_date, end_date, reason, status)
                VALUES (%s, %s, %s, %s, %s, 'requested')
            """, (contractor_id, type, start_date, end_date, reason or None))
            conn.commit()
            return cur.lastrowid
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def list_clients_and_sites() -> tuple:
        """Return (clients, sites) for dropdowns. Uses time_billing clients/sites."""
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, name FROM clients WHERE active = 1 ORDER BY name")
            clients = cur.fetchall() or []
            cur.execute("SELECT id, client_id, name FROM sites WHERE active = 1 ORDER BY name")
            sites = cur.fetchall() or []
            return clients, sites
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def list_job_types() -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, name, code FROM job_types WHERE active = 1 ORDER BY name")
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def list_contractors() -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, name, initials, email FROM tb_contractors WHERE status = 'active' ORDER BY name")
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()
