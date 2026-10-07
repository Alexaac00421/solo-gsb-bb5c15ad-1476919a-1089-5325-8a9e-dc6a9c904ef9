# 实验室测量不确定度复核服务

基于 **Python 3.11 + FastAPI + PostgreSQL** 的 HTTP 服务。发布由 **加 / 减 / 乘 / 除**
组成的公式依赖图，输入带标识、数值、线性 SI 单位和标准不确定度的原始测量
（可选测量间协方差），输出目标量的：

* 数值与单位（协调 SI 基本单位表示）；
* 合成标准不确定度 \(u(Y)\)；
* 每个原始测量的**灵敏系数** \(c_i = \partial Y/\partial x_i\)（含单位）。

公式版本不可改；结果固定引用 **公式版本哈希** 与 **输入快照哈希**。

---

## 1. 一键启动

需要 Docker 20.10+（含 Compose v2）。

```bash
docker compose up --build
```

启动后：

* API 地址：`http://localhost:8080`
* Swagger 交互文档：`http://localhost:8080/docs`
* OpenAPI：`http://localhost:8080/openapi.json`
* 健康检查：`GET http://localhost:8080/health`
* PostgreSQL：`localhost:5432`（库 `uncertainty`，用户 `lab`）

首次启动会**自动建表**并写入一条幂等示例数据（直流功率 P=U·I）：

```bash
curl -s http://localhost:8080/api/v1/formulas | python3 -m json.tool
curl -s http://localhost:8080/api/v1/calculations/00000000-0000-4000-8000-000000000002 | python3 -m json.tool
```

停止与清理：

```bash
docker compose down          # 停止
docker compose down -v       # 停止并删除数据库卷
```

---

## 2. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_DSN` | `host=db port=5432 dbname=uncertainty user=lab password=labpass` | PostgreSQL 连接串 |
| `SEED_DEMO` | `1` | 启动时写入幂等示例数据，置 `0` 关闭 |
| `DB_POOL_MIN` | `1` | 连接池最小连接数 |
| `DB_POOL_MAX` | `10` | 连接池最大连接数 |

API 监听固定为容器内 `0.0.0.0:8080`，宿主机端口可在 `docker-compose.yml`
的 `ports` 中调整。完整配置模板见 `.env.example`。

---

## 3. 快速上手（curl）

### 3.1 发布公式（得到不可变版本）

```bash
curl -s -X POST http://localhost:8080/api/v1/formulas \
  -H 'Content-Type: application/json' \
  -d @examples/formula_power.json
```

响应：

```json
{
  "formula_id": "160999e7-faf6-5310-9f14-0bd02843f29c",
  "formula_version": "fe08346e60e412c50f8052a04473ab1f490edb213ea97f4625039919a5925a33",
  "name": "直流功率 P=U·I",
  "description": "功率 = 电压 × 电流",
  "graph": { "…": "…" }
}
```

> `formula_version` 是依赖图规范化 JSON 的 SHA-256。同一张图重复（或并发）
> 发布返回**同一个** `formula_id`/`formula_version`；图一经发布不可修改，
> 改动图内容会产生新版本。

### 3.2 执行不确定度复核计算

把上一步的 `formula_id` 填入请求：

```bash
FID=160999e7-faf6-5310-9f14-0bd02843f29c
curl -s -X POST http://localhost:8080/api/v1/calculations \
  -H 'Content-Type: application/json' \
  -d "{
    \"formula_id\": \"$FID\",
    \"measurements\": [
      {\"measurement_id\": \"U\", \"value\": 10.0, \"unit\": \"V\", \"std_uncertainty\": 0.01},
      {\"measurement_id\": \"I\", \"value\": 1.0,  \"unit\": \"A\", \"std_uncertainty\": 0.005}
    ],
    \"covariance\": [
      {\"a\": \"U\", \"b\": \"I\", \"cov\": 0.00002}
    ]
  }"
```

响应（节选）：

```json
{
  "calculation_id": "649b7b84-…",
  "formula_id": "160999e7-faf6-5310-9f14-0bd02843f29c",
  "formula_version": "fe08346e60e412c50f8052a04473ab1f490edb213ea97f4625039919a5925a33",
  "input_snapshot": "57efeb451a999c511991b175c1af73d64c767410f279c613d15f5a4e7e7b1ed2",
  "value": 10.0,
  "unit": "m^2 kg s^-3",
  "std_uncertainty": 0.0547723,
  "used_measurements": ["U", "I"],
  "sensitivities": [
    {"measurement_id": "U", "value": 1.0,  "unit": "A"},
    {"measurement_id": "I", "value": 10.0, "unit": "m^2 kg s^-3 A^-1"}
  ]
}
```

也可以直接在计算请求中内联 `graph`（不填 `formula_id`）；内联图同样会被
不可变发布并在响应中给出 `formula_version`。

### 3.3 Python 调用示例（仅标准库）

```bash
python3 examples/call_api.py
```

---

## 4. API 一览

