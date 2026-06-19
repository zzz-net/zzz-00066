import csv
import io
from datetime import datetime

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse

from app.database import get_db, init_db
from app.models import (
    BoxImportItem,
    BoxImportJSON,
    ThresholdCreate,
    TransitionRequest,
)

app = FastAPI(title="冷链交接 API")

TRANSITIONS = {
    "dispatch": {
        "from": ["待出库"],
        "to": "转运中",
        "roles": ["出库员"],
    },
    "arrive": {
        "from": ["转运中"],
        "to": "待签收",
        "roles": ["转运员"],
    },
    "receive": {
        "from": ["待签收"],
        "to": "已签收",
        "roles": ["库房签收员"],
    },
    "mark_exception": {
        "from": ["转运中", "待签收", "待出库"],
        "to": "异常待处理",
        "roles": ["管理员"],
    },
    "rollback": {
        "from": ["待出库", "异常待处理"],
        "to": "已回退",
        "roles": ["管理员"],
    },
    "recover": {
        "from": ["异常待处理"],
        "to": "待出库",
        "roles": ["管理员"],
    },
}

TERMINAL_STATES = {"已签收", "已回退"}


@app.on_event("startup")
def startup():
    init_db()


# ── Thresholds ───────────────────────────────────────────────────────────────


@app.post("/api/thresholds")
def create_threshold(data: ThresholdCreate):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO thresholds (sample_type, temp_min, temp_max, timeout_minutes) VALUES (?, ?, ?, ?)",
            (data.sample_type, data.temp_min, data.temp_max, data.timeout_minutes),
        )
    return {"ok": True, "sample_type": data.sample_type}


@app.get("/api/thresholds")
def list_thresholds():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM thresholds").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/thresholds/{sample_type}")
def get_threshold(sample_type: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM thresholds WHERE sample_type = ?", (sample_type,)
        ).fetchone()
    if not row:
        raise HTTPException(404, f"样本类型 {sample_type} 的阈值未配置")
    return dict(row)


@app.delete("/api/thresholds/{sample_type}")
def delete_threshold(sample_type: str):
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM thresholds WHERE sample_type = ?", (sample_type,)
        )
    if cur.rowcount == 0:
        raise HTTPException(404, f"样本类型 {sample_type} 的阈值未配置")
    return {"ok": True}


# ── Box Import ───────────────────────────────────────────────────────────────


@app.post("/api/boxes/import/json")
def import_boxes_json(data: BoxImportJSON):
    return _import_boxes(data.boxes)


@app.post("/api/boxes/import/csv")
def import_boxes_csv(file: UploadFile = File(...)):
    content = file.file.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    items = []
    for row in reader:
        temp = row.get("current_temp", "").strip()
        items.append(
            BoxImportItem(
                box_code=row["box_code"].strip(),
                sample_type=row["sample_type"].strip(),
                current_temp=float(temp) if temp else None,
            )
        )
    return _import_boxes(items)


def _import_boxes(items: list[BoxImportItem]):
    imported = []
    rejected = []
    with get_db() as conn:
        existing_codes = {
            r[0] for r in conn.execute("SELECT box_code FROM boxes").fetchall()
        }
        request_codes = set()
        for item in items:
            if item.box_code in existing_codes:
                rejected.append({"box_code": item.box_code, "reason": "箱码已存在"})
                continue
            if item.box_code in request_codes:
                rejected.append({"box_code": item.box_code, "reason": "导入清单内重复"})
                continue
            threshold = conn.execute(
                "SELECT 1 FROM thresholds WHERE sample_type = ?", (item.sample_type,)
            ).fetchone()
            if not threshold:
                rejected.append(
                    {
                        "box_code": item.box_code,
                        "reason": f"样本类型 {item.sample_type} 未配置阈值",
                    }
                )
                continue
            request_codes.add(item.box_code)
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT INTO boxes (box_code, sample_type, current_temp, status, created_at, updated_at) VALUES (?, ?, ?, '待出库', ?, ?)",
                (item.box_code, item.sample_type, item.current_temp, now, now),
            )
            conn.execute(
                "INSERT INTO audit_log (box_code, from_status, to_status, role, operator, reason, temp_at_action, created_at) VALUES (?, NULL, '待出库', '系统', '导入', '导入创建', ?, ?)",
                (item.box_code, item.current_temp, now),
            )
            imported.append(item.box_code)
    return {"imported": imported, "rejected": rejected}


# ── Box Queries ──────────────────────────────────────────────────────────────


@app.get("/api/boxes")
def list_boxes(status: str = None):
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM boxes WHERE status = ?", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM boxes").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/boxes/{box_code}")
def get_box(box_code: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM boxes WHERE box_code = ?", (box_code,)
        ).fetchone()
    if not row:
        raise HTTPException(404, f"箱码 {box_code} 不存在")
    return dict(row)


