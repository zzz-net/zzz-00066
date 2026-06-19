import json
import sys
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


def main():
    passed = 0
    failed = 0

    def run(name, result, expect_status=200, check=None):
        nonlocal passed, failed
        ok = result["status"] == expect_status
        if check and ok:
            ok = check(result["body"])
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok:
            print(f"         body: {json.dumps(result['body'], ensure_ascii=False)}")
            failed += 1
        else:
            passed += 1

    print("\n=== 重启后持久化验证 ===")

    thresholds = api("GET", "/api/thresholds")
    run("阈值配置持久(3条)", thresholds, check=lambda b: len(b) == 3)

    boxes = api("GET", "/api/boxes")
    run("箱状态持久(6箱)", boxes, check=lambda b: len(b) == 6)

    run("BOX-V001仍为已签收", api("GET", "/api/boxes/BOX-V001"),
        check=lambda b: b["status"] == "已签收" and b["receive_at"] is not None)

    run("BOX-HOT1仍为已回退", api("GET", "/api/boxes/BOX-HOT1"),
        check=lambda b: b["status"] == "已回退")

    run("BOX-V002仍为异常待处理", api("GET", "/api/boxes/BOX-V002"),
        check=lambda b: b["status"] == "异常待处理")

    audit = api("GET", "/api/audit")
    run("审计记录持久", audit, check=lambda b: len(b) >= 4)

    export = api("GET", "/api/export/json")
    run("导出JSON持久", export, check=lambda b: len(b) == 6)

    run("已签收不可回退(重启后仍409)", api("POST", "/api/boxes/BOX-V001/rollback", {
        "role": "管理员", "operator": "管理员A", "reason": "重启后仍想回退"
    }), expect_status=409)

    print(f"\n{'='*50}")
    print(f"  结果: {passed} passed, {failed} failed")
    print(f"{'='*50}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
