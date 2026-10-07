-- 应用同时会在启动时执行 CREATE TABLE IF NOT EXISTS（幂等）；
-- 此文件保证 docker compose 首次初始化数据库时模式即就绪。

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
