"""SQLite 持久化：上传记录、校验任务与问题明细。

应用启动时 init_db() 自动建表。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator, Optional

DB_PATH = os.environ.get("SUB_DB_PATH", "/data/subtitles.db")


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """启动初始化：创建全部表（幂等）。"""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filename        TEXT NOT NULL,
                format          TEXT NOT NULL DEFAULT 'vtt',
                content         TEXT NOT NULL,
                size_bytes      INTEGER NOT NULL,
                language        TEXT NOT NULL DEFAULT 'unknown',
                cue_count       INTEGER NOT NULL DEFAULT 0,
                created_at      REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS analyses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id       INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
                max_chars       INTEGER NOT NULL,
                min_dwell_ms    INTEGER NOT NULL,
                cps_max         REAL NOT NULL,
                merge_gap_ms    INTEGER NOT NULL,
                result_json     TEXT NOT NULL,
                revised_vtt     TEXT NOT NULL,
                language        TEXT NOT NULL,
                error_count     INTEGER NOT NULL DEFAULT 0,
                warning_count   INTEGER NOT NULL DEFAULT 0,
                created_at      REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS issues (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id     INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
                cue_index       INTEGER NOT NULL,
                type            TEXT NOT NULL,
                severity        TEXT NOT NULL,
                language        TEXT NOT NULL,
                message         TEXT NOT NULL,
                detail_json     TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_issues_lang_type
                ON issues(language, type);
            CREATE INDEX IF NOT EXISTS idx_analyses_upload ON analyses(upload_id);
            """
        )


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def create_upload(filename: str, fmt: str, content: str, language: str, cue_count: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO uploads (filename, format, content, size_bytes, language, cue_count, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (filename, fmt, content, len(content.encode("utf-8")), language, cue_count, time.time()),
        )
        return cur.lastrowid


def create_analysis(
    upload_id: int,
    params: dict,
    result_dict: dict,
    revised_vtt: str,
    language: str,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO analyses (upload_id, max_chars, min_dwell_ms, cps_max, merge_gap_ms,"
            " result_json, revised_vtt, language, error_count, warning_count, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                upload_id,
                params["max_chars"],
                params["min_dwell_ms"],
                params["cps_max"],
                params["merge_gap_ms"],
                json.dumps(result_dict, ensure_ascii=False),
                revised_vtt,
                language,
                result_dict["stats"]["error_count"],
                result_dict["stats"]["warning_count"],
                time.time(),
            ),
        )
        analysis_id = cur.lastrowid

        rows = [
            (
                analysis_id,
                iss["cue_index"],
                iss["type"],
                iss["severity"],
                language,
                iss["message"],
                json.dumps(iss.get("detail", {}), ensure_ascii=False),
            )
            for iss in result_dict["issues"]
        ]
        conn.executemany(
            "INSERT INTO issues (analysis_id, cue_index, type, severity, language, message, detail_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return analysis_id


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def get_analysis(analysis_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            return None
        result = json.loads(row["result_json"])
        result["id"] = row["id"]
        result["upload_id"] = row["upload_id"]
        result["params"] = {
            "max_chars": row["max_chars"],
            "min_dwell_ms": row["min_dwell_ms"],
            "cps_max": row["cps_max"],
            "merge_gap_ms": row["merge_gap_ms"],
        }
        return result


def get_revised_vtt(analysis_id: int) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT revised_vtt FROM analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
        return row["revised_vtt"] if row else None


def list_issues(language: Optional[str] = None, issue_type: Optional[str] = None) -> list:
    sql = (
        "SELECT i.id, i.analysis_id, i.cue_index, i.type, i.severity, i.language,"
        " i.message, i.detail_json, a.created_at AS analyzed_at, u.filename"
        " FROM issues i"
        " JOIN analyses a ON a.id = i.analysis_id"
        " JOIN uploads u ON u.id = a.upload_id"
    )
    where, params = [], []
    if language and language != "all":
        where.append("i.language = ?")
        params.append(language)
    if issue_type and issue_type != "all":
        where.append("i.type = ?")
        params.append(issue_type)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.id DESC LIMIT 500"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json"))
        out.append(d)
    return out


def list_recent_analyses(limit: int = 20) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT a.id, a.upload_id, a.language, a.error_count, a.warning_count,"
            " a.max_chars, a.min_dwell_ms, a.cps_max, a.created_at, u.filename"
            " FROM analyses a JOIN uploads u ON u.id = a.upload_id"
            " ORDER BY a.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def language_facets() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT language, COUNT(*) AS n FROM issues GROUP BY language ORDER BY n DESC"
        ).fetchall()
    return [dict(r) for r in rows]
