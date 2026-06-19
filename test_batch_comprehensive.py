"""
批次签收功能全面回归测试
覆盖：部分签收、撤销后再签收、越权撤销、重启恢复、导出核对、重复到达拦截
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


def main():
    global passed, failed
    print("=" * 70)
    print("  批次签收功能全面回归测试")
    print("=" * 70)
    print()

    print("=== 阶段1: 初始化配置 ===")
    api("POST", "/api/thresholds", {
        "sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120
    })
    print("  阈值配置完成")
    print()

    print("=== 场景1: 部分签收 + 缺失登记，保留剩余待办 ===")
    print()

    print("  1.1 创建批次 BATCH-REG-001 (5箱)")
    status, data = api("POST", "/api/batches", {
        "batch_no": "BATCH-REG-001",
        "sample_type": "疫苗",
        "operator": "管理员A",
    })
    test("创建批次", status, check=data.get("ok"))

    status, data = api("POST", "/api/boxes/import/json", {
        "batch_no": "BATCH-REG-001",
        "boxes": [
            {"box_code": "R001", "sample_type": "疫苗", "current_temp": 4.0},
            {"box_code": "R002", "sample_type": "疫苗", "current_temp": 4.5},
            {"box_code": "R003", "sample_type": "疫苗", "current_temp": 3.5},
            {"box_code": "R004", "sample_type": "疫苗", "current_temp": 5.0},
            {"box_code": "R005", "sample_type": "疫苗", "current_temp": 4.2},
        ]
    })
    test("导入5箱", status, check=len(data.get("imported", [])) == 5)

    status, data = api("GET", "/api/batches/BATCH-REG-001")
    test("初始状态：5待办", status,
         check=data["batch"]["total_boxes"] == 5 and len(data["pending_todos"]) == 5)

    print()
    print("  1.2 批次出库 → 转运中 → 待签收")
    status, data = api("POST", "/api/batches/BATCH-REG-001/dispatch", {
        "role": "出库员", "operator": "张三", "current_temp": 4.0,
    })
    test("批次出库", status, check=data.get("to") == "转运中")

    status, data = api("POST", "/api/batches/BATCH-REG-001/arrive", {
        "role": "转运员", "operator": "李四", "current_temp": 5.0,
    })
    test("批次到达", status, check=data.get("to") == "待签收")

    print()
    print("  1.3 先签收2箱，登记2箱缺失，保留1箱待办")
    status, data = api("POST", "/api/batches/BATCH-REG-001/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": ["R001", "R002"],
        "missing_boxes": ["R003", "R004"],
        "missing_reason": "运输途中遗漏",
    })
    test("签收2箱+缺失2箱 → 部分签收", status,
         check=(data.get("to") == "部分签收"
                and data.get("received_count") == 2
                and data.get("missing_registered_count") == 2))

    status, data = api("GET", "/api/batches/BATCH-REG-001")
    test("验证：剩余1箱待办(R005)，批次状态部分签收", status,
         check=(data["batch"]["status"] == "部分签收"
                and data["batch"]["received_boxes"] == 2
                and data["batch"]["missing_boxes"] == 2
                and len(data["pending_todos"]) == 1
                and "R005" in data["pending_todos"]))
    print(f"    批次状态: {data['batch']['status']}, "
          f"已签收: {data['batch']['received_boxes']}, "
          f"缺失: {data['batch']['missing_boxes']}, "
          f"待办: {data['pending_todos']}")

    print()
    print("  1.4 缺失箱R003补到，先到达再签收")
    status, data = api("POST", "/api/batches/BATCH-REG-001/arrive", {
        "role": "转运员", "operator": "李四", "current_temp": 4.8,
    })
    test("补到箱子到达（智能跳过已处理）", status,
         check=data.get("to") == "部分签收" and data.get("skip_count", 0) >= 2)
    print(f"    成功: {data.get('success_count')}, 跳过: {data.get('skip_count')}, 失败: {data.get('fail_count')}")

    status, data = api("POST", "/api/batches/BATCH-REG-001/cancel_missing", {
        "role": "管理员",
        "operator": "管理员A",
        "reason": "箱子已找到并补运到达",
        "box_codes": ["R003"],
    })
    test("管理员撤销R003缺失登记", status,
         check=data.get("ok") and data.get("cancelled_count") == 1)

    status, data = api("GET", "/api/batches/BATCH-REG-001")
    test("验证：撤销后待办变为2箱(R003, R005)，已签收保持2箱", status,
         check=(data["batch"]["status"] == "部分签收"
                and data["batch"]["received_boxes"] == 2
                and data["batch"]["missing_boxes"] == 1
                and len(data["pending_todos"]) == 2
                and "R003" in data["pending_todos"]
                and "R005" in data["pending_todos"]))
    print(f"    批次状态: {data['batch']['status']}, "
          f"已签收: {data['batch']['received_boxes']}, "
          f"缺失: {data['batch']['missing_boxes']}, "
          f"待办: {data['pending_todos']}")

    status, data = api("POST", "/api/batches/BATCH-REG-001/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": ["R003"],
    })
    test("签收补到的R003", status,
         check=data.get("to") == "部分签收" and data.get("received_count") == 1)

    status, data = api("GET", "/api/batches/BATCH-REG-001")
    test("验证：已签收变为3箱，待办剩1箱(R005)", status,
         check=(data["batch"]["received_boxes"] == 3
                and len(data["pending_todos"]) == 1
                and "R005" in data["pending_todos"]))

    print()
    print("  1.5 最后1箱R005签收，R004仍缺失")
    status, data = api("POST", "/api/batches/BATCH-REG-001/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": ["R005"],
    })
    test("签收R005 → 仍部分签收（有缺失）", status,
         check=data.get("to") == "部分签收")

    status, data = api("GET", "/api/batches/BATCH-REG-001")
    test("验证：已签收4箱，缺失1箱，无待办", status,
         check=(data["batch"]["received_boxes"] == 4
                and data["batch"]["missing_boxes"] == 1
                and len(data["pending_todos"]) == 0))
    print(f"    批次状态: {data['batch']['status']}, "
          f"已签收: {data['batch']['received_boxes']}, "
          f"缺失: {data['batch']['missing_boxes']}, "
          f"待办: {data['pending_todos']}")

    print()
    print("  1.6 R004最终找到，撤销缺失并签收，批次完成")
    status, data = api("POST", "/api/batches/BATCH-REG-001/cancel_missing", {
        "role": "管理员",
        "operator": "管理员A",
        "reason": "箱子在仓库角落找到",
        "box_codes": ["R004"],
    })
    test("管理员撤销R004缺失登记", status,
         check=data.get("ok") and data.get("batch_status") == "部分签收")

    status, data = api("POST", "/api/batches/BATCH-REG-001/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": ["R004"],
    })
    test("签收R004 → 批次已签收", status,
         check=data.get("to") == "已签收")

    status, data = api("GET", "/api/batches/BATCH-REG-001")
    test("验证：全部5箱签收完成", status,
         check=(data["batch"]["status"] == "已签收"
                and data["batch"]["received_boxes"] == 5
                and data["batch"]["missing_boxes"] == 0
                and len(data["pending_todos"]) == 0))
    print(f"    批次状态: {data['batch']['status']}, "
          f"已签收: {data['batch']['received_boxes']}, "
          f"缺失: {data['batch']['missing_boxes']}")

    print()
    print("=== 场景2: 撤销缺失登记不回退已签收状态 ===")
    print()

    print("  2.1 创建批次 BATCH-REG-002 (4箱)")
    status, data = api("POST", "/api/batches", {
        "batch_no": "BATCH-REG-002",
        "sample_type": "疫苗",
        "operator": "管理员A",
    })
    test("创建批次", status, check=data.get("ok"))

    api("POST", "/api/boxes/import/json", {
        "batch_no": "BATCH-REG-002",
        "boxes": [
            {"box_code": "S001", "sample_type": "疫苗", "current_temp": 4.0},
            {"box_code": "S002", "sample_type": "疫苗", "current_temp": 4.5},
            {"box_code": "S003", "sample_type": "疫苗", "current_temp": 3.5},
            {"box_code": "S004", "sample_type": "疫苗", "current_temp": 5.0},
        ]
    })

    api("POST", "/api/batches/BATCH-REG-002/dispatch", {
        "role": "出库员", "operator": "张三", "current_temp": 4.0,
    })
    api("POST", "/api/batches/BATCH-REG-002/arrive", {
        "role": "转运员", "operator": "李四", "current_temp": 5.0,
    })

    print()
    print("  2.2 签收3箱，登记1箱缺失 → 部分签收")
    status, data = api("POST", "/api/batches/BATCH-REG-002/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": ["S001", "S002", "S003"],
        "missing_boxes": ["S004"],
        "missing_reason": "未找到",
    })
    test("签收3箱+缺失1箱", status,
         check=data.get("to") == "部分签收" and data.get("received_count") == 3)

    status, data = api("GET", "/api/batches/BATCH-REG-002")
    test("验证：已签收3箱，缺失1箱", status,
         check=(data["batch"]["received_boxes"] == 3
                and data["batch"]["missing_boxes"] == 1))

    print()
    print("  2.3 撤销缺失登记，验证已签收状态不回退")
    status, data = api("POST", "/api/batches/BATCH-REG-002/cancel_missing", {
        "role": "管理员",
        "operator": "管理员A",
        "reason": "箱子找到",
        "box_codes": ["S004"],
    })
    test("撤销缺失登记", status,
         check=data.get("ok") and data.get("batch_status") == "部分签收")

    status, data = api("GET", "/api/batches/BATCH-REG-002")
    test("验证：已签收保持3箱，状态仍为部分签收，待办1箱", status,
         check=(data["batch"]["status"] == "部分签收"
                and data["batch"]["received_boxes"] == 3
                and data["batch"]["missing_boxes"] == 0
                and len(data["pending_todos"]) == 1
                and "S004" in data["pending_todos"]))
    print(f"    批次状态: {data['batch']['status']}, "
          f"已签收: {data['batch']['received_boxes']}, "
          f"缺失: {data['batch']['missing_boxes']}, "
          f"待办: {data['pending_todos']}")

    print()
    print("=== 场景3: 越权操作拦截 ===")
    print()

    print("  3.1 出库员尝试撤销缺失登记 → 403")
    status, data = api("POST", "/api/batches/BATCH-REG-002/cancel_missing", {
        "role": "出库员",
        "operator": "张三",
        "reason": "越权尝试",
        "box_codes": ["S004"],
    })
    test("出库员越权撤销 → 403", status, expected_status=403)

    print("  3.2 转运员尝试签收 → 403")
    status, data = api("POST", "/api/batches/BATCH-REG-002/receive", {
        "role": "转运员",
        "operator": "李四",
        "received_boxes": ["S004"],
    })
    test("转运员越权签收 → 403", status, expected_status=403)

    print("  3.3 已签收箱子尝试重复签收 → 跳过不报错")
    status, data = api("POST", "/api/batches/BATCH-REG-002/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": ["S001", "S004"],
    })
    test("重复签收已签收箱子 → 智能跳过", status,
         check=data.get("received_count") == 1 and data.get("skip_count") == 1)
    print(f"    签收: {data.get('received_count')}, 跳过: {data.get('skip_count')}")

    status, data = api("GET", "/api/batches/BATCH-REG-002")
    test("验证：S001状态仍为已签收，S004签收完成", status,
         check=(data["batch"]["status"] == "已签收"
                and data["batch"]["received_boxes"] == 4))

    print()
    print("=== 场景4: 重复到达拦截（智能跳过） ===")
    print()

    print("  4.1 创建批次 BATCH-REG-003 (3箱)")
    status, data = api("POST", "/api/batches", {
        "batch_no": "BATCH-REG-003",
        "sample_type": "疫苗",
        "operator": "管理员A",
    })
    test("创建批次", status, check=data.get("ok"))

    api("POST", "/api/boxes/import/json", {
        "batch_no": "BATCH-REG-003",
        "boxes": [
            {"box_code": "T001", "sample_type": "疫苗", "current_temp": 4.0},
            {"box_code": "T002", "sample_type": "疫苗", "current_temp": 4.5},
            {"box_code": "T003", "sample_type": "疫苗", "current_temp": 3.5},
        ]
    })

    api("POST", "/api/batches/BATCH-REG-003/dispatch", {
        "role": "出库员", "operator": "张三", "current_temp": 4.0,
    })

    print()
    print("  4.2 第一次到达")
    status, data = api("POST", "/api/batches/BATCH-REG-003/arrive", {
        "role": "转运员", "operator": "李四", "current_temp": 5.0,
    })
    test("第一次到达", status,
         check=data.get("to") == "待签收" and data.get("success_count") == 3)

    print()
    print("  4.3 重复到达 → 全部跳过")
    status, data = api("POST", "/api/batches/BATCH-REG-003/arrive", {
        "role": "转运员", "operator": "李四", "current_temp": 5.0,
    })
    test("重复到达 → 全部跳过", status,
         check=data.get("skip_count") == 3 and data.get("success_count") == 0)
    print(f"    成功: {data.get('success_count')}, 跳过: {data.get('skip_count')}")

    print()
    print("  4.4 签收1箱后，再次到达 → 跳过已签收和已到达")
    api("POST", "/api/batches/BATCH-REG-003/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": ["T001"],
    })

    status, data = api("POST", "/api/batches/BATCH-REG-003/arrive", {
        "role": "转运员", "operator": "李四", "current_temp": 5.0,
    })
    test("部分签收后再次到达 → 智能跳过", status,
         check=data.get("skip_count") == 3 and data.get("to") == "部分签收")
    print(f"    成功: {data.get('success_count')}, 跳过: {data.get('skip_count')}, 批次状态: {data.get('to')}")

    print()
    print("=== 场景5: 导出汇总与操作日志核对 ===")
    print()

    print("  5.1 导出 BATCH-REG-001 汇总")
    status, data = api("GET", "/api/export/json?batch_no=BATCH-REG-001")
    test("导出成功", status,
         check=("batch_summary" in data
                and "batch_boxes" in data
                and "batch_audit_log" in data))

    summary = data["batch_summary"]
    test("导出汇总验证：5箱全部签收", status,
         check=(summary["total_boxes"] == 5
                and summary["received_boxes"] == 5
                and summary["missing_boxes"] == 0
                and summary["pending_boxes"] == 0
                and len(summary["pending_todos"]) == 0))
    print(f"    汇总: {summary['total_boxes']}箱, 已签收{summary['received_boxes']}, "
          f"缺失{summary['missing_boxes']}, 待办{summary['pending_boxes']}")

    test("导出包含箱子明细", status,
         check=len(data["batch_boxes"]) == 5)

    audit_count = len(data["batch_audit_log"])
    test(f"导出包含操作日志（{audit_count}条）", status,
         check=audit_count >= 10)
    print(f"    操作日志: {audit_count}条")

    print()
    print("  5.2 验证箱子明细中的缺失撤销记录")
    r004_info = next((b for b in data["batch_boxes"] if b["box_code"] == "R004"), None)
    test("R004包含缺失撤销信息", status,
         check=(r004_info is not None
                and r004_info["missing_cancelled_at"] is not None
                and r004_info["missing_cancelled_by"] == "管理员A"))
    print(f"    R004 缺失撤销时间: {r004_info['missing_cancelled_at']}, "
          f"撤销人: {r004_info['missing_cancelled_by']}, "
          f"撤销原因: {r004_info['missing_cancel_reason']}")

    print()
    print("=== 场景6: 服务重启后状态持久化验证 ===")
    print()

    print("  6.1 记录重启前状态")
    status, before = api("GET", "/api/batches/BATCH-REG-001")
    before_status = before["batch"]["status"]
    before_received = before["batch"]["received_boxes"]
    before_missing = before["batch"]["missing_boxes"]
    print(f"    重启前: 状态={before_status}, 已签收={before_received}, 缺失={before_missing}")

    status, before3 = api("GET", "/api/batches/BATCH-REG-003")
    before3_status = before3["batch"]["status"]
    before3_received = before3["batch"]["received_boxes"]
    print(f"    BATCH-REG-003 重启前: 状态={before3_status}, 已签收={before3_received}")

    print()
    print("  6.2 重启服务...")
    print("    停止服务...")

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
            print(f"    验证进程归属 (端口:8000, PID:{pid})...")
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
                    print(f"    命令行: {proc_info.get('CommandLine', '')}")
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
    print("  6.3 验证重启后状态")
    status, after = api("GET", "/api/batches/BATCH-REG-001")
    test("BATCH-REG-001 重启后状态一致", status,
         check=(after["batch"]["status"] == before_status
                and after["batch"]["received_boxes"] == before_received
                and after["batch"]["missing_boxes"] == before_missing))
    print(f"    重启后: 状态={after['batch']['status']}, "
          f"已签收={after['batch']['received_boxes']}, "
          f"缺失={after['batch']['missing_boxes']}")

    status, after3 = api("GET", "/api/batches/BATCH-REG-003")
    test("BATCH-REG-003 重启后状态一致", status,
         check=(after3["batch"]["status"] == before3_status
                and after3["batch"]["received_boxes"] == before3_received))
    print(f"    BATCH-REG-003 重启后: 状态={after3['batch']['status']}, "
          f"已签收={after3['batch']['received_boxes']}")

    print()
    print("  6.4 验证重启后仍可继续操作")
    status, data = api("POST", "/api/batches/BATCH-REG-003/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": ["T002", "T003"],
    })
    test("重启后可继续签收剩余箱子", status,
         check=data.get("to") == "已签收" and data.get("received_count") == 2)

    status, data = api("GET", "/api/batches/BATCH-REG-003")
    test("验证：BATCH-REG-003 全部签收完成", status,
         check=(data["batch"]["status"] == "已签收"
                and data["batch"]["received_boxes"] == 3))

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
