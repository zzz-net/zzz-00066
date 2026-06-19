# 本地样本冷链交接 JSON API

基于 FastAPI + SQLite 的冷链样本交接系统，支持箱码 CSV/JSON 导入、多状态流转、温度越界自动异常、角色权限校验、完整审计历史和可追溯导出。**无前端**，所有接口均为 JSON。

---

## 0. 快速启动 & 一键跑完全链路

### 安装与启动服务

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Swagger UI（可视化调试）: http://localhost:8000/docs

### 一键跑通所有链路（推荐，跨平台无引号坑）

```bash
# 成功路径 + 失败路径 + 管理员操作 + 导出校验
python run_examples.py

# 只跑某一段
python run_examples.py happy          # 成功路径
python run_examples.py failure        # 失败路径
python run_examples.py admin          # 管理员操作 + 导出/审计对齐
python run_examples.py persistence    # 重启后持久化（需先重启服务）
```

### 批次签收全面回归测试

```bash
# 覆盖部分签收、撤销后再签收、越权撤销、重启恢复、导出核对、重复到达拦截
python test_batch_comprehensive.py

# 基础冒烟测试
python test_batch_smoke.py
```

> 所有请求体都从 `examples/*.json` 读取，避免 PowerShell / bash 之间的引号、中文编码差异。

---

## 1. 状态机 & 角色矩阵

```
                     出库员                     转运员                   库房签收员
待出库 ──────────────→ 转运中 ────────────────→ 待签收 ────────────────→ 已签收(终态)
  │                      │                       │
  │                      │         管理员         │
  ├──────────────────────┴───────────┬───────────┤
  │                                  │           │
  │                        +──→ 异常待处理 ←─────+
  │管理员                    管理员 │             
  └────→ 已回退(终态)      ┌────────┴──────┐
                          │               │
                    管理员→ 待出库     ← 管理员
                              │
                              └──→ 已回退(终态)
```

**角色权限**：

| 动作 | 允许角色 |
|------|---------|
| dispatch 出库 | 出库员 |
| arrive 转运到达 | 转运员 |
| receive 库房签收 | 库房签收员 |
| exception 标记异常 / rollback 回退 / recover 恢复 | 管理员 |

**终态**：`已签收`、`已回退` — 不可再变更。

---

## 2. 成功路径（curl 版，请求体用 @file 跨平台）

> 所有 `-d @xxx.json` 文件均在 `examples/` 目录下，请先 cd 到项目根。

### 2.1 配置温度阈值 & 超时

```bash
# 疫苗: 2~8°C, 120 分钟超时
curl -sS -X POST http://localhost:8000/api/thresholds \
  -H "Content-Type: application/json" \
  -d @examples/threshold_vaccine.json

# 血液制品: 1~6°C, 90 分钟
curl -sS -X POST http://localhost:8000/api/thresholds \
  -H "Content-Type: application/json" \
  -d @examples/threshold_blood.json

# 试剂: -20~-15°C, 180 分钟
curl -sS -X POST http://localhost:8000/api/thresholds \
  -H "Content-Type: application/json" \
  -d @examples/threshold_reagent.json

# 查询
curl -sS http://localhost:8000/api/thresholds
```

### 2.2 导入样本箱（JSON / CSV 二选一）

**JSON 导入 4 箱**：

```bash
curl -sS -X POST http://localhost:8000/api/boxes/import/json \
  -H "Content-Type: application/json" \
  -d @examples/boxes_import.json
# {"imported":["BOX-V001","BOX-V002","BOX-B001","BOX-R001"],"rejected":[]}
```

**CSV 再导入 3 箱**：

```bash
curl -sS -X POST http://localhost:8000/api/boxes/import/csv \
  -F "file=@examples/boxes_import.csv;type=text/csv"
```

### 2.3 出库 → 转运 → 签收（BOX-V001）

```bash
# 出库 (待出库 → 转运中)
curl -sS -X POST http://localhost:8000/api/boxes/BOX-V001/dispatch \
  -H "Content-Type: application/json" \
  -d @examples/dispatch_ok.json

# 转运到达 (转运中 → 待签收)
curl -sS -X POST http://localhost:8000/api/boxes/BOX-V001/arrive \
  -H "Content-Type: application/json" \
  -d @examples/arrive_ok.json

# 库房签收 (待签收 → 已签收)
curl -sS -X POST http://localhost:8000/api/boxes/BOX-V001/receive \
  -H "Content-Type: application/json" \
  -d @examples/receive_ok.json
```

