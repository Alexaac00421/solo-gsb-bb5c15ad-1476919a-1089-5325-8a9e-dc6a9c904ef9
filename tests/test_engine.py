"""引擎端到端测试：量纲、传播、共享路径、拒绝条件。"""
import hashlib
import math

import pytest

from app import engine
from app.errors import ServiceError
from app.models import (
    Covariance,
    FormulaNode,
    Measurement,
    PublishFormulaRequest,
    content_hash,
    formula_payload,
    input_payload,
)


def eval_(formula, measurements, covariance=()):
    cov = list(covariance)
    fh = content_hash("fh", formula_payload(formula))
    ih = content_hash("ih", input_payload(measurements, cov))
    fv = "fv_" + fh.split("_", 1)[1]
    isn = "is_" + ih.split("_", 1)[1]
    rid = "res_" + hashlib.sha256((fv + "|" + isn).encode()).hexdigest()
    return engine.evaluate(
        formula, measurements, cov,
        formula_version_id=fv, formula_hash=fh,
        input_snapshot_id=isn, input_hash=ih, result_id=rid,
    )


def mul_formula(out="y", a="a", b="b"):
    return PublishFormulaRequest(
        output=out, nodes=[FormulaNode(id=out, op="mul", operands=[a, b])]
    )


def m(mid, v, unit="", u=0.0):
    return Measurement(id=mid, value=v, unit=unit, standard_uncertainty=u)


def test_area_unit_conversion_and_uncertainty():
    f = mul_formula("A")
    r = eval_(f, [m("a", 100, "cm", 0.1), m("b", 500, "mm", 0.5)])
    assert r.value == pytest.approx(0.5)
    assert r.unit == "m^2"
    # u² = (0.005·0.1)² + (0.001·0.5)²
    assert r.standard_uncertainty == pytest.approx(
        math.hypot(0.005 * 0.1, 0.001 * 0.5)
    )
    c = {s.measurement_id: s.coefficient for s in r.sensitivities}
    assert c == {"a": pytest.approx(0.005), "b": pytest.approx(0.001)}


def test_covariance_full_positive_correlation():
    f = mul_formula("A")
    r = eval_(
        f,
        [m("a", 100, "cm", 0.1), m("b", 500, "mm", 0.5)],
        [Covariance(a="a", b="b", covariance=0.05)],
    )
    assert r.standard_uncertainty == pytest.approx(
        0.005 * 0.1 + 0.001 * 0.5
    )


def test_covariance_negative_correlation_cancels():
    # y = a + b；完全负相关时不确定度 = |u_a - u_b|
    f = PublishFormulaRequest(
        output="y", nodes=[FormulaNode(id="y", op="add", operands=["a", "b"])]
    )
    r = eval_(
        f,
        [m("a", 1.0, "m", 0.1), m("b", 2.0, "m", 0.1)],
        [Covariance(a="a", b="b", covariance=-0.01)],
    )
    assert r.value == pytest.approx(3.0)
    # 完全负相关、等方差时解析为 0；允许双精度抵消残差（~1e-9）
    assert r.standard_uncertainty == pytest.approx(0.0, abs=1e-8)


def test_add_requires_same_dimension():
    f = PublishFormulaRequest(
        output="y", nodes=[FormulaNode(id="y", op="add", operands=["a", "b"])]
    )
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("a", 1, "m"), m("b", 1, "kg")])
    assert ei.value.code == "DIMENSION_MISMATCH"


def test_add_tolerates_same_dimension_different_prefix():
    f = PublishFormulaRequest(
        output="y", nodes=[FormulaNode(id="y", op="add", operands=["a", "b"])]
    )
    r = eval_(f, [m("a", 1, "kg", 0.0), m("b", 500, "g", 0.0)])
    assert r.value == pytest.approx(1.5)
    assert r.unit == "kg"


