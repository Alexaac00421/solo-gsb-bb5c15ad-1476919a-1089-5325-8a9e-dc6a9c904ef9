"""线性 SI 单位解析与量纲推导。

一个单位在内部表示为 ``Unit(scale, dim)``：

* ``dim``：7 个 SI 基本量纲的整数指数向量
  ``[长度, 质量, 时间, 电流, 热力学温度, 物质的量, 发光强度]``；
* ``scale``：相对 coherent SI 单位（即 dim 对应的基本单位组合）的正线性比例。
  例如 ``g`` 为 ``1e-3 kg``，``cm`` 为 ``1e-2 m``，``bar`` 为 ``1e5 Pa``。

只支持线性单位（无仿射零点），因此摄氏度/华氏度、分贝等不予接受；
升、吨、巴、电子伏、rad/sr 等可表示为线性 SI 比例，予以接受。
单位字符串文法（同时接受 ``*``、``·`` 与 ``.`` 作为乘号）：

    unit     := factor*
    factor   := token ('^' signed_int)?
    token    := 可选 SI 前缀 + 已知单位符号
    除法按从左到右、每段单独求逆（无括号，避免 a/b/c 歧义）：
        m/s^2  ->  m * s^-2
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .errors import error

# 7 个基本量纲的下标
_LEN, _MASS, _TIME, _CUR, _TEMP, _AMOUNT, _LUM = range(7)


def _v(*, length=0, mass=0, time=0, current=0, temperature=0, amount=0, lumen=0):
    """按关键字构造量纲向量。"""
    return [length, mass, time, current, temperature, amount, lumen]


# SI 词头（符号 -> 比例）。da 单独处理（两字符）。
PREFIXES: dict[str, float] = {
    "Q": 1e30, "R": 1e27, "Y": 1e24, "Z": 1e21, "E": 1e18, "P": 1e15,
    "T": 1e12, "G": 1e9, "M": 1e6, "k": 1e3, "h": 1e2, "da": 1e1,
    "d": 1e-1, "c": 1e-2, "m": 1e-3, "u": 1e-6, "µ": 1e-6,
    "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18, "z": 1e-21,
    "y": 1e-24, "r": 1e-27, "q": 1e-30,
}

# 基本单位
_BASE: dict[str, tuple[float, list[int]]] = {
    "m": (1.0, _v(length=1)),
    "g": (1e-3, _v(mass=1)),            # 基本量纲质量以 kg 为 coherent 单位
    "s": (1.0, _v(time=1)),
    "A": (1.0, _v(current=1)),
    "K": (1.0, _v(temperature=1)),
    "mol": (1.0, _v(amount=1)),
    "cd": (1.0, _v(lumen=1)),
}

# 具有专用名称的导出单位与可接受的常用线性单位（符号 -> (比例, 量纲)）
_DERIVED: dict[str, tuple[float, list[int]]] = {
    "rad": (1.0, _v()),
    "sr": (1.0, _v()),
    "Hz": (1.0, _v(time=-1)),
    "N": (1.0, _v(length=1, mass=1, time=-2)),
    "Pa": (1.0, _v(length=-1, mass=1, time=-2)),
    "J": (1.0, _v(length=2, mass=1, time=-2)),
    "W": (1.0, _v(length=2, mass=1, time=-3)),
    "C": (1.0, _v(time=1, current=1)),
    "V": (1.0, _v(length=2, mass=1, time=-3, current=-1)),
    "F": (1.0, _v(length=-2, mass=-1, time=4, current=2)),
    "ohm": (1.0, _v(length=2, mass=1, time=-3, current=-2)),
    "Ω": (1.0, _v(length=2, mass=1, time=-3, current=-2)),
    "S": (1.0, _v(length=-2, mass=-1, time=3, current=2)),
    "Wb": (1.0, _v(length=2, mass=1, time=-2, current=-1)),
    "T": (1.0, _v(mass=1, time=-2, current=-1)),
    "H": (1.0, _v(length=2, mass=1, time=-2, current=-2)),
    "lm": (1.0, _v(lumen=1)),
    "lx": (1.0, _v(length=-2, lumen=1)),
    "Bq": (1.0, _v(time=-1)),
    "Gy": (1.0, _v(length=2, time=-2)),
    "Sv": (1.0, _v(length=2, time=-2)),
    "kat": (1.0, _v(time=-1, amount=1)),
    # 可接受的非 SI 线性单位
    "L": (1e-3, _v(length=3)),
    "l": (1e-3, _v(length=3)),
    "bar": (1e5, _v(length=-1, mass=1, time=-2)),
    "t": (1e3, _v(mass=1)),
    "eV": (1.602176634e-19, _v(length=2, mass=1, time=-2)),
    "min": (60.0, _v(time=1)),
    "h": (3600.0, _v(time=1)),
    "d": (86400.0, _v(time=1)),
    "deg": (math.pi / 180.0, _v()),
}

# 不接受的常见（非线性或非 SI 线性）单位，给出明确报错
_REJECTED = {
    "°C": "摄氏度含仿射零点，不是线性 SI 单位，请改用 K",
    "Celsius": "摄氏度含仿射零点，不是线性 SI 单位，请改用 K",
    "°F": "华氏度含仿射零点，不是线性 SI 单位，请改用 K",
    "Fahrenheit": "华氏度含仿射零点，不是线性 SI 单位，请改用 K",
    "%": "百分数不是量纲一致的线性单位（请按小数输入，或使用无量纲 1）",
    "dB": "分贝为对数量纲，不是线性 SI 单位",
    "degC": "摄氏度含仿射零点，不是线性 SI 单位，请改用 K",
    "degF": "华氏度含仿射零点，不是线性 SI 单位，请改用 K",
}

_UNITS: dict[str, tuple[float, list[int]]] = {}
_UNITS.update(_BASE)
_UNITS.update(_DERIVED)


@dataclass(frozen=True)
class Unit:
    """归一化单位：scale 个 coherent SI(dim)。"""

    scale: float
    dim: tuple[int, ...]

    def is_dimensionless(self) -> bool:
        return not any(self.dim)


_DIMENSIONLESS = Unit(1.0, (0, 0, 0, 0, 0, 0, 0))

_TOKEN_RE = re.compile(r"^[A-Za-zΩµ°]+$")
_EXP_RE = re.compile(r"^[+-]?\d{1,2}$")


def _match_token(token: str) -> tuple[float, tuple[int, ...]]:
    """匹配「词头 + 单位」。

    优先整体精确匹配（``kg`` 先命中 kg 而非 k·g），其次取最长词头切分；
    ``da`` 为双字符词头单独尝试。
    """
    if token in _REJECTED:
        raise error("INVALID_UNIT", f"非法单位 {token!r}：{_REJECTED[token]}")
    if token in _UNITS:
        s, d = _UNITS[token]
        return s, tuple(d)
    # 双字符词头 da
    if token.startswith("da") and token[2:] in _UNITS:
        s, d = _UNITS[token[2:]]
        return 10.0 * s, tuple(d)
    # 最长单字符词头
    for i in range(len(token) - 1, 0, -1):
        prefix, base = token[:i], token[i:]
        if prefix in PREFIXES and base in _UNITS:
            s, d = _UNITS[base]
            return PREFIXES[prefix] * s, tuple(d)
    raise error(
        "INVALID_UNIT",
        f"非法或未知的单位符号 {token!r}；只支持带 SI 词头的线性 SI 单位",
    )


def parse_unit(text: str | None) -> Unit:
    """解析单位字符串；空串或 ``1`` 表示无量纲。"""
    if text is None:
        return _DIMENSIONLESS
    s = text.strip()
    if s == "" or s == "1":
        return _DIMENSIONLESS

    normalized = s.replace("*", ".").replace("·", ".")
    segments = normalized.split("/")
    scale = 1.0
    dim = [0] * 7

    for si, segment in enumerate(segments):
        if segment == "":
            raise error("INVALID_UNIT", f"非法单位表达式 {text!r}：空除段")
        invert = si > 0  # 第一段为分子，其后每段整体为分母
        for factor in segment.split("."):
            if factor == "":
                raise error("INVALID_UNIT", f"非法单位表达式 {text!r}：空因子")
            if "^" in factor:
                sym, exp_text = factor.split("^", 1)
                if not _EXP_RE.match(exp_text):
                    raise error(
                        "INVALID_UNIT",
                        f"非法单位表达式 {text!r}：指数 {exp_text!r} 应为小整数",
                    )
                exponent = int(exp_text)
            else:
                sym, exponent = factor, 1
            if not _TOKEN_RE.match(sym):
                raise error("INVALID_UNIT", f"非法单位符号 {sym!r}（位于 {text!r}）")
            if invert:
                exponent = -exponent
            ts, td = _match_token(sym)
            scale *= ts ** exponent
            for j in range(7):
                dim[j] += td[j] * exponent

    if not (math.isfinite(scale) and scale > 0):
        raise error("INVALID_UNIT", f"非法单位表达式 {text!r}：比例必须为有限正数")
    return Unit(scale, tuple(dim))


# ---- 量纲推导 -----------------------------------------------------------

def multiply(a: Unit, b: Unit) -> Unit:
    # 参与运算的数值已归一化到 coherent SI，scale 恒为 1
    return Unit(
        1.0,
        tuple(a.dim[j] + b.dim[j] for j in range(7)),
    )


def divide(a: Unit, b: Unit) -> Unit:
    return Unit(
        1.0,
        tuple(a.dim[j] - b.dim[j] for j in range(7)),
    )


def check_compatible(a: Unit, b: Unit, op: str) -> None:
    """加减要求量纲完全相同。"""
    if a.dim != b.dim:
        raise error(
            "DIMENSION_MISMATCH",
            f"运算 {op} 量纲不相容：{render_dim(a.dim)} 与 {render_dim(b.dim)}",
        )


# ---- 单位渲染 -----------------------------------------------------------

# canonical SI 基本量纲顺序（ISO/IEC 指南 99 惯例）：kg, m, s, A, K, mol, cd
_BASE_NAMES = ("kg", "m", "s", "A", "K", "mol", "cd")


def render_dim(dim: tuple[int, ...]) -> str:
    """把量纲向量渲染成 canonical coherent SI 单位串。

    内部向量顺序为 [m, kg, s, A, K, mol, cd]，渲染时把质量提到长度之前。
    """
    order = (_MASS, _LEN, _TIME, _CUR, _TEMP, _AMOUNT, _LUM)
    positive = [
        f"{_BASE_NAMES[k]}^{dim[j]}" if dim[j] != 1 else _BASE_NAMES[k]
        for k, j in enumerate(order)
        if dim[j] > 0
    ]
    negative = [
        f"{_BASE_NAMES[k]}^{-dim[j]}" if dim[j] != -1 else _BASE_NAMES[k]
        for k, j in enumerate(order)
        if dim[j] < 0
    ]
    num = ".".join(positive) if positive else "1"
    if not negative:
        return num
    return num + "/" + ".".join(negative)


def render(unit: Unit) -> str:
    """内部单位 scale 恒为 1，直接渲染量纲。"""
    if unit.scale != 1.0:  # 防御性：仅输入侧可能带 scale
        raise error("INVALID_UNIT", "内部单位未归一化到 coherent SI")
    return render_dim(unit.dim)
