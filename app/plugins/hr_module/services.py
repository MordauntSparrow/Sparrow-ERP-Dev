from typing import Any, Dict, List, Optional
from app.objects import get_db_connection


def get_staff_profile(contractor_id: int) -> Dict[str, Any]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, name, email, initials FROM tb_contractors WHERE id = %s",
            (contractor_id,),
        )
        row = cur.fetchone() or {}
        cur.execute(
            "SELECT phone, address_line1, address_line2, postcode, emergency_contact_name, emergency_contact_phone FROM hr_staff_details WHERE contractor_id = %s",
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


def list_document_requests(contractor_id: int) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT r.id, r.title, r.description, r.required_by_date, r.status, r.created_at,
                   (SELECT COUNT(*) FROM hr_document_uploads u WHERE u.request_id = r.id) AS upload_count
            FROM hr_document_requests r
            WHERE r.contractor_id = %s
            ORDER BY r.status = 'pending' DESC, r.required_by_date ASC
        """, (contractor_id,))
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()


def add_upload(request_id: int, contractor_id: int, file_path: str, file_name: Optional[str] = None) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT contractor_id FROM hr_document_requests WHERE id = %s", (request_id,))
        row = cur.fetchone()
        if not row or row[0] != contractor_id:
            raise ValueError("Request not found or not yours")
        cur.execute(
            "INSERT INTO hr_document_uploads (request_id, file_path, file_name) VALUES (%s, %s, %s)",
            (request_id, file_path, file_name),
        )
        upload_id = cur.lastrowid
        cur.execute(
            "UPDATE hr_document_requests SET status = 'uploaded' WHERE id = %s",
            (request_id,),
        )
        conn.commit()
        return upload_id
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