### 2.4 查看审计历史

```bash
# BOX-V001 单箱审计，应该有 4 条：导入 + 出库 + 到达 + 签收
curl -sS "http://localhost:8000/api/audit?box_code=BOX-V001"

# 全局审计
curl -sS http://localhost:8000/api/audit
```

### 2.5 导出（历史可追溯，JSON & CSV 字段一致）

**JSON 导出**：每条记录对应一次状态流转/操作，包含角色、操作者、原因、温度、时间

```bash
curl -sS http://localhost:8000/api/export/json
```

返回结构（所有字段含义和 CSV 表头严格对齐）：

```jsonc
{
  "generated_at": "2026-06-19T10:30:00.123456",
  "fields": [
    "box_code", "sample_type", "sequence",
    "from_status", "to_status",
    "role", "operator", "reason",
    "temp_at_action", "action_at", "current_status"
  ],
  "rows": [
    {
      "box_code": "BOX-V001",
      "sample_type": "疫苗",
      "sequence": 1,
      "from_status": "",
      "to_status": "待出库",
      "role": "系统",
      "operator": "导入",
      "reason": "导入创建",
      "temp_at_action": 4.0,
      "action_at": "2026-06-19T10:00:00.000000",
      "current_status": "已签收"
    },
    {
      "box_code": "BOX-V001",
      "sample_type": "疫苗",
      "sequence": 2,
      "from_status": "待出库",
      "to_status": "转运中",
      "role": "出库员",
      "operator": "张三",
      "reason": "",
      "temp_at_action": 4.5,
      "action_at": "2026-06-19T10:05:00.000000",
      "current_status": "已签收"
    }
    // ... arrive, receive, 以及其他箱子的完整历史
  ]
}
```

**CSV 导出**：表头 = `fields`，每行 = `rows[i]`

```bash
curl -sS http://localhost:8000/api/export/csv -o cold_chain_history.csv
```

CSV 内容示例：

```
box_code,sample_type,sequence,from_status,to_status,role,operator,reason,temp_at_action,action_at,current_status
BOX-V001,疫苗,1,,待出库,系统,导入,导入创建,4.0,2026-06-19T10:00:00.000000,已签收
BOX-V001,疫苗,2,待出库,转运中,出库员,张三,,4.5,2026-06-19T10:05:00.000000,已签收
BOX-V001,疫苗,3,转运中,待签收,转运员,李四,,5.0,2026-06-19T10:15:00.000000,已签收
BOX-V001,疫苗,4,待签收,已签收,库房签收员,王五,外观完好温度正常,4.2,2026-06-19T10:20:00.000000,已签收
```

**字段语义**：

| 字段 | 说明 |
|------|------|
| `box_code` | 箱码 |
| `sample_type` | 样本类型（从 boxes 表反查） |
| `sequence` | 该箱的第几次操作（从 1 递增） |
| `from_status` | 操作前状态，初次导入为空串 |
| `to_status` | 操作后状态 |
| `role` | 操作角色（出库员/转运员/库房签收员/管理员/系统） |
| `operator` | 操作人姓名 |
| `reason` | 操作原因；温度越界时前缀 `[温度越界]`；超时时前缀 `[转运超时]` |
| `temp_at_action` | 操作时报告的温度 |
| `action_at` | 操作发生时间 |
| `current_status` | 箱子当前的最终状态（导出瞬间 boxes.status） |

---

## 3. 失败路径（全部可复现）

### 3.1 重复箱码被拦截

```bash
# 导入 BOX-V001（DB 已存在）
curl -sS -X POST http://localhost:8000/api/boxes/import/json \
  -H "Content-Type: application/json" \
  -d @examples/boxes_existing_dup.json
# {"imported":[],"rejected":[{"box_code":"BOX-V001","reason":"箱码已存在"}]}

# 同批次内重复
curl -sS -X POST http://localhost:8000/api/boxes/import/json \
  -H "Content-Type: application/json" \
  -d @examples/boxes_batch_dup.json
# {"imported":["BOX-DUP1"],"rejected":[{"box_code":"BOX-DUP1","reason":"导入清单内重复"}]}
```

