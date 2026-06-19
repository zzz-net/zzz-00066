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
    ReviewConfigUpdate,
    ReviewInitiateRequest,
    ReviewBoxRequest,
    ReviewCancelRequest,
    ArchiveRequest,
    DisputeConfigUpdate,
    DisputeTicketCreate,
    DisputeConfirmRequest,
    DisputeRejectRequest,
    DisputeWithdrawRequest,
    DisputeReopenRequest,
    DisputeResubmitRequest,
    DisputeCloseRequest,
    DisputeEvidenceRequest,
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

BATCH_TERMINAL_STATES = {"已回退", "已归档"}
BATCH_LOCKED_AFTER_RECEIVE = {"已签收"}

VALID_REVIEW_RESULTS = {"通过", "破损", "温控待确认"}


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
                if batch_info["status"] in BATCH_TERMINAL_STATES or batch_info["status"] in BATCH_LOCKED_AFTER_RECEIVE:
                    raise HTTPException(
                        409,
                        f"批次 {batch_no} 当前状态「{batch_info['status']}」已完成签收，不可新增箱子",
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


def _get_review_config(conn):
    row = conn.execute("SELECT * FROM review_config WHERE id = 1").fetchone()
    if not row:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO review_config (id, require_double_review, updated_at, updated_by) VALUES (1, 0, ?, '系统初始化')",
            (now,),
        )
        row = conn.execute("SELECT * FROM review_config WHERE id = 1").fetchone()
    return row


def _get_active_review(conn, batch_no):
    return conn.execute(
        "SELECT * FROM batch_reviews WHERE batch_no = ? AND status = '进行中'",
        (batch_no,),
    ).fetchone()


def _compute_review_progress(conn, review_id, require_double_review):
    boxes = conn.execute(
        "SELECT * FROM batch_review_boxes WHERE review_id = ?",
        (review_id,),
    ).fetchall()
    total = len(boxes)
    first_done = 0
    second_done = 0
    final_done = 0
    pending_first = []
    pending_second = []
    pending_temp_confirm = []

    for b in boxes:
        if b["first_review_result"]:
            first_done += 1
        else:
            pending_first.append(b["box_code"])

        if require_double_review:
            if b["second_review_result"]:
                second_done += 1
            elif b["first_review_result"]:
                pending_second.append(b["box_code"])
            if b["final_result"] and b["final_result"] != "温控待确认":
                final_done += 1
            if b["final_result"] == "温控待确认" or b["first_review_result"] == "温控待确认" or b["second_review_result"] == "温控待确认":
                pending_temp_confirm.append(b["box_code"])
        else:
            if b["first_review_result"] and b["first_review_result"] != "温控待确认":
                final_done += 1
            if b["first_review_result"] == "温控待确认":
                pending_temp_confirm.append(b["box_code"])

    all_done = False
    if require_double_review:
        all_done = (second_done == total) and (len(pending_temp_confirm) == 0)
    else:
        all_done = (first_done == total) and (len(pending_temp_confirm) == 0)

    return {
        "total_boxes": total,
        "first_review_done": first_done,
        "second_review_done": second_done,
        "final_review_done": final_done,
        "pending_first_review": pending_first,
        "pending_second_review": pending_second,
        "pending_temp_confirmation": pending_temp_confirm,
        "all_reviewed": all_done,
    }


