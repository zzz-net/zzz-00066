import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

BASE = "http://localhost:8000"


def api(method, path, data=None):
    encoded_path = urllib.parse.quote(path, safe="/:=&?[]@!$'()*,;")
    url = f"{BASE}{encoded_path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return {"status": resp.status, "body": json.loads(resp.read())}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": json.loads(e.read())}


def test(name, result, expect_status=200, check=None):
    ok = result["status"] == expect_status
    extra = ""
    if check and ok:
        ok = check(result["body"])
        if not ok:
            extra = " [check failed]"
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name} (HTTP {result['status']}){extra}")
    if not ok:
        print(f"         body: {json.dumps(result['body'], ensure_ascii=False)}")
    return ok


def main():
    passed = 0
    failed = 0

    def run(name, result, expect_status=200, check=None):
        nonlocal passed, failed
        if test(name, result, expect_status, check):
            passed += 1
        else:
            failed += 1

    print("\n=== 1. 配置阈值 ===")
    run("创建疫苗阈值", api("POST", "/api/thresholds", {
        "sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120
    }))
    run("创建血液制品阈值", api("POST", "/api/thresholds", {
        "sample_type": "血液制品", "temp_min": 1, "temp_max": 6, "timeout_minutes": 90
    }))
    run("创建试剂阈值", api("POST", "/api/thresholds", {
        "sample_type": "试剂", "temp_min": -20, "temp_max": -15, "timeout_minutes": 180
    }))
    run("列出阈值(3条)", api("GET", "/api/thresholds"), check=lambda b: len(b) == 3)
    run("查询疫苗阈值", api("GET", "/api/thresholds/疫苗"), check=lambda b: b["temp_min"] == 2)

    print("\n=== 2. 导入箱码 ===")
    run("导入4箱", api("POST", "/api/boxes/import/json", {
        "boxes": [
            {"box_code": "BOX-V001", "sample_type": "疫苗", "current_temp": 4.0},
            {"box_code": "BOX-V002", "sample_type": "疫苗", "current_temp": 3.5},
            {"box_code": "BOX-B001", "sample_type": "血液制品", "current_temp": 2.0},
            {"box_code": "BOX-R001", "sample_type": "试剂", "current_temp": -18.0},
        ]
    }), check=lambda b: len(b["imported"]) == 4 and len(b["rejected"]) == 0)

    print("\n=== 3. 重复箱码拦截 ===")
    run("重复箱码被拦", api("POST", "/api/boxes/import/json", {
        "boxes": [{"box_code": "BOX-V001", "sample_type": "疫苗", "current_temp": 4.0}]
    }), check=lambda b: len(b["rejected"]) == 1 and "已存在" in b["rejected"][0]["reason"])

    run("同批次重复被拦", api("POST", "/api/boxes/import/json", {
        "boxes": [
            {"box_code": "BOX-DUP1", "sample_type": "疫苗", "current_temp": 4.0},
            {"box_code": "BOX-DUP1", "sample_type": "疫苗", "current_temp": 4.0},
        ]
    }), check=lambda b: len(b["rejected"]) == 1 and "重复" in b["rejected"][0]["reason"])

    print("\n=== 4. 未配置阈值的样本类型导入 ===")
    run("未配置阈值被拒", api("POST", "/api/boxes/import/json", {
        "boxes": [{"box_code": "BOX-X001", "sample_type": "未知类型", "current_temp": 4.0}]
    }), check=lambda b: len(b["rejected"]) == 1 and "未配置阈值" in b["rejected"][0]["reason"])

    print("\n=== 5. 成功路径：出库 → 转运 → 签收 ===")
    run("查看BOX-V001(待出库)", api("GET", "/api/boxes/BOX-V001"),
        check=lambda b: b["status"] == "待出库")

    run("出库 BOX-V001", api("POST", "/api/boxes/BOX-V001/dispatch", {
        "role": "出库员", "operator": "张三", "current_temp": 4.5
    }), check=lambda b: b["to"] == "转运中")

    run("转运到达 BOX-V001", api("POST", "/api/boxes/BOX-V001/arrive", {
        "role": "转运员", "operator": "李四", "current_temp": 5.0
    }), check=lambda b: b["to"] == "待签收")

    run("签收 BOX-V001", api("POST", "/api/boxes/BOX-V001/receive", {
        "role": "库房签收员", "operator": "王五", "current_temp": 4.2
    }), check=lambda b: b["to"] == "已签收")

    print("\n=== 6. 审计记录 ===")
    run("BOX-V001审计(4条)", api("GET", "/api/audit?box_code=BOX-V001"),
        check=lambda b: len(b) == 4)

    print("\n=== 7. 温度越界 → 异常待处理 ===")
    api("POST", "/api/boxes/import/json", {
        "boxes": [{"box_code": "BOX-HOT1", "sample_type": "疫苗", "current_temp": 5.0}]
    })
    run("出库时温度越界→异常", api("POST", "/api/boxes/BOX-HOT1/dispatch", {
        "role": "出库员", "operator": "张三", "current_temp": 15.0
    }), check=lambda b: b["to"] == "异常待处理" and "warning" in b)

    print("\n=== 8. 转运员不能代替库房签收 ===")
    api("POST", "/api/boxes/BOX-V002/dispatch", {
        "role": "出库员", "operator": "张三", "current_temp": 3.5
    })
    api("POST", "/api/boxes/BOX-V002/arrive", {
        "role": "转运员", "operator": "李四", "current_temp": 4.0
    })
    run("转运员签收被拒(403)", api("POST", "/api/boxes/BOX-V002/receive", {
        "role": "转运员", "operator": "李四", "current_temp": 4.0
    }), expect_status=403)

    print("\n=== 9. 非法回退不能改成功状态 ===")
    run("已签收不可回退(409)", api("POST", "/api/boxes/BOX-V001/rollback", {
        "role": "管理员", "operator": "管理员A", "reason": "误操作"
    }), expect_status=409)

    print("\n=== 10. 管理员操作路径 ===")
    run("管理员标记异常", api("POST", "/api/boxes/BOX-V002/exception", {
        "role": "管理员", "operator": "管理员A", "reason": "转运延迟"
    }), check=lambda b: b["to"] == "异常待处理")

    run("异常恢复", api("POST", "/api/boxes/BOX-HOT1/recover", {
        "role": "管理员", "operator": "管理员A", "reason": "温度已恢复"
    }), check=lambda b: b["to"] == "待出库")

    run("回退BOX-HOT1", api("POST", "/api/boxes/BOX-HOT1/rollback", {
        "role": "管理员", "operator": "管理员A", "reason": "样本损坏"
    }), check=lambda b: b["to"] == "已回退")

    run("已回退不可再变更(409)", api("POST", "/api/boxes/BOX-HOT1/recover", {
        "role": "管理员", "operator": "管理员A", "reason": "想恢复"
    }), expect_status=409)

    print("\n=== 11. 导出 ===")
    run("导出JSON", api("GET", "/api/export/json"),
        check=lambda b: isinstance(b, list) and len(b) > 0)

    print("\n=== 12. 按状态过滤 ===")
    run("已签收列表", api("GET", "/api/boxes?status=已签收"),
        check=lambda b: len(b) == 1 and b[0]["box_code"] == "BOX-V001")

    print("\n=== 13. 全局审计 ===")
    run("全局审计记录", api("GET", "/api/audit"),
        check=lambda b: len(b) >= 4)

    print(f"\n{'='*50}")
    print(f"  结果: {passed} passed, {failed} failed")
    print(f"{'='*50}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
