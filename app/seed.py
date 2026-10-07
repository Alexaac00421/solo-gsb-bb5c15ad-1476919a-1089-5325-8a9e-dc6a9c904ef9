"""可选的初始化示例数据（SEED_DEMO=1 时写入，幂等）。

固定 UUID 与内容哈希，README / 示例脚本可直接引用。
"""
from __future__ import annotations

from .db import insert_calculation, upsert_formula
from .engine import evaluate
from .hashing import formula_hash, input_snapshot_hash
from .units import format_unit

DEMO_FORMULA_ID = "00000000-0000-4000-8000-000000000001"
DEMO_CALC_ID = "00000000-0000-4000-8000-000000000002"

DEMO_FORMULA = {
    "name": "直流功率 P=U·I",
    "description": "功率由电压与电流相乘得到，演示乘法语义与不确定度传播。",
    "graph": {
        "root": {"action": "mul", "a": "voltage", "b": "current"},
        "voltage": {"action": "leaf", "measurement_id": "U"},
        "current": {"action": "leaf", "measurement_id": "I"},
    },
}

DEMO_REQUEST = {
    "measurements": [
        {"measurement_id": "U", "value": 10.0, "unit": "V", "std_uncertainty": 0.01},
        {"measurement_id": "I", "value": 1.0, "unit": "A", "std_uncertainty": 0.005},
    ],
    "covariance": [
        {"a": "U", "b": "I", "cov": 0.00002},
        {"a": "U", "b": "U", "cov": 0.0001},
    ],
    "formula_id": DEMO_FORMULA_ID,
}


def seed_demo(pool) -> None:
    graph = DEMO_FORMULA["graph"]
    version = formula_hash(graph)
    with pool.connection() as conn:
        upsert_formula(
            conn,
            formula_id=DEMO_FORMULA_ID,
            version=version,
            name=DEMO_FORMULA["name"],
            description=DEMO_FORMULA["description"],
            graph=graph,
        )
        existing = conn.execute(
            "SELECT 1 FROM calculations WHERE calculation_id = %s",
            (DEMO_CALC_ID,),
        ).fetchone()
        if existing:
            return

    result = evaluate(
        graph, DEMO_REQUEST["measurements"], DEMO_REQUEST["covariance"]
    )
    snapshot = input_snapshot_hash(
        DEMO_REQUEST["measurements"], DEMO_REQUEST["covariance"]
    )
    result_body = {
        "calculation_id": DEMO_CALC_ID,
        "formula_id": DEMO_FORMULA_ID,
        "formula_version": version,
        "input_snapshot": snapshot,
        "value": result.value,
        "unit": format_unit(result.unit),
        "std_uncertainty": result.std_uncertainty,
        "used_measurements": result.used_measurements,
        "sensitivities": [
            {"measurement_id": mid,
             "value": result.sensitivities[mid],
             "unit": result.sensitivity_units[mid]}
            for mid in result.used_measurements
        ],
    }
    with pool.connection() as conn:
        insert_calculation(
            conn,
            calculation_id=DEMO_CALC_ID,
            formula_id=DEMO_FORMULA_ID,
            formula_version=version,
            input_snapshot=snapshot,
            request=DEMO_REQUEST,
            result=result_body,
        )
