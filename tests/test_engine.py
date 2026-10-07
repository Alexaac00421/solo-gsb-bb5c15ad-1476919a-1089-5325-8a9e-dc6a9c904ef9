"""pytest 单元测试：单位解析与不确定度引擎（无需数据库）。"""
from __future__ import annotations

import math

import pytest

from app.engine import (
    CovarianceError,
    EngineError,
    GraphError,
    MeasurementError,
    evaluate,
)
from app.units import UnitError, format_unit, parse_unit


# ---------------- 单位 ----------------


@pytest.mark.parametrize(
    "text,dims,scale",
    [
        ("m", (1, 0, 0, 0, 0, 0, 0), 1.0),
        ("kg", (0, 1, 0, 0, 0, 0, 0), 1.0),
        ("g", (0, 1, 0, 0, 0, 0, 0), 1e-3),
        ("mm", (1, 0, 0, 0, 0, 0, 0), 1e-3),
        ("V", (2, 1, -3, -1, 0, 0, 0), 1.0),
        ("N", (1, 1, -2, 0, 0, 0, 0), 1.0),
        ("Pa", (-1, 1, -2, 0, 0, 0, 0), 1.0),
        ("kg*m/s^2", (1, 1, -2, 0, 0, 0, 0), 1.0),
        ("kg m s^-2", (1, 1, -2, 0, 0, 0, 0), 1.0),
        ("kg/(m*s^2)", (-1, 1, -2, 0, 0, 0, 0), 1.0),
        ("1", (0, 0, 0, 0, 0, 0, 0), 1.0),
        ("μF", (-2, -1, 4, 2, 0, 0, 0), 1e-6),
        ("kΩ", (2, 1, -3, -2, 0, 0, 0), 1e3),
        ("(m/s)^2", (2, 0, -2, 0, 0, 0, 0), 1.0),
        ("Hz", (0, 0, -1, 0, 0, 0, 0), 1.0),
    ],
)
def test_parse_valid_units(text, dims, scale):
    u = parse_unit(text)
    assert u.dims == dims
    assert u.scale == scale


@pytest.mark.parametrize(
    "text",
    ["degC", "L", "l", "min", "°", "%", "kg**2", "m/", "(m", "m)",
     "m^", "m + s", "", "2m", "mol/L", "celsius", "tonne"],
)
def test_parse_invalid_units(text):
    with pytest.raises(UnitError):
        parse_unit(text)


def test_format_and_arithmetic():
    assert format_unit(parse_unit("N") * parse_unit("m")) == "m^2 kg s^-2"
    assert format_unit(parse_unit("V") / parse_unit("A")) == "m^2 kg s^-3 A^-2"


# ---------------- 引擎 ----------------

POWER_GRAPH = {
    "root": {"action": "mul", "a": "nU", "b": "nI"},
    "nU": {"action": "leaf", "measurement_id": "U"},
    "nI": {"action": "leaf", "measurement_id": "I"},
}
POWER_MEAS = [
    {"measurement_id": "U", "value": 10.0, "unit": "V", "std_uncertainty": 0.01},
    {"measurement_id": "I", "value": 1.0, "unit": "A", "std_uncertainty": 0.005},
]


def test_power_with_covariance():
    r = evaluate(POWER_GRAPH, POWER_MEAS,
                 [{"a": "U", "b": "I", "cov": 0.00002}])
    assert r.value == 10.0
    assert format_unit(r.unit) == "m^2 kg s^-3"
    assert r.sensitivities["U"] == pytest.approx(1.0)
    assert r.sensitivities["I"] == pytest.approx(10.0)
    var = 0.01**2 + 100 * 0.005**2 + 2 * 10 * 0.00002
    assert r.std_uncertainty == pytest.approx(math.sqrt(var))
    assert r.sensitivity_units["U"] == "A"


def test_diagonal_only_when_no_covariance():
    r = evaluate(POWER_GRAPH, POWER_MEAS)
    var = 0.01**2 + 100 * 0.005**2
    assert r.std_uncertainty == pytest.approx(math.sqrt(var))


def test_shared_measurement_merges_gradients():
    # y = (x + x) * x = 2x^2 ; dy/dx = 4x = 12 at x=3
    g = {
        "root": {"action": "mul", "a": "s", "b": "lx2"},
        "s": {"action": "add", "a": "lx0", "b": "lx1"},
        "lx0": {"action": "leaf", "measurement_id": "x"},
        "lx1": {"action": "leaf", "measurement_id": "x"},
        "lx2": {"action": "leaf", "measurement_id": "x"},
    }
    m = [{"measurement_id": "x", "value": 3.0, "unit": "m",
          "std_uncertainty": 0.1}]
    r = evaluate(g, m)
    assert r.value == 18.0
    assert r.sensitivities["x"] == pytest.approx(12.0)
    assert r.std_uncertainty == pytest.approx(1.2)


