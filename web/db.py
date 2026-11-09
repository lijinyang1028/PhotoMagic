import sqlite3
import json
from pathlib import Path
import settings

# 兼容 Linux 与 Windows，存放在用户目录下
DB_FILE = Path(settings.CONFIG_DIR) / "photomagic_web.sqlite3"

def get_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    # timeout 防止多线程并发写入时锁死
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                image_path TEXT,
                description TEXT,
                scores TEXT,
                summary TEXT,
                suggestions TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_review_user ON review_history(user_id);
        """)

def get_user_settings(user_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT data FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return json.loads(row["data"]) if row else {}

def save_user_settings(user_id: str, data: dict):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, data) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
            (user_id, json.dumps(data, ensure_ascii=False))
        )

def add_review_history(user_id: str, entry: dict):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO review_history 
               (user_id, timestamp, image_path, description, scores, summary, suggestions) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, entry["timestamp"], entry["image_path"], entry["description"],
                json.dumps(entry["scores"], ensure_ascii=False),
                entry["summary"], json.dumps(entry["suggestions"], ensure_ascii=False)
            )
        )

def get_review_history(user_id: str) -> list:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM review_history WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()
        result = []
        for r in rows:
            # 只返回文件名，避免把服务器上的绝对路径（含用户名等）泄露给客户端
            stored_path = r["image_path"] or ""
            filename = Path(stored_path).name if stored_path else ""
            result.append({
                "id": r["id"], "timestamp": r["timestamp"], "image_path": filename,
                "description": r["description"], "scores": json.loads(r["scores"]),
                "summary": r["summary"], "suggestions": json.loads(r["suggestions"])
            })
        return result

def clear_review_history(user_id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM review_history WHERE user_id = ?", (user_id,))

def get_all_users():
    """供 CLI 工具查询使用"""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT u.user_id, 
                   (SELECT COUNT(*) FROM review_history r WHERE r.user_id = u.user_id) as review_count
            FROM user_settings u
            UNION
            SELECT r.user_id, COUNT(*) 
            FROM review_history r 
            WHERE r.user_id NOT IN (SELECT user_id FROM user_settings)
            GROUP BY r.user_id
        """).fetchall()
        return [dict(r) for r in rows]