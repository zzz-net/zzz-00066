"""
工单服务重构回归测试 (v2)
覆盖 4 大场景：跨批次重复拦截、关闭后再建单、撤回重提责任重算、导出字段和审计对账
针对三类工单：Exception（异常处置）、Liability（代录责任）、Proxy Report（异常代报）
使用时间戳生成完全唯一的 ID，避免数据冲突。
"""

import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import csv
import io
from datetime import datetime

BASE = "http://localhost:8765"
TS = datetime.now().strftime('%H%M%S')

passed = 0
failed = 0
warnings = []


def uid(prefix):
    """生成完全唯一的标识符前缀"""
    return f"{prefix}{TS}"


def api(method, path, data=None, raw=False):
    url = f"{BASE}{urllib.parse.quote(path, safe='/:=&?[]@!$\'()*,;')}"
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw_body = resp.read()
            if raw:
                return resp.status, raw_body.decode("utf-8-sig")
            try:
                return resp.status, json.loads(raw_body.decode("utf-8"))
            except Exception:
                return resp.status, raw_body.decode("utf-8")
    except urllib.error.HTTPError as e:
        raw_body = e.read()
        try:
            return e.code, json.loads(raw_body.decode("utf-8"))
        except Exception:
            return e.code, raw_body.decode("utf-8")


def test(name, status, expected_status=200, check=None, warn_only=False):
    global passed, failed
    ok = status == expected_status
    extra = ""
    if ok and check is not None:
        try:
            if hasattr(check, '__call__'):
                ok = bool(check())
            else:
                ok = bool(check)
        except Exception as e:
            ok = False
            extra = f" [check exception: {e}]"
    if warn_only and not ok:
        warnings.append(f"{name} -> HTTP {status}{extra}")
        print(f"  [WARN] {name} -> HTTP {status}{extra}")
        return
    if ok:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} -> HTTP {status}{extra}")


def wait_for_service(timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        try:
            s, _ = api("GET", "/api/thresholds")
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def create_batch(batch_no, box_codes, skip_errors=False):
    """创建批次 + 箱号导入 + 派发 + 到货 + 签收"""
    api("POST", "/api/batches", {
        "batch_no": batch_no,
        "sample_type": "疫苗",
        "operator": "管理员A",
    })
    boxes = [
        {"box_code": bc, "sample_type": "疫苗", "current_temp": 4.5}
        for bc in box_codes
    ]
    api("POST", "/api/boxes/import/json", {
        "batch_no": batch_no,
        "boxes": boxes,
    })
    api("POST", f"/api/batches/{batch_no}/dispatch", {
        "role": "出库员", "operator": "张三", "current_temp": 4.0,
    })
    api("POST", f"/api/batches/{batch_no}/arrive", {
        "role": "转运员", "operator": "李四", "current_temp": 5.0,
    })
    status, data = api("POST", f"/api/batches/{batch_no}/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": [b["box_code"] for b in boxes],
    })
    return boxes, status, data


