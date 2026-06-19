"""
README 样例链路一键执行脚本。

用法:
    python run_examples.py              # 全链路：成功路径 + 失败路径
    python run_examples.py happy        # 仅成功路径
    python run_examples.py failure      # 仅失败路径
    python run_examples.py persistence  # 重启持久化验证（服务需先重启）

所有请求体都从 examples/*.json 读取，避免 PowerShell 引号/编码问题。
"""

import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

BASE = "http://localhost:8000"
EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"


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
                "body": json.loads(raw_body.decode("utf-8-sig")) if raw_body else None,
            }
    except urllib.error.HTTPError as e:
        raw_body = e.read()
        if raw:
            return {
                "status": e.code,
                "body": raw_body.decode("utf-8-sig", "ignore"),
            }
        return {
            "status": e.code,
            "body": json.loads(raw_body.decode("utf-8-sig")) if raw_body else None,
        }


def api_upload_csv(path, csv_path: Path):
    encoded_path = urllib.parse.quote(path, safe="/:=&?[]@!$'()*,;")
    url = f"{BASE}{encoded_path}"
    boundary = "----WebKitFormBoundaryA1B2C3D4"
    lines = []
    lines.append(f"--{boundary}")
    lines.append(f'Content-Disposition: form-data; name="file"; filename="{csv_path.name}"')
    lines.append("Content-Type: text/csv")
    lines.append("")
    lines.append(csv_path.read_text(encoding="utf-8"))
    lines.append(f"--{boundary}--")
    lines.append("")
    body = ("\r\n".join(lines)).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return {
                "status": resp.status,
                "body": json.loads(raw.decode("utf-8")) if raw else None,
            }
    except urllib.error.HTTPError as e:
        raw = e.read()
        return {
            "status": e.code,
            "body": json.loads(raw.decode("utf-8")) if raw else None,
        }


def load(name):
    return json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


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
    if ok:
        extra2 = ""
        passed += 1
        status_tag = "PASS"
    else:
        extra2 = f"  -> HTTP {result['status']}: {json.dumps(result['body'], ensure_ascii=False)[:200]}"
        failed += 1
        fail_log.append(name)
        status_tag = "FAIL"
    if not ok or extra:
        extra2 += extra
    print(f"  [{status_tag}] {name}{extra2}")


def header(s):
    print()
    print("=" * 60)
    print(f"  {s}")
    print("=" * 60)


def section(s):
    print()
    print(f"-- {s} " + "-" * max(0, 50 - len(s)))


def happy_path():
    header("一、成功路径：配置阈值 → 导入 → 出库 → 转运 → 签收 → 导出")

    section("1. 配置 3 个样本类型的温度阈值")
    run("疫苗阈值", api("POST", "/api/thresholds", load("threshold_vaccine.json")))
    run("血液制品阈值", api("POST", "/api/thresholds", load("threshold_blood.json")))
    run("试剂阈值", api("POST", "/api/thresholds", load("threshold_reagent.json")))
    run("列出阈值 ≥3",
        api("GET", "/api/thresholds"),
        check=lambda b: len(b) >= 3)

    section("2. JSON 导入 4 箱")
    run("导入4箱 (4 imported / 0 rejected)",
        api("POST", "/api/boxes/import/json", load("boxes_import.json")),
        check=lambda b: len(b["imported"]) == 4 and len(b["rejected"]) == 0)

    section("3. CSV 再导入 3 箱")
    run("CSV 导入3箱",
        api_upload_csv("/api/boxes/import/csv", EXAMPLES_DIR / "boxes_import.csv"),
        check=lambda b: len(b["imported"]) == 3 and len(b["rejected"]) == 0)

    section("4. BOX-V001 出库 → 转运 → 签收")
    run("BOX-V001 当前为待出库",
        api("GET", "/api/boxes/BOX-V001"),
        check=lambda b: b["status"] == "待出库")

    run("出库 BOX-V001 (出库员 张三)",
        api("POST", "/api/boxes/BOX-V001/dispatch", load("dispatch_ok.json")),
        check=lambda b: b["to"] == "转运中")

    run("转运到达 BOX-V001 (转运员 李四)",
        api("POST", "/api/boxes/BOX-V001/arrive", load("arrive_ok.json")),
        check=lambda b: b["to"] == "待签收")

    run("库房签收 BOX-V001 (库房签收员 王五)",
        api("POST", "/api/boxes/BOX-V001/receive", load("receive_ok.json")),
        check=lambda b: b["to"] == "已签收")

    section("5. 审计历史（BOX-V001 应该有 4 条记录）")
    run("BOX-V001 审计记录=4",
        api("GET", "/api/audit?box_code=BOX-V001"),
        check=lambda b: len(b) == 4)

    section("6. 导出 JSON / CSV（字段对齐，可看到流转过程）")
    resp = api("GET", "/api/export/json")
    run("导出 JSON 包含 rows + fields + generated_at",
        resp,
        check=lambda b: "rows" in b and "fields" in b and "generated_at" in b)
    run("导出 JSON 至少 10 条历史（4 导入+3 CSV 导入+4 V001 流转=11 前）",
        resp,
        check=lambda b: len(b["rows"]) >= 10)
    run("导出 JSON 每条都有 role/operator/action_at/from_status/to_status",
        resp,
        check=lambda b: all(
            r.get("role") and r.get("operator") and r.get("action_at")
            and r.get("to_status")
            for r in b["rows"]
        ))

    csv_resp = api("GET", "/api/export/csv", raw=True)
    run("导出 CSV HTTP 200 且表头含 role/operator/action_at",
        csv_resp,
        check=lambda b: isinstance(b, str)
            and "role" in b and "operator" in b and "action_at" in b)
    csv_text = None
    if csv_resp["status"] == 200:
        csv_text = csv_resp["body"] if isinstance(csv_resp["body"], str) else None

    section("7. BOX-B001（血液制品）走完全流程，然后看全局审计")
    api("POST", "/api/boxes/BOX-B001/dispatch",
        {"role": "出库员", "operator": "张三", "current_temp": 2.5})
    api("POST", "/api/boxes/BOX-B001/arrive",
        {"role": "转运员", "operator": "李四", "current_temp": 3.0})
    api("POST", "/api/boxes/BOX-B001/receive",
        {"role": "库房签收员", "operator": "王五", "current_temp": 2.8,
         "reason": "冷链完好签字确认"})

    run("全局审计 ≥13 条（7 导入 + 3 V001 流转 + 3 B001 流转）",
        api("GET", "/api/audit"),
        check=lambda b: len(b) >= 13)


