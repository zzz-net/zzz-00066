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

围绕已签收或已归档批次发起争议单，记录箱号、问题类型、证据说明、责任判断、处理时限和最终结论。支持班组长代理一线员工补录异常单。

### 状态机

```
                    库房签收员/仓库主管/质控/班组长
                      创建争议单（班组长可代理）
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
| 创建争议单 | 库房签收员、仓库主管、质控、班组长 |
| 代理提交争议单 | 班组长、仓库主管、质控（需开启 allow_proxy_submit 配置） |
| 确认/驳回 | 仓库主管、质控（单确认模式下仅仓库主管） |
| 补充证据 | 库房签收员、仓库主管、质控 |
| 撤回 | **仅创建人本人**（按 operator 精确匹配，同角色不同人不可越权） |
| 重开（撤回后）| **仅创建人本人** |
| 重新提交（驳回后）| **仅创建人本人** |
| 结案 | 仓库主管、质控 |

**防护规则**：

- 重复建单拦截：同一批次有活跃（待确认/处理中）工单时，不可再创建
- 跨批次混填拦截：箱号必须在指定批次的已签收箱子中
- 越权结案拦截：仅仓库主管和质控可结案，签收员不可
- 配置切换：双确认配置变更只影响新工单，已创建的工单保持创建时的配置
- 代理提交配置切换：allow_proxy_submit 变更只影响新单，不影响已有工单
- 创建人本人校验：撤回/重开/重新提交严格匹配创建人 operator，同角色不同人不可越权

### 代理提交机制

工单中区分「创建人」和「实际提交人」：

| 字段 | 含义 | 普通提交 | 代理提交 |
|------|------|---------|---------|
| `created_by` | 创建人（被代理人/一线员工） | operator 本身 | on_behalf_of 指定的员工 |
| `submitted_by` | 实际提交人（代理人/班组长） | null | 代理人 operator |
| `proxy_submitted` | 是否为代理提交 | false | true |
| `created_role` | 提交角色 | 创建人角色 | 代理人角色 |

**关键设计**：
- 代理提交时，`created_by` 设为被代理人（`on_behalf_of`），确保撤回/重开/重新提交的权限属于被代理人本人
- 代理提交人（班组长）仅负责补录创建，不可代为撤回/重开/重新提交
- 未开启 `allow_proxy_submit` 时，传入 `on_behalf_of` 会被 403 拦截

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

## 6.6 仓内异常处置单模块

围绕仓内发现的异常（温度异常、包装破损、数量差异、标签错误等）发起处置单，**明确分离发起人、代录人、当前处理人三个独立字段**，支持补充图片或文字证据、转交责任人、完整操作流水记录。转交责任后导出 CSV 可清晰看到完整责任流转链。

### 核心设计：三字段分离

| 字段 | 含义 | 普通创建 | 代录创建 |
|------|------|---------|---------|
| `initiator` | **发起人**（真正的异常发现人，拥有撤回/重提权限） | operator 本人 | on_behalf_of 指定的员工 |
| `proxy_recorder` | **代录人**（代为录入系统的班组长/主管） | null | 代录的 operator |
| `current_handler` | **当前处理人**（当前负责处置的责任人） | 发起人（初始值） | 发起人（初始值），转交后更新 |

### 状态机

```
            库房管理员/仓库主管/质控/班组长/库房签收员
                    创建处置单（班组长可代录）
                            │
                            ▼
                        待处理 ──────── 仓库主管/质控驳回 ────────→ 已驳回
                     │      │                                       │
      仓库主管确认：   │      │                                      │ 发起人
      仓库主管确认    │      │                                      │ 重新提交
                     │      │                                      │ (补充证据)
                     ▼      ▼                                       ▼
                     处理中 ←─────────────────────────────── 待处理
                  │      │
       仓库主管/  │      │ 发起人
       质控结案   │      │ 撤回
       转交责任人 │      │
                  ▼      ▼
                已结案  已撤回
                          │
                          │ 发起人重提
                          ▼
                        待处理