def _check_review_conflict_on_receive(conn, batch_no):
    active = _get_active_review(conn, batch_no)
    if active:
        snapshot_boxes = {
            r["box_code"] for r in conn.execute(
                "SELECT box_code FROM batch_review_boxes WHERE review_id = ?",
                (active["id"],),
            ).fetchall()
        }
        current_received = {
            r["box_code"] for r in conn.execute(
                """
                SELECT bb.box_code FROM batch_boxes bb
                JOIN boxes b ON bb.box_code = b.box_code
                WHERE bb.batch_no = ? AND b.status = '已签收'
                """,
                (batch_no,),
            ).fetchall()
        }
        new_boxes = current_received - snapshot_boxes
        if new_boxes:
            raise HTTPException(
                409,
                f"批次 {batch_no} 正在复核中，复核启动后新签收了 {len(new_boxes)} 箱: {', '.join(sorted(new_boxes))}。"
                f"请先撤销当前复核再重新发起。"
            )


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

        result = {
            "batch": dict(batch),
            "boxes": [dict(r) for r in boxes],
            "pending_todos": pending,
        }

        active_review = _get_active_review(conn, batch_no)
        if active_review:
            review_boxes = conn.execute(
                "SELECT * FROM batch_review_boxes WHERE review_id = ? ORDER BY id",
                (active_review["id"],),
            ).fetchall()
            progress = _compute_review_progress(
                conn, active_review["id"], bool(active_review["require_double_review"])
            )
            result["review"] = {
                "review_id": active_review["id"],
                "status": active_review["status"],
                "require_double_review": bool(active_review["require_double_review"]),
                "initiated_by": active_review["initiated_by"],
                "initiated_at": active_review["initiated_at"],
                "handed_over_by": active_review["handed_over_by"],
                "boxes": [dict(r) for r in review_boxes],
                "progress": progress,
            }
        else:
            last_review = conn.execute(
                "SELECT * FROM batch_reviews WHERE batch_no = ? ORDER BY id DESC LIMIT 1",
                (batch_no,),
            ).fetchone()
            if last_review:
                review_boxes = conn.execute(
                    "SELECT * FROM batch_review_boxes WHERE review_id = ? ORDER BY id",
                    (last_review["id"],),
                ).fetchall()
                result["review"] = {
                    "review_id": last_review["id"],
                    "status": last_review["status"],
                    "require_double_review": bool(last_review["require_double_review"]),
                    "initiated_by": last_review["initiated_by"],
                    "initiated_at": last_review["initiated_at"],
                    "handed_over_by": last_review["handed_over_by"],
                    "cancelled_at": last_review["cancelled_at"],
                    "cancelled_by": last_review["cancelled_by"],
                    "cancelled_reason": last_review["cancelled_reason"],
                    "completed_at": last_review["completed_at"],
                    "boxes": [dict(r) for r in review_boxes],
                }

    return result


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

        if current_status in BATCH_LOCKED_AFTER_RECEIVE and action != "receive":
            raise HTTPException(
                409,
                f"批次 {batch_no} 当前状态「{current_status}」已签收，仅允许签收（补签）操作"
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
            all_handled = (stats["received_cnt"] + stats["missing_cnt"]) >= stats["total"] and stats["total"] > 0

            if all_received and not has_missing:
                batch_target = "已签收"
            elif all_handled:
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
        if current_status not in ("待签收", "部分签收", "已签收"):
            raise HTTPException(
                409,
                f"批次当前状态「{current_status}」不允许签收（允许的状态: 待签收, 部分签收, 已签收）",
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
        all_handled = (stats["received_cnt"] + stats["missing_cnt"]) >= stats["total"] and stats["total"] > 0

        if all_received and not has_missing:
            batch_target = "已签收"
        elif all_handled:
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
        all_handled = (stats["received_cnt"] + stats["missing_cnt"]) >= stats["total"] and stats["total"] > 0

        batch_target = batch["status"]
        if not has_missing and all_received:
            batch_target = "已签收"
        elif all_handled:
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
                    "review_status": batch["review_status"],
                    "archived_at": batch["archived_at"],
                    "archived_by": batch["archived_by"],
                }
                result["batch_boxes"] = [dict(r) for r in boxes]
                result["batch_audit_log"] = [dict(r) for r in batch_audit]

                review = conn.execute(
                    "SELECT * FROM batch_reviews WHERE batch_no = ? AND status = '进行中'",
                    (batch_no,),
                ).fetchone()
                if review:
                    review_boxes = conn.execute(
                        "SELECT * FROM batch_review_boxes WHERE review_id = ?",
                        (review["id"],),
                    ).fetchall()
                    result["current_review"] = {
                        "review_id": review["id"],
                        "status": review["status"],
                        "require_double_review": bool(review["require_double_review"]),
                        "initiated_by": review["initiated_by"],
                        "initiated_role": review["initiated_role"],
                        "initiated_at": review["initiated_at"],
                        "handed_over_by": review["handed_over_by"],
                        "boxes": [dict(r) for r in review_boxes],
                    }
                else:
                    last_review = conn.execute(
                        "SELECT * FROM batch_reviews WHERE batch_no = ? ORDER BY id DESC LIMIT 1",
                        (batch_no,),
                    ).fetchone()
                    if last_review:
                        review_boxes = conn.execute(
                            "SELECT * FROM batch_review_boxes WHERE review_id = ?",
                            (last_review["id"],),
                        ).fetchall()
                        result["current_review"] = {
                            "review_id": last_review["id"],
                            "status": last_review["status"],
                            "require_double_review": bool(last_review["require_double_review"]),
                            "initiated_by": last_review["initiated_by"],
                            "initiated_role": last_review["initiated_role"],
                            "initiated_at": last_review["initiated_at"],
                            "handed_over_by": last_review["handed_over_by"],
                            "cancelled_at": last_review["cancelled_at"],
                            "cancelled_by": last_review["cancelled_by"],
                            "cancelled_reason": last_review["cancelled_reason"],
                            "completed_at": last_review["completed_at"],
                            "boxes": [dict(r) for r in review_boxes],
                        }
    return result


# ── Review Configuration API ─────────────────────────────────────────────────


@app.get("/api/review/config")
def get_review_config():
    with get_db() as conn:
        cfg = _get_review_config(conn)
    return {
        "require_double_review": bool(cfg["require_double_review"]),
        "updated_at": cfg["updated_at"],
        "updated_by": cfg["updated_by"],
    }


@app.post("/api/review/config")
def update_review_config(data: ReviewConfigUpdate):
    with get_db() as conn:
        _get_review_config(conn)
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE review_config SET require_double_review = ?, updated_at = ?, updated_by = ? WHERE id = 1",
            (1 if data.require_double_review else 0, now, data.operator),
        )
    return {
        "ok": True,
        "require_double_review": data.require_double_review,
    }


# ── Review APIs ───────────────────────────────────────────────────────────────


@app.post("/api/batches/{batch_no}/review/initiate")
def initiate_review(batch_no: str, req: ReviewInitiateRequest):
    if req.role != "仓库主管":
        raise HTTPException(
            403,
            f"角色「{req.role}」无权发起交接复核，允许角色: ['仓库主管']",
        )

    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        if batch["status"] == "已归档":
            raise HTTPException(409, f"批次 {batch_no} 已归档，不可再发起复核")

        if batch["status"] != "已签收":
            raise HTTPException(
                409,
                f"批次当前状态「{batch['status']}」不允许发起交接复核，必须在「已签收」后发起"
            )

        active = _get_active_review(conn, batch_no)
        if active:
            raise HTTPException(
                409,
                f"批次 {batch_no} 已有进行中的复核（ID: {active['id']}），请先撤销或完成后再发起"
            )

        cfg = _get_review_config(conn)
        require_double = bool(cfg["require_double_review"])

        batch_boxes = conn.execute(
            """
            SELECT bb.box_code FROM batch_boxes bb
            JOIN boxes b ON bb.box_code = b.box_code
            WHERE bb.batch_no = ? AND b.status = '已签收'
            ORDER BY bb.id
            """,
            (batch_no,),
        ).fetchall()

        if not batch_boxes:
            raise HTTPException(400, f"批次 {batch_no} 没有已签收的箱子，无法发起复核")

        now = datetime.now().isoformat()

        cur = conn.execute(
            """
            INSERT INTO batch_reviews (batch_no, status, require_double_review,
                                       initiated_by, initiated_role, initiated_at, handed_over_by)
            VALUES (?, '进行中', ?, ?, ?, ?, ?)
            """,
            (batch_no, 1 if require_double else 0, req.operator, req.role, now, req.handed_over_by),
        )
        review_id = cur.lastrowid

        for br in batch_boxes:
            conn.execute(
                "INSERT INTO batch_review_boxes (review_id, box_code) VALUES (?, ?)",
                (review_id, br["box_code"]),
            )

        conn.execute(
            "UPDATE batches SET review_status = '复核中', updated_at = ? WHERE batch_no = ?",
            (now, batch_no),
        )

        _log_batch_audit(
            conn, batch_no, None, "发起交接复核",
            batch["review_status"], "复核中",
            req.role, req.operator, None,
            f"发起交接复核（{'双人复核' if require_double else '单人复核'}），交接人: {req.handed_over_by or '未指定'}，快照 {len(batch_boxes)} 箱",
            now,
        )

        progress = _compute_review_progress(conn, review_id, require_double)

    return {
        "ok": True,
        "review_id": review_id,
        "batch_no": batch_no,
        "require_double_review": require_double,
        "initiated_at": now,
        "initiated_by": req.operator,
        "handed_over_by": req.handed_over_by,
        "total_boxes": len(batch_boxes),
        "progress": progress,
    }


