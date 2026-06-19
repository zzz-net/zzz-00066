"""
端到端回归测试：
  1. 成功路径全链路（阈值→导入→出库→转运→签收→导出）
  2. 失败路径：重复箱码、温度越界、越权签收、非法回退
  3. 管理员操作 + 导出语义验证（重点：导出 ↔ 审计对齐、字段语义、失败操作不写历史）
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
                return {
                    "status": resp.status,
                    "body": raw_body.decode("utf-8-sig"),
                }
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
        extra2 = f"  -> HTTP {result['status']}: {json.dumps(result['body'], ensure_ascii=False)[:300]}"
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
    section("1. 配置 3 个阈值")
    run("疫苗阈值", api("POST", "/api/thresholds",
        {"sample_type": "疫苗", "temp_min": 2, "temp_max": 8, "timeout_minutes": 120}))
    run("血液阈值", api("POST", "/api/thresholds",
        {"sample_type": "血液制品", "temp_min": 1, "temp_max": 6, "timeout_minutes": 90}))
    run("试剂阈值", api("POST", "/api/thresholds",
        {"sample_type": "试剂", "temp_min": -20, "temp_max": -15, "timeout_minutes": 180}))
    run("列表=3条", api("GET", "/api/thresholds"), check=lambda b: len(b) == 3)

    section("2. 导入 + 重复箱码拦截")
    run("导入4箱=4/0",
        api("POST", "/api/boxes/import/json", {"boxes": [
            {"box_code": "BOX-V001", "sample_type": "疫苗", "current_temp": 4.0},
            {"box_code": "BOX-V002", "sample_type": "疫苗", "current_temp": 3.5},
            {"box_code": "BOX-B001", "sample_type": "血液制品", "current_temp": 2.0},
            {"box_code": "BOX-R001", "sample_type": "试剂", "current_temp": -18.0},
        ]}),
        check=lambda b: len(b["imported"]) == 4 and len(b["rejected"]) == 0)

    run("重复箱码被拦",
        api("POST", "/api/boxes/import/json", {"boxes": [
            {"box_code": "BOX-V001", "sample_type": "疫苗", "current_temp": 4.0}
        ]}),
        check=lambda b: len(b["imported"]) == 0
            and len(b["rejected"]) == 1
            and "已存在" in b["rejected"][0]["reason"])

    run("同批次重复被拦",
        api("POST", "/api/boxes/import/json", {"boxes": [
            {"box_code": "BOX-DUP1", "sample_type": "疫苗", "current_temp": 4.0},
            {"box_code": "BOX-DUP1", "sample_type": "疫苗", "current_temp": 4.0},
        ]}),
        check=lambda b: len(b["imported"]) == 1
            and len(b["rejected"]) == 1
            and "重复" in b["rejected"][0]["reason"])

    section("3. 成功路径 BOX-V001: 出库→转运→签收")
    run("出库 V001",
        api("POST", "/api/boxes/BOX-V001/dispatch",
            {"role": "出库员", "operator": "张三", "current_temp": 4.5}),
        check=lambda b: b["to"] == "转运中")
    run("到达 V001",
        api("POST", "/api/boxes/BOX-V001/arrive",
            {"role": "转运员", "operator": "李四", "current_temp": 5.0}),
        check=lambda b: b["to"] == "待签收")
    run("签收 V001",
        api("POST", "/api/boxes/BOX-V001/receive",
            {"role": "库房签收员", "operator": "王五", "current_temp": 4.2,
             "reason": "外观完好温度正常"}),
        check=lambda b: b["to"] == "已签收")

    section("4. 失败路径：越权 + 非法回退 + 顺序错误")
    # 先把 V002 走到 待签收
    api("POST", "/api/boxes/BOX-V002/dispatch",
        {"role": "出库员", "operator": "张三", "current_temp": 3.5})
    api("POST", "/api/boxes/BOX-V002/arrive",
        {"role": "转运员", "operator": "李四", "current_temp": 4.0})

    audit_v002_before = api("GET", "/api/audit?box_code=BOX-V002")["body"]
    audit_v001_before = api("GET", "/api/audit?box_code=BOX-V001")["body"]
    audit_r001_before = api("GET", "/api/audit?box_code=BOX-R001")["body"]

    run("转运员签收 V002 → 403",
        api("POST", "/api/boxes/BOX-V002/receive",
            {"role": "转运员", "operator": "李四", "current_temp": 4.0}),
        expect_status=403)

    run("V001 已签收→回退→409",
        api("POST", "/api/boxes/BOX-V001/rollback",
            {"role": "管理员", "operator": "管理员A", "reason": "误操作"}),
        expect_status=409)

    run("R001 待出库直接签收 → 409",
        api("POST", "/api/boxes/BOX-R001/receive",
            {"role": "库房签收员", "operator": "王五", "current_temp": -17.0}),
        expect_status=409)

    # —— 关键：失败操作没有写入审计 ——
    audit_v002_after = api("GET", "/api/audit?box_code=BOX-V002")["body"]
    audit_v001_after = api("GET", "/api/audit?box_code=BOX-V001")["body"]
    audit_r001_after = api("GET", "/api/audit?box_code=BOX-R001")["body"]
    run("越权签收未写入审计 (V002 audit 数不变)",
        {"status": 200, "body": {"before": len(audit_v002_before),
                                 "after": len(audit_v002_after)}},
        check=lambda b: b["before"] == b["after"])
    run("非法回退未写入审计 (V001 audit 数不变)",
        {"status": 200, "body": {"before": len(audit_v001_before),
                                 "after": len(audit_v001_after)}},
        check=lambda b: b["before"] == b["after"])
    run("顺序错误签收未写入审计 (R001 audit 数不变)",
        {"status": 200, "body": {"before": len(audit_r001_before),
                                 "after": len(audit_r001_after)}},
        check=lambda b: b["before"] == b["after"])

    section("5. 温度越界 → 异常待处理")
    api("POST", "/api/boxes/import/json", {"boxes": [
        {"box_code": "BOX-HOT1", "sample_type": "疫苗", "current_temp": 5.0}
    ]})
    run("HOT1 出库越界→异常",
        api("POST", "/api/boxes/BOX-HOT1/dispatch",
            {"role": "出库员", "operator": "张三", "current_temp": 15.0}),
        check=lambda b: b["to"] == "异常待处理" and "warning" in b)

    section("6. 管理员：标记异常 + 恢复 + 回退")
    run("标记 V002 异常",
        api("POST", "/api/boxes/BOX-V002/exception",
            {"role": "管理员", "operator": "管理员A", "reason": "转运延迟"}),
        check=lambda b: b["to"] == "异常待处理")

    run("恢复 HOT1 → 待出库",
        api("POST", "/api/boxes/BOX-HOT1/recover",
            {"role": "管理员", "operator": "管理员A", "reason": "温度已恢复正常"}),
        check=lambda b: b["to"] == "待出库")

    run("回退 HOT1 → 已回退",
        api("POST", "/api/boxes/BOX-HOT1/rollback",
            {"role": "管理员", "operator": "管理员A", "reason": "样本损坏正式报废"}),
        check=lambda b: b["to"] == "已回退")

    run("已回退不可再变 → 409",
        api("POST", "/api/boxes/BOX-HOT1/recover",
            {"role": "管理员", "operator": "管理员A", "reason": "想再恢复"}),
        expect_status=409)

    section("7. 导出结构 + 语义验证（新）")
    export = api("GET", "/api/export/json")
    run("导出 HTTP 200 并含 generated_at/fields/rows",
        export,
        check=lambda b: all(k in b for k in ("generated_at", "fields", "rows")))

    run("导出 fields == 要求的 11 个字段（顺序一致）",
        {"status": 200, "body": {"fields": export["body"].get("fields", [])}},
        check=lambda b: b["fields"] == REQUIRED_EXPORT_FIELDS)

    audit_all = api("GET", "/api/audit")["body"]
    run("导出行数 == 审计总行数",
        {"status": 200, "body": {"audit": len(audit_all),
                                 "export": len(export["body"]["rows"])}},
        check=lambda b: b["audit"] == b["export"])

    run("每行都有 role/operator/action_at/to_status",
        {"status": 200, "body": {"rows": export["body"]["rows"]}},
        check=lambda b: all(
            r.get("role") and r.get("operator")
            and r.get("action_at") and r.get("to_status")
            for r in b["rows"]
        ))

    # V001 应该有 4 条导出记录（导入/出库/到达/签收）
    v001_rows = [r for r in export["body"]["rows"] if r["box_code"] == "BOX-V001"]
    run("BOX-V001 导出 4 条，sequence 1..4 递增",
        {"status": 200, "body": {"rows": v001_rows}},
        check=lambda b: (len(b["rows"]) == 4
                         and [r["sequence"] for r in b["rows"]] == [1, 2, 3, 4]))

    # V001 最后一条应该是 已签收，角色=库房签收员
    run("V001 最后一条是已签收(库房签收员 王五)",
        {"status": 200, "body": {"last": v001_rows[-1] if v001_rows else {}}},
        check=lambda b: (b["last"]["to_status"] == "已签收"
                         and b["last"]["role"] == "库房签收员"
                         and b["last"]["operator"] == "王五"
                         and "完好" in b["last"]["reason"]
                         and b["last"]["current_status"] == "已签收"))

    # HOT1 温度越界那条的 reason 应该带 [温度越界] 前缀
    hot_violation = next(
        (r for r in export["body"]["rows"]
         if r["box_code"] == "BOX-HOT1" and r["from_status"] == "待出库"),
        None
    )
    run("HOT1 越界那条的 reason 带 [温度越界]",
        {"status": 200, "body": {"row": hot_violation}},
        check=lambda b: b["row"] is not None
            and "温度越界" in b["row"]["reason"]
            and b["row"]["to_status"] == "异常待处理")

    # CSV 导出可下载 + 表头包含所有关键列
    csv_resp = api("GET", "/api/export/csv", raw=True)
    run("CSV 导出 HTTP 200 且表头含关键列",
        csv_resp,
        check=lambda b: (isinstance(b, str)
                         and "box_code" in b
                         and "role" in b
                         and "operator" in b
                         and "action_at" in b))

    section("8. 重启前持久化快照（用户需手动重启后跑 test_persistence.py 再确认）")
    snapshot = {
        "thresholds_count": len(api("GET", "/api/thresholds")["body"]),
        "boxes_count": len(api("GET", "/api/boxes")["body"]),
        "audit_count": len(audit_all),
        "export_rows": len(export["body"]["rows"]),
        "v001_status": next(
            (r["current_status"] for r in api("GET", "/api/export/json")["body"]["rows"]
             if r["box_code"] == "BOX-V001"),
            None,
        ),
    }
    print(f"  快照: {json.dumps(snapshot, ensure_ascii=False)}")

    print()
    print("=" * 70)
    print(f"  汇总: {passed} passed, {failed} failed")
    if failed:
        print(f"  失败项: {', '.join(fail_log)}")
    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
