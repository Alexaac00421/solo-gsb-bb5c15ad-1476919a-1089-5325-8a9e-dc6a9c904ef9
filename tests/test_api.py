"""HTTP 层测试（DISABLE_DB 模式，专注校验与内联计算）。"""
import os

os.environ["DISABLE_DB"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def area_body(**over):
    body = {
        "formula": {
            "name": "面积",
            "output": "A",
            "nodes": [{"id": "A", "op": "mul", "operands": ["a", "b"]}],
        },
        "measurements": [
            {"id": "a", "value": 100, "unit": "cm", "standard_uncertainty": 0.1},
            {"id": "b", "value": 500, "unit": "mm", "standard_uncertainty": 0.5},
        ],
        "covariance": [],
    }
    body.update(over)
    return body


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "database": "disabled"}


def test_evaluate_inline(client):
    r = client.post("/api/v1/evaluations", json=area_body())
    assert r.status_code == 201
    d = r.json()
    assert d["value"] == 0.5
    assert d["unit"] == "m^2"
    assert d["formula_version_id"].startswith("fv_")
    assert d["input_snapshot_id"].startswith("is_")
    assert len(d["sensitivities"]) == 2


def test_deterministic_ids_and_name_excluded(client):
    d1 = client.post("/api/v1/evaluations", json=area_body()).json()
    renamed = area_body(formula={
        "name": "完全不同的名字",
        "output": "A",
        "nodes": [{"id": "A", "op": "mul", "operands": ["a", "b"]}],
    })
    d2 = client.post("/api/v1/evaluations", json=renamed).json()
    assert d1["formula_version_id"] == d2["formula_version_id"]


def test_payload_change_changes_snapshot(client):
    d1 = client.post("/api/v1/evaluations", json=area_body()).json()
    changed = area_body(measurements=[
        {"id": "a", "value": 101, "unit": "cm", "standard_uncertainty": 0.1},
        {"id": "b", "value": 500, "unit": "mm", "standard_uncertainty": 0.5},
    ])
    d2 = client.post("/api/v1/evaluations", json=changed).json()
    assert d1["input_snapshot_id"] != d2["input_snapshot_id"]
    assert d1["result_id"] != d2["result_id"]


def test_formula_persistence_requires_db(client):
    r = client.post("/api/v1/formulas", json={
        "output": "A",
        "nodes": [{"id": "A", "op": "mul", "operands": ["a", "b"]}],
    })
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DATABASE_UNAVAILABLE"


def test_exactly_one_formula_source(client):
    body = area_body()
    body["formula_version_id"] = "fv_x"
    r = client.post("/api/v1/evaluations", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"

    body.pop("formula")
    body.pop("formula_version_id")
    r = client.post("/api/v1/evaluations", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("code,patch", [
    ("CYCLIC_REFERENCE", {"formula": {"output": "n1", "nodes": [
        {"id": "n1", "op": "add", "operands": ["n2", "x"]},
        {"id": "n2", "op": "mul", "operands": ["n1", "y"]}]}}),
    ("MISSING_MEASUREMENT", {"measurements": [
        {"id": "a", "value": 1, "unit": "m", "standard_uncertainty": 0}]}),
    ("INVALID_UNIT", {"measurements": [
        {"id": "a", "value": 1, "unit": "xx", "standard_uncertainty": 0},
        {"id": "b", "value": 1, "unit": "m", "standard_uncertainty": 0}]}),
    ("COVARIANCE_DIAGONAL_MISMATCH", {"covariance": [
        {"a": "a", "b": "a", "covariance": 99}]}),
    ("COVARIANCE_NOT_PSD", {"covariance": [
        {"a": "a", "b": "b", "covariance": 9}]}),
    ("COVARIANCE_UNKNOWN_MEASUREMENT", {"covariance": [
        {"a": "a", "b": "zz", "covariance": 0.1}]}),
])
def test_rejections(client, code, patch):
    body = area_body()
    body.update(patch)
    r = client.post("/api/v1/evaluations", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code


def test_dimension_and_zero_divisor_rejections(client):
    add_body = area_body(formula={
        "output": "S",
        "nodes": [{"id": "S", "op": "add", "operands": ["a", "b"]}],
    })
    add_body["measurements"] = [
        {"id": "a", "value": 1, "unit": "m", "standard_uncertainty": 0},
        {"id": "b", "value": 1, "unit": "kg", "standard_uncertainty": 0},
    ]
    r = client.post("/api/v1/evaluations", json=add_body)
    assert r.json()["error"]["code"] == "DIMENSION_MISMATCH"

    div_body = area_body(formula={
        "output": "q",
        "nodes": [{"id": "q", "op": "div", "operands": ["a", "b"]}],
    })
    div_body["measurements"] = [
        {"id": "a", "value": 1, "unit": "m", "standard_uncertainty": 0},
        {"id": "b", "value": 0, "unit": "s", "standard_uncertainty": 0.1},
    ]
    r = client.post("/api/v1/evaluations", json=div_body)
    assert r.json()["error"]["code"] == "ZERO_DIVISOR"


def test_validation_error_envelope(client):
    body = area_body(measurements=[
        {"id": "a", "value": 1, "unit": "m", "standard_uncertainty": -1},
        {"id": "b", "value": 1, "unit": "m", "standard_uncertainty": 0},
    ])
    r = client.post("/api/v1/evaluations", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_extra_field_rejected(client):
    body = area_body()
    body["unexpected"] = 1
    r = client.post("/api/v1/evaluations", json=body)
    assert r.status_code == 422
