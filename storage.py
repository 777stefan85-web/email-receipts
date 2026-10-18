"""Локальное хранилище обработанных писем (SQLite)."""
import json
import sqlite3
from datetime import datetime

import config


def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS processed (
            message_id   TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL,
            mail_date    TEXT,
            subject      TEXT,
            status       TEXT NOT NULL,   -- receipt | no_items | not_receipt | duplicate | error
            data_json    TEXT
        )"""
    )
    return conn


def is_processed(message_id: str) -> bool:
    # Чеки без позиций (только ссылка) разбираем заново: позиции могут появиться на сайте ОФД позже,
    # или разбор научился их доставать
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed WHERE message_id = ? AND status != 'no_items'", (message_id,)
        ).fetchone()
    return row is not None


def save(message_id: str, mail_date: str, subject: str, status: str, data: dict | None):
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO processed VALUES (?, ?, ?, ?, ?, ?)",
            (
                message_id,
                datetime.now().isoformat(timespec="seconds"),
                mail_date,
                subject,
                status,
                json.dumps(data, ensure_ascii=False) if data is not None else None,
            ),
        )


def list_entries(include_non_receipts: bool = False) -> list[dict]:
    query = "SELECT * FROM processed"
    if not include_non_receipts:
        query += " WHERE status IN ('receipt', 'no_items')"
    query += " ORDER BY processed_at DESC"
    with _connect() as conn:
        rows = conn.execute(query).fetchall()
    result = []
    for r in rows:
        entry = dict(r)
        entry["data"] = json.loads(r["data_json"]) if r["data_json"] else None
        result.append(entry)
    return result
