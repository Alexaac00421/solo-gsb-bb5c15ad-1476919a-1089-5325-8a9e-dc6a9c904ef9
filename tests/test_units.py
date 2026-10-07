"""单位解析与量纲推导测试。"""
import math

import pytest

from app import units as um
from app.errors import ServiceError


@pytest.mark.parametrize(
    "text,scale,dim",
    [
        ("", 1.0, (0, 0, 0, 0, 0, 0, 0)),
        ("1", 1.0, (0, 0, 0, 0, 0, 0, 0)),
        ("m", 1.0, (1, 0, 0, 0, 0, 0, 0)),
        ("kg", 1.0, (0, 1, 0, 0, 0, 0, 0)),
        ("g", 1e-3, (0, 1, 0, 0, 0, 0, 0)),
        ("mm", 1e-3, (1, 0, 0, 0, 0, 0, 0)),
        ("cm", 1e-2, (1, 0, 0, 0, 0, 0, 0)),
        ("km", 1e3, (1, 0, 0, 0, 0, 0, 0)),
        ("m/s", 1.0, (1, 0, -1, 0, 0, 0, 0)),
        ("m/s^2", 1.0, (1, 0, -2, 0, 0, 0, 0)),
        ("N.m", 1.0, (2, 1, -2, 0, 0, 0, 0)),
        ("kPa", 1e3, (-1, 1, -2, 0, 0, 0, 0)),
        ("L", 1e-3, (3, 0, 0, 0, 0, 0, 0)),
        ("mL", 1e-6, (3, 0, 0, 0, 0, 0, 0)),
        ("t", 1e3, (0, 1, 0, 0, 0, 0, 0)),
        ("MHz", 1e6, (0, 0, -1, 0, 0, 0, 0)),
        ("deg", math.pi / 180, (0, 0, 0, 0, 0, 0, 0)),
        ("bar", 1e5, (-1, 1, -2, 0, 0, 0, 0)),
        ("eV", 1.602176634e-19, (2, 1, -2, 0, 0, 0, 0)),
    ],
)
def test_parse_units(text, scale, dim):
    u = um.parse_unit(text)
    assert u.dim == dim
    assert u.scale == pytest.approx(scale)


@pytest.mark.parametrize("bad", ["°C", "°F", "dB", "%", "xx", "m^", "s/", "m//s", "k", "m^x"])
def test_reject_units(bad):
    with pytest.raises(ServiceError) as ei:
        um.parse_unit(bad)
    assert ei.value.code == "INVALID_UNIT"


def test_dimension_arithmetic():
    m = um.parse_unit("m")
    kg = um.parse_unit("kg")
    s = um.parse_unit("s")
    assert um.multiply(m, kg).dim == (1, 1, 0, 0, 0, 0, 0)
    assert um.divide(m, s).dim == (1, 0, -1, 0, 0, 0, 0)
    um.check_compatible(m, m, "add")
    with pytest.raises(ServiceError) as ei:
        um.check_compatible(m, kg, "add")
    assert ei.value.code == "DIMENSION_MISMATCH"


def test_render():
    assert um.render(um.parse_unit("N")) == "kg.m/s^2"
    assert um.render(um.parse_unit("")) == "1"
    assert um.render(um.parse_unit("V")) == "kg.m^2/s^3.A"
