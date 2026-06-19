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
    BatchCreate,
    BatchReceiveRequest,
    MissingBoxRegisterRequest,
    MissingBoxCancelRequest,
    BatchTransitionRequest,
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

BATCH_TRANSITIONS = {
    "dispatch": {
        "from": ["待出库"],
        "to": "转运中",
        "roles": ["出库员"],
    },
    "arrive": {
        "from": ["转运中", "待签收", "部分签收"],
        "to": "待签收",
        "roles": ["转运员"],
    },
    "receive": {
        "from": ["待签收", "部分签收"],
        "to": "已签收",
        "roles": ["库房签收员"],
    },
    "mark_exception": {
        "from": ["待出库", "转运中", "待签收", "部分签收"],
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

BATCH_TERMINAL_STATES = {"已签收", "已回退"}


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
    return _import_boxes(
        data.boxes,
        batch_no=data.batch_no,
        scheduled_outbound_time=data.scheduled_outbound_time,
        estimated_arrival_deadline=data.estimated_arrival_deadline,
    )


@app.post("/api/boxes/import/csv")
def import_boxes_csv(
    file: UploadFile = File(...),
    batch_no: str = None,
    scheduled_outbound_time: str = None,
    estimated_arrival_deadline: str = None,
):
    content = file.file.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    items = []
    for row in reader:
        temp = row.get("current_temp", "").strip()
        item_batch = row.get("batch_no", "").strip() or batch_no
        items.append(
            BoxImportItem(
                box_code=row["box_code"].strip(),
                sample_type=row["sample_type"].strip(),
                current_temp=float(temp) if temp else None,
                batch_no=item_batch if item_batch else None,
            )
        )
    return _import_boxes(
        items,
        batch_no=batch_no,
        scheduled_outbound_time=scheduled_outbound_time,
        estimated_arrival_deadline=estimated_arrival_deadline,
    )


def _import_boxes(items: list[BoxImportItem], batch_no: str = None,
                  scheduled_outbound_time: str = None,
                  estimated_arrival_deadline: str = None):
    imported = []
    rejected = []
    with get_db() as conn:
        existing_codes = {
            r[0] for r in conn.execute("SELECT box_code FROM boxes").fetchall()
        }
        request_codes = set()

        batch_sample_type = None
        batch_info = None
        if batch_no:
            batch_info = conn.execute(
                "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            if batch_info:
                batch_sample_type = batch_info["sample_type"]
                if batch_info["status"] in BATCH_TERMINAL_STATES:
                    raise HTTPException(
                        409,
                        f"批次 {batch_no} 当前状态「{batch_info['status']}」为终态，不可新增箱子",
                    )

        for item in items:
            item_batch = item.batch_no or batch_no

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

            if item_batch:
                if batch_sample_type is None:
                    batch_sample_type = item.sample_type
                elif batch_sample_type != item.sample_type:
                    rejected.append(
                        {
                            "box_code": item.box_code,
                            "reason": f"样本类型冲突：批次 {item_batch} 为「{batch_sample_type}」，该箱为「{item.sample_type}」",
                        }
                    )
                    continue

                existing_batch_box = conn.execute(
                    """
                    SELECT bb.batch_no FROM batch_boxes bb
                    JOIN batches b ON bb.batch_no = b.batch_no
                    WHERE bb.box_code = ? AND b.status NOT IN ('已签收', '已回退')
                    """,
                    (item.box_code,),
                ).fetchone()
                if existing_batch_box and existing_batch_box["batch_no"] != item_batch:
                    rejected.append(
                        {
                            "box_code": item.box_code,
                            "reason": f"箱子已在未完成批次 {existing_batch_box['batch_no']} 中",
                        }
                    )
                    continue

            request_codes.add(item.box_code)
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT INTO boxes (box_code, sample_type, current_temp, status, created_at, updated_at, batch_no) VALUES (?, ?, ?, '待出库', ?, ?, ?)",
                (item.box_code, item.sample_type, item.current_temp, now, now, item_batch),
            )
            conn.execute(
                "INSERT INTO audit_log (box_code, from_status, to_status, role, operator, reason, temp_at_action, created_at, batch_no) VALUES (?, NULL, '待出库', '系统', '导入', '导入创建', ?, ?, ?)",
                (item.box_code, item.current_temp, now, item_batch),
            )

            if item_batch:
                conn.execute(
                    "INSERT OR IGNORE INTO batch_boxes (batch_no, box_code, box_batch_status) VALUES (?, ?, '正常')",
                    (item_batch, item.box_code),
                )

            imported.append(item.box_code)

        if batch_no and imported:
            now = datetime.now().isoformat()
            if not batch_info:
                conn.execute(
                    """
                    INSERT INTO batches (batch_no, sample_type, status, scheduled_outbound_time,
                                         estimated_arrival_deadline, total_boxes, received_boxes,
                                         missing_boxes, created_at, updated_at, created_by)
                    VALUES (?, ?, '待出库', ?, ?, ?, 0, 0, ?, ?, '系统导入')
                    """,
                    (batch_no, batch_sample_type, scheduled_outbound_time,
                     estimated_arrival_deadline, len(imported), now, now),
                )
                _log_batch_audit(conn, batch_no, None, "创建批次",
                                 None, "待出库", "系统", "导入",
                                 f"导入创建批次，共 {len(imported)} 箱", None, now)
            else:
                total = conn.execute(
                    "SELECT COUNT(*) as cnt FROM batch_boxes WHERE batch_no = ?",
                    (batch_no,),
                ).fetchone()["cnt"]
                conn.execute(
                    "UPDATE batches SET total_boxes = ?, updated_at = ? WHERE batch_no = ?",
                    (total, now, batch_no),
                )
                _log_batch_audit(conn, batch_no, None, "新增箱子",
                                 None, None, "系统", "导入",
                                 f"批次新增 {len(imported)} 箱，当前共 {total} 箱", None, now)

    return {"imported": imported, "rejected": rejected}


def _log_batch_audit(conn, batch_no, box_code, action, from_status, to_status,
                     role, operator, reason, detail, created_at):
    conn.execute(
        """
        INSERT INTO batch_audit_log (batch_no, box_code, action, from_status, to_status,
                                     role, operator, reason, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (batch_no, box_code, action, from_status, to_status,
         role, operator, reason, detail, created_at),
    )


def _update_batch_stats(conn, batch_no):
    stats = conn.execute(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN bb.box_batch_status = '缺失' THEN 1 ELSE 0 END) as missing_cnt,
            SUM(CASE WHEN b.status = '已签收' THEN 1 ELSE 0 END) as received_cnt
        FROM batch_boxes bb
        JOIN boxes b ON bb.box_code = b.box_code
        WHERE bb.batch_no = ?
        """,
        (batch_no,),
    ).fetchone()
    now = datetime.now().isoformat()
    conn.execute(
        """
        UPDATE batches SET total_boxes = ?, received_boxes = ?, missing_boxes = ?, updated_at = ?
        WHERE batch_no = ?
        """,
        (stats["total"], stats["received_cnt"], stats["missing_cnt"], now, batch_no),
    )
    return stats


def _action_label(action: str) -> str:
    labels = {
        "dispatch": "出库",
        "arrive": "到达",
        "receive": "签收",
        "mark_exception": "标记异常",
        "rollback": "回退",
        "recover": "恢复",
    }
    return labels.get(action, action)


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
            "INSERT INTO audit_log (box_code, from_status, to_status, role, operator, reason, temp_at_action, created_at, batch_no) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                box_code,
                current_status,
                target_status,
                req.role,
                req.operator,
                audit_reason,
                req.current_temp,
                now,
                box["batch_no"],
            ),
        )

        if box["batch_no"]:
            stats = _update_batch_stats(conn, box["batch_no"])
            _log_batch_audit(
                conn, box["batch_no"], box_code,
                f"单箱{_action_label(action)}",
                current_status, target_status,
                req.role, req.operator, audit_reason,
                f"批次统计: 共{stats['total']}箱, 已签收{stats['received_cnt']}箱, 缺失{stats['missing_cnt']}箱",
                now,
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


EXPORT_FIELDS = [
    "box_code",
    "sample_type",
    "sequence",
    "from_status",
    "to_status",
    "role",
    "operator",
    "reason",
    "temp_at_action",
    "action_at",
    "current_status",
]


@app.get("/api/export/csv")
def export_csv(batch_no: str = None):
    rows = _build_export_rows(batch_no=batch_no)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cold_chain_history.csv"},
    )


