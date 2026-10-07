"""测量间协方差：构建、对角线核对与半正定检验。

调用方按稀疏上三角列表给出协方差；未给出的交叉项按零处理。
矩阵先按输入单位构建，再逐元素换算到 coherent SI：

    C_coh[i,j] = scale_i * scale_j * C_in[i,j]

对角线必须与各测量给出的标准不确定度平方一致
（允许 1e-12 的相对容差吸收序列化误差）。
半正定通过带选主的 Cholesky 分解判定，不引入 numpy 依赖。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import error

_TOL = 1e-12
_MIN_TOL = 1e-300


@dataclass(frozen=True)
class CovEntry:
    i: int
    j: int
    value: float


def build_matrix(
    n: int,
    raw_entries: list[tuple[str, str, float]],
    ids: list[str],
    variances: list[float],
    scales: list[float],
) -> list[list[float]]:
    """构建并校验 coherent SI 空间的 n×n 协方差矩阵。

    ``variances`` 为输入单位下的方差（标准不确定度平方）；
    ``scales`` 为输入单位到 coherent SI 的线性比例。
    """
    index = {mid: k for k, mid in enumerate(ids)}
    matrix = [[0.0] * n for _ in range(n)]

    for a, b, value in raw_entries:
        if a not in index or b not in index:
            raise error(
                "COVARIANCE_UNKNOWN_MEASUREMENT",
                f"协方差引用了不存在的测量：{a!r} 或 {b!r}",
            )
        if value is None or not math.isfinite(value):
            raise error("INVALID_COVARIANCE", f"协方差 ({a},{b}) 必须为有限数")
        i, j = index[a], index[b]
        if i == j:
            # 对角由标准不确定度唯一确定，不允许再用协方差条目给出
            expected = variances[i]
            if not _close(value, expected):
                raise error(
                    "COVARIANCE_DIAGONAL_MISMATCH",
                    f"协方差对角线 {a} 与标准不确定度平方不符："
                    f"给出 {value}，应为 {expected}",
                )
            value = expected
        lo, hi = (i, j) if i <= j else (j, i)
        if matrix[lo][hi] != 0.0:
            raise error(
                "INVALID_COVARIANCE",
                f"协方差 ({ids[lo]},{ids[hi]}) 被重复给出",
            )
        matrix[lo][hi] = value

    # 对角线填入标准不确定度平方
    for k in range(n):
        matrix[k][k] = variances[k]

    # 镜像成对称矩阵（条目始终只写入上三角）
    for i in range(n):
        for j in range(i + 1, n):
            matrix[j][i] = matrix[i][j]

    # 相关系数必须落在 [-1, 1]（非 PSD 也会捕获，提前给出更明确的诊断）
    for i in range(n):
        for j in range(i + 1, n):
            denom = math.sqrt(max(matrix[i][i], 0.0) * max(matrix[j][j], 0.0))
            if denom > 0.0 and abs(matrix[i][j]) > denom * (1.0 + _TOL * 100):
                raise error(
                    "COVARIANCE_NOT_PSD",
                    f"测量 {ids[i]!r} 与 {ids[j]!r} 的相关系数绝对值超过 1，"
                    "协方差矩阵非半正定",
                )

    # 换算到 coherent SI
    scaled = [
        [matrix[i][j] * scales[i] * scales[j] for j in range(n)]
        for i in range(n)
    ]

    if not is_psd(scaled):
        raise error(
            "COVARIANCE_NOT_PSD",
            "协方差矩阵非半正定：Cholesky 分解出现负主轴",
        )
    return scaled


def _close(a: float, b: float) -> bool:
    scale = max(1.0, abs(a), abs(b))
    return abs(a - b) <= _TOL * scale or abs(a - b) <= _MIN_TOL


def is_psd(matrix: list[list[float]]) -> bool:
    """通过 LDLᵀ 型 Cholesky 分解判定半正定。

    采用顺序主元（不选主）：协方差矩阵理论上正定/半正定，
    微小负主轴（低于迹 × n × eps 的阈值）视为零，显著为负即拒绝。
    """
    n = len(matrix)
    work = [row[:] for row in matrix]
    trace = sum(abs(work[k][k]) for k in range(n))
    tol = max(_TOL, n * 2.23e-16) * max(trace, 1.0)

    for k in range(n):
        pivot = work[k][k]
        if pivot < -tol:
            return False
        pivot_eff = max(pivot, 0.0)
        if pivot_eff <= tol:
            # 零主轴：其整行整列必须为零，否则存在负特征方向
            for j in range(k + 1, n):
                if abs(work[k][j]) > tol or abs(work[j][k]) > tol:
                    return False
            continue
        inv = 1.0 / pivot_eff
        for i in range(k + 1, n):
            for j in range(k + 1, n):
                work[i][j] -= work[i][k] * work[k][j] * inv
        for i in range(k + 1, n):
            work[i][k] = 0.0
            work[k][i] = 0.0
    return True