def test_shared_measurement_with_covariance_paths():
    # y = x*x （同一测量走两条路径），u(y) 必须等于 2x*u(x) 而非 sqrt(2)*x*u(x)
    g = {
        "root": {"action": "mul", "a": "l1", "b": "l2"},
        "l1": {"action": "leaf", "measurement_id": "x"},
        "l2": {"action": "leaf", "measurement_id": "x"},
    }
    m = [{"measurement_id": "x", "value": 4.0, "unit": "1",
          "std_uncertainty": 0.1}]
    r = evaluate(g, m)
    assert r.value == 16.0
    assert r.sensitivities["x"] == pytest.approx(8.0)
    assert r.std_uncertainty == pytest.approx(0.8)


def test_add_sub_dimension_compatible_with_scaling():
    g = {"root": {"action": "sub", "a": "la", "b": "lb"},
         "la": {"action": "leaf", "measurement_id": "a"},
         "lb": {"action": "leaf", "measurement_id": "b"}}
    m = [
        {"measurement_id": "a", "value": 1.0, "unit": "m", "std_uncertainty": 0.01},
        {"measurement_id": "b", "value": 50.0, "unit": "cm", "std_uncertainty": 5.0},
    ]
    r = evaluate(g, m)
    assert r.value == pytest.approx(0.5)
    assert r.std_uncertainty == pytest.approx(math.sqrt(0.01**2 + 0.05**2))
    assert format_unit(r.unit) == "m"


def test_div_gradients_and_zero_denominator():
    g = {"root": {"action": "div", "a": "la", "b": "lb"},
         "la": {"action": "leaf", "measurement_id": "a"},
         "lb": {"action": "leaf", "measurement_id": "b"}}
    r = evaluate(g, [
        {"measurement_id": "a", "value": 10.0, "unit": "m", "std_uncertainty": 0.1},
        {"measurement_id": "b", "value": 2.0, "unit": "s", "std_uncertainty": 0.01},
    ])
    assert r.value == 5.0
    assert r.sensitivities["a"] == pytest.approx(0.5)
    assert r.sensitivities["b"] == pytest.approx(-2.5)
    with pytest.raises(EngineError) as ei:
        evaluate(g, [
            {"measurement_id": "a", "value": 10.0, "unit": "m", "std_uncertainty": 0.1},
            {"measurement_id": "b", "value": 0.0, "unit": "s", "std_uncertainty": 0.01},
        ])
    assert ei.value.code == "ZERO_DENOMINATOR"


def test_dimension_mismatch_rejected():
    g = {"root": {"action": "add", "a": "la", "b": "lb"},
         "la": {"action": "leaf", "measurement_id": "a"},
         "lb": {"action": "leaf", "measurement_id": "b"}}
    with pytest.raises(EngineError) as ei:
        evaluate(g, [
            {"measurement_id": "a", "value": 1.0, "unit": "m", "std_uncertainty": 0.01},
            {"measurement_id": "b", "value": 1.0, "unit": "s", "std_uncertainty": 0.01},
        ])
    assert ei.value.code == "DIMENSION_MISMATCH"


def test_cycle_rejected():
    g = {"root": {"action": "add", "a": "n1", "b": "n2"},
         "n1": {"action": "add", "a": "root", "b": "n2"},
         "n2": {"action": "leaf", "measurement_id": "x"}}
    with pytest.raises(GraphError):
        evaluate(g, [{"measurement_id": "x", "value": 1, "unit": "1",
                      "std_uncertainty": 0.1}])


def test_missing_measurement_and_node():
    g = {"root": {"action": "leaf", "measurement_id": "zzz"}}
    with pytest.raises(MeasurementError) as ei:
        evaluate(g, [{"measurement_id": "x", "value": 1, "unit": "1",
                      "std_uncertainty": 0.1}])
    assert ei.value.code == "MEASUREMENT_NOT_FOUND"

    g2 = {"root": {"action": "add", "a": "x", "b": "y"},
          "x": {"action": "leaf", "measurement_id": "x"}}
    with pytest.raises(GraphError):
        evaluate(g2, [{"measurement_id": "x", "value": 1, "unit": "1",
                       "std_uncertainty": 0.1}])


def test_covariance_diagonal_mismatch_and_psd():
    with pytest.raises(CovarianceError):
        evaluate(POWER_GRAPH, POWER_MEAS,
                 [{"a": "U", "b": "U", "cov": 0.99}])
    # rho = 2 违反 |rho|<=1
    with pytest.raises(CovarianceError):
        evaluate(POWER_GRAPH, POWER_MEAS,
                 [{"a": "U", "b": "I", "cov": 0.0001}])
    # 重复给同一对
    with pytest.raises(CovarianceError):
        evaluate(POWER_GRAPH, POWER_MEAS, [
            {"a": "U", "b": "I", "cov": 0.0},
            {"a": "I", "b": "U", "cov": 0.0},
        ])


