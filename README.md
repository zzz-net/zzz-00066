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

### 交接复核全面回归测试

```bash
# 覆盖 6 大场景：正常归档、越权拦截、撤销重开、补签冲突、重启恢复、导出核对
python test_review_comprehensive.py
```

> 所有请求体都从 `examples/*.json` 读取，避免 PowerShell / bash 之间的引号、中文编码差异。

---

## 1. 状态机 & 角色矩阵

```
                     出库员                     转运员                   库房签收员
待出库 ──────────────→ 转运中 ────────────────→ 待签收 ────────────────→ 已签收
  │                      │                       │                          │
  │                      │         管理员         │                          │ 仓库主管
  ├──────────────────────┴───────────┬───────────┤                          │ 发起复核
  │                                  │           │                          ▼
  │                        +──→ 异常待处理 ←─────+                    复核中
  │管理员                    管理员 │              │                    │    │
  └────→ 已回退(终态)      ┌────────┴──────┐      │ 全部复核完成       │    │ 撤销
                          │               │      ▼                    │    ▼
                    管理员→ 待出库     ← 管理员  已归档(终态)◄─────────┘   未开始
                              │
                              └──→ 已回退(终态)
```

**角色权限**：

| 动作 | 允许角色 |
|------|---------|
| dispatch 出库 | 出库员 |
| arrive 转运到达 | 转运员 |
| receive 库房签收（含补签） | 库房签收员 |
| exception 标记异常 / rollback 回退 / recover 恢复 / 撤销缺失登记 | 管理员 |
| 发起复核 / 按箱复核 / 撤销复核 / 归档 | 仓库主管 |

**终态**：`已回退`、`已归档` — 不可再变更。
**已签收后仍可操作**：补签箱子、登记缺失、撤销缺失、发起交接复核。
**配置项**：是否启用**双人复核**（配置变更只影响新开的复核单）。

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

## 6.5 异常追责工单模块

围绕已签收或已归档批次发起争议单，记录箱号、问题类型、证据说明、责任判断、处理时限和最终结论。

### 状态机

```
                        库房签收员/仓库主管/质控
                          创建争议单
                              │
                              ▼
                          待确认 ──────── 仓库主管/质控驳回 ────────→ 已驳回
                       │      │                                   │
            单确认：   │      │ 双确认：                           │ 创建人
            仓库主管确认│      │ 主管+质控都确认                   │ 重新提交
                       │      │ 才进入处理中                       │ (补充证据)
                       ▼      ▼                                    ▼
                         处理中 ←─────────────────────────────── 待确认
                      │      │
           仓库主管/  │      │ 创建人
           质控结案   │      │ 撤回
                      ▼      ▼
                    已结案  已撤回
                              │
                              │ 创建人重开
                              ▼
                            待确认
```

**角色权限**：

| 动作 | 允许角色 |
|------|---------|
| 创建争议单 | 库房签收员、仓库主管、质控 |
| 确认/驳回 | 仓库主管、质控（单确认模式下仅仓库主管） |
| 补充证据 | 库房签收员、仓库主管、质控 |
| 撤回 | 仅创建人 |
| 重开（撤回后）| 仅创建人 |
| 重新提交（驳回后）| 仅创建人 |
| 结案 | 仓库主管、质控 |

**防护规则**：

- 重复建单拦截：同一批次有活跃（待确认/处理中）工单时，不可再创建
- 跨批次混填拦截：箱号必须在指定批次的已签收箱子中
- 越权结案拦截：仅仓库主管和质控可结案，签收员不可
- 配置切换：双确认配置变更只影响新工单，已创建的工单保持创建时的配置

### 异常追责工单 curl 示例

