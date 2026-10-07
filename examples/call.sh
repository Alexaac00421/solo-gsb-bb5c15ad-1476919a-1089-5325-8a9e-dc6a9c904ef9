#!/usr/bin/env bash
# 不确定度复核服务调用示例。
# 用法：BASE_URL=http://127.0.0.1:8080 ./examples/call.sh
set -euo pipefail

BASE="${BASE_URL:-http://127.0.0.1:8080}"
CURL=(curl -sS -H 'Content-Type: application/json')

echo "== 1. 健康检查 =="
"${CURL[@]}" "$BASE/health"
echo; echo

echo "== 2. 发布公式版本（不可改；重复发布返回同一版本号，created=false）=="
FORMULA='{
  "name": "矩形面积 A=a*b",
  "output": "A",
  "nodes": [{"id": "A", "op": "mul", "operands": ["a", "b"]}]
}'
PUB=$("${CURL[@]}" -X POST "$BASE/api/v1/formulas" -d "$FORMULA")
echo "$PUB" | python3 -m json.tool
FV=$(python3 -c "import json,sys;print(json.load(sys.stdin)['formula_version_id'])" <<<"$PUB")
echo "formula_version_id = $FV"
echo

echo "== 3. 引用已发布版本执行复核（带测量间协方差）=="
EVAL=$(cat <<JSON
{
  "formula_version_id": "$FV",
  "measurements": [
    {"id": "a", "value": 100.0, "unit": "cm", "standard_uncertainty": 0.1},
    {"id": "b", "value": 500.0, "unit": "mm", "standard_uncertainty": 0.5}
  ],
  "covariance": [
    {"a": "a", "b": "b", "covariance": 0.005}
  ]
}
JSON
)
RES=$("${CURL[@]}" -X POST "$BASE/api/v1/evaluations" -d "$EVAL")
echo "$RES" | python3 -m json.tool
RID=$(python3 -c "import json,sys;print(json.load(sys.stdin)['result_id'])" <<<"$RES")
echo

echo "== 4. 按 result_id 回读（结果永不改变）=="
"${CURL[@]}" "$BASE/api/v1/evaluations/$RID" | python3 -m json.tool
echo

echo "== 5. 非法输入示例：零分母（返回 422 + 错误码 ZERO_DIVISOR）=="
"${CURL[@]}" -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/api/v1/evaluations" -d '{
  "formula": {"output":"q","nodes":[{"id":"q","op":"div","operands":["a","b"]}]},
  "measurements":[
    {"id":"a","value":1,"unit":"m","standard_uncertainty":0},
    {"id":"b","value":0,"unit":"s","standard_uncertainty":0.1}]}'

echo
echo "== 6. 内联公式一步发布并计算（不调 /formulas 也可）=="
"${CURL[@]}" -X POST "$BASE/api/v1/evaluations" \
  --data-binary @examples/area.json | python3 -m json.tool
