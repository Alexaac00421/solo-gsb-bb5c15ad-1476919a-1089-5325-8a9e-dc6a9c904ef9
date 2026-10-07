"""测量不确定度复核核心引擎（仅依赖标准库）。

公式为仅含加/减/乘/除的依赖图：

* ``leaf`` —— 引用一个原始测量；
* ``add``/``sub`` —— 要求两个子节点量纲完全一致（含无量纲）；
* ``mul``/``div`` —— 量纲按乘除推导，除数节点为零时拒绝整次计算。

不确定度采用一阶（GUM）传播。对目标量 Y = f(X1..Xn)：

    u²(Y) = Σᵢ Σⱼ cᵢ cⱼ u(xᵢ,xⱼ),   cᵢ = ∂Y/∂xᵢ

同一原始测量沿多条路径出现（共享测量）时，先由反向自动微分把各路径
的偏导合并成单个灵敏系数 cᵢ，再与协方差矩阵相乘，从而正确保留
多路径之间的相关性；未给出的交叉协方差按零处理。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from .units import Unit, UnitError, format_unit, parse_unit

# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class EngineError(ValueError):
    """整次计算必须拒绝的业务错误。"""

    code: str = "INVALID_REQUEST"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class GraphError(EngineError):
    code = "INVALID_GRAPH"


class MeasurementError(EngineError):
    code = "INVALID_MEASUREMENT"


class CovarianceError(EngineError):
    code = "INVALID_COVARIANCE"


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------

_OPS = ("add", "sub", "mul", "div")
_ACTIONS = {"add", "sub", "mul", "div", "leaf"}


@dataclass(frozen=True)
class Node:
    node_id: str
    action: str
    a: str | None = None
    b: str | None = None
    measurement_id: str | None = None


@dataclass(frozen=True)
class Measurement:
    measurement_id: str
    value: float
    unit: Unit
    std_uncertainty: float
    # 原始单位 -> 协调 SI 的换算倍数
    scale: float = 1.0


@dataclass
class _Compiled:
    nodes: dict[str, Node]
    order: list[str]
    used_ids: list[str]


# ---------------------------------------------------------------------------
# 图校验：结构、循环、缺失引用
# ---------------------------------------------------------------------------


def compile_graph(graph: Mapping[str, Any]) -> _Compiled:
    if not isinstance(graph, dict) or not graph:
        raise GraphError("公式依赖图必须为非空对象")

    nodes: dict[str, Node] = {}
    for nid, spec in graph.items():
        if not isinstance(nid, str) or not nid:
            raise GraphError("节点 id 必须为非空字符串")
        if not isinstance(spec, dict):
            raise GraphError(f"节点 {nid!r} 定义必须为对象")
        action = spec.get("action")
        if action not in _ACTIONS:
            raise GraphError(
                f"节点 {nid!r} 的 action 非法，允许 {sorted(_ACTIONS)}"
            )
        if action == "leaf":
            mid = spec.get("measurement_id")
            if not isinstance(mid, str) or not mid:
                raise GraphError(f"叶节点 {nid!r} 缺少 measurement_id")
            nodes[nid] = Node(nid, "leaf", measurement_id=mid)
        else:
            children = {}
            for key in ("a", "b"):
                ref = spec.get(key)
                if not isinstance(ref, str) or not ref:
                    raise GraphError(f"节点 {nid!r} 缺少子节点 {key!r}")
                children[key] = ref
            nodes[nid] = Node(nid, action, children["a"], children["b"])

    if "root" not in nodes:
        raise GraphError("依赖图必须包含 id 为 'root' 的节点")

    # 显式检查所有子节点引用存在（root 不可达的孤儿节点不参与计算）
    for nid, node in nodes.items():
        for child in (node.a, node.b):
            if child is not None and child not in nodes:
                raise GraphError(
                    f"节点 {nid!r} 引用了不存在的节点 {child!r}"
                )

    # DFS：检测循环 + 收集可达节点 + 拓扑序（后序）
    color: dict[str, int] = {}  # 0=白 1=灰(在栈中) 2=黑
    order: list[str] = []

    def visit(nid: str, chain: list[str]) -> None:
        if nid not in nodes:
            chain_desc = " -> ".join(chain + [nid])
            raise GraphError(f"节点 {nid!r} 不存在（引用链: {chain_desc}）")
        state = color.get(nid, 0)
        if state == 1:
            cycle = chain[chain.index(nid):] + [nid]
            raise GraphError("公式依赖图存在循环引用: " + " -> ".join(cycle))
        if state == 2:
            return
        color[nid] = 1
        node = nodes[nid]
        if node.action != "leaf":
            visit(node.a, chain + [nid])  # type: ignore[arg-type]
            visit(node.b, chain + [nid])  # type: ignore[arg-type]
        color[nid] = 2
        order.append(nid)

    visit("root", [])
    used_leaf_ids: list[str] = []
    for nid in order:
        if nodes[nid].action == "leaf":
            mid = nodes[nid].measurement_id
            assert mid is not None
            if mid not in used_leaf_ids:
                used_leaf_ids.append(mid)
    return _Compiled(nodes=nodes, order=order, used_ids=used_leaf_ids)


# ---------------------------------------------------------------------------
# 测量与协方差
# ---------------------------------------------------------------------------


def build_measurements(raw: list[dict[str, Any]]) -> dict[str, Measurement]:
    if not isinstance(raw, list) or not raw:
        raise MeasurementError("measurements 必须为非空数组")
    out: dict[str, Measurement] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise MeasurementError("measurement 必须为对象")
        mid = item.get("measurement_id")
        if not isinstance(mid, str) or not mid:
            raise MeasurementError("measurement 缺少 measurement_id")
        if mid in out:
            raise MeasurementError(f"measurement_id 重复: {mid!r}")
        value = item.get("value")
        unc = item.get("std_uncertainty")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise MeasurementError(f"测量 {mid!r} 的 value 必须为有限数值")
        if not isinstance(unc, (int, float)) or isinstance(unc, bool) or not math.isfinite(unc) or unc < 0:
            raise MeasurementError(f"测量 {mid!r} 的 std_uncertainty 必须为非负有限数值")
        try:
            unit = parse_unit(item.get("unit", ""))
        except UnitError as exc:
            raise MeasurementError(str(exc), code="INVALID_UNIT") from exc
        out[mid] = Measurement(
            measurement_id=mid,
            value=float(value) * unit.scale,
            unit=unit,
            std_uncertainty=float(unc) * unit.scale,
            scale=unit.scale,
        )
    return out


def _cholesky_psd(matrix: list[list[float]], tol: float) -> list[list[float]] | None:
    """返回 Cholesky 下三角 L；非半正定（在 tol 内）返回 None。"""
    n = len(matrix)
    l = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = sum(l[i][k] * l[j][k] for k in range(j))
            if i == j:
                diag = matrix[i][i] - s
                if diag < -tol:
                    return None
                if diag < 0.0:
                    diag = 0.0
                l[i][j] = math.sqrt(max(diag, 0.0))
            else:
                if l[j][j] == 0.0:
                    if abs(matrix[i][j] - s) > tol:
                        return None
                    l[i][j] = 0.0
                else:
                    l[i][j] = (matrix[i][j] - s) / l[j][j]
    return l


@dataclass
class CovarianceData:
    order: list[str]                       # 与图相关的测量顺序
    matrix: list[list[float]]             # 协调 SI 单位下的协方差
    scales: dict[str, float]


def build_covariance(
    raw: Any,
    measurements: Mapping[str, Measurement],
    used_ids: list[str],
) -> CovarianceData:
    """构造使用测量的完整协方差矩阵。

    * 对角线缺省取 u²，显式给出则必须与 u² 一致；
    * 未给出的交叉项按零处理；
    * 同一对测量重复给出、值非法、矩阵非半正定均拒绝整次计算。
    """
    entries: dict[tuple[str, str], float] = {}
    if raw is not None:
        if not isinstance(raw, list):
            raise CovarianceError("covariance 必须为数组")
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                raise CovarianceError(f"covariance[{idx}] 必须为对象")
            a = item.get("a")
            b = item.get("b")
            v = item.get("cov")
            if not isinstance(a, str) or not isinstance(b, str):
                raise CovarianceError(f"covariance[{idx}] 的 a/b 必须为 measurement_id")
            if a not in measurements or b not in measurements:
                raise CovarianceError(
                    f"covariance[{idx}] 引用了不存在的测量: {a!r}/{b!r}"
                )
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
                raise CovarianceError(f"covariance[{idx}] 的 cov 必须为有限数值")
            key = (a, b) if a <= b else (b, a)
            if key in entries:
                raise CovarianceError(f"测量对 {key[0]!r}/{key[1]!r} 的协方差重复给出")
            entries[key] = float(v)

    n = len(used_ids)
    matrix = [[0.0] * n for _ in range(n)]
    scales = {mid: measurements[mid].scale for mid in used_ids}

    for i, mi in enumerate(used_ids):
        u = measurements[mi].std_uncertainty
        matrix[i][i] = u * u

    for (a, b), cov in entries.items():
        i, j = used_ids.index(a), used_ids.index(b)
        scaled = cov * scales[a] * scales[b]
        if i == j:
            u = measurements[a].std_uncertainty
            expected = u * u
            tol = 1e-12 * max(1.0, abs(expected))
            if abs(scaled - expected) > tol:
                raise CovarianceError(
                    f"测量 {a!r} 的协方差对角线 {cov:g} 与其标准不确定度平方 "
                    f"{(u / scales[a]) ** 2:g} 不符"
                )
            # 以方差为准，消除输入侧的微小表示误差
            matrix[i][i] = expected
        else:
            matrix[i][j] = scaled
            matrix[j][i] = scaled

    # 半正定检查（Cholesky；容差按矩阵尺度）
    max_scale = max((abs(matrix[i][i]) for i in range(n)), default=0.0)
    tol = 1e-10 * max(1.0, max_scale)
    if _cholesky_psd(matrix, tol) is None:
        raise CovarianceError("协方差矩阵非半正定（PSD），整次计算已拒绝")
    return CovarianceData(order=list(used_ids), matrix=matrix, scales=scales)


# ---------------------------------------------------------------------------
# 求值 + 反向自动微分
# ---------------------------------------------------------------------------


@dataclass
class _Slot:
    value: float
    unit: Unit
    grad: float = 0.0


def _format_scaled_unit(unit: Unit) -> str:
    """格式化可能带非 1 倍数的灵敏系数单位。

    scale=1 时输出协调 SI 基本单位串；否则在量纲串前加数值倍数
    （如 scale=1000 且量纲为 A 时输出 ``1000 A``，即 W/mV = 1000·A）。
    """
    dim_text = format_unit(Unit(unit.dims, 1.0))
    if math.isclose(unit.scale, 1.0, rel_tol=0, abs_tol=0.0):
        return dim_text
    factor = _format_number(unit.scale)
    if dim_text == "1":
        return factor
    return f"{factor} {dim_text}"


def _format_number(x: float) -> str:
    if abs(x - round(x)) < 1e-12 * max(1.0, abs(x)) and abs(x) < 1e6:
        return str(int(round(x)))
    return f"{x:g}"


@dataclass
class EvaluationResult:
    value: float
    unit: Unit
    std_uncertainty: float
    sensitivities: dict[str, float] = field(default_factory=dict)
    sensitivity_units: dict[str, str] = field(default_factory=dict)
    used_measurements: list[str] = field(default_factory=list)


def evaluate(
    graph: Mapping[str, Any],
    measurements_raw: list[dict[str, Any]],
    covariance_raw: Any = None,
) -> EvaluationResult:
    compiled = compile_graph(graph)
    measurements = build_measurements(measurements_raw)

    # 图引用的测量必须全部存在
    missing = [mid for mid in compiled.used_ids if mid not in measurements]
    if missing:
        raise MeasurementError(
            "公式引用了缺失的原始测量: " + ", ".join(repr(m) for m in missing),
            code="MEASUREMENT_NOT_FOUND",
        )

    cov = build_covariance(covariance_raw, measurements, compiled.used_ids)

    slots: dict[str, _Slot] = {}

    # ---- 前向 ----
    for nid in compiled.order:
        node = compiled.nodes[nid]
        if node.action == "leaf":
            m = measurements[node.measurement_id]  # type: ignore[index]
            # 数值已换算到协调 SI，单位量纲保留、倍数归一
            slots[nid] = _Slot(m.value, Unit(m.unit.dims, 1.0))
            continue

        sa, sb = slots[node.a], slots[node.b]  # type: ignore[index]
        try:
            if node.action in ("add", "sub"):
                if not sa.unit.same_dims_as(sb.unit):
                    raise UnitError(
                        f"节点 {nid!r}: 加/减要求量纲相容，"
                        f"得到 {format_unit(sa.unit)} 与 {format_unit(sb.unit)}"
                    )
                # 子节点已换算到协调 SI，结果量纲相同且 scale=1
                unit = Unit(sa.unit.dims, 1.0)
                v = sa.value + sb.value if node.action == "add" else sa.value - sb.value
            elif node.action == "mul":
                unit = sa.unit * sb.unit
                v = sa.value * sb.value
            else:  # div
                if sb.value == 0.0:
                    raise EngineError(f"节点 {nid!r}: 分母为零，整次计算已拒绝",
                                      code="ZERO_DENOMINATOR")
                unit = sa.unit / sb.unit
                v = sa.value / sb.value
        except UnitError as exc:
            raise EngineError(str(exc), code="DIMENSION_MISMATCH") from exc
        slots[nid] = _Slot(v, unit)
    root = slots["root"]

    # ---- 反向伴随：共享节点的梯度自然累加合并 ----
    slots["root"].grad = 1.0
    for nid in reversed(compiled.order):
        node = compiled.nodes[nid]
        if node.action == "leaf":
            continue
        sa, sb = slots[node.a], slots[node.b]  # type: ignore[index]
        g = slots[nid].grad
        if node.action == "add":
            sa.grad += g
            sb.grad += g
        elif node.action == "sub":
            sa.grad += g
            sb.grad -= g
        elif node.action == "mul":
            sa.grad += g * sb.value
            sb.grad += g * sa.value
        else:  # div: v = a/b -> da=1/b, db=-a/b^2
            sa.grad += g / sb.value
            sb.grad += -g * sa.value / (sb.value * sb.value)

    # 合并共享测量：同一 measurement_id 多个叶节点的偏导求和，得到对
    # “协调 SI 输入量”的灵敏系数 c_SI。
    grad_si: dict[str, float] = {mid: 0.0 for mid in compiled.used_ids}
    for nid in compiled.order:
        node = compiled.nodes[nid]
        if node.action == "leaf":
            grad_si[node.measurement_id] += slots[nid].grad  # type: ignore[index]

    # ---- 协方差传播（先合并系数，再传播协方差）----
    # 协方差矩阵已换算到协调 SI，故传播使用 c_SI；共享测量的多路径
    # 贡献已在上方累加合并，路径间相关性因此被正确保留。
    n = len(cov.order)
    c_si = [grad_si[mid] for mid in cov.order]
    variance = 0.0
    for i in range(n):
        variance += c_si[i] * cov.matrix[i][i] * c_si[i]
        for j in range(i + 1, n):
            variance += 2.0 * c_si[i] * cov.matrix[i][j] * c_si[j]
    if variance < 0.0 and variance > -1e-10 * max(
        (abs(cov.matrix[i][i]) for i in range(n)), default=1.0
    ):
        variance = 0.0
    if variance < 0.0:
        raise CovarianceError("传播后方差为负，输入协方差数据不一致")
    std_unc = math.sqrt(variance)

    # 对外报告的灵敏系数按输入**原始报告单位下的数值**给出。输入的 SI
    # 量 x_SI = scale·x_raw，由链式法则 c_report = ∂Y/∂x_raw = c_SI·scale；
    # 其单位 = 目标协调单位 / 原始输入单位，倍数（1/scale 等）体现在单位中
    # （例如目标 W、输入 mV：c=0.01 W/mV，数值系数 0.01，单位 1000·A）。
    sensitivities: dict[str, float] = {}
    sens_units: dict[str, str] = {}
    out_unit = Unit(root.unit.dims, 1.0)
    for i, mid in enumerate(cov.order):
        m = measurements[mid]
        scale = m.scale
        sensitivities[mid] = c_si[i] * scale
        # 灵敏系数单位 = 目标协调单位 / 原始（带倍数）单位
        coeff_unit = out_unit / m.unit
        sens_units[mid] = _format_scaled_unit(coeff_unit)

    return EvaluationResult(
        value=root.value,
        unit=out_unit,
        std_uncertainty=std_unc,
        sensitivities=sensitivities,
        sensitivity_units=sens_units,
        used_measurements=list(cov.order),
    )
