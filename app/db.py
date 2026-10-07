"""PostgreSQL 访问：模式初始化与不可变内容的幂等写入。

三张只追加表（公式版本 / 输入快照 / 计算结果），主键为内容派生的
SHA-256 版本号。并发发布通过 ON CONFLICT DO NOTHING 幂等化：
重复或并发请求返回同一行，已返回结果永不改变。
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS formula_versions (
    formula_version_id TEXT PRIMARY KEY,
    formula_hash       TEXT NOT NULL,
    name               TEXT NOT NULL DEFAULT '',
    payload            JSONB NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS input_snapshots (
    input_snapshot_id TEXT PRIMARY KEY,
    input_hash        TEXT NOT NULL,
    payload           JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS results (
    result_id          TEXT PRIMARY KEY,
    formula_version_id TEXT NOT NULL REFERENCES formula_versions,
    input_snapshot_id  TEXT NOT NULL REFERENCES input_snapshots,
    payload            JSONB NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (formula_version_id, input_snapshot_id)
);
"""


def dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://uncertainty:uncertainty@localhost:5432/uncertainty",
    )


class Database:
    def __init__(self, url: str | None = None) -> None:
        self._pool: ConnectionPool | None = None
        self._url = url or dsn()

    def connect(self, *, retries: int = 30, delay: float = 1.0) -> None:
        import time

        last: Exception | None = None
        for attempt in range(retries):
            try:
                self._pool = ConnectionPool(self._url, min_size=1, max_size=10, open=True)
                with self._pool.connection() as conn:
                    conn.execute(SCHEMA_SQL)
                return
            except Exception as exc:  # 容器内数据库可能晚于应用启动
                last = exc
                if self._pool is not None:
                    self._pool.close()
                    self._pool = None
                time.sleep(delay)
        raise RuntimeError(f"无法连接数据库：{last}")

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def ready(self) -> bool:
        if self._pool is None:
            return False
        try:
            with self._pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def _conn(self):
        if self._pool is None:
            raise RuntimeError("数据库未连接")
        return self._pool.connection()

    def upsert_formula(
        self, version_id: str, content_hash: str, name: str, payload: dict[str, Any]
    ) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO formula_versions
                    (formula_version_id, formula_hash, name, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (formula_version_id) DO NOTHING
                """,
                (version_id, content_hash, name, Jsonb(payload)),
            )
            return cur.rowcount == 1

    def get_formula(self, version_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT formula_version_id, formula_hash, name, payload, created_at "
                "FROM formula_versions WHERE formula_version_id = %s",
                (version_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "formula_version_id": row[0],
            "formula_hash": row[1],
            "name": row[2],
            "payload": row[3],
            "created_at": row[4].isoformat(),
        }

    def upsert_snapshot(
        self, snapshot_id: str, content_hash: str, payload: dict[str, Any]
    ) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO input_snapshots
                    (input_snapshot_id, input_hash, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT (input_snapshot_id) DO NOTHING
                """,
                (snapshot_id, content_hash, Jsonb(payload)),
            )
            return cur.rowcount == 1

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT input_snapshot_id, input_hash, payload, created_at "
                "FROM input_snapshots WHERE input_snapshot_id = %s",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "input_snapshot_id": row[0],
            "input_hash": row[1],
            "payload": row[2],
            "created_at": row[3].isoformat(),
        }

    def upsert_result(
        self,
        result_id: str,
        formula_version_id: str,
        snapshot_id: str,
        payload: dict[str, Any],
    ) -> bool:
        """幂等写入结果。

        并发下可能出现 result_id 相同（内容寻址必然相同）或
        (公式版本, 快照) 已存在的情况：后者捕获唯一冲突，
        由调用方回读胜出行。
        """
        with self._conn() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO results
                        (result_id, formula_version_id, input_snapshot_id, payload)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (result_id) DO NOTHING
                    """,
                    (result_id, formula_version_id, snapshot_id, Jsonb(payload)),
                )
                return cur.rowcount == 1
            except psycopg.errors.UniqueViolation:
                # (formula_version_id, input_snapshot_id) 唯一约束冲突：
                # 并发的另一个请求已经写入（其 result_id 与本请求内容一致，
                # 但连接先后导致约束抢先），回滚本条语句并交由调用方回读
                conn.rollback()
                return False

    def get_result(self, result_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload, created_at FROM results WHERE result_id = %s",
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        return {"payload": row[0], "created_at": row[1].isoformat()}

    def find_result(self, formula_version_id: str, snapshot_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT result_id FROM results "
                "WHERE formula_version_id = %s AND input_snapshot_id = %s",
                (formula_version_id, snapshot_id),
            ).fetchone()
        return row[0] if row else None
