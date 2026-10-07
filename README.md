# 实验室测量不确定度复核服务

输入带标识、数值、**线性 SI 单位**及标准不确定度的原始测量（可选测量间协方差），
发布由 **加 / 减 / 乘 / 除** 组成的公式依赖图（DAG），输出：

- 目标量数值与推导后的单位（coherent SI）；
- 目标量标准不确定度；
- 各原始测量的**灵敏系数**（共享测量沿多条路径出现时先合并再传播协方差）；
- 固定引用的**公式版本号**与**输入快照号**（内容寻址，不可改变）。

技术栈：Python 3.11 · FastAPI · PostgreSQL 16 · psycopg 3。

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 组件 | 地址 |
| --- | --- |
| API 服务 | <http://localhost:8080> |
| 健康检查 | <http://localhost:8080/health> |
| 交互式 API 文档（Swagger） | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |
| PostgreSQL | `localhost:5432`（库/用户/口令均为 `uncertainty`） |

数据库表结构在首次启动时自动创建（`initdb/01_schema.sql`，应用启动时也会幂等执行
`CREATE TABLE IF NOT EXISTS`），无需手工初始化。停止与清理：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 停止并删除数据卷
```

## 2. 本机直接运行（可选）

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 需要一个可达的 PostgreSQL，并用环境变量指定连接串
export DATABASE_URL="postgresql://uncertainty:uncertainty@localhost:5432/uncertainty"
uvicorn app.main:app --host 0.0.0.0 --port 8080

# 自检/无数据库场景（公式持久化接口返回 503，内联计算可用）
DISABLE_DB=1 uvicorn app.main:app --port 8080
```

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://uncertainty:uncertainty@localhost:5432/uncertainty` | PostgreSQL 连接串 |
| `DISABLE_DB` | （空） | 设为 `1` / `true` / `yes` 时不连数据库启动 |

运行测试：

```bash
pip install pytest && python3 -m pytest tests -q
```

## 3. API

所有请求/响应均为 JSON，基路径 `/api/v1`。错误统一信封：

```json
{ "error": { "code": "ZERO_DIVISOR", "message": "节点 'q' 除法分母为零" } }
```

### 3.1 `POST /api/v1/formulas` — 发布不可改公式版本

请求体：

```json
{
  "name": "矩形面积 A=a*b",
  "output": "A",
  "nodes": [
    {"id": "A", "op": "mul", "operands": ["a", "b"]}
  ]
}
```

- `op` ∈ `add` / `sub` / `mul` / `div`，每个节点恰好两个操作数；
- 操作数可以是原始测量 id，也可以是其他节点 id；`output` 为目标量节点；
- 版本号对公式结构做 SHA-256 内容寻址；`name` 仅展示用，**不参与版本哈希**；
- 公式一经发布不可修改；结构变化即产生新版本。重复/并发发布返回同一版本号
  （首次 `created=true`，之后 `created=false`）。

响应：

```json
{
  "formula_version_id": "fv_f4fe02bb…",
  "formula_hash": "fh_f4fe02bb…",
  "created": true
}
```

### 3.2 `POST /api/v1/evaluations` — 执行复核

两种用法二选一：传 `formula_version_id` 引用已发布版本，或直接内联 `formula`
（内联时服务端幂等发布对应版本，再计算）。

```json
{
  "formula_version_id": "fv_f4fe02bb…",
  "measurements": [
    {"id": "a", "value": 100.0, "unit": "cm", "standard_uncertainty": 0.1},
    {"id": "b", "value": 500.0, "unit": "mm", "standard_uncertainty": 0.5}
  ],
  "covariance": [
    {"a": "a", "b": "b", "covariance": 0.005}
  ]
}
```

- 测量 `value` 与 `standard_uncertainty` 同单位；不确定度非负；
- `covariance` 为可选项，只需给出上三角的非零交叉项，**未给出的交叉协方差按 0**；
  对角线由各测量标准不确定度唯一确定；
- 结果固定引用 `formula_version_id` 与 `input_snapshot_id`（输入快照同样内容寻址）。
  相同公式版本 + 相同输入快照永远返回同一 `result_id` 与同一份结果，并发安全。

响应（节选）：

```json
{
  "result_id": "res_d9ada3bf…",
  "formula_version_id": "fv_f4fe02bb…",
  "input_snapshot_id": "is_a12a05a9…",
  "output_node": "A",
  "value": 0.5,
  "unit": "m^2",
  "standard_uncertainty": 0.00075,
  "sensitivities": [
    {"measurement_id": "a", "coefficient": 0.005, "unit": "m / (cm)"},
    {"measurement_id": "b", "coefficient": 0.001, "unit": "m / (mm)"}
  ],
  "covariance_matrix": [[1.0e-6, 5.0e-6], [5.0e-6, 2.5e-7]],
  "measurement_order": ["a", "b"]
}
```

> `covariance_matrix` 为实际参与传播的完整矩阵，单位空间为 coherent SI。
> 灵敏系数按**调用方给出的输入数值与单位**求偏导（所以 cm 输入的系数为 0.005 m/cm）。