@app.get("/api/batches/{batch_no}/review")
def get_review_status(batch_no: str):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        active = _get_active_review(conn, batch_no)
        if not active:
            last = conn.execute(
                "SELECT * FROM batch_reviews WHERE batch_no = ? ORDER BY id DESC LIMIT 1",
                (batch_no,),
            ).fetchone()
            if not last:
                return {
                    "batch_no": batch_no,
                    "review_status": batch["review_status"],
                    "active_review": None,
                    "message": "该批次尚未发起过交接复核",
                }
            active = last

        boxes = conn.execute(
            "SELECT * FROM batch_review_boxes WHERE review_id = ? ORDER BY id",
            (active["id"],),
        ).fetchall()

        progress = _compute_review_progress(
            conn, active["id"], bool(active["require_double_review"])
        )

        result = {
            "batch_no": batch_no,
            "review_status": batch["review_status"],
            "active_review": {
                "review_id": active["id"],
                "status": active["status"],
                "require_double_review": bool(active["require_double_review"]),
                "initiated_by": active["initiated_by"],
                "initiated_role": active["initiated_role"],
                "initiated_at": active["initiated_at"],
                "handed_over_by": active["handed_over_by"],
                "cancelled_at": active["cancelled_at"],
                "cancelled_by": active["cancelled_by"],
                "cancelled_reason": active["cancelled_reason"],
                "completed_at": active["completed_at"],
                "boxes": [dict(r) for r in boxes],
            },
            "progress": progress,
        }

        if bool(active["require_double_review"]):
            result["progress"]["second_review_done"] = progress["second_review_done"]
            result["progress"]["pending_second_review"] = progress["pending_second_review"]

        return result


@app.post("/api/batches/{batch_no}/review/boxes")
def review_boxes(batch_no: str, req: ReviewBoxRequest):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        active = _get_active_review(conn, batch_no)
        if not active:
            raise HTTPException(
                409, f"批次 {batch_no} 没有进行中的复核，请先发起复核"
            )

        _check_review_conflict_on_receive(conn, batch_no)

        require_double = bool(active["require_double_review"])
        review_id = active["id"]

        review_box_map = {
            r["box_code"]: dict(r) for r in conn.execute(
                "SELECT * FROM batch_review_boxes WHERE review_id = ?",
                (review_id,),
            ).fetchall()
        }

        now = datetime.now().isoformat()
        processed = 0
        skipped = 0
        updated_boxes = []

        for item in req.reviews:
            if item.result not in VALID_REVIEW_RESULTS:
                raise HTTPException(
                    400,
                    f"箱子 {item.box_code} 的复核结果「{item.result}」无效，"
                    f"允许的值: {', '.join(sorted(VALID_REVIEW_RESULTS))}"
                )

            if item.box_code not in review_box_map:
                raise HTTPException(
                    400,
                    f"箱子 {item.box_code} 不在当前复核单中（复核启动后新增的箱子需撤销重开）"
                )

            rb = review_box_map[item.box_code]

            if not require_double:
                conn.execute(
                    """
                    UPDATE batch_review_boxes
                    SET first_review_result = ?, first_reviewer = ?, first_review_role = ?,
                        first_review_reason = ?, first_review_at = ?, final_result = ?
                    WHERE review_id = ? AND box_code = ?
                    """,
                    (item.result, req.operator, req.role, item.reason, now, item.result,
                     review_id, item.box_code),
                )
                processed += 1
                updated_boxes.append(item.box_code)
            elif rb["first_review_result"] is None:
                conn.execute(
                    """
                    UPDATE batch_review_boxes
                    SET first_review_result = ?, first_reviewer = ?, first_review_role = ?,
                        first_review_reason = ?, first_review_at = ?, final_result = ?
                    WHERE review_id = ? AND box_code = ?
                    """,
                    (item.result, req.operator, req.role, item.reason, now, None,
                     review_id, item.box_code),
                )
                processed += 1
                updated_boxes.append(item.box_code)
            elif rb["second_review_result"] is None:
                if rb["first_reviewer"] == req.operator:
                    raise HTTPException(
                        409,
                        f"双人复核要求不同人员，箱子 {item.box_code} 第一复核人已为「{req.operator}」"
                    )
                conn.execute(
                    """
                    UPDATE batch_review_boxes
                    SET second_review_result = ?, second_reviewer = ?, second_review_role = ?,
                        second_review_reason = ?, second_review_at = ?, final_result = ?
                    WHERE review_id = ? AND box_code = ?
                    """,
                    (item.result, req.operator, req.role, item.reason, now, item.result,
                     review_id, item.box_code),
                )
                processed += 1
                updated_boxes.append(item.box_code)
            else:
                skipped += 1
                continue

            _log_batch_audit(
                conn, batch_no, item.box_code, "交接复核",
                None, None,
                req.role, req.operator, item.reason,
                f"复核结果: {item.result}",
                now,
            )

        progress = _compute_review_progress(conn, review_id, require_double)

    return {
        "ok": True,
        "review_id": review_id,
        "processed": processed,
        "skipped": skipped,
        "updated_boxes": updated_boxes,
        "progress": progress,
    }


@app.post("/api/batches/{batch_no}/review/cancel")
def cancel_review(batch_no: str, req: ReviewCancelRequest):
    if req.role != "仓库主管":
        raise HTTPException(
            403,
            f"角色「{req.role}」无权撤销交接复核，允许角色: ['仓库主管']",
        )

    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        active = _get_active_review(conn, batch_no)
        if not active:
            raise HTTPException(
                409, f"批次 {batch_no} 没有进行中的复核，无法撤销"
            )

        now = datetime.now().isoformat()

        conn.execute(
            """
            UPDATE batch_reviews
            SET status = '已撤销', cancelled_at = ?, cancelled_by = ?, cancelled_reason = ?
            WHERE id = ?
            """,
            (now, req.operator, req.reason, active["id"]),
        )

        conn.execute(
            "UPDATE batches SET review_status = '未开始', updated_at = ? WHERE batch_no = ?",
            (now, batch_no),
        )

        _log_batch_audit(
            conn, batch_no, None, "撤销交接复核",
            "复核中", "未开始",
            req.role, req.operator, req.reason,
            f"复核单 ID {active['id']} 已撤销，原因: {req.reason}",
            now,
        )

    return {
        "ok": True,
        "batch_no": batch_no,
        "review_id": active["id"],
        "cancelled_at": now,
        "cancelled_by": req.operator,
        "reason": req.reason,
    }


