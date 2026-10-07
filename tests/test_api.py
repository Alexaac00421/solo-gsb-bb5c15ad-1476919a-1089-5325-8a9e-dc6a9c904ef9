"""API 端到端测试：用内存假存储替代 PostgreSQL，覆盖全部 HTTP 路径。

运行：python3 tests/test_api.py（无需数据库）
"""
from __future__ import annotations

import sys
import os
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402


class FakeConn:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        # 解开 psycopg 的 JSON 包装器，取原始 Python 对象
        params = tuple(getattr(p, "obj", p) for p in params)
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO formulas"):
            fid, version, name, desc, graph = params
            if version in self.store["formulas_by_version"]:
                row = self.store["formulas_by_version"][version]
            else:
                row = {
                    "formula_id": uuid.UUID(fid),
                    "formula_version": version,
                    "name": name,
                    "description": desc,
                    "graph": graph,
                }
                self.store["formulas_by_version"][version] = row
                self.store["formulas_by_id"][fid] = row
            return _Result([row])
        if s.startswith("SELECT formula_id, formula_version, name, description, graph FROM formulas WHERE formula_id"):
            return _Result([self.store["formulas_by_id"].get(params[0])]
                           if self.store["formulas_by_id"].get(params[0]) else [])
        if s.startswith("SELECT formula_id"):
            rows = list(self.store["formulas_by_id"].values())
            return _Result(rows[: params[0]])
        if s.startswith("INSERT INTO calculations"):
            cid, fid, version, snapshot, request, result = params
            self.store["calcs"][cid] = {
                "calculation_id": cid, "formula_id": fid,
                "formula_version": version, "input_snapshot": snapshot,
                "request": request, "result": result,
            }
            return _Result([])
        if s.startswith("SELECT calculation_id"):
            row = self.store["calcs"].get(params[0])
            return _Result([row] if row else [])
        if s.startswith("SELECT 1 FROM calculations"):
            return _Result([1] if params[0] in self.store["calcs"] else [])
        if s.startswith("CREATE"):
            return _Result([])
        raise AssertionError("unhandled SQL: " + s)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakePool:
    def __init__(self):
        self.store = {
            "formulas_by_version": {},
            "formulas_by_id": {},
            "calcs": {},
        }

    def connection(self):
        return FakeConn(self.store)

    def close(self):
        pass


import app.db as db_module  # noqa: E402

db_module.wait_for_postgres = lambda *a, **k: None
os.environ["SEED_DEMO"] = "0"

from app.main import app  # noqa: E402

_pool = FakePool()
app.state.pool = _pool

# 手动跑 lifespan 中除 DB 外的设置（TestClient 会触发 lifespan，
# 但 make_pool 已被替换）
db_module.make_pool = lambda: _pool
db_module.init_schema = lambda p: None

client = TestClient(app)

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)


FORMULA = {
    "name": "P=U*I",
    "description": "power",
    "graph": {
        "root": {"action": "mul", "a": "u", "b": "i"},
        "u": {"action": "leaf", "measurement_id": "U"},
        "i": {"action": "leaf", "measurement_id": "I"},
    },
}

# health
r = client.get("/health")
check("health", r.status_code == 200, r.text)

# publish
r = client.post("/api/v1/formulas", json=FORMULA)
check("publish 201", r.status_code == 201, r.text)
f1 = r.json()
check("version hash len", len(f1["formula_version"]) == 64)

# republish identical -> same version (并发/重复发布不可变)
r = client.post("/api/v1/formulas", json=FORMULA)
f2 = r.json()
check("republish same version", f1["formula_version"] == f2["formula_version"])
check("republish same id", f1["formula_id"] == f2["formula_id"])

# 修改图内容 -> 新版本
changed = {**FORMULA, "graph": {
    "root": {"action": "div", "a": "u", "b": "i"},
    "u": {"action": "leaf", "measurement_id": "U"},
    "i": {"action": "leaf", "measurement_id": "I"},
}}
r = client.post("/api/v1/formulas", json=changed)
check("changed graph new version",
      r.json()["formula_version"] != f1["formula_version"], r.text)

