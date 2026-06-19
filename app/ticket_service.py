from datetime import datetime
from typing import Optional


def _row_get(row, key, default=None):
    """安全访问 sqlite3.Row，兼容 dict.get() 语义"""
    try:
        if hasattr(row, 'keys'):
            return row[key] if key in row.keys() else default
        return row.get(key, default) if isinstance(row, dict) else default
    except (KeyError, IndexError):
        return default


class TicketType:
    EXCEPTION = "exception"
    LIABILITY = "liability"
    PROXY_REPORT = "proxy_report"


TICKET_CONFIG = {
    TicketType.EXCEPTION: {
        "table": "exception_tickets",
        "audit_table": "exception_audit_log",
        "evidence_table": "exception_evidence",
        "transfer_table": "exception_handler_transfers",
        "id_col": "id",
        "ticket_no_col": "ticket_no",
        "statuses": {
            "active": ["待处理", "处理中"],
            "terminal": ["已结案"],
            "initial": "待处理",
            "after_confirm": "处理中",
            "withdrawn": "已撤回",
            "rejected": "已驳回",
            "closed": "已结案",
        },
        "fields": {
            "originator_field": "initiator",
            "originator_role_field": "initiator_role",
            "proxy_field": "proxy_recorder",
            "proxy_role_field": "proxy_recorder_role",
            "handler_field": "current_handler",
            "handler_role_field": "current_handler_role",
            "allow_proxy_flag": "allow_proxy_record_at_create",
            "batch_no_col": "batch_no",
            "box_code_col": "box_code",
            "reason_category_col": "reason_category",
        },
        "transfer_fields": {
            "from_handler": "from_handler",
            "from_handler_role": "from_handler_role",
            "to_handler": "to_handler",
            "to_handler_role": "to_handler_role",
            "actor": "transferred_by",
            "actor_role": "transferred_role",
            "reason": "transfer_reason",
        },
    },
    TicketType.LIABILITY: {
        "table": "liability_tickets",
        "audit_table": "liability_audit_log",
        "evidence_table": "liability_evidence",
        "transfer_table": "liability_handler_transfers",
        "id_col": "id",
        "ticket_no_col": "ticket_no",
        "statuses": {
            "active": ["待处理", "处理中"],
            "terminal": ["已结案"],
            "initial": "待处理",
            "after_confirm": "处理中",
            "withdrawn": "已撤回",
            "rejected": "已驳回",
            "closed": "已结案",
        },
        "fields": {
            "originator_field": "reporter",
            "originator_role_field": "reporter_role",
            "proxy_field": "proxy_recorder",
            "proxy_role_field": "proxy_recorder_role",
            "handler_field": "current_handler",
            "handler_role_field": "current_handler_role",
            "allow_proxy_flag": "allow_proxy_record_at_create",
            "batch_no_col": "batch_no",
            "box_code_col": "box_code",
            "reason_category_col": "reason_category",
        },
        "transfer_fields": {
            "from_handler": "from_handler",
            "from_handler_role": "from_handler_role",
            "to_handler": "to_handler",
            "to_handler_role": "to_handler_role",
            "actor": "transferred_by",
            "actor_role": "transferred_role",
            "reason": "transfer_reason",
        },
    },
    TicketType.PROXY_REPORT: {
        "table": "proxy_report_tickets",
        "audit_table": "proxy_report_audit_log",
        "evidence_table": "proxy_report_evidence",
        "transfer_table": "proxy_report_assignments",
        "id_col": "id",
        "ticket_no_col": "ticket_no",
        "statuses": {
            "active": ["待指派", "处理中"],
            "terminal": ["已结案"],
            "initial": "待指派",
            "after_confirm": "处理中",
            "withdrawn": "已撤回",
            "rejected": "已驳回",
            "closed": "已结案",
        },
        "fields": {
            "originator_field": "originator",
            "originator_role_field": "originator_role",
            "responsibility_role_field": "responsibility_role",
            "proxy_field": "proxy_recorder",
            "proxy_role_field": "proxy_recorder_role",
            "handler_field": "current_handler",
            "handler_role_field": "current_handler_role",
            "allow_proxy_flag": "allow_proxy_at_create",
            "batch_no_col": "batch_no",
            "box_code_col": "box_code",
            "reason_category_col": "reason_category",
        },
        "transfer_fields": {
            "from_handler": "from_handler",
            "from_handler_role": "from_handler_role",
            "to_handler": "to_handler",
            "to_handler_role": "to_handler_role",
            "actor": "assigned_by",
            "actor_role": "assigned_role",
            "reason": "assign_reason",
        },
    },
}