```bash
# 配置单确认模式
curl -sS -X POST http://localhost:8000/api/dispute/config \
  -H "Content-Type: application/json" \
  -d @examples/dispute_config_single.json

# 配置双确认模式
curl -sS -X POST http://localhost:8000/api/dispute/config \
  -H "Content-Type: application/json" \
  -d @examples/dispute_config_double.json

# 创建争议单
curl -sS -X POST http://localhost:8000/api/dispute/tickets \
  -H "Content-Type: application/json" \
  -d @examples/dispute_create.json

# 查询争议配置
curl -sS http://localhost:8000/api/dispute/config

# 列出争议工单（支持 ?status= 和 ?batch_no= 筛选）
curl -sS "http://localhost:8000/api/dispute/tickets?status=待确认"
curl -sS "http://localhost:8000/api/dispute/tickets?batch_no=BATCH-DSP-001"

# 查看工单详情（含箱号、证据列表）
curl -sS http://localhost:8000/api/dispute/tickets/1

# 仓库主管确认
curl -sS -X POST http://localhost:8000/api/dispute/tickets/1/confirm \
  -H "Content-Type: application/json" \
  -d @examples/dispute_confirm.json

# 质控确认（双确认模式下）
curl -sS -X POST http://localhost:8000/api/dispute/tickets/1/confirm \
  -H "Content-Type: application/json" \
  -d @examples/dispute_confirm_qc.json

# 驳回争议单
curl -sS -X POST http://localhost:8000/api/dispute/tickets/1/reject \
  -H "Content-Type: application/json" \
  -d @examples/dispute_reject.json

# 撤回争议单（仅创建人）
curl -sS -X POST http://localhost:8000/api/dispute/tickets/1/withdraw \
  -H "Content-Type: application/json" \
  -d @examples/dispute_withdraw.json

# 重开争议单（仅创建人，从已撤回到待确认）
curl -sS -X POST http://localhost:8000/api/dispute/tickets/1/reopen \
  -H "Content-Type: application/json" \
  -d @examples/dispute_reopen.json

# 重新提交（驳回后，可补充证据）
curl -sS -X POST http://localhost:8000/api/dispute/tickets/1/resubmit \
  -H "Content-Type: application/json" \
  -d @examples/dispute_resubmit.json

# 补充证据
curl -sS -X POST http://localhost:8000/api/dispute/tickets/1/evidence \
  -H "Content-Type: application/json" \
  -d @examples/dispute_evidence.json

# 结案（仅仓库主管/质控）
curl -sS -X POST http://localhost:8000/api/dispute/tickets/1/close \
  -H "Content-Type: application/json" \
  -d @examples/dispute_close.json

# 查看工单审计日志
curl -sS http://localhost:8000/api/dispute/tickets/1/audit

# 查看批次争议汇总
curl -sS http://localhost:8000/api/dispute/batches/BATCH-DSP-001/summary

# 导出争议工单 JSON（支持 ?status= 和 ?batch_no= 筛选）
curl -sS "http://localhost:8000/api/dispute/export/json"
curl -sS "http://localhost:8000/api/dispute/export/json?status=已结案"

# 导出争议工单 CSV
curl -sS "http://localhost:8000/api/dispute/export/csv" -o dispute_tickets.csv
```

