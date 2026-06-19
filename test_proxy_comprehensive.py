"""
工单代理提交模块全面回归测试
覆盖 7 大场景：代理提交通路、同角色不同人越权拦截、配置切换只影响新单、
           撤回/重开/重新提交仅限创建人本人、撤回后再提、导出核对含代理信息、重启恢复
"""

import json
import sys
import time
import subprocess
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


def create_full_batch(batch_no, box_count=3, prefix="PX"):
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
    print("  工单代理提交模块全面回归测试")
    print("=" * 70)
    print()

    print("=== 阶段0: 初始化 ===")
    api("POST", "/api/thresholds", {
        "sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120
    })
    print("  阈值配置完成")

    api("POST", "/api/dispute/config", {
        "require_double_confirm": False,
        "allow_proxy_submit": False,
        "operator": "管理员A",
    })
    status, cfg = api("GET", "/api/dispute/config")
    test("初始配置: 代理提交关闭", status,
         check=cfg["allow_proxy_submit"] is False)
    print()

    # ==================================================================
    print("=== 场景1: 代理提交通路 ===")
    print()

    print("  1.1 未开启代理提交时，代理提交被拦截 → 403")
    boxes1, _, _ = create_full_batch("BATCH-PRX-001", 3, "PA")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-001",
        "box_codes": ["PA001", "PA002"],
        "problem_type": "温度超标",
        "evidence_desc": "运输途中温度超标",
        "role": "班组长",
        "operator": "刘班长",
        "on_behalf_of": "一线员工甲",
    })
    test("未开启代理提交时代理提交被拦截", status, expected_status=403)

    print()
    print("  1.2 开启代理提交配置")
    status, data = api("POST", "/api/dispute/config", {
        "require_double_confirm": False,
        "allow_proxy_submit": True,
        "operator": "管理员A",
    })
    test("开启代理提交配置成功", status,
         check=data.get("allow_proxy_submit") is True)

    print()
    print("  1.3 班组长代一线员工创建争议单")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-001",
        "box_codes": ["PA001", "PA002"],
        "problem_type": "温度超标",
        "evidence_desc": "班组长代录：运输途中多次超过8°C",
        "responsibility_judgment": "转运方责任",
        "deadline": "2026-06-26T18:00:00",
        "role": "班组长",
        "operator": "刘班长",
        "on_behalf_of": "一线员工甲",
    })
    test("班组长代理提交成功", status,
         check=(data.get("ok") and data.get("proxy_submitted") is True
                and data.get("created_by") == "一线员工甲"
                and data.get("submitted_by") == "刘班长"))
    ticket1_id = data["ticket_id"]
    print(f"    工单ID: {ticket1_id}, 创建人: {data['created_by']}, 提交人: {data['submitted_by']}")

    print()
    print("  1.4 工单详情包含代理信息")
    status, data = api("GET", f"/api/dispute/tickets/{ticket1_id}")
    test("工单详情含代理字段", status,
         check=(data["proxy_submitted"] is True
                and data["created_by"] == "一线员工甲"
                and data["submitted_by"] == "刘班长"))

    print()
    print("  1.5 审计日志记录代理信息")
    status, audit = api("GET", f"/api/dispute/tickets/{ticket1_id}/audit")
    create_audit = next(a for a in audit if a["action"] == "创建争议单")
    test("审计日志含代理提交详情", status,
         check=("代理提交" in create_audit["detail"]
                and "刘班长" in create_audit["detail"]
                and "一线员工甲" in create_audit["detail"]))

    print()
    print("  1.6 代理工单正常闭环: 确认→补充证据→结案")
    status, data = api("POST", f"/api/dispute/tickets/{ticket1_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    test("代理工单确认成功", status,
         check=data.get("ok") and data.get("status") == "处理中")

    status, data = api("POST", f"/api/dispute/tickets/{ticket1_id}/evidence", {
        "evidence_desc": "补充温度监控记录",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("代理工单补充证据成功", status, check=data.get("ok"))

    status, data = api("POST", f"/api/dispute/tickets/{ticket1_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "转运方负全责",
    })
    test("代理工单结案成功", status,
         check=data.get("ok") and data.get("status") == "已结案")

    print()
    print("  1.7 普通提交（非代理）不受影响")
    boxes1b, _, _ = create_full_batch("BATCH-PRX-001B", 2, "PB")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-001B",
        "box_codes": ["PB001"],
        "problem_type": "外包装破损",
        "evidence_desc": "箱子外表严重变形",
        "role": "库房签收员",
        "operator": "王五",
    })
    test("普通提交成功，无代理标记", status,
         check=(data.get("ok") and data.get("proxy_submitted") is False
                and data.get("created_by") == "王五"
                and data.get("submitted_by") is None))

    # ==================================================================
    print()
    print("=== 场景2: 同角色不同人越权拦截 ===")
    print()

    print("  2.1 创建人甲发起争议单，确认到处理中")
    boxes2, _, _ = create_full_batch("BATCH-PRX-002", 3, "PC")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-002",
        "box_codes": ["PC001"],
        "problem_type": "数量差异",
        "evidence_desc": "少一箱",
        "role": "库房签收员",
        "operator": "王五",
    })
    ticket2_id = data["ticket_id"]
    api("POST", f"/api/dispute/tickets/{ticket2_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })

    print()
    print("  2.2 同角色不同人(王六)撤回 → 403（关键：旧代码 OR 逻辑会放行）")
    status, data = api("POST", f"/api/dispute/tickets/{ticket2_id}/withdraw", {
        "role": "库房签收员", "operator": "王六",
        "reason": "同角色不同人越权尝试撤回",
    })
    test("同角色不同人撤回被拦截", status, expected_status=403)
    print(f"    返回提示: {data.get('detail', '')[:100]}")

    print()
    print("  2.3 同角色不同人(王六)不可重开已撤回的工单")
    status, data = api("POST", f"/api/dispute/tickets/{ticket2_id}/withdraw", {
        "role": "库房签收员", "operator": "王五",
        "reason": "创建人本人撤回",
    })
    test("创建人本人撤回成功", status,
         check=data.get("ok") and data.get("status") == "已撤回")

    status, data = api("POST", f"/api/dispute/tickets/{ticket2_id}/reopen", {
        "role": "库房签收员", "operator": "王六",
        "reason": "同角色不同人越权尝试重开",
    })
    test("同角色不同人重开被拦截", status, expected_status=403)

    print()
    print("  2.4 同角色不同人(王六)不可重新提交已驳回的工单")
    status, data = api("POST", f"/api/dispute/tickets/{ticket2_id}/reopen", {
        "role": "库房签收员", "operator": "王五",
        "reason": "创建人本人重开",
    })
    test("创建人本人重开成功", status, check=data.get("ok"))

    status, data = api("POST", f"/api/dispute/tickets/{ticket2_id}/reject", {
        "role": "仓库主管", "operator": "赵主管",
        "reason": "证据不足",
    })
    test("驳回成功", status, check=data.get("status") == "已驳回")

    status, data = api("POST", f"/api/dispute/tickets/{ticket2_id}/resubmit", {
        "role": "库房签收员", "operator": "王六",
        "evidence_desc": "同角色不同人越权尝试重新提交",
    })
    test("同角色不同人重新提交被拦截", status, expected_status=403)

    print()
    print("  2.5 创建人本人可以重新提交")
    status, data = api("POST", f"/api/dispute/tickets/{ticket2_id}/resubmit", {
        "role": "库房签收员", "operator": "王五",
        "evidence_desc": "补充证据后重新提交",
    })
    test("创建人本人重新提交成功", status,
         check=data.get("ok") and data.get("status") == "待确认")

    print()
    print("  2.6 代理提交的工单，被代理人（创建人）本人可操作")
    boxes2b, _, _ = create_full_batch("BATCH-PRX-002B", 2, "PD")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-002B",
        "box_codes": ["PD001"],
        "problem_type": "包装破损",
        "role": "班组长",
        "operator": "刘班长",
        "on_behalf_of": "一线员工乙",
    })
    ticket2b_id = data["ticket_id"]
    test("班组长代理一线员工乙提交成功", status, check=data.get("ok"))

    api("POST", f"/api/dispute/tickets/{ticket2b_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })

    status, data = api("POST", f"/api/dispute/tickets/{ticket2b_id}/withdraw", {
        "role": "库房签收员", "operator": "一线员工乙",
        "reason": "被代理人本人撤回",
    })
    test("被代理人(创建人)本人撤回成功", status,
         check=data.get("ok") and data.get("status") == "已撤回")

    print()
    print("  2.7 代理提交人（刘班长）不是创建人，不可撤回")
    boxes2c, _, _ = create_full_batch("BATCH-PRX-002C", 2, "PE")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-002C",
        "box_codes": ["PE001"],
        "problem_type": "标签错误",
        "role": "班组长",
        "operator": "刘班长",
        "on_behalf_of": "一线员工丙",
    })
    ticket2c_id = data["ticket_id"]
    api("POST", f"/api/dispute/tickets/{ticket2c_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("POST", f"/api/dispute/tickets/{ticket2c_id}/withdraw", {
        "role": "班组长", "operator": "刘班长",
        "reason": "代理提交人试图撤回",
    })
    test("代理提交人(非创建人)撤回被拦截", status, expected_status=403)

    # ==================================================================
    print()
    print("=== 场景3: 配置切换只影响新单 ===")
    print()

    print("  3.1 当前代理提交已开启，创建一个代理工单")
    boxes3a, _, _ = create_full_batch("BATCH-PRX-003A", 2, "PF")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-003A",
        "box_codes": ["PF001"],
        "problem_type": "温度异常",
        "role": "班组长",
        "operator": "刘班长",
        "on_behalf_of": "一线员工丁",
    })
    ticket3a_id = data["ticket_id"]
    test("开启时代理提交成功", status,
         check=data.get("proxy_submitted") is True)

    print()
    print("  3.2 关闭代理提交配置")
    status, data = api("POST", "/api/dispute/config", {
        "require_double_confirm": False,
        "allow_proxy_submit": False,
        "operator": "管理员A",
    })
    test("关闭代理提交配置", status,
         check=data.get("allow_proxy_submit") is False)

    print()
    print("  3.3 关闭后代理提交被拦截 → 403")
    boxes3b, _, _ = create_full_batch("BATCH-PRX-003B", 2, "PG")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-003B",
        "box_codes": ["PG001"],
        "problem_type": "数量差异",
        "role": "班组长",
        "operator": "刘班长",
        "on_behalf_of": "一线员工戊",
    })
    test("关闭代理配置后代理提交被拦截", status, expected_status=403)

    print()
    print("  3.4 关闭后普通提交仍正常")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-003B",
        "box_codes": ["PG001"],
        "problem_type": "数量差异",
        "evidence_desc": "普通提交",
        "role": "库房签收员",
        "operator": "王五",
    })
    test("关闭代理后普通提交正常", status,
         check=data.get("ok") and data.get("proxy_submitted") is False)

    print()
    print("  3.5 已创建的代理工单不受影响")
    status, data = api("GET", f"/api/dispute/tickets/{ticket3a_id}")
    test("旧代理工单代理标记保留", status,
         check=data["proxy_submitted"] is True)

    api("POST", f"/api/dispute/tickets/{ticket3a_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("POST", f"/api/dispute/tickets/{ticket3a_id}/evidence", {
        "evidence_desc": "关闭配置后仍可补证",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("配置关闭后旧代理工单仍可操作", status, check=data.get("ok"))

    print()
    print("  3.6 配置切换不影响双确认配置")
    status, data = api("POST", "/api/dispute/config", {
        "require_double_confirm": True,
        "allow_proxy_submit": True,
        "operator": "管理员A",
    })
    test("双确认+代理提交同时开启", status,
         check=(data.get("require_double_confirm") is True
                and data.get("allow_proxy_submit") is True))

    boxes3c, _, _ = create_full_batch("BATCH-PRX-003C", 2, "PH")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-003C",
        "box_codes": ["PH001"],
        "problem_type": "冷链断裂",
        "role": "班组长",
        "operator": "刘班长",
        "on_behalf_of": "一线员工己",
    })
    ticket3c_id = data["ticket_id"]
    test("双确认+代理提交工单创建成功", status,
         check=(data.get("proxy_submitted") is True
                and data.get("require_double_confirm") is True))

    api("POST", f"/api/dispute/tickets/{ticket3c_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("GET", f"/api/dispute/tickets/{ticket3c_id}")
    test("主管确认后仍为待确认", status,
         check=data["status"] == "待确认" and data["supervisor_confirmed"] is True)

    api("POST", f"/api/dispute/tickets/{ticket3c_id}/confirm", {
        "role": "质控", "operator": "钱质控",
    })
    status, data = api("GET", f"/api/dispute/tickets/{ticket3c_id}")
    test("双确认完成后进入处理中", status, check=data["status"] == "处理中")

    # ==================================================================
    print()
    print("=== 场景4: 撤回后再提 ===")
    print()

    print("  4.1 代理创建→确认→创建人撤回→创建人重开→确认→结案")
    boxes4, _, _ = create_full_batch("BATCH-PRX-004", 2, "PI")
    api("POST", "/api/dispute/config", {
        "require_double_confirm": False,
        "allow_proxy_submit": True,
        "operator": "管理员A",
    })
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-004",
        "box_codes": ["PI001"],
        "problem_type": "外包装破损",
        "evidence_desc": "代理补录：箱子变形",
        "role": "班组长",
        "operator": "刘班长",
        "on_behalf_of": "一线员工庚",
    })
    ticket4_id = data["ticket_id"]
    test("代理创建工单成功", status,
         check=data.get("created_by") == "一线员工庚")

    api("POST", f"/api/dispute/tickets/{ticket4_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    test("确认后进入处理中", status,
         check=True)

    status, data = api("POST", f"/api/dispute/tickets/{ticket4_id}/withdraw", {
        "role": "库房签收员", "operator": "一线员工庚",
        "reason": "创建人需要补充更多证据",
    })
    test("被代理人(创建人)撤回成功", status,
         check=data.get("ok") and data.get("status") == "已撤回")

    status, data = api("POST", f"/api/dispute/tickets/{ticket4_id}/reopen", {
        "role": "库房签收员", "operator": "一线员工庚",
        "reason": "证据补充完毕，重新发起",
    })
    test("创建人重开成功", status,
         check=data.get("ok") and data.get("status") == "待确认")

    api("POST", f"/api/dispute/tickets/{ticket4_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("POST", f"/api/dispute/tickets/{ticket4_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "确认外包装破损为转运方责任",
    })
    test("重开后结案成功", status,
         check=data.get("ok") and data.get("status") == "已结案")

    print()
    print("  4.2 驳回后重新提交（代理工单）")
    boxes4b, _, _ = create_full_batch("BATCH-PRX-004B", 2, "PJ")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-004B",
        "box_codes": ["PJ001"],
        "problem_type": "标签错误",
        "evidence_desc": "代理补录标签信息",
        "role": "班组长",
        "operator": "刘班长",
        "on_behalf_of": "一线员工辛",
    })
    ticket4b_id = data["ticket_id"]

    status, data = api("POST", f"/api/dispute/tickets/{ticket4b_id}/reject", {
        "role": "仓库主管", "operator": "赵主管",
        "reason": "证据不够清晰",
    })
    test("驳回代理工单成功", status,
         check=data.get("status") == "已驳回")

    status, data = api("POST", f"/api/dispute/tickets/{ticket4b_id}/resubmit", {
        "role": "库房签收员", "operator": "一线员工辛",
        "evidence_desc": "补充了标签照片",
    })
    test("创建人重新提交代理工单成功", status,
         check=data.get("ok") and data.get("status") == "待确认")

    # ==================================================================
    print()
    print("=== 场景5: 导出核对含代理信息 ===")
    print()

    print("  5.1 JSON 导出包含代理字段")
    status, export = api("GET", "/api/dispute/export/json")
    test("JSON导出成功", status,
         check="generated_at" in export and "tickets" in export)

    proxy_tickets = [t for t in export["tickets"] if t.get("proxy_submitted")]
    test("导出包含代理工单", status, check=len(proxy_tickets) >= 1)

    if proxy_tickets:
        pt = proxy_tickets[0]
        test("代理工单导出含submitted_by", status,
             check=pt.get("submitted_by") is not None
                   and pt.get("submitted_by") != "")
        test("代理工单导出proxy_submitted为true", status,
             check=pt.get("proxy_submitted") is True)
        test("代理工单导出created_by为被代理人", status,
             check=pt.get("created_by") in ["一线员工甲", "一线员工丁",
                                            "一线员工己", "一线员工庚",
                                            "一线员工辛", "一线员工乙",
                                            "一线员工丙"])

    non_proxy_tickets = [t for t in export["tickets"] if not t.get("proxy_submitted")]
    if non_proxy_tickets:
        npt = non_proxy_tickets[0]
        test("非代理工导出submitted_by为null", status,
             check=npt.get("submitted_by") is None)
        test("非代理工导出proxy_submitted为false", status,
             check=npt.get("proxy_submitted") is False)

    print()
    print("  5.2 CSV 导出包含代理字段")
    status, csv_body = api("GET", "/api/dispute/export/csv", raw=True)
    test("CSV导出成功", status,
         check=isinstance(csv_body, str) and "submitted_by" in csv_body
               and "proxy_submitted" in csv_body)

    print()
    print("  5.3 按状态筛选导出（含代理工单）")
    status, export_closed = api("GET", "/api/dispute/export/json?status=已结案")
    test("已结案工单导出", status,
         check=len(export_closed["tickets"]) >= 1)

    closed_proxy = [t for t in export_closed["tickets"] if t.get("proxy_submitted")]
    test("已结案中包含代理工单", status, check=len(closed_proxy) >= 1)

    print()
    print("  5.4 审计日志与工单状态对齐")
    all_consistent = True
    for t in export["tickets"]:
        audit_log = t.get("audit_log", [])
        if not audit_log:
            continue
        first_action = audit_log[0]
        if first_action["action"] != "创建争议单":
            all_consistent = False
            break
        if t["status"] == "已结案":
            has_close = any(a["action"] == "结案争议单" for a in audit_log)
            if not has_close:
                all_consistent = False
                break
        if t.get("proxy_submitted") and audit_log:
            has_proxy_in_detail = any("代理提交" in a.get("detail", "")
                                      for a in audit_log
                                      if a["action"] == "创建争议单")
            if not has_proxy_in_detail:
                all_consistent = False
                break
    test("所有工单审计日志与状态一致", status, check=all_consistent)

    # ==================================================================
    print()
    print("=== 场景6: 重启恢复 ===")
    print()

    print("  6.1 记录重启前状态")
    status, before_ticket = api("GET", f"/api/dispute/tickets/{ticket3c_id}")
    test("重启前工单查询成功", status,
         check=(before_ticket["status"] == "处理中"
                and before_ticket["proxy_submitted"] is True
                and before_ticket["created_by"] == "一线员工己"
                and before_ticket["submitted_by"] == "刘班长"
                and before_ticket["require_double_confirm"] is True))

    status, before_audit = api("GET", f"/api/dispute/tickets/{ticket3c_id}/audit")
    audit_count_before = len(before_audit)

    status, before_config = api("GET", "/api/dispute/config")
    print(f"    配置: 双确认={before_config['require_double_confirm']}, "
          f"代理提交={before_config['allow_proxy_submit']}")

    print()
    print("  6.2 重启服务...")
    try:
        status_port = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True
        )
        pid = None
        for line in status_port.stdout.split("\n"):
            if ":8000" in line and "LISTENING" in line:
                pid = line.strip().split()[-1]
                break
        if pid:
            print(f"    找到服务 PID: {pid}")
            wmi_result = subprocess.run(
                ["powershell", "-Command",
                 f"Get-WmiObject Win32_Process -Filter \"ProcessId = '{pid}'\" | Select-Object ProcessId, CommandLine, ExecutablePath | ConvertTo-Json"],
                capture_output=True, text=True
            )
            try:
                proc_info = json.loads(wmi_result.stdout)
                if proc_info and "uvicorn" in str(proc_info.get("CommandLine", "")).lower() \
                        and "app.main:app" in str(proc_info.get("CommandLine", "")):
                    print(f"    OK 确认归属: PID={pid} 是当前项目的 uvicorn 服务")
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    time.sleep(2)
                else:
                    print(f"    FAIL 进程归属验证失败，跳过停止")
            except Exception as e:
                print(f"    进程归属验证出错: {e}，跳过停止")
    except Exception as e:
        print(f"    停止服务时出错: {e}")

    print("    启动服务...")
    devnull = open(os.devnull, 'w')
    subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd="d:\\workSpace\\AI__SPACE\\zzz-00066",
        stdout=devnull, stderr=devnull
    )
    print("    等待服务启动...")
    if wait_for_service():
        print("    服务已重启")
    else:
        print("    警告: 服务启动超时，继续测试")

    print()
    print("  6.3 验证重启后数据完整")
    status, after_ticket = api("GET", f"/api/dispute/tickets/{ticket3c_id}")
    test("重启后工单状态一致", status,
         check=(after_ticket["status"] == before_ticket["status"]
                and after_ticket["require_double_confirm"] == before_ticket["require_double_confirm"]
                and after_ticket["proxy_submitted"] == before_ticket["proxy_submitted"]
                and after_ticket["created_by"] == before_ticket["created_by"]
                and after_ticket["submitted_by"] == before_ticket["submitted_by"]))

    status, after_audit = api("GET", f"/api/dispute/tickets/{ticket3c_id}/audit")
    test("重启后审计记录数一致", status,
         check=len(after_audit) == audit_count_before)

    status, after_config = api("GET", "/api/dispute/config")
    test("重启后配置保留(双确认+代理提交)", status,
         check=(after_config["require_double_confirm"] == before_config["require_double_confirm"]
                and after_config["allow_proxy_submit"] == before_config["allow_proxy_submit"]))

    print()
    print("  6.4 重启后继续操作处理中的工单")
    status, _ = api("POST", f"/api/dispute/tickets/{ticket3c_id}/evidence", {
        "evidence_desc": "重启后补充证据",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("重启后可继续操作处理中的工单", status, check=True)

    status, data = api("POST", f"/api/dispute/tickets/{ticket3c_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "经调查确认冷链断裂为转运方责任",
    })
    test("重启后结案成功", status,
         check=data.get("ok") and data.get("status") == "已结案")

    # ==================================================================
    print()
    print("=== 场景7: 权限与重复建单 ===")
    print()

    print("  7.1 无权角色不可代理提交")
    api("POST", "/api/dispute/config", {
        "require_double_confirm": False,
        "allow_proxy_submit": True,
        "operator": "管理员A",
    })
    boxes7, _, _ = create_full_batch("BATCH-PRX-007", 2, "PK")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-007",
        "box_codes": ["PK001"],
        "problem_type": "温度超标",
        "role": "库房签收员",
        "operator": "王五",
        "on_behalf_of": "一线员工壬",
    })
    test("库房签收员不可代理提交", status, expected_status=403)

    print()
    print("  7.2 重复建单拦截（代理提交也受约束）")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-007",
        "box_codes": ["PK001"],
        "problem_type": "温度超标",
        "evidence_desc": "普通创建",
        "role": "库房签收员",
        "operator": "王五",
    })
    ticket7_id = data["ticket_id"]
    test("普通创建成功", status, check=data.get("ok"))

    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-PRX-007",
        "box_codes": ["PK002"],
        "problem_type": "其他问题",
        "role": "班组长",
        "operator": "刘班长",
        "on_behalf_of": "一线员工癸",
    })
    test("同批次有活跃工单时代理提交被拦截", status, expected_status=409)

    print()
    print("  7.3 工单列表包含代理信息")
    status, tickets = api("GET", "/api/dispute/tickets")
    test("工单列表查询成功", status, check=len(tickets) >= 1)

    proxy_in_list = any(
        t.get("proxy_submitted") or
        (t.get("submitted_by") is not None and t.get("submitted_by") != "")
        for t in tickets
    )
    test("工单列表包含代理工单", status, check=proxy_in_list)

    # ==================================================================
    print()
    print("=" * 70)
    print(f"  测试汇总: {passed} passed, {failed} failed")
    print("=" * 70)

    if failed > 0:
        print()
        print("  !!! 存在失败的测试，请检查 !!!")
        sys.exit(1)
    else:
        print()
        print("  OK 所有测试通过!")
        sys.exit(0)


if __name__ == "__main__":
    main()
