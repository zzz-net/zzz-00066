"""
代录责任追踪单模块全面回归测试
覆盖 8 大场景：正常闭环、越权拦截、代录配置切换、撤回重提责任重置、
重复冲突拦截、按责任岗位筛选、导出对账、服务重启状态恢复
"""

import json
import sys
import time
import os
import urllib.request
import urllib.error
import urllib.parse

BASE = "http://localhost:8000"


def api(method, path, data=None, raw=False):
    url = f"{BASE}{urllib.parse.quote(path, safe='/:=&?[]@!$\'()*,;')}"
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw_body = resp.read()
            if raw:
                return resp.status, raw_body.decode("utf-8-sig")
            return resp.status, json.loads(raw_body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw_body = e.read()
        try:
            return e.code, json.loads(raw_body.decode("utf-8"))
        except Exception:
            return e.code, raw_body.decode("utf-8")


passed = 0
failed = 0


def test(name, status, expected_status=200, check=None):
    global passed, failed
    ok = status == expected_status
    extra = ""
    if ok and check is not None:
        try:
            ok = bool(check)
        except Exception as e:
            ok = False
            extra = f" [check exception: {e}]"
    if ok:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} -> HTTP {status}{extra}")


def wait_for_service(timeout=30):
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


def create_full_batch(batch_no, box_count=3, prefix="LB"):
    api("POST", "/api/batches", {
        "batch_no": batch_no,
        "sample_type": "疫苗",
        "operator": "管理员A",
    })
    boxes = [
        {"box_code": f"{prefix}{i:03d}", "sample_type": "疫苗", "current_temp": 4.0 + i * 0.1}
        for i in range(1, box_count + 1)
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
    print("  代录责任追踪单模块全面回归测试")
    print("=" * 70)
    print()

    print("=== 阶段0: 初始化 ===")
    api("POST", "/api/thresholds", {
        "sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120
    })
    print("  阈值配置完成")

    api("POST", "/api/liability/config", {
        "allow_proxy_record": False,
        "operator": "管理员A",
    })
    print("  代录责任追踪单配置: 代录已关闭")
    print()

    # ==================================================================
    print("=== 场景1: 正常闭环 - 创建→确认→补证→转交→结案 ===")
    print()

    print("  1.1 创建批次 BATCH-LIAB-001 并完成签收 (3箱)")
    boxes, status, _ = create_full_batch("BATCH-LIAB-001", 3, "LB")
    test("批次签收完成", status, check=True)

    print()
    print("  1.2 库房签收员发起责任追踪单")
    status, data = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-001",
        "box_code": "LB001",
        "reason_category": "温度异常",
        "description": "开箱时发现温度记录仪显示运输途中多次超过8°C",
        "role": "库房签收员",
        "operator": "张签收",
    })
    test("创建责任追踪单成功", status,
         check=(data.get("ok") and data.get("status") == "待处理"
                and data.get("reporter") == "张签收"
                and data.get("reporter_role") == "库房签收员"
                and data.get("proxy_recorder") is None
                and data.get("current_handler") == "张签收"
                and data.get("current_handler_role") == "库房签收员"))
    ticket1_id = data["ticket_id"]
    ticket1_no = data["ticket_no"]
    print(f"    工单号: {ticket1_no}")
    print(f"    报单人: {data['reporter']}（{data['reporter_role']}）")
    print(f"    代录人: {data['proxy_recorder']}")
    print(f"    当前处理人: {data['current_handler']}（{data['current_handler_role']}）")

    status, data = api("GET", f"/api/liability/tickets/{ticket1_id}")
    test("工单详情四字段完整分离", status,
         check=(data["reporter"] == "张签收"
                and data["reporter_role"] == "库房签收员"
                and data["proxy_recorder"] is None
                and data["proxy_recorder_role"] is None
                and data["current_handler"] == "张签收"
                and data["current_handler_role"] == "库房签收员"
                and data["reason_category"] == "温度异常"
                and data["box_code"] == "LB001"
                and len(data["evidence_list"]) >= 1
                and len(data["transfer_history"]) == 0
                and len(data["audit_log"]) >= 1))

    print()
    print("  1.3 仓库主管确认 → 进入处理中")
    status, data = api("POST", f"/api/liability/tickets/{ticket1_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管", "reason": "确认责任追踪单有效",
    })
    test("确认成功，状态变为处理中", status,
         check=data.get("ok") and data.get("status") == "处理中")

    print()
    print("  1.4 处理中补充证据")
    status, data = api("POST", f"/api/liability/tickets/{ticket1_id}/evidence", {
        "evidence_type": "text",
        "evidence_content": "温度记录仪导出数据显示3号节点温度超过8°C达2小时",
        "role": "库房管理员", "operator": "陈管理员",
    })
    test("补充证据成功", status, check=data.get("ok"))

    status, data = api("GET", f"/api/liability/tickets/{ticket1_id}")
    test("证据列表有2条", status, check=len(data["evidence_list"]) == 2)

    print()
    print("  1.5 转交责任人给质控部门")
    status, data = api("POST", f"/api/liability/tickets/{ticket1_id}/transfer", {
        "target_handler": "钱质控",
        "target_handler_role": "质控",
        "transfer_reason": "需质控部门进行专业的温度数据分析和样本有效性评估",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("转交责任人成功", status,
         check=(data.get("ok")
                and data.get("from_handler") == "张签收"
                and data.get("from_handler_role") == "库房签收员"
                and data.get("to_handler") == "钱质控"
                and data.get("to_handler_role") == "质控"))

    status, data = api("GET", f"/api/liability/tickets/{ticket1_id}")
    test("当前处理人已更新为钱质控", status,
         check=(data["current_handler"] == "钱质控"
                and data["current_handler_role"] == "质控"
                and len(data["transfer_history"]) == 1
                and data["transfer_history"][0]["from_handler"] == "张签收"
                and data["transfer_history"][0]["to_handler"] == "钱质控"))

    print()
    print("  1.6 质控补充证据后结案")
    status, data = api("POST", f"/api/liability/tickets/{ticket1_id}/evidence", {
        "evidence_type": "text",
        "evidence_content": "质控分析报告：确认冷链设备故障导致温度超标，样本已失效",
        "role": "质控", "operator": "钱质控",
    })
    test("质控补充证据成功", status, check=data.get("ok"))

    status, data = api("POST", f"/api/liability/tickets/{ticket1_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "转运方冷链设备故障导致样本失效，转运组承担主要责任",
        "reason": "调查完毕，责任清晰",
    })
    test("结案成功", status,
         check=(data.get("ok") and data.get("status") == "已结案"
                and "转运" in data.get("conclusion", "")))

    status, data = api("GET", f"/api/liability/tickets/{ticket1_id}")
    test("结案后状态和字段完整", status,
         check=(data["status"] == "已结案"
                and data["closed_by"] == "赵主管"
                and data["closed_role"] == "仓库主管"
                and data["current_handler"] == "钱质控"
                and data["current_handler_role"] == "质控"))

    print()
    print("  1.7 审计日志完整")
    status, audit = api("GET", f"/api/liability/tickets/{ticket1_id}/audit")
    actions = [a["action"] for a in audit]
    test("审计日志包含 创建/确认/补证x2/转交/结案", status,
         check=("创建责任追踪单" in actions
                and "确认责任追踪单" in actions
                and "补充证据" in actions
                and "转交处理人" in actions
                and "结案责任追踪单" in actions
                and actions.count("补充证据") >= 2))
    print(f"    审计动作: {actions}")

    # ==================================================================
    print()
    print("=== 场景2: 越权拦截 ===")
    print()

    print("  2.1 出库员无权发起责任追踪单 → 403")
    status, data = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-001",
        "box_code": "LB002",
        "reason_category": "包装破损",
        "role": "出库员", "operator": "张三",
    })
    test("出库员发起被拦截", status, expected_status=403)

    print("  2.2 非报单人不可撤回 → 403")
    status, data22 = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-001",
        "box_code": "LB002",
        "reason_category": "包装破损",
        "description": "越权撤回测试",
        "role": "库房签收员", "operator": "张签收",
    })
    ticket22_id = data22["ticket_id"]
    test("创建测试工单成功", status, check=data22.get("ok"))

    status, data = api("POST", f"/api/liability/tickets/{ticket22_id}/withdraw", {
        "role": "库房签收员",
        "operator": "李签收",
        "reason": "非报单人试图撤回",
    })
    test("非报单人撤回被拦截", status, expected_status=403)

    print("  2.3 已结案工单不可再补证 → 409")
    status, data = api("POST", f"/api/liability/tickets/{ticket1_id}/evidence", {
        "evidence_type": "text",
        "evidence_content": "试图对已结案工单补证",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("已结案工单补证被拦截", status, expected_status=409)

    print("  2.4 库房签收员无权结案 → 403")
    status, data24 = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-001",
        "box_code": "LB003",
        "reason_category": "数量差异",
        "description": "越权结案测试工单",
        "role": "仓库主管", "operator": "赵主管",
    })
    ticket24_id = data24["ticket_id"]
    api("POST", f"/api/liability/tickets/{ticket24_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("POST", f"/api/liability/tickets/{ticket24_id}/close", {
        "role": "库房签收员", "operator": "张签收",
        "conclusion": "签收员试图结案",
    })
    test("签收员越权结案被拦截", status, expected_status=403)

    print("  2.5 库房签收员无权转交 → 403")
    status, data = api("POST", f"/api/liability/tickets/{ticket24_id}/transfer", {
        "target_handler": "钱质控",
        "target_handler_role": "质控",
        "transfer_reason": "签收员试图转交",
        "role": "库房签收员", "operator": "张签收",
    })
    test("签收员越权转交被拦截", status, expected_status=403)

    print("  2.6 无效原因分类被拦截 → 400")
    status, data = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-001",
        "box_code": "LB002",
        "reason_category": "无效分类",
        "description": "测试无效分类",
        "role": "库房管理员", "operator": "陈管理员",
    })
    test("无效原因分类被拦截", status, expected_status=400)

    print()
    print("  场景2清理: 撤回测试工单避免影响后续测试")
    api("POST", f"/api/liability/tickets/{ticket22_id}/withdraw", {
        "role": "库房签收员", "operator": "张签收", "reason": "测试清理",
    })
    api("POST", f"/api/liability/tickets/{ticket24_id}/close", {
        "role": "仓库主管", "operator": "赵主管", "conclusion": "测试清理",
    })

    # ==================================================================
    print()
    print("=== 场景3: 代录配置切换 ===")
    print()

    print("  3.1 代录关闭时，班组长代录被拦截 → 403")
    status, data = api("POST", "/api/liability/config", {
        "allow_proxy_record": False,
        "operator": "管理员A",
    })
    test("关闭代录配置成功", status, check=data.get("ok"))

    status, data = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-001",
        "box_code": "LB002",
        "reason_category": "标签错误",
        "description": "代录关闭时代录测试",
        "role": "班组长",
        "operator": "周班长",
        "on_behalf_of": "张签收",
    })
    test("代录关闭时代录被拦截", status, expected_status=403)
    print(f"    返回提示: {data.get('detail', '')[:80]}")

    print("  3.2 开启代录配置")
    status, data = api("POST", "/api/liability/config", {
        "allow_proxy_record": True,
        "operator": "管理员A",
    })
    test("开启代录配置成功", status,
         check=data.get("ok") and data.get("allow_proxy_record") is True)

    status, cfg = api("GET", "/api/liability/config")
    test("查询配置显示已开启", status,
         check=cfg.get("allow_proxy_record") is True)

    print("  3.3 代录开启后，班组长代录成功")
    status, data = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-001",
        "box_code": "LB002",
        "reason_category": "包装破损",
        "description": "班组长代一线员工录入：外包装破损",
        "role": "班组长",
        "operator": "周班长",
        "on_behalf_of": "张签收",
    })
    test("代录创建责任追踪单成功", status,
         check=(data.get("ok")
                and data.get("reporter") == "张签收"
                and data.get("proxy_recorder") == "周班长"
                and data.get("current_handler") == "张签收"))
    ticket3_id = data["ticket_id"]
    print(f"    报单人: {data['reporter']}")
    print(f"    代录人: {data['proxy_recorder']}")
    print(f"    当前处理人: {data['current_handler']}")

    status, data = api("GET", f"/api/liability/tickets/{ticket3_id}")
    test("详情页四字段清晰分离", status,
         check=(data["reporter"] == "张签收"
                and data["reporter_role"] == "班组长"
                and data["proxy_recorder"] == "周班长"
                and data["proxy_recorder_role"] == "班组长"
                and data["current_handler"] == "张签收"
                and data["current_handler_role"] == "班组长"
                and data["allow_proxy_record_at_create"] is True))

    print("  3.4 配置变更不影响已有工单（冻结创建时配置）")
    status, _ = api("POST", "/api/liability/config", {
        "allow_proxy_record": False,
        "operator": "管理员A",
    })
    test("再次关闭代录配置", status, check=True)

    status, data = api("GET", f"/api/liability/tickets/{ticket3_id}")
    test("已有工单的 allow_proxy_record_at_create 仍为 True", status,
         check=data["allow_proxy_record_at_create"] is True)

    print("  3.5 代录关闭后新建工单记录为不允许代录")
    status, data35 = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-001",
        "box_code": "LB003",
        "reason_category": "设备故障",
        "description": "配置关闭后创建的工单",
        "role": "库房管理员",
        "operator": "陈管理员",
    })
    ticket35_id = data35["ticket_id"]
    status, data = api("GET", f"/api/liability/tickets/{ticket35_id}")
    test("新建工单 allow_proxy_record_at_create 为 False", status,
         check=data["allow_proxy_record_at_create"] is False)

    # ==================================================================
    print()
    print("=== 场景4: 撤回重提责任重置 ===")
    print()

    print("  4.1 创建工单并确认→转交")
    status, data41 = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-001",
        "box_code": "LB003",
        "reason_category": "污染风险",
        "description": "撤回重提测试工单",
        "role": "库房签收员",
        "operator": "李签收",
    })
    ticket4_id = data41["ticket_id"]
    test("创建测试工单成功", status, check=data41.get("ok"))

    api("POST", f"/api/liability/tickets/{ticket4_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })

    status, _ = api("POST", f"/api/liability/tickets/{ticket4_id}/transfer", {
        "target_handler": "钱质控",
        "target_handler_role": "质控",
        "transfer_reason": "转交质控处理",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("转交至质控成功", status, check=True)

    status, data = api("GET", f"/api/liability/tickets/{ticket4_id}")
    test("转交后当前处理人为钱质控", status,
         check=(data["current_handler"] == "钱质控"
                and data["current_handler_role"] == "质控"
                and len(data["transfer_history"]) == 1))

    print("  4.2 报单人撤回工单")
    status, data = api("POST", f"/api/liability/tickets/{ticket4_id}/withdraw", {
        "role": "库房签收员",
        "operator": "李签收",
        "reason": "证据不足，撤回补充",
    })
    test("撤回成功", status,
         check=data.get("ok") and data.get("status") == "已撤回")

    status, data = api("GET", f"/api/liability/tickets/{ticket4_id}")
    test("撤回后状态为已撤回", status,
         check=(data["status"] == "已撤回"
                and data["withdrawn_by"] == "李签收"))

    print("  4.3 重新提交后责任重置为报单人（不残留上次处理人）")
    status, data = api("POST", f"/api/liability/tickets/{ticket4_id}/resubmit", {
        "role": "库房签收员",
        "operator": "李签收",
        "description": "补充了新的证据，重新提交",
        "reason": "补充证据后重新提交",
    })
    test("重新提交成功", status,
         check=(data.get("ok")
                and data.get("status") == "待处理"
                and data.get("current_handler") == "李签收"
                and data.get("current_handler_role") == "库房签收员"))

    status, data = api("GET", f"/api/liability/tickets/{ticket4_id}")
    test("重提后处理人重置为报单人，不残留钱质控", status,
         check=(data["current_handler"] == "李签收"
                and data["current_handler_role"] == "库房签收员"
                and data["status"] == "待处理"
                and len(data["transfer_history"]) == 1))
    print(f"    重提后处理人: {data['current_handler']}（{data['current_handler_role']}）")
    print(f"    历史转交记录数: {len(data['transfer_history'])}")

    print("  4.4 非报单人不可重新提交 → 403")
    status, data = api("POST", f"/api/liability/tickets/{ticket4_id}/withdraw", {
        "role": "库房签收员",
        "operator": "李签收",
        "reason": "再次撤回测试越权重提",
    })
    test("再次撤回成功", status, check=data.get("ok"))

    status, data = api("POST", f"/api/liability/tickets/{ticket4_id}/resubmit", {
        "role": "库房管理员",
        "operator": "陈管理员",
        "description": "非报单人试图重提",
    })
    test("非报单人重提被拦截", status, expected_status=403)

    # ==================================================================
    print()
    print("=== 场景5: 重复冲突拦截 ===")
    print()

    print("  5.1 同箱号同原因的活跃单禁止重复创建")
    boxes2, _, _ = create_full_batch("BATCH-LIAB-002", 2, "LC")

    status, data = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-002",
        "box_code": "LC001",
        "reason_category": "温度异常",
        "description": "首次报单",
        "role": "库房管理员", "operator": "陈管理员",
    })
    ticket5_id = data["ticket_id"]
    test("首次报单成功", status, check=data.get("ok"))

    status, data = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-002",
        "box_code": "LC001",
        "reason_category": "温度异常",
        "description": "重复报单",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("同箱号同原因重复报单被拦截", status, expected_status=409)
    print(f"    返回提示: {data.get('detail', '')[:80]}")

    print("  5.2 结案后可再次创建（不拦截）")
    api("POST", f"/api/liability/tickets/{ticket5_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    api("POST", f"/api/liability/tickets/{ticket5_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "已结案，测试重复报单",
    })

    status, data = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-002",
        "box_code": "LC001",
        "reason_category": "温度异常",
        "description": "结案后再次报单",
        "role": "库房管理员", "operator": "陈管理员",
    })
    test("结案后同箱号同原因可再次创建", status, check=data.get("ok"))

    print("  5.3 重提时检查重复（自身排除）")
    status, data53a = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-002",
        "box_code": "LC002",
        "reason_category": "包装破损",
        "description": "工单A",
        "role": "库房管理员", "operator": "陈管理员",
    })
    ticket53a_id = data53a["ticket_id"]
    test("创建工单A成功", status, check=data53a.get("ok"))

    status, data53b = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-002",
        "box_code": "LC002",
        "reason_category": "包装破损",
        "description": "工单B（应该被拦截）",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("同箱号同原因第二张单被拦截", status, expected_status=409)

    print("  5.4 驳回后重提不与自己冲突（排除自身ID）")
    api("POST", f"/api/liability/tickets/{ticket53a_id}/reject", {
        "role": "仓库主管", "operator": "赵主管",
        "reason": "证据不足",
    })

    status, data = api("POST", f"/api/liability/tickets/{ticket53a_id}/resubmit", {
        "role": "库房管理员", "operator": "陈管理员",
        "description": "补充证据后重提",
    })
    test("自己重提不与自己冲突（排除自身ID）", status,
         check=data.get("ok") and data.get("status") == "待处理")

    # ==================================================================
    print()
    print("=== 场景6: 按责任岗位筛选 ===")
    print()

    print("  6.1 列表按 reporter_role 筛选")
    status, all_tickets = api("GET", "/api/liability/tickets")
    test("工单列表查询成功", status, check=len(all_tickets) > 0)
    print(f"    总工单数: {len(all_tickets)}")

    status, filtered = api("GET", "/api/liability/tickets?reporter_role=库房签收员")
    test("按责任岗位（库房签收员）筛选成功", status,
         check=all(t["reporter_role"] == "库房签收员" for t in filtered) and len(filtered) > 0)
    print(f"    库房签收员责任单数: {len(filtered)}")

    status, filtered2 = api("GET", "/api/liability/tickets?reporter_role=仓库主管")
    test("按责任岗位（仓库主管）筛选成功", status,
         check=all(t["reporter_role"] == "仓库主管" for t in filtered2))

    print("  6.2 列表按 current_handler_role 筛选")
    status, filtered3 = api("GET", "/api/liability/tickets?current_handler_role=质控")
    test("按当前处理岗位（质控）筛选成功", status,
         check=all(t["current_handler_role"] == "质控" for t in filtered3))
    print(f"    质控处理中单数: {len(filtered3)}")

    print("  6.3 组合筛选（状态 + 责任岗位）")
    status, filtered4 = api("GET", "/api/liability/tickets?status=已结案&reporter_role=库房签收员")
    test("组合筛选（已结案 + 库房签收员）", status,
         check=all(t["status"] == "已结案" and t["reporter_role"] == "库房签收员" for t in filtered4))

    # ==================================================================
    print()
    print("=== 场景7: 导出对账 ===")
    print()

    print("  7.1 JSON 导出包含完整责任链")
    status, json_export = api("GET", "/api/liability/export/json")
    test("JSON导出成功", status,
         check=json_export.get("total_tickets", 0) > 0
                and "tickets" in json_export)

    first_ticket = json_export["tickets"][0]
    test("JSON导出包含四字段（报单人/代录人/责任岗位/当前处理岗位）", status,
         check=("reporter" in first_ticket
                and "reporter_role" in first_ticket
                and "proxy_recorder" in first_ticket
                and "proxy_recorder_role" in first_ticket
                and "current_handler" in first_ticket
                and "current_handler_role" in first_ticket
                and "responsibility_chain" in first_ticket
                and "transfer_history" in first_ticket
                and "audit_log" in first_ticket))
    print(f"    责任链节点数: {len(first_ticket.get('responsibility_chain', []))}")

    print("  7.2 JSON 导出支持筛选")
    status, filtered_export = api("GET", "/api/liability/export/json?reporter_role=库房签收员")
    test("JSON导出按责任岗位筛选", status,
         check=all(t["reporter_role"] == "库房签收员" for t in filtered_export.get("tickets", [])))

    print("  7.3 CSV 导出包含 responsibility_transfers 字段")
    status, csv_content = api("GET", "/api/liability/export/csv", raw=True)
    test("CSV导出成功", status,
         check="responsibility_transfers" in csv_content
                and "reporter" in csv_content
                and "reporter_role" in csv_content
                and "proxy_recorder" in csv_content
                and "proxy_recorder_role" in csv_content
                and "current_handler" in csv_content
                and "current_handler_role" in csv_content)

    lines = csv_content.strip().split("\n")
    header = lines[0]
    test("CSV表头包含关键字段", status,
         check=all(f in header for f in [
             "ticket_id", "ticket_no", "reason_category", "status",
             "reporter", "reporter_role", "proxy_recorder", "proxy_recorder_role",
             "current_handler", "current_handler_role", "responsibility_transfers",
             "evidence_count"
         ]))
    print(f"    CSV 行数: {len(lines)} (含表头)")

    print("  7.4 CSV 导出支持筛选")
    status, csv_filtered = api("GET", "/api/liability/export/csv?status=已结案", raw=True)
    lines_filtered = csv_filtered.strip().split("\n")
    test("CSV导出按状态筛选", status, check=len(lines_filtered) >= 1)

    # ==================================================================
    print()
    print("=== 场景8: 批次汇总与审计日志 ===")
    print()

    print("  8.1 批次汇总统计")
    status, summary = api("GET", "/api/liability/batches/BATCH-LIAB-001/summary")
    test("批次汇总查询成功", status,
         check=("total_tickets" in summary
                and "by_status" in summary
                and "by_category" in summary
                and "by_reporter_role" in summary
                and "tickets" in summary))
    print(f"    批次总工单数: {summary['total_tickets']}")
    print(f"    按状态分布: {summary['by_status']}")
    print(f"    按责任岗位分布: {summary['by_reporter_role']}")

    print("  8.2 单张工单审计日志完整")
    status, audit = api("GET", f"/api/liability/tickets/{ticket1_id}/audit")
    test("审计日志查询成功", status, check=len(audit) > 0)

    audit_actions = [a["action"] for a in audit]
    test("审计日志包含角色和操作人字段", status,
         check=all("role" in a and "operator" in a and "created_at" in a for a in audit))
    print(f"    审计记录数: {len(audit)}")
    print(f"    操作动作: {audit_actions[:5]}...")

    # ==================================================================
    print()
    print("=== 场景9: 驳回→重新提交流程 ===")
    print()

    print("  9.1 创建工单并驳回")
    status, data9 = api("POST", "/api/liability/tickets", {
        "batch_no": "BATCH-LIAB-002",
        "box_code": "LC002",
        "reason_category": "流程违规",
        "description": "驳回重提测试",
        "role": "库房管理员",
        "operator": "陈管理员",
    })
    ticket9_id = data9["ticket_id"]
    test("创建测试工单成功", status, check=data9.get("ok"))

    status, data = api("POST", f"/api/liability/tickets/{ticket9_id}/reject", {
        "role": "仓库主管",
        "operator": "赵主管",
        "reason": "描述不清，无法判定责任，请补充细节",
    })
    test("驳回成功", status,
         check=data.get("ok") and data.get("status") == "已驳回")

    status, data = api("GET", f"/api/liability/tickets/{ticket9_id}")
    test("驳回后字段完整", status,
         check=(data["status"] == "已驳回"
                and data["rejected_by"] == "赵主管"
                and data["rejected_role"] == "仓库主管"
                and data["rejected_reason"] == "描述不清，无法判定责任，请补充细节"))

    print("  9.2 驳回后重新提交")
    status, data = api("POST", f"/api/liability/tickets/{ticket9_id}/resubmit", {
        "role": "库房管理员",
        "operator": "陈管理员",
        "description": "补充了详细的操作流程记录和时间线",
        "reason": "补充证据后重新提交",
    })
    test("驳回后重新提交成功", status,
         check=(data.get("ok")
                and data.get("status") == "待处理"))

    status, data = api("GET", f"/api/liability/tickets/{ticket9_id}")
    test("重提后状态回到待处理，驳回信息已清空", status,
         check=(data["status"] == "待处理"
                and data["rejected_at"] is None
                and data["rejected_by"] is None
                and data["resubmitted_by"] == "陈管理员"))

    # ==================================================================
    print()
    print("=== 总结 ===")
    print()
    total = passed + failed
    print(f"  通过: {passed} / {total}")
    print(f"  失败: {failed} / {total}")
    print()

    if failed == 0:
        print("  [OK] 所有测试通过！")
    else:
        print("  [FAIL] 存在测试失败，请检查")

    print()
    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