def _cfg(ticket_type: str) -> dict:
    return TICKET_CONFIG[ticket_type]


def check_duplicate_ticket(
    conn,
    ticket_type: str,
    box_code: Optional[str],
    reason_category: str,
    exclude_ticket_id: Optional[int] = None,
):
    """
    统一去重检查：按【箱号 + 事由】匹配，只要还有未关闭（非终态）记录就拦截。
    不再按批次号区分，避免同一箱号同事由跨批次重复建单。
    已关闭的记录不参与拦截，允许重新发起新工单。
    """
    cfg = _cfg(ticket_type)
    table = cfg["table"]
    active_statuses = cfg["statuses"]["active"]
    status_placeholders = ", ".join("?" * len(active_statuses))

    query = f"""
        SELECT * FROM {table}
        WHERE reason_category = ?
          AND status IN ({status_placeholders})
          AND box_code IS NOT NULL
          AND box_code != ''
    """
    params = [reason_category] + list(active_statuses)

    if box_code:
        query += " AND box_code = ?"
        params.append(box_code)

    if exclude_ticket_id is not None:
        query += f" AND {cfg['id_col']} != ?"
        params.append(exclude_ticket_id)

    return conn.execute(query, params).fetchone()


def compute_responsibility(
    ticket_type: str,
    operator: str,
    role: str,
    on_behalf_of: Optional[str] = None,
    on_behalf_of_role: Optional[str] = None,
) -> dict:
    """
    统一责任归属计算。
    返回 dict: {
        originator, originator_role, responsibility_role,
        proxy_recorder, proxy_recorder_role,
        current_handler, current_handler_role,
        is_proxy
    }
    """
    cfg = _cfg(ticket_type)
    is_proxy = on_behalf_of is not None and on_behalf_of.strip() != ""

    if is_proxy:
        originator = on_behalf_of
        proxy_recorder = operator
        proxy_recorder_role = role

        if ticket_type == TicketType.PROXY_REPORT:
            originator_role = on_behalf_of_role or role
            responsibility_role = on_behalf_of_role or role
        else:
            originator_role = role
            responsibility_role = originator_role
    else:
        originator = operator
        originator_role = role
        responsibility_role = role
        proxy_recorder = None
        proxy_recorder_role = None

    current_handler = originator
    current_handler_role = responsibility_role

    return {
        "originator": originator,
        "originator_role": originator_role,
        "responsibility_role": responsibility_role,
        "proxy_recorder": proxy_recorder,
        "proxy_recorder_role": proxy_recorder_role,
        "current_handler": current_handler,
        "current_handler_role": current_handler_role,
        "is_proxy": is_proxy,
    }


def recompute_responsibility_on_resubmit(
    ticket_type: str,
    ticket_row: dict,
) -> dict:
    """
    撤回/驳回后重新提交时，责任归属重算。
    规则：处理人重置为发起人（originator），处理岗位重置为发起岗位。
    """
    cfg = _cfg(ticket_type)
    fields = cfg["fields"]
    originator = ticket_row[fields["originator_field"]]
    originator_role = ticket_row[fields["originator_role_field"]]

    if ticket_type == TicketType.PROXY_REPORT:
        resp_role_field = fields.get("responsibility_role_field", "")
        responsibility_role = _row_get(ticket_row, resp_role_field, originator_role) or originator_role
        current_handler_role = responsibility_role
    else:
        current_handler_role = originator_role

    return {
        "current_handler": originator,
        "current_handler_role": current_handler_role,
    }


def get_ticket_by_id(conn, ticket_type: str, ticket_id: int):
    cfg = _cfg(ticket_type)
    return conn.execute(
        f"SELECT * FROM {cfg['table']} WHERE {cfg['id_col']} = ?",
        (ticket_id,),
    ).fetchone()


def list_tickets(
    conn,
    ticket_type: str,
    status: Optional[str] = None,
    batch_no: Optional[str] = None,
    box_code: Optional[str] = None,
    extra_filters: Optional[dict] = None,
):
    cfg = _cfg(ticket_type)
    query = f"SELECT * FROM {cfg['table']}"
    conditions = []
    params = []

    if status:
        conditions.append("status = ?")
        params.append(status)
    if batch_no:
        conditions.append(f"{cfg['fields']['batch_no_col']} = ?")
        params.append(batch_no)
    if box_code:
        conditions.append(f"{cfg['fields']['box_code_col']} = ?")
        params.append(box_code)
    if extra_filters:
        for k, v in extra_filters.items():
            conditions.append(f"{k} = ?")
            params.append(v)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC"
    return conn.execute(query, params).fetchall()