def test_unit_conversion_with_covariance_scaling():
    # U=10000 mV, I=1000 mA, cov=20 (mV·mA)
    m = [
        {"measurement_id": "U", "value": 10000.0, "unit": "mV", "std_uncertainty": 10.0},
        {"measurement_id": "I", "value": 1000.0, "unit": "mA", "std_uncertainty": 5.0},
    ]
    r = evaluate(POWER_GRAPH, m, [{"a": "U", "b": "I", "cov": 20.0}])
    assert r.value == pytest.approx(10.0)
    var = 0.01**2 + 100 * 0.005**2 + 2 * 10 * 0.00002
    assert r.std_uncertainty == pytest.approx(math.sqrt(var), rel=1e-9)
    # 报告系数按原始单位数值：c_U(W/mV)=0.001，c_I(W/mA)=0.01
    assert r.sensitivities["U"] == pytest.approx(0.001)
    assert r.sensitivities["I"] == pytest.approx(0.01)
    assert r.sensitivity_units["U"] == "1000 A"
    assert r.sensitivity_units["I"] == "1000 m^2 kg s^-3 A^-1"


def test_non_coherent_units_compose_to_coherent_si():
    # 密度 rho = 1000 g / 1000 cm^3 = 1000 kg/m^3
    g = {"root": {"action": "div", "a": "lm", "b": "lv"},
         "lm": {"action": "leaf", "measurement_id": "m"},
         "lv": {"action": "leaf", "measurement_id": "V"}}
    m = [
        {"measurement_id": "m", "value": 1000.0, "unit": "g", "std_uncertainty": 1.0},
        {"measurement_id": "V", "value": 1000.0, "unit": "cm^3", "std_uncertainty": 5.0},
    ]
    r = evaluate(g, m)
    assert r.value == pytest.approx(1000.0)
    assert format_unit(r.unit) == "m^-3 kg"
    # 报告系数按原始单位数值：每 1 g 改变 1 (kg/m^3)，每 1 cm^3 改变 -1
    assert r.sensitivities["m"] == pytest.approx(1.0)
    assert r.sensitivities["V"] == pytest.approx(-1.0)
    assert r.sensitivity_units["m"] == "1000 m^-3"
    assert r.sensitivity_units["V"] == "1000000 m^-6 kg"
    # SI 空间：c_m=1000 m^-3 ×u_m=1e-3 kg；c_V=-1e6 ×u_V=5e-6 m^3
    assert r.std_uncertainty == pytest.approx(math.sqrt(1.0**2 + 5.0**2))


def test_non_coherent_addition_and_newton():
    g_add = {"root": {"action": "add", "a": "a", "b": "b"},
             "a": {"action": "leaf", "measurement_id": "x"},
             "b": {"action": "leaf", "measurement_id": "y"}}
    r = evaluate(g_add, [
        {"measurement_id": "x", "value": 100.0, "unit": "cm", "std_uncertainty": 1.0},
        {"measurement_id": "y", "value": 200.0, "unit": "cm", "std_uncertainty": 2.0},
    ])
    assert r.value == pytest.approx(3.0)
    assert format_unit(r.unit) == "m"

    # F = m*d/t^2: 1000 g * 100 cm / s^2 = 1 N（t 为共享测量，走两条路径）
    g_n = {"root": {"action": "div", "a": "p", "b": "t2"},
           "p": {"action": "mul", "a": "lm", "b": "lc"},
           "t2": {"action": "mul", "a": "t1", "b": "t2b"},
           "t1": {"action": "leaf", "measurement_id": "t"},
           "t2b": {"action": "leaf", "measurement_id": "t"},
           "lm": {"action": "leaf", "measurement_id": "m"},
           "lc": {"action": "leaf", "measurement_id": "d"}}
    r = evaluate(g_n, [
        {"measurement_id": "m", "value": 1000.0, "unit": "g", "std_uncertainty": 0.0},
        {"measurement_id": "d", "value": 100.0, "unit": "cm", "std_uncertainty": 0.0},
        {"measurement_id": "t", "value": 1.0, "unit": "s", "std_uncertainty": 0.01},
    ])
    assert r.value == pytest.approx(1.0)
    assert format_unit(r.unit) == "m kg s^-2"
    # t 沿两条路径出现，合并后 c_t = -2 m d / t^3 = -2（SI）
    assert r.sensitivities["t"] == pytest.approx(-2.0)
