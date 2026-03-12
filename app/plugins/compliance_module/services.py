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
            "SELECT id, title, slug, summary, body, file_path, file_name, version, effective_from, effective_to, required_acknowledgement FROM compliance_policies WHERE slug = %s AND active = 1",
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


# -----------------------------------------------------------------------------
# Admin: policies CRUD and acknowledgements list
# -----------------------------------------------------------------------------


def list_policies_admin(active_only: bool = False) -> List[Dict[str, Any]]:
    """List all policies for admin; optional filter by active=1."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        where = "active = 1" if active_only else "1=1"
        cur.execute(f"""
            SELECT id, title, slug, summary, file_path, file_name, version, effective_from, effective_to, required_acknowledgement, active, created_at
            FROM compliance_policies
            WHERE {where}
            ORDER BY effective_from DESC, id DESC
        """)
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()


def get_policy_by_id(policy_id: int) -> Optional[Dict[str, Any]]:
    """Get policy by id for admin edit (includes active, body)."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, title, slug, summary, body, file_path, file_name, version, effective_from, effective_to, required_acknowledgement, active FROM compliance_policies WHERE id = %s",
            (policy_id,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


def create_policy(
    title: str,
    slug: str,
    summary: Optional[str] = None,
    body: Optional[str] = None,
    file_path: Optional[str] = None,
    file_name: Optional[str] = None,
    version: int = 1,
    effective_from: Optional[date] = None,
    effective_to: Optional[date] = None,
    required_acknowledgement: bool = True,
    active: bool = True,
) -> int:
    """Create a policy. Returns new id. Raises on duplicate slug."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO compliance_policies (title, slug, summary, body, file_path, file_name, version, effective_from, effective_to, required_acknowledgement, active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            title, slug, (summary or "").strip() or None, (body or "").strip() or None,
            (file_path or "").strip() or None, (file_name or "").strip() or None,
            version, effective_from, effective_to, 1 if required_acknowledgement else 0, 1 if active else 0,
        ))
        conn.commit()
        return cur.lastrowid
    finally:
        cur.close()
        conn.close()


def update_policy(
    policy_id: int,
    title: Optional[str] = None,
    slug: Optional[str] = None,
    summary: Optional[str] = None,
    body: Optional[str] = None,
    file_path: Optional[str] = None,
    file_name: Optional[str] = None,
    version: Optional[int] = None,
    effective_from: Optional[date] = None,
    effective_to: Optional[date] = None,
    required_acknowledgement: Optional[bool] = None,
    active: Optional[bool] = None,
) -> bool:
    """Update policy. Only provided fields are updated."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        updates = []
        params: List[Any] = []
        for k, v in [
            ("title", title), ("slug", slug), ("summary", summary), ("body", body),
            ("file_path", file_path), ("file_name", file_name),
            ("version", version), ("effective_from", effective_from), ("effective_to", effective_to),
        ]:
            if v is not None:
                updates.append(f"{k} = %s")
                params.append(None if k in ("file_path", "file_name") and v == "" else v)
        if required_acknowledgement is not None:
            updates.append("required_acknowledgement = %s")
            params.append(1 if required_acknowledgement else 0)
        if active is not None:
            updates.append("active = %s")
            params.append(1 if active else 0)
        if not updates:
            return True
        params.append(policy_id)
        cur.execute(f"UPDATE compliance_policies SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def list_acknowledgements(
    policy_id: Optional[int] = None,
    contractor_id: Optional[int] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """List acknowledgements with policy title and contractor name."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        where = ["1=1"]
        params: List[Any] = []
        if policy_id is not None:
            where.append("a.policy_id = %s")
            params.append(policy_id)
        if contractor_id is not None:
            where.append("a.contractor_id = %s")
            params.append(contractor_id)
        params.append(limit)
        cur.execute(f"""
            SELECT a.id, a.policy_id, a.contractor_id, a.acknowledged_at, a.ip_address, a.user_agent,
                   p.title AS policy_title, p.slug AS policy_slug,
                   c.name AS contractor_name, c.email AS contractor_email
            FROM compliance_acknowledgements a
            JOIN compliance_policies p ON p.id = a.policy_id
            JOIN tb_contractors c ON c.id = a.contractor_id
            WHERE {" AND ".join(where)}
            ORDER BY a.acknowledged_at DESC
            LIMIT %s
        """, params)
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()


def list_contractors_for_select(limit: int = 500) -> List[Dict[str, Any]]:
    """List contractors id, name for admin dropdowns (e.g. acknowledgements filter)."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, name, email FROM tb_contractors WHERE status = 'active' ORDER BY name LIMIT %s",
            (limit,),
        )
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()
