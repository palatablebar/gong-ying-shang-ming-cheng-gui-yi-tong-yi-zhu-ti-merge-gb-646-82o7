"""M8 付款批次编排：解码采信 → 行校验与隔离 → 主体锁簇 → 状态落库 → 净额对账。

领域结论（回应"抵扣跟随原记录核销，还是进入同一主体后统一抵减"）：
- 抵扣行按供应商号锁回它在主数据中的那条记录及其所在簇，匹配不上即隔离，
  因此未核抵扣永远不会减到别的主体；
- 导入后抵扣一律 pending（待核），不进入可付净额，避免负数直接冲低整簇；
- 复核核销（approve）后才在"同一主体簇内"统一抵减，驳回（reject）则永久不抵减；
- 行键是内容指纹，重跑替换付款行但保留核销决定，已确认归属与净额不被改变。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from ... import store
from ...config import (
    LINE_TYPE_DEDUCTION,
    LINE_TYPE_PAYABLE,
    PAY_RULE_VERSION,
    PAYMENT_FILES,
    SAMPLE_DIR,
)
from ...errors import DomainError, NotFound
from ...manifest import combined_digest, utc_iso, utc_now
from ...reasons import (
    E_FILE_MISSING,
    E_MISSING_COLUMN,
    V_UNKNOWN_RUN,
    W_AMOUNT_INVALID,
    W_AMOUNT_ZERO,
    W_CLUSTER_CONFLICT,
    W_DEDUCTION_PENDING,
    W_LINE_KEY_DUP,
    W_NAME_MISMATCH,
    W_ROW_LONG,
    W_ROW_SHORT,
    W_WRITEOFF_REJECTED,
)
from ..m1_intake import decode as decode_mod
from ..m1_intake import reader
from ..m5_clusters import service as cluster_service
from . import matching, pcolumns

CENT = Decimal("0.01")


def _q(value: Decimal) -> str:
    """金额对外统一两位小数字符串，避免 0 与 0.00 口径不一致。"""
    return f"{Decimal(str(value)).quantize(CENT):.2f}"

# 付款行状态机。
STATUS_PAYABLE = "payable"          # 应付行：直接计入应付
STATUS_PENDING = "pending"          # 抵扣行：待核销，不抵减
STATUS_APPROVED = "approved"        # 抵扣行：已核销，参与抵减
STATUS_REJECTED = "rejected"        # 抵扣行：驳回，不抵减
STATUS_BLOCKED = "blocked"          # 抵扣行：主体异常（名称不符/税号冲突）挂起


def _safe_name(name: str) -> str:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise DomainError(E_FILE_MISSING, "文件名非法")
    return name


def payment_batch_id(combined_sha: str) -> str:
    digest = hashlib.sha256()
    digest.update(combined_sha.encode("ascii"))
    digest.update(b"|")
    digest.update(PAY_RULE_VERSION.encode("utf-8"))
    return "PAY-" + digest.hexdigest()[:12].upper()


def _quarantine(source, line_no, raw, code, detail=""):
    return {"source_file": source, "line_no": line_no, "raw_line": raw,
            "reason_code": code, "detail": detail}


def _ensure_master(run_id: str):
    """确保名录主数据与合并簇已就绪，返回匹配所需的索引。"""
    cluster_service.ensure_clusters(run_id)
    records = store.list_records(run_id)
    record_index = matching.build_record_index(records)
    members = store.list_cluster_members(run_id)
    supplier_cluster = matching.build_supplier_cluster(members)
    clusters_by_id = {c["cluster_id"]: c for c in store.list_clusters(run_id)}
    return record_index, supplier_cluster, clusters_by_id


def import_payment(run_id: str, files=None, force: bool = False) -> dict:
    if not store.get_run(run_id):
        raise NotFound(V_UNKNOWN_RUN, run_id)
    names = tuple(_safe_name(name) for name in (files or PAYMENT_FILES))
    started = utc_now()

    record_index, supplier_cluster, clusters_by_id = _ensure_master(run_id)
    digests = []
    accepted, quarantine = [], []
    input_rows = 0

    for source in names:
        path = SAMPLE_DIR / source
        if not path.is_file():
            raise NotFound(E_FILE_MISSING, source)
        data = path.read_bytes()
        digests.append((source, hashlib.sha256(data).hexdigest()))
        decoded = decode_mod.decode_bytes(data)
        parsed = reader.parse_stream(decoded.text)
        header = pcolumns.normalize_header(parsed.header)
        index, missing = pcolumns.map_columns(header)
        if missing:
            raise DomainError(E_MISSING_COLUMN, ",".join(missing))

        for rec in parsed.rows:
            input_rows += 1
            if len(rec.cells) < len(header):
                quarantine.append(_quarantine(source, rec.line_no, rec.raw,
                                              W_ROW_SHORT, f"{len(rec.cells)}/{len(header)}"))
                continue
            if len(rec.cells) > len(header):
                quarantine.append(_quarantine(source, rec.line_no, rec.raw,
                                              W_ROW_LONG, f"{len(rec.cells)}/{len(header)}"))
                continue
            line = _parse_line(index, rec.cells, rec.raw, source, rec.line_no,
                               record_index, supplier_cluster, clusters_by_id, quarantine)
            if line is not None:
                accepted.append(line)

    combined = combined_digest(digests)
    batch_id = payment_batch_id(combined)
    # 非强制重跑且同内容批次已存在：直接复用，绝不重新落库或改动核销决定。
    if not force and store.get_payment_batch(batch_id) is not None:
        return store.get_payment_batch(batch_id)

    # 行键去重：同批内容指纹冲突说明数据本身重复，后者隔离，避免重复计入抵扣。
    deduped, seen = [], set()
    for line in accepted:
        if line["line_key"] in seen:
            quarantine.append(_quarantine(line["source_file"], line["src_line_no"],
                                          line["raw_line"], W_LINE_KEY_DUP, line["line_key"]))
            continue
        seen.add(line["line_key"])
        deduped.append(line)

    lines = _reapply_writeoffs(run_id, batch_id, deduped)
    store.replace_payment_lines(run_id, batch_id, lines)
    store.replace_payment_quarantine(run_id, quarantine)
    counts = _count(input_rows, lines, quarantine)
    batch = {
        "batch_id": batch_id,
        "run_id": run_id,
        "input_sha256": combined,
        "rule_version": PAY_RULE_VERSION,
        "source_file": ",".join(names),
        "params_json": json.dumps({"files": list(names), "force": force}, ensure_ascii=False),
        "created_at": utc_iso(started),
        "counts_json": json.dumps(counts, ensure_ascii=False, sort_keys=True),
    }
    store.save_payment_batch(batch)
    return store.get_payment_batch(batch_id)


def _parse_line(index, cells, raw, source, line_no, record_index,
                supplier_cluster, clusters_by_id, quarantine):
    supplier_id = pcolumns.clean_code(pcolumns.cell(cells, index, "supplier_id"))
    supplier_name = pcolumns.cell(cells, index, "supplier_name").strip()
    doc_no = pcolumns.clean_code(pcolumns.cell(cells, index, "doc_no"))
    raw_type = pcolumns.cell(cells, index, "line_type")
    src_line_raw = pcolumns.cell(cells, index, "line_no_src").strip()
    amount, amount_error = pcolumns.parse_amount(pcolumns.cell(cells, index, "amount"))
    currency = pcolumns.cell(cells, index, "currency").strip() or "CNY"
    remark = pcolumns.cell(cells, index, "remark").strip()
    src_line_no = int(src_line_raw) if src_line_raw.isdigit() else line_no

    if amount_error:
        quarantine.append(_quarantine(source, line_no, raw, amount_error,
                                      pcolumns.cell(cells, index, "amount")))
        return None
    if amount == 0:
        quarantine.append(_quarantine(source, line_no, raw, W_AMOUNT_ZERO))
        return None

    line_type, type_error = pcolumns.classify_line_type(raw_type, amount)
    if type_error:
        quarantine.append(_quarantine(source, line_no, raw, type_error, raw_type))
        return None
    # 符号与类型一致性：抵扣必须为负，应付必须为正。
    if line_type == LINE_TYPE_DEDUCTION and amount > 0:
        quarantine.append(_quarantine(source, line_no, raw, W_AMOUNT_INVALID, "抵扣行为正数"))
        return None
    if line_type == LINE_TYPE_PAYABLE and amount < 0:
        quarantine.append(_quarantine(source, line_no, raw, W_AMOUNT_INVALID, "应付行为负数"))
        return None

    matched = matching.match(supplier_id, supplier_name, record_index,
                             supplier_cluster, clusters_by_id)
    if not matched["ok"]:
        quarantine.append(_quarantine(source, line_no, raw,
                                      matched["quarantine_code"], matched["detail"]))
        return None

    line_key = pcolumns.line_key(source, src_line_no, supplier_id, doc_no, line_type, amount)
    name_warning = matched["block_code"] if matched["block_code"] == W_NAME_MISMATCH else ""
    if line_type == LINE_TYPE_PAYABLE:
        # 应付行：名称不符只预警不阻断付款；税号冲突簇则阻断整主体付款。
        if matched["block_code"] == W_CLUSTER_CONFLICT:
            status, block_code, block_detail = (
                STATUS_BLOCKED, W_CLUSTER_CONFLICT, matched["detail"])
        else:
            status, block_code, block_detail = STATUS_PAYABLE, "", ""
    else:
        # 抵扣行默认待核；若主体本身异常（名称不符/税号冲突）则挂起阻断。
        if matched["block_code"]:
            status = STATUS_BLOCKED
            block_code = matched["block_code"]
            block_detail = matched["detail"]
        else:
            status = STATUS_PENDING
            block_code = W_DEDUCTION_PENDING
            block_detail = "退款抵扣待复核核销，暂不抵减"

    return {
        "line_key": line_key,
        "src_line_no": src_line_no,
        "supplier_id": supplier_id,
        "supplier_name": supplier_name,
        "doc_no": doc_no,
        "line_type": line_type,
        "amount": amount,
        "currency": currency,
        "remark": remark,
        "cluster_id": matched["cluster_id"],
        "matched_name": matched["matched_name"],
        "status": status,
        "block_code": block_code,
        "block_detail": block_detail,
        "warning_code": name_warning,
        "writeoff_version": 0,
        "source_file": source,
        "raw_line": raw,
    }


def _reapply_writeoffs(run_id: str, batch_id: str, lines: list) -> list:
    """重跑时把已存在的核销决定按行键接回；没有历史决定的行保持初始状态。"""
    for line in lines:
        decision = store.get_writeoff(run_id, line["line_key"])
        if decision is None:
            continue
        line["writeoff_version"] = decision["version"]
        if line["line_type"] != LINE_TYPE_DEDUCTION:
            continue
        if decision["action"] == "approve":
            line["status"] = STATUS_APPROVED
            line["block_code"] = ""
            line["block_detail"] = ""
        else:
            line["status"] = STATUS_REJECTED
            line["block_code"] = W_WRITEOFF_REJECTED
            line["block_detail"] = "复核驳回，不参与抵减"
    return lines


def _count(input_rows: int, lines: list, quarantine: list) -> dict:
    payable = [x for x in lines if x["line_type"] == LINE_TYPE_PAYABLE]
    deductions = [x for x in lines if x["line_type"] == LINE_TYPE_DEDUCTION]
    return {
        "input_rows": input_rows,
        "accepted": len(lines),
        "payable_lines": len(payable),
        "deduction_lines": len(deductions),
        "pending": sum(1 for x in deductions if x["status"] == STATUS_PENDING),
        "approved": sum(1 for x in deductions if x["status"] == STATUS_APPROVED),
        "rejected": sum(1 for x in deductions if x["status"] == STATUS_REJECTED),
        "blocked": sum(1 for x in deductions if x["status"] == STATUS_BLOCKED),
        "quarantined": len(quarantine),
    }


def ensure_payment(run_id: str, force: bool = False) -> dict:
    existing = store.latest_payment_batch(run_id)
    if existing and not force:
        return existing
    return import_payment(run_id, force=force)


# ---- 读取与汇总 ----

def list_lines(run_id: str, status: str = "", line_type: str = "") -> list:
    return store.list_payment_lines(run_id, status, line_type)


def pending_deductions(run_id: str) -> list:
    """待核/阻断的抵扣行：复核页的工作队列。"""
    rows = store.list_payment_lines(run_id, line_type=LINE_TYPE_DEDUCTION)
    return [row for row in rows if row["status"] in (STATUS_PENDING, STATUS_BLOCKED)]


def cluster_payment(run_id: str) -> list:
    """每个主体簇的应付、已核抵扣、未核抵扣与可付净额，以及阻断原因。

    可付净额 = 应付合计 + 已核销抵扣（负数）。未核/驳回/阻断抵扣一律不进净额，
    只在未核抵扣列单独披露，保证复核页、主数据与导出三处口径一致。
    """
    cluster_service.ensure_clusters(run_id)
    clusters = store.list_clusters(run_id)
    lines = store.list_payment_lines(run_id)
    by_cluster = {c["cluster_id"]: [] for c in clusters}
    for line in lines:
        by_cluster.setdefault(line["cluster_id"], []).append(line)

    result = []
    for cluster in clusters:
        cid = cluster["cluster_id"]
        cluster_lines = by_cluster.get(cid, [])
        payable_total = sum(
            (Decimal(x["amount"]) for x in cluster_lines
             if x["line_type"] == LINE_TYPE_PAYABLE), Decimal("0"))
        approved_deduction = sum(
            (Decimal(x["amount"]) for x in cluster_lines
             if x["line_type"] == LINE_TYPE_DEDUCTION and x["status"] == STATUS_APPROVED),
            Decimal("0"))
        pending_deduction = sum(
            (Decimal(x["amount"]) for x in cluster_lines
             if x["line_type"] == LINE_TYPE_DEDUCTION
             and x["status"] in (STATUS_PENDING, STATUS_BLOCKED)), Decimal("0"))
        rejected_deduction = sum(
            (Decimal(x["amount"]) for x in cluster_lines
             if x["line_type"] == LINE_TYPE_DEDUCTION and x["status"] == STATUS_REJECTED),
            Decimal("0"))
        net = payable_total + approved_deduction
        block_codes = sorted({x["block_code"] for x in cluster_lines if x["block_code"]})
        warning_codes = sorted({x["warning_code"] for x in cluster_lines if x.get("warning_code")})
        has_lines = bool(cluster_lines)
        # 只有存在付款行时，负净额才构成真实阻断。
        blocked_pay = has_lines and net < 0
        result.append({
            "cluster_id": cid,
            "rep_id": cluster["rep_id"],
            "tax_id": cluster["tax_id"],
            "conflict": bool(cluster["conflict"]),
            "payable_total": payable_total,
            "approved_deduction": approved_deduction,
            "pending_deduction": pending_deduction,
            "rejected_deduction": rejected_deduction,
            "net_payable": net,
            "block_codes": block_codes,
            "warning_codes": warning_codes,
            "payment_blocked": blocked_pay,
            "block_reason": _block_reason(block_codes, blocked_pay,
                                           bool(cluster["conflict"]), has_lines),
        })
    return result


def _block_reason(block_codes, blocked_pay, cluster_conflict, has_lines) -> str:
    reasons = list(block_codes)
    if blocked_pay and "W_NET_NEGATIVE" not in reasons:
        reasons.append("W_NET_NEGATIVE")
    # 无付款行的冲突簇与付款无关，不产生付款阻断原因。
    if has_lines and cluster_conflict and "W_CLUSTER_CONFLICT" not in reasons:
        reasons.append("W_CLUSTER_CONFLICT")
    return ",".join(reasons)


def summary(run_id: str) -> dict:
    batch = store.latest_payment_batch(run_id)
    rows = store.list_payment_lines(run_id)
    cluster_rows = cluster_payment(run_id)
    payable = sum((Decimal(x["amount"]) for x in rows
                   if x["line_type"] == LINE_TYPE_PAYABLE), Decimal("0"))
    approved = sum((Decimal(x["amount"]) for x in rows
                    if x["line_type"] == LINE_TYPE_DEDUCTION
                    and x["status"] == STATUS_APPROVED), Decimal("0"))
    pending = sum((Decimal(x["amount"]) for x in rows
                   if x["line_type"] == LINE_TYPE_DEDUCTION
                   and x["status"] in (STATUS_PENDING, STATUS_BLOCKED)), Decimal("0"))
    net = payable + approved
    return {
        "run_id": run_id,
        "batch_id": batch["batch_id"] if batch else "",
        "counts": json.loads(batch["counts_json"]) if batch else {},
        "payable_total": _q(payable),
        "approved_deduction": _q(approved),
        "pending_deduction": _q(pending),
        "net_payable": _q(net),
        "clusters_with_payment": sum(1 for c in cluster_rows
                                     if c["payable_total"] != 0 or c["approved_deduction"] != 0
                                     or c["pending_deduction"] != 0
                                     or c["rejected_deduction"] != 0),
        # 只统计实际发生付款、且带阻断原因的簇；无付款行的冲突簇不计入。
        "blocked_clusters": sum(
            1 for c in cluster_rows
            if c["block_reason"] and (
                c["payable_total"] != 0 or c["approved_deduction"] != 0
                or c["pending_deduction"] != 0 or c["rejected_deduction"] != 0)),
    }


def reconcile(run_id: str) -> dict:
    """付款侧对账：行级守恒与净额三处口径一致（页面/主数据/导出共用本函数）。"""
    rows = store.list_payment_lines(run_id)
    cluster_rows = cluster_payment(run_id)
    batch = store.latest_payment_batch(run_id)
    counts = json.loads(batch["counts_json"]) if batch else {}

    line_payable = sum((Decimal(x["amount"]) for x in rows
                        if x["line_type"] == LINE_TYPE_PAYABLE), Decimal("0"))
    line_approved = sum((Decimal(x["amount"]) for x in rows
                         if x["line_type"] == LINE_TYPE_DEDUCTION
                         and x["status"] == STATUS_APPROVED), Decimal("0"))
    line_pending = sum((Decimal(x["amount"]) for x in rows
                        if x["line_type"] == LINE_TYPE_DEDUCTION
                        and x["status"] in (STATUS_PENDING, STATUS_BLOCKED)), Decimal("0"))
    line_rejected = sum((Decimal(x["amount"]) for x in rows
                         if x["line_type"] == LINE_TYPE_DEDUCTION
                         and x["status"] == STATUS_REJECTED), Decimal("0"))

    cluster_payable = sum((Decimal(c["payable_total"]) for c in cluster_rows), Decimal("0"))
    cluster_approved = sum((Decimal(c["approved_deduction"]) for c in cluster_rows), Decimal("0"))
    cluster_pending = sum((Decimal(c["pending_deduction"]) for c in cluster_rows), Decimal("0"))
    cluster_net = sum((Decimal(c["net_payable"]) for c in cluster_rows), Decimal("0"))

    # 未核抵扣不得抵减：净额只能由应付与已核抵扣构成。
    net_identity = (line_payable + line_approved) == cluster_net
    quarantined_rows = store.list_payment_quarantine(run_id)
    checks = [
        {"name": "应付行级=簇级", "left": _q(line_payable), "right": _q(cluster_payable),
         "ok": line_payable == cluster_payable},
        {"name": "已核抵扣行级=簇级", "left": _q(line_approved), "right": _q(cluster_approved),
         "ok": line_approved == cluster_approved},
        {"name": "未核抵扣不进净额", "left": _q(line_pending), "right": _q(cluster_pending),
         "ok": line_pending == cluster_pending and net_identity},
        {"name": "可付净额口径一致", "left": _q(line_payable + line_approved),
         "right": _q(cluster_net), "ok": net_identity},
        {"name": "付款行守恒", "left": counts.get("input_rows", 0),
         "right": len(rows) + len(quarantined_rows),
         "ok": counts.get("input_rows", 0) == len(rows) + len(quarantined_rows)},
    ]
    # 抵扣归属守恒：每条抵扣行都锁在一个真实存在的簇上，且未跨主体。
    valid_clusters = {c["cluster_id"] for c in cluster_rows}
    deductions = [x for x in rows if x["line_type"] == LINE_TYPE_DEDUCTION]
    bound = all(x["cluster_id"] in valid_clusters for x in deductions)
    checks.append({"name": "抵扣均锁定本主体", "left": len(deductions),
                   "right": sum(1 for x in deductions if x["cluster_id"] in valid_clusters),
                   "ok": bound})
    return {
        "run_id": run_id,
        "batch_id": batch["batch_id"] if batch else "",
        "payable_total": _q(line_payable),
        "approved_deduction": _q(line_approved),
        "pending_deduction": _q(line_pending),
        "rejected_deduction": _q(line_rejected),
        "net_payable": _q(cluster_net),
        "checks": checks,
        "ok": all(check["ok"] for check in checks),
    }
