#!/usr/bin/env python3
"""不确定度复核服务调用示例（仅标准库，无需安装第三方依赖）。

用法：
    python3 examples/call_api.py [BASE_URL]
默认 BASE_URL=http://localhost:8080

流程：
1. 发布公式 P=U·I（内容哈希版本，重复发布得到同一不可变版本）；
2. 提交测量与交叉协方差执行计算；
3. GET 回读结果，验证其固定引用 formula_version 与 input_snapshot；
4. 再演示一个共享测量 y=(x+x)·x 的例子（灵敏系数先合并再传播）。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8')}")
        raise


def main(base: str = "http://localhost:8080") -> None:
    # 1) 发布公式
    formula_body = {
        "name": "直流功率 P=U·I",
        "description": "电压乘电流",
        "graph": {
            "root": {"action": "mul", "a": "voltage", "b": "current"},
            "voltage": {"action": "leaf", "measurement_id": "U"},
            "current": {"action": "leaf", "measurement_id": "I"},
        },
    }
    formula = call("POST", f"{base}/api/v1/formulas", formula_body)
    print("已发布公式:")
    print("  formula_id     :", formula["formula_id"])
    print("  formula_version:", formula["formula_version"])

    # 2) 执行计算（带测量间交叉协方差）
    calc_body = {
        "formula_id": formula["formula_id"],
        "measurements": [
            {"measurement_id": "U", "value": 10.0, "unit": "V",
             "std_uncertainty": 0.01},
            {"measurement_id": "I", "value": 1.0, "unit": "A",
             "std_uncertainty": 0.005},
        ],
        "covariance": [
            {"a": "U", "b": "I", "cov": 0.00002},
        ],
    }
    result = call("POST", f"{base}/api/v1/calculations", calc_body)
    print("\n计算结果:")
    print(f"  Y = {result['value']:g} {result['unit']}")
    print(f"  u(Y) = {result['std_uncertainty']:.6g}")
    print("  固定引用:")
    print("    formula_version:", result["formula_version"])
    print("    input_snapshot :", result["input_snapshot"])
    print("  灵敏系数:")
    for s in result["sensitivities"]:
        print(f"    c_{s['measurement_id']} = {s['value']:g} {s['unit']}")

    # 3) 回读已固化的结果
    again = call("GET", f"{base}/api/v1/calculations/{result['calculation_id']}")
    assert again["std_uncertainty"] == result["std_uncertainty"]
    assert again["formula_version"] == result["formula_version"]
    print("\nGET 回读一致: calculation_id =", result["calculation_id"])

    # 4) 共享测量：y=(x+x)*x，同一 x 出现在 3 条路径
    shared_body = {
        "measurements": [
            {"measurement_id": "x", "value": 3.0, "unit": "m",
             "std_uncertainty": 0.1},
        ],
        "graph": {
            "root": {"action": "mul", "a": "sum", "b": "lx2"},
            "sum": {"action": "add", "a": "lx0", "b": "lx1"},
            "lx0": {"action": "leaf", "measurement_id": "x"},
            "lx1": {"action": "leaf", "measurement_id": "x"},
            "lx2": {"action": "leaf", "measurement_id": "x"},
        },
    }
    r2 = call("POST", f"{base}/api/v1/calculations", shared_body)
    print("\n共享测量示例 y=(x+x)·x:")
    print(f"  y = {r2['value']:g} {r2['unit']}, u(y) = {r2['std_uncertainty']:g}")
    print(f"  合并后的灵敏系数 c_x = {r2['sensitivities'][0]['value']:g} "
          f"{r2['sensitivities'][0]['unit']}  (= dy/dx = 4x = 12)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080")