# 非法图：循环
cyc = {**FORMULA, "graph": {
    "root": {"action": "add", "a": "x", "b": "y"},
    "x": {"action": "add", "a": "root", "b": "y"},
    "y": {"action": "leaf", "measurement_id": "U"},
}}
r = client.post("/api/v1/formulas", json=cyc)
# 发布期只校验结构（FastAPI 层），循环在计算期拒绝；这里图结构合法
check("cycle structurally publishable", r.status_code == 201, r.text)
bad_formula_id = r.json()["formula_id"]

# 用合法公式计算
calc_req = {
    "formula_id": f1["formula_id"],
    "measurements": [
        {"measurement_id": "U", "value": 10.0, "unit": "V", "std_uncertainty": 0.01},
        {"measurement_id": "I", "value": 1.0, "unit": "A", "std_uncertainty": 0.005},
    ],
    "covariance": [{"a": "U", "b": "I", "cov": 0.00002}],
}
r = client.post("/api/v1/calculations", json=calc_req)
check("calc 201", r.status_code == 201, r.text)
res = r.json()
check("calc refs formula_version", res["formula_version"] == f1["formula_version"])
check("calc has snapshot", len(res["input_snapshot"]) == 64)
check("calc value", abs(res["value"] - 10.0) < 1e-12)
check("calc unit", res["unit"] == "m^2 kg s^-3", res["unit"])
check("calc unc", abs(res["std_uncertainty"] - (0.003 ** 0.5)) < 1e-12,
      str(res["std_uncertainty"]))
sens = {s["measurement_id"]: s for s in res["sensitivities"]}
check("sens cU", abs(sens["U"]["value"] - 1.0) < 1e-12)
check("sens cI", abs(sens["I"]["value"] - 10.0) < 1e-12)
check("sens units", sens["U"]["unit"] == "A" and
      sens["I"]["unit"] == "m^2 kg s^-3 A^-1")

# 重复计算 -> 结果字段一致（值固定），仅 calculation_id 不同
r = client.post("/api/v1/calculations", json=calc_req)
res2 = r.json()
for k in ("value", "unit", "std_uncertainty", "formula_version", "input_snapshot"):
    check(f"deterministic {k}", res[k] == res2[k])
check("different calc id", res["calculation_id"] != res2["calculation_id"])

# GET 已存结果（与返回一致；并发发布不改变已返回结果）
r = client.get(f"/api/v1/calculations/{res['calculation_id']}")
check("get calc", r.status_code == 200 and r.json() == res, r.text)

# 内联图
inline_req = {
    "measurements": [
        {"measurement_id": "X", "value": 3.0, "unit": "m", "std_uncertainty": 0.1},
    ],
    "graph": {
        "root": {"action": "add", "a": "a1", "b": "a2"},
        "a1": {"action": "leaf", "measurement_id": "X"},
        "a2": {"action": "leaf", "measurement_id": "X"},
    },
}
r = client.post("/api/v1/calculations", json=inline_req)
check("inline calc", r.status_code == 201 and r.json()["value"] == 6.0, r.text)
check("inline merged sens", r.json()["sensitivities"][0]["value"] == 2.0)

# 既无 graph 又无 formula_id
r = client.post("/api/v1/calculations", json={"measurements": calc_req["measurements"]})
check("missing formula ref", r.status_code == 422, r.text)

# formula_id 不存在
r = client.post("/api/v1/calculations", json={
    "formula_id": "11111111-1111-4111-8111-111111111111",
    "measurements": calc_req["measurements"],
})
check("formula not found", r.status_code == 404, r.text)

# 缺失测量
r = client.post("/api/v1/calculations", json={
    "formula_id": f1["formula_id"],
    "measurements": [{"measurement_id": "U", "value": 10.0, "unit": "V",
                      "std_uncertainty": 0.01}],
})
check("missing measurement", r.status_code == 422
      and r.json()["error"]["code"] == "MEASUREMENT_NOT_FOUND", r.text)

# 量纲不相容
r = client.post("/api/v1/calculations", json={
    "formula_id": f1["formula_id"],  # mul 本身相容；用内联 add 测
    "measurements": [],
    "graph": {
        "root": {"action": "add", "a": "u", "b": "i"},
        "u": {"action": "leaf", "measurement_id": "U"},
        "i": {"action": "leaf", "measurement_id": "I"},
    },
})
# measurements 为空会被 schema 拦截
check("empty measurements schema", r.status_code == 422)