### 3.2 温度越界 → 自动进入「异常待处理」

```bash
# 先导入一个新箱
curl -sS -X POST http://localhost:8000/api/boxes/import/json \
  -H "Content-Type: application/json" \
  -d @examples/boxes_hot1.json

# 出库报 15°C（疫苗阈值 2~8°C）
curl -sS -X POST http://localhost:8000/api/boxes/BOX-HOT1/dispatch \
  -H "Content-Type: application/json" \
  -d @examples/dispatch_hot.json
```

返回：

```json
{
  "ok": true,
  "box_code": "BOX-HOT1",
  "from": "待出库",
  "to": "异常待处理",
  "warning": "温度越界，已自动转入异常待处理"
}
```

导出记录里 `reason` 会带上 `[温度越界]` 前缀。

### 3.3 越权：转运员不能代替库房签收

```bash
# 先完成 BOX-V002 的出库 + 到达
curl -sS -X POST http://localhost:8000/api/boxes/BOX-V002/dispatch \
  -H "Content-Type: application/json" \
  -d @examples/dispatch_ok.json

curl -sS -X POST http://localhost:8000/api/boxes/BOX-V002/arrive \
  -H "Content-Type: application/json" \
  -d @examples/arrive_ok.json

# 转运员尝试签收 → HTTP 403
curl -sS -X POST http://localhost:8000/api/boxes/BOX-V002/receive \
  -H "Content-Type: application/json" \
  -d @examples/receive_unauthorized.json
# 403 {"detail":"角色「转运员」无权执行 receive 操作，允许角色: ['库房签收员']"}
```

### 3.4 非法回退：终态不能被改

```bash
# BOX-V001 已签收 → 回退 → HTTP 409
curl -sS -X POST http://localhost:8000/api/boxes/BOX-V001/rollback \
  -H "Content-Type: application/json" \
  -d @examples/rollback_invalid.json
# 409 {"detail":"...「已签收」为终态，不可变更"}
```

> **关键保证**：3.3 和 3.4 这类失败请求**不会写入审计历史**。
> 导出和审计记录只会记录真正成功的操作。可通过：
> ```bash
> curl -sS "http://localhost:8000/api/audit?box_code=BOX-V001"
> ```
> 重复执行失败回退后，审计条数不变来验证。

---

## 4. 管理员操作 & 完整生命周期

```bash
# 标记 BOX-V002 异常（状态从 待签收 或 转运中 都可以）
curl -sS -X POST http://localhost:8000/api/boxes/BOX-V002/exception \
  -H "Content-Type: application/json" \
  -d @examples/mark_exception.json

# 恢复 BOX-HOT1（异常待处理 → 待出库，可重新走流程）
curl -sS -X POST http://localhost:8000/api/boxes/BOX-HOT1/recover \
  -H "Content-Type: application/json" \
  -d @examples/recover.json

# 正式回退 BOX-HOT1（样本报废，待出库 → 已回退 终态）
curl -sS -X POST http://localhost:8000/api/boxes/BOX-HOT1/rollback \
  -H "Content-Type: application/json" \
  -d @examples/rollback_ok.json
```

---

## 5. 重启持久化验证

```bash
# 1. 先做个快照
curl -sS http://localhost:8000/api/boxes > boxes_before.json
curl -sS http://localhost:8000/api/audit > audit_before.json
curl -sS http://localhost:8000/api/thresholds > thresholds_before.json
curl -sS http://localhost:8000/api/export/json > export_before.json

# 2. 停止服务（Ctrl+C），再启动
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3. 再查一遍，内容必须完全一致（除时间戳无关字段）
curl -sS http://localhost:8000/api/boxes
curl -sS http://localhost:8000/api/audit
curl -sS http://localhost:8000/api/thresholds
curl -sS http://localhost:8000/api/export/json
```

或直接运行自动化校验：

```bash
python run_examples.py persistence
```

---

