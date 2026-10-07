"""线性 SI 单位的解析与量纲运算（仅依赖标准库）。

内部表示 ``Unit(dims, scale)``：

* ``dims``：7 个 SI 基本量纲指数 (m, kg, s, A, K, mol, cd)；
* ``scale``：该单位相对于协调 SI 单位的倍数，例如 ``g`` 的 scale=1e-3。

支持：

* 基本单位 ``m, g, kg, s, A, K, mol, cd``；
* 带专门名称的协调导出单位 ``Hz, N, Pa, J, W, C, V, F, Ω/ohm, S,
  Wb, T, H, Bq, Gy, Sv, kat, lm, lx, rad, sr``；
* 全部标准 SI 词头（``k``、``M``、``m``、``μ/u``、``n`` …）；
* 复合单位：``kg*m/s^2``、``kg m s^-2``、``m/s2``、``kg/(m*s^2)``、
  ``V^2``、无量纲 ``1``。

摄氏度（偏移非线性）、升、吨、分钟/小时等非 SI 单位一律拒绝。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Tuple

Dims = Tuple[int, int, int, int, int, int, int]

_DIMENSIONLESS: Dims = (0, 0, 0, 0, 0, 0, 0)
_BASE_ORDER = ("m", "kg", "s", "A", "K", "mol", "cd")


class UnitError(ValueError):
    """单位非法或量纲不相容。"""


@dataclass(frozen=True)
class Unit:
    dims: Dims
    scale: float = 1.0

    @property
    def dimensionless(self) -> bool:
        return self.dims == _DIMENSIONLESS

    def __mul__(self, other: "Unit") -> "Unit":
        return Unit(
            tuple(a + b for a, b in zip(self.dims, other.dims)),  # type: ignore[arg-type]
            self.scale * other.scale,
        )

    def __truediv__(self, other: "Unit") -> "Unit":
        return Unit(
            tuple(a - b for a, b in zip(self.dims, other.dims)),  # type: ignore[arg-type]
            self.scale / other.scale,
        )

    def __pow__(self, exponent: int) -> "Unit":
        return Unit(
            tuple(d * exponent for d in self.dims),  # type: ignore[arg-type]
            self.scale**exponent,
        )

    def same_dims_as(self, other: "Unit") -> bool:
        return self.dims == other.dims


# ---------------------------------------------------------------------------
# 原子单位表：名称 -> Unit。基本质量原子单位取 g(scale=1e-3)，
# 这样 kg 可由词头 k 与 g 组合自然生成。
# ---------------------------------------------------------------------------

# 7 维顺序: m, kg, s, A, K, mol, cd
def _d(m=0, kg=0, s=0, A=0, K=0, mol=0, cd=0) -> Dims:  # noqa: N802
    return (m, kg, s, A, K, mol, cd)


_ATOMIC: dict[str, Unit] = {
    "m": Unit(_d(m=1)),
    "g": Unit(_d(kg=1), 1e-3),
    "s": Unit(_d(s=1)),
    "A": Unit(_d(A=1)),
    "K": Unit(_d(K=1)),
    "mol": Unit(_d(mol=1)),
    "cd": Unit(_d(cd=1)),
    # 协调导出单位（scale 恒为 1）
    "rad": Unit(_d()),
    "sr": Unit(_d()),
    "Hz": Unit(_d(s=-1)),
    "N": Unit(_d(m=1, kg=1, s=-2)),
    "Pa": Unit(_d(m=-1, kg=1, s=-2)),
    "J": Unit(_d(m=2, kg=1, s=-2)),
    "W": Unit(_d(m=2, kg=1, s=-3)),
    "C": Unit(_d(s=1, A=1)),
    "V": Unit(_d(m=2, kg=1, s=-3, A=-1)),
    "F": Unit(_d(m=-2, kg=-1, s=4, A=2)),
    "Ω": Unit(_d(m=2, kg=1, s=-3, A=-2)),
    "ohm": Unit(_d(m=2, kg=1, s=-3, A=-2)),
    "S": Unit(_d(m=-2, kg=-1, s=3, A=2)),
    "Wb": Unit(_d(m=2, kg=1, s=-2, A=-1)),
    "T": Unit(_d(kg=1, s=-2, A=-1)),
    "H": Unit(_d(m=2, kg=1, s=-2, A=-2)),
    "Bq": Unit(_d(s=-1)),
    "Gy": Unit(_d(m=2, s=-2)),
    "Sv": Unit(_d(m=2, s=-2)),
    "kat": Unit(_d(s=-1, mol=1)),
    "lm": Unit(_d(cd=1)),
    "lx": Unit(_d(m=-2, cd=1)),
}

_PREFIXES: dict[str, float] = {
    "Q": 1e30, "R": 1e27, "Y": 1e24, "Z": 1e21, "E": 1e18, "P": 1e15,
    "T": 1e12, "G": 1e9, "M": 1e6, "k": 1e3, "h": 1e2, "da": 1e1,
    "d": 1e-1, "c": 1e-2, "m": 1e-3, "μ": 1e-6, "µ": 1e-6, "u": 1e-6,
    "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18, "z": 1e-21,
    "y": 1e-24, "r": 1e-27, "q": 1e-30,
}

# 展开 “词头 + 原子单位”。kg = k·g 由此生成，故不会出现 mkg 这类形式。
NAMED_UNITS: dict[str, Unit] = dict(_ATOMIC)
for _pname, _pfactor in _PREFIXES.items():
    for _uname, _u in _ATOMIC.items():
        NAMED_UNITS.setdefault(_pname + _uname, Unit(_u.dims, _pfactor * _u.scale))
NAMED_UNITS["1"] = Unit(_DIMENSIONLESS)

_TOKEN_RE = re.compile(r"[A-Za-zΩµμ°]+|\d+|[*^/()\-+]")


def _tokenize(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text)
    if "".join(tokens) != re.sub(r"\s+", "", text):
        raise UnitError(f"非法单位: {text!r}")
    if not tokens:
        raise UnitError("单位为空")
    return tokens


class _Parser:
    """unit := factor (('*'|'/')? factor)* ；隐式乘法同样合法。"""

    def __init__(self, tokens: list[str], text: str):
        self.toks = tokens
        self.text = text
        self.i = 0

    def _peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self) -> str:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def _expect(self, ch: str) -> None:
        if self._peek() != ch:
            raise UnitError(f"非法单位: {self.text!r}")
        self._next()

    def _exponent(self) -> int:
        """消费（可能不存在的）指数：^? [+-]? digits 或 ^ digits。"""
        sign = 1
        tok = self._peek()
        if tok == "^":
            self._next()
            tok = self._peek()
        if tok in ("+", "-"):
            sign = -1 if tok == "-" else 1
            self._next()
            tok = self._peek()
        if tok is None or not tok.isdigit():
            raise UnitError(f"非法单位: {self.text!r}")
        return sign * int(self._next())

    def _factor(self) -> Unit:
        tok = self._peek()
        if tok == "(":
            self._next()
            u = self._expr()
            self._expect(")")
            if self._peek() is not None and self._is_exponent_start():
                return u ** self._exponent()
            return u
        if tok == "1":
            self._next()
            return Unit(_DIMENSIONLESS)
        if tok is None or not all(ch.isalpha() or ch in "Ωµμ" for ch in tok):
            # 仅允许字母名称（含希腊字母 Ω/µ/μ）
            raise UnitError(f"非法单位: {self.text!r}")
        name = self._next()
        base = NAMED_UNITS.get(name)
        if base is None:
            raise UnitError(f"未知或非 SI 单位: {name!r}（单位 {self.text!r}）")
        # name 后若紧跟指数（m2、s-2、m^2）则处理
        if self._peek() is not None and self._is_exponent_start():
            return base ** self._exponent()
        return base

    def _is_exponent_start(self) -> bool:
        tok = self._peek()
        if tok is None:
            return False
        if tok.isdigit() or tok in ("+", "-"):
            return True
        if tok == "^":
            return True
        return False

    def _expr(self) -> Unit:
        result = self._factor()
        while True:
            tok = self._peek()
            if tok == "*":
                self._next()
                result = result * self._factor()
            elif tok == "/":
                self._next()
                result = result / self._factor()
            elif tok in (None, ")"):
                break
            else:
                # 相邻因子视为隐式乘法，如 kg m s^-2
                result = result * self._factor()
        return result

    def parse(self) -> Unit:
        u = self._expr()
        if self.i != len(self.toks):
            raise UnitError(f"非法单位: {self.text!r}")
        return u


def parse_unit(text: str) -> Unit:
    """解析单位字符串为 :class:`Unit`，非法时抛 :class:`UnitError`。"""
    if not isinstance(text, str) or not text.strip():
        raise UnitError("单位为空")
    parser = _Parser(_tokenize(text.strip()), text.strip())
    return parser.parse()


def format_unit(unit: Unit) -> str:
    """以协调 SI 基本单位格式化，量纲的 scale 必须为 1。"""
    if not math.isclose(unit.scale, 1.0, rel_tol=0, abs_tol=0.0):
        # 协调结果不应带非 1 倍数；出现即说明内部错误
        raise UnitError(f"无法格式化非协调单位: scale={unit.scale}")
    parts = [
        name if exp == 1 else f"{name}^{exp}"
        for name, exp in zip(_BASE_ORDER, unit.dims)
        if exp != 0
    ]
    return " ".join(parts) if parts else "1"