### 3.3 只读接口

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /health` | 存活与数据库状态 |
| `GET /api/v1/formulas/{formula_version_id}` | 回读公式版本 |
| `GET /api/v1/evaluations/{result_id}` | 回读固定结果 |
| `GET /api/v1/snapshots/{input_snapshot_id}` | 回读输入快照 |

## 4. 单位规则

- 接受 7 个 SI 基本单位 `m kg s A K mol cd`、全部标准词头（`k M G … c m μ n …`，
  含 `da`）、具有专用名称的导出单位（`N Pa J W C V F Ω S Wb T Hz …`）以及
  线性非 SI 单位 `L t bar eV min h d rad sr deg` 等；
- 单位串支持乘法（`*`、`·`、`.`）、除法（`/`）与整数指数（`^n`），例如
  `kg.m^2/s^2`、`V/m`、`m/s^2`、空串或 `1` 表示无量纲；
- 内部统一换算到 coherent SI 后参与计算，因此 `1 kg + 500 g` 等量纲运算成立；
- **只支持线性单位**：摄氏度/华氏度（仿射零点）、分贝（对数）、`%` 等直接拒绝
  （`INVALID_UNIT`）。

量纲规则：

- `add` / `sub`：两操作数量纲必须完全一致，否则 `DIMENSION_MISMATCH`；
- `mul` / `div`：量纲指数相加/相减自动推导。

## 5. 不确定度传播方法

1. DAG 校验（无环、引用可解析、目标节点存在、无不可达节点、id 无冲突）；
2. 构建协方差矩阵：核对对角线、对称性与半正定性（Cholesky 主轴判定，
   允许秩亏损的半正定矩阵，如完全相关），并换算到 coherent SI；
3. 前向求值（量纲推导 + 除零检查）；
4. **反向自动微分**：一次反向遍历累加偏导，共享测量沿多条路径的贡献在传播前
   自动合并为单个灵敏系数；
5. 一阶传播：`u²(y) = cᵀ C c = Σᵢ Σⱼ cᵢ Cᵢⱼ cⱼ`。

## 6. 拒绝整次计算的情况（HTTP 422）

| `error.code` | 触发条件 |
| --- | --- |
| `VALIDATION_ERROR` | 请求结构/类型不合法（字段缺失、多余字段、NaN、负不确定度等） |
| `INVALID_REQUEST` | `formula` 与 `formula_version_id` 未给或同时给出 |
| `CYCLIC_REFERENCE` | 公式依赖图存在循环（含自引用） |
| `MISSING_MEASUREMENT` | 操作数引用了不存在的测量/节点 |
| `DUPLICATE_NODE` / `DUPLICATE_MEASUREMENT` | id 重复 |
| `ID_COLLISION` | 测量 id 与公式节点同名 |
| `UNREACHABLE_NODE` / `MISSING_OUTPUT` | 目标节点缺失或存在不可达节点 |
| `UNUSED_MEASUREMENT` | 提供了公式未引用的测量 |
| `DIMENSION_MISMATCH` | 加减量纲不相容 |
| `ZERO_DIVISOR` | 除法分母为零 |
| `NONFINITE_RESULT` | 中间或最终结果非有限 |
| `INVALID_UNIT` | 非法/非线性/未知单位 |
| `COVARIANCE_DIAGONAL_MISMATCH` | 协方差对角线与标准不确定度平方不符 |
| `COVARIANCE_NOT_SYMMETRIC` | 矩阵不对称 |
| `COVARIANCE_NOT_PSD` | 矩阵非半正定（含相关系数绝对值 > 1） |
| `COVARIANCE_UNKNOWN_MEASUREMENT` | 协方差引用未知测量 |
| `FORMULA_NOT_FOUND` / `NOT_FOUND` | 引用的版本或结果不存在（404） |
| `DATABASE_UNAVAILABLE` | 需要持久化但数据库不可用（503） |

任一校验失败都不会产生或修改任何版本、快照与结果。

## 7. 调用示例

```bash
# shell + curl
BASE_URL=http://127.0.0.1:8080 ./examples/call.sh

# Python（仅标准库）
python3 examples/call.py

# 直接用示例请求体
curl -s http://127.0.0.1:8080/api/v1/evaluations \
  -H 'Content-Type: application/json' \
  --data-binary @examples/area.json
```

更多请求体见 `examples/`：`area.json`（单位换算）、`speed.json`（除法）、
`force_dag.json`（多节点 DAG）、`correlated.json`（测量间协方差）。

## 8. 目录结构

```
app/
  main.py        FastAPI 路由与错误信封
  models.py      请求/响应模型、规范化哈希
  engine.py      DAG 校验、前向求值、反向微分、不确定度传播
  units.py       线性 SI 单位解析与量纲推导
  covariance.py  协方差矩阵构建/对角线核对/半正定检验
  db.py          PostgreSQL 连接池与幂等写入
  errors.py      领域错误
initdb/01_schema.sql   数据库初始化（compose 自动挂载）
examples/              curl 与 Python 调用示例 + 示例请求体
tests/                 70 个单元/接口测试
Dockerfile, docker-compose.yml, requirements.txt
```