@app.post("/api/batches/{batch_no}/archive")
def archive_batch(batch_no: str, req: ArchiveRequest):
    if req.role != "仓库主管":
        raise HTTPException(
            403,
            f"角色「{req.role}」无权归档批次，允许角色: ['仓库主管']",
        )

    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        if batch["status"] == "已归档":
            raise HTTPException(409, f"批次 {batch_no} 已归档，不可重复归档")

        if batch["status"] != "已签收":
            raise HTTPException(
                409,
                f"批次当前状态「{batch['status']}」不允许归档，必须在「已签收」后归档"
            )

        last_review = conn.execute(
            "SELECT * FROM batch_reviews WHERE batch_no = ? ORDER BY id DESC LIMIT 1",
            (batch_no,),
        ).fetchone()

        if not last_review:
            raise HTTPException(
                409,
                f"批次 {batch_no} 未完成交接复核，不允许归档。请先发起并完成复核。"
            )

        if last_review["status"] == "已撤销":
            raise HTTPException(
                409,
                f"批次 {batch_no} 最近一次复核已撤销，需重新发起并完成复核后才能归档"
            )

        if last_review["status"] != "已完成":
            progress = _compute_review_progress(
                conn, last_review["id"], bool(last_review["require_double_review"])
            )
            if not progress["all_reviewed"]:
                pending_msg = []
                if progress["pending_first_review"]:
                    pending_msg.append(f"待一复: {', '.join(progress['pending_first_review'])}")
                if progress["pending_second_review"]:
                    pending_msg.append(f"待二复: {', '.join(progress['pending_second_review'])}")
                if progress["pending_temp_confirmation"]:
                    pending_msg.append(f"温控待确认: {', '.join(progress['pending_temp_confirmation'])}")
                raise HTTPException(
                    409,
                    f"批次 {batch_no} 复核未完成，不允许归档。{'；'.join(pending_msg)}"
                )

        _check_review_conflict_on_receive(conn, batch_no)

        now = datetime.now().isoformat()

        if last_review["status"] != "已完成":
            conn.execute(
                "UPDATE batch_reviews SET status = '已完成', completed_at = ? WHERE id = ?",
                (now, last_review["id"]),
            )

        conn.execute(
            """
            UPDATE batches
            SET status = '已归档', review_status = '已归档',
                archived_at = ?, archived_by = ?, updated_at = ?
            WHERE batch_no = ?
            """,
            (now, req.operator, now, batch_no),
        )

        _log_batch_audit(
            conn, batch_no, None, "批次归档",
            "已签收", "已归档",
            req.role, req.operator, None,
            "批次交接复核完成，正式归档",
            now,
        )

    return {
        "ok": True,
        "batch_no": batch_no,
        "archived_at": now,
        "archived_by": req.operator,
    }


# ── Dispute Accountability Module ────────────────────────────────────────────

DISPUTE_CREATE_ROLES = {"库房签收员", "仓库主管", "质控", "班组长"}
DISPUTE_PROXY_ROLES = {"班组长", "仓库主管", "质控"}
DISPUTE_CONFIRM_ROLES = {"仓库主管", "质控"}
DISPUTE_CLOSE_ROLES = {"仓库主管", "质控"}
DISPUTE_EVIDENCE_ROLES = {"库房签收员", "仓库主管", "质控"}
DISPUTE_STATUSES = {"待确认", "处理中", "已驳回", "已撤回", "已结案"}
DISPUTE_TERMINAL_STATUSES = {"已结案"}
DISPUTE_ACTIVE_STATUSES = {"待确认", "处理中"}


def _get_dispute_config(conn):
    row = conn.execute("SELECT * FROM dispute_config WHERE id = 1").fetchone()
    if not row:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO dispute_config (id, require_double_confirm, allow_proxy_submit, updated_at, updated_by) VALUES (1, 0, 0, ?, '系统初始化')",
            (now,),
        )
        row = conn.execute("SELECT * FROM dispute_config WHERE id = 1").fetchone()
    return row


