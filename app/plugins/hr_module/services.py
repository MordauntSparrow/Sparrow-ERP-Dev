from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from app.objects import get_db_connection

# Request types for document requests
REQUEST_TYPES = ["right_to_work", "driving_licence", "dbs", "contract", "other"]


def get_staff_profile(contractor_id: int) -> Dict[str, Any]:
    """Contractor-facing profile: core + HR details (including doc/expiry fields for read-only summary)."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, name, email, initials, status FROM tb_contractors WHERE id = %s",
            (contractor_id,),
        )
        row = cur.fetchone() or {}
        try:
            cur.execute("""
                SELECT phone, address_line1, address_line2, postcode, emergency_contact_name, emergency_contact_phone,
                       driving_licence_number, driving_licence_expiry, driving_licence_document_path,
                       right_to_work_type, right_to_work_expiry, right_to_work_document_path,
                       dbs_level, dbs_number, dbs_expiry, dbs_document_path,
                       contract_type, contract_start, contract_end, contract_document_path
                FROM hr_staff_details WHERE contractor_id = %s
            """, (contractor_id,))
        except Exception:
            cur.execute(
                "SELECT phone, address_line1, address_line2, postcode, emergency_contact_name, emergency_contact_phone "
                "FROM hr_staff_details WHERE contractor_id = %s",
                (contractor_id,),
            )
        extra = cur.fetchone()
        if extra:
            row.update(extra)
        return row
    finally:
        cur.close()
        conn.close()


def update_staff_details(contractor_id: int, data: Dict[str, Any]) -> None:
    """Contractor self-service: update phone, address, emergency contact only."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO hr_staff_details (contractor_id, phone, address_line1, address_line2, postcode, emergency_contact_name, emergency_contact_phone)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            phone = COALESCE(VALUES(phone), phone),
            address_line1 = COALESCE(VALUES(address_line1), address_line1),
            address_line2 = COALESCE(VALUES(address_line2), address_line2),
            postcode = COALESCE(VALUES(postcode), postcode),
            emergency_contact_name = COALESCE(VALUES(emergency_contact_name), emergency_contact_name),
            emergency_contact_phone = COALESCE(VALUES(emergency_contact_phone), emergency_contact_phone)
        """, (
            contractor_id,
            data.get("phone"),
            data.get("address_line1"),
            data.get("address_line2"),
            data.get("postcode"),
            data.get("emergency_contact_name"),
            data.get("emergency_contact_phone"),
        ))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# -----------------------------------------------------------------------------
# Admin: contractor search and full profile
# -----------------------------------------------------------------------------


def admin_list_contractors_for_select(limit: int = 500) -> List[Dict[str, Any]]:
    """List contractors for admin dropdowns (id, name, email)."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, name, email FROM tb_contractors ORDER BY name LIMIT %s",
            (limit,),
        )
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()