## 6. 完整 API 端点一览

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/thresholds` | 创建/更新阈值（按 sample_type 覆盖） |
| `GET` | `/api/thresholds` | 列出所有阈值 |
| `GET` | `/api/thresholds/{sample_type}` | 查询单个阈值 |
| `DELETE` | `/api/thresholds/{sample_type}` | 删除阈值 |
| `POST` | `/api/boxes/import/json` | JSON 格式批量导入箱码 |
| `POST` | `/api/boxes/import/csv` | CSV 格式批量导入箱码 |
| `GET` | `/api/boxes` | 箱子列表（`?status=已签收` 过滤） |
| `GET` | `/api/boxes/{box_code}` | 查询单个箱子当前快照 |
| `POST` | `/api/boxes/{box_code}/dispatch` | 出库（出库员） |
| `POST` | `/api/boxes/{box_code}/arrive` | 转运到达（转运员） |
| `POST` | `/api/boxes/{box_code}/receive` | 库房签收（库房签收员） |
| `POST` | `/api/boxes/{box_code}/exception` | 标记异常（管理员） |
| `POST` | `/api/boxes/{box_code}/rollback` | 回退（管理员） |
| `POST` | `/api/boxes/{box_code}/recover` | 异常恢复→待出库（管理员） |
| `GET` | `/api/audit` | 审计记录（`?box_code=BOX-V001` 过滤） |
| `GET` | `/api/export/json` | **导出完整审计历史（JSON）**，包含角色/操作者/原因/时间 |
| `GET` | `/api/export/csv` | **导出完整审计历史（CSV）**，字段与 JSON 完全一致 |

---

## 7. 项目结构

```
zzz-00066/
├── app/
│   ├── __init__.py
│   ├── database.py       # SQLite 连接（WAL + FK）
│   ├── models.py         # Pydantic 数据模型
│   └── main.py           # 所有路由 + 状态机 + 导出聚合逻辑
├── examples/             # curl -d @file 用的请求体样例
│   ├── threshold_*.json
│   ├── boxes_*.json / boxes_*.csv
│   └── *.json            # 状态流转请求体
├── run_examples.py       # 一键跑完 README 所有链路（跨平台）
├── test_api.py           # API 回归测试（24+ 用例）
├── test_persistence.py   # 重启持久化回归测试
├── requirements.txt
└── README.md
```

---

## 8. 根因修复说明（本次相对初版的变更）

| 问题 | 根因 | 修复 |
|------|------|------|
| 导出只给箱子当前快照 | `export_*` 直接查 `boxes` 表，等同于「列出箱子」接口 | 改为从 `audit_log` 逐条聚合（`_build_export_rows`），每一行对应一次真实操作，字段语义与 `audit_log` 对齐 |
| JSON/CSV 字段语义不一致 | JSON 用 boxes 列、CSV 也用 boxes 列但顺序不同 | 抽出 `EXPORT_FIELDS` 常量，JSON 和 CSV 共用同一套字段定义和同一数据源 |
| README 链路在 PowerShell 复现不出 | PowerShell 中单引号不被 shell 解析、中文参数编码有坑 | 提供 `examples/*.json` + `curl -d @file` 方案；额外提供 `python run_examples.py` 一键脚本 |
| 样例数据不完整 | 没有现成请求体文件 | 补齐 16 个样例文件覆盖阈值/导入/所有状态流转 |
| 没有批次能力 | 只能单箱操作，无法批量管理 | 新增 batches / batch_boxes / batch_audit_log 三张表，支持批次创建、批量导入、批次出库/到达/签收、缺失箱登记与撤销、批次审计、按批次导出 |
| 批次内样本类型不一致 | 不同样本类型阈值不同，混装会出错 | 导入时强校验：同一批次所有箱子必须同一样本类型，不一致直接拦截 |
| 箱子跨批次冲突 | 一箱可能被重复加入多个批次 | 导入时校验：箱子已在其他未完成批次中则拦截 |
| 签收只能整箱整批 | 实际物流中常有部分到货、部分缺失 | 支持部分签收 + 缺失箱登记，批次进入「部分签收」状态，补到后可继续签收 |
| 缺失登记无法撤销 | 登记错了或箱子找到后无法回退 | 管理员可撤销缺失登记，撤销后箱子恢复「待签收」可继续走流程 |
| 批次操作没有审计 | 无法追溯批次级别的操作 | 独立 batch_audit_log 表，记录每次批次操作的操作人、角色、原因、详情 |
| 导出无法按批次筛选 | 只能全量导出，分析困难 | JSON/CSV 导出均支持 `?batch_no=xxx` 筛选，JSON 导出附带 batch_summary 汇总信息 |
