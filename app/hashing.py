"""版本与输入快照的确定性哈希。

* ``formula_version``：对规范化（排序键、紧凑分隔、sort_keys）后的公式
  依赖图做 SHA-256；图内容不可变，哈希即不可变版本号。
* ``input_snapshot``：对测量与协方差输入整体做 SHA-256。
结果固定引用这两个哈希，保证可复算与可审计。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def formula_hash(graph: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({"graph": graph})).hexdigest()


def input_snapshot_hash(
    measurements: list[dict[str, Any]],
    covariance: list[dict[str, Any]] | None,
) -> str:
    return hashlib.sha256(
        _canonical({"measurements": measurements, "covariance": covariance or []})
    ).hexdigest()
