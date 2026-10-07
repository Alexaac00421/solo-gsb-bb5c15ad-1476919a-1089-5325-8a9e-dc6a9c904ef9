"""实验室测量不确定度复核服务 —— FastAPI 入口。"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import db as database
from .engine import EngineError, evaluate
from .hashing import formula_hash, input_snapshot_hash
from .schemas import (
    CalculationCreate,
    CalculationResult,
    FormulaPublish,
    FormulaResult,
)
from .units import format_unit


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.wait_for_postgres()
    pool = database.make_pool()
    database.init_schema(pool)
    app.state.pool = pool
    if os.getenv("SEED_DEMO", "1") == "1":
        from .seed import seed_demo

        seed_demo(pool)
    try:
        yield
    finally:
        pool.close()


app = FastAPI(
    title="实验室测量不确定度复核服务",
    version="1.0.0",
    description=(
        "发布加/减/乘/除公式依赖图并执行一阶(GUM)不确定度传播；"
        "公式版本不可改，结果固定引用公式版本与输入快照。"
    ),
    lifespan=lifespan,
)


def pool_from(request: Request):
    return request.app.state.pool


def _graph_to_plain(graph: dict) -> dict:
    # Pydantic 模型 -> 可序列化 dict（与入库 / 哈希内容一致）
    return {nid: node.model_dump() for nid, node in graph.items()}


def _row_to_formula(row: dict) -> dict:
    return {
        "formula_id": str(row["formula_id"]),
        "formula_version": row["formula_version"],
        "name": row["name"],
        "description": row["description"],
        "graph": row["graph"],
    }


@app.exception_handler(EngineError)
async def engine_error_handler(_: Request, exc: EngineError) -> JSONResponse:
    """业务校验失败：整次计算拒绝，不落库、不返回部分结果。"""
    status_code = 404 if exc.code == "FORMULA_NOT_FOUND" else 422
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/formulas", response_model=FormulaResult, status_code=201)
def publish_formula(payload: FormulaPublish, pool=Depends(pool_from)) -> dict:
    graph = _graph_to_plain(payload.graph)
    version = formula_hash(graph)
    formula_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"formula:{version}"))
    with pool.connection() as conn:
        row = database.upsert_formula(
            conn,
            formula_id=formula_id,
            version=version,
            name=payload.name,
            description=payload.description,
            graph=graph,
        )
    return _row_to_formula(row)


@app.get("/api/v1/formulas", response_model=list[FormulaResult])
def list_formulas(pool=Depends(pool_from)) -> list[dict]:
    with pool.connection() as conn:
        rows = database.list_formulas(conn)
    return [_row_to_formula(row) for row in rows]


@app.get("/api/v1/formulas/{formula_id}", response_model=FormulaResult)
def get_formula(formula_id: str, pool=Depends(pool_from)) -> dict:
    try:
        uuid.UUID(formula_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="formula not found") from exc
    with pool.connection() as conn:
        row = database.get_formula(conn, formula_id)
    if row is None:
        raise HTTPException(status_code=404, detail="formula not found")
    return _row_to_formula(row)


@app.post("/api/v1/calculations", response_model=CalculationResult, status_code=201)
def create_calculation(payload: CalculationCreate, pool=Depends(pool_from)) -> dict:
    inline_graph = _graph_to_plain(payload.graph) if payload.graph is not None else None

    with pool.connection() as conn:
        if inline_graph is not None:
            # 内联公式同样不可变发布；相同图复用既有版本
            version = formula_hash(inline_graph)
            fid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"formula:{version}"))
            row = database.upsert_formula(
                conn,
                formula_id=fid,
                version=version,
                name=f"(inline {version[:12]})",
                description="内联公式，由计算请求自动发布，不可修改。",
                graph=inline_graph,
            )
            graph = row["graph"]
            formula_id = str(row["formula_id"])
            formula_version = row["formula_version"]
        else:
            if not payload.formula_id:
                raise EngineError(
                    "graph 与 formula_id 至少提供一个",
                    code="FORMULA_REQUIRED",
                )
            try:
                uuid.UUID(payload.formula_id)
            except ValueError as exc:
                raise EngineError(
                    f"formula_id={payload.formula_id} 不是合法 UUID",
                    code="FORMULA_NOT_FOUND",
                ) from exc
            row = database.get_formula(conn, payload.formula_id)
            if row is None:
                raise EngineError(
                    f"formula_id={payload.formula_id} 不存在",
                    code="FORMULA_NOT_FOUND",
                )
            graph = row["graph"]
            formula_id = str(row["formula_id"])
            formula_version = row["formula_version"]

    measurements = [m.model_dump() for m in payload.measurements]
    covariance = (
        [c.model_dump() for c in payload.covariance]
        if payload.covariance is not None else None
    )

    # 任何引擎错误（循环/缺失/零分母/非法单位/对角线不符/非PSD/量纲不相容）
    # 都会经异常处理器整体拒绝
    result = evaluate(graph, measurements, covariance)

    snapshot = input_snapshot_hash(measurements, covariance)
    calculation_id = str(uuid.uuid4())
    body = {
        "calculation_id": calculation_id,
        "formula_id": formula_id,
        "formula_version": formula_version,
        "input_snapshot": snapshot,
        "value": result.value,
        "unit": format_unit(result.unit),
        "std_uncertainty": result.std_uncertainty,
        "used_measurements": result.used_measurements,
        "sensitivities": [
            {
                "measurement_id": mid,
                "value": result.sensitivities[mid],
                "unit": result.sensitivity_units[mid],
            }
            for mid in result.used_measurements
        ],
    }

    stored_request = payload.model_dump(mode="json")
    with pool.connection() as conn:
        database.insert_calculation(
            conn,
            calculation_id=calculation_id,
            formula_id=formula_id,
            formula_version=formula_version,
            input_snapshot=snapshot,
            request=stored_request,
            result=body,
        )
    return body


@app.get("/api/v1/calculations/{calculation_id}", response_model=CalculationResult)
def get_calculation(calculation_id: str, pool=Depends(pool_from)) -> dict:
    try:
        uuid.UUID(calculation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="calculation not found") from exc
    with pool.connection() as conn:
        row = database.get_calculation(conn, calculation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="calculation not found")
    return row["result"]