def _log_dispute_audit(conn, ticket_id, action, from_status, to_status,
                       role, operator, reason, detail, created_at):
    conn.execute(
        """
        INSERT INTO dispute_audit_log (ticket_id, action, from_status, to_status,
                                       role, operator, reason, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticket_id, action, from_status, to_status,
         role, operator, reason, detail, created_at),
    )


def _get_dispute_ticket(conn, ticket_id):
    return conn.execute(
        "SELECT * FROM dispute_tickets WHERE id = ?", (ticket_id,)
    ).fetchone()


def _build_ticket_detail(conn, ticket):
    boxes = conn.execute(
        "SELECT * FROM dispute_ticket_boxes WHERE ticket_id = ? ORDER BY id",
        (ticket["id"],),
    ).fetchall()
    evidence = conn.execute(
        "SELECT * FROM dispute_evidence WHERE ticket_id = ? ORDER BY id",
        (ticket["id"],),
    ).fetchall()
    return {
        "id": ticket["id"],
        "ticket_no": ticket["ticket_no"],
        "batch_no": ticket["batch_no"],
        "status": ticket["status"],
        "problem_type": ticket["problem_type"],
        "evidence_desc": ticket["evidence_desc"],
        "responsibility_judgment": ticket["responsibility_judgment"],
        "deadline": ticket["deadline"],
        "conclusion": ticket["conclusion"],
        "require_double_confirm": bool(ticket["require_double_confirm"]),
        "created_by": ticket["created_by"],
        "created_role": ticket["created_role"],
        "submitted_by": ticket["submitted_by"],
        "proxy_submitted": bool(ticket["proxy_submitted"]),
        "created_at": ticket["created_at"],
        "updated_at": ticket["updated_at"],
        "supervisor_confirmed": bool(ticket["supervisor_confirmed"]),
        "supervisor_confirmed_by": ticket["supervisor_confirmed_by"],
        "supervisor_confirmed_at": ticket["supervisor_confirmed_at"],
        "qc_confirmed": bool(ticket["qc_confirmed"]),
        "qc_confirmed_by": ticket["qc_confirmed_by"],
        "qc_confirmed_at": ticket["qc_confirmed_at"],
        "rejected_at": ticket["rejected_at"],
        "rejected_by": ticket["rejected_by"],
        "rejected_role": ticket["rejected_role"],
        "rejected_reason": ticket["rejected_reason"],
        "withdrawn_at": ticket["withdrawn_at"],
        "withdrawn_by": ticket["withdrawn_by"],
        "withdrawn_reason": ticket["withdrawn_reason"],
        "resubmitted_at": ticket["resubmitted_at"],
        "resubmitted_by": ticket["resubmitted_by"],
        "closed_at": ticket["closed_at"],
        "closed_by": ticket["closed_by"],
        "closed_role": ticket["closed_role"],
        "boxes": [dict(b) for b in boxes],
        "evidence_list": [dict(e) for e in evidence],
    }


@app.get("/api/dispute/config")
def get_dispute_config():
    with get_db() as conn:
        cfg = _get_dispute_config(conn)
    return {
        "require_double_confirm": bool(cfg["require_double_confirm"]),
        "allow_proxy_submit": bool(cfg["allow_proxy_submit"]),
        "updated_at": cfg["updated_at"],
        "updated_by": cfg["updated_by"],
    }


@app.post("/api/dispute/config")
def update_dispute_config(data: DisputeConfigUpdate):
    with get_db() as conn:
        cfg = _get_dispute_config(conn)
        now = datetime.now().isoformat()
        if data.allow_proxy_submit is not None:
            conn.execute(
                "UPDATE dispute_config SET require_double_confirm = ?, allow_proxy_submit = ?, updated_at = ?, updated_by = ? WHERE id = 1",
                (1 if data.require_double_confirm else 0,
                 1 if data.allow_proxy_submit else 0,
                 now, data.operator),
            )
        else:
            conn.execute(
                "UPDATE dispute_config SET require_double_confirm = ?, updated_at = ?, updated_by = ? WHERE id = 1",
                (1 if data.require_double_confirm else 0, now, data.operator),
            )
    return {
        "ok": True,
        "require_double_confirm": data.require_double_confirm,
        "allow_proxy_submit": data.allow_proxy_submit if data.allow_proxy_submit is not None else bool(cfg["allow_proxy_submit"]),
    }


@app.post("/api/dispute/tickets")
def create_dispute_ticket(data: DisputeTicketCreate):
    if data.role not in DISPUTE_CREATE_ROLES:
        raise HTTPException(
            403,
            f"角色「{data.role}」无权发起争议单，允许角色: {sorted(DISPUTE_CREATE_ROLES)}",
        )

    if not data.box_codes:
        raise HTTPException(400, "争议单必须关联至少一个箱号")

    is_proxy = data.on_behalf_of is not None and data.on_behalf_of != ""

    with get_db() as conn:
        cfg = _get_dispute_config(conn)
        allow_proxy = bool(cfg["allow_proxy_submit"])

        if is_proxy:
            if not allow_proxy:
                raise HTTPException(
                    403,
                    "当前配置不允许代理提交，请联系管理员开启 allow_proxy_submit 配置",
                )
            if data.role not in DISPUTE_PROXY_ROLES:
                raise HTTPException(
                    403,
                    f"角色「{data.role}」无权代理提交争议单，允许代理角色: {sorted(DISPUTE_PROXY_ROLES)}",
                )

        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (data.batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {data.batch_no} 不存在")

        if batch["status"] not in ("已签收", "已归档"):
            raise HTTPException(
                409,
                f"批次 {data.batch_no} 当前状态「{batch['status']}」不允许发起争议单，仅限已签收或已归档批次",
            )

        active_ticket = conn.execute(
            "SELECT * FROM dispute_tickets WHERE batch_no = ? AND status IN ('待确认', '处理中')",
            (data.batch_no,),
        ).fetchone()
        if active_ticket:
            raise HTTPException(
                409,
                f"批次 {data.batch_no} 已有进行中的争议单（工单号: {active_ticket['ticket_no']}），请先处理完成后再发起新争议单",
            )

        batch_box_rows = conn.execute(
            """
            SELECT bb.box_code FROM batch_boxes bb
            JOIN boxes b ON bb.box_code = b.box_code
            WHERE bb.batch_no = ? AND b.status = '已签收'
            """,
            (data.batch_no,),
        ).fetchall()
        batch_box_codes = {r["box_code"] for r in batch_box_rows}

        for bc in data.box_codes:
            if bc not in batch_box_codes:
                raise HTTPException(
                    400,
                    f"箱号 {bc} 不在批次 {data.batch_no} 的已签收箱子中，不允许跨批次混填",
                )

        require_double = bool(cfg["require_double_confirm"])

        now = datetime.now().isoformat()
        ticket_no = f"DSP-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        created_by = data.on_behalf_of if is_proxy else data.operator
        submitted_by = data.operator if is_proxy else None

        cur = conn.execute(
            """
            INSERT INTO dispute_tickets (ticket_no, batch_no, status, problem_type,
                evidence_desc, responsibility_judgment, deadline, conclusion,
                require_double_confirm, created_by, created_role, submitted_by,
                proxy_submitted, created_at, updated_at)
            VALUES (?, ?, '待确认', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticket_no, data.batch_no, data.problem_type,
             data.evidence_desc, data.responsibility_judgment,
             data.deadline, 1 if require_double else 0,
             created_by, data.role, submitted_by,
             1 if is_proxy else 0, now, now),
        )
        ticket_id = cur.lastrowid

        for bc in data.box_codes:
            conn.execute(
                "INSERT INTO dispute_ticket_boxes (ticket_id, box_code) VALUES (?, ?)",
                (ticket_id, bc),
            )

        if data.evidence_desc:
            conn.execute(
                """
                INSERT INTO dispute_evidence (ticket_id, evidence_desc, added_by, added_role, added_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ticket_id, data.evidence_desc, data.operator, data.role, now),
            )

        proxy_detail = ""
        if is_proxy:
            proxy_detail = f"，代理提交：实际提交人 {data.operator}，代 {data.on_behalf_of} 创建"
        _log_dispute_audit(
            conn, ticket_id, "创建争议单", None, "待确认",
            data.role, data.operator, data.evidence_desc,
            f"关联批次 {data.batch_no}，{len(data.box_codes)} 箱，问题类型: {data.problem_type}，"
            f"{'双确认' if require_double else '单确认'}模式{proxy_detail}",
            now,
        )

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "ticket_no": ticket_no,
        "status": "待确认",
        "require_double_confirm": require_double,
        "created_by": created_by,
        "submitted_by": submitted_by,
        "proxy_submitted": is_proxy,
    }


@app.get("/api/dispute/tickets")
def list_dispute_tickets(status: str = None, batch_no: str = None):
    with get_db() as conn:
        query = "SELECT * FROM dispute_tickets"
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if batch_no:
            conditions.append("batch_no = ?")
            params.append(batch_no)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/dispute/tickets/{ticket_id}")
def get_dispute_ticket(ticket_id: int):
    with get_db() as conn:
        ticket = _get_dispute_ticket(conn, ticket_id)
        if not ticket:
            raise HTTPException(404, f"争议工单 {ticket_id} 不存在")
        return _build_ticket_detail(conn, ticket)


@app.post("/api/dispute/tickets/{ticket_id}/confirm")
def confirm_dispute_ticket(ticket_id: int, req: DisputeConfirmRequest):
    if req.role not in DISPUTE_CONFIRM_ROLES:
        raise HTTPException(
            403,
            f"角色「{req.role}」无权确认争议单，允许角色: {sorted(DISPUTE_CONFIRM_ROLES)}",
        )

    with get_db() as conn:
        ticket = _get_dispute_ticket(conn, ticket_id)
        if not ticket:
            raise HTTPException(404, f"争议工单 {ticket_id} 不存在")

        if ticket["status"] != "待确认":
            raise HTTPException(
                409,
                f"争议工单当前状态「{ticket['status']}」不允许确认，仅「待确认」状态可确认",
            )

        now = datetime.now().isoformat()
        require_double = bool(ticket["require_double_confirm"])

        if require_double:
            if req.role == "仓库主管":
                if ticket["supervisor_confirmed"]:
                    raise HTTPException(409, "仓库主管已确认，不可重复确认")
                conn.execute(
                    """
                    UPDATE dispute_tickets SET supervisor_confirmed = 1,
                        supervisor_confirmed_by = ?, supervisor_confirmed_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (req.operator, now, now, ticket_id),
                )
                _log_dispute_audit(
                    conn, ticket_id, "仓库主管确认", "待确认", "待确认",
                    req.role, req.operator, req.reason,
                    "仓库主管已确认，等待质控确认", now,
                )
                qc_done = bool(ticket["qc_confirmed"])
                if qc_done:
                    conn.execute(
                        "UPDATE dispute_tickets SET status = '处理中', updated_at = ? WHERE id = ?",
                        (now, ticket_id),
                    )
                    _log_dispute_audit(
                        conn, ticket_id, "双确认完成", "待确认", "处理中",
                        req.role, req.operator, req.reason,
                        "仓库主管与质控均已确认，工单进入处理中", now,
                    )
            elif req.role == "质控":
                if ticket["qc_confirmed"]:
                    raise HTTPException(409, "质控已确认，不可重复确认")
                conn.execute(
                    """
                    UPDATE dispute_tickets SET qc_confirmed = 1,
                        qc_confirmed_by = ?, qc_confirmed_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (req.operator, now, now, ticket_id),
                )
                _log_dispute_audit(
                    conn, ticket_id, "质控确认", "待确认", "待确认",
                    req.role, req.operator, req.reason,
                    "质控已确认，等待仓库主管确认", now,
                )
                supervisor_done = bool(ticket["supervisor_confirmed"])
                if supervisor_done:
                    conn.execute(
                        "UPDATE dispute_tickets SET status = '处理中', updated_at = ? WHERE id = ?",
                        (now, ticket_id),
                    )
                    _log_dispute_audit(
                        conn, ticket_id, "双确认完成", "待确认", "处理中",
                        req.role, req.operator, req.reason,
                        "仓库主管与质控均已确认，工单进入处理中", now,
                    )
        else:
            if req.role != "仓库主管":
                raise HTTPException(
                    403,
                    f"单确认模式下仅仓库主管可确认，角色「{req.role}」无权确认",
                )
            conn.execute(
                "UPDATE dispute_tickets SET status = '处理中', updated_at = ?, "
                "supervisor_confirmed = 1, supervisor_confirmed_by = ?, supervisor_confirmed_at = ? "
                "WHERE id = ?",
                (now, req.operator, now, ticket_id),
            )
            _log_dispute_audit(
                conn, ticket_id, "确认争议单", "待确认", "处理中",
                req.role, req.operator, req.reason,
                "仓库主管确认，工单进入处理中", now,
            )

        ticket = _get_dispute_ticket(conn, ticket_id)

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "status": ticket["status"],
        "supervisor_confirmed": bool(ticket["supervisor_confirmed"]),
        "qc_confirmed": bool(ticket["qc_confirmed"]),
    }


@app.post("/api/dispute/tickets/{ticket_id}/reject")
def reject_dispute_ticket(ticket_id: int, req: DisputeRejectRequest):
    if req.role not in DISPUTE_CONFIRM_ROLES:
        raise HTTPException(
            403,
            f"角色「{req.role}」无权驳回争议单，允许角色: {sorted(DISPUTE_CONFIRM_ROLES)}",
        )

    with get_db() as conn:
        ticket = _get_dispute_ticket(conn, ticket_id)
        if not ticket:
            raise HTTPException(404, f"争议工单 {ticket_id} 不存在")

        if ticket["status"] != "待确认":
            raise HTTPException(
                409,
                f"争议工单当前状态「{ticket['status']}」不允许驳回，仅「待确认」状态可驳回",
            )

        now = datetime.now().isoformat()

        conn.execute(
            """
            UPDATE dispute_tickets SET status = '已驳回', updated_at = ?,
                rejected_at = ?, rejected_by = ?, rejected_role = ?, rejected_reason = ?
            WHERE id = ?
            """,
            (now, now, req.operator, req.role, req.reason, ticket_id),
        )

        _log_dispute_audit(
            conn, ticket_id, "驳回争议单", "待确认", "已驳回",
            req.role, req.operator, req.reason,
            f"驳回原因: {req.reason}", now,
        )

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "status": "已驳回",
        "rejected_by": req.operator,
        "rejected_reason": req.reason,
    }


@app.post("/api/dispute/tickets/{ticket_id}/withdraw")
def withdraw_dispute_ticket(ticket_id: int, req: DisputeWithdrawRequest):
    with get_db() as conn:
        ticket = _get_dispute_ticket(conn, ticket_id)
        if not ticket:
            raise HTTPException(404, f"争议工单 {ticket_id} 不存在")

        if ticket["status"] != "处理中":
            raise HTTPException(
                409,
                f"争议工单当前状态「{ticket['status']}」不允许撤回，仅「处理中」状态可撤回",
            )

        if req.operator != ticket["created_by"]:
            raise HTTPException(
                403,
                f"仅工单创建人可撤回，当前操作人「{req.operator}」不是创建人「{ticket['created_by']}」",
            )

        now = datetime.now().isoformat()

        conn.execute(
            """
            UPDATE dispute_tickets SET status = '已撤回', updated_at = ?,
                withdrawn_at = ?, withdrawn_by = ?, withdrawn_reason = ?
            WHERE id = ?
            """,
            (now, now, req.operator, req.reason, ticket_id),
        )

        _log_dispute_audit(
            conn, ticket_id, "撤回争议单", "处理中", "已撤回",
            req.role, req.operator, req.reason,
            "创建人撤回争议单", now,
        )

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "status": "已撤回",
        "withdrawn_by": req.operator,
    }


@app.post("/api/dispute/tickets/{ticket_id}/reopen")
def reopen_dispute_ticket(ticket_id: int, req: DisputeReopenRequest):
    with get_db() as conn:
        ticket = _get_dispute_ticket(conn, ticket_id)
        if not ticket:
            raise HTTPException(404, f"争议工单 {ticket_id} 不存在")

        if ticket["status"] != "已撤回":
            raise HTTPException(
                409,
                f"争议工单当前状态「{ticket['status']}」不允许重开，仅「已撤回」状态可重开",
            )

        if req.operator != ticket["created_by"]:
            raise HTTPException(
                403,
                f"仅工单创建人可重开，当前操作人「{req.operator}」不是创建人「{ticket['created_by']}」",
            )

        now = datetime.now().isoformat()

        conn.execute(
            """
            UPDATE dispute_tickets SET status = '待确认', updated_at = ?,
                supervisor_confirmed = 0, supervisor_confirmed_by = NULL,
                supervisor_confirmed_at = NULL,
                qc_confirmed = 0, qc_confirmed_by = NULL, qc_confirmed_at = NULL,
                withdrawn_at = NULL, withdrawn_by = NULL, withdrawn_reason = NULL
            WHERE id = ?
            """,
            (now, ticket_id),
        )

        _log_dispute_audit(
            conn, ticket_id, "重开争议单", "已撤回", "待确认",
            req.role, req.operator, req.reason,
            "创建人重开争议单，重新进入待确认", now,
        )

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "status": "待确认",
    }


@app.post("/api/dispute/tickets/{ticket_id}/resubmit")
def resubmit_dispute_ticket(ticket_id: int, req: DisputeResubmitRequest):
    with get_db() as conn:
        ticket = _get_dispute_ticket(conn, ticket_id)
        if not ticket:
            raise HTTPException(404, f"争议工单 {ticket_id} 不存在")

        if ticket["status"] != "已驳回":
            raise HTTPException(
                409,
                f"争议工单当前状态「{ticket['status']}」不允许重新提交，仅「已驳回」状态可重新提交",
            )

        if req.operator != ticket["created_by"]:
            raise HTTPException(
                403,
                f"仅工单创建人可重新提交，当前操作人「{req.operator}」不是创建人「{ticket['created_by']}」",
            )

        now = datetime.now().isoformat()

        update_fields = [
            "status = '待确认'", "updated_at = ?",
            "supervisor_confirmed = 0", "supervisor_confirmed_by = NULL",
            "supervisor_confirmed_at = NULL",
            "qc_confirmed = 0", "qc_confirmed_by = NULL", "qc_confirmed_at = NULL",
            "rejected_at = NULL", "rejected_by = NULL", "rejected_role = NULL",
            "rejected_reason = NULL",
            "resubmitted_at = ?", "resubmitted_by = ?",
        ]
        params = [now, now, req.operator]

        if req.evidence_desc:
            update_fields.append("evidence_desc = ?")
            params.append(req.evidence_desc)

        params.append(ticket_id)

        conn.execute(
            f"UPDATE dispute_tickets SET {', '.join(update_fields)} WHERE id = ?",
            params,
        )

        detail_parts = ["创建人重新提交争议单"]
        if req.evidence_desc:
            detail_parts.append(f"补充证据说明: {req.evidence_desc}")

        _log_dispute_audit(
            conn, ticket_id, "重新提交争议单", "已驳回", "待确认",
            req.role, req.operator, req.reason,
            "；".join(detail_parts), now,
        )

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "status": "待确认",
    }


@app.post("/api/dispute/tickets/{ticket_id}/close")
def close_dispute_ticket(ticket_id: int, req: DisputeCloseRequest):
    if req.role not in DISPUTE_CLOSE_ROLES:
        raise HTTPException(
            403,
            f"角色「{req.role}」无权结案争议单，允许角色: {sorted(DISPUTE_CLOSE_ROLES)}",
        )

    with get_db() as conn:
        ticket = _get_dispute_ticket(conn, ticket_id)
        if not ticket:
            raise HTTPException(404, f"争议工单 {ticket_id} 不存在")

        if ticket["status"] != "处理中":
            raise HTTPException(
                409,
                f"争议工单当前状态「{ticket['status']}」不允许结案，仅「处理中」状态可结案",
            )

        now = datetime.now().isoformat()

        conn.execute(
            """
            UPDATE dispute_tickets SET status = '已结案', updated_at = ?,
                conclusion = ?, closed_at = ?, closed_by = ?, closed_role = ?
            WHERE id = ?
            """,
            (now, req.conclusion, now, req.operator, req.role, ticket_id),
        )

        _log_dispute_audit(
            conn, ticket_id, "结案争议单", "处理中", "已结案",
            req.role, req.operator, req.reason,
            f"结案结论: {req.conclusion}", now,
        )

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "status": "已结案",
        "conclusion": req.conclusion,
        "closed_by": req.operator,
    }


@app.post("/api/dispute/tickets/{ticket_id}/evidence")
def add_dispute_evidence(ticket_id: int, req: DisputeEvidenceRequest):
    if req.role not in DISPUTE_EVIDENCE_ROLES:
        raise HTTPException(
            403,
            f"角色「{req.role}」无权补充证据，允许角色: {sorted(DISPUTE_EVIDENCE_ROLES)}",
        )

    with get_db() as conn:
        ticket = _get_dispute_ticket(conn, ticket_id)
        if not ticket:
            raise HTTPException(404, f"争议工单 {ticket_id} 不存在")

        if ticket["status"] not in DISPUTE_ACTIVE_STATUSES:
            raise HTTPException(
                409,
                f"争议工单当前状态「{ticket['status']}」不允许补充证据，仅「待确认」或「处理中」状态可补充",
            )

        now = datetime.now().isoformat()

        conn.execute(
            """
            INSERT INTO dispute_evidence (ticket_id, evidence_desc, added_by, added_role, added_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ticket_id, req.evidence_desc, req.operator, req.role, now),
        )

        conn.execute(
            "UPDATE dispute_tickets SET updated_at = ? WHERE id = ?",
            (now, ticket_id),
        )

        _log_dispute_audit(
            conn, ticket_id, "补充证据", ticket["status"], ticket["status"],
            req.role, req.operator, None,
            f"补充证据: {req.evidence_desc}", now,
        )

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "evidence_desc": req.evidence_desc,
        "added_by": req.operator,
    }