def main():
    global passed, failed
    print("=" * 70)
    print(f"  工单服务重构回归测试 v2 (TS={TS})")
    print("=" * 70)
    print()

    if not wait_for_service():
        print("ERROR: 服务启动超时")
        sys.exit(1)

    print("=== 阶段0: 初始化配置 ===")
    api("POST", "/api/thresholds", {
        "sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120
    })
    api("POST", "/api/exception/config", {"allow_proxy_record": True, "operator": "管理员A"})
    api("POST", "/api/liability/config", {"allow_proxy_record": True, "operator": "管理员A"})
    api("POST", "/api/proxy_report/config", {"allow_proxy_record": True, "operator": "管理员A"})
    print("  配置完成: 阈值设置，三代录开关全部开启")
    print()

    # =========================================================================
    print("=== 场景1: 跨批次重复拦截 - Exception (新去重口径: 箱号+事由) ===")
    print()

    BOX_A = uid("BXA")
    BOX_B = uid("BXB")  # 不同的箱号，但异常单将使用 BOX_A
    BATCH_1 = uid("BAT1")
    BATCH_2 = uid("BAT2")
    REASON = "温度异常"

    print(f"  使用标识: batch1={BATCH_1}, batch2={BATCH_2}, shared_box_for_ticket={BOX_A}")
    print()

    print("  1.1 创建批次 BATCH_1 (含箱号 BOX_A) 并完成签收")
    create_batch(BATCH_1, [BOX_A])
    print("  批次1创建完成")
    print()

    print("  1.2 库房签收员在 BATCH_1 上对 BOX_A 发起异常处置单 (事由: 温度异常)")
    s, d = api("POST", "/api/exception/tickets", {
        "role": "库房签收员",
        "operator": "王五",
        "batch_no": BATCH_1,
        "box_code": BOX_A,
        "reason_category": REASON,
        "description": "场景1: 批次1的第一张单",
    })
    exc_id_1 = d["ticket_id"] if s == 200 else None
    test("第一张异常处置单创建成功", s, 200, exc_id_1 is not None)
    print()

    print("  1.3 先创建 BATCH_2 (不同批次，使用独立箱号避免签收冲突)，再尝试同箱号+同事由建单 -> 必须 409 拦截 (新口径)")
    create_batch(BATCH_2, [uid("BXDIF")])  # 独立箱号，我们只需要 batch_no 存在
    s, d = api("POST", "/api/exception/tickets", {
        "role": "库房签收员",
        "operator": "王五",
        "batch_no": BATCH_2,  # 不同批次
        "box_code": BOX_A,   # 同箱号
        "reason_category": REASON,  # 同事由
        "description": "场景1: 跨批次重复，应被拦截",
    })
    msg = d.get("detail", "") if isinstance(d, dict) else str(d)
    test("跨批次同箱号同事由建单 被409拦截", s, 409,
         "重复" in msg or "已存在" in msg)
    print()

    print("  1.4 关闭第一张单 后 -> 再次建单应允许 (新流程)")
    api("POST", f"/api/exception/tickets/{exc_id_1}/confirm", {
        "role": "仓库主管", "operator": "管理员A",
    })
    s, _ = api("POST", f"/api/exception/tickets/{exc_id_1}/close", {
        "role": "仓库主管", "operator": "管理员A",
        "conclusion": "已解决，允许再次建单",
    })
    test("第一张处置单关闭成功", s, 200)

    s, d = api("POST", "/api/exception/tickets", {
        "role": "库房签收员",
        "operator": "王五",
        "batch_no": BATCH_1,
        "box_code": BOX_A,
        "reason_category": REASON,
        "description": "场景1: 关闭后重建",
    })
    exc_id_2 = d["ticket_id"] if s == 200 else None
    test("关闭后 再次建单 成功 (新工单流程)", s, 200, exc_id_2 is not None)
    test("关闭后新工单 ID 不同于原工单", 200, 200, exc_id_2 != exc_id_1)
    print()

    print("  1.5 审计日志检查: 两张工单独立存在")
    s1, audit1 = api("GET", f"/api/exception/tickets/{exc_id_1}/audit")
    s2, audit2 = api("GET", f"/api/exception/tickets/{exc_id_2}/audit")
    both_have_audit = isinstance(audit1, list) and len(audit1) > 0 and isinstance(audit2, list) and len(audit2) > 0
    different_tickets_in_audit = both_have_audit and audit1[0].get("ticket_id") != audit2[0].get("ticket_id")
    test("两张工单的审计日志各自独立", s1, 200, different_tickets_in_audit)
    exc_id_2_for_close = exc_id_2  # save for later
    print()

    # =========================================================================
    print("=== 场景2: 关闭后再建单 - Liability + Proxy Report ===")
    print()

    BOX_L1 = uid("BXL1")
    BOX_P1 = uid("BXP1")
    BATCH_CL = uid("BATC")
    REASON_L = "包装破损"
    REASON_P = "数量差异"

    print("  2.1 创建测试批次")
    create_batch(BATCH_CL, [BOX_L1, BOX_P1])
    print("  批次创建完毕")
    print()

    print("  2.2 Liability: 建单 -> 结案 -> 再建单 验证放行")
    s, d = api("POST", "/api/liability/tickets", {
        "role": "库房签收员",
        "operator": "王五",
        "batch_no": BATCH_CL,
        "box_code": BOX_L1,
        "reason_category": REASON_L,
        "description": "场景2: 第一次责任单",
    })
    liab_id_1 = d["ticket_id"] if s == 200 else None
    test("第一次 Liability 创建成功", s, 200, liab_id_1 is not None)

    api("POST", f"/api/liability/tickets/{liab_id_1}/confirm", {
        "role": "仓库主管", "operator": "管理员A",
    })
    s, _ = api("POST", f"/api/liability/tickets/{liab_id_1}/close", {
        "role": "仓库主管", "operator": "管理员A",
        "conclusion": "责任人已认定",
    })
    test("第一次 Liability 结案成功", s, 200)

    s, d = api("POST", "/api/liability/tickets", {
        "role": "库房签收员",
        "operator": "王五",
        "batch_no": BATCH_CL,
        "box_code": BOX_L1,
        "reason_category": REASON_L,
        "description": "场景2: 结案后再次建单",
    })
    liab_id_2 = d["ticket_id"] if s == 200 else None
    test("结案后 Liability 再建单 成功", s, 200, liab_id_2 is not None)
    print()

    print("  2.3 Proxy Report: 建单 -> 指派 -> 结案 -> 再建单")
    s, d = api("POST", "/api/proxy_report/tickets", {
        "role": "质控",
        "operator": "质控员1",
        "batch_no": BATCH_CL,
        "box_code": BOX_P1,
        "reason_category": REASON_P,
        "description": "场景2: 第一次代报单",
    })
    pr_id_1 = d["ticket_id"] if s == 200 else None
    test("第一次 Proxy Report 创建成功", s, 200, pr_id_1 is not None)

    api("POST", f"/api/proxy_report/tickets/{pr_id_1}/assign", {
        "role": "仓库主管", "operator": "管理员A",
        "target_handler": "质控员2", "target_handler_role": "质控",
        "assign_reason": "指派处理",
    })
    s, _ = api("POST", f"/api/proxy_report/tickets/{pr_id_1}/close", {
        "role": "仓库主管", "operator": "管理员A",
        "conclusion": "差异已处理",
    })
    test("第一次 Proxy Report 结案成功", s, 200)

    s, d = api("POST", "/api/proxy_report/tickets", {
        "role": "质控",
        "operator": "质控员1",
        "batch_no": BATCH_CL,
        "box_code": BOX_P1,
        "reason_category": REASON_P,
        "description": "场景2: 结案后再次代报",
    })
    pr_id_2 = d["ticket_id"] if s == 200 else None
    test("结案后 Proxy Report 再建单 成功", s, 200, pr_id_2 is not None)
    print()

    # =========================================================================
    print("=== 场景3: 撤回重提责任重算 - 三类工单 ===")
    print()

    BOX_RSE = uid("BXRE")
    BOX_RSL = uid("BXRL")
    BOX_RSP = uid("BXRP")
    BATCH_RS = uid("BATR")

    print("  3.1 创建测试批次")
    create_batch(BATCH_RS, [BOX_RSE, BOX_RSL, BOX_RSP])
    print("  批次创建完毕")
    print()

    print("  3.2 Exception: 代录创建 -> 撤回 -> 重提 -> 处理人应重置为发起人")
    s, d = api("POST", "/api/exception/tickets", {
        "role": "仓库主管",
        "operator": "主管代录",
        "on_behalf_of": "发起人王五",
        "batch_no": BATCH_RS,
        "box_code": BOX_RSE,
        "reason_category": "标签错误",
        "description": "场景3: 代录创建",
    })
    exc_rs_id = d["ticket_id"] if s == 200 else None
    test("代录 Exception 创建成功", s, 200, exc_rs_id is not None)
    test("代录人字段正确 (主管代录)", 200, 200, d.get("proxy_recorder") == "主管代录")
    test("发起人字段正确 (发起人王五)", 200, 200, d.get("initiator") == "发起人王五")
    print()

    print("  3.3 撤回处置单 (发起人操作)")
    s, _ = api("POST", f"/api/exception/tickets/{exc_rs_id}/withdraw", {
        "role": "库房签收员",
        "operator": "发起人王五",
        "reason": "需要修正后重提",
    })
    test("撤回成功", s, 200)
    print()

    print("  3.4 重提后 处理人应重置为发起人 (而非代录人)")
    s, d = api("POST", f"/api/exception/tickets/{exc_rs_id}/resubmit", {
        "role": "库房签收员",
        "operator": "发起人王五",
        "description": "场景3: 修正后重提",
    })
    test("重提成功", s, 200)

    s, detail = api("GET", f"/api/exception/tickets/{exc_rs_id}")
    test("重提后 处理人为发起人 (发起人王五)", s, 200,
         isinstance(detail, dict) and detail.get("current_handler") == "发起人王五")
    test("重提后 代录人字段保留 (主管代录)", s, 200,
         isinstance(detail, dict) and detail.get("proxy_recorder") == "主管代录")
    print()

    print("  3.5 Liability: 代录创建 -> 撤回 -> 重提 -> 责任重算")
    s, d = api("POST", "/api/liability/tickets", {
        "role": "班组长",
        "operator": "组长代录",
        "on_behalf_of": "责任人工人甲",
        "batch_no": BATCH_RS,
        "box_code": BOX_RSL,
        "reason_category": "设备故障",
        "description": "场景3: 代录责任",
    })
    liab_rs_id = d["ticket_id"] if s == 200 else None
    test("代录 Liability 创建成功", s, 200, liab_rs_id is not None)

    api("POST", f"/api/liability/tickets/{liab_rs_id}/withdraw", {
        "role": "库房签收员",
        "operator": "责任人工人甲",
        "reason": "修正",
    })
    s, d = api("POST", f"/api/liability/tickets/{liab_rs_id}/resubmit", {
        "role": "库房签收员",
        "operator": "责任人工人甲",
        "description": "场景3: 修正重提",
    })
    test("Liability 重提成功", s, 200)

    s, detail = api("GET", f"/api/liability/tickets/{liab_rs_id}")
    test("Liability 重提后 处理人为责任人工人甲", s, 200,
         isinstance(detail, dict) and detail.get("current_handler") == "责任人工人甲")
    print()

    print("  3.6 Proxy Report: 代录创建 -> 撤回 -> 重提 -> 责任重算")
    s, d = api("POST", "/api/proxy_report/tickets", {
        "role": "质控",
        "operator": "质控代录",
        "on_behalf_of": "真实报单人李工",
        "on_behalf_of_role": "库房签收员",
        "batch_no": BATCH_RS,
        "box_code": BOX_RSP,
        "reason_category": "污染风险",
        "description": "场景3: 代录代报",
    })
    pr_rs_id = d["ticket_id"] if s == 200 else None
    test("代录 Proxy Report 创建成功", s, 200, pr_rs_id is not None)

    api("POST", f"/api/proxy_report/tickets/{pr_rs_id}/withdraw", {
        "role": "库房签收员",
        "operator": "真实报单人李工",
        "reason": "修正",
    })
    s, d = api("POST", f"/api/proxy_report/tickets/{pr_rs_id}/resubmit", {
        "role": "库房签收员",
        "operator": "真实报单人李工",
        "description": "场景3: 代报修正重提",
    })
    test("Proxy Report 重提成功", s, 200)

    s, detail = api("GET", f"/api/proxy_report/tickets/{pr_rs_id}")
    test("Proxy Report 重提后 处理人为真实报单人李工", s, 200,
         isinstance(detail, dict) and detail.get("current_handler") == "真实报单人李工")
    test("Proxy Report 重提后 responsibility_role 正确 (库房签收员)", s, 200,
         isinstance(detail, dict) and detail.get("responsibility_role") == "库房签收员")
    print()

    # =========================================================================
    print("=== 场景4: 导出字段和审计对账 ===")
    print()

    BOX_EXP = uid("BXEX")
    BATCH_EXP = uid("BATE")

    print("  4.1 Exception: 创建 -> 确认 -> 转交 -> 结案 (生成完整闭环数据)")
    create_batch(BATCH_EXP, [BOX_EXP])
    s, d = api("POST", "/api/exception/tickets", {
        "role": "库房签收员",
        "operator": "王五",
        "batch_no": BATCH_EXP,
        "box_code": BOX_EXP,
        "reason_category": "设备故障",
        "description": "场景4: 导出对账用处置单",
    })
    exc_exp_id = d["ticket_id"] if s == 200 else None
    test("导出用 Exception 创建成功", s, 200, exc_exp_id is not None)

    api("POST", f"/api/exception/tickets/{exc_exp_id}/confirm", {
        "role": "仓库主管", "operator": "管理员A",
    })
    api("POST", f"/api/exception/tickets/{exc_exp_id}/transfer", {
        "role": "仓库主管", "operator": "管理员A",
        "target_handler": "质控员1", "target_handler_role": "质控",
        "transfer_reason": "质控介入调查",
    })
    api("POST", f"/api/exception/tickets/{exc_exp_id}/close", {
        "role": "仓库主管", "operator": "管理员A",
        "conclusion": "导出对账测试完成",
    })
    print("  完成处置单闭环: 创建→确认→转交→结案")
    print()

    print("  4.2 JSON 导出字段完整性检查")
    s, json_resp = api("GET", f"/api/exception/export/json")
    tickets_json = json_resp.get("tickets", []) if isinstance(json_resp, dict) else []
    test("JSON 导出成功 且返回工单列表", s, 200, len(tickets_json) > 0)

    target_json = None
    for t in tickets_json:
        if isinstance(t, dict) and t.get("ticket_id") == exc_exp_id:
            target_json = t
            break
    test("JSON 导出中找到目标处置单", s, 200, target_json is not None)

    if target_json:
        required_json_fields = [
            "ticket_id", "ticket_no", "batch_no", "box_code",
            "reason_category", "status", "initiator", "initiator_role",
            "proxy_recorder", "current_handler", "current_handler_role",
            "evidence_list", "transfer_history", "responsibility_chain", "audit_log"
        ]
        all_present = all(f in target_json for f in required_json_fields)
        test(f"JSON 导出必需 {len(required_json_fields)} 个字段齐全", 200, 200, all_present)

        chain_len = len(target_json["responsibility_chain"])
        audit_len = len(target_json["audit_log"])
        transfers = len(target_json["transfer_history"])
        test("责任链至少 3 节点 (初始→转交→当前)", 200, 200, chain_len >= 3)
        test("审计日志至少 4 条 (创建/确认/转交/结案)", 200, 200, audit_len >= 4)
        test("转交历史至少 1 条记录", 200, 200, transfers >= 1)
    print()

    print("  4.3 CSV 导出字段完整性检查")
    s, csv_text = api("GET", f"/api/exception/export/csv", raw=True)
    test("CSV 导出 HTTP 成功", s, 200)

    if s == 200 and isinstance(csv_text, str):
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        csv_fields = reader.fieldnames
        test(f"CSV 至少 1 行数据 (共 {len(rows)} 行)", 200, 200, len(rows) > 0)

        required_csv_fields = [
            "ticket_id", "ticket_no", "box_code", "reason_category",
            "initiator", "current_handler", "responsibility_transfers", "audit_log"
        ]
        missing_csv = [f for f in required_csv_fields if f not in (csv_fields or [])]
        test(f"CSV 必需字段齐全 (缺失: {missing_csv})", 200, 200, len(missing_csv) == 0)
    print()

    print("  4.4 审计日志 vs 详情页审计对账")
    s, detail_data = api("GET", f"/api/exception/tickets/{exc_exp_id}")
    detail_audit = detail_data.get("audit_log", []) if isinstance(detail_data, dict) else []

    s, audit_data = api("GET", f"/api/exception/tickets/{exc_exp_id}/audit")
    endpoint_audit = audit_data if isinstance(audit_data, list) else []

    same_count = len(detail_audit) == len(endpoint_audit)
    min_len = min(len(detail_audit), len(endpoint_audit))
    same_actions = all(
        detail_audit[i].get("action") == endpoint_audit[i].get("action")
        for i in range(min_len)
    )
    test(f"详情页审计和audit端点对账一致 (条数={len(detail_audit)} vs {len(endpoint_audit)})",
         s, 200, same_count and same_actions)
    print()

    print("  4.5 三类工单 导出端点基本可用")
    for path, name in [
        ("/api/liability/export/json", "Liability JSON"),
        ("/api/liability/export/csv", "Liability CSV"),
        ("/api/proxy_report/export/json", "Proxy JSON"),
        ("/api/proxy_report/export/csv", "Proxy CSV"),
    ]:
        s, _ = api("GET", path, raw=True if "csv" in path else False)
        test(f"{name} 导出端点 HTTP 正常", s, 200)
    print()

    # =========================================================================
    print("=" * 70)
    print(f"  测试总结: 通过 {passed}, 失败 {failed}, 警告 {len(warnings)}")
    print("=" * 70)
    if warnings:
        print("\n警告列表:")
        for w in warnings:
            print(f"  - {w}")
    if failed > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
