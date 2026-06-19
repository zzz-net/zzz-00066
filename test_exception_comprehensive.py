"""
仓内异常处置单模块全面回归测试
覆盖 7 大场景：正常闭环、越权拦截、撤回重开、转交责任、配置切换、重启恢复、导出核对
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


def create_full_batch(batch_no, box_count=3, prefix="EV"):
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
    print("  仓内异常处置单模块全面回归测试")
    print("=" * 70)
    print()

    print("=== 阶段0: 初始化 ===")
    api("POST", "/api/thresholds", {
        "sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120
    })
    print("  阈值配置完成")

    api("POST", "/api/exception/config", {
        "allow_proxy_record": False,
        "operator": "管理员A",
    })
    print("  异常配置: 代录已关闭")
    print()

    # ==================================================================
    print("=== 场景1: 正常闭环 - 创建→确认→补证→转交→结案 ===")
    print()

    print("  1.1 创建批次 BATCH-EXC-001 并完成签收 (3箱)")
    boxes, status, _ = create_full_batch("BATCH-EXC-001", 3, "EV")
    test("批次签收完成", status, check=True)

    print()
    print("  1.2 库房签收员发起处置单")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-001",
        "box_code": "EV001",
        "reason_category": "温度异常",
        "description": "开箱时发现温度记录仪显示运输途中多次超过8°C",
        "role": "库房签收员",
        "operator": "王五",
    })
    test("创建处置单成功", status,
         check=(data.get("ok") and data.get("status") == "待处理"
                and data.get("initiator") == "王五"
                and data.get("proxy_recorder") is None
                and data.get("current_handler") == "王五"))
    ticket1_id = data["ticket_id"]
    ticket1_no = data["ticket_no"]
    print(f"    工单号: {ticket1_no}, 发起人: {data['initiator']}, 当前处理人: {data['current_handler']}")

    status, data = api("GET", f"/api/exception/tickets/{ticket1_id}")
    test("工单详情字段完整分离", status,
         check=(data["initiator"] == "王五"
                and data["proxy_recorder"] is None
                and data["current_handler"] == "王五"
                and data["reason_category"] == "温度异常"
                and data["box_code"] == "EV001"
                and len(data["evidence_list"]) >= 1
                and len(data["transfer_history"]) == 0))

    print()
    print("  1.3 仓库主管确认 → 进入处理中")
    status, data = api("POST", f"/api/exception/tickets/{ticket1_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管", "reason": "确认异常属实",
    })
    test("确认成功，状态变为处理中", status,
         check=data.get("ok") and data.get("status") == "处理中")

    print()
    print("  1.4 处理中补充证据（图片+文字）")
    status, data = api("POST", f"/api/exception/tickets/{ticket1_id}/evidence", {
        "evidence_type": "image",
        "evidence_content": "温度记录仪导出的温度曲线照片，显示3小时内温度持续超标",
        "role": "库房签收员", "operator": "王五",
    })
    test("补充证据成功", status, check=data.get("ok"))

    status, data = api("GET", f"/api/exception/tickets/{ticket1_id}")
    test("证据列表有2条", status, check=len(data["evidence_list"]) == 2)

    print()
    print("  1.5 转交责任人给质控部门")
    status, data = api("POST", f"/api/exception/tickets/{ticket1_id}/transfer", {
        "target_handler": "钱质控",
        "target_handler_role": "质控",
        "transfer_reason": "需要专业数据分析",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("转交责任人成功", status,
         check=(data.get("ok")
                and data.get("from_handler") == "王五"
                and data.get("to_handler") == "钱质控"))

    status, data = api("GET", f"/api/exception/tickets/{ticket1_id}")
    test("当前处理人已更新为钱质控", status,
         check=(data["current_handler"] == "钱质控"
                and data["current_handler_role"] == "质控"
                and len(data["transfer_history"]) == 1
                and data["transfer_history"][0]["from_handler"] == "王五"
                and data["transfer_history"][0]["to_handler"] == "钱质控"))

    print()
    print("  1.6 质控补充证据后结案")
    status, data = api("POST", f"/api/exception/tickets/{ticket1_id}/evidence", {
        "evidence_type": "text",
        "evidence_content": "质控数据分析报告：确认冷链设备故障导致温度超标，样本已失效",
        "role": "质控", "operator": "钱质控",
    })
    test("质控补充证据成功", status, check=data.get("ok"))

    status, data = api("POST", f"/api/exception/tickets/{ticket1_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "转运方冷链设备故障导致样本失效，转运方承担全部责任",
        "reason": "调查完毕",
    })
    test("结案成功", status,
         check=(data.get("ok") and data.get("status") == "已结案"
                and "转运方" in data.get("conclusion", "")))

    status, data = api("GET", f"/api/exception/tickets/{ticket1_id}")
    test("结案后状态和字段完整", status,
         check=(data["status"] == "已结案"
                and data["closed_by"] == "赵主管"
                and data["current_handler"] == "钱质控"))

    print()
    print("  1.7 审计日志完整")
    status, audit = api("GET", f"/api/exception/tickets/{ticket1_id}/audit")
    actions = [a["action"] for a in audit]
    test("审计日志包含 创建/确认/补证x2/转交/结案", status,
         check=("创建处置单" in actions
                and "确认处置单" in actions
                and "补充证据" in actions
                and "转交责任人" in actions
                and "结案处置单" in actions
                and actions.count("补充证据") >= 2))
    print(f"    审计动作: {actions}")

    # ==================================================================
    print()
    print("=== 场景2: 越权拦截 ===")
    print()

    print("  2.1 出库员无权发起处置单 → 403")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-001",
        "box_code": "EV002",
        "reason_category": "包装破损",
        "role": "出库员", "operator": "张三",
    })
    test("出库员发起被拦截", status, expected_status=403)

    print("  2.2 同批次同箱号同原因重复报单被拦截 → 409")
    boxes2, _, _ = create_full_batch("BATCH-EXC-002", 2, "EW")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-002",
        "box_code": "EW001",
        "reason_category": "包装破损",
        "description": "首次报单",
        "role": "库房管理员", "operator": "孙管理",
    })
    ticket2_id = data["ticket_id"]
    test("首次报单成功", status, check=data.get("ok"))

    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-002",
        "box_code": "EW001",
        "reason_category": "包装破损",
        "description": "重复报单",
        "role": "库房管理员", "operator": "孙管理",
    })
    test("同批次同箱号同原因重复报单被拦截", status, expected_status=409)
    print(f"    返回提示: {data.get('detail', '')[:80]}")

    print("  2.3 已结案工单不可再补证 → 409")
    status, data = api("POST", f"/api/exception/tickets/{ticket1_id}/evidence", {
        "evidence_type": "text",
        "evidence_content": "试图对已结案工单补证",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("已结案工单补证被拦截", status, expected_status=409)

    print("  2.4 库房签收员无权结案 → 403")
    status, data24 = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-002",
        "box_code": "EW002",
        "reason_category": "数量差异",
        "description": "越权结案测试工单",
        "role": "仓库主管", "operator": "赵主管",
    })
    ticket24_id = data24["ticket_id"]
    api("POST", f"/api/exception/tickets/{ticket24_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("POST", f"/api/exception/tickets/{ticket24_id}/close", {
        "role": "库房签收员", "operator": "王五",
        "conclusion": "签收员试图结案",
    })
    test("签收员越权结案被拦截", status, expected_status=403)

    print("  2.5 非发起人不可撤回 → 403")
    api("POST", f"/api/exception/tickets/{ticket2_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("POST", f"/api/exception/tickets/{ticket2_id}/withdraw", {
        "role": "仓库主管", "operator": "赵主管",
        "reason": "不是发起人想撤回",
    })
    test("非发起人撤回被拦截", status, expected_status=403)

    print("  2.6 同角色不同人不可撤回 → 403")
    status, data = api("POST", f"/api/exception/tickets/{ticket2_id}/withdraw", {
        "role": "库房管理员", "operator": "周管理",
        "reason": "同角色不同人越权撤回",
    })
    test("同角色不同人撤回被拦截", status, expected_status=403)

    print("  2.7 非仓库主管/质控不可转交责任人 → 403")
    status, data = api("POST", f"/api/exception/tickets/{ticket2_id}/transfer", {
        "target_handler": "某人",
        "target_handler_role": "质控",
        "role": "库房签收员", "operator": "王五",
    })
    test("签收员越权转交被拦截", status, expected_status=403)

    print("  2.8 待处理状态不可转交 → 409")
    boxes2b, _, _ = create_full_batch("BATCH-EXC-002B", 2, "EX")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-002B",
        "box_code": "EX001",
        "reason_category": "标签错误",
        "role": "库房管理员", "operator": "孙管理",
    })
    ticket2b_id = data["ticket_id"]
    status, data = api("POST", f"/api/exception/tickets/{ticket2b_id}/transfer", {
        "target_handler": "钱质控",
        "target_handler_role": "质控",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("待处理状态转交被拦截", status, expected_status=409)

    print("  2.9 无效原因分类被拦截 → 400")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-002B",
        "box_code": "EX002",
        "reason_category": "不存在的分类",
        "role": "库房管理员", "operator": "孙管理",
    })
    test("无效原因分类被拦截", status, expected_status=400)

    # ==================================================================
    print()
    print("=== 场景3: 撤回重开与重提 ===")
    print()

    print("  3.1 创建处置单并确认到处理中")
    boxes3, _, _ = create_full_batch("BATCH-EXC-003", 2, "EY")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-003",
        "box_code": "EY001",
        "reason_category": "数量差异",
        "description": "实际到货比单据少1箱",
        "role": "库房管理员", "operator": "孙管理",
    })
    ticket3_id = data["ticket_id"]
    api("POST", f"/api/exception/tickets/{ticket3_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })

    print()
    print("  3.2 发起人撤回处置单（从处理中）")
    status, data = api("POST", f"/api/exception/tickets/{ticket3_id}/withdraw", {
        "role": "库房管理员", "operator": "孙管理",
        "reason": "需要补充更多证据",
    })
    test("撤回成功，状态变为已撤回", status,
         check=data.get("ok") and data.get("status") == "已撤回")

    status, data = api("GET", f"/api/exception/tickets/{ticket3_id}")
    test("撤回后字段正确", status,
         check=(data["status"] == "已撤回"
                and data["withdrawn_by"] == "孙管理"))

    print()
    print("  3.3 撤回后重新提交，权限重新校验")
    status, data = api("POST", f"/api/exception/tickets/{ticket3_id}/resubmit", {
        "role": "库房管理员", "operator": "孙管理",
        "description": "补充：已联系转运方，对方承认少装1箱，提供了装车录像截图",
        "reason": "证据已充分",
    })
    test("重新提交成功，状态变回待处理", status,
         check=data.get("ok") and data.get("status") == "待处理")

    status, data = api("GET", f"/api/exception/tickets/{ticket3_id}")
    test("重提后处理人重置为发起人", status,
         check=(data["status"] == "待处理"
                and data["current_handler"] == "孙管理"
                and data["resubmitted_by"] == "孙管理"))

    print()
    print("  3.4 重提后可以重新确认和结案")
    status, data = api("POST", f"/api/exception/tickets/{ticket3_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    test("重提后重新确认成功", status, check=data.get("status") == "处理中")

    status, data = api("POST", f"/api/exception/tickets/{ticket3_id}/close", {
        "role": "质控", "operator": "钱质控",
        "conclusion": "转运方承认少装，转运方补发并承担相应损失",
    })
    test("重提后结案成功", status, check=data.get("status") == "已结案")

    print()
    print("  3.5 驳回后重新提交")
    boxes3b, _, _ = create_full_batch("BATCH-EXC-003B", 2, "EZ")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-003B",
        "box_code": "EZ001",
        "reason_category": "流程违规",
        "description": "疑似操作不规范",
        "role": "库房管理员", "operator": "孙管理",
    })
    ticket3b_id = data["ticket_id"]

    status, data = api("POST", f"/api/exception/tickets/{ticket3b_id}/reject", {
        "role": "仓库主管", "operator": "赵主管",
        "reason": "描述太模糊，需要补充具体时间和证据",
    })
    test("驳回成功", status, check=data.get("status") == "已驳回")

    status, data = api("POST", f"/api/exception/tickets/{ticket3b_id}/resubmit", {
        "role": "库房管理员", "operator": "孙管理",
        "description": "补充：6月18日14:30，未按规定进行温度复测就签收，监控录像可查",
    })
    test("驳回后重提成功", status, check=data.get("status") == "待处理")

    print()
    print("  3.6 重提时重复报单检查")
    boxes3c, _, _ = create_full_batch("BATCH-EXC-003C", 2, "FA")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-003C",
        "box_code": "FA001",
        "reason_category": "污染风险",
        "role": "库房管理员", "operator": "孙管理",
    })
    ticket3c_first_id = data["ticket_id"]

    status, data = api("POST", f"/api/exception/tickets/{ticket3c_first_id}/withdraw", {
        "role": "库房管理员", "operator": "孙管理",
        "reason": "测试重提重复拦截",
    })

    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-003C",
        "box_code": "FA001",
        "reason_category": "污染风险",
        "role": "库房管理员", "operator": "孙管理",
    })
    ticket3c_second_id = data["ticket_id"]
    test("撤回期间可以新建相同工单", status, check=data.get("ok"))

    api("POST", f"/api/exception/tickets/{ticket3c_second_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })

    status, data = api("POST", f"/api/exception/tickets/{ticket3c_first_id}/resubmit", {
        "role": "库房管理员", "operator": "孙管理",
    })
    test("重提时检测到活跃工单被拦截", status, expected_status=409)

    print()
    print("  3.7 转交后撤回重提，责任归属重新判定")
    boxes3d, _, _ = create_full_batch("BATCH-EXC-003D", 2, "FB")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-003D",
        "box_code": "FB001",
        "reason_category": "温度异常",
        "description": "测试转交后撤回重提责任重置",
        "role": "库房管理员", "operator": "孙管理",
    })
    ticket3d_id = data["ticket_id"]
    test("创建工单成功，处理人为发起人", status,
         check=data.get("current_handler") == "孙管理")

    api("POST", f"/api/exception/tickets/{ticket3d_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })

    status, data = api("POST", f"/api/exception/tickets/{ticket3d_id}/transfer", {
        "target_handler": "钱质控",
        "target_handler_role": "质控",
        "transfer_reason": "需要专业分析",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("转交责任人成功", status, check=data.get("to_handler") == "钱质控")

    status, data = api("GET", f"/api/exception/tickets/{ticket3d_id}")
    test("转交后处理人为钱质控", status,
         check=(data["current_handler"] == "钱质控"
                and data["current_handler_role"] == "质控"
                and len(data["transfer_history"]) == 1))

    status, data = api("POST", f"/api/exception/tickets/{ticket3d_id}/withdraw", {
        "role": "库房管理员", "operator": "孙管理",
        "reason": "需要重新整理材料",
    })
    test("发起人撤回成功", status, check=data.get("status") == "已撤回")

    status, data = api("POST", f"/api/exception/tickets/{ticket3d_id}/resubmit", {
        "role": "库房管理员", "operator": "孙管理",
        "description": "补充：已整理完整温度曲线数据",
        "reason": "材料已备齐",
    })
    test("重提成功，状态变回待处理", status, check=data.get("status") == "待处理")

    status, data = api("GET", f"/api/exception/tickets/{ticket3d_id}")
    test("重提后处理人重置为发起人，不残留上次转交人", status,
         check=(data["current_handler"] == "孙管理"
                and data["current_handler_role"] == "库房管理员"
                and data["resubmitted_by"] == "孙管理"))

    status, data = api("POST", f"/api/exception/tickets/{ticket3d_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    test("重提后可重新确认", status, check=data.get("status") == "处理中")

    status, audit = api("GET", f"/api/exception/tickets/{ticket3d_id}/audit")
    actions = [a["action"] for a in audit]
    test("审计日志包含完整流转链路", status,
         check=("创建处置单" in actions
                and "确认处置单" in actions
                and "转交责任人" in actions
                and "撤回处置单" in actions
                and "重新提交处置单" in actions
                and "确认处置单" in actions))

    # ==================================================================
    print()
    print("=== 场景4: 配置切换（代录开关） ===")
    print()

    print("  4.1 当前代录关闭，尝试代录 → 403")
    api("POST", "/api/exception/config", {
        "allow_proxy_record": False, "operator": "管理员A",
    })
    status, cfg = api("GET", "/api/exception/config")
    test("代录配置已关闭", status, check=cfg["allow_proxy_record"] is False)

    boxes4a, _, _ = create_full_batch("BATCH-EXC-004A", 2, "FB")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-004A",
        "box_code": "FB001",
        "reason_category": "其他",
        "role": "班组长", "operator": "周班长",
        "on_behalf_of": "王五",
    })
    test("代录关闭时代录被拦截", status, expected_status=403)
    print(f"    返回提示: {data.get('detail', '')[:80]}")

    print()
    print("  4.2 开启代录配置")
    status, data = api("POST", "/api/exception/config", {
        "allow_proxy_record": True, "operator": "管理员A",
    })
    test("代录配置开启成功", status, check=data.get("allow_proxy_record") is True)

    print()
    print("  4.3 班组长代录成功，字段正确分离")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-004A",
        "box_code": "FB001",
        "reason_category": "设备故障",
        "description": "班组长代录：冷库门密封胶条老化导致温度波动",
        "role": "班组长", "operator": "周班长",
        "on_behalf_of": "王五",
    })
    ticket4a_id = data["ticket_id"]
    test("代录创建成功，发起人=王五，代录人=周班长", status,
         check=(data.get("ok")
                and data.get("initiator") == "王五"
                and data.get("proxy_recorder") == "周班长"))

    status, data = api("GET", f"/api/exception/tickets/{ticket4a_id}")
    test("代录工单字段完整分离", status,
         check=(data["initiator"] == "王五"
                and data["proxy_recorder"] == "周班长"
                and data["proxy_recorder_role"] == "班组长"
                and data["current_handler"] == "王五"
                and data["allow_proxy_record_at_create"] is True))

    print()
    print("  4.4 切换配置为关闭，验证旧工单不受影响")
    api("POST", "/api/exception/config", {
        "allow_proxy_record": False, "operator": "管理员A",
    })
    status, data = api("GET", f"/api/exception/tickets/{ticket4a_id}")
    test("旧工单的 allow_proxy_record_at_create 仍为 True", status,
         check=data["allow_proxy_record_at_create"] is True)

    print()
    print("  4.5 新工单的 allow_proxy_record_at_create 为 False")
    boxes4b, _, _ = create_full_batch("BATCH-EXC-004B", 2, "FC")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-004B",
        "box_code": "FC001",
        "reason_category": "其他",
        "role": "库房管理员", "operator": "孙管理",
    })
    ticket4b_id = data["ticket_id"]
    status, data = api("GET", f"/api/exception/tickets/{ticket4b_id}")
    test("新工单 allow_proxy_record_at_create 为 False", status,
         check=data["allow_proxy_record_at_create"] is False)

    print()
    print("  4.6 代录工单的撤回权限属于发起人（被代理人）")
    api("POST", f"/api/exception/tickets/{ticket4a_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("POST", f"/api/exception/tickets/{ticket4a_id}/withdraw", {
        "role": "班组长", "operator": "周班长",
        "reason": "代录人试图撤回",
    })
    test("代录人（周班长）无权撤回", status, expected_status=403)

    status, data = api("POST", f"/api/exception/tickets/{ticket4a_id}/withdraw", {
        "role": "库房签收员", "operator": "王五",
        "reason": "发起人本人撤回",
    })
    test("发起人（王五）本人可以撤回", status, check=data.get("ok"))

    # ==================================================================
    print()
    print("=== 场景5: 重启恢复 ===")
    print()

    print("  5.1 记录重启前状态")
    api("POST", "/api/exception/config", {
        "allow_proxy_record": True, "operator": "管理员A",
    })
    boxes5, _, _ = create_full_batch("BATCH-EXC-005", 2, "FD")
    status, data = api("POST", "/api/exception/tickets", {
        "batch_no": "BATCH-EXC-005",
        "box_code": "FD001",
        "reason_category": "温度异常",
        "description": "重启测试工单",
        "role": "班组长", "operator": "周班长",
        "on_behalf_of": "王五",
    })
    ticket5_id = data["ticket_id"]
    api("POST", f"/api/exception/tickets/{ticket5_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    api("POST", f"/api/exception/tickets/{ticket5_id}/transfer", {
        "target_handler": "钱质控",
        "target_handler_role": "质控",
        "transfer_reason": "重启测试转交",
        "role": "仓库主管", "operator": "赵主管",
    })
    api("POST", f"/api/exception/tickets/{ticket5_id}/evidence", {
        "evidence_type": "text",
        "evidence_content": "重启前补充的证据",
        "role": "质控", "operator": "钱质控",
    })

    status, before_ticket = api("GET", f"/api/exception/tickets/{ticket5_id}")
    test("重启前工单查询成功", status,
         check=(before_ticket["status"] == "处理中"
                and before_ticket["initiator"] == "王五"
                and before_ticket["proxy_recorder"] == "周班长"
                and before_ticket["current_handler"] == "钱质控"
                and len(before_ticket["transfer_history"]) == 1
                and len(before_ticket["evidence_list"]) >= 2))
    print(f"    重启前: 状态={before_ticket['status']}, "
          f"发起人={before_ticket['initiator']}, "
          f"代录人={before_ticket['proxy_recorder']}, "
          f"处理人={before_ticket['current_handler']}")

    status, before_audit = api("GET", f"/api/exception/tickets/{ticket5_id}/audit")
    audit_count_before = len(before_audit)
    print(f"    审计记录数: {audit_count_before}")

    status, before_config = api("GET", "/api/exception/config")
    print(f"    配置: allow_proxy_record={before_config['allow_proxy_record']}")

    print()
    print("  5.2 重启服务...")
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
    print("  5.3 验证重启后数据完整")
    status, after_ticket = api("GET", f"/api/exception/tickets/{ticket5_id}")
    test("重启后工单字段完全一致", status,
         check=(after_ticket["status"] == before_ticket["status"]
                and after_ticket["initiator"] == before_ticket["initiator"]
                and after_ticket["proxy_recorder"] == before_ticket["proxy_recorder"]
                and after_ticket["current_handler"] == before_ticket["current_handler"]
                and after_ticket["current_handler_role"] == before_ticket["current_handler_role"]
                and len(after_ticket["transfer_history"]) == len(before_ticket["transfer_history"])
                and len(after_ticket["evidence_list"]) == len(before_ticket["evidence_list"])))

    status, after_audit = api("GET", f"/api/exception/tickets/{ticket5_id}/audit")
    test("重启后审计记录数一致", status,
         check=len(after_audit) == audit_count_before)

    status, after_config = api("GET", "/api/exception/config")
    test("重启后代录配置保留", status,
         check=after_config["allow_proxy_record"] == before_config["allow_proxy_record"])

    print()
    print("  5.4 重启后可继续操作")
    status, _ = api("POST", f"/api/exception/tickets/{ticket5_id}/evidence", {
        "evidence_type": "text",
        "evidence_content": "重启后补充证据",
        "role": "质控", "operator": "钱质控",
    })
    test("重启后可继续补充证据", status, check=True)

    status, data = api("POST", f"/api/exception/tickets/{ticket5_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "重启测试结案，确认数据持久化正常",
    })
    test("重启后结案成功", status, check=data.get("status") == "已结案")

    # ==================================================================
    print()
    print("=== 场景6: 导出核对 ===")
    print()

    print("  6.1 JSON 导出包含责任流转链")
    status, export = api("GET", "/api/exception/export/json")
    test("异常JSON导出成功", status,
         check=("generated_at" in export and "tickets" in export))

    total_export = len(export["tickets"])
    print(f"    导出工单数: {total_export}")

    ticket1_export = next((t for t in export["tickets"] if t["ticket_id"] == ticket1_id), None)
    test("导出包含工单1（已结案）", status, check=ticket1_export is not None)
    if ticket1_export:
        test("工单1导出字段完整分离", status,
             check=(ticket1_export["initiator"] == "王五"
                    and ticket1_export["proxy_recorder"] is None
                    and ticket1_export["current_handler"] == "钱质控"))
        test("工单1导出包含责任流转链", status,
             check=("responsibility_chain" in ticket1_export
                    and len(ticket1_export["responsibility_chain"]) >= 3))
        test("工单1导出包含转交历史", status,
             check=len(ticket1_export["transfer_history"]) >= 1)
        test("工单1导出包含证据列表", status,
             check=len(ticket1_export["evidence_list"]) >= 2)
        test("工单1导出包含审计日志", status,
             check=len(ticket1_export["audit_log"]) >= 5)

    ticket4a_export = next((t for t in export["tickets"] if t["ticket_id"] == ticket4a_id), None)
    test("导出包含代录工单4a", status, check=ticket4a_export is not None)
    if ticket4a_export:
        test("代录工单的代录人字段正确", status,
             check=(ticket4a_export["proxy_recorder"] == "周班长"
                    and ticket4a_export["initiator"] == "王五"))

    print()
    print("  6.2 按状态筛选导出")
    status, export_closed = api("GET", "/api/exception/export/json?status=已结案")
    test("已结案工单导出", status, check=len(export_closed["tickets"]) >= 1)

    print()
    print("  6.3 按批次筛选导出")
    status, export_batch = api("GET", "/api/exception/export/json?batch_no=BATCH-EXC-001")
    test("按批次导出成功", status, check=len(export_batch["tickets"]) >= 1)

    print()
    print("  6.4 按箱号筛选导出")
    status, export_box = api("GET", "/api/exception/export/json?box_code=EV001")
    test("按箱号导出成功", status, check=len(export_box["tickets"]) >= 1)

    print()
    print("  6.5 CSV 导出包含责任流转字段")
    status, csv_body = api("GET", "/api/exception/export/csv", raw=True)
    test("异常CSV导出成功", status,
         check=(isinstance(csv_body, str)
                and "ticket_no" in csv_body
                and "initiator" in csv_body
                and "proxy_recorder" in csv_body
                and "current_handler" in csv_body
                and "responsibility_transfers" in csv_body))
    lines = csv_body.strip().split("\n")
    header = lines[0]
    print(f"    CSV 表头: {header}")
    print(f"    CSV 行数(含表头): {len(lines)}")

    print()
    print("  6.6 批次异常汇总")
    status, summary = api("GET", "/api/exception/batches/BATCH-EXC-001/summary")
    test("批次异常汇总查询成功", status,
         check=(summary["batch_no"] == "BATCH-EXC-001"
                and summary["total_tickets"] >= 1
                and "by_status" in summary
                and "by_category" in summary
                and len(summary["tickets"]) >= 1))

    print()
    print("  6.7 工单列表查询")
    status, tickets = api("GET", "/api/exception/tickets")
    test("异常工单列表查询成功", status, check=len(tickets) >= 1)

    status, tickets_active = api("GET", "/api/exception/tickets?status=处理中")
    test("按状态查询工单", status, check=isinstance(tickets_active, list))

    status, tickets_batch = api("GET", "/api/exception/tickets?batch_no=BATCH-EXC-003")
    test("按批次查询工单", status, check=len(tickets_batch) >= 1)

    status, tickets_box = api("GET", "/api/exception/tickets?box_code=EV001")
    test("按箱号查询工单", status, check=len(tickets_box) >= 1)

    print()
    print("  6.8 导出审计日志与工单状态对齐")
    all_consistent = True
    for t in export["tickets"]:
        audit_log = t["audit_log"]
        if not audit_log:
            continue
        first_action = audit_log[0]
        if first_action["action"] != "创建处置单":
            all_consistent = False
            break
        if t["status"] == "已结案":
            has_close = any(a["action"] == "结案处置单" for a in audit_log)
            if not has_close:
                all_consistent = False
                break
    test("所有工单审计日志与状态一致", status, check=all_consistent)

    print()
    print("  6.9 CSV 责任流转字段可对账")
    found_transfer_line = False
    for line in lines[1:]:
        if "钱质控" in line and "王五" in line:
            found_transfer_line = True
            break
    test("CSV 中可找到转交责任的工单", status, check=found_transfer_line)

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