def log_ticket_audit(
    conn,
    ticket_type: str,
    ticket_id: int,
    action: str,
    from_status: Optional[str],
    to_status: str,
    role: str,
    operator: str,
    reason: Optional[str],
    detail: str,
    created_at: Optional[str] = None,
):
    cfg = _cfg(ticket_type)
    audit_table = cfg["audit_table"]
    if created_at is None:
        created_at = datetime.now().isoformat()
    conn.execute(
        f"""
        INSERT INTO {audit_table} (ticket_id, action, from_status, to_status,
                                   role, operator, reason, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticket_id, action, from_status, to_status,
         role, operator, reason, detail, created_at),
    )


def log_handler_transfer(
    conn,
    ticket_type: str,
    ticket_id: int,
    from_handler: str,
    from_handler_role: str,
    to_handler: str,
    to_handler_role: str,
    actor: str,
    actor_role: str,
    transfer_reason: Optional[str],
    created_at: Optional[str] = None,
):
    cfg = _cfg(ticket_type)
    transfer_table = cfg["transfer_table"]
    tf = cfg["transfer_fields"]
    if created_at is None:
        created_at = datetime.now().isoformat()
    conn.execute(
        f"""
        INSERT INTO {transfer_table} (
            ticket_id, {tf['from_handler']}, {tf['from_handler_role']},
            {tf['to_handler']}, {tf['to_handler_role']},
            {tf['actor']}, {tf['actor_role']}, {tf['reason']}, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticket_id, from_handler, from_handler_role,
         to_handler, to_handler_role,
         actor, actor_role, transfer_reason, created_at),
    )


def get_transfers(conn, ticket_type: str, ticket_id: int):
    cfg = _cfg(ticket_type)
    return conn.execute(
        f"SELECT * FROM {cfg['transfer_table']} WHERE ticket_id = ? ORDER BY id",
        (ticket_id,),
    ).fetchall()


def get_evidence(conn, ticket_type: str, ticket_id: int):
    cfg = _cfg(ticket_type)
    return conn.execute(
        f"SELECT * FROM {cfg['evidence_table']} WHERE ticket_id = ? ORDER BY id",
        (ticket_id,),
    ).fetchall()


def get_audit_log(conn, ticket_type: str, ticket_id: int):
    cfg = _cfg(ticket_type)
    return conn.execute(
        f"SELECT * FROM {cfg['audit_table']} WHERE ticket_id = ? ORDER BY id",
        (ticket_id,),
    ).fetchall()


def build_detail(conn, ticket_type: str, ticket_row: dict, extra_detail: Optional[dict] = None):
    """
    统一构建工单详情。列表、详情页、导出都走同一套字段来源。
    """
    cfg = _cfg(ticket_type)
    fields = cfg["fields"]
    t = ticket_row

    evidence = get_evidence(conn, ticket_type, t[cfg["id_col"]])
    transfers = get_transfers(conn, ticket_type, t[cfg["id_col"]])

    originator = t[fields["originator_field"]]
    originator_role = t[fields["originator_role_field"]]
    proxy_recorder = t[fields["proxy_field"]]
    proxy_recorder_role = t[fields["proxy_role_field"]]
    current_handler = t[fields["handler_field"]]
    current_handler_role = t[fields["handler_role_field"]]

    base = {
        "id": t[cfg["id_col"]],
        "ticket_no": t[cfg["ticket_no_col"]],
        "batch_no": t[fields["batch_no_col"]],
        "box_code": t[fields["box_code_col"]],
        "reason_category": t[fields["reason_category_col"]],
        "description": t["description"],
        "status": t["status"],
        "conclusion": t["conclusion"],
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
        fields["originator_field"]: originator,
        fields["originator_role_field"]: originator_role,
        fields["proxy_field"]: proxy_recorder,
        fields["proxy_role_field"]: proxy_recorder_role,
        fields["handler_field"]: current_handler,
        fields["handler_role_field"]: current_handler_role,
        "allow_proxy_at_create": bool(t[fields["allow_proxy_flag"]]),
        "withdrawn_at": t["withdrawn_at"],
        "withdrawn_by": t["withdrawn_by"],
        "withdrawn_reason": t["withdrawn_reason"],
        "resubmitted_at": t["resubmitted_at"],
        "resubmitted_by": t["resubmitted_by"],
        "rejected_at": t["rejected_at"],
        "rejected_by": t["rejected_by"],
        "rejected_role": t["rejected_role"],
        "rejected_reason": t["rejected_reason"],
        "closed_at": t["closed_at"],
        "closed_by": t["closed_by"],
        "closed_role": t["closed_role"],
        "evidence_list": [dict(e) for e in evidence],
        "transfer_history": [dict(tr) for tr in transfers],
    }

    if ticket_type == TicketType.PROXY_REPORT:
        resp_role_field = fields.get("responsibility_role_field")
        if resp_role_field:
            base[resp_role_field] = _row_get(t, resp_role_field, originator_role)

    if extra_detail:
        base.update(extra_detail)
    return base