# ── Batch Management ─────────────────────────────────────────────────────────


@app.post("/api/batches")
def create_batch(data: BatchCreate):
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (data.batch_no,)
        ).fetchone()
        if existing:
            raise HTTPException(409, f"批次 {data.batch_no} 已存在")

        threshold = conn.execute(
            "SELECT 1 FROM thresholds WHERE sample_type = ?", (data.sample_type,)
        ).fetchone()
        if not threshold:
            raise HTTPException(
                400, f"样本类型 {data.sample_type} 未配置阈值，无法创建批次"
            )

        now = datetime.now().isoformat()
        conn.execute(
            """
            INSERT INTO batches (batch_no, sample_type, status, scheduled_outbound_time,
                                 estimated_arrival_deadline, total_boxes, received_boxes,
                                 missing_boxes, created_at, updated_at, created_by)
            VALUES (?, ?, '待出库', ?, ?, 0, 0, 0, ?, ?, ?)
            """,
            (data.batch_no, data.sample_type, data.scheduled_outbound_time,
             data.estimated_arrival_deadline, now, now, data.operator or "系统"),
        )
        _log_batch_audit(conn, data.batch_no, None, "创建批次",
                         None, "待出库", "系统", data.operator or "系统",
                         "手动创建批次", None, now)
    return {"ok": True, "batch_no": data.batch_no}