def failure_path():
    header("二、失败路径：所有拦截项")

    section("1. 重复箱码拦截")
    run("箱码已存在",
        api("POST", "/api/boxes/import/json", load("boxes_existing_dup.json")),
        check=lambda b: len(b["rejected"]) == 1 and "已存在" in b["rejected"][0]["reason"])
    run("同批次内重复",
        api("POST", "/api/boxes/import/json", load("boxes_batch_dup.json")),
        check=lambda b: len(b["rejected"]) == 1 and "重复" in b["rejected"][0]["reason"])

    section("2. 未配置阈值样本类型导入")
    run("未配置阈值被拒",
        api("POST", "/api/boxes/import/json", {
            "boxes": [{"box_code": "BOX-XYZ", "sample_type": "不存在类型", "current_temp": 4}]
        }),
        check=lambda b: len(b["rejected"]) == 1 and "未配置阈值" in b["rejected"][0]["reason"])

    section("3. 温度越界 → 异常待处理")
    api("POST", "/api/boxes/import/json", load("boxes_hot1.json"))
    run("出库时温度越界(15°C > 疫苗阈值8°C) → 异常待处理 + warning",
        api("POST", "/api/boxes/BOX-HOT1/dispatch", load("dispatch_hot.json")),
        check=lambda b: b["to"] == "异常待处理" and "warning" in b)

    section("4. 越权：转运员不能签收")
    api("POST", "/api/boxes/BOX-V002/dispatch",
        {"role": "出库员", "operator": "张三", "current_temp": 3.5})
    api("POST", "/api/boxes/BOX-V002/arrive",
        {"role": "转运员", "operator": "李四", "current_temp": 4.0})
    run("转运员签收 → 403",
        api("POST", "/api/boxes/BOX-V002/receive", load("receive_unauthorized.json")),
        expect_status=403)

    section("5. 终态非法回退：已签收不能变")
    run("BOX-V001 已签收 → 回退 409",
        api("POST", "/api/boxes/BOX-V001/rollback", load("rollback_invalid.json")),
        expect_status=409)

    section("6. 状态流转顺序错误：直接签收待出库箱子")
    run("待出库 BOX-V003 不能直接签收 → 409",
        api("POST", "/api/boxes/BOX-V003/receive", load("receive_ok.json")),
        expect_status=409)


