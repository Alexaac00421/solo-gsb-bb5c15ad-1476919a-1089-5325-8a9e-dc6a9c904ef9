"""PostgreSQL 访问层（psycopg 3 连接池）。

发布采用 upsert + 内容哈希：同一依赖图并发发布只会得到同一个不可变版本，
已返回结果不会被后续发布改变。
"""
from __future__ import annotations

import os
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS formulas (
    formula_id      UUID PRIMARY KEY,
    formula_version TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    graph           JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calculations (
    calculation_id UUID PRIMARY KEY,
    formula_id     UUID NOT NULL REFERENCES formulas(formula_id),
    formula_version TEXT NOT NULL,
    input_snapshot TEXT NOT NULL,
    request        JSONB NOT NULL,
    result         JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_calculations_formula
    ON calculations(formula_id);
"""


def dsn() -> str:
    return os.getenv(
        "DATABASE_DSN",
        "host=db port=5432 dbname=uncertainty user=lab password=labpass",
    )


def wait_for_postgres(retries: int = 60, delay: float = 1.0) -> None:
    """容器启动阶段等待 Postgres 就绪。"""
    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            with psycopg.connect(dsn(), connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError as exc:
            last_exc = exc
            time.sleep(delay)
    raise RuntimeError(f"无法连接 PostgreSQL: {last_exc}")


def make_pool():  # noqa: ANN201
    from psycopg_pool import ConnectionPool

    return ConnectionPool(
        dsn(),
        min_size=int(os.getenv("DB_POOL_MIN", "1")),
        max_size=int(os.getenv("DB_POOL_MAX", "10")),
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=True,
    )


def init_schema(pool) -> None:
    with pool.connection() as conn:
        conn.execute(SCHEMA_SQL)


def upsert_formula(
    conn, formula_id: str, version: str, name: str, description: str, graph: dict
) -> dict[str, Any]:
    """按公式内容哈希幂等发布，返回持久化行（含已存在版本）。"""
    row = conn.execute(
        """
        INSERT INTO formulas (formula_id, formula_version, name, description, graph)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (formula_version) DO UPDATE
            SET formula_version = EXCLUDED.formula_version
        RETURNING formula_id, formula_version, name, description, graph
        """,
        (formula_id, version, name, description, Jsonb(graph)),
    ).fetchone()
    return row


def get_formula(conn, formula_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT formula_id, formula_version, name, description, graph "
        "FROM formulas WHERE formula_id = %s",
        (formula_id,),
    ).fetchone()


def list_formulas(conn, limit: int = 100) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT formula_id, formula_version, name, description, graph, created_at "
        "FROM formulas ORDER BY created_at DESC LIMIT %s",
        (limit,),
    ).fetchall()


def insert_calculation(
    conn,
    calculation_id: str,
    formula_id: str,
    formula_version: str,
    input_snapshot: str,
    request: dict,
    result: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO calculations
            (calculation_id, formula_id, formula_version, input_snapshot,
             request, result)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (calculation_id, formula_id, formula_version, input_snapshot,
         Jsonb(request), Jsonb(result)),
    )


def get_calculation(conn, calculation_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT calculation_id, formula_id, formula_version, input_snapshot, "
        "request, result, created_at "
        "FROM calculations WHERE calculation_id = %s",
        (calculation_id,),
    ).fetchone()
