"""
异常代报受理单模块全面回归测试
覆盖 9 大场景：正常闭环、越权拦截、代录配置切换、撤回重提责任重置、
           重复冲突拦截、按责任岗位筛选、导出对账、服务重启状态恢复、驳回→重新提交
"""

import json
import sys
import time
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
        with urllib.request.urlopen(req, timeout=15) as resp:
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


def create_full_batch(batch_no, box_count=3, prefix="PRB"):
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
    print("  异常代报受理单模块全面回归测试")
    print("=" * 70)
    print()

    print("=== 阶段0: 初始化 ===")
    api("POST", "/api/thresholds", {
        "sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120
    })
    print("  阈值配置完成")

    api("POST", "/api/proxy_report/config", {
        "allow_proxy_record": False,
        "operator": "管理员A",
    })
    print("  异常代报受理单配置: 代报已关闭")
    print()

    # ==================================================================
    print("=== 场景1: 正常闭环 - 录入→指派→补证→结案 ===")
    print()

    print("  1.1 创建批次 BATCH-PR-001 并完成签收 (3箱)")
    boxes, status, _ = create_full_batch("BATCH-PR-001", 3, "PRB")
    test("批次签收完成", status, check=True)

    print()
    print("  1.2 库房签收员发起代报受理单（真实报单人）")
    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB001",
        "reason_category": "温度异常",
        "description": "开箱时发现温度记录仪显示运输途中多次超过8°C",
        "role": "库房签收员",
        "operator": "张签收",
    })
    test("创建代报受理单成功（真实报单人）", status,
         check=(data.get("ok") and data.get("status") == "待指派"
                and data.get("originator") == "张签收"
                and data.get("originator_role") == "库房签收员"
                and data.get("responsibility_role") == "库房签收员"
                and data.get("proxy_recorder") is None
                and data.get("proxy_recorder_role") is None
                and data.get("current_handler") == "张签收"
                and data.get("current_handler_role") == "库房签收员"))
    ticket1_id = data["ticket_id"]
    ticket1_no = data["ticket_no"]
    print(f"    工单号: {ticket1_no}")
    print(f"    真实报单人: {data['originator']}（{data['originator_role']}）")
    print(f"    责任岗位: {data['responsibility_role']}")
    print(f"    代填人: {data['proxy_recorder']}")
    print(f"    当前处理人: {data['current_handler']}（{data['current_handler_role']}）")

    status, data = api("GET", f"/api/proxy_report/tickets/{ticket1_id}")
    test("工单详情四字段完整分离", status,
         check=(data["originator"] == "张签收"
                and data["originator_role"] == "库房签收员"
                and data["responsibility_role"] == "库房签收员"
                and data["proxy_recorder"] is None
                and data["proxy_recorder_role"] is None
                and data["current_handler"] == "张签收"
                and data["current_handler_role"] == "库房签收员"
                and data["reason_category"] == "温度异常"
                and data["box_code"] == "PRB001"
                and len(data["evidence_list"]) >= 1
                and len(data["assignment_history"]) == 0
                and len(data["audit_log"]) >= 1
                and len(data["responsibility_chain"]) >= 2))

    print()
    print("  1.3 仓库主管指派 → 进入处理中")
    status, data = api("POST", f"/api/proxy_report/tickets/{ticket1_id}/assign", {
        "target_handler": "赵主管",
        "target_handler_role": "仓库主管",
        "assign_reason": "需要仓库主管协调调查",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("指派成功，状态变为处理中", status,
         check=(data.get("ok") and data.get("status") == "处理中"
                and data.get("from_handler") == "张签收"
                and data.get("to_handler") == "赵主管"))

    status, data = api("GET", f"/api/proxy_report/tickets/{ticket1_id}")
    test("指派后当前处理人更新为赵主管", status,
         check=(data["status"] == "处理中"
                and data["current_handler"] == "赵主管"
                and data["current_handler_role"] == "仓库主管"
                and data["responsibility_role"] == "库房签收员"
                and len(data["assignment_history"]) == 1
                and data["assignment_history"][0]["to_handler"] == "赵主管"))
    print(f"    指派后当前处理人: {data['current_handler']}（{data['current_handler_role']}）")
    print(f"    责任岗位（不变）: {data['responsibility_role']}")

    print()
    print("  1.4 处理中补充证据")
    status, data = api("POST", f"/api/proxy_report/tickets/{ticket1_id}/evidence", {
        "evidence_type": "text",
        "evidence_content": "温度记录仪导出数据显示3号节点温度超过8°C达2小时",
        "role": "库房管理员", "operator": "陈管理员",
    })
    test("补充证据成功", status, check=data.get("ok"))

    status, data = api("GET", f"/api/proxy_report/tickets/{ticket1_id}")
    test("证据列表有2条", status, check=len(data["evidence_list"]) == 2)

    print()
    print("  1.5 再次指派给质控部门")
    status, data = api("POST", f"/api/proxy_report/tickets/{ticket1_id}/assign", {
        "target_handler": "钱质控",
        "target_handler_role": "质控",
        "assign_reason": "需质控部门进行专业的温度数据分析和样本有效性评估",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("指派给质控成功", status,
         check=(data.get("ok")
                and data.get("from_handler") == "赵主管"
                and data.get("to_handler") == "钱质控"))

    status, data = api("GET", f"/api/proxy_report/tickets/{ticket1_id}")
    test("当前处理人已更新为钱质控，责任岗位仍是库房签收员", status,
         check=(data["current_handler"] == "钱质控"
                and data["current_handler_role"] == "质控"
                and data["responsibility_role"] == "库房签收员"
                and len(data["assignment_history"]) == 2))
    print(f"    当前处理人: {data['current_handler']}（{data['current_handler_role']}）")
    print(f"    责任岗位: {data['responsibility_role']}")
    print(f"    责任链节点数: {len(data['responsibility_chain'])}")

    print()
    print("  1.6 质控补充证据后结案")
    status, data = api("POST", f"/api/proxy_report/tickets/{ticket1_id}/evidence", {
        "evidence_type": "text",
        "evidence_content": "质控分析报告：确认冷链设备故障导致温度超标，样本已失效",
        "role": "质控", "operator": "钱质控",
    })
    test("质控补充证据成功", status, check=data.get("ok"))

    status, data = api("POST", f"/api/proxy_report/tickets/{ticket1_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "转运方冷链设备故障导致样本失效，转运组承担主要责任",
        "reason": "调查完毕，责任清晰",
    })
    test("结案成功", status,
         check=(data.get("ok") and data.get("status") == "已结案"
                and "转运" in data.get("conclusion", "")))

    status, data = api("GET", f"/api/proxy_report/tickets/{ticket1_id}")
    test("结案后状态和字段完整", status,
         check=(data["status"] == "已结案"
                and data["closed_by"] == "赵主管"
                and data["closed_role"] == "仓库主管"
                and data["current_handler"] == "钱质控"
                and data["current_handler_role"] == "质控"
                and data["responsibility_role"] == "库房签收员"))

    print()
    print("  1.7 审计日志完整")
    status, audit = api("GET", f"/api/proxy_report/tickets/{ticket1_id}/audit")
    actions = [a["action"] for a in audit]
    test("审计日志包含 创建/指派x2/补证x2/结案", status,
         check=("创建异常代报受理单" in actions
                and "指派责任人" in actions
                and "补充证据" in actions
                and "办结受理单" in actions
                and actions.count("指派责任人") >= 2
                and actions.count("补充证据") >= 2))
    print(f"    审计动作: {actions}")

    # ==================================================================
    print()
    print("=== 场景2: 越权拦截 ===")
    print()

    print("  2.1 无权限角色（如系统管理员）发起被拦截 → 403")
    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB002",
        "reason_category": "包装破损",
        "role": "系统管理员", "operator": "管理员A",
    })
    test("系统管理员发起被拦截", status, expected_status=403)

    print("  2.2 代报关闭时，班组长代报被拦截 → 403")
    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB002",
        "reason_category": "包装破损",
        "role": "班组长",
        "operator": "周班长",
        "on_behalf_of": "张签收",
        "on_behalf_of_role": "库房签收员",
    })
    test("代报关闭时代报被拦截", status, expected_status=403)
    print(f"    返回提示: {str(data.get('detail', ''))[:80]}")

    print("  2.3 代报时未指定 on_behalf_of_role 被拦截 → 400")
    api("POST", "/api/proxy_report/config", {
        "allow_proxy_record": True, "operator": "管理员A",
    })
    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB002",
        "reason_category": "流程违规",
        "role": "班组长",
        "operator": "周班长",
        "on_behalf_of": "张签收",
    })
    test("代报未指定真实岗位被拦截", status, expected_status=400)
    api("POST", "/api/proxy_report/config", {
        "allow_proxy_record": False, "operator": "管理员A",
    })

    print("  2.4 非真实报单人不可撤回 → 403")
    status, data24 = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB002",
        "reason_category": "数量差异",
        "description": "越权撤回测试",
        "role": "库房签收员", "operator": "张签收",
    })
    ticket24_id = data24["ticket_id"]
    test("创建测试工单成功", status, check=data24.get("ok"))

    status, data = api("POST", f"/api/proxy_report/tickets/{ticket24_id}/withdraw", {
        "role": "库房签收员",
        "operator": "李签收",
        "reason": "非报单人试图撤回",
    })
    test("非报单人撤回被拦截", status, expected_status=403)

    print("  2.5 已结案工单不可再补证 → 409")
    status, data = api("POST", f"/api/proxy_report/tickets/{ticket1_id}/evidence", {
        "evidence_type": "text",
        "evidence_content": "试图对已结案工单补证",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("已结案工单补证被拦截", status, expected_status=409)

    print("  2.6 库房签收员无权指派 → 403")
    status, data26 = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB003",
        "reason_category": "流程违规",
        "description": "越权指派测试工单",
        "role": "库房管理员", "operator": "陈管理员",
    })
    ticket26_id = data26["ticket_id"]
    test("创建测试工单成功", status, check=data26.get("ok"))

    status, data = api("POST", f"/api/proxy_report/tickets/{ticket26_id}/assign", {
        "target_handler": "钱质控",
        "target_handler_role": "质控",
        "assign_reason": "签收员试图指派",
        "role": "库房签收员", "operator": "张签收",
    })
    test("签收员越权指派被拦截", status, expected_status=403)

    print("  2.7 库房签收员无权结案 → 403")
    api("POST", f"/api/proxy_report/tickets/{ticket26_id}/assign", {
        "target_handler": "赵主管", "target_handler_role": "仓库主管",
        "assign_reason": "进入处理中", "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("POST", f"/api/proxy_report/tickets/{ticket26_id}/close", {
        "role": "库房签收员", "operator": "张签收",
        "conclusion": "签收员试图结案",
    })
    test("签收员越权结案被拦截", status, expected_status=403)

    print("  2.8 无效原因分类被拦截 → 400")
    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB003",
        "reason_category": "无效分类",
        "description": "测试无效分类",
        "role": "库房管理员", "operator": "陈管理员",
    })
    test("无效原因分类被拦截", status, expected_status=400)

    print()
    print("  场景2清理: 撤回/结案测试工单避免影响后续测试")
    api("POST", f"/api/proxy_report/tickets/{ticket24_id}/withdraw", {
        "role": "库房签收员", "operator": "张签收", "reason": "测试清理",
    })
    api("POST", f"/api/proxy_report/tickets/{ticket26_id}/close", {
        "role": "仓库主管", "operator": "赵主管", "conclusion": "测试清理",
    })

    # ==================================================================
    print()
    print("=== 场景3: 代报开关切换（仅影响新建单） ===")
    print()

    print("  3.1 开启代报配置")
    status, data = api("POST", "/api/proxy_report/config", {
        "allow_proxy_record": True,
        "operator": "管理员A",
    })
    test("开启代报配置成功", status,
         check=data.get("ok") and data.get("allow_proxy_record") is True)

    status, cfg = api("GET", "/api/proxy_report/config")
    test("查询配置显示已开启", status,
         check=cfg.get("allow_proxy_record") is True)

    print("  3.2 班组长代报张签收成功，四字段正确分离")
    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB002",
        "reason_category": "包装破损",
        "description": "班组长代一线员工录入：外包装破损",
        "role": "班组长",
        "operator": "周班长",
        "on_behalf_of": "张签收",
        "on_behalf_of_role": "库房签收员",
    })
    test("代报创建成功，四字段彻底分离", status,
         check=(data.get("ok")
                and data.get("originator") == "张签收"
                and data.get("originator_role") == "库房签收员"
                and data.get("responsibility_role") == "库房签收员"
                and data.get("proxy_recorder") == "周班长"
                and data.get("proxy_recorder_role") == "班组长"
                and data.get("current_handler") == "张签收"
                and data.get("current_handler_role") == "库房签收员"))
    ticket3_id = data["ticket_id"]
    print(f"    真实报单人: {data['originator']}（{data['originator_role']}）")
    print(f"    责任岗位: {data['responsibility_role']}")
    print(f"    代填人: {data['proxy_recorder']}（{data['proxy_recorder_role']}）")
    print(f"    当前处理人: {data['current_handler']}（{data['current_handler_role']}）")

    status, data = api("GET", f"/api/proxy_report/tickets/{ticket3_id}")
    test("详情页四字段清晰分离 + 创建时配置冻结", status,
         check=(data["originator"] == "张签收"
                and data["originator_role"] == "库房签收员"
                and data["responsibility_role"] == "库房签收员"
                and data["proxy_recorder"] == "周班长"
                and data["proxy_recorder_role"] == "班组长"
                and data["current_handler"] == "张签收"
                and data["current_handler_role"] == "库房签收员"
                and data["allow_proxy_at_create"] is True))

    print("  3.3 关闭代报配置（不影响已存在工单）")
    status, _ = api("POST", "/api/proxy_report/config", {
        "allow_proxy_record": False,
        "operator": "管理员A",
    })
    test("再次关闭代报配置", status, check=True)

    status, data = api("GET", f"/api/proxy_report/tickets/{ticket3_id}")
    test("已有工单的 allow_proxy_at_create 仍为 True（冻结）", status,
         check=data["allow_proxy_at_create"] is True)

    print("  3.4 关闭后新工单记录为不允许代报，代报被拦截")
    status, data34 = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB003",
        "reason_category": "设备故障",
        "description": "配置关闭后创建的工单",
        "role": "库房管理员",
        "operator": "陈管理员",
    })
    ticket34_id = data34["ticket_id"]
    status, data = api("GET", f"/api/proxy_report/tickets/{ticket34_id}")
    test("新建工单 allow_proxy_at_create 为 False", status,
         check=data["allow_proxy_at_create"] is False)

    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB002",
        "reason_category": "标签错误",
        "role": "班组长",
        "operator": "周班长",
        "on_behalf_of": "李签收",
        "on_behalf_of_role": "库房签收员",
    })
    test("代报关闭后新代报请求被拦截", status, expected_status=403)

    print("  3.5 没有代报权限的角色代报被拦截（出库员不在PROXY_ROLES）")
    api("POST", "/api/proxy_report/config", {
        "allow_proxy_record": True, "operator": "管理员A",
    })
    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB003",
        "reason_category": "标签错误",
        "role": "出库员",
        "operator": "张出库",
        "on_behalf_of": "李签收",
        "on_behalf_of_role": "库房签收员",
    })
    test("出库员代报被拦截（无代报权限）", status, expected_status=403)
    api("POST", "/api/proxy_report/config", {
        "allow_proxy_record": False, "operator": "管理员A",
    })

    # ==================================================================
    print()
    print("=== 场景4: 撤回重提责任重置（核心！按真实报单人重算） ===")
    print()

    print("  4.1 创建工单并指派→质控（代填人周班长，真实报单人张签收）")
    api("POST", "/api/proxy_report/config", {
        "allow_proxy_record": True, "operator": "管理员A",
    })
    status, data41 = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-001",
        "box_code": "PRB003",
        "reason_category": "污染风险",
        "description": "撤回重提测试工单（代报）",
        "role": "班组长",
        "operator": "周班长",
        "on_behalf_of": "张签收",
        "on_behalf_of_role": "库房签收员",
    })
    ticket4_id = data41["ticket_id"]
    test("创建代报工单成功", status,
         check=(data41.get("ok")
                and data41.get("originator") == "张签收"
                and data41.get("proxy_recorder") == "周班长"))

    api("POST", f"/api/proxy_report/tickets/{ticket4_id}/assign", {
        "target_handler": "钱质控", "target_handler_role": "质控",
        "assign_reason": "指派质控处理", "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("GET", f"/api/proxy_report/tickets/{ticket4_id}")
    test("指派后当前处理人为钱质控，代填人为周班长", status,
         check=(data["current_handler"] == "钱质控"
                and data["current_handler_role"] == "质控"
                and data["proxy_recorder"] == "周班长"
                and data["responsibility_role"] == "库房签收员"
                and len(data["assignment_history"]) == 1))
    print(f"    指派后当前处理人: {data['current_handler']}（{data['current_handler_role']}）")
    print(f"    代填人: {data['proxy_recorder']}（{data['proxy_recorder_role']}）")

    print("  4.2 真实报单人撤回工单（非代填人！）")
    status, data = api("POST", f"/api/proxy_report/tickets/{ticket4_id}/withdraw", {
        "role": "库房签收员",
        "operator": "张签收",
        "reason": "证据不足，撤回补充",
    })
    test("真实报单人撤回成功", status,
         check=data.get("ok") and data.get("status") == "已撤回")

    status, data = api("GET", f"/api/proxy_report/tickets/{ticket4_id}")
    test("撤回后状态为已撤回", status,
         check=(data["status"] == "已撤回"
                and data["withdrawn_by"] == "张签收"))

    print("  4.3 再次上报 → 责任重置为真实报单人（不沿用代填人/上一位处理人）！")
    status, data = api("POST", f"/api/proxy_report/tickets/{ticket4_id}/resubmit", {
        "role": "库房签收员",
        "operator": "张签收",
        "description": "补充了新的证据，重新提交",
        "reason": "补充证据后重新提交",
    })
    test("重新提交成功 → 当前处理人=张签收，不残留钱质控/周班长", status,
         check=(data.get("ok")
                and data.get("status") == "待指派"
                and data.get("originator") == "张签收"
                and data.get("current_handler") == "张签收"
                and data.get("current_handler_role") == "库房签收员"
                and data.get("responsibility_role") == "库房签收员"))

    status, data = api("GET", f"/api/proxy_report/tickets/{ticket4_id}")
    test("重提后验证：不沿用代填人/上一位处理人的岗位", status,
         check=(data["current_handler"] == "张签收"
                and data["current_handler_role"] == "库房签收员"
                and data["status"] == "待指派"
                and data["responsibility_role"] == "库房签收员"
                and len(data["assignment_history"]) == 1))
    print(f"    重提后当前处理人: {data['current_handler']}（{data['current_handler_role']}）")
    print(f"    代填人信息保留但不参与责任: {data['proxy_recorder']}（{data['proxy_recorder_role']}）")
    print(f"    责任岗位: {data['responsibility_role']}")
    print(f"    历史指派记录保留数: {len(data['assignment_history'])}")

    print("  4.4 非真实报单人不可重新提交 → 403")
    api("POST", f"/api/proxy_report/tickets/{ticket4_id}/withdraw", {
        "role": "库房签收员",
        "operator": "张签收",
        "reason": "再次撤回测试越权重提",
    })

    status, data = api("POST", f"/api/proxy_report/tickets/{ticket4_id}/resubmit", {
        "role": "班组长",
        "operator": "周班长",
        "description": "代填人试图重提",
    })
    test("代填人试图重提被拦截", status, expected_status=403)

    status, data = api("POST", f"/api/proxy_report/tickets/{ticket4_id}/resubmit", {
        "role": "质控",
        "operator": "钱质控",
        "description": "上一位处理人试图重提",
    })
    test("上一位处理人试图重提被拦截", status, expected_status=403)

    # 恢复以便后续用
    api("POST", f"/api/proxy_report/tickets/{ticket4_id}/resubmit", {
        "role": "库房签收员", "operator": "张签收",
    })
    api("POST", "/api/proxy_report/config", {
        "allow_proxy_record": False, "operator": "管理员A",
    })

    # ==================================================================
    print()
    print("=== 场景5: 重复冲突拦截 ===")
    print()

    print("  5.1 同箱号+同事由的活跃单禁止重复创建")
    boxes2, _, _ = create_full_batch("BATCH-PR-002", 2, "PRC")

    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-002",
        "box_code": "PRC001",
        "reason_category": "温度异常",
        "description": "首次报单",
        "role": "库房管理员", "operator": "陈管理员",
    })
    ticket5_id = data["ticket_id"]
    test("首次报单成功", status, check=data.get("ok"))

    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-002",
        "box_code": "PRC001",
        "reason_category": "温度异常",
        "description": "重复报单",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("同箱号同事由重复报单被拦截", status, expected_status=409)
    print(f"    返回提示: {str(data.get('detail', ''))[:80]}")

    print("  5.2 同批次+同事由（无箱号）也禁止重复创建")
    status, data52a = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-002",
        "reason_category": "流程违规",
        "description": "批次级首次报单",
        "role": "库房管理员", "operator": "陈管理员",
    })
    ticket52a_id = data52a["ticket_id"]
    test("批次级首次报单成功", status, check=data52a.get("ok"))

    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-002",
        "reason_category": "流程违规",
        "description": "批次级重复报单",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("批次级同由重复报单被拦截", status, expected_status=409)

    print("  5.3 结案后可再次创建（不拦截）")
    api("POST", f"/api/proxy_report/tickets/{ticket5_id}/assign", {
        "target_handler": "赵主管", "target_handler_role": "仓库主管",
        "assign_reason": "进入处理中", "role": "仓库主管", "operator": "赵主管",
    })
    api("POST", f"/api/proxy_report/tickets/{ticket5_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "已结案，测试重复报单",
    })

    status, data = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-002",
        "box_code": "PRC001",
        "reason_category": "温度异常",
        "description": "结案后再次报单",
        "role": "库房管理员", "operator": "陈管理员",
    })
    test("结案后同箱号同事由可再次创建", status, check=data.get("ok"))

    print("  5.4 撤回后重提不与自己冲突（排除自身ID）")
    status, data54a = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-002",
        "box_code": "PRC002",
        "reason_category": "包装破损",
        "description": "工单A",
        "role": "库房管理员", "operator": "陈管理员",
    })
    ticket54a_id = data54a["ticket_id"]
    test("创建工单A成功", status, check=data54a.get("ok"))

    status, data54b = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-002",
        "box_code": "PRC002",
        "reason_category": "包装破损",
        "description": "工单B（应该被拦截）",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("同箱号同事由第二张单被拦截", status, expected_status=409)

    api("POST", f"/api/proxy_report/tickets/{ticket54a_id}/withdraw", {
        "role": "库房管理员", "operator": "陈管理员",
        "reason": "证据不足撤回",
    })

    status, data = api("POST", f"/api/proxy_report/tickets/{ticket54a_id}/resubmit", {
        "role": "库房管理员", "operator": "陈管理员",
        "description": "补充证据后重提",
    })
    test("自己重提不与自己冲突（排除自身ID）", status,
         check=data.get("ok") and data.get("status") == "待指派")

    # ==================================================================
    print()
    print("=== 场景6: 按责任岗位筛选和当前处理岗位筛选 ===")
    print()

    print("  6.1 列表按 responsibility_role（责任岗位）筛选")
    status, all_tickets = api("GET", "/api/proxy_report/tickets")
    test("工单列表查询成功", status, check=len(all_tickets) > 0)
    print(f"    总工单数: {len(all_tickets)}")

    status, filtered = api("GET", "/api/proxy_report/tickets?responsibility_role=库房签收员")
    test("按责任岗位（库房签收员）筛选成功", status,
         check=all(t["responsibility_role"] == "库房签收员" for t in filtered) and len(filtered) > 0)
    print(f"    库房签收员责任单数: {len(filtered)}")

    status, filtered2 = api("GET", "/api/proxy_report/tickets?responsibility_role=仓库主管")
    test("按责任岗位（仓库主管）筛选成功", status,
         check=all(t["responsibility_role"] == "仓库主管" for t in filtered2))

    print("  6.2 列表按 current_handler_role（当前卡住岗位）筛选")
    status, filtered3 = api("GET", "/api/proxy_report/tickets?current_handler_role=质控")
    test("按当前处理岗位（质控）筛选成功", status,
         check=all(t["current_handler_role"] == "质控" for t in filtered3))
    print(f"    质控处理中单数: {len(filtered3)}")

    status, filtered3b = api("GET", "/api/proxy_report/tickets?current_handler_role=库房签收员")
    test("按当前处理岗位（库房签收员）筛选成功", status,
         check=all(t["current_handler_role"] == "库房签收员" for t in filtered3b) and len(filtered3b) > 0)

    print("  6.3 组合筛选（状态 + 责任岗位）")
    status, filtered4 = api("GET", "/api/proxy_report/tickets?status=已结案&responsibility_role=库房签收员")
    test("组合筛选（已结案 + 库房签收员责任）", status,
         check=all(t["status"] == "已结案" and t["responsibility_role"] == "库房签收员" for t in filtered4))

    print("  6.4 按箱号/批次筛选辅助对账")
    status, filtered5 = api("GET", "/api/proxy_report/tickets?box_code=PRB001")
    test("按箱号筛选成功", status,
         check=all(t["box_code"] == "PRB001" for t in filtered5) and len(filtered5) > 0)

    status, filtered6 = api("GET", "/api/proxy_report/tickets?batch_no=BATCH-PR-001")
    test("按批次筛选成功", status,
         check=all(t["batch_no"] == "BATCH-PR-001" for t in filtered6) and len(filtered6) > 0)

    # ==================================================================
    print()
    print("=== 场景7: 导出对账（JSON/CSV 含完整四字段和责任链） ===")
    print()

    print("  7.1 JSON 导出包含完整四字段和责任链")
    status, json_export = api("GET", "/api/proxy_report/export/json")
    test("JSON导出成功", status,
         check=json_export.get("total_tickets", 0) > 0
                and "tickets" in json_export)

    first_ticket = None
    for t in json_export["tickets"]:
        if t.get("proxy_recorder"):
            first_ticket = t
            break
    if first_ticket is None:
        first_ticket = json_export["tickets"][0]
    test("JSON导出包含完整四字段", status,
         check=("originator" in first_ticket
                and "originator_role" in first_ticket
                and "responsibility_role" in first_ticket
                and "proxy_recorder" in first_ticket
                and "proxy_recorder_role" in first_ticket
                and "current_handler" in first_ticket
                and "current_handler_role" in first_ticket
                and "responsibility_chain" in first_ticket
                and "assignment_history" in first_ticket
                and "audit_log" in first_ticket
                and "evidence_list" in first_ticket))
    print(f"    责任链节点数: {len(first_ticket.get('responsibility_chain', []))}")
    print(f"    四字段: 发起人={first_ticket.get('originator')} 责任岗={first_ticket.get('responsibility_role')} 代填人={first_ticket.get('proxy_recorder')} 当前岗={first_ticket.get('current_handler_role')}")

    print("  7.2 JSON 导出支持按责任岗位筛选")
    status, filtered_export = api("GET", "/api/proxy_report/export/json?responsibility_role=库房签收员")
    test("JSON导出按责任岗位筛选", status,
         check=all(t["responsibility_role"] == "库房签收员" for t in filtered_export.get("tickets", []))
                and len(filtered_export.get("tickets", [])) > 0)

    print("  7.3 JSON 导出支持按当前处理岗位筛选")
    status, filtered_export2 = api("GET", "/api/proxy_report/export/json?current_handler_role=库房签收员")
    test("JSON导出按当前处理岗位筛选", status,
         check=all(t["current_handler_role"] == "库房签收员" for t in filtered_export2.get("tickets", []))
                and len(filtered_export2.get("tickets", [])) > 0)

    print("  7.4 CSV 导出包含完整对账字段")
    status, csv_content = api("GET", "/api/proxy_report/export/csv", raw=True)
    test("CSV导出成功", status,
         check="originator" in csv_content
                and "originator_role" in csv_content
                and "responsibility_role" in csv_content
                and "proxy_recorder" in csv_content
                and "proxy_recorder_role" in csv_content
                and "current_handler" in csv_content
                and "current_handler_role" in csv_content
                and "responsibility_chain_text" in csv_content)

    lines = csv_content.strip().split("\n")
    header = lines[0]
    test("CSV表头包含关键字段", status,
         check=all(f in header for f in [
             "ticket_id", "ticket_no", "reason_category", "status",
             "originator", "originator_role", "responsibility_role",
             "proxy_recorder", "proxy_recorder_role",
             "current_handler", "current_handler_role",
             "responsibility_chain_text", "evidence_count",
             "allow_proxy_at_create",
         ]))
    print(f"    CSV 行数: {len(lines)} (含表头)")
    print(f"    CSV 表头: {header[:200]}...")

    print("  7.5 CSV 导出支持筛选（按责任岗位）")
    status, csv_filtered = api("GET", "/api/proxy_report/export/csv?responsibility_role=库房签收员", raw=True)
    lines_filtered = csv_filtered.strip().split("\n")
    test("CSV导出按责任岗位筛选（至少含表头）", status, check=len(lines_filtered) >= 1)
    print(f"    筛选后 CSV 行数: {len(lines_filtered)}")

    print("  7.6 批次汇总接口支持对账（按责任/当前岗位分布）")
    status, summary = api("GET", "/api/proxy_report/batches/BATCH-PR-001/summary")
    test("批次汇总查询成功", status,
         check=("total_tickets" in summary
                and "by_status" in summary
                and "by_category" in summary
                and "by_responsibility_role" in summary
                and "by_current_handler_role" in summary
                and "tickets" in summary))
    print(f"    批次总工单数: {summary['total_tickets']}")
    print(f"    按状态分布: {summary['by_status']}")
    print(f"    按责任岗位分布: {summary['by_responsibility_role']}")
    print(f"    按当前处理岗位分布: {summary['by_current_handler_role']}")

    # ==================================================================
    print()
    print("=== 场景8: 服务重启后的状态恢复（完整性修复） ===")
    print()

    print("  8.1 直接调用启动事件中的完整性恢复（验证函数存在并正确执行）")
    import importlib
    import app.database as db_module
    importlib.reload(db_module)

    repaired = db_module.recover_proxy_report_integrity()
    test("完整性恢复函数执行成功", 200,
         check=repaired is not None and isinstance(repaired, int))
    print(f"    修复的不一致记录数: {repaired}")

    print("  8.2 验证恢复后所有活跃工单四字段完整")
    status, all_active = api("GET", "/api/proxy_report/tickets?status=待指派")
    status2, all_active2 = api("GET", "/api/proxy_report/tickets?status=处理中")
    all_active_list = all_active + all_active2 if isinstance(all_active, list) and isinstance(all_active2, list) else []
    test("活跃工单查询成功", status, check=isinstance(all_active, list))

    if len(all_active_list) > 0:
        integrity_ok = all(
            t.get("originator") and t.get("originator_role")
            and t.get("responsibility_role")
            and t.get("current_handler") and t.get("current_handler_role")
            for t in all_active_list
        )
        test("活跃工单四字段无空值（恢复后）", 200, check=integrity_ok)
        print(f"    已验证 {len(all_active_list)} 条活跃工单完整性")

    # ==================================================================
    print()
    print("=== 场景9: 驳回→重新提交流程 ===")
    print()

    print("  9.1 先创建一张测试工单，我们模拟已驳回状态（直接从已撤回进入待指派后再走流程）")
    status, data9 = api("POST", "/api/proxy_report/tickets", {
        "batch_no": "BATCH-PR-002",
        "box_code": "PRC002",
        "reason_category": "其他",
        "description": "撤回重提测试2",
        "role": "库房管理员",
        "operator": "陈管理员",
    })
    ticket9_id = data9["ticket_id"]
    test("创建测试工单成功", status, check=data9.get("ok"))

    print("  9.2 撤回后再重新提交（模拟驳回→重提的流程效果）")
    api("POST", f"/api/proxy_report/tickets/{ticket9_id}/assign", {
        "target_handler": "赵主管", "target_handler_role": "仓库主管",
        "assign_reason": "先指派进入处理中", "role": "仓库主管", "operator": "赵主管",
    })
    status, data_before = api("GET", f"/api/proxy_report/tickets/{ticket9_id}")
    test("指派后当前处理人为赵主管", status,
         check=data_before["current_handler"] == "赵主管"
               and data_before["current_handler_role"] == "仓库主管")

    status, data = api("POST", f"/api/proxy_report/tickets/{ticket9_id}/withdraw", {
        "role": "库房管理员",
        "operator": "陈管理员",
        "reason": "被要求补充证据",
    })
    test("撤回成功", status, check=data.get("ok"))

    status, data = api("POST", f"/api/proxy_report/tickets/{ticket9_id}/resubmit", {
        "role": "库房管理员",
        "operator": "陈管理员",
        "description": "补充了详细的操作流程记录和时间线",
        "reason": "补充证据后重新提交",
    })
    test("重新提交成功 → 当前处理人重置为真实报单人（陈管理员）", status,
         check=(data.get("ok")
                and data.get("status") == "待指派"
                and data.get("current_handler") == "陈管理员"
                and data.get("current_handler_role") == "库房管理员"))

    status, data = api("GET", f"/api/proxy_report/tickets/{ticket9_id}")
    test("重提后状态回到待指派，不残留赵主管作为当前处理人", status,
         check=(data["status"] == "待指派"
                and data["current_handler"] == "陈管理员"
                and data["current_handler_role"] == "库房管理员"
                and data["resubmitted_by"] == "陈管理员"))
    print(f"    重提后当前处理人: {data['current_handler']}（{data['current_handler_role']}）")
    print(f"    上一位处理人赵主管已不在当前处理人字段，仅保留在历史指派中: {len(data['assignment_history'])} 条记录")

    print()
    print("  9.3 审计日志包含完整撤回和重新提交记录")
    status, audit9 = api("GET", f"/api/proxy_report/tickets/{ticket9_id}/audit")
    actions9 = [a["action"] for a in audit9]
    test("审计日志包含 创建/指派/撤回/重新提交", status,
         check=("创建异常代报受理单" in actions9
                and "指派责任人" in actions9
                and "撤回受理单" in actions9
                and "重新上报受理单" in actions9))
    print(f"    审计动作: {actions9}")

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
