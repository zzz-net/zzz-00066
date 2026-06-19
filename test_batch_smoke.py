"""
批次功能冒烟测试
"""

import json
import sys
import urllib.request
import urllib.error

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
        print(f"  [FAIL] {name} -> HTTP {status}")


def main():
    print("--- 批次功能冒烟测试 ---")
    print()

    print("1. 准备：配置阈值")
    api("POST", "/api/thresholds", {
        "sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120
    })
    api("POST", "/api/thresholds", {
        "sample_type": "血液制品", "temp_min": 2, "temp_max": 6, "timeout_minutes": 60
    })

    print()
    print("2. 批次创建 & 导入")

    status, data = api("POST", "/api/batches", {
        "batch_no": "BATCH-001",
        "sample_type": "疫苗",
        "scheduled_outbound_time": "2026-06-20T09:00:00",
        "estimated_arrival_deadline": "2026-06-20T15:00:00",
        "operator": "管理员A",
    })
    test("创建批次 BATCH-001", status, check=data.get("ok"))

    status, data = api("POST", "/api/boxes/import/json", {
        "batch_no": "BATCH-001",
        "boxes": [
            {"box_code": "B001", "sample_type": "疫苗", "current_temp": 4.0},
            {"box_code": "B002", "sample_type": "疫苗", "current_temp": 4.5},
            {"box_code": "B003", "sample_type": "疫苗", "current_temp": 3.5},
        ]
    })
    test("导入3箱到批次 (3 imported)", status,
         check=len(data.get("imported", [])) == 3)

    status, data = api("POST", "/api/boxes/import/json", {
        "batch_no": "BATCH-001",
        "boxes": [
            {"box_code": "B004", "sample_type": "血液制品", "current_temp": 3.0},
        ]
    })
    test("样本类型冲突被拦截", status,
         check=len(data.get("rejected", [])) == 1 and "冲突" in data["rejected"][0]["reason"])

    status, data = api("POST", "/api/batches", {
        "batch_no": "BATCH-002",
        "sample_type": "疫苗",
    })
    test("创建批次 BATCH-002", status, check=data.get("ok"))

    status, data = api("POST", "/api/boxes/import/json", {
        "batch_no": "BATCH-002",
        "boxes": [
            {"box_code": "B001", "sample_type": "疫苗", "current_temp": 4.0},
        ]
    })
    test("箱子已在其他未完成批次被拦截", status,
         check="已存在" in data.get("rejected", [{}])[0].get("reason", "") or
               "未完成批次" in data.get("rejected", [{}])[0].get("reason", ""))

    status, data = api("POST", "/api/boxes/import/json", {
        "batch_no": "BATCH-002",
        "boxes": [
            {"box_code": "B201", "sample_type": "疫苗", "current_temp": 4.0},
            {"box_code": "B202", "sample_type": "疫苗", "current_temp": 4.2},
        ]
    })
    test("BATCH-002 导入2箱", status,
         check=len(data.get("imported", [])) == 2)

    status, data = api("GET", "/api/batches/BATCH-001")
    test("批次详情：3箱、3待办", status,
         check=data["batch"]["total_boxes"] == 3 and len(data["pending_todos"]) == 3)

    print()
    print("3. 批次状态流转")

    status, data = api("POST", "/api/batches/BATCH-001/dispatch", {
        "role": "出库员",
        "operator": "张三",
        "current_temp": 4.0,
    })
    test("批次出库 → 转运中", status,
         check=data.get("to") == "转运中" and data.get("success_count") == 3)

    status, data = api("POST", "/api/batches/BATCH-001/arrive", {
        "role": "转运员",
        "operator": "李四",
        "current_temp": 5.0,
    })
    test("批次到达 → 待签收", status, check=data.get("to") == "待签收")

    print()
    print("4. 批次签收 + 缺失箱")

    status, data = api("POST", "/api/batches/BATCH-001/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": ["B001", "B002"],
        "missing_boxes": ["B003"],
        "missing_reason": "未在运输车辆上找到",
    })
    test("批次签收：2签收+1缺失 → 部分签收", status,
         check=(data.get("to") == "部分签收"
                and data.get("received_count") == 2
                and data.get("missing_registered_count") == 1))

    status, data = api("GET", "/api/batches/BATCH-001/audit")
    test("批次审计记录 ≥5条", status, check=len(data) >= 5)

    print()
    print("5. 导出按批次筛选")

    status, data = api("GET", "/api/export/json?batch_no=BATCH-001")
    test("导出按批次筛选 + 汇总", status,
         check=("batch_summary" in data
                and data["batch_summary"]["total_boxes"] == 3))

    print()
    print("6. 缺失箱撤销 & 重新签收")

    status, data = api("POST", "/api/batches/BATCH-001/cancel_missing", {
        "role": "出库员",
        "operator": "张三",
        "box_codes": ["B003"],
    })
    test("越权撤销缺失 → 403", status, expected_status=403)

    status, data = api("POST", "/api/batches/BATCH-001/cancel_missing", {
        "role": "管理员",
        "operator": "管理员A",
        "reason": "箱子已找到",
        "box_codes": ["B003"],
    })
    test("管理员撤销缺失登记", status,
         check=data.get("ok") and data.get("cancelled_count") == 1)

    status, data = api("POST", "/api/batches/BATCH-001/receive", {
        "role": "库房签收员",
        "operator": "王五",
        "received_boxes": ["B003"],
    })
    test("撤销后重新签收 → 已签收", status,
         check=data.get("to") == "已签收" and data.get("received_count") == 1)

    print()
    print("7. 越权操作 & 终态校验")

    status, data = api("POST", "/api/batches/BATCH-002/rollback", {
        "role": "出库员",
        "operator": "张三",
        "reason": "越权关闭批次",
    })
    test("出库员越权回退批次 → 403", status, expected_status=403)

    status, data = api("POST", "/api/batches/BATCH-001/dispatch", {
        "role": "出库员",
        "operator": "张三",
    })
    test("已签收批次不能再出库 → 409", status, expected_status=409)

    print()
    print("=" * 50)
    print(f"  汇总: {passed} passed, {failed} failed")
    print("=" * 50)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
