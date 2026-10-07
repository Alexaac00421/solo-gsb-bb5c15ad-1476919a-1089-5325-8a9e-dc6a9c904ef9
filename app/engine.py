"""不确定度传播引擎。

流程（任一环节失败则拒绝整次计算，不产生部分结果）：

1. 结构校验：节点 id 唯一、操作数恰好 2 个、引用必须可解析、
   从 output 可达子图无环；未被 output 可达的节点拒绝（防隐藏错误）。
2. 输入校验：测量 id 唯一、单位可解析为线性 SI。
3. 协方差矩阵：稀疏三角 -> 完整矩阵，核对对角线，检验半正定，
   并换算到 coherent SI。
4. 前向求值：加减要求量纲相容；乘除推导量纲；除零拒绝。
5. 反向自动微分：共享测量沿多条路径的贡献在偏导中自然累加合并，
   再统一做 ``u²(y) = cᵀ C c`` 协方差传播。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import covariance as covmod
from . import units as um
from .errors import error
from .models import (
    Covariance,
    EvaluateRequest,
    EvaluateResponse,
    Measurement,
    PublishFormulaRequest,
    Sensitivity,
)
from .units import Unit


@dataclass
class _Node:
    id: str
    op: str
    operands: list[str]


def _structure_check(formula: PublishFormulaRequest) -> tuple[dict[str, _Node], list[str]]:
    """校验图结构，返回节点表与从 output 出发的后序遍历序列。"""
    nodes: dict[str, _Node] = {}
    for n in formula.nodes:
        if n.id in nodes:
            raise error("DUPLICATE_NODE", f"公式节点 id 重复：{n.id!r}")
        nodes[n.id] = _Node(n.id, n.op, list(n.operands))

    if formula.output not in nodes:
        raise error(
            "MISSING_OUTPUT",
            f"目标量节点 {formula.output!r} 不在节点列表中",
        )

    # 迭代 DFS：颜色 0 未访问 / 1 在栈中 / 2 已完成；后序列表即拓扑序
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    order: list[str] = []
    # (node_id, 下一个待处理操作数下标)
    stack: list[tuple[str, int]] = [(formula.output, 0)]
    color[formula.output] = GRAY

    while stack:
        cur, k = stack[-1]
        node = nodes[cur]
        if k < len(node.operands):
            stack[-1] = (cur, k + 1)
            ref = node.operands[k]
            if ref not in nodes:
                # 引用原始测量或悬空引用，在输入解析阶段区分
                color.setdefault(ref, WHITE)
                continue
            state = color.get(ref, WHITE)
            if state == GRAY:
                raise error(
                    "CYCLIC_REFERENCE",
                    f"公式依赖图存在循环引用：{ref!r} 已在求值路径上",
                )
            if state == WHITE:
                color[ref] = GRAY
                stack.append((ref, 0))
        else:
            color[cur] = BLACK
            order.append(cur)
            stack.pop()

    unreachable = [nid for nid in nodes if color.get(nid, WHITE) != BLACK]
    if unreachable:
        raise error(
            "UNREACHABLE_NODE",
            f"存在目标量不可达的公式节点：{sorted(unreachable)}",
        )
    return nodes, order


def evaluate(
    formula: PublishFormulaRequest,
    measurements: list[Measurement],
    covariances: list[Covariance],
    *,
    formula_version_id: str,
    formula_hash: str,
    input_snapshot_id: str,
    input_hash: str,
    result_id: str,
) -> EvaluateResponse:
    nodes, order = _structure_check(formula)

    # ---- 输入测量 ------------------------------------------------------
    seen: set[str] = set()
    measure: dict[str, Measurement] = {}
    parsed_unit: dict[str, Unit] = {}
    coherent_value: dict[str, float] = {}

    for m in measurements:
        if m.id in seen:
            raise error("DUPLICATE_MEASUREMENT", f"测量 id 重复：{m.id!r}")
        seen.add(m.id)
        u = um.parse_unit(m.unit)
        measure[m.id] = m
        parsed_unit[m.id] = u
        coherent_value[m.id] = m.value * u.scale

    # 操作数引用解析（此时才区分测量缺失与节点缺失）
    referenced: set[str] = set()
    for node in nodes.values():
        for ref in node.operands:
            if ref not in nodes and ref not in measure:
                raise error(
                    "MISSING_MEASUREMENT",
                    f"操作数 {ref!r} 既不是公式节点也不是已知测量",
                )
            if ref in measure:
                referenced.add(ref)

    # id 冲突：节点优先解析会让同名测量被静默遮蔽
    for mid in measure:
        if mid in nodes:
            raise error(
                "ID_COLLISION",
                f"测量 id {mid!r} 与公式节点同名，引用将产生歧义",
            )

    # 未被目标量引用的测量（可能是拼写错误），拒绝以保证复核严格性
    unused = sorted(set(measure) - referenced)
    if unused:
        raise error(
            "UNUSED_MEASUREMENT",
            f"存在未被公式目标量引用的测量：{unused}",
        )

    ids = [m.id for m in measurements]
    scales = [parsed_unit[k].scale for k in ids]
    variances = [measure[k].standard_uncertainty ** 2 for k in ids]
    raw_cov = [(c.a, c.b, c.covariance) for c in covariances]
    cov = covmod.build_matrix(len(ids), raw_cov, ids, variances, scales)

    # ---- 前向求值（数值均在 coherent SI 空间） -------------------------
    value: dict[str, float] = dict(coherent_value)
    unit_of: dict[str, Unit] = {k: Unit(1.0, parsed_unit[k].dim) for k in ids}

    for nid in order:
        node = nodes[nid]
        a, b = node.operands
        va, vb = value[a], value[b]
        ua, ub = unit_of[a], unit_of[b]
        if not (math.isfinite(va) and math.isfinite(vb)):
            raise error("NONFINITE_RESULT", f"节点 {nid!r} 的操作数非有限值")
        if node.op == "add":
            um.check_compatible(ua, ub, "add")
            value[nid], unit_of[nid] = va + vb, ua
        elif node.op == "sub":
            um.check_compatible(ua, ub, "sub")
            value[nid], unit_of[nid] = va - vb, ua
        elif node.op == "mul":
            value[nid], unit_of[nid] = va * vb, um.multiply(ua, ub)
        else:  # div
            if vb == 0.0:
                raise error("ZERO_DIVISOR", f"节点 {nid!r} 除法分母为零")
            value[nid], unit_of[nid] = va / vb, um.divide(ua, ub)

    out = formula.output
    out_value = value[out]
    out_unit = unit_of[out]
    if not math.isfinite(out_value):
        raise error("NONFINITE_RESULT", "目标量结果非有限值")

    # ---- 反向自动微分：合并所有路径的灵敏系数 --------------------------
    grad: dict[str, float] = {nid: 0.0 for nid in value}
    grad[out] = 1.0
    for nid in reversed(order):
        node = nodes[nid]
        a, b = node.operands
        g = grad[nid]
        if node.op == "add":
            grad[a] += g
            grad[b] += g
        elif node.op == "sub":
            grad[a] += g
            grad[b] -= g
        elif node.op == "mul":
            grad[a] += g * value[b]
            grad[b] += g * value[a]
        else:  # div：y = a / b
            grad[a] += g / value[b]
            grad[b] -= g * value[a] / (value[b] ** 2)

    # coherent 空间灵敏系数向量（与 cov 矩阵对齐）
    c_coh = [grad.get(k, 0.0) for k in ids]

    # ---- 协方差传播 u² = cᵀ C c ---------------------------------------
    variance = 0.0
    n = len(ids)
    for i in range(n):
        for j in range(n):
            variance += c_coh[i] * cov[i][j] * c_coh[j]
    if variance < 0.0 and variance > -1e-12 * max(1.0, abs(out_value) ** 2):
        variance = 0.0  # 吸收舍入导致的微小负值
    if variance < 0.0:
        raise error(
            "COVARIANCE_NOT_PSD",
            f"传播后方差为负（{variance}），协方差矩阵可能非半正定",
        )
    uncertainty = math.sqrt(variance)

    # 输入空间灵敏系数（按调用方给出的数值/单位计），单位 = 输出/输入
    sensitivities: list[Sensitivity] = []
    for k, mid in enumerate(ids):
        coeff = c_coh[k] * scales[k]
        in_unit = parsed_unit[mid]
        ratio_dim = tuple(out_unit.dim[j] - in_unit.dim[j] for j in range(7))
        ratio_text = um.render_dim(ratio_dim)
        literal = measure[mid].unit.strip()
        if literal and literal != "1":
            unit_text = f"{ratio_text} / ({literal})"
        else:
            unit_text = ratio_text
        sensitivities.append(
            Sensitivity(
                measurement_id=mid,
                coefficient=coeff,
                unit=unit_text,
            )
        )

    return EvaluateResponse(
        result_id=result_id,
        formula_version_id=formula_version_id,
        formula_hash=formula_hash,
        input_snapshot_id=input_snapshot_id,
        input_hash=input_hash,
        output_node=out,
        value=out_value,
        unit=um.render(out_unit),
        standard_uncertainty=uncertainty,
        sensitivities=sensitivities,
        covariance_matrix=cov,
        measurement_order=ids,
    )