def admin_search_contractors(q: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Search contractors by name, email; optionally join phone from hr_staff_details."""
    if not q or not q.strip():
        return []
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        term = "%" + q.strip() + "%"
        cur.execute("""
            SELECT c.id, c.name, c.email, c.status, h.phone
            FROM tb_contractors c
            LEFT JOIN hr_staff_details h ON h.contractor_id = c.id
            WHERE c.name LIKE %s OR c.email LIKE %s
            ORDER BY c.name
            LIMIT %s
        """, (term, term, limit))
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()


def admin_get_staff_profile(contractor_id: int) -> Optional[Dict[str, Any]]:
    """Full HR profile for admin: core + all staff_details columns."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, name, email, initials, status FROM tb_contractors WHERE id = %s",
            (contractor_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        try:
            cur.execute("""
                SELECT phone, address_line1, address_line2, postcode, emergency_contact_name, emergency_contact_phone,
                       driving_licence_number, driving_licence_expiry, driving_licence_document_path,
                       right_to_work_type, right_to_work_expiry, right_to_work_document_path,
                       dbs_level, dbs_number, dbs_expiry, dbs_document_path,
                       contract_type, contract_start, contract_end, contract_document_path, updated_at
                FROM hr_staff_details WHERE contractor_id = %s
            """, (contractor_id,))
        except Exception:
            cur.execute(
                "SELECT phone, address_line1, address_line2, postcode, emergency_contact_name, emergency_contact_phone, updated_at "
                "FROM hr_staff_details WHERE contractor_id = %s",
                (contractor_id,),
            )
        extra = cur.fetchone()
        if extra:
            row.update(extra)
        return row
    finally:
        cur.close()
        conn.close()


def admin_update_staff_profile(contractor_id: int, data: Dict[str, Any]) -> bool:
    """Update all admin-editable HR fields. Returns True if contractor exists."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM tb_contractors WHERE id = %s", (contractor_id,))
        if not cur.fetchone():
            return False
        cur.execute("""
            INSERT INTO hr_staff_details (
                contractor_id, phone, address_line1, address_line2, postcode,
                emergency_contact_name, emergency_contact_phone,
                driving_licence_number, driving_licence_expiry, driving_licence_document_path,
                right_to_work_type, right_to_work_expiry, right_to_work_document_path,
                dbs_level, dbs_number, dbs_expiry, dbs_document_path,
                contract_type, contract_start, contract_end, contract_document_path
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            )
            ON DUPLICATE KEY UPDATE
            phone = VALUES(phone), address_line1 = VALUES(address_line1), address_line2 = VALUES(address_line2),
            postcode = VALUES(postcode), emergency_contact_name = VALUES(emergency_contact_name),
            emergency_contact_phone = VALUES(emergency_contact_phone),
            driving_licence_number = VALUES(driving_licence_number), driving_licence_expiry = VALUES(driving_licence_expiry),
            driving_licence_document_path = VALUES(driving_licence_document_path),
            right_to_work_type = VALUES(right_to_work_type), right_to_work_expiry = VALUES(right_to_work_expiry),
            right_to_work_document_path = VALUES(right_to_work_document_path),
            dbs_level = VALUES(dbs_level), dbs_number = VALUES(dbs_number), dbs_expiry = VALUES(dbs_expiry),
            dbs_document_path = VALUES(dbs_document_path),
            contract_type = VALUES(contract_type), contract_start = VALUES(contract_start),
            contract_end = VALUES(contract_end), contract_document_path = VALUES(contract_document_path)
        """, (
            contractor_id,
            data.get("phone"),
            data.get("address_line1"),
            data.get("address_line2"),
            data.get("postcode"),
            data.get("emergency_contact_name"),
            data.get("emergency_contact_phone"),
            data.get("driving_licence_number"),
            _parse_date(data.get("driving_licence_expiry")),
            data.get("driving_licence_document_path"),
            data.get("right_to_work_type"),
            _parse_date(data.get("right_to_work_expiry")),
            data.get("right_to_work_document_path"),
            data.get("dbs_level"),
            data.get("dbs_number"),
            _parse_date(data.get("dbs_expiry")),
            data.get("dbs_document_path"),
            data.get("contract_type"),
            _parse_date(data.get("contract_start")),
            _parse_date(data.get("contract_end")),
            data.get("contract_document_path"),
        ))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        cur.close()
        conn.close()


def _parse_date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    try:
        if isinstance(v, datetime):
            return v.date()
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def list_document_requests(contractor_id: int) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT r.id, r.title, r.description, r.required_by_date, r.status, r.created_at, r.request_type,
                   r.approved_at, r.rejected_at, r.admin_notes,
                   (SELECT COUNT(*) FROM hr_document_uploads u WHERE u.request_id = r.id) AS upload_count
            FROM hr_document_requests r
            WHERE r.contractor_id = %s
            ORDER BY r.status IN ('pending','overdue') DESC, r.required_by_date ASC
        """, (contractor_id,))
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()


def add_upload(
    request_id: int,
    contractor_id: int,
    file_path: str,
    file_name: Optional[str] = None,
    document_type: str = "primary",
) -> int:
    """Contractor upload; allowed for pending or rejected (replacement). Sets status to 'uploaded'."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT contractor_id, status FROM hr_document_requests WHERE id = %s",
            (request_id,),
        )
        row = cur.fetchone()
        if not row or row["contractor_id"] != contractor_id:
            raise ValueError("Request not found or not yours")
        if row["status"] not in ("pending", "uploaded", "overdue", "rejected"):
            raise ValueError("Cannot upload for this request")
        doc_type = "replacement" if document_type == "replacement" else "primary"
        cur2 = conn.cursor()
        cur2.execute(
            "INSERT INTO hr_document_uploads (request_id, file_path, file_name, document_type) VALUES (%s, %s, %s, %s)",
            (request_id, file_path, file_name, doc_type),
        )
        upload_id = cur2.lastrowid
        cur2.execute(
            "UPDATE hr_document_requests SET status = 'uploaded', rejected_at = NULL, rejected_by_user_id = NULL, admin_notes = NULL WHERE id = %s",
            (request_id,),
        )
        conn.commit()
        cur2.close()
        return upload_id
    finally:
        cur.close()
        conn.close()


# -----------------------------------------------------------------------------
# Admin: document requests
# -----------------------------------------------------------------------------


def admin_list_document_requests(
    contractor_id: Optional[int] = None,
    status: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = 100,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """List requests with filters. Returns (rows, total)."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        where = ["1=1"]
        params: List[Any] = []
        if contractor_id is not None:
            where.append("r.contractor_id = %s")
            params.append(contractor_id)
        if status:
            where.append("r.status = %s")
            params.append(status)
        if date_from:
            where.append("r.required_by_date >= %s")
            params.append(date_from)
        if date_to:
            where.append("r.required_by_date <= %s")
            params.append(date_to)
        params.extend([limit, offset])
        cur.execute(f"""
            SELECT SQL_CALC_FOUND_ROWS r.id, r.contractor_id, r.title, r.description, r.required_by_date, r.status,
                   r.request_type, r.created_at, c.name AS contractor_name, c.email AS contractor_email
            FROM hr_document_requests r
            JOIN tb_contractors c ON c.id = r.contractor_id
            WHERE {" AND ".join(where)}
            ORDER BY r.required_by_date IS NULL ASC, r.required_by_date ASC, r.created_at DESC
            LIMIT %s OFFSET %s
        """, params)
        rows = cur.fetchall() or []
        cur.execute("SELECT FOUND_ROWS() AS total")
        total = (cur.fetchone() or {}).get("total") or 0
        return rows, total
    finally:
        cur.close()
        conn.close()


def admin_create_document_request(
    contractor_ids: List[int],
    title: str,
    description: Optional[str] = None,
    required_by_date: Optional[date] = None,
    request_type: str = "other",
) -> int:
    """Create one request per contractor. Returns count created."""
    if not contractor_ids or not title.strip():
        return 0
    req_type = request_type if request_type in REQUEST_TYPES else "other"
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        count = 0
        for cid in contractor_ids:
            cur.execute("""
                INSERT INTO hr_document_requests (contractor_id, title, description, required_by_date, request_type)
                VALUES (%s, %s, %s, %s, %s)
            """, (cid, title.strip()[:255], (description or "")[:65535] or None, required_by_date, req_type))
            count += cur.rowcount
        conn.commit()
        return count
    finally:
        cur.close()
        conn.close()


def admin_get_request(request_id: int) -> Optional[Dict[str, Any]]:
    """Get request with uploads for admin."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT r.*, c.name AS contractor_name, c.email AS contractor_email
            FROM hr_document_requests r
            JOIN tb_contractors c ON c.id = r.contractor_id
            WHERE r.id = %s
        """, (request_id,))
        req = cur.fetchone()
        if not req:
            return None
        cur.execute(
            "SELECT id, file_path, file_name, document_type, uploaded_at FROM hr_document_uploads WHERE request_id = %s ORDER BY uploaded_at",
            (request_id,),
        )
        req["uploads"] = cur.fetchall() or []
        return req
    finally:
        cur.close()
        conn.close()