# ── State Transitions ────────────────────────────────────────────────────────


@app.post("/api/boxes/{box_code}/dispatch")
def dispatch_box(box_code: str, req: TransitionRequest):
    return _transition(box_code, "dispatch", req)


@app.post("/api/boxes/{box_code}/arrive")
def arrive_box(box_code: str, req: TransitionRequest):
    return _transition(box_code, "arrive", req)


@app.post("/api/boxes/{box_code}/receive")
def receive_box(box_code: str, req: TransitionRequest):
    return _transition(box_code, "receive", req)


@app.post("/api/boxes/{box_code}/exception")
def mark_exception(box_code: str, req: TransitionRequest):
    return _transition(box_code, "mark_exception", req)


@app.post("/api/boxes/{box_code}/rollback")
def rollback_box(box_code: str, req: TransitionRequest):
    return _transition(box_code, "rollback", req)


@app.post("/api/boxes/{box_code}/recover")
def recover_box(box_code: str, req: TransitionRequest):
    return _transition(box_code, "recover", req)


def _transition(box_code: str, action: str, req: TransitionRequest):
    rule = TRANSITIONS[action]
    with get_db() as conn:
        box = conn.execute(
            "SELECT * FROM boxes WHERE box_code = ?", (box_code,)
        ).fetchone()
        if not box:
            raise HTTPException(404, f"箱码 {box_code} 不存在")

        current_status = box["status"]

        if current_status in TERMINAL_STATES:
            raise HTTPException(
                409, f"箱码 {box_code} 当前状态「{current_status}」为终态，不可变更"
            )

        if current_status not in rule["from"]:
            raise HTTPException(
                409,
                f"当前状态「{current_status}」不允许执行 {action} 操作（允许的状态: {rule['from']}）",
            )

        if req.role not in rule["roles"]:
            raise HTTPException(
                403,
                f"角色「{req.role}」无权执行 {action} 操作，允许角色: {rule['roles']}",
            )

        target_status = rule["to"]
        temp_violation = False
        timeout_violation = False

        if req.current_temp is not None and action in ("dispatch", "arrive", "receive"):
            threshold = conn.execute(
                "SELECT * FROM thresholds WHERE sample_type = ?",
                (box["sample_type"],),
            ).fetchone()
            if threshold:
                if req.current_temp < threshold["temp_min"] or req.current_temp > threshold["temp_max"]:
                    temp_violation = True
                    target_status = "异常待处理"

        if action == "arrive" and not temp_violation:
            threshold = conn.execute(
                "SELECT * FROM thresholds WHERE sample_type = ?",
                (box["sample_type"],),
            ).fetchone()
            if threshold and box["dispatch_at"]:
                dispatch_time = datetime.fromisoformat(box["dispatch_at"])
                elapsed_minutes = (datetime.now() - dispatch_time).total_seconds() / 60
                if elapsed_minutes > threshold["timeout_minutes"]:
                    timeout_violation = True
                    target_status = "异常待处理"

        now = datetime.now().isoformat()
        updates = {
            "status": target_status,
            "updated_at": now,
        }
        if req.current_temp is not None:
            updates["current_temp"] = req.current_temp

        if action == "dispatch" and not temp_violation:
            updates["dispatch_at"] = now
        if action == "receive" and not temp_violation and not timeout_violation:
            updates["receive_at"] = now

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE boxes SET {set_clause} WHERE box_code = ?",
            list(updates.values()) + [box_code],
        )

        audit_reason = req.reason or ""
        if temp_violation:
            audit_reason = (
                f"[温度越界] 当前温度 {req.current_temp}°C 超出阈值范围。{audit_reason}"
            )
        if timeout_violation:
            audit_reason = (
                f"[转运超时] 转运时间超出配置上限。{audit_reason}"
            )

        conn.execute(
            "INSERT INTO audit_log (box_code, from_status, to_status, role, operator, reason, temp_at_action, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                box_code,
                current_status,
                target_status,
                req.role,
                req.operator,
                audit_reason,
                req.current_temp,
                now,
            ),
        )

    result = {
        "ok": True,
        "box_code": box_code,
        "from": current_status,
        "to": target_status,
    }
    if temp_violation:
        result["warning"] = "温度越界，已自动转入异常待处理"
    if timeout_violation:
        result["warning"] = "转运超时，已自动转入异常待处理"
    return result


# ── Audit ────────────────────────────────────────────────────────────────────


@app.get("/api/audit")
def list_audit(box_code: str = None):
    with get_db() as conn:
        if box_code:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE box_code = ? ORDER BY id",
                (box_code,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    return [dict(r) for r in rows]


# ── Export ────────────────────────────────────────────────────────────────────


@app.get("/api/export/json")
def export_json():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM boxes").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/export/csv")
def export_csv():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM boxes").fetchall()
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=boxes.csv"},
    )