def test_division_derives_dimension_and_sensitivity_sign():
    f = PublishFormulaRequest(
        output="v", nodes=[FormulaNode(id="v", op="div", operands=["s", "t"])]
    )
    r = eval_(f, [m("s", 100, "m", 1.0), m("t", 10, "s", 0.1)])
    assert r.value == pytest.approx(10.0)
    assert r.unit == "m/s"
    c = {x.measurement_id: x.coefficient for x in r.sensitivities}
    assert c["s"] == pytest.approx(0.1)
    assert c["t"] == pytest.approx(-1.0)
    assert r.standard_uncertainty == pytest.approx(
        math.hypot(0.1 * 1.0, 1.0 * 0.1)
    )


def test_shared_measurement_merges_sensitivity():
    # y = (x + one) * x，x 沿两条路径出现：dy/dx = 2x + one = 7
    f = PublishFormulaRequest(output="y", nodes=[
        FormulaNode(id="p", op="add", operands=["x", "one"]),
        FormulaNode(id="y", op="mul", operands=["p", "x"]),
    ])
    r = eval_(f, [m("x", 3, "m", 0.1), m("one", 1, "m", 0.0)])
    assert r.value == pytest.approx(12.0)
    c = {s.measurement_id: s.coefficient for s in r.sensitivities}
    assert c["x"] == pytest.approx(7.0)
    assert r.standard_uncertainty == pytest.approx(0.7)


def test_diamond_dag_no_double_counting_without_covariance():
    # y = (x+a)*(x-a)，x 出现两次；独立时按合并后的总灵敏系数 2x 传播一次
    f = PublishFormulaRequest(output="y", nodes=[
        FormulaNode(id="p", op="add", operands=["x", "a"]),
        FormulaNode(id="q", op="sub", operands=["x", "a"]),
        FormulaNode(id="y", op="mul", operands=["p", "q"]),
    ])
    r = eval_(f, [m("x", 5, "m", 0.1), m("a", 2, "m", 0.0)])
    assert r.value == pytest.approx(21.0)
    c = {s.measurement_id: s.coefficient for s in r.sensitivities}
    assert c["x"] == pytest.approx(10.0)  # d(x²-a²)/dx = 2x
    assert c["a"] == pytest.approx(-4.0)  # d(x²-a²)/da = -2a
    assert r.standard_uncertainty == pytest.approx(1.0)


def test_cyclic_reference_rejected():
    f = PublishFormulaRequest(output="n1", nodes=[
        FormulaNode(id="n1", op="add", operands=["n2", "x"]),
        FormulaNode(id="n2", op="mul", operands=["n1", "y"]),
    ])
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("x", 0), m("y", 0, unit="m")])
    assert ei.value.code == "CYCLIC_REFERENCE"


def test_self_cycle_rejected():
    f = PublishFormulaRequest(output="n1", nodes=[
        FormulaNode(id="n1", op="add", operands=["n1", "x"]),
    ])
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("x", 0)])
    assert ei.value.code == "CYCLIC_REFERENCE"


def test_missing_measurement_rejected():
    f = PublishFormulaRequest(
        output="y", nodes=[FormulaNode(id="y", op="add", operands=["a", "z"])]
    )
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("a", 0)])
    assert ei.value.code == "MISSING_MEASUREMENT"


def test_zero_divisor_rejected():
    f = PublishFormulaRequest(
        output="y", nodes=[FormulaNode(id="y", op="div", operands=["a", "b"])]
    )
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("a", 1, "m"), m("b", 0, "s", 0.1)])
    assert ei.value.code == "ZERO_DIVISOR"


def test_duplicate_node_rejected():
    f = PublishFormulaRequest(output="y", nodes=[
        FormulaNode(id="y", op="add", operands=["a", "b"]),
        FormulaNode(id="y", op="sub", operands=["a", "b"]),
    ])
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("a", 1, "m"), m("b", 2, "m")])
    assert ei.value.code == "DUPLICATE_NODE"