def admin_approve_request(request_id: int, user_id: Optional[int], admin_notes: Optional[str] = None) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE hr_document_requests
            SET status = 'approved', approved_at = NOW(), approved_by_user_id = %s,
                rejected_at = NULL, rejected_by_user_id = NULL, admin_notes = %s
            WHERE id = %s
        """, (user_id, (admin_notes or "").strip() or None, request_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def admin_reject_request(request_id: int, user_id: Optional[int], admin_notes: Optional[str] = None) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE hr_document_requests
            SET status = 'rejected', rejected_at = NOW(), rejected_by_user_id = %s, admin_notes = %s,
                approved_at = NULL, approved_by_user_id = NULL
            WHERE id = %s
        """, (user_id, (admin_notes or "").strip() or None, request_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def get_expiring_documents(days: int = 30) -> List[Dict[str, Any]]:
    """List staff with documents expiring within the next N days (licence, right to work, DBS, contract end)."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        from datetime import timedelta
        today = date.today()
        end = today + timedelta(days=days)
        cur.execute("""
            SELECT contractor_id AS id, 'driving_licence' AS doc_type, driving_licence_expiry AS expiry_date
            FROM hr_staff_details WHERE driving_licence_expiry IS NOT NULL AND driving_licence_expiry BETWEEN %s AND %s
            UNION ALL
            SELECT contractor_id, 'right_to_work', right_to_work_expiry FROM hr_staff_details
            WHERE right_to_work_expiry IS NOT NULL AND right_to_work_expiry BETWEEN %s AND %s
            UNION ALL
            SELECT contractor_id, 'dbs', dbs_expiry FROM hr_staff_details
            WHERE dbs_expiry IS NOT NULL AND dbs_expiry BETWEEN %s AND %s
            UNION ALL
            SELECT contractor_id, 'contract_end', contract_end FROM hr_staff_details
            WHERE contract_end IS NOT NULL AND contract_end BETWEEN %s AND %s
            ORDER BY expiry_date
        """, (today, end, today, end, today, end, today, end))
        rows = cur.fetchall() or []
        # Attach names
        for r in rows:
            cur.execute("SELECT name, email FROM tb_contractors WHERE id = %s", (r["id"],))
            c = cur.fetchone()
            r["contractor_name"] = c.get("name") if c else ""
            r["contractor_email"] = c.get("email") if c else ""
        return rows
    except Exception:
        return []
    finally:
        cur.close()
        conn.close()


def hr_compliance_overview() -> Dict[str, Any]:
    """Count contractors with/without right to work, DBS, contract on file."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT COUNT(DISTINCT contractor_id) AS total FROM hr_staff_details")
        total_staff = (cur.fetchone() or {}).get("total") or 0
        cur.execute("SELECT COUNT(DISTINCT contractor_id) AS n FROM hr_staff_details WHERE right_to_work_expiry IS NOT NULL AND right_to_work_expiry >= CURDATE()")
        with_rtw = (cur.fetchone() or {}).get("n") or 0
        cur.execute("SELECT COUNT(DISTINCT contractor_id) AS n FROM hr_staff_details WHERE dbs_expiry IS NOT NULL AND dbs_expiry >= CURDATE()")
        with_dbs = (cur.fetchone() or {}).get("n") or 0
        cur.execute("SELECT COUNT(DISTINCT contractor_id) AS n FROM hr_staff_details WHERE contract_end IS NOT NULL AND contract_end >= CURDATE()")
        with_contract = (cur.fetchone() or {}).get("n") or 0
        total_contractors = 0
        cur.execute("SELECT COUNT(*) AS n FROM tb_contractors")
        total_contractors = (cur.fetchone() or {}).get("n") or 0
        return {
            "total_contractors": total_contractors,
            "with_right_to_work": with_rtw,
            "with_dbs": with_dbs,
            "with_contract": with_contract,
            "staff_with_hr_record": total_staff,
        }
    finally:
        cur.close()
        conn.close()


def pending_requests_count(contractor_id: int) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COUNT(*) FROM hr_document_requests WHERE contractor_id = %s AND status = 'pending'",
            (contractor_id,),
        )
        return cur.fetchone()[0] or 0
    finally:
        cur.close()
        conn.close()