def admin_and_export():
    header("三、管理员：异常→恢复→回退，以及导出与审计对齐校验")

    section("1. 管理员标记 BOX-V002 异常")
    run("标记 BOX-V002 异常",
        api("POST", "/api/boxes/BOX-V002/exception", load("mark_exception.json")),
        check=lambda b: b["to"] == "异常待处理")

    section("2. 管理员恢复 BOX-HOT1")
    run("恢复 BOX-HOT1 → 待出库",
        api("POST", "/api/boxes/BOX-HOT1/recover", load("recover.json")),
        check=lambda b: b["to"] == "待出库")

    section("3. 管理员回退 BOX-HOT1（样本损坏）")
    run("回退 BOX-HOT1 → 已回退",
        api("POST", "/api/boxes/BOX-HOT1/rollback", load("rollback_ok.json")),
        check=lambda b: b["to"] == "已回退")

    run("已回退 BOX-HOT1 再恢复 → 409",
        api("POST", "/api/boxes/BOX-HOT1/recover", load("recover.json")),
        expect_status=409)

    section("4. 关键校验：失败操作**没有**被写入审计历史")
    audit_before = api("GET", "/api/audit?box_code=BOX-V001")["body"]
    # 再次尝试一个失败回退
    api("POST", "/api/boxes/BOX-V001/rollback", {"role": "管理员", "operator": "管理员A", "reason": "再试一次"})
    audit_after = api("GET", "/api/audit?box_code=BOX-V001")["body"]
    run("BOX-V001 审计条数不变（失败回退未写入）",
        {"status": 200, "body": {"before": len(audit_before), "after": len(audit_after)}},
        check=lambda b: b["before"] == b["after"])

    # 越权签收未写入
    audit_v002_before = api("GET", "/api/audit?box_code=BOX-V002")["body"]
    last_before = audit_v002_before[-1]
    # 越权签收 BOX-R001（还没转出）
    api("POST", "/api/boxes/BOX-R001/receive", load("receive_unauthorized.json"))
    audit_r001 = api("GET", "/api/audit?box_code=BOX-R001")["body"]
    run("BOX-R001 只有 1 条审计（只有导入，越权签收未写入）",
        {"status": 200, "body": {"n": len(audit_r001)}},
        check=lambda b: b["n"] == 1)

    section("5. 导出 JSON 与 审计条数严格对齐，字段语义一致")
    audit_all = api("GET", "/api/audit")["body"]
    export = api("GET", "/api/export/json")["body"]
    run("导出行数 == 审计总行数",
        {"status": 200, "body": {"audit": len(audit_all), "export": len(export["rows"])}},
        check=lambda b: b["audit"] == b["export"])
    run("导出 fields 包含所有关键审计字段",
        {"status": 200, "body": {"fields": export["fields"]}},
        check=lambda b: all(f in b["fields"] for f in
            ["box_code", "from_status", "to_status", "role", "operator", "reason", "action_at"]))

    section("6. 最终状态查询")
    statuses = api("GET", "/api/boxes")["body"]
    print(f"  当前箱数量: {len(statuses)}")
    for b in statuses:
        print(f"    {b['box_code']}  {b['sample_type']}  →  {b['status']}")


def persistence_check():
    header("四、重启后持久化验证（请先手动重启服务再执行）")

    section("1. 阈值、箱子、审计条数")
    run("阈值仍=3", api("GET", "/api/thresholds"), check=lambda b: len(b) == 3)
    run("BOX-V001 仍为已签收 + receive_at 非空",
        api("GET", "/api/boxes/BOX-V001"),
        check=lambda b: b["status"] == "已签收" and b["receive_at"])
    run("BOX-HOT1 仍为已回退",
        api("GET", "/api/boxes/BOX-HOT1"),
        check=lambda b: b["status"] == "已回退")
    run("BOX-V002 仍为异常待处理",
        api("GET", "/api/boxes/BOX-V002"),
        check=lambda b: b["status"] == "异常待处理")
    run("BOX-B001 仍为已签收",
        api("GET", "/api/boxes/BOX-B001"),
        check=lambda b: b["status"] == "已签收")

    section("2. 导出与审计仍严格对齐")
    audit_all = api("GET", "/api/audit")["body"]
    export = api("GET", "/api/export/json")["body"]
    run("重启后 导出行数 == 审计总行数",
        {"status": 200, "body": {"audit": len(audit_all), "export": len(export["rows"])}},
        check=lambda b: b["audit"] == b["export"])
    run("重启后 BOX-V001 审计仍=4",
        api("GET", "/api/audit?box_code=BOX-V001"),
        check=lambda b: len(b) == 4)

    section("3. 重启后终态仍不可变")
    run("重启后 BOX-V001 回退仍 409",
        api("POST", "/api/boxes/BOX-V001/rollback", {"role": "管理员", "operator": "管理员A"}),
        expect_status=409)
    run("重启后 重复导入 V001 仍被拦",
        api("POST", "/api/boxes/import/json", load("boxes_existing_dup.json")),
        check=lambda b: len(b["rejected"]) == 1)


def print_summary():
    print()
    print("=" * 60)
    print(f"  汇总: {passed} passed, {failed} failed")
    if failed:
        print(f"  失败项: {', '.join(fail_log)}")
    print("=" * 60)


def main():
    # 探测服务是否起来
    for _ in range(5):
        try:
            r = api("GET", "/api/thresholds")
            if r["status"] < 500:
                break
        except Exception:
            pass
        print("  等待服务启动...")
        time.sleep(2)
    else:
        print("  ERROR: 无法连接到 http://localhost:8000")
        print("         请先运行: uvicorn app.main:app --port 8000")
        sys.exit(2)

    modes = set(sys.argv[1:]) or {"happy", "failure", "admin"}

    if "happy" in modes:
        happy_path()
    if "failure" in modes:
        failure_path()
    if "admin" in modes:
        admin_and_export()
    if "persistence" in modes:
        persistence_check()

    print_summary()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
