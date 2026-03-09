from datetime import date
from typing import Any, Dict, List, Optional
from app.objects import get_db_connection


def list_policies_for_staff(contractor_id: int, include_ack_status: bool = True) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        today = date.today()
        cur.execute("""
            SELECT p.id, p.title, p.slug, p.summary, p.version, p.effective_from, p.effective_to, p.required_acknowledgement
            FROM compliance_policies p
            WHERE p.active = 1
              AND p.effective_from <= %s
              AND (p.effective_to IS NULL OR p.effective_to >= %s)
            ORDER BY p.effective_from DESC
        """, (today, today))
        rows = cur.fetchall() or []
        if include_ack_status and rows:
            cur.execute(
                "SELECT policy_id FROM compliance_acknowledgements WHERE contractor_id = %s",
                (contractor_id,),
            )
            acked = {r["policy_id"] for r in (cur.fetchall() or [])}
            for r in rows:
                r["acknowledged"] = r["id"] in acked
        return rows
    finally:
        cur.close()
        conn.close()


def get_policy(slug: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, title, slug, summary, body, version, effective_from, effective_to, required_acknowledgement FROM compliance_policies WHERE slug = %s AND active = 1",
            (slug,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


def is_acknowledged(policy_id: int, contractor_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT 1 FROM compliance_acknowledgements WHERE policy_id = %s AND contractor_id = %s",
            (policy_id, contractor_id),
        )
        return bool(cur.fetchone())
    finally:
        cur.close()
        conn.close()


def acknowledge_policy(policy_id: int, contractor_id: int, ip_address: Optional[str] = None, user_agent: Optional[str] = None) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO compliance_acknowledgements (policy_id, contractor_id, ip_address, user_agent)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE acknowledged_at = CURRENT_TIMESTAMP, ip_address = VALUES(ip_address), user_agent = VALUES(user_agent)
        """, (policy_id, contractor_id, (ip_address or "")[:64], (user_agent or "")[:255]))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


def pending_policies_count(contractor_id: int) -> int:
    policies = list_policies_for_staff(contractor_id)
    return sum(1 for p in policies if p.get("required_acknowledgement") and not p.get("acknowledged"))
