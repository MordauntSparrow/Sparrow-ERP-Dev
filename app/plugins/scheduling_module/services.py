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
                       st.postcode AS site_postcode,
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
                       st.postcode AS site_postcode,
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
            "contractor_id", "client_id", "site_id", "job_type_id", "work_date",
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
    def list_time_off(
        contractor_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        status: Optional[str] = None,
        type_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            where = ["1=1"]
            params: List[Any] = []
            if contractor_id is not None:
                where.append("t.contractor_id = %s")
                params.append(contractor_id)
            if date_from is not None:
                where.append("t.end_date >= %s")
                params.append(date_from)
            if date_to is not None:
                where.append("t.start_date <= %s")
                params.append(date_to)
            if status:
                where.append("t.status = %s")
                params.append(status)
            if type_filter:
                where.append("t.type = %s")
                params.append(type_filter)
            cur.execute(f"""
                SELECT t.*, u.name AS contractor_name, u.email AS contractor_email
                FROM schedule_time_off t
                JOIN tb_contractors u ON u.id = t.contractor_id
                WHERE {" AND ".join(where)}
                ORDER BY t.start_date DESC
            """, params)
            return cur.fetchall() or []
        except Exception as e:
            logger.warning("list_time_off failed (run scheduling install/upgrade if needed): %s", e)
            return []
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_time_off(time_off_id: int) -> Optional[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT t.*, u.name AS contractor_name, u.email AS contractor_email
                FROM schedule_time_off t
                JOIN tb_contractors u ON u.id = t.contractor_id
                WHERE t.id = %s
            """, (time_off_id,))
            return cur.fetchone()
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
    def create_time_off_on_behalf(
        contractor_id: int,
        start_date: date,
        end_date: date,
        type: str = "annual",
        reason: Optional[str] = None,
        status: str = "approved",
    ) -> int:
        """Admin creates time off (e.g. recorded sickness). Default status approved."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO schedule_time_off (contractor_id, type, start_date, end_date, reason, status)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (contractor_id, type, start_date, end_date, reason or None, status))
            conn.commit()
            return cur.lastrowid
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def approve_time_off(time_off_id: int, reviewed_by_user_id: Optional[int] = None, admin_notes: Optional[str] = None) -> bool:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE schedule_time_off
                SET status = 'approved', reviewed_at = NOW(), reviewed_by_user_id = %s, admin_notes = %s
                WHERE id = %s AND status = 'requested'
            """, (reviewed_by_user_id, admin_notes or None, time_off_id))
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            return False
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def reject_time_off(time_off_id: int, reviewed_by_user_id: Optional[int] = None, admin_notes: Optional[str] = None) -> bool:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE schedule_time_off
                SET status = 'rejected', reviewed_at = NOW(), reviewed_by_user_id = %s, admin_notes = %s
                WHERE id = %s AND status = 'requested'
            """, (reviewed_by_user_id, admin_notes or None, time_off_id))
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            return False
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def cancel_time_off(time_off_id: int, contractor_id: int) -> bool:
        """Contractor cancels own pending request. Returns True if cancelled."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE schedule_time_off SET status = 'cancelled' WHERE id = %s AND contractor_id = %s AND status = 'requested'",
                (time_off_id, contractor_id),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            return False
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

    # ---------- Availability (contractor self-service + admin) ----------

    @staticmethod
    def get_availability(avail_id: int, contractor_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            if contractor_id is not None:
                cur.execute("SELECT * FROM schedule_availability WHERE id = %s AND contractor_id = %s", (avail_id, contractor_id))
            else:
                cur.execute("SELECT * FROM schedule_availability WHERE id = %s", (avail_id,))
            return cur.fetchone()
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def add_availability(
        contractor_id: int,
        day_of_week: int,
        start_time: time,
        end_time: time,
        effective_from: date,
        effective_to: Optional[date] = None,
    ) -> int:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO schedule_availability (contractor_id, day_of_week, start_time, end_time, effective_from, effective_to)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (contractor_id, day_of_week, start_time, end_time, effective_from, effective_to))
            conn.commit()
            return cur.lastrowid
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def update_availability(
        avail_id: int,
        contractor_id: int,
        day_of_week: Optional[int] = None,
        start_time: Optional[time] = None,
        end_time: Optional[time] = None,
        effective_from: Optional[date] = None,
        effective_to: Optional[date] = None,
    ) -> bool:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            updates = []
            params: List[Any] = []
            for k, v in [
                ("day_of_week", day_of_week),
                ("start_time", start_time),
                ("end_time", end_time),
                ("effective_from", effective_from),
                ("effective_to", effective_to),
            ]:
                if v is not None:
                    updates.append(f"{k} = %s")
                    params.append(v)
            if not updates:
                return True
            params.extend([avail_id, contractor_id])
            cur.execute(
                f"UPDATE schedule_availability SET {', '.join(updates)} WHERE id = %s AND contractor_id = %s",
                params,
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def delete_availability(avail_id: int, contractor_id: int) -> bool:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM schedule_availability WHERE id = %s AND contractor_id = %s", (avail_id, contractor_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            cur.close()
            conn.close()

    # ---------- Smart scheduling: conflicts, suggest staff, copy week ----------

    @staticmethod
    def check_shift_conflicts(
        contractor_id: int,
        work_date: date,
        scheduled_start: time,
        scheduled_end: time,
        exclude_shift_id: Optional[int] = None,
    ) -> List[str]:
        """Return list of conflict messages (double-book, time off)."""
        conflicts: List[str] = []
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            # Other shifts same day overlapping (ranges overlap if start1 < end2 and end1 > start2)
            cur.execute("""
                SELECT s.id, s.work_date, s.scheduled_start, s.scheduled_end, c.name AS client_name
                FROM schedule_shifts s
                JOIN clients c ON c.id = s.client_id
                WHERE s.contractor_id = %s AND s.work_date = %s AND s.status NOT IN ('cancelled')
                AND s.scheduled_start < %s AND s.scheduled_end > %s
            """, (contractor_id, work_date, scheduled_end, scheduled_start))
            rows = cur.fetchall() or []
            for r in rows:
                if exclude_shift_id and r.get("id") == exclude_shift_id:
                    continue
                conflicts.append(f"Overlaps existing shift at {r.get('client_name', '—')} ({r.get('scheduled_start')}–{r.get('scheduled_end')})")
            # Time off on this day
            cur.execute("""
                SELECT type, start_date, end_date FROM schedule_time_off
                WHERE contractor_id = %s AND status IN ('requested', 'approved')
                AND start_date <= %s AND end_date >= %s
            """, (contractor_id, work_date, work_date))
            to_rows = cur.fetchall() or []
            for r in to_rows:
                conflicts.append(f"Time off ({r.get('type', '—')}) on this date")
            return conflicts
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def suggest_available_contractors(
        work_date: date,
        start_time: time,
        end_time: time,
        client_id: Optional[int] = None,
        job_type_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return contractors who have no shift and no time off on work_date (suitable for assigning)."""
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, name, initials, email FROM tb_contractors WHERE status = 'active' ORDER BY name")
            all_c = cur.fetchall() or []
            available = []
            for c in all_c:
                cid = c["id"]
                # Has shift on this day overlapping?
                cur.execute("""
                    SELECT 1 FROM schedule_shifts
                    WHERE contractor_id = %s AND work_date = %s AND status NOT IN ('cancelled')
                    AND ((scheduled_start < %s AND scheduled_end > %s) OR (scheduled_start < %s AND scheduled_end > %s)
                         OR (scheduled_start >= %s AND scheduled_end <= %s))
                """, (cid, work_date, end_time, start_time, end_time, start_time, start_time, end_time))
                if cur.fetchone():
                    continue
                cur.execute("""
                    SELECT 1 FROM schedule_time_off
                    WHERE contractor_id = %s AND status IN ('requested', 'approved')
                    AND start_date <= %s AND end_date >= %s
                """, (cid, work_date, work_date))
                if cur.fetchone():
                    continue
                available.append(c)
            return available
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def copy_week_shifts(from_monday: date, to_monday: date) -> int:
        """Copy all shifts from one week (Monday) to another. Creates draft shifts. Returns count."""
        from_end = from_monday + timedelta(days=6)
        shifts = ScheduleService.list_shifts(date_from=from_monday, date_to=from_end)
        if not shifts:
            return 0
        delta_days = (to_monday - from_monday).days
        count = 0
        for s in shifts:
            if s.get("status") == "cancelled":
                continue
            new_date = s.get("work_date")
            if hasattr(new_date, "weekday"):
                new_date = new_date + timedelta(days=delta_days)
            else:
                continue
            data = {
                "contractor_id": s["contractor_id"],
                "client_id": s["client_id"],
                "site_id": s.get("site_id"),
                "job_type_id": s["job_type_id"],
                "work_date": new_date,
                "scheduled_start": s["scheduled_start"],
                "scheduled_end": s["scheduled_end"],
                "break_mins": s.get("break_mins") or 0,
                "notes": s.get("notes"),
                "status": "draft",
                "source": "manual",
            }
            try:
                ScheduleService.create_shift(data)
                count += 1
            except Exception:
                pass
        return count

    # ---------- Templates ----------

    @staticmethod
    def list_templates() -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT t.*, c.name AS client_name, st.name AS site_name, jt.name AS job_type_name
                FROM schedule_templates t
                LEFT JOIN clients c ON c.id = t.client_id
                LEFT JOIN sites st ON st.id = t.site_id
                LEFT JOIN job_types jt ON jt.id = t.job_type_id
                WHERE t.active = 1
                ORDER BY t.name
            """)
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_template(template_id: int) -> Optional[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT t.*, c.name AS client_name, st.name AS site_name, jt.name AS job_type_name
                FROM schedule_templates t
                LEFT JOIN clients c ON c.id = t.client_id
                LEFT JOIN sites st ON st.id = t.site_id
                LEFT JOIN job_types jt ON jt.id = t.job_type_id
                WHERE t.id = %s
            """, (template_id,))
            return cur.fetchone()
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def create_template(name: str, client_id: Optional[int] = None, site_id: Optional[int] = None, job_type_id: Optional[int] = None) -> int:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO schedule_templates (name, client_id, site_id, job_type_id, active)
                VALUES (%s, %s, %s, %s, 1)
            """, (name, client_id, site_id, job_type_id))
            conn.commit()
            return cur.lastrowid
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def update_template(template_id: int, name: Optional[str] = None, client_id: Optional[int] = None, site_id: Optional[int] = None, job_type_id: Optional[int] = None) -> bool:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            updates = []
            params: List[Any] = []
            for k, v in [("name", name), ("client_id", client_id), ("site_id", site_id), ("job_type_id", job_type_id)]:
                if v is not None:
                    updates.append(f"{k} = %s")
                    params.append(v)
            if not updates:
                return True
            params.append(template_id)
            cur.execute(f"UPDATE schedule_templates SET {', '.join(updates)} WHERE id = %s", params)
            conn.commit()
            return cur.rowcount > 0
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def list_template_slots(template_id: int) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM schedule_template_slots WHERE template_id = %s ORDER BY day_of_week, start_time", (template_id,))
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def add_template_slot(template_id: int, day_of_week: int, start_time: time, end_time: time, position_label: Optional[str] = None) -> int:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO schedule_template_slots (template_id, day_of_week, start_time, end_time, position_label)
                VALUES (%s, %s, %s, %s, %s)
            """, (template_id, day_of_week, start_time, end_time, position_label))
            conn.commit()
            return cur.lastrowid
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def delete_template_slot(slot_id: int) -> bool:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM schedule_template_slots WHERE id = %s", (slot_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def apply_template_to_week(template_id: int, week_monday: date, contractor_id: int) -> int:
        """Create draft shifts for the week from template slots. One contractor for all. Returns count."""
        t = ScheduleService.get_template(template_id)
        if not t:
            return 0
        slots = ScheduleService.list_template_slots(template_id)
        if not slots:
            return 0
        count = 0
        for slot in slots:
            dow = slot.get("day_of_week", 0)
            work_date = week_monday + timedelta(days=dow)
            data = {
                "contractor_id": contractor_id,
                "client_id": t.get("client_id") or 0,
                "site_id": t.get("site_id"),
                "job_type_id": t.get("job_type_id") or 0,
                "work_date": work_date,
                "scheduled_start": slot.get("start_time"),
                "scheduled_end": slot.get("end_time"),
                "break_mins": 0,
                "notes": slot.get("position_label"),
                "status": "draft",
                "source": "manual",
            }
            if data["client_id"] and data["job_type_id"]:
                try:
                    ScheduleService.create_shift(data)
                    count += 1
                except Exception:
                    pass
        return count

    @staticmethod
    def repeat_shift(shift_id: int, num_weeks: int) -> int:
        """Create copies of this shift for the next num_weeks (same weekday). Returns count."""
        shift = ScheduleService.get_shift(shift_id)
        if not shift or num_weeks < 1:
            return 0
        count = 0
        for i in range(1, num_weeks + 1):
            wd = shift.get("work_date")
            if not wd or not hasattr(wd, "weekday"):
                continue
            new_date = wd + timedelta(days=7 * i)
            data = {
                "contractor_id": shift["contractor_id"],
                "client_id": shift["client_id"],
                "site_id": shift.get("site_id"),
                "job_type_id": shift["job_type_id"],
                "work_date": new_date,
                "scheduled_start": shift.get("scheduled_start"),
                "scheduled_end": shift.get("scheduled_end"),
                "break_mins": shift.get("break_mins") or 0,
                "notes": shift.get("notes"),
                "status": "draft",
                "source": "manual",
            }
            try:
                ScheduleService.create_shift(data)
                count += 1
            except Exception:
                pass
        return count

    # ---------- Shift swap ----------

    @staticmethod
    def create_swap_request(shift_id: int, requester_contractor_id: int, notes: Optional[str] = None) -> Optional[int]:
        """Offer my shift for swap. Requester must own the shift. Returns swap id or None."""
        shift = ScheduleService.get_shift(shift_id)
        if not shift or shift["contractor_id"] != requester_contractor_id:
            return None
        if shift.get("status") == "cancelled":
            return None
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM shift_swap_requests WHERE shift_id = %s AND status IN ('open','claimed')", (shift_id,))
            if cur.fetchone():
                return None
            cur.execute("""
                INSERT INTO shift_swap_requests (shift_id, requester_contractor_id, status, notes)
                VALUES (%s, %s, 'open', %s)
            """, (shift_id, requester_contractor_id, notes))
            conn.commit()
            return cur.lastrowid
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def list_swap_requests(
        contractor_id: Optional[int] = None,
        status: Optional[str] = None,
        for_claimer: bool = False,
    ) -> List[Dict[str, Any]]:
        """List swap requests. If for_claimer=True, only open ones (that this contractor could claim)."""
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            where = ["1=1"]
            params: List[Any] = []
            if contractor_id is not None:
                if for_claimer:
                    where.append("r.status = 'open'")
                    where.append("s.contractor_id != %s")
                    params.append(contractor_id)
                else:
                    where.append("(r.requester_contractor_id = %s OR r.claimer_contractor_id = %s)")
                    params.extend([contractor_id, contractor_id])
            if status:
                where.append("r.status = %s")
                params.append(status)
            cur.execute(f"""
                SELECT r.*, s.work_date, s.scheduled_start, s.scheduled_end,
                       c.name AS client_name, u1.name AS requester_name, u2.name AS claimer_name
                FROM shift_swap_requests r
                JOIN schedule_shifts s ON s.id = r.shift_id
                JOIN clients c ON c.id = s.client_id
                JOIN tb_contractors u1 ON u1.id = r.requester_contractor_id
                LEFT JOIN tb_contractors u2 ON u2.id = r.claimer_contractor_id
                WHERE {" AND ".join(where)}
                ORDER BY r.requested_at DESC
            """, params)
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def claim_swap(swap_id: int, claimer_contractor_id: int) -> bool:
        """Claim an open swap. Returns True if updated."""
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM shift_swap_requests WHERE id = %s AND status = 'open'", (swap_id,))
            r = cur.fetchone()
            if not r or r["requester_contractor_id"] == claimer_contractor_id:
                return False
            cur.execute("""
                UPDATE shift_swap_requests SET status = 'claimed', claimer_contractor_id = %s, claimed_at = NOW()
                WHERE id = %s
            """, (claimer_contractor_id, swap_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def approve_swap(swap_id: int) -> bool:
        """Approve a claimed swap: reassign shift to claimer."""
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM shift_swap_requests WHERE id = %s AND status = 'claimed'", (swap_id,))
            r = cur.fetchone()
            if not r:
                return False
            ScheduleService.update_shift(r["shift_id"], {"contractor_id": r["claimer_contractor_id"]})
            cur.execute("""
                UPDATE shift_swap_requests SET status = 'approved', resolved_at = NOW()
                WHERE id = %s
            """, (swap_id,))
            conn.commit()
            return True
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def reject_swap(swap_id: int) -> bool:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("UPDATE shift_swap_requests SET status = 'rejected', resolved_at = NOW() WHERE id = %s AND status IN ('open','claimed')", (swap_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def cancel_swap(swap_id: int, contractor_id: int) -> bool:
        """Requester or claimer cancels."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("UPDATE shift_swap_requests SET status = 'cancelled', resolved_at = NOW() WHERE id = %s AND status IN ('open','claimed') AND (requester_contractor_id = %s OR claimer_contractor_id = %s)", (swap_id, contractor_id, contractor_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            cur.close()
            conn.close()