### 异常追责工单 API 端点一览

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/dispute/config` | 查询争议配置（是否双确认） |
| `POST` | `/api/dispute/config` | 更新争议配置（只影响新工单） |
| `POST` | `/api/dispute/tickets` | 创建争议单 |
| `GET` | `/api/dispute/tickets` | 工单列表（`?status=` `?batch_no=` 筛选） |
| `GET` | `/api/dispute/tickets/{id}` | 工单详情（含箱号、证据列表） |
| `POST` | `/api/dispute/tickets/{id}/confirm` | 确认工单 |
| `POST` | `/api/dispute/tickets/{id}/reject` | 驳回工单 |
| `POST` | `/api/dispute/tickets/{id}/withdraw` | 撤回工单（仅创建人） |
| `POST` | `/api/dispute/tickets/{id}/reopen` | 重开工单（仅创建人，从已撤回） |
| `POST` | `/api/dispute/tickets/{id}/resubmit` | 重新提交（仅创建人，从已驳回） |
| `POST` | `/api/dispute/tickets/{id}/close` | 结案（仅仓库主管/质控） |
| `POST` | `/api/dispute/tickets/{id}/evidence` | 补充证据 |
| `GET` | `/api/dispute/tickets/{id}/audit` | 工单审计日志 |
| `GET` | `/api/dispute/batches/{batch_no}/summary` | 批次争议汇总 |
| `GET` | `/api/dispute/export/json` | 导出争议工单 JSON |
| `GET` | `/api/dispute/export/csv` | 导出争议工单 CSV |

### 异常追责工单回归测试

```bash
# 覆盖 6 大场景：正常闭环、越权拦截、撤回重开、配置切换、重启恢复、导出核对
python test_dispute_comprehensive.py
```

---

## 7. 项目结构

```
zzz-00066/
├── app/
│   ├── __init__.py
│   ├── database.py       # SQLite 连接（WAL + FK）+ 数据库迁移
│   ├── models.py         # Pydantic 数据模型（含批次相关模型）
│   └── main.py           # 所有路由 + 状态机 + 导出聚合逻辑
├── examples/             # curl -d @file 用的请求体样例
│   ├── threshold_*.json
│   ├── boxes_*.json / boxes_*.csv
│   ├── batch_*.json      # 批次相关请求体
│   └── *.json            # 状态流转请求体
├── run_examples.py       # 一键跑完 README 所有链路（跨平台）
├── test_api.py           # API 回归测试（24+ 用例）
├── test_persistence.py   # 重启持久化回归测试
├── test_batch_smoke.py   # 批次功能基础冒烟测试
├── test_batch_comprehensive.py  # 批次签收全面回归测试（6大场景）
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
| 样例数据不完整 | 没有现成请求体文件 | 补齐 25+ 个样例文件覆盖阈值/导入/所有状态流转/批次操作 |
| 没有批次能力 | 只能单箱操作，无法批量管理 | 新增 batches / batch_boxes / batch_audit_log 三张表，支持批次创建、批量导入、批次出库/到达/签收、缺失箱登记与撤销、批次审计、按批次导出 |
| 批次内样本类型不一致 | 不同样本类型阈值不同，混装会出错 | 导入时强校验：同一批次所有箱子必须同一样本类型，不一致直接拦截 |
| 箱子跨批次冲突 | 一箱可能被重复加入多个批次 | 导入时校验：箱子已在其他未完成批次中则拦截 |
| 签收只能整箱整批 | 实际物流中常有部分到货、部分缺失 | 支持部分签收 + 缺失箱登记，批次进入「部分签收」状态，补到后可继续签收 |
| **登记缺失后整批待办被清空** | 缺失登记逻辑错误地清空了待办 | 修复 `pending_todos` 计算逻辑，只排除终态和缺失箱，未签收且未缺失的箱子保留在待办 |
| **撤销缺失时回退已签收进度** | 状态机逻辑错误，撤销后批次从「部分签收」回退为「待签收」 | 修复 `cancel_missing` 状态判断：撤销后根据实际已签收数量判断状态，已签收 >0 则保持「部分签收」 |
| **缺失箱补到后无法继续到达** | 批次到达只允许从「转运中」状态开始 | 扩展 `BATCH_TRANSITIONS.arrive.from` 允许从「部分签收」状态到达，智能跳过已处理箱子 |
| **重复到达/签收报错** | 没有幂等性处理，重复操作会报错 | 添加 `skip_count` 统计，已处理箱子智能跳过不报错，返回明确的成功/跳过/失败计数 |
| **已签收箱子状态被错误修改** | 批次操作时没有检查单箱状态 | 批次操作时逐箱检查状态，已签收箱子自动跳过，状态不被修改 |
| 缺失登记无法撤销 | 登记错了或箱子找到后无法回退 | 管理员可撤销缺失登记，撤销后箱子恢复「待签收」可继续走流程 |
| **撤销缺失无操作记录** | 没有记录谁在何时撤销了缺失 | 新增 `missing_cancelled_at/by/reason` 字段，完整记录撤销操作信息 |
| **撤销缺失权限控制不严** | 缺少角色校验 | `cancel_missing` 严格校验仅「管理员」角色可执行，越权返回 403 |
| 批次操作没有审计 | 无法追溯批次级别的操作 | 独立 batch_audit_log 表，记录每次批次操作的操作人、角色、原因、详情 |
| 导出无法按批次筛选 | 只能全量导出，分析困难 | JSON/CSV 导出均支持 `?batch_no=xxx` 筛选，JSON 导出附带 batch_summary、batch_boxes、batch_audit_log 完整信息 |
| **导出缺少批次汇总信息** | 只有单箱操作记录 | 按批次导出时附带完整汇总：总数、已签收数、缺失数、待办数、待办列表、各箱子明细、操作时间线 |
| **服务重启后状态丢失** | 内存状态未持久化 | 所有状态完全存储在 SQLite，数据库使用 WAL 模式，重启后通过 `init_db()` 自动加载，所有中间状态（部分签收、缺失登记等）完整保留 |
| 缺少自动化回归测试 | 只能手动测试 | 提供 `test_batch_comprehensive.py` 覆盖 6 大场景：部分签收流程、撤销不回退、越权拦截、重复到达拦截、导出核对、重启持久化 |