def build_export_json_row(conn, ticket_type: str, ticket_row: dict):
    """
    构建 JSON 导出的单行数据。
    包含完整的责任链（初始 → 每次转交 → 当前）。
    """
    cfg = _cfg(ticket_type)
    fields = cfg["fields"]
    tf = cfg["transfer_fields"]
    t = ticket_row
    ticket_id = t[cfg["id_col"]]

    evidence = get_evidence(conn, ticket_type, ticket_id)
    transfers = get_transfers(conn, ticket_type, ticket_id)
    audit = get_audit_log(conn, ticket_type, ticket_id)

    originator = t[fields["originator_field"]]
    originator_role = t[fields["originator_role_field"]]
    current_handler = t[fields["handler_field"]]
    current_handler_role = t[fields["handler_role_field"]]

    responsibility_chain = []
    responsibility_chain.append({
        "handler": originator,
        "handler_role": originator_role,
        "action": "初始责任人",
        "at": t["created_at"],
    })
    for tr in transfers:
        responsibility_chain.append({
            "from_handler": tr[tf["from_handler"]],
            "from_handler_role": tr[tf["from_handler_role"]],
            "to_handler": tr[tf["to_handler"]],
            "to_handler_role": tr[tf["to_handler_role"]],
            "transferred_by": tr[tf["actor"]],
            "transferred_role": tr[tf["actor_role"]],
            "transfer_reason": tr[tf["reason"]],
            "at": tr["created_at"],
        })
    responsibility_chain.append({
        "handler": current_handler,
        "handler_role": current_handler_role,
        "action": "当前责任人",
        "at": t["updated_at"],
    })

    row = {
        "ticket_id": t[cfg["id_col"]],
        "ticket_no": t[cfg["ticket_no_col"]],
        "batch_no": t[fields["batch_no_col"]],
        "box_code": t[fields["box_code_col"]],
        "reason_category": t[fields["reason_category_col"]],
        "description": t["description"],
        "status": t["status"],
        "conclusion": t["conclusion"],
        "allow_proxy_record_at_create": bool(t[fields["allow_proxy_flag"]]),
        fields["originator_field"]: originator,
        fields["originator_role_field"]: originator_role,
        fields["proxy_field"]: t[fields["proxy_field"]],
        fields["proxy_role_field"]: t[fields["proxy_role_field"]],
        fields["handler_field"]: current_handler,
        fields["handler_role_field"]: current_handler_role,
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
        "withdrawn_at": t["withdrawn_at"],
        "withdrawn_by": t["withdrawn_by"],
        "withdrawn_reason": t["withdrawn_reason"],
        "resubmitted_at": t["resubmitted_at"],
        "resubmitted_by": t["resubmitted_by"],
        "rejected_at": t["rejected_at"],
        "rejected_by": t["rejected_by"],
        "rejected_reason": t["rejected_reason"],
        "closed_at": t["closed_at"],
        "closed_by": t["closed_by"],
        "closed_role": t["closed_role"],
        "evidence_list": [dict(e) for e in evidence],
        "transfer_history": [dict(tr) for tr in transfers],
        "responsibility_chain": responsibility_chain,
        "audit_log": [dict(a) for a in audit],
    }

    if ticket_type == TicketType.PROXY_REPORT:
        resp_role_field = fields.get("responsibility_role_field")
        if resp_role_field:
            row[resp_role_field] = _row_get(t, resp_role_field, originator_role)
    return row


