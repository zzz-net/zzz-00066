"""
交接复核功能全面回归测试
覆盖：正常归档(单人/双人)、越权拦截、撤销重开、补签冲突、重启恢复、导出核对
"""

import json
import sys
import urllib.request
import urllib.error
import time
import subprocess
import os

BASE = "http://localhost:8000"


def api(method, path, data=None):
    url = f"{BASE}{path}"
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


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
            status, _ = api("GET", "/api/thresholds")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def create_full_batch(batch_no, box_count=5, prefix="V"):
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
    print("  交接复核功能全面回归测试")
    print("=" * 70)
    print()

    print("=== 阶段0: 初始化配置 ===")
    api("POST", "/api/thresholds", {
        "sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120
    })
    print("  阈值配置完成")
    api("POST", "/api/review/config", {
        "require_double_review": False,
        "operator": "管理员A",
    })
    print("  复核配置: 单人复核")
    print()

    # ==================================================================
    print("=== 场景1: 单人复核 - 正常归档路径 ===")
    print()

    print("  1.1 创建并完成签收 BATCH-REV-001 (4箱)")
    boxes, _, _ = create_full_batch("BATCH-REV-001", 4, "A")
    status, data = api("GET", "/api/batches/BATCH-REV-001")
    test("批次状态为已签收", status,
         check=data["batch"]["status"] == "已签收" and data["batch"]["received_boxes"] == 4)
    print(f"    批次状态: {data['batch']['status']}, 已签收: {data['batch']['received_boxes']}")

    print()
    print("  1.2 仓库主管发起复核")
    status, data = api("POST", "/api/batches/BATCH-REV-001/review/initiate", {
        "role": "仓库主管",
        "operator": "赵主管",
        "handed_over_by": "王五",
    })
    test("发起复核成功", status, check=data.get("ok") and data.get("total_boxes") == 4)
    review_id_single = data["review_id"]
    print(f"    复核单ID: {review_id_single}, 配置: {'双人' if data['require_double_review'] else '单人'}复核")

    status, data = api("GET", "/api/batches/BATCH-REV-001")
    test("批次 review_status 变为复核中", status,
         check=data["batch"]["review_status"] == "复核中")

    print()
    print("  1.3 按箱复核 - 2箱通过, 1箱破损, 1箱温控待确认")
    status, data = api("POST", "/api/batches/BATCH-REV-001/review/boxes", {
        "role": "仓库主管",
        "operator": "赵主管",
        "reviews": [
            {"box_code": "A001", "result": "通过", "reason": "外观完好温度正常"},
            {"box_code": "A002", "result": "通过", "reason": "包装完好"},
            {"box_code": "A003", "result": "破损", "reason": "外包装有裂痕"},
            {"box_code": "A004", "result": "温控待确认", "reason": "记录仪显示短暂超标"},
        ],
    })
    test("4箱复核完成（含温控待确认）", status, check=data.get("processed") == 4)

    status, data = api("GET", "/api/batches/BATCH-REV-001/review")
    test("进度显示1箱温控待确认", status,
         check=(len(data["progress"]["pending_temp_confirmation"]) == 1
                and "A004" in data["progress"]["pending_temp_confirmation"]))
    print(f"    待一复: {data['progress']['pending_first_review']}, "
          f"温控待确认: {data['progress']['pending_temp_confirmation']}")

    print()
    print("  1.4 未完成复核尝试归档 → 409")
    status, data = api("POST", "/api/batches/BATCH-REV-001/archive", {
        "role": "仓库主管",
        "operator": "赵主管",
    })
    test("温控待确认时归档被拦截", status, expected_status=409)

    print()
    print("  1.5 修改温控待确认箱为通过，完成全部复核")
    status, data = api("POST", "/api/batches/BATCH-REV-001/review/boxes", {
        "role": "仓库主管",
        "operator": "赵主管",
        "reviews": [
            {"box_code": "A004", "result": "通过", "reason": "核实温度波动在允许范围内"},
        ],
    })
    test("A004改判通过（单人复核直接覆盖最终结果）", status, check=data.get("processed") == 1)

    status, data = api("GET", "/api/batches/BATCH-REV-001/review")
    test("进度显示全部完成", status, check=data["progress"]["all_reviewed"])

    print()
    print("  1.6 正式归档")
    status, data = api("POST", "/api/batches/BATCH-REV-001/archive", {
        "role": "仓库主管",
        "operator": "赵主管",
    })
    test("归档成功", status, check=data.get("ok"))

    status, data = api("GET", "/api/batches/BATCH-REV-001")
    test("批次状态变为已归档", status,
         check=(data["batch"]["status"] == "已归档"
                and data["batch"]["review_status"] == "已归档"
                and data["batch"]["archived_by"] == "赵主管"))
    print(f"    批次状态: {data['batch']['status']}, 归档人: {data['batch']['archived_by']}")

    # ==================================================================
    print()
    print("=== 场景2: 越权操作拦截 ===")
    print()

    print("  2.1 创建 BATCH-REV-002 并完成签收")
    create_full_batch("BATCH-REV-002", 3, "B")

    print()
    print("  2.2 库房签收员尝试发起复核 → 403")
    status, data = api("POST", "/api/batches/BATCH-REV-002/review/initiate", {
        "role": "库房签收员",
        "operator": "王五",
        "handed_over_by": "王五",
    })
    test("非主管发起复核被拦截", status, expected_status=403)

    print("  2.3 转运员尝试归档 → 403")
    status, data = api("POST", "/api/batches/BATCH-REV-002/archive", {
        "role": "转运员",
        "operator": "李四",
    })
    test("非主管归档被拦截", status, expected_status=403)

    print("  2.4 先由主管发起，再由库房签收员尝试撤销复核 → 403")
    api("POST", "/api/batches/BATCH-REV-002/review/initiate", {
        "role": "仓库主管", "operator": "赵主管", "handed_over_by": "王五",
    })
    status, data = api("POST", "/api/batches/BATCH-REV-002/review/cancel", {
        "role": "库房签收员",
        "operator": "王五",
        "reason": "越权撤销",
    })
    test("非主管撤销复核被拦截", status, expected_status=403)

    # ==================================================================
    print()
    print("=== 场景3: 撤销复核后重新发起 ===")
    print()

    print("  3.1 主管撤销 BATCH-REV-002 复核")
    status, data = api("POST", "/api/batches/BATCH-REV-002/review/cancel", {
        "role": "仓库主管",
        "operator": "赵主管",
        "reason": "发现有遗漏箱未处理",
    })
    test("撤销复核成功", status, check=data.get("ok"))

    status, data = api("GET", "/api/batches/BATCH-REV-002")
    test("批次 review_status 回到未开始", status,
         check=data["batch"]["review_status"] == "未开始")

    print()
    print("  3.2 重复发起 → 409（防并发）")
    api("POST", "/api/batches/BATCH-REV-002/review/initiate", {
        "role": "仓库主管", "operator": "赵主管", "handed_over_by": "王五",
    })
    status, data = api("POST", "/api/batches/BATCH-REV-002/review/initiate", {
        "role": "仓库主管", "operator": "赵主管", "handed_over_by": "王五",
    })
    test("重复发起复核被拦截", status, expected_status=409)

    # ==================================================================
    print()
    print("=== 场景4: 复核过程中补签新箱 - 冲突检测 ===")
    print()

    print("  4.1 创建 BATCH-REV-003，签收3箱，登记C004缺失 → 批次已签收")
    api("POST", "/api/batches", {
        "batch_no": "BATCH-REV-003", "sample_type": "疫苗", "operator": "管理员A",
    })
    api("POST", "/api/boxes/import/json", {
        "batch_no": "BATCH-REV-003",
        "boxes": [
            {"box_code": "C001", "sample_type": "疫苗", "current_temp": 4.0},
            {"box_code": "C002", "sample_type": "疫苗", "current_temp": 4.5},
            {"box_code": "C003", "sample_type": "疫苗", "current_temp": 3.5},
            {"box_code": "C004", "sample_type": "疫苗", "current_temp": 4.2},
        ],
    })
    api("POST", "/api/batches/BATCH-REV-003/dispatch", {
        "role": "出库员", "operator": "张三", "current_temp": 4.0,
    })
    api("POST", "/api/batches/BATCH-REV-003/arrive", {
        "role": "转运员", "operator": "李四", "current_temp": 5.0,
    })
    status, data = api("POST", "/api/batches/BATCH-REV-003/receive", {
        "role": "库房签收员", "operator": "王五",
        "received_boxes": ["C001", "C002", "C003"],
        "missing_boxes": ["C004"],
        "missing_reason": "运输途中暂时未找到",
    })
    test("签收3箱 + 登记C004缺失 → 已签收（所有箱子都处理完）", status,
         check=(data.get("to") == "已签收"
                and data.get("received_count") == 3
                and data.get("missing_registered_count") == 1))

    status, data = api("GET", "/api/batches/BATCH-REV-003")
    test("验证批次状态已签收", status,
         check=(data["batch"]["status"] == "已签收"
                and data["batch"]["received_boxes"] == 3
                and data["batch"]["missing_boxes"] == 1))

    print()
    print("  4.2 主管基于当前3箱发起复核（快照只有已签收的3箱）")
    status, data = api("POST", "/api/batches/BATCH-REV-003/review/initiate", {
        "role": "仓库主管", "operator": "赵主管", "handed_over_by": "王五",
    })
    test("发起复核成功（快照3箱）", status, check=data.get("total_boxes") == 3)
    print(f"    复核快照: {data.get('total_boxes')} 箱（C004是缺失状态，不纳入快照）")

    print()
    print("  4.3 复核过程中，C004找到了 → 撤销缺失并补签")
    status, data = api("POST", "/api/batches/BATCH-REV-003/cancel_missing", {
        "role": "管理员",
        "operator": "管理员A",
        "reason": "箱子在仓库角落找到",
        "box_codes": ["C004"],
    })
    test("管理员撤销C004缺失登记", status, check=data.get("ok"))

    status, data = api("POST", "/api/batches/BATCH-REV-003/receive", {
        "role": "库房签收员", "operator": "王五",
        "received_boxes": ["C004"],
    })
    test("补签C004成功", status, check=data.get("received_count") == 1)

    status, data = api("GET", "/api/batches/BATCH-REV-003")
    test("验证：4箱全部签收完成", status,
         check=(data["batch"]["status"] == "已签收"
                and data["batch"]["received_boxes"] == 4))

    print()
    print("  4.4 继续复核时检测到补签冲突 → 409（快照3箱 vs 实际已签收4箱）")
    status, data = api("POST", "/api/batches/BATCH-REV-003/review/boxes", {
        "role": "仓库主管",
        "operator": "赵主管",
        "reviews": [
            {"box_code": "C001", "result": "通过", "reason": "正常"},
        ],
    })
    test("复核检测到补签冲突被拦截", status, expected_status=409)
    print(f"    返回提示: {data.get('detail', '')[:150]}")

    print()
    print("  4.5 撤销复核并重新发起，快照变为4箱")
    api("POST", "/api/batches/BATCH-REV-003/review/cancel", {
        "role": "仓库主管", "operator": "赵主管",
        "reason": "补签了新箱C004，需重开",
    })
    status, data = api("POST", "/api/batches/BATCH-REV-003/review/initiate", {
        "role": "仓库主管", "operator": "赵主管", "handed_over_by": "王五",
    })
    test("重新发起复核 - 快照包含4箱", status, check=data.get("total_boxes") == 4)
    print(f"    重开后快照: {data.get('total_boxes')} 箱")

    # ==================================================================
    print()
    print("=== 场景5: 双人复核 + 重启持久化 ===")
    print()

    print("  5.1 切换为双人复核配置")
    status, data = api("POST", "/api/review/config", {
        "require_double_review": True,
        "operator": "管理员A",
    })
    test("双人复核配置生效", status, check=data.get("require_double_review"))

    print()
    print("  5.2 创建 BATCH-REV-004 并完成签收 (3箱)")
    create_full_batch("BATCH-REV-004", 3, "D")

    print()
    print("  5.3 发起双人复核并完成第一复核")
    status, data = api("POST", "/api/batches/BATCH-REV-004/review/initiate", {
        "role": "仓库主管", "operator": "赵主管", "handed_over_by": "王五",
    })
    review_id_double = data["review_id"]
    test("双人复核发起成功", status, check=data.get("require_double_review"))

    status, data = api("POST", "/api/batches/BATCH-REV-004/review/boxes", {
        "role": "仓库主管",
        "operator": "赵主管",
        "reviews": [
            {"box_code": "D001", "result": "通过", "reason": "一复正常"},
            {"box_code": "D002", "result": "通过", "reason": "一复正常"},
        ],
    })
    test("第一复核完成2箱", status, check=data.get("processed") == 2)

    print()
    print("  5.4 同一人尝试二复 → 409（双人复核必须换人）")
    status, data = api("POST", "/api/batches/BATCH-REV-004/review/boxes", {
        "role": "仓库主管",
        "operator": "赵主管",
        "reviews": [
            {"box_code": "D001", "result": "通过", "reason": "重复人尝试"},
        ],
    })
    test("同一人二次复核被拦截", status, expected_status=409)

    print()
    print("  5.5 记录重启前的复核状态")
    status, before = api("GET", "/api/batches/BATCH-REV-004/review")
    test("重启前状态查询成功", status,
         check=(before["progress"]["first_review_done"] == 2
                and len(before["progress"]["pending_first_review"]) == 1))
    print(f"    重启前: 一复完成={before['progress']['first_review_done']}, "
          f"待一复={before['progress']['pending_first_review']}")

    print()
    print("  5.6 重启服务...")
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
    print("  5.7 验证重启后复核状态完整保留")
    status, after = api("GET", "/api/batches/BATCH-REV-004/review")
    test("重启后复核状态一致", status,
         check=(after["progress"]["first_review_done"] == before["progress"]["first_review_done"]
                and after["progress"]["pending_first_review"] == before["progress"]["pending_first_review"]))
    print(f"    重启后: 一复完成={after['progress']['first_review_done']}, "
          f"待一复={after['progress']['pending_first_review']}")

    test("重启后双人复核配置保留", status,
         check=after["active_review"]["require_double_review"] is True)

    print()
    print("  5.8 换人完成剩余一复 + 二复")
    status, data = api("POST", "/api/batches/BATCH-REV-004/review/boxes", {
        "role": "仓库主管",
        "operator": "钱主管",
        "reviews": [
            {"box_code": "D003", "result": "通过", "reason": "一复正常"},
        ],
    })
    test("钱主管完成D003的一复", status, check=data.get("processed") == 1)

    status, data = api("POST", "/api/batches/BATCH-REV-004/review/boxes", {
        "role": "仓库主管",
        "operator": "钱主管",
        "reviews": [
            {"box_code": "D001", "result": "通过", "reason": "二复一致"},
            {"box_code": "D002", "result": "通过", "reason": "二复一致"},
        ],
    })
    test("钱主管完成D001、D002的二复（非一复人）", status,
         check=data.get("processed") == 2)

    status, data = api("POST", "/api/batches/BATCH-REV-004/review/boxes", {
        "role": "仓库主管",
        "operator": "孙主管",
        "reviews": [
            {"box_code": "D003", "result": "通过", "reason": "二复一致"},
        ],
    })
    test("孙主管完成D003的二复（D003一复人是钱主管）", status,
         check=data.get("processed") == 1)

    status, data = api("GET", "/api/batches/BATCH-REV-004/review")
    test("双人复核全部完成", status, check=data["progress"]["all_reviewed"])
    print(f"    一复完成: {data['progress']['first_review_done']}, "
          f"二复完成: {data['progress']['second_review_done']}, "
          f"待温控确认: {data['progress']['pending_temp_confirmation']}")

    print()
    print("  5.9 双人复核完成后归档")
    status, data = api("POST", "/api/batches/BATCH-REV-004/archive", {
        "role": "仓库主管",
        "operator": "赵主管",
    })
    test("双人复核完成后归档成功", status, check=data.get("ok"))

    # ==================================================================
    print()
    print("=== 场景6: 导出核对（汇总/箱明细/日志一致） ===")
    print()

    print("  6.1 导出已归档的 BATCH-REV-001 JSON")
    status, data = api("GET", "/api/export/json?batch_no=BATCH-REV-001")
    test("导出成功", status,
         check=("batch_summary" in data and "current_review" in data))

    summary = data["batch_summary"]
    test("导出汇总: 状态已归档", status,
         check=(summary["status"] == "已归档"
                and summary["review_status"] == "已归档"
                and summary["archived_by"] == "赵主管"))
    print(f"    汇总: 状态={summary['status']}, 复核状态={summary['review_status']}, "
          f"归档人={summary['archived_by']}")

    test("导出包含复核信息", status,
         check=("current_review" in data and data["current_review"]["status"] == "已完成"))
    print(f"    复核单: ID={data['current_review']['review_id']}, "
          f"状态={data['current_review']['status']}, "
          f"完成时间={data['current_review'].get('completed_at', 'N/A')}")

    review_boxes = data["current_review"]["boxes"]
    test("导出复核箱明细: 4箱全部有最终结果", status,
         check=len(review_boxes) == 4 and all(b["final_result"] for b in review_boxes))
    for rb in review_boxes:
        print(f"      {rb['box_code']}: 一复={rb['first_review_result']}({rb['first_reviewer']}), "
              f"最终={rb['final_result']}")

    audit_count = len(data["batch_audit_log"])
    audit_actions = [a["action"] for a in data["batch_audit_log"]]
    test(f"导出操作日志: {audit_count}条，包含复核相关操作", status,
         check=("发起交接复核" in audit_actions
                and "交接复核" in audit_actions
                and "批次归档" in audit_actions))
    print(f"    操作日志共 {audit_count} 条")
    print(f"    包含动作: 发起交接复核={'是' if '发起交接复核' in audit_actions else '否'}, "
          f"交接复核={'是' if '交接复核' in audit_actions else '否'}, "
          f"批次归档={'是' if '批次归档' in audit_actions else '否'}")

    print()
    print("  6.2 验证 BATCH-REV-004 双人复核导出明细")
    status, data = api("GET", "/api/export/json?batch_no=BATCH-REV-004")
    review_boxes_4 = data["current_review"]["boxes"]
    test("双人复核箱明细包含两次复核人信息", status,
         check=all(b["first_reviewer"] and b["second_reviewer"] for b in review_boxes_4))
    for rb in review_boxes_4:
        print(f"      {rb['box_code']}: 一复={rb['first_reviewer']}({rb['first_review_result']}), "
              f"二复={rb['second_reviewer']}({rb['second_review_result']})")

    print()
    print("  6.3 配置变更只影响新复核单（已开的不受影响）")
    api("POST", "/api/review/config", {
        "require_double_review": False, "operator": "管理员A",
    })
    status, data = api("GET", "/api/batches/BATCH-REV-004/review")
    test("已开的BATCH-REV-004复核单仍保持双人配置", status,
         check=data["active_review"]["require_double_review"] is True)
    print(f"    已开复核单配置: {'双人' if data['active_review']['require_double_review'] else '单人'} (不变)")
    print(f"    当前全局配置已改为单人，新开复核单会使用单人模式")

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
