"""M8 付款 CSV 导出：簇级可付净额、行级抵扣证据、隔离行。

三处口径一致：本导出的可付净额与付款页、reconcile() 完全相同，
都只把"已核销抵扣"计入抵减，未核/驳回/阻断抵扣单列披露。
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from ...reasons import label_of

CLUSTER_HEADER = ("主体编号", "代表供应商号", "税号", "应付合计(元)", "已核抵扣(元)",
                  "未核抵扣(元)", "驳回抵扣(元)", "可付净额(元)", "是否阻断", "阻断原因")

LINE_HEADER = ("行键", "来源行号", "供应商号", "匹配名称", "主体编号", "行类型",
               "金额(元)", "状态", "阻断原因码", "阻断原因", "单据号", "备注")

QUARANTINE_HEADER = ("来源文件", "行号", "原因码", "原因", "详情", "原始内容")

TYPE_LABELS = {"payable": "应付", "deduction": "退款抵扣"}
STATUS_LABELS = {
    "payable": "应付",
    "pending": "待核",
    "approved": "已核",
    "rejected": "已驳回",
    "blocked": "阻断",
}


def _money(value) -> str:
    return f"{Decimal(str(value)):.2f}"


def render_cluster_payment(cluster_rows) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CLUSTER_HEADER)
    for row in cluster_rows:
        reason_labels = "；".join(label_of(code) for code in row["block_codes"])
        if row["payment_blocked"] and "W_NET_NEGATIVE" not in row["block_codes"]:
            reason_labels = (reason_labels + "；" if reason_labels else "") + label_of("W_NET_NEGATIVE")
        writer.writerow([
            row["cluster_id"],
            row["rep_id"],
            row["tax_id"],
            _money(row["payable_total"]),
            _money(row["approved_deduction"]),
            _money(row["pending_deduction"]),
            _money(row["rejected_deduction"]),
            _money(row["net_payable"]),
            "是" if row["block_reason"] else "否",
            reason_labels,
        ])
    return buffer.getvalue()


def render_lines(lines) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(LINE_HEADER)
    for line in lines:
        writer.writerow([
            line["line_key"],
            line["src_line_no"],
            line["supplier_id"],
            line["matched_name"],
            line["cluster_id"],
            TYPE_LABELS.get(line["line_type"], line["line_type"]),
            _money(line["amount"]),
            STATUS_LABELS.get(line["status"], line["status"]),
            line["block_code"],
            label_of(line["block_code"]) if line["block_code"] else "",
            line["doc_no"],
            line["remark"],
        ])
    return buffer.getvalue()


def render_quarantine(rows) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(QUARANTINE_HEADER)
    for row in rows:
        writer.writerow([
            row["source_file"],
            row["line_no"],
            row["reason_code"],
            label_of(row["reason_code"]),
            row.get("detail", ""),
            (row.get("raw_line") or "").replace("\n", " "),
        ])
    return buffer.getvalue()
