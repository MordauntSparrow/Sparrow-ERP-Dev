"""
Training module: items, assignments, completions.
"""
from datetime import date
from typing import Any, Dict, List, Optional
from app.objects import get_db_connection


class TrainingService:
    @staticmethod
    def list_items(active_only: bool = True) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            where = "active = 1" if active_only else "1=1"
            cur.execute(f"SELECT * FROM training_items WHERE {where} ORDER BY title")
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_item(item_id: int) -> Optional[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM training_items WHERE id = %s", (item_id,))
            return cur.fetchone()
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_item_by_slug(slug: str) -> Optional[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM training_items WHERE slug = %s AND active = 1", (slug,))
            return cur.fetchone()
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def create_item(
        title: str,
        slug: str,
        summary: Optional[str] = None,
        content: Optional[str] = None,
        item_type: str = "document",
        external_url: Optional[str] = None,
    ) -> int:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO training_items (title, slug, summary, content, item_type, external_url, active)
                VALUES (%s, %s, %s, %s, %s, %s, 1)
            """, (title, slug, summary or None, content or None, item_type, external_url))
            conn.commit()
            return cur.lastrowid
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def update_item(
        item_id: int,
        title: Optional[str] = None,
        slug: Optional[str] = None,
        summary: Optional[str] = None,
        content: Optional[str] = None,
        item_type: Optional[str] = None,
        external_url: Optional[str] = None,
        active: Optional[bool] = None,
    ) -> bool:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            updates = []
            params: List[Any] = []
            for k, v in [
                ("title", title), ("slug", slug), ("summary", summary), ("content", content),
                ("item_type", item_type), ("external_url", external_url),
            ]:
                if v is not None:
                    updates.append(f"{k} = %s")
                    params.append(v)
            if active is not None:
                updates.append("active = %s")
                params.append(1 if active else 0)
            if not updates:
                return True
            params.append(item_id)
            cur.execute(f"UPDATE training_items SET {', '.join(updates)} WHERE id = %s", params)
            conn.commit()
            return cur.rowcount > 0
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def list_assignments(
        contractor_id: Optional[int] = None,
        training_item_id: Optional[int] = None,
        include_completed: bool = True,
    ) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            where = ["1=1"]
            params: List[Any] = []
            if contractor_id is not None:
                where.append("a.contractor_id = %s")
                params.append(contractor_id)
            if training_item_id is not None:
                where.append("a.training_item_id = %s")
                params.append(training_item_id)
            cur.execute(f"""
                SELECT a.*, t.title, t.slug, t.summary, t.item_type, t.external_url,
                       u.name AS contractor_name, u.email AS contractor_email,
                       (SELECT 1 FROM training_completions c WHERE c.assignment_id = a.id LIMIT 1) AS completed
                FROM training_assignments a
                JOIN training_items t ON t.id = a.training_item_id
                JOIN tb_contractors u ON u.id = a.contractor_id
                WHERE {" AND ".join(where)}
                ORDER BY a.assigned_at DESC
            """, params)
            rows = cur.fetchall() or []
            if not include_completed:
                rows = [r for r in rows if not r.get("completed")]
            return rows
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_assignment(assignment_id: int, contractor_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            where = "a.id = %s"
            params: List[Any] = [assignment_id]
            if contractor_id is not None:
                where += " AND a.contractor_id = %s"
                params.append(contractor_id)
            cur.execute(f"""
                SELECT a.*, t.title, t.slug, t.summary, t.content, t.item_type, t.external_url,
                       u.name AS contractor_name
                FROM training_assignments a
                JOIN training_items t ON t.id = a.training_item_id
                JOIN tb_contractors u ON u.id = a.contractor_id
                WHERE {where}
            """, params)
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("SELECT id, completed_at, notes FROM training_completions WHERE assignment_id = %s", (assignment_id,))
            comp = cur.fetchone()
            row["completed"] = comp is not None
            row["completed_at"] = comp["completed_at"] if comp else None
            row["completion_notes"] = comp.get("notes") if comp else None
            return row
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def add_assignment(
        training_item_id: int,
        contractor_id: int,
        due_date: Optional[date] = None,
        mandatory: bool = False,
        assigned_by_user_id: Optional[int] = None,
    ) -> int:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO training_assignments (training_item_id, contractor_id, due_date, mandatory, assigned_by_user_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (training_item_id, contractor_id, due_date, 1 if mandatory else 0, assigned_by_user_id))
            conn.commit()
            return cur.lastrowid
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def mark_complete(assignment_id: int, contractor_id: int, notes: Optional[str] = None) -> bool:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM training_assignments WHERE id = %s AND contractor_id = %s", (assignment_id, contractor_id))
            if not cur.fetchone():
                return False
            cur.execute("INSERT INTO training_completions (assignment_id, notes) VALUES (%s, %s) ON DUPLICATE KEY UPDATE notes = VALUES(notes)", (assignment_id, notes))
            conn.commit()
            return cur.rowcount > 0
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def list_completions(
        training_item_id: Optional[int] = None,
        contractor_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            where = ["1=1"]
            params: List[Any] = []
            if training_item_id is not None:
                where.append("a.training_item_id = %s")
                params.append(training_item_id)
            if contractor_id is not None:
                where.append("a.contractor_id = %s")
                params.append(contractor_id)
            if date_from is not None:
                where.append("c.completed_at >= %s")
                params.append(date_from)
            if date_to is not None:
                where.append("c.completed_at <= %s")
                params.append(date_to)
            cur.execute(f"""
                SELECT c.id, c.assignment_id, c.completed_at, c.notes,
                       a.contractor_id, a.training_item_id, a.due_date,
                       t.title AS item_title, u.name AS contractor_name
                FROM training_completions c
                JOIN training_assignments a ON a.id = c.assignment_id
                JOIN training_items t ON t.id = a.training_item_id
                JOIN tb_contractors u ON u.id = a.contractor_id
                WHERE {" AND ".join(where)}
                ORDER BY c.completed_at DESC
            """, params)
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def list_contractors() -> List[Dict[str, Any]]:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, name, email, initials FROM tb_contractors WHERE status = 'active' ORDER BY name")
            return cur.fetchall() or []
        finally:
            cur.close()
            conn.close()