@app.get("/api/dispute/tickets/{ticket_id}/audit")
def get_dispute_audit(ticket_id: int):
    with get_db() as conn:
        ticket = _get_dispute_ticket(conn, ticket_id)
        if not ticket:
            raise HTTPException(404, f"争议工单 {ticket_id} 不存在")

        rows = conn.execute(
            "SELECT * FROM dispute_audit_log WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/dispute/batches/{batch_no}/summary")
def get_batch_dispute_summary(batch_no: str):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"批次 {batch_no} 不存在")

        tickets = conn.execute(
            "SELECT * FROM dispute_tickets WHERE batch_no = ? ORDER BY created_at DESC",
            (batch_no,),
        ).fetchall()

        total = len(tickets)
        by_status = {}
        for t in tickets:
            s = t["status"]
            by_status[s] = by_status.get(s, 0) + 1

        ticket_summaries = []
        for t in tickets:
            boxes = conn.execute(
                "SELECT box_code FROM dispute_ticket_boxes WHERE ticket_id = ?",
                (t["id"],),
            ).fetchall()
            ticket_summaries.append({
                "ticket_id": t["id"],
                "ticket_no": t["ticket_no"],
                "status": t["status"],
                "problem_type": t["problem_type"],
                "created_by": t["created_by"],
                "created_at": t["created_at"],
                "box_codes": [b["box_code"] for b in boxes],
                "conclusion": t["conclusion"],
            })

    return {
        "batch_no": batch_no,
        "batch_status": batch["status"],
        "total_tickets": total,
        "by_status": by_status,
        "tickets": ticket_summaries,
    }


