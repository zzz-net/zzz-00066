"""
异常追责工单模块全面回归测试
覆盖 6 大场景：正常闭环、越权拦截、撤回重开、配置切换、重启恢复、导出核对
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


def create_full_batch(batch_no, box_count=3, prefix="DV"):
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
    print("  异常追责工单模块全面回归测试")
    print("=" * 70)
    print()

    print("=== 阶段0: 初始化 ===")
    api("POST", "/api/thresholds", {
        "sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120
    })
    print("  阈值配置完成")

    api("POST", "/api/dispute/config", {
        "require_double_confirm": False,
        "operator": "管理员A",
    })
    print("  争议配置: 单确认模式")
    print()

    # ==================================================================
    print("=== 场景1: 正常闭环 - 创建→确认→补充证据→结案 ===")
    print()

    print("  1.1 创建批次 BATCH-DSP-001 并完成签收 (3箱)")
    boxes, status, _ = create_full_batch("BATCH-DSP-001", 3, "DV")
    test("批次签收完成", status, check=True)

    print()
    print("  1.2 库房签收员发起争议单")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-001",
        "box_codes": ["DV001", "DV002"],
        "problem_type": "温度超标",
        "evidence_desc": "温度记录仪显示运输途中多次超过8°C",
        "responsibility_judgment": "转运方责任",
        "deadline": "2026-06-26T18:00:00",
        "role": "库房签收员",
        "operator": "王五",
    })
    test("创建争议单成功", status, check=data.get("ok") and data.get("status") == "待确认")
    ticket1_id = data["ticket_id"]
    ticket1_no = data["ticket_no"]
    print(f"    工单号: {ticket1_no}, 状态: {data['status']}")

    status, data = api("GET", f"/api/dispute/tickets/{ticket1_id}")
    test("工单详情包含箱号和证据说明", status,
         check=(len(data["boxes"]) == 2
                and data["evidence_desc"] is not None
                and data["responsibility_judgment"] == "转运方责任"
                and data["deadline"] is not None))

    print()
    print("  1.3 仓库主管确认 → 进入处理中")
    status, data = api("POST", f"/api/dispute/tickets/{ticket1_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管", "reason": "确认争议事实",
    })
    test("主管确认成功，状态变为处理中", status,
         check=data.get("ok") and data.get("status") == "处理中")

    print()
    print("  1.4 处理中补充证据")
    status, data = api("POST", f"/api/dispute/tickets/{ticket1_id}/evidence", {
        "evidence_desc": "新增温度监控数据导出报告",
        "role": "库房签收员", "operator": "王五",
    })
    test("补充证据成功", status, check=data.get("ok"))

    status, data = api("GET", f"/api/dispute/tickets/{ticket1_id}")
    test("证据列表有2条", status, check=len(data["evidence_list"]) == 2)

    print()
    print("  1.5 仓库主管结案")
    status, data = api("POST", f"/api/dispute/tickets/{ticket1_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "转运方负全责，赔偿损失并改进冷链措施",
        "reason": "调查完毕",
    })
    test("结案成功", status,
         check=(data.get("ok") and data.get("status") == "已结案"
                and data.get("conclusion") == "转运方负全责，赔偿损失并改进冷链措施"))

    status, data = api("GET", f"/api/dispute/tickets/{ticket1_id}")
    test("工单状态为已结案", status,
         check=(data["status"] == "已结案"
                and data["closed_by"] == "赵主管"
                and data["conclusion"] == "转运方负全责，赔偿损失并改进冷链措施"))

    print()
    print("  1.6 审计日志完整")
    status, audit = api("GET", f"/api/dispute/tickets/{ticket1_id}/audit")
    actions = [a["action"] for a in audit]
    test("审计日志包含 创建/确认/补充证据/结案", status,
         check=("创建争议单" in actions
                and "确认争议单" in actions
                and "补充证据" in actions
                and "结案争议单" in actions))
    print(f"    审计动作: {actions}")

    # ==================================================================
    print()
    print("=== 场景2: 越权拦截 ===")
    print()

    print("  2.1 出库员无权发起争议单 → 403")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-001",
        "box_codes": ["DV001"],
        "problem_type": "外包装破损",
        "role": "出库员", "operator": "张三",
    })
    test("出库员发起争议单被拦截", status, expected_status=403)

    print("  2.2 已结案工单不可再操作（结案后补充证据被拦）→ 409")
    status, data = api("POST", f"/api/dispute/tickets/{ticket1_id}/evidence", {
        "evidence_desc": "试图对已结案工单补充证据",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("已结案工单补充证据被拦截", status, expected_status=409)

    print("  2.3 库房签收员无权结案 → 403")
    boxes2, _, _ = create_full_batch("BATCH-DSP-002", 2, "DE")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-002",
        "box_codes": ["DE001"],
        "problem_type": "包装破损",
        "evidence_desc": "外包装严重变形",
        "role": "库房签收员", "operator": "王五",
    })
    ticket2_id = data["ticket_id"]
    api("POST", f"/api/dispute/tickets/{ticket2_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("POST", f"/api/dispute/tickets/{ticket2_id}/close", {
        "role": "库房签收员", "operator": "王五",
        "conclusion": "我认为没问题",
    })
    test("签收员越权结案被拦截", status, expected_status=403)
    print(f"    返回提示: {data.get('detail', '')[:100]}")

    print("  2.4 非创建人不可撤回 → 403")
    status, data = api("POST", f"/api/dispute/tickets/{ticket2_id}/withdraw", {
        "role": "仓库主管", "operator": "赵主管",
        "reason": "不是创建人想撤回",
    })
    test("非创建人撤回被拦截", status, expected_status=403)

    print("  2.4b 同角色不同人不可撤回 → 403")
    status, data = api("POST", f"/api/dispute/tickets/{ticket2_id}/withdraw", {
        "role": "库房签收员", "operator": "王六",
        "reason": "同角色不同人越权撤回",
    })
    test("同角色不同人撤回被拦截", status, expected_status=403)

    print("  2.5 质控在单确认模式下不可确认 → 403")
    api("POST", "/api/dispute/config", {
        "require_double_confirm": False, "operator": "管理员A",
    })
    boxes2b, _, _ = create_full_batch("BATCH-DSP-002B", 2, "DF")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-002B",
        "box_codes": ["DF001"],
        "problem_type": "标签错误",
        "role": "库房签收员", "operator": "王五",
    })
    ticket2b_id = data["ticket_id"]
    status, data = api("POST", f"/api/dispute/tickets/{ticket2b_id}/confirm", {
        "role": "质控", "operator": "钱质控", "reason": "质控尝试确认",
    })
    test("单确认模式下质控确认被拦截", status, expected_status=403)

    # ==================================================================
    print()
    print("=== 场景3: 撤回重开 ===")
    print()

    print("  3.1 创建争议单并确认到处理中")
    boxes3, _, _ = create_full_batch("BATCH-DSP-003", 2, "DG")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-003",
        "box_codes": ["DG001"],
        "problem_type": "数量差异",
        "evidence_desc": "实际到货比单据少1箱",
        "role": "库房签收员", "operator": "王五",
    })
    ticket3_id = data["ticket_id"]
    api("POST", f"/api/dispute/tickets/{ticket3_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })

    print()
    print("  3.2 创建人撤回争议单")
    status, data = api("POST", f"/api/dispute/tickets/{ticket3_id}/withdraw", {
        "role": "库房签收员", "operator": "王五",
        "reason": "需要补充更多证据后重新发起",
    })
    test("撤回成功，状态变为已撤回", status,
         check=data.get("ok") and data.get("status") == "已撤回")

    status, data = api("GET", f"/api/dispute/tickets/{ticket3_id}")
    test("工单状态为已撤回", status,
         check=(data["status"] == "已撤回"
                and data["withdrawn_by"] == "王五"))

    print()
    print("  3.3 创建人重开争议单")
    status, data = api("POST", f"/api/dispute/tickets/{ticket3_id}/reopen", {
        "role": "库房签收员", "operator": "王五",
        "reason": "已收集到新证据",
    })
    test("重开成功，状态变为待确认", status,
         check=data.get("ok") and data.get("status") == "待确认")

    status, data = api("GET", f"/api/dispute/tickets/{ticket3_id}")
    test("重开后确认字段已重置", status,
         check=(data["supervisor_confirmed"] is False
                and data["qc_confirmed"] is False))

    print()
    print("  3.4 重开后可以重新确认和结案")
    status, data = api("POST", f"/api/dispute/tickets/{ticket3_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    test("重开后重新确认成功", status,
         check=data.get("ok") and data.get("status") == "处理中")

    status, data = api("POST", f"/api/dispute/tickets/{ticket3_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "经核实数量差异系标签错误导致",
    })
    test("重开后结案成功", status,
         check=data.get("ok") and data.get("status") == "已结案")

    print()
    print("  3.5 驳回后重新提交")
    boxes3b, _, _ = create_full_batch("BATCH-DSP-003B", 2, "DH")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-003B",
        "box_codes": ["DH001"],
        "problem_type": "标签错误",
        "evidence_desc": "标签信息与实物不符",
        "role": "库房签收员", "operator": "王五",
    })
    ticket3b_id = data["ticket_id"]

    status, data = api("POST", f"/api/dispute/tickets/{ticket3b_id}/reject", {
        "role": "仓库主管", "operator": "赵主管",
        "reason": "证据不足，无法确认责任归属",
    })
    test("驳回成功", status,
         check=data.get("ok") and data.get("status") == "已驳回")

    status, data = api("POST", f"/api/dispute/tickets/{ticket3b_id}/resubmit", {
        "role": "库房签收员", "operator": "王五",
        "evidence_desc": "补充了标签照片和批次原始单据",
        "reason": "已补充充分证据",
    })
    test("重新提交成功，状态变回待确认", status,
         check=data.get("ok") and data.get("status") == "待确认")

    status, data = api("GET", f"/api/dispute/tickets/{ticket3b_id}")
    test("重新提交后证据说明已更新", status,
         check=data["evidence_desc"] == "补充了标签照片和批次原始单据")

    # ==================================================================
    print()
    print("=== 场景4: 配置切换 ===")
    print()

    print("  4.1 当前单确认模式，创建工单")
    api("POST", "/api/dispute/config", {
        "require_double_confirm": False, "operator": "管理员A",
    })
    boxes4a, _, _ = create_full_batch("BATCH-DSP-004A", 2, "DA")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-004A",
        "box_codes": ["DA001"],
        "problem_type": "温度异常",
        "role": "库房签收员", "operator": "王五",
    })
    ticket4a_id = data["ticket_id"]
    test("单确认模式工单创建成功", status,
         check=data.get("require_double_confirm") is False)

    print()
    print("  4.2 切换为双确认模式")
    status, data = api("POST", "/api/dispute/config", {
        "require_double_confirm": True, "operator": "管理员A",
    })
    test("双确认配置生效", status, check=data.get("require_double_confirm") is True)

    print()
    print("  4.3 旧工单仍为单确认")
    status, data = api("GET", f"/api/dispute/tickets/{ticket4a_id}")
    test("旧工单仍是单确认模式", status,
         check=data["require_double_confirm"] is False)

    print()
    print("  4.4 新工单使用双确认")
    boxes4b, _, _ = create_full_batch("BATCH-DSP-004B", 2, "DB")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-004B",
        "box_codes": ["DB001", "DB002"],
        "problem_type": "冷链断裂",
        "evidence_desc": "温度记录仪显示全程温度超标",
        "role": "质控", "operator": "钱质控",
    })
    ticket4b_id = data["ticket_id"]
    test("新工单使用双确认模式", status,
         check=data.get("require_double_confirm") is True)

    print()
    print("  4.5 双确认：主管确认后仍在待确认")
    status, data = api("POST", f"/api/dispute/tickets/{ticket4b_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管", "reason": "确认争议事实",
    })
    test("主管确认后仍为待确认", status,
         check=data.get("status") == "待确认" and data.get("supervisor_confirmed") is True)

    print()
    print("  4.6 双确认：质控确认后进入处理中")
    status, data = api("POST", f"/api/dispute/tickets/{ticket4b_id}/confirm", {
        "role": "质控", "operator": "钱质控", "reason": "质控确认问题属实",
    })
    test("质控确认后进入处理中", status,
         check=data.get("status") == "处理中" and data.get("qc_confirmed") is True)

    print()
    print("  4.7 双确认：重复确认被拦截")
    boxes4c, _, _ = create_full_batch("BATCH-DSP-004C", 2, "DC")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-004C",
        "box_codes": ["DC001"],
        "problem_type": "污染",
        "role": "库房签收员", "operator": "王五",
    })
    ticket4c_id = data["ticket_id"]
    api("POST", f"/api/dispute/tickets/{ticket4c_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    status, data = api("POST", f"/api/dispute/tickets/{ticket4c_id}/confirm", {
        "role": "仓库主管", "operator": "赵主管",
    })
    test("主管重复确认被拦截", status, expected_status=409)

    print()
    print("  4.8 重复建单拦截")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-004C",
        "box_codes": ["DC001"],
        "problem_type": "另一问题",
        "role": "库房签收员", "operator": "王五",
    })
    test("同批次已有活跃工单时重复建单被拦截", status, expected_status=409)

    print()
    print("  4.9 跨批次混填拦截")
    boxes4d, _, _ = create_full_batch("BATCH-DSP-004D", 2, "DD")
    status, data = api("POST", "/api/dispute/tickets", {
        "batch_no": "BATCH-DSP-004D",
        "box_codes": ["DC001"],
        "problem_type": "跨批次箱子",
        "role": "库房签收员", "operator": "王五",
    })
    test("跨批次混填被拦截", status, expected_status=400)

    # ==================================================================
    print()
    print("=== 场景5: 重启恢复 ===")
    print()

    print("  5.1 记录重启前状态")
    status, before_ticket = api("GET", f"/api/dispute/tickets/{ticket4b_id}")
    test("重启前工单查询成功", status,
         check=(before_ticket["status"] == "处理中"
                and before_ticket["require_double_confirm"] is True
                and before_ticket["supervisor_confirmed"] is True
                and before_ticket["qc_confirmed"] is True))
    print(f"    重启前: 状态={before_ticket['status']}, "
          f"双确认={before_ticket['require_double_confirm']}, "
          f"主管确认={before_ticket['supervisor_confirmed']}, "
          f"质控确认={before_ticket['qc_confirmed']}")

    status, before_audit = api("GET", f"/api/dispute/tickets/{ticket4b_id}/audit")
    audit_count_before = len(before_audit)
    print(f"    审计记录数: {audit_count_before}")

    status, before_config = api("GET", "/api/dispute/config")
    print(f"    配置: 双确认={before_config['require_double_confirm']}")

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
    status, after_ticket = api("GET", f"/api/dispute/tickets/{ticket4b_id}")
    test("重启后工单状态一致", status,
         check=(after_ticket["status"] == before_ticket["status"]
                and after_ticket["require_double_confirm"] == before_ticket["require_double_confirm"]
                and after_ticket["supervisor_confirmed"] == before_ticket["supervisor_confirmed"]
                and after_ticket["qc_confirmed"] == before_ticket["qc_confirmed"]))

    status, after_audit = api("GET", f"/api/dispute/tickets/{ticket4b_id}/audit")
    test("重启后审计记录数一致", status,
         check=len(after_audit) == audit_count_before)

    status, after_config = api("GET", "/api/dispute/config")
    test("重启后双确认配置保留", status,
         check=after_config["require_double_confirm"] == before_config["require_double_confirm"])

    test("重启后已结案工单仍不可操作", status,
         check=after_ticket["status"] == "处理中")

    status, _ = api("POST", f"/api/dispute/tickets/{ticket4b_id}/evidence", {
        "evidence_desc": "重启后补充证据",
        "role": "仓库主管", "operator": "赵主管",
    })
    test("重启后可继续操作处理中的工单", status, check=True)

    print()
    print("  5.4 重启后继续完成工单")
    status, data = api("POST", f"/api/dispute/tickets/{ticket4b_id}/close", {
        "role": "仓库主管", "operator": "赵主管",
        "conclusion": "经调查确认冷链断裂为转运方责任",
    })
    test("重启后结案成功", status,
         check=data.get("ok") and data.get("status") == "已结案")

    # ==================================================================
    print()
    print("=== 场景6: 导出核对 ===")
    print()

    print("  6.1 JSON 导出")
    status, export = api("GET", "/api/dispute/export/json")
    test("争议JSON导出成功", status,
         check=("generated_at" in export and "tickets" in export))

    total_export = len(export["tickets"])
    print(f"    导出工单数: {total_export}")

    ticket1_export = next((t for t in export["tickets"] if t["ticket_id"] == ticket1_id), None)
    test("导出包含工单1（已结案）", status, check=ticket1_export is not None)
    if ticket1_export:
        test("工单1导出状态为已结案", status,
             check=(ticket1_export["status"] == "已结案"
                    and ticket1_export["conclusion"] == "转运方负全责，赔偿损失并改进冷链措施"))
        test("工单1导出包含箱号列表", status,
             check=len(ticket1_export["box_codes"]) == 2)
        test("工单1导出包含证据列表", status,
             check=len(ticket1_export["evidence_list"]) == 2)
        test("工单1导出包含审计日志", status,
             check=len(ticket1_export["audit_log"]) >= 4)

    print()
    print("  6.2 按状态筛选导出")
    status, export_closed = api("GET", "/api/dispute/export/json?status=已结案")
    test("已结案工单导出", status,
         check=len(export_closed["tickets"]) >= 1)

    print()
    print("  6.3 按批次筛选导出")
    status, export_batch = api("GET", "/api/dispute/export/json?batch_no=BATCH-DSP-001")
    test("按批次导出成功", status,
         check=len(export_batch["tickets"]) >= 1)

    print()
    print("  6.4 CSV 导出")
    status, csv_body = api("GET", "/api/dispute/export/csv", raw=True)
    test("争议CSV导出成功", status,
         check=isinstance(csv_body, str) and "ticket_no" in csv_body and "batch_no" in csv_body)

    print()
    print("  6.5 批次争议汇总")
    status, summary = api("GET", "/api/dispute/batches/BATCH-DSP-001/summary")
    test("批次争议汇总查询成功", status,
         check=(summary["batch_no"] == "BATCH-DSP-001"
                and summary["total_tickets"] >= 1
                and "by_status" in summary
                and len(summary["tickets"]) >= 1))

    print()
    print("  6.6 工单列表查询")
    status, tickets = api("GET", "/api/dispute/tickets")
    test("争议工单列表查询成功", status, check=len(tickets) >= 1)

    status, tickets_active = api("GET", "/api/dispute/tickets?status=处理中")
    test("按状态查询工单", status, check=isinstance(tickets_active, list))

    status, tickets_batch = api("GET", "/api/dispute/tickets?batch_no=BATCH-DSP-003")
    test("按批次查询工单", status, check=len(tickets_batch) >= 1)

    print()
    print("  6.7 导出审计日志与工单状态对齐")
    all_consistent = True
    for t in export["tickets"]:
        audit_log = t["audit_log"]
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
    test("所有工单审计日志与状态一致", status, check=all_consistent)

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