@app.get("/api/batches")
def list_batches(status: str = None):
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM batches WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/batches/{batch_no}")
def get_batch(batch_no: str):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        boxes = conn.execute(
            """
            SELECT b.box_code, b.sample_type, b.status, bb.box_batch_status,
                   bb.received_at, bb.missing_reason, bb.missing_registered_at,
                   bb.missing_registered_by, bb.missing_cancelled_at,
                   bb.missing_cancelled_by, bb.missing_cancel_reason
            FROM batch_boxes bb
            JOIN boxes b ON bb.box_code = b.box_code
            WHERE bb.batch_no = ?
            ORDER BY bb.id
            """,
            (batch_no,),
        ).fetchall()

        pending = [
            r["box_code"] for r in boxes
            if r["status"] not in TERMINAL_STATES and r["box_batch_status"] != "缺失"
        ]

    return {
        "batch": dict(batch),
        "boxes": [dict(r) for r in boxes],
        "pending_todos": pending,
    }


@app.post("/api/batches/{batch_no}/dispatch")
def dispatch_batch(batch_no: str, req: BatchTransitionRequest):
    return _batch_transition(batch_no, "dispatch", req)


@app.post("/api/batches/{batch_no}/arrive")
def arrive_batch(batch_no: str, req: BatchTransitionRequest):
    return _batch_transition(batch_no, "arrive", req)


@app.post("/api/batches/{batch_no}/exception")
def batch_mark_exception(batch_no: str, req: BatchTransitionRequest):
    return _batch_transition(batch_no, "mark_exception", req)


@app.post("/api/batches/{batch_no}/rollback")
def batch_rollback(batch_no: str, req: BatchTransitionRequest):
    return _batch_transition(batch_no, "rollback", req)


