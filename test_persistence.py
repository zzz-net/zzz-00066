"""
重启持久化验证：
  1. 阈值、箱子状态、审计历史条数对得上
  2. 导出 JSON ↔ 审计条数严格对齐
  3. 每条导出记录都有 role/operator/action_at 等关键审计字段
  4. 终态仍不可变（非法回退 / 越权仍被拦截）
  5. 重复导入仍被拦
"""

import json
import sys
import urllib.request
import urllib.error
import urllib.parse

BASE = "http://localhost:8000"

REQUIRED_EXPORT_FIELDS = [
    "box_code", "sample_type", "sequence",
    "from_status", "to_status",
    "role", "operator", "reason",
    "temp_at_action", "action_at", "current_status",
]


def api(method, path, data=None, raw=False):
    encoded_path = urllib.parse.quote(path, safe="/:=&?[]@!$'()*,;")
    url = f"{BASE}{encoded_path}"
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_body = resp.read()
            if raw:
                return {"status": resp.status, "body": raw_body.decode("utf-8-sig")}
            return {
                "status": resp.status,
                "body": json.loads(raw_body.decode("utf-8")) if raw_body else None,
            }
    except urllib.error.HTTPError as e:
        raw_body = e.read()
        if raw:
            return {"status": e.code, "body": raw_body.decode("utf-8-sig", "ignore")}
        return {
            "status": e.code,
            "body": json.loads(raw_body.decode("utf-8")) if raw_body else None,
        }


passed = 0
failed = 0
fail_log = []


def run(name, result, expect_status=200, check=None):
    global passed, failed
    ok = result["status"] == expect_status
    extra = ""
    if ok and check is not None:
        try:
            ok = bool(check(result["body"]))
        except Exception as e:
            ok = False
            extra = f" [check exception: {e}]"
    status_tag = "PASS" if ok else "FAIL"
    if not ok:
        extra2 = f"  -> HTTP {result['status']}: {json.dumps(result['body'], ensure_ascii=False, default=str)[:300]}"
        failed += 1
        fail_log.append(name)
    else:
        extra2 = ""
        passed += 1
    if not ok or extra:
        extra2 += extra
    print(f"  [{status_tag}] {name}{extra2}")


def section(s):
    print()
    print(f"-- {s} " + "-" * max(0, 60 - len(s)))