@app.get("/api/dispute/export/json")
def export_dispute_json(status: str = None, batch_no: str = None):
    with get_db() as conn:
        query = "SELECT * FROM dispute_tickets"
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if batch_no:
            conditions.append("batch_no = ?")
            params.append(batch_no)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        tickets = conn.execute(query, params).fetchall()

        result_rows = []
        for t in tickets:
            boxes = conn.execute(
                "SELECT box_code FROM dispute_ticket_boxes WHERE ticket_id = ?",
                (t["id"],),
            ).fetchall()
            evidence = conn.execute(
                "SELECT * FROM dispute_evidence WHERE ticket_id = ? ORDER BY id",
                (t["id"],),
            ).fetchall()
            audit = conn.execute(
                "SELECT * FROM dispute_audit_log WHERE ticket_id = ? ORDER BY id",
                (t["id"],),
            ).fetchall()

            result_rows.append({
                "ticket_id": t["id"],
                "ticket_no": t["ticket_no"],
                "batch_no": t["batch_no"],
                "status": t["status"],
                "problem_type": t["problem_type"],
                "evidence_desc": t["evidence_desc"],
                "responsibility_judgment": t["responsibility_judgment"],
                "deadline": t["deadline"],
                "conclusion": t["conclusion"],
                "require_double_confirm": bool(t["require_double_confirm"]),
                "created_by": t["created_by"],
                "created_role": t["created_role"],
                "submitted_by": t["submitted_by"],
                "proxy_submitted": bool(t["proxy_submitted"]),
                "created_at": t["created_at"],
                "supervisor_confirmed": bool(t["supervisor_confirmed"]),
                "supervisor_confirmed_by": t["supervisor_confirmed_by"],
                "qc_confirmed": bool(t["qc_confirmed"]),
                "qc_confirmed_by": t["qc_confirmed_by"],
                "closed_by": t["closed_by"],
                "closed_at": t["closed_at"],
                "box_codes": [b["box_code"] for b in boxes],
                "evidence_list": [dict(e) for e in evidence],
                "audit_log": [dict(a) for a in audit],
            })

    return {
        "generated_at": datetime.now().isoformat(),
        "total_tickets": len(result_rows),
        "tickets": result_rows,
    }


