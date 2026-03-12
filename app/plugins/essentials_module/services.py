"""
Essentials module: documents (raw text or file) for staff to view on the portal.
"""
from typing import Any, Dict, List, Optional
from app.objects import get_db_connection


def list_documents(active_only: bool = True) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        where = "active = 1" if active_only else "1=1"
        cur.execute(f"""
            SELECT id, title, slug, summary, content, file_path, file_name, display_order, created_at
            FROM essential_documents
            WHERE {where}
            ORDER BY display_order ASC, id ASC
        """)
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()


def get_document_by_id(doc_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, title, slug, summary, content, file_path, file_name, active, display_order, created_at, updated_at FROM essential_documents WHERE id = %s",
            (doc_id,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


def get_document_by_slug(slug: str, active_only: bool = True) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        where = "slug = %s AND active = 1" if active_only else "slug = %s"
        cur.execute(
            f"SELECT id, title, slug, summary, content, file_path, file_name, created_at FROM essential_documents WHERE {where}",
            (slug,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


def create_document(
    title: str,
    slug: str,
    summary: Optional[str] = None,
    content: Optional[str] = None,
    file_path: Optional[str] = None,
    file_name: Optional[str] = None,
    active: bool = True,
    display_order: int = 0,
) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO essential_documents (title, slug, summary, content, file_path, file_name, active, display_order)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            title, slug, (summary or "").strip() or None, (content or "").strip() or None,
            (file_path or "").strip() or None, (file_name or "").strip() or None,
            1 if active else 0, display_order,
        ))
        conn.commit()
        return cur.lastrowid
    finally:
        cur.close()
        conn.close()


def update_document(
    doc_id: int,
    title: Optional[str] = None,
    slug: Optional[str] = None,
    summary: Optional[str] = None,
    content: Optional[str] = None,
    file_path: Optional[str] = None,
    file_name: Optional[str] = None,
    active: Optional[bool] = None,
    display_order: Optional[int] = None,
) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        updates = []
        params: List[Any] = []
        for k, v in [
            ("title", title), ("slug", slug), ("summary", summary), ("content", content),
            ("file_path", file_path), ("file_name", file_name), ("display_order", display_order),
        ]:
            if v is not None:
                updates.append(f"{k} = %s")
                params.append(v)
        if active is not None:
            updates.append("active = %s")
            params.append(1 if active else 0)
        if not updates:
            return True
        params.append(doc_id)
        cur.execute(f"UPDATE essential_documents SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def delete_document(doc_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM essential_documents WHERE id = %s", (doc_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()