def build_export_csv_fields(ticket_type: str) -> list:
    """返回 CSV 导出的字段名列表"""
    cfg = _cfg(ticket_type)
    fields = cfg["fields"]

    base = [
        "ticket_id", "ticket_no", "batch_no", "box_code",
        "reason_category", "description", "status", "conclusion",
        fields["originator_field"], fields["originator_role_field"],
        fields["proxy_field"], fields["proxy_role_field"],
        fields["handler_field"], fields["handler_role_field"],
        "responsibility_transfers", "audit_log",
        "created_at", "updated_at",
        "withdrawn_by", "withdrawn_at", "withdrawn_reason",
        "rejected_by", "rejected_at", "rejected_reason",
        "closed_by", "closed_at", "closed_role",
        "evidence_count",
    ]

    if ticket_type == TicketType.PROXY_REPORT:
        resp_role_field = fields.get("responsibility_role_field")
        if resp_role_field:
            idx = base.index(fields["originator_role_field"]) + 1
            base.insert(idx, resp_role_field)

    return base


def build_export_csv_row(conn, ticket_type: str, ticket_row: dict) -> dict:
    """构建 CSV 导出的单行数据 dict"""
    cfg = _cfg(ticket_type)
    fields = cfg["fields"]
    tf = cfg["transfer_fields"]
    t = ticket_row
    ticket_id = t[cfg["id_col"]]

    transfers = get_transfers(conn, ticket_type, ticket_id)
    evidence_count = conn.execute(
        f"SELECT COUNT(*) as cnt FROM {cfg['evidence_table']} WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()["cnt"]
    audit = get_audit_log(conn, ticket_type, ticket_id)

    originator = t[fields["originator_field"]]
    originator_role = t[fields["originator_role_field"]]
    current_handler = t[fields["handler_field"]]
    current_handler_role = t[fields["handler_role_field"]]

    transfer_parts = [f"初始:{originator}({originator_role})"]
    for tr in transfers:
        transfer_parts.append(
            f"{tr[tf['from_handler']]}→{tr[tf['to_handler']]}({tr[tf['to_handler_role']]})"
            f"[{tr[tf['actor']]}@{tr['created_at'][:19]}]"
        )
    transfer_parts.append(f"当前:{current_handler}({current_handler_role})")
    transfer_str = " → ".join(transfer_parts)

    audit_parts = []
    for a in audit:
        time_str = str(a["created_at"])[:19]
        action_str = str(a["action"])
        actor_str = str(a["operator"])
        detail_str = str(a["detail"] or "")[:40]
        audit_parts.append(f"[{time_str}]{actor_str}:{action_str}-{detail_str}")
    audit_str = " | ".join(audit_parts)

    row = {
        "ticket_id": t[cfg["id_col"]],
        "ticket_no": t[cfg["ticket_no_col"]],
        "batch_no": t[fields["batch_no_col"]] or "",
        "box_code": t[fields["box_code_col"]] or "",
        "reason_category": t[fields["reason_category_col"]],
        "description": t["description"] or "",
        "status": t["status"],
        "conclusion": t["conclusion"] or "",
        fields["originator_field"]: originator,
        fields["originator_role_field"]: originator_role or "",
        fields["proxy_field"]: t[fields["proxy_field"]] or "",
        fields["proxy_role_field"]: t[fields["proxy_role_field"]] or "",
        fields["handler_field"]: current_handler,
        fields["handler_role_field"]: current_handler_role,
        "responsibility_transfers": transfer_str,
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
        "withdrawn_by": t["withdrawn_by"] or "",
        "withdrawn_at": t["withdrawn_at"] or "",
        "withdrawn_reason": t["withdrawn_reason"] or "",
        "rejected_by": t["rejected_by"] or "",
        "rejected_at": t["rejected_at"] or "",
        "rejected_reason": t["rejected_reason"] or "",
        "closed_by": t["closed_by"] or "",
        "closed_at": t["closed_at"] or "",
        "closed_role": t["closed_role"] or "",
        "evidence_count": evidence_count,
        "audit_log": audit_str,
    }

    if ticket_type == TicketType.PROXY_REPORT:
        resp_role_field = fields.get("responsibility_role_field")
        if resp_role_field:
            row[resp_role_field] = _row_get(t, resp_role_field, originator_role) or ""
    return row


def get_duplicate_error_msg(ticket_type: str, dup_row: dict) -> str:
    """统一的重复建单错误消息"""
    cfg = _cfg(ticket_type)
    type_labels = {
        TicketType.EXCEPTION: "处置单",
        TicketType.LIABILITY: "责任追踪单",
        TicketType.PROXY_REPORT: "异常代报受理单",
    }
    label = type_labels.get(ticket_type, "工单")
    return (
        f"已存在同箱号+同事由的活跃{label}（工单号: {dup_row[cfg['ticket_no_col']]}），"
        f"请勿重复报单。如同一问题发生在不同批次，请先关闭既有工单后再重新发起。"
    )
