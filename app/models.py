"""API 的请求/响应模型与内容寻址哈希。

所有哈希都基于规范化 JSON：UTF-8、无空白、键按排序、不允许 NaN/Infinity。
公式哈希刻意排除 ``name``（名称只是展示标签，改名不产生新版本）；
输入快照哈希包含 measurements 与 covariance 原文。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")

Operation = Literal["add", "sub", "mul", "div"]


def _validate_id(value: str) -> str:
    if not ID_PATTERN.match(value):
        raise ValueError(
            "标识须以字母或下划线开头，仅含字母/数字/下划线/点/冒号/连字符，长度 1-128"
        )
    return value


class Measurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    value: float = Field(..., description="测量值（按 unit 计）")
    unit: str = Field("", description="线性 SI 单位字符串；空串或 1 表示无量纲")
    standard_uncertainty: float = Field(
        ...,
        description="标准不确定度（与 value 同单位），非负",
        ge=0.0,
    )

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        return _validate_id(v)

    @field_validator("value", "standard_uncertainty")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("数值必须有限")
        return v


class Covariance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    a: str
    b: str
    covariance: float = Field(..., description="u(a,b)，与两个测量同单位乘积")

    @field_validator("a", "b")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        return _validate_id(v)

    @field_validator("covariance")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("协方差必须有限")
        return v


class FormulaNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    op: Operation
    operands: list[str] = Field(..., min_length=2, max_length=2)

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        return _validate_id(v)


class PublishFormulaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field("", description="公式的展示名称（不参与版本哈希）")
    output: str = Field(..., description="目标量节点 id")
    nodes: list[FormulaNode] = Field(..., min_length=1)


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    formula_version_id: str | None = Field(
        None, description="已发布公式版本；与 formula 二选一"
    )
    formula: PublishFormulaRequest | None = None
    measurements: list[Measurement] = Field(..., min_length=1)
    covariance: list[Covariance] = Field(default_factory=list)


class Sensitivity(BaseModel):
    measurement_id: str
    coefficient: float = Field(
        ..., description="灵敏系数 ∂y/∂x_i（按调用方给出的输入数值与单位）"
    )
    unit: str = Field(..., description="灵敏系数单位：输出单位/输入单位")


class EvaluateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str
    formula_version_id: str
    formula_hash: str
    input_snapshot_id: str
    input_hash: str
    output_node: str
    value: float
    unit: str
    standard_uncertainty: float
    sensitivities: list[Sensitivity]
    covariance_matrix: list[list[float]] = Field(
        ...,
        description="实际使用的协方差矩阵（coherent SI），未给交叉项按 0",
    )
    measurement_order: list[str]


class PublishFormulaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formula_version_id: str
    formula_hash: str
    created: bool


class InputSnapshotRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_snapshot_id: str
    input_hash: str
    created: bool


class HealthResponse(BaseModel):
    status: str
    database: str


def canonical_json(obj: Any) -> bytes:
    """规范化序列化：排序键、无空白、确保非标准浮点被拒绝。"""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_hash(prefix: str, payload: Any) -> str:
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    return f"{prefix}_{digest}"


def formula_payload(formula: PublishFormulaRequest) -> dict[str, Any]:
    """参与哈希的公式内容（排除展示名称）。"""
    return {
        "output": formula.output,
        "nodes": [
            {"id": n.id, "op": n.op, "operands": list(n.operands)}
            for n in formula.nodes
        ],
    }


def input_payload(
    measurements: list[Measurement], covariances: list[Covariance]
) -> dict[str, Any]:
    return {
        "measurements": [m.model_dump() for m in measurements],
        "covariance": [c.model_dump() for c in covariances],
    }