r = client.post("/api/v1/calculations", json={
    "measurements": [
        {"measurement_id": "U", "value": 10.0, "unit": "V", "std_uncertainty": 0.01},
        {"measurement_id": "I", "value": 1.0, "unit": "A", "std_uncertainty": 0.005},
    ],
    "graph": {
        "root": {"action": "add", "a": "u", "b": "i"},
        "u": {"action": "leaf", "measurement_id": "U"},
        "i": {"action": "leaf", "measurement_id": "I"},
    },
})
check("dimension mismatch 422", r.status_code == 422
      and r.json()["error"]["code"] == "DIMENSION_MISMATCH", r.text)

# 零分母
r = client.post("/api/v1/calculations", json={
    "measurements": [
        {"measurement_id": "A", "value": 1.0, "unit": "m", "std_uncertainty": 0.0},
        {"measurement_id": "B", "value": 0.0, "unit": "s", "std_uncertainty": 0.01},
    ],
    "graph": {
        "root": {"action": "div", "a": "a", "b": "b"},
        "a": {"action": "leaf", "measurement_id": "A"},
        "b": {"action": "leaf", "measurement_id": "B"},
    },
})
check("zero denominator", r.status_code == 422
      and r.json()["error"]["code"] == "ZERO_DENOMINATOR", r.text)

# 非 PSD
r = client.post("/api/v1/calculations", json={
    **{k: v for k, v in calc_req.items() if k != "covariance"},
    "covariance": [{"a": "U", "b": "I", "cov": 0.0001}],
})
check("non-PSD rejected", r.status_code == 422
      and "PSD" in r.json()["error"]["message"], r.text)

# 对角线不符
r = client.post("/api/v1/calculations", json={
    **calc_req,
    "covariance": [{"a": "U", "b": "U", "cov": 0.99}],
})
check("diagonal mismatch", r.status_code == 422
      and "对角线" in r.json()["error"]["message"], r.text)

# 非法单位
r = client.post("/api/v1/calculations", json={
    "formula_id": f1["formula_id"],
    "measurements": [
        {"measurement_id": "U", "value": 10.0, "unit": "degC", "std_uncertainty": 0.01},
        {"measurement_id": "I", "value": 1.0, "unit": "A", "std_uncertainty": 0.005},
    ],
})
check("invalid unit", r.status_code == 422
      and r.json()["error"]["code"] == "INVALID_UNIT", r.text)

# 循环图在计算期被拒绝
r = client.post("/api/v1/calculations", json={
    "formula_id": bad_formula_id,
    "measurements": [
        {"measurement_id": "U", "value": 10.0, "unit": "V", "std_uncertainty": 0.01},
        {"measurement_id": "I", "value": 1.0, "unit": "A", "std_uncertainty": 0.005},
    ],
})
check("cycle rejected at calc", r.status_code == 422
      and "循环" in r.json()["error"]["message"], r.text)

# schema 层：负不确定度 / 额外字段 / 错误 action
r = client.post("/api/v1/calculations", json={
    "measurements": [
        {"measurement_id": "U", "value": 10.0, "unit": "V", "std_uncertainty": -1},
    ],
    "graph": {"root": {"action": "leaf", "measurement_id": "U"}},
})
check("negative unc schema", r.status_code == 422)

r = client.post("/api/v1/formulas", json={**FORMULA, "extra": 1})
check("extra field forbidden", r.status_code == 422)

r = client.post("/api/v1/formulas", json={
    **FORMULA,
    "graph": {"root": {"action": "sqrt", "measurement_id": "U"}},
})
check("bad action schema", r.status_code == 422, r.text[:200])

# 公式列表 / 详情
r = client.get("/api/v1/formulas")
check("list formulas", r.status_code == 200 and len(r.json()) >= 2)
r = client.get(f"/api/v1/formulas/{f1['formula_id']}")
check("get formula", r.status_code == 200 and r.json()["formula_version"] == f1["formula_version"])
r = client.get("/api/v1/formulas/not-a-uuid")
check("bad formula id 404", r.status_code == 404)
r = client.get("/api/v1/calculations/not-a-uuid")
check("bad calc id 404", r.status_code == 404)

print()
if failures:
    print(f"{len(failures)} FAILURES:", failures)
    sys.exit(1)
print("ALL API TESTS PASSED")