```

**角色权限**：

| 动作 | 允许角色 |
|------|---------|
| 创建处置单 | 库房管理员、仓库主管、质控、班组长、库房签收员 |
| 代录处置单 | 班组长、仓库主管、质控（需开启 allow_proxy_record 配置） |
| 确认/驳回 | 仓库主管、质控 |
| 补充证据 | 库房管理员、仓库主管、质控、库房签收员 |
| 转交责任人 | 仓库主管、质控、库房管理员 |
| 撤回 | **仅发起人本人**（严格按 initiator 精确匹配，同角色不同人不可越权） |
| 重提（撤回后）| **仅发起人本人** |
| 重新提交（驳回后）| **仅发起人本人** |
| 结案 | 仓库主管、质控 |

**原因分类**：`温度异常`、`包装破损`、`数量差异`、`标签错误`、`污染风险`、`设备故障`、`流程违规`、`其他`

**防护规则**：

- **重复报单拦截**：同批次号 + 同箱号 + 同原因分类的活跃（待处理/处理中）工单不可重复创建，409 拦截
- **撤回后重提重新校验权限**：重提时再次校验 `operator == initiator`，同时检查是否存在重复报单
- **配置切换只影响新单**：`allow_proxy_record` 变更仅影响新建工单，已有工单的 `allow_proxy_record_at_create` 冻结不变
- **越权操作全面拦截**：撤回、重提、重新提交严格校验 `operator == initiator`，同角色不同人一律 403

### 代录机制

班组长或主管代一线员工录入时：
- `initiator` = `on_behalf_of`（被代理人，真正的发起人，享有撤回/重提权限）
- `proxy_recorder` = `operator`（实际代录人）
- `current_handler` = `initiator`（初始处理人为发起人）
- 代录人**不享有**撤回/重提权限，只有被代理人（发起人）可以操作
- 未开启 `allow_proxy_record` 时，传入 `on_behalf_of` 会被 403 拦截

### 责任人转交与流转记录

处理中状态下可转交责任人，每次转交都会：
1. 更新 `current_handler` 和 `current_handler_role`
2. 向 `exception_handler_transfers` 表写入一条转交记录（含转交原因、转交人、时间）
3. JSON 导出包含 `responsibility_chain`（完整责任流转链：初始→每次转交→当前）
4. CSV 导出的 `responsibility_transfers` 字段格式：`初始:发起人(角色) → A→B(角色)[转交人@时间] → ... → 当前:处理人(角色)`

### 仓内异常处置单 curl 示例

```bash
# 配置：允许代录
curl -sS -X POST http://localhost:8000/api/exception/config \
  -H "Content-Type: application/json" \
  -d @examples/exception_config_proxy_enabled.json

# 配置：禁止代录
curl -sS -X POST http://localhost:8000/api/exception/config \
  -H "Content-Type: application/json" \
  -d @examples/exception_config_proxy_disabled.json

# 查询配置
curl -sS http://localhost:8000/api/exception/config

# 库房签收员创建处置单（普通方式）
curl -sS -X POST http://localhost:8000/api/exception/tickets \
  -H "Content-Type: application/json" \
  -d @examples/exception_create.json

# 班组长代录处置单（需开启 allow_proxy_record）
curl -sS -X POST http://localhost:8000/api/exception/tickets \
  -H "Content-Type: application/json" \
  -d @examples/exception_create_proxy.json

# 代录关闭时代录被拦截
curl -sS -X POST http://localhost:8000/api/exception/tickets \
  -H "Content-Type: application/json" \
  -d @examples/exception_create_proxy_blocked.json

# 列出处置单（支持 ?status= / ?batch_no= / ?box_code= 筛选）
curl -sS "http://localhost:8000/api/exception/tickets"
curl -sS "http://localhost:8000/api/exception/tickets?status=处理中"
curl -sS "http://localhost:8000/api/exception/tickets?batch_no=BATCH-EXC-001"
curl -sS "http://localhost:8000/api/exception/tickets?box_code=EXC001"

# 查看工单详情（含证据列表、转交历史、审计日志）
curl -sS http://localhost:8000/api/exception/tickets/1

