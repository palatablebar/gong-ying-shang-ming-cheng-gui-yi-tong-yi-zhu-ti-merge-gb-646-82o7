"""M8 主体匹配：付款行的供应商号 → 已采信记录 → 合并簇。

抵扣归属的硬规则：抵扣行只能挂到它供应商号所在记录的那一簇，绝不允许落到同名的
别的主体。匹配不上主数据、或所在簇存在税号冲突，都给出明确阻断/隔离原因。
"""
from __future__ import annotations

from ...reasons import W_CLUSTER_CONFLICT, W_NAME_MISMATCH, W_SUPPLIER_UNKNOWN


def build_record_index(records) -> dict:
    return {record["supplier_id"]: record for record in records}


def build_supplier_cluster(members) -> dict:
    """cluster_members 行 -> {supplier_id: cluster_id}。"""
    return {member["supplier_id"]: member["cluster_id"] for member in members}


def match(supplier_id: str, supplier_name: str, record_index: dict,
          supplier_cluster: dict, clusters_by_id: dict) -> dict:
    """返回匹配结果 dict：ok / cluster_id / matched_name / block_code / detail。

    - 供应商号不在主数据：不可采信（调用方隔离 W_SUPPLIER_UNKNOWN）。
    - 名称与主数据不符：仍按供应商号锁簇，但带 W_NAME_MISMATCH 阻断待人工确认。
    - 簇内税号冲突：带 W_CLUSTER_CONFLICT 阻断，避免在身份未定的主体上抵减。
    """
    record = record_index.get(supplier_id)
    if record is None:
        return {
            "ok": False,
            "quarantine_code": W_SUPPLIER_UNKNOWN,
            "cluster_id": "",
            "matched_name": "",
            "block_code": "",
            "detail": supplier_id,
        }

    cluster_id = supplier_cluster.get(supplier_id, "")
    matched_name = record.get("raw_name", "")
    block_code = ""
    detail = ""

    declared = (supplier_name or "").strip()
    if declared and matched_name and declared != matched_name:
        block_code = W_NAME_MISMATCH
        detail = f"付款名「{_truncate(declared)}」≠ 主数据「{_truncate(matched_name)}」"

    cluster = clusters_by_id.get(cluster_id)
    if cluster is not None and cluster.get("conflict"):
        # 税号冲突优先级更高：身份都未确定，不能在该主体上做净额抵减。
        block_code = W_CLUSTER_CONFLICT
        detail = f"簇 {cluster_id} 税号冲突"

    return {
        "ok": True,
        "quarantine_code": "",
        "cluster_id": cluster_id,
        "matched_name": matched_name,
        "block_code": block_code,
        "detail": detail,
    }


def _truncate(text: str, limit: int = 20) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