def main():
    section("1. 阈值 & 箱子状态持久化")
    run("阈值仍=3条",
        api("GET", "/api/thresholds"),
        check=lambda b: len(b) == 3)

    run("V001 仍为已签收 + receive_at 非空",
        api("GET", "/api/boxes/BOX-V001"),
        check=lambda b: b["status"] == "已签收" and b.get("receive_at"))

    run("HOT1 仍为已回退",
        api("GET", "/api/boxes/BOX-HOT1"),
        check=lambda b: b["status"] == "已回退")

    run("V002 仍为异常待处理",
        api("GET", "/api/boxes/BOX-V002"),
        check=lambda b: b["status"] == "异常待处理")

    section("2. 审计与导出条数严格对齐（核心：重启后导出不能有错位）")
    audit_all = api("GET", "/api/audit")["body"]
    export = api("GET", "/api/export/json")["body"]

    run("导出行数 == 审计总行数",
        {"status": 200, "body": {"audit": len(audit_all),
                                 "export": len(export["rows"])}},
        check=lambda b: b["audit"] == b["export"])

    run("导出 fields 仍是 11 个（顺序不变）",
        {"status": 200, "body": {"fields": export.get("fields", [])}},
        check=lambda b: b["fields"] == REQUIRED_EXPORT_FIELDS)

    run("所有导出记录都含 role/operator/action_at/to_status",
        {"status": 200, "body": {"rows": export["rows"]}},
        check=lambda b: all(
            r.get("role") and r.get("operator")
            and r.get("action_at") and r.get("to_status")
            for r in b["rows"]
        ))

    # 逐箱校验：导出 sequence 从 1 递增且连续
    def seq_check(rows):
        by_box = {}
        for r in rows:
            by_box.setdefault(r["box_code"], []).append(r["sequence"])
        for bc, seqs in by_box.items():
            if seqs != list(range(1, len(seqs) + 1)):
                return False
        return True

    run("每箱导出 sequence 1..N 严格递增连续",
        {"status": 200, "body": {"rows": export["rows"]}},
        check=lambda b: seq_check(b["rows"]))

    # V001 仍 4 条
    v001_rows = [r for r in export["rows"] if r["box_code"] == "BOX-V001"]
    run("V001 重启后仍导出 4 条",
        {"status": 200, "body": {"n": len(v001_rows)}},
        check=lambda b: b["n"] == 4)
    run("V001 最后一条：已签收 + 库房签收员 王五",
        {"status": 200, "body": {"last": v001_rows[-1] if v001_rows else {}}},
        check=lambda b: (b["last"]["to_status"] == "已签收"
                         and b["last"]["role"] == "库房签收员"
                         and b["last"]["operator"] == "王五"
                         and b["last"]["current_status"] == "已签收"))

    # HOT1 越界那条仍带 [温度越界]
    hot_violation = next(
        (r for r in export["rows"]
         if r["box_code"] == "BOX-HOT1" and r["from_status"] == "待出库"),
        None
    )
    run("HOT1 越界那条 reason 仍带 [温度越界]",
        {"status": 200, "body": {"row": hot_violation}},
        check=lambda b: b["row"] is not None
            and "温度越界" in b["row"]["reason"])

    section("3. CSV 导出持久化（表头 + 行数对得上）")
    csv_text = api("GET", "/api/export/csv", raw=True)["body"]
    lines = csv_text.strip().splitlines()
    run("CSV 表头 == 11 列（与 EXPORT_FIELDS 一致）",
        {"status": 200, "body": {"header": lines[0] if lines else ""}},
        check=lambda b: b["header"].split(",") == REQUIRED_EXPORT_FIELDS)
    run("CSV 数据行数 == 导出行数（除表头）",
        {"status": 200, "body": {"csv_rows": max(0, len(lines) - 1),
                                 "json_rows": len(export["rows"])}},
        check=lambda b: b["csv_rows"] == b["json_rows"])

    section("4. 重启后非法操作仍被拦截 & 不写审计")
    audit_before = len(api("GET", "/api/audit")["body"])
    run("V001 已签收→回退→409",
        api("POST", "/api/boxes/BOX-V001/rollback",
            {"role": "管理员", "operator": "管理员A", "reason": "重启后仍想回退"}),
        expect_status=409)

    run("HOT1 已回退→恢复→409",
        api("POST", "/api/boxes/BOX-HOT1/recover",
            {"role": "管理员", "operator": "管理员A", "reason": "想恢复"}),
        expect_status=409)

    # 直接拿一个确定是 待出库 的箱子 R001，做越权出库（要求出库员，用转运员→403）
    run("R001 待出库：转运员越权出库→403",
        api("POST", "/api/boxes/BOX-R001/dispatch",
            {"role": "转运员", "operator": "李四"}),
        expect_status=403)

    audit_after = len(api("GET", "/api/audit")["body"])
    run("上述 3 次失败操作都没写入审计",
        {"status": 200, "body": {"before": audit_before, "after": audit_after}},
        check=lambda b: b["before"] == b["after"])

    section("5. 重复导入仍被拦截")
    run("重复导入 V001 仍被拒",
        api("POST", "/api/boxes/import/json", {"boxes": [
            {"box_code": "BOX-V001", "sample_type": "疫苗", "current_temp": 4.0}
        ]}),
        check=lambda b: (len(b["imported"]) == 0
                         and len(b["rejected"]) == 1
                         and "已存在" in b["rejected"][0]["reason"]))

    # 确保失败导入未写入审计
    audit_after_dup = len(api("GET", "/api/audit")["body"])
    run("重复导入也未写入审计",
        {"status": 200, "body": {"before": audit_after,
                                 "after": audit_after_dup}},
        check=lambda b: b["before"] == b["after"])

    print()
    print("=" * 70)
    print(f"  汇总: {passed} passed, {failed} failed")
    if failed:
        print(f"  失败项: {', '.join(fail_log)}")
    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