# 仓库主管确认 → 进入处理中
curl -sS -X POST http://localhost:8000/api/exception/tickets/1/confirm \
  -H "Content-Type: application/json" \
  -d @examples/exception_confirm.json

# 驳回处置单
curl -sS -X POST http://localhost:8000/api/exception/tickets/1/reject \
  -H "Content-Type: application/json" \
  -d @examples/exception_reject.json

# 撤回处置单（仅发起人本人）
curl -sS -X POST http://localhost:8000/api/exception/tickets/1/withdraw \
  -H "Content-Type: application/json" \
  -d @examples/exception_withdraw.json

# 重新提交（驳回/撤回后，仅发起人本人，可补充描述）
curl -sS -X POST http://localhost:8000/api/exception/tickets/1/resubmit \
  -H "Content-Type: application/json" \
  -d @examples/exception_resubmit.json

# 补充证据（图片或文字）
curl -sS -X POST http://localhost:8000/api/exception/tickets/1/evidence \
  -H "Content-Type: application/json" \
  -d @examples/exception_evidence.json

# 转交责任人
curl -sS -X POST http://localhost:8000/api/exception/tickets/1/transfer \
  -H "Content-Type: application/json" \
  -d @examples/exception_transfer.json

# 结案（仅仓库主管/质控）
curl -sS -X POST http://localhost:8000/api/exception/tickets/1/close \
  -H "Content-Type: application/json" \
  -d @examples/exception_close.json

# 查看工单审计日志
curl -sS http://localhost:8000/api/exception/tickets/1/audit

# 查看批次异常汇总
curl -sS http://localhost:8000/api/exception/batches/BATCH-EXC-001/summary

# 导出 JSON（支持 ?status= / ?batch_no= / ?box_code= 筛选，含责任流转链）
curl -sS "http://localhost:8000/api/exception/export/json"
curl -sS "http://localhost:8000/api/exception/export/json?status=已结案"
curl -sS "http://localhost:8000/api/exception/export/json?batch_no=BATCH-EXC-001"

# 导出 CSV（含 responsibility_transfers 字段，清晰展示责任流转）
curl -sS "http://localhost:8000/api/exception/export/csv" -o exception_tickets.csv
```

### 仓内异常处置单 API 端点一览

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/exception/config` | 查询代录配置 |
| `POST` | `/api/exception/config` | 更新代录配置（只影响新工单） |
| `POST` | `/api/exception/tickets` | 创建处置单（支持代录） |
| `GET` | `/api/exception/tickets` | 工单列表（`?status=` `?batch_no=` `?box_code=` 筛选） |
| `GET` | `/api/exception/tickets/{id}` | 工单详情（含证据、转交历史、审计日志） |
| `POST` | `/api/exception/tickets/{id}/confirm` | 确认工单（仓库主管/质控） |
| `POST` | `/api/exception/tickets/{id}/reject` | 驳回工单（仓库主管/质控） |
| `POST` | `/api/exception/tickets/{id}/withdraw` | 撤回工单（仅发起人本人） |
| `POST` | `/api/exception/tickets/{id}/resubmit` | 重新提交（仅发起人本人，从驳回/撤回） |
| `POST` | `/api/exception/tickets/{id}/evidence` | 补充证据（文字/图片） |
| `POST` | `/api/exception/tickets/{id}/transfer` | 转交责任人（仓库主管/质控/库房管理员） |
| `POST` | `/api/exception/tickets/{id}/close` | 结案（仅仓库主管/质控） |
| `GET` | `/api/exception/tickets/{id}/audit` | 工单审计日志 |
| `GET` | `/api/exception/batches/{batch_no}/summary` | 批次异常汇总 |
| `GET` | `/api/exception/export/json` | 导出 JSON（含责任流转链 responsibility_chain） |
| `GET` | `/api/exception/export/csv` | 导出 CSV（含 responsibility_transfers 字段） |

### 仓内异常处置单回归测试