@app.post("/api/batches/{batch_no}/recover")
def batch_recover(batch_no: str, req: BatchTransitionRequest):
    return _batch_transition(batch_no, "recover", req)


def _batch_transition(batch_no: str, action: str, req: BatchTransitionRequest):
    rule = BATCH_TRANSITIONS[action]
    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        current_status = batch["status"]

        if current_status in BATCH_TERMINAL_STATES:
            raise HTTPException(
                409, f"批次 {batch_no} 当前状态「{current_status}」为终态，不可变更"
            )

        if current_status not in rule["from"]:
            raise HTTPException(
                409,
                f"批次当前状态「{current_status}」不允许执行 {action} 操作（允许的状态: {rule['from']}）",
            )

        if req.role not in rule["roles"]:
            raise HTTPException(
                403,
                f"角色「{req.role}」无权执行批次 {action} 操作，允许角色: {rule['roles']}",
            )

        box_rows = conn.execute(
            "SELECT bb.box_code FROM batch_boxes bb WHERE bb.batch_no = ?",
            (batch_no,),
        ).fetchall()
        if not box_rows:
            raise HTTPException(400, f"批次 {batch_no} 中没有箱子")

        target_status = rule["to"]
        now = datetime.now().isoformat()
        success_count = 0
        fail_count = 0
        skip_count = 0
        temp_violation_any = False
        timeout_violation_any = False

        for br in box_rows:
            box_code = br["box_code"]
            box = conn.execute(
                "SELECT * FROM boxes WHERE box_code = ?", (box_code,)
            ).fetchone()
            if not box:
                fail_count += 1
                continue

            box_current = box["status"]
            if box_current in TERMINAL_STATES:
                skip_count += 1
                continue

            box_rule = TRANSITIONS[action]
            if box_current not in box_rule["from"]:
                if action == "arrive" and box_current in ("待签收", "已签收", "部分签收"):
                    skip_count += 1
                    continue
                if action == "dispatch" and box_current in ("转运中", "待签收", "已签收"):
                    skip_count += 1
                    continue
                if action == "receive" and box_current == "已签收":
                    skip_count += 1
                    continue
                fail_count += 1
                continue

            box_target = box_rule["to"]
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
                        box_target = "异常待处理"
                        temp_violation_any = True

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
                        box_target = "异常待处理"
                        timeout_violation_any = True

            updates = {
                "status": box_target,
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
                audit_reason = f"[温度越界] 当前温度 {req.current_temp}°C 超出阈值范围。{audit_reason}"
            if timeout_violation:
                audit_reason = f"[转运超时] 转运时间超出配置上限。{audit_reason}"

            conn.execute(
                """
                INSERT INTO audit_log (box_code, from_status, to_status, role, operator,
                                       reason, temp_at_action, created_at, batch_no)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (box_code, box_current, box_target, req.role, req.operator,
                 audit_reason, req.current_temp, now, batch_no),
            )
            success_count += 1

        if temp_violation_any or timeout_violation_any:
            batch_target = "异常待处理"
        else:
            stats = _update_batch_stats(conn, batch_no)
            has_missing = stats["missing_cnt"] > 0
            all_received = stats["received_cnt"] >= stats["total"]
            has_partial = stats["received_cnt"] > 0 and not all_received

            if all_received and not has_missing:
                batch_target = "已签收"
            elif has_partial or has_missing:
                batch_target = "部分签收"
            else:
                batch_target = target_status

        conn.execute(
            "UPDATE batches SET status = ?, updated_at = ? WHERE batch_no = ?",
            (batch_target, now, batch_no),
        )

        stats = _update_batch_stats(conn, batch_no)
        _log_batch_audit(
            conn, batch_no, None, f"批次{_action_label(action)}",
            current_status, batch_target,
            req.role, req.operator, req.reason,
            f"成功 {success_count} 箱, 跳过 {skip_count} 箱, 失败 {fail_count} 箱, 共{stats['total']}箱, 已签收{stats['received_cnt']}箱, 缺失{stats['missing_cnt']}箱",
            now,
        )

    result = {
        "ok": True,
        "batch_no": batch_no,
        "from": current_status,
        "to": batch_target,
        "success_count": success_count,
        "skip_count": skip_count,
        "fail_count": fail_count,
    }
    if temp_violation_any:
        result["warning"] = "存在温度越界箱子，批次已自动转入异常待处理"
    if timeout_violation_any:
        result["warning"] = "存在转运超时箱子，批次已自动转入异常待处理"
    return result


@app.post("/api/batches/{batch_no}/receive")
def receive_batch(batch_no: str, req: BatchReceiveRequest):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        current_status = batch["status"]
        if current_status in BATCH_TERMINAL_STATES:
            raise HTTPException(
                409, f"批次 {batch_no} 当前状态「{current_status}」为终态，不可变更"
            )
        if current_status not in ("待签收", "部分签收"):
            raise HTTPException(
                409,
                f"批次当前状态「{current_status}」不允许签收（允许的状态: 待签收, 部分签收）",
            )

        if req.role != "库房签收员":
            raise HTTPException(
                403,
                f"角色「{req.role}」无权执行批次签收操作，允许角色: ['库房签收员']",
            )

        batch_boxes = conn.execute(
            """
            SELECT bb.box_code, bb.box_batch_status, b.status
            FROM batch_boxes bb
            JOIN boxes b ON bb.box_code = b.box_code
            WHERE bb.batch_no = ?
            """,
            (batch_no,),
        ).fetchall()
        batch_box_codes = {r["box_code"]: r for r in batch_boxes}

        for bc in req.received_boxes:
            if bc not in batch_box_codes:
                raise HTTPException(400, f"箱子 {bc} 不在批次 {batch_no} 中")

        missing_set = set(req.missing_boxes or [])
        received_set = set(req.received_boxes)
        overlap = received_set & missing_set
        if overlap:
            raise HTTPException(
                400,
                f"箱子不能同时被签收和标记为缺失: {', '.join(overlap)}",
            )

        now = datetime.now().isoformat()
        received_count = 0
        skip_count = 0
        missing_registered_count = 0
        temp_violation_any = False

        for box_code in req.received_boxes:
            box_info = batch_box_codes[box_code]
            if box_info["status"] == "已签收":
                skip_count += 1
                continue
            if box_info["box_batch_status"] == "缺失":
                raise HTTPException(
                    409,
                    f"箱子 {box_code} 已登记为缺失，请先撤销缺失登记后再签收",
                )
            if box_info["status"] != "待签收":
                raise HTTPException(
                    409,
                    f"箱子 {box_code} 当前状态「{box_info['status']}」不允许签收",
                )

            threshold = conn.execute(
                "SELECT * FROM thresholds WHERE sample_type = ?",
                (batch["sample_type"],),
            ).fetchone()
            temp_violation = False
            if threshold and req.reason is not None and "温度越界" in req.reason:
                temp_violation = True
                temp_violation_any = True

            conn.execute(
                """
                UPDATE boxes SET status = '已签收', receive_at = ?, updated_at = ?
                WHERE box_code = ?
                """,
                (now, now, box_code),
            )
            conn.execute(
                """
                UPDATE batch_boxes SET box_batch_status = '正常', received_at = ?,
                       missing_reason = NULL, missing_registered_at = NULL,
                       missing_registered_by = NULL
                WHERE batch_no = ? AND box_code = ?
                """,
                (now, batch_no, box_code),
            )
            conn.execute(
                """
                INSERT INTO audit_log (box_code, from_status, to_status, role, operator,
                                       reason, temp_at_action, created_at, batch_no)
                VALUES (?, '待签收', '已签收', ?, ?, ?, ?, ?, ?)
                """,
                (box_code, req.role, req.operator, req.reason or "批次签收",
                 None, now, batch_no),
            )
            received_count += 1

        if req.missing_boxes:
            for box_code in req.missing_boxes:
                box_info = batch_box_codes[box_code]
                if box_info["status"] == "已签收":
                    raise HTTPException(
                        409, f"箱子 {box_code} 已签收，不能标记为缺失"
                    )
                if box_info["box_batch_status"] == "缺失":
                    continue

                conn.execute(
                    """
                    UPDATE batch_boxes SET box_batch_status = '缺失',
                           missing_reason = ?, missing_registered_at = ?,
                           missing_registered_by = ?
                    WHERE batch_no = ? AND box_code = ?
                    """,
                    (req.missing_reason or "未说明原因", now, req.operator,
                     batch_no, box_code),
                )
                missing_registered_count += 1
                _log_batch_audit(
                    conn, batch_no, box_code, "登记缺失",
                    box_info["status"], None,
                    req.role, req.operator, req.missing_reason,
                    f"箱子 {box_code} 登记为缺失",
                    now,
                )

        stats = _update_batch_stats(conn, batch_no)

        all_received = stats["received_cnt"] >= stats["total"]
        has_missing = stats["missing_cnt"] > 0

        if all_received and not has_missing:
            batch_target = "已签收"
        elif has_missing or stats["received_cnt"] > 0:
            batch_target = "部分签收"
        else:
            batch_target = "待签收"

        conn.execute(
            "UPDATE batches SET status = ?, updated_at = ? WHERE batch_no = ?",
            (batch_target, now, batch_no),
        )

        _log_batch_audit(
            conn, batch_no, None, "批次签收",
            current_status, batch_target,
            req.role, req.operator, req.reason,
            f"签收 {received_count} 箱, 跳过 {skip_count} 箱, 登记缺失 {missing_registered_count} 箱, "
            f"共{stats['total']}箱, 已签收{stats['received_cnt']}箱, 缺失{stats['missing_cnt']}箱",
            now,
        )

    return {
        "ok": True,
        "batch_no": batch_no,
        "from": current_status,
        "to": batch_target,
        "received_count": received_count,
        "skip_count": skip_count,
        "missing_registered_count": missing_registered_count,
        "total_boxes": stats["total"],
        "received_boxes": stats["received_cnt"],
        "missing_boxes": stats["missing_cnt"],
    }


@app.post("/api/batches/{batch_no}/missing")
def register_missing_boxes(batch_no: str, req: MissingBoxRegisterRequest):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        if batch["status"] in BATCH_TERMINAL_STATES:
            raise HTTPException(
                409, f"批次 {batch_no} 为终态，不可登记缺失"
            )

        if req.role not in ("库房签收员", "管理员"):
            raise HTTPException(
                403,
                f"角色「{req.role}」无权登记缺失，允许角色: ['库房签收员', '管理员']",
            )

        batch_boxes = conn.execute(
            """
            SELECT bb.box_code, bb.box_batch_status, b.status
            FROM batch_boxes bb
            JOIN boxes b ON bb.box_code = b.box_code
            WHERE bb.batch_no = ?
            """,
            (batch_no,),
        ).fetchall()
        batch_box_map = {r["box_code"]: r for r in batch_boxes}

        now = datetime.now().isoformat()
        registered = 0
        for box_code in req.box_codes:
            if box_code not in batch_box_map:
                raise HTTPException(400, f"箱子 {box_code} 不在批次 {batch_no} 中")
            info = batch_box_map[box_code]
            if info["status"] == "已签收":
                raise HTTPException(409, f"箱子 {box_code} 已签收，不能标记为缺失")
            if info["box_batch_status"] == "缺失":
                continue

            conn.execute(
                """
                UPDATE batch_boxes SET box_batch_status = '缺失',
                       missing_reason = ?, missing_registered_at = ?,
                       missing_registered_by = ?
                WHERE batch_no = ? AND box_code = ?
                """,
                (req.reason, now, req.operator, batch_no, box_code),
            )
            registered += 1
            _log_batch_audit(
                conn, batch_no, box_code, "登记缺失",
                info["status"], None,
                req.role, req.operator, req.reason,
                f"箱子 {box_code} 登记为缺失",
                now,
            )

        stats = _update_batch_stats(conn, batch_no)
        has_missing = stats["missing_cnt"] > 0
        batch_target = batch["status"]
        if has_missing and batch["status"] in ("待签收", "部分签收"):
            batch_target = "部分签收"

        if batch_target != batch["status"]:
            conn.execute(
                "UPDATE batches SET status = ?, updated_at = ? WHERE batch_no = ?",
                (batch_target, now, batch_no),
            )
            _log_batch_audit(
                conn, batch_no, None, "批次状态变更",
                batch["status"], batch_target,
                req.role, req.operator, req.reason,
                f"登记缺失后批次状态变更",
                now,
            )

    return {
        "ok": True,
        "batch_no": batch_no,
        "registered_count": registered,
        "total_boxes": stats["total"],
        "received_boxes": stats["received_cnt"],
        "missing_boxes": stats["missing_cnt"],
        "batch_status": batch_target,
    }


@app.post("/api/batches/{batch_no}/cancel_missing")
def cancel_missing_boxes(batch_no: str, req: MissingBoxCancelRequest):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        if req.role != "管理员":
            raise HTTPException(
                403,
                f"角色「{req.role}」无权撤销缺失登记，允许角色: ['管理员']",
            )

        batch_boxes = conn.execute(
            """
            SELECT bb.box_code, bb.box_batch_status, b.status
            FROM batch_boxes bb
            JOIN boxes b ON bb.box_code = b.box_code
            WHERE bb.batch_no = ?
            """,
            (batch_no,),
        ).fetchall()
        batch_box_map = {r["box_code"]: r for r in batch_boxes}

        now = datetime.now().isoformat()
        cancelled = 0
        for box_code in req.box_codes:
            if box_code not in batch_box_map:
                raise HTTPException(400, f"箱子 {box_code} 不在批次 {batch_no} 中")
            info = batch_box_map[box_code]
            if info["box_batch_status"] != "缺失":
                continue

            conn.execute(
                """
                UPDATE batch_boxes SET box_batch_status = '正常',
                       missing_reason = NULL, missing_registered_at = NULL,
                       missing_registered_by = NULL,
                       missing_cancelled_at = ?, missing_cancelled_by = ?,
                       missing_cancel_reason = ?
                WHERE batch_no = ? AND box_code = ?
                """,
                (now, req.operator, req.reason, batch_no, box_code),
            )
            cancelled += 1
            _log_batch_audit(
                conn, batch_no, box_code, "撤销缺失登记",
                None, None,
                req.role, req.operator, req.reason,
                f"管理员撤销箱子 {box_code} 的缺失登记，原因: {req.reason or '未说明'}",
                now,
            )

        stats = _update_batch_stats(conn, batch_no)
        has_missing = stats["missing_cnt"] > 0
        all_received = stats["received_cnt"] >= stats["total"]
        has_partial = stats["received_cnt"] > 0 and not all_received

        batch_target = batch["status"]
        if not has_missing and all_received:
            batch_target = "已签收"
        elif not has_missing and has_partial:
            batch_target = "部分签收"
        elif not has_missing and not has_partial:
            batch_target = "待签收"

        if batch_target != batch["status"]:
            conn.execute(
                "UPDATE batches SET status = ?, updated_at = ? WHERE batch_no = ?",
                (batch_target, now, batch_no),
            )
            _log_batch_audit(
                conn, batch_no, None, "批次状态变更",
                batch["status"], batch_target,
                req.role, req.operator, req.reason,
                f"撤销缺失登记后批次状态变更",
                now,
            )

    return {
        "ok": True,
        "batch_no": batch_no,
        "cancelled_count": cancelled,
        "total_boxes": stats["total"],
        "received_boxes": stats["received_cnt"],
        "missing_boxes": stats["missing_cnt"],
        "batch_status": batch_target,
    }


@app.get("/api/batches/{batch_no}/audit")
def get_batch_audit(batch_no: str):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT 1 FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        rows = conn.execute(
            "SELECT * FROM batch_audit_log WHERE batch_no = ? ORDER BY id",
            (batch_no,),
        ).fetchall()
    return [dict(r) for r in rows]


def _build_export_rows(batch_no: str = None):
    with get_db() as conn:
        if batch_no:
            audits = conn.execute(
                "SELECT * FROM audit_log WHERE batch_no = ? ORDER BY box_code, id",
                (batch_no,),
            ).fetchall()
        else:
            audits = conn.execute(
                "SELECT * FROM audit_log ORDER BY box_code, id"
            ).fetchall()
        boxes = {
            r["box_code"]: dict(r)
            for r in conn.execute("SELECT * FROM boxes").fetchall()
        }
    rows = []
    seq_by_box = {}
    for a in audits:
        bc = a["box_code"]
        seq_by_box[bc] = seq_by_box.get(bc, 0) + 1
        box_info = boxes.get(bc, {})
        rows.append(
            {
                "box_code": bc,
                "sample_type": box_info.get("sample_type", ""),
                "sequence": seq_by_box[bc],
                "from_status": a["from_status"] or "",
                "to_status": a["to_status"],
                "role": a["role"],
                "operator": a["operator"],
                "reason": a["reason"] or "",
                "temp_at_action": "" if a["temp_at_action"] is None else a["temp_at_action"],
                "action_at": a["created_at"],
                "current_status": box_info.get("status", ""),
            }
        )
    return rows


@app.get("/api/export/json")
def export_json(batch_no: str = None):
    result = {
        "generated_at": datetime.now().isoformat(),
        "fields": EXPORT_FIELDS,
        "rows": _build_export_rows(batch_no=batch_no),
    }
    if batch_no:
        with get_db() as conn:
            batch = conn.execute(
                "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            if batch:
                boxes = conn.execute(
                    """
                    SELECT bb.box_code, bb.box_batch_status, b.status,
                           bb.received_at, bb.missing_reason, bb.missing_registered_at,
                           bb.missing_registered_by, bb.missing_cancelled_at,
                           bb.missing_cancelled_by, bb.missing_cancel_reason
                    FROM batch_boxes bb
                    JOIN boxes b ON bb.box_code = b.box_code
                    WHERE bb.batch_no = ?
                    ORDER BY bb.id
                    """,
                    (batch_no,),
                ).fetchall()

                pending = [
                    r["box_code"] for r in boxes
                    if r["status"] not in TERMINAL_STATES and r["box_batch_status"] != "缺失"
                ]

                batch_audit = conn.execute(
                    "SELECT * FROM batch_audit_log WHERE batch_no = ? ORDER BY id",
                    (batch_no,),
                ).fetchall()

                result["batch_summary"] = {
                    "batch_no": batch["batch_no"],
                    "sample_type": batch["sample_type"],
                    "status": batch["status"],
                    "total_boxes": batch["total_boxes"],
                    "received_boxes": batch["received_boxes"],
                    "missing_boxes": batch["missing_boxes"],
                    "pending_boxes": len(pending),
                    "pending_todos": pending,
                    "scheduled_outbound_time": batch["scheduled_outbound_time"],
                    "estimated_arrival_deadline": batch["estimated_arrival_deadline"],
                    "created_at": batch["created_at"],
                    "updated_at": batch["updated_at"],
                    "created_by": batch["created_by"],
                }
                result["batch_boxes"] = [dict(r) for r in boxes]
                result["batch_audit_log"] = [dict(r) for r in batch_audit]
    return result
