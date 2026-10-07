#!/usr/bin/env python3
"""Python 调用示例：发布公式 -> 提交复核 -> 回读固定结果。

仅依赖标准库。用法（默认连接本机 compose 启动的服务）：
    python3 examples/call.py
    BASE_URL=http://127.0.0.1:8080 python3 examples/call.py
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8080")


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise SystemExit(f"HTTP {exc.code}: {body}") from None


def main() -> None:
    print("health:", call("GET", "/health"))

    formula = {
        "name": "平均速度 v = s / t",
        "output": "v",
        "nodes": [{"id": "v", "op": "div", "operands": ["s", "t"]}],
    }
    published = call("POST", "/api/v1/formulas", formula)
    print("published:", json.dumps(published, ensure_ascii=False))
    version_id = published["formula_version_id"]

    request = {
        "formula_version_id": version_id,
        "measurements": [
            {"id": "s", "value": 100.0, "unit": "m", "standard_uncertainty": 1.0},
            {"id": "t", "value": 10.0, "unit": "s", "standard_uncertainty": 0.1},
        ],
        "covariance": [],
    }
    result = call("POST", "/api/v1/evaluations", request)
    print(
        f"v = {result['value']:.6g} {result['unit']}  "
        f"u(v) = {result['standard_uncertainty']:.6g}"
    )
    for s in result["sensitivities"]:
        print(f"  ∂v/∂{s['measurement_id']} = {s['coefficient']:.6g} {s['unit']}")

    again = call("POST", "/api/v1/evaluations", request)
    assert again["result_id"] == result["result_id"], "结果应固定"
    print("重复提交返回同一 result_id:", result["result_id"])

    stored = call("GET", f"/api/v1/evaluations/{result['result_id']}")
    assert stored["standard_uncertainty"] == result["standard_uncertainty"]
    print("回读一致，结果固定引用:", stored["formula_version_id"], stored["input_snapshot_id"])


if __name__ == "__main__":
    main()