@app.get("/api/dispute/export/csv")
def export_dispute_csv(status: str = None, batch_no: str = None):
    with get_db() as conn:
        query = "SELECT * FROM dispute_tickets"
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if batch_no:
            conditions.append("batch_no = ?")
            params.append(batch_no)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        tickets = conn.execute(query, params).fetchall()

        fields = [
            "ticket_id", "ticket_no", "batch_no", "status", "problem_type",
            "evidence_desc", "responsibility_judgment", "deadline", "conclusion",
            "require_double_confirm", "created_by", "created_role",
            "submitted_by", "proxy_submitted",
            "created_at",
            "supervisor_confirmed", "supervisor_confirmed_by",
            "qc_confirmed", "qc_confirmed_by",
            "closed_by", "closed_at", "box_codes",
        ]

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()

        for t in tickets:
            boxes = conn.execute(
                "SELECT box_code FROM dispute_ticket_boxes WHERE ticket_id = ?",
                (t["id"],),
            ).fetchall()
            row = {
                "ticket_id": t["id"],
                "ticket_no": t["ticket_no"],
                "batch_no": t["batch_no"],
                "status": t["status"],
                "problem_type": t["problem_type"],
                "evidence_desc": t["evidence_desc"] or "",
                "responsibility_judgment": t["responsibility_judgment"] or "",
                "deadline": t["deadline"] or "",
                "conclusion": t["conclusion"] or "",
                "require_double_confirm": bool(t["require_double_confirm"]),
                "created_by": t["created_by"],
                "created_role": t["created_role"],
                "submitted_by": t["submitted_by"] or "",
                "proxy_submitted": bool(t["proxy_submitted"]),
                "created_at": t["created_at"],
                "supervisor_confirmed": bool(t["supervisor_confirmed"]),
                "supervisor_confirmed_by": t["supervisor_confirmed_by"] or "",
                "qc_confirmed": bool(t["qc_confirmed"]),
                "qc_confirmed_by": t["qc_confirmed_by"] or "",
                "closed_by": t["closed_by"] or "",
                "closed_at": t["closed_at"] or "",
                "box_codes": ";".join(b["box_code"] for b in boxes),
            }
            writer.writerow(row)

    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=dispute_tickets.csv"},
    )