```bash
# 覆盖 7 大场景：正常闭环、越权拦截、撤回重开、转交责任、配置切换、重启恢复、导出核对
python test_exception_comprehensive.py
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
| 缺少异常追责能力 | 签收后发现问题无法追溯和追责 | 新增异常追责工单模块：5 状态（待确认/处理中/已驳回/已撤回/已结案）、支持单/双确认配置、证据补充、撤回重开、驳回重提、完整审计日志 |
| 争议工单重复建单 | 同一批次可能被反复开单 | 同批次有活跃工单时禁止新建，409 拦截 |
| 跨批次箱号混填 | 争议单关联了不属于该批次的箱子 | 创建时校验所有箱号必须在指定批次的已签收箱子中 |
| 越权结案 | 签收员可能越权关闭工单 | 结案操作仅仓库主管和质控可执行，签收员 403 |
| 配置变更影响已有工单 | 切换双确认后旧工单被改变 | 配置冻结在创建时，变更只影响新工单 |
| 争议数据重启丢失 | 处理中的工单状态可能丢失 | 全部存储在 SQLite WAL 模式，重启后完整恢复 |
| 缺少代理提交能力 | 班组长无法代一线员工补录异常单 | 新增 `allow_proxy_submit` 配置和 `on_behalf_of` 参数，代理提交时 `created_by` 设为被代理人，`submitted_by` 记录实际提交人 |
| **同角色不同人可越权撤回/重开/重新提交** | 撤回/重开/重新提交使用 `operator != created_by AND role != created_role` 的 OR 逻辑，同角色不同人可绕过 | 改为 `operator != created_by` 严格匹配创建人本人，同角色不同人不再放行 |
| 代理提交配置回写历史单 | 切换配置后已有代理工单可能受影响 | `allow_proxy_submit` 配置仅控制新建时是否允许代理提交，已有工单的 `proxy_submitted` 标记不受配置变更影响 |
| 发起人/代录人/处理人三字段混用 | 历史上只有一个「创建人」字段，无法区分真正发起人和代录人 | 新增 `exception_tickets` 主表三字段分离：`initiator`（发起人）、`proxy_recorder`（代录人）、`current_handler`（当前处理人），各自独立角色字段 |
| 异常单缺少批次/箱号信息 | 无法关联到具体的物流批次或具体箱子 | `exception_tickets` 表新增 `batch_no`、`box_code`、`reason_category` 核心业务字段，支持批次维度汇总查询 |
| 缺少图片/文字证据 | 异常没有证据链，处置缺乏依据 | 独立 `exception_evidence` 表，支持 `evidence_type=text/image`，记录证据内容、提交人、提交时间 |
| 操作无流水记录 | 无法追溯谁在何时做了什么 | 独立 `exception_audit_log` 表，每次状态变更、证据补充、转交都写入审计日志 |
| 无法转交责任人 | 异常责任不能在不同角色间流转 | 新增 `exception_handler_transfers` 表 + `/transfer` 接口，每次转交完整记录：转出人/转入人/转交原因/转交人/时间 |
| 导出看不到责任流转 | CSV 只有当前处理人，无法追溯责任链 | CSV 导出新增 `responsibility_transfers` 链状字段：`初始:发起人(角色) → A→B(角色)[转交人@时间] → ... → 当前:处理人(角色)`，JSON 导出含完整 `responsibility_chain` 列表 |
| 代录无配置开关 | 不能灵活控制是否允许班组长代录 | 独立 `exception_config` 表单例配置 `allow_proxy_record`，新建时冻结 `allow_proxy_record_at_create` 到工单，配置变更只影响新单 |
| 同箱号同原因重复报单 | 同一异常被反复建单造成管理混乱 | 创建和重提时检查 `batch_no+box_code+reason_category` + 活跃状态（待处理/处理中），冲突返回 409 拦截 |
| 撤回后重提权限校验不严 | 同角色不同人可能越权重开他人工单 | 重提时再次严格校验 `operator == initiator`，同时重新检查重复报单拦截 |
| 异常单重启丢失 | 处理中状态无法持久化 | 所有表（5张）使用 SQLite WAL 模式持久化，`init_db()` 自动重建表结构，服务重启后状态完整恢复 |
| 越权操作无拦截 | 同角色不同人可随意操作他人工单 | 撤回/重提/重新提交严格 `operator == initiator`，其他操作按角色白名单控制，越权一律 403 |
