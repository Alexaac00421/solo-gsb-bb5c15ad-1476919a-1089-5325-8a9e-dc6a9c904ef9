"""请求 / 响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Finite = Annotated[Union[StrictInt, StrictFloat], Field(description="有限数值")]


class MeasurementIn(_Strict):
    measurement_id: str = Field(min_length=1, description="原始测量唯一标识")
    value: Finite
    unit: str = Field(min_length=1, description="线性 SI 单位，例如 V、kg*m/s^2")
    std_uncertainty: float = Field(ge=0, description="标准不确定度 u(x)，与 value 同单位")


class CovarianceIn(_Strict):
    a: str = Field(min_length=1)
    b: str = Field(min_length=1)
    cov: float = Field(description="协方差 u(a,b)，单位为 a、b 原始单位之积")


class LeafNode(_Strict):
    action: Literal["leaf"]
    measurement_id: str = Field(min_length=1)


class BinaryNode(_Strict):
    action: Literal["add", "sub", "mul", "div"]
    a: str = Field(min_length=1, description="左子节点 id")
    b: str = Field(min_length=1, description="右子节点 id（div 时为分母）")


NodeIn = Annotated[Union[BinaryNode, LeafNode], Field(discriminator="action")]


class FormulaPublish(_Strict):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    graph: dict[str, NodeIn] = Field(description="依赖节点映射，必须包含 id 为 root 的节点")


class CalculationCreate(_Strict):
    measurements: list[MeasurementIn] = Field(min_length=1)
    covariance: list[CovarianceIn] | None = None
    graph: dict[str, NodeIn] | None = Field(
        default=None, description="内联公式；为空则使用 formula_id 引用的已发布公式"
    )
    formula_id: str | None = Field(default=None, description="已发布公式版本 id")


class Sensitivity(BaseModel):
    measurement_id: str
    value: float = Field(description="灵敏系数 c = ∂Y/∂x（协调 SI 单位制）")
    unit: str = Field(description="灵敏系数单位 = 目标单位 / 该测量单位")


class CalculationResult(BaseModel):
    calculation_id: str
    formula_id: str
    formula_version: str
    input_snapshot: str
    value: float
    unit: str
    std_uncertainty: float
    used_measurements: list[str]
    sensitivities: list[Sensitivity]


class FormulaResult(BaseModel):
    formula_id: str
    formula_version: str
    name: str
    description: str
    graph: dict
