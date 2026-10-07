"""FastAPI 入口与 HTTP 路由。

版本与不变性约定：

* ``POST /api/v1/formulas`` 发布不可改公式版本（内容寻址哈希）；
* ``POST /api/v1/evaluations`` 执行复核；可直接内联公式，服务端自动
  幂等发布公式版本与输入快照，结果固定引用二者；
* 所有写操作对相同内容返回同一 id，并发安全（ON CONFLICT DO NOTHING）。
"""

from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import engine
from .db import Database
from .errors import ServiceError
from .models import (
    EvaluateRequest,
    EvaluateResponse,
    HealthResponse,
    PublishFormulaRequest,
    PublishFormulaResponse,
    content_hash,
    formula_payload,
    input_payload,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database()
    if os.environ.get("DISABLE_DB", "").lower() in ("1", "true", "yes"):
        app.state.db = None
    else:
        db.connect()
        app.state.db = db
    try:
        yield
    finally:
        db.close()


app = FastAPI(
    title="实验室测量不确定度复核服务",
    version="1.0.0",
    description=(
        "发布加减乘除公式依赖图，对带线性 SI 单位与标准不确定度的原始测量"
        "进行量纲推导与协方差不确定度传播。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ServiceError)
async def _service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """请求结构/类型校验失败统一为业务错误信封（HTTP 仍为 422）。"""
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    detail = first.get("msg", "请求参数不合法")
    message = f"{loc}: {detail}" if loc else detail
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "VALIDATION_ERROR", "message": message}},
    )


def _require_db(request: Request) -> Database:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise ServiceError("DATABASE_UNAVAILABLE", "数据库未启用/不可用", 503)
    return db


def _publish_formula(db: Database | None, formula: PublishFormulaRequest) -> tuple[str, str, bool]:
    fhash = content_hash("fh", formula_payload(formula))
    version_id = "fv_" + fhash.split("_", 1)[1]
    if db is None:
        return version_id, fhash, False
    created = db.upsert_formula(version_id, fhash, formula.name, formula.model_dump())
    return version_id, fhash, created


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health(request: Request) -> HealthResponse:
    db = getattr(request.app.state, "db", None)
    if db is None:
        return HealthResponse(status="ok", database="disabled")
    return HealthResponse(
        status="ok", database="up" if db.ready() else "down"
    )


@app.post(
    "/api/v1/formulas",
    response_model=PublishFormulaResponse,
    status_code=201,
    tags=["formulas"],
)
def publish_formula_route(
    body: PublishFormulaRequest, request: Request
) -> PublishFormulaResponse:
    db = _require_db(request)
    version_id, fhash, created = _publish_formula(db, body)
    return PublishFormulaResponse(
        formula_version_id=version_id, formula_hash=fhash, created=created
    )


@app.get("/api/v1/formulas/{formula_version_id}", tags=["formulas"])
def get_formula_route(formula_version_id: str, request: Request):
    db = _require_db(request)
    row = db.get_formula(formula_version_id)
    if row is None:
        raise ServiceError("NOT_FOUND", "公式版本不存在", 404)
    return row


def _load_formula(db: Database, version_id: str) -> PublishFormulaRequest:
    row = db.get_formula(version_id)
    if row is None:
        raise ServiceError("FORMULA_NOT_FOUND", f"公式版本 {version_id} 不存在", 404)
    return PublishFormulaRequest.model_validate(row["payload"])


@app.post(
    "/api/v1/evaluations",
    response_model=EvaluateResponse,
    status_code=201,
    tags=["evaluations"],
)
def evaluate_route(body: EvaluateRequest, request: Request) -> EvaluateResponse:
    if (body.formula is None) == (body.formula_version_id is None):
        raise ServiceError(
            "INVALID_REQUEST",
            "formula 与 formula_version_id 必须且只能提供一个",
        )

    db = getattr(request.app.state, "db", None)

    if body.formula is not None:
        formula = body.formula
    else:
        if db is None:
            raise ServiceError("DATABASE_UNAVAILABLE", "数据库未启用/不可用", 503)
        formula = _load_formula(db, body.formula_version_id)

    fversion_id, fhash, _ = _publish_formula(db, formula)

    ipayload = input_payload(body.measurements, body.covariance)
    ihash = content_hash("ih", ipayload)
    snapshot_id = "is_" + ihash.split("_", 1)[1]
    result_id = "res_" + hashlib.sha256(
        (fversion_id + "|" + snapshot_id).encode("utf-8")
    ).hexdigest()

    # 计算在任何写库动作之前完成：非法输入直接 4xx，不产生任何版本数据
    response = engine.evaluate(
        formula,
        body.measurements,
        body.covariance,
        formula_version_id=fversion_id,
        formula_hash=fhash,
        input_snapshot_id=snapshot_id,
        input_hash=ihash,
        result_id=result_id,
    )

    if db is not None:
        db.upsert_snapshot(snapshot_id, ihash, ipayload)
        existing = db.find_result(fversion_id, snapshot_id)
        if existing is not None:
            # 相同公式版本 + 相同输入快照，返回固定结果
            stored = db.get_result(existing)
            if stored is not None:
                return EvaluateResponse.model_validate(stored["payload"])
        db.upsert_result(
            result_id,
            fversion_id,
            snapshot_id,
            response.model_dump(),
        )
        # 并发下可能落败，以数据库最终行为为准
        winner = db.find_result(fversion_id, snapshot_id)
        if winner is not None and winner != result_id:
            stored = db.get_result(winner)
            if stored is not None:
                return EvaluateResponse.model_validate(stored["payload"])

    return response


@app.get("/api/v1/evaluations/{result_id}", tags=["evaluations"])
def get_result_route(result_id: str, request: Request) -> EvaluateResponse:
    db = _require_db(request)
    row = db.get_result(result_id)
    if row is None:
        raise ServiceError("NOT_FOUND", "计算结果不存在", 404)
    return EvaluateResponse.model_validate(row["payload"])


@app.get("/api/v1/snapshots/{input_snapshot_id}", tags=["snapshots"])
def get_snapshot_route(input_snapshot_id: str, request: Request):
    db = _require_db(request)
    row = db.get_snapshot(input_snapshot_id)
    if row is None:
        raise ServiceError("NOT_FOUND", "输入快照不存在", 404)
    return row