def test_duplicate_measurement_rejected():
    f = mul_formula()
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("a", 1), m("a", 2)])
    assert ei.value.code == "DUPLICATE_MEASUREMENT"


def test_unreachable_node_rejected():
    f = PublishFormulaRequest(output="y", nodes=[
        FormulaNode(id="y", op="mul", operands=["a", "b"]),
        FormulaNode(id="ghost", op="add", operands=["a", "b"]),
    ])
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("a", 1, "m"), m("b", 2, "m")])
    assert ei.value.code == "UNREACHABLE_NODE"


def test_missing_output_rejected():
    f = PublishFormulaRequest(
        output="zzz",
        nodes=[FormulaNode(id="y", op="mul", operands=["a", "b"])],
    )
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("a", 1, "m"), m("b", 2, "m")])
    assert ei.value.code == "MISSING_OUTPUT"


def test_diagonal_mismatch_rejected():
    f = mul_formula()
    with pytest.raises(ServiceError) as ei:
        eval_(
            f, [m("a", 1, "m", 1.0), m("b", 1, "m", 1.0)],
            [Covariance(a="a", b="a", covariance=2.0)],
        )
    assert ei.value.code == "COVARIANCE_DIAGONAL_MISMATCH"


def test_non_psd_rejected():
    f = mul_formula()
    with pytest.raises(ServiceError) as ei:
        eval_(
            f, [m("a", 1, "m", 1.0), m("b", 1, "m", 1.0)],
            [Covariance(a="a", b="b", covariance=2.0)],
        )
    assert ei.value.code == "COVARIANCE_NOT_PSD"


def test_singular_psd_accepted():
    # 完全相关的 2x2 矩阵秩 1，仍为半正定，必须接受
    f = PublishFormulaRequest(
        output="y", nodes=[FormulaNode(id="y", op="add", operands=["a", "b"])]
    )
    r = eval_(
        f, [m("a", 1, "m", 0.1), m("b", 1, "m", 0.1)],
        [Covariance(a="a", b="b", covariance=0.01)],
    )
    assert r.standard_uncertainty == pytest.approx(0.2)


def test_covariance_unknown_measurement_rejected():
    f = mul_formula()
    with pytest.raises(ServiceError) as ei:
        eval_(
            f, [m("a", 1, "m", 1.0), m("b", 1, "m", 1.0)],
            [Covariance(a="a", b="zz", covariance=0.0)],
        )
    assert ei.value.code == "COVARIANCE_UNKNOWN_MEASUREMENT"


def test_constants_with_zero_uncertainty():
    # F = m·a，常量无不确定度
    f = mul_formula("F", "mm", "ac")
    r = eval_(
        f,
        [m("mm", 2.0, "kg", 0.01), m("ac", 3.0, "m/s^2", 0.0)],
    )
    assert r.value == pytest.approx(6.0)
    assert r.unit == "kg.m/s^2"
    assert r.standard_uncertainty == pytest.approx(0.03)


def test_result_references_versions_and_snapshot():
    f = mul_formula("A")
    meas = [m("a", 1, "m"), m("b", 1, "m")]
    r = eval_(f, meas)
    assert r.formula_version_id.startswith("fv_")
    assert r.formula_hash.startswith("fh_")
    assert r.input_snapshot_id.startswith("is_")
    assert r.input_hash.startswith("ih_")
    assert r.result_id.startswith("res_")


def test_id_collision_rejected():
    f = PublishFormulaRequest(output="a", nodes=[
        FormulaNode(id="a", op="add", operands=["x", "y"]),
    ])
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("a", 1, "m"), m("x", 1, "m"), m("y", 1, "m")])
    assert ei.value.code == "ID_COLLISION"


def test_unused_measurement_rejected():
    f = mul_formula("A", "a", "b")
    with pytest.raises(ServiceError) as ei:
        eval_(f, [m("a", 1, "m"), m("b", 2, "m"), m("extra", 3, "m")])
    assert ei.value.code == "UNUSED_MEASUREMENT"