基址：`http://localhost:8080`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/v1/formulas` | 发布公式版本（按图内容哈希幂等） |
| `GET` | `/api/v1/formulas` | 列出公式 |
| `GET` | `/api/v1/formulas/{formula_id}` | 获取公式（含图与版本哈希） |
| `POST` | `/api/v1/calculations` | 执行复核计算并固化结果 |
| `GET` | `/api/v1/calculations/{calculation_id}` | 回读已固化结果 |

### 公式图节点

```json
{ "action": "leaf", "measurement_id": "U" }
{ "action": "add" | "sub" | "mul" | "div", "a": "<节点id>", "b": "<节点id>" }
```

* 图为 `{节点id: 节点}` 的映射，必须包含 id 为 **`root`** 的节点；
* `leaf` 引用原始测量；`div` 的 `b` 是分母；
* 同一 `measurement_id` 允许被多个叶节点引用（共享测量）。

### 测量与协方差

```json
{ "measurement_id": "U", "value": 10.0, "unit": "V", "std_uncertainty": 0.01 }
{ "a": "U", "b": "I", "cov": 0.00002 }
```

* 单位为**线性 SI 单位**：基本单位、带专门名称导出单位（`N, Pa, J, W, C, V,
  F, Ω/ohm, S, Wb, T, H, Hz, Bq, Gy, Sv, kat, lm, lx, rad, sr`）、全部标准
  词头（`k, M, m, μ/u, n …`），以及复合写法（`kg*m/s^2`、`kg m s^-2`、
  `kg/(m*s^2)`、`m/s2`、`1` 无量纲）；
* 摄氏度等偏移单位、升、吨、分钟等非 SI 单位一律拒绝；
* 协方差单位为两个测量**原始单位之积**，服务端按各测量的 SI 倍数自动换算；
* **未给出的交叉协方差按零处理**；
* 对角线可省略（取 \(u^2\)）；显式给出时必须与标准不确定度平方一致。

---

## 5. 计算规则

### 5.1 量纲

* **加 / 减**：两个子节点量纲必须完全一致（无量纲只能与无量纲加减），
  否则整次计算拒绝（`DIMENSION_MISMATCH`）；
* **乘 / 除**：量纲按乘除推导；`div` 分母数值为零时拒绝
  （`ZERO_DENOMINATOR`）；
* 输出单位统一为协调 SI 基本单位（如 W → `m^2 kg s^-3`）。

### 5.2 不确定度传播（一阶 GUM）

\[
u^2(Y)=\sum_i\sum_j c_i\,c_j\,u(x_i,x_j),\qquad c_i=\frac{\partial Y}{\partial x_i}
\]

偏导由依赖图的**反向自动微分**得到。共享测量沿多条路径出现时，
**先把各路径偏导累加成单个灵敏系数**，再与协方差矩阵相乘——多路径间的
相关性因此被正确保留（例如 \(y=x\cdot x\) 给出 \(u(y)=2x\,u(x)\)，
而不是按独立副本得到的 \(\sqrt2\,x\,u(x)\)）。

灵敏系数附带单位：`输出单位 / 输入单位`（协调 SI 制）。

### 5.3 版本固化与并发

* 公式按内容 SHA-256 寻址（数据库 `ON CONFLICT` upsert），并发发布不会
  产生两个版本，也不会覆盖旧版本；
* 每次计算保存请求快照与结果，响应与回读都带 `formula_version`、
  `input_snapshot`；后续任何发布都**不改变已返回的结果**。

### 5.4 整次拒绝的情形（HTTP 422，错误体 `{"error":{"code",...}}`）

| code | 触发条件 |
| --- | --- |
| `INVALID_GRAPH` | 循环引用、缺失节点、缺少 `root`、非法 action / 结构 |
| `MEASUREMENT_NOT_FOUND` | 图引用了未提供的原始测量 |
| `INVALID_MEASUREMENT` | measurement 重复 id、数值非有限、不确定度为负等 |
| `INVALID_UNIT` | 非法或非线性 SI 单位 |
| `DIMENSION_MISMATCH` | 加 / 减量纲不相容 |
| `ZERO_DENOMINATOR` | 除法分母为零 |
| `INVALID_COVARIANCE` | 对角线与 \(u^2\) 不符、测量对重复、引用不存在测量、矩阵非半正定（PSD，Cholesky 判定） |
| `FORMULA_NOT_FOUND` / 404 | 引用的 formula_id 不存在 |

请求体结构错误（缺字段、类型错、多余字段、非法枚举）由 FastAPI/Pydantic
直接返回 422 校验明细。

---

## 6. 本地开发与测试

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest                       # 引擎/单位单元测试（无需数据库）
python3 tests/test_api.py               # HTTP 端到端测试（内存假存储）

# 本地运行（需要先启动一个 PostgreSQL 并导出 DATABASE_DSN）
uvicorn app.main:app --reload --port 8080
```

## 7. 项目结构

```
app/
  units.py      线性 SI 单位解析、量纲运算
  engine.py     图校验 + 前向求值 + 反向微分 + 协方差传播/PSD
  schemas.py    Pydantic 请求/响应模型
  hashing.py    公式版本与输入快照哈希
  db.py         PostgreSQL 连接池、建表、幂等发布与结果存取
  seed.py       幂等示例数据
  main.py       FastAPI 路由与异常处理
examples/       curl JSON 与 Python 调用示例
tests/          单元测试与 API 端到端测试
Dockerfile, docker-compose.yml
```
