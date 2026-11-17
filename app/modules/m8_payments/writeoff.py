"""M8 抵扣核销的乐观锁：expected_version 与当前版本不一致即拒绝。

核销决定独立于付款行存储。整批重跑会替换付款行，但不会动这张表，
因此同批重跑不能改变已确认归属；行键（内容指纹）把决定稳定锚回原行。
"""
from __future__ import annotations

from ... import store
from ...config import WRITEOFF_ACTIONS, WRITEOFF_APPROVE
from ...errors import Conflict, DomainError, NotFound
from ...manifest import utc_iso, utc_now
from ...reasons import (
    V_BAD_DECISION,
    V_STALE_DECISION,
    V_UNKNOWN_RUN,
    W_CLUSTER_CONFLICT,
    W_WRITEOFF_REJECTED,
)


def current_version(run_id: str, line_key: str) -> int:
    decision = store.get_writeoff(run_id, line_key)
    return decision["version"] if decision else 0


def decide(run_id: str, line_key: str, action: str, expected_version: int,
           operator: str = "付款复核组") -> dict:
    if action not in WRITEOFF_ACTIONS:
        raise DomainError(V_BAD_DECISION, action)
    line = store.get_payment_line(run_id, line_key)
    if line is None:
        raise NotFound(V_UNKNOWN_RUN, line_key)
    if line["line_type"] != "deduction":
        # 应付行不存在抵扣核销，只对退款抵扣行开放。
        raise DomainError(V_BAD_DECISION, "仅抵扣行可核销")
    # 阻断行（主体税号冲突/名称不符）身份未定，禁止核销抵减，须先在主数据层解阻。
    if line["status"] == "blocked" and action == WRITEOFF_APPROVE:
        raise DomainError(W_CLUSTER_CONFLICT, f"{line['line_key']} 主体阻断未解除")
    current = current_version(run_id, line_key)
    if expected_version != current:
        raise Conflict(V_STALE_DECISION, f"当前版本 {current}")

    record = {
        "run_id": run_id,
        "line_key": line_key,
        "action": action,
        "expected_version": expected_version,
        "version": current + 1,
        "operator": operator,
        "decided_at": utc_iso(utc_now()),
    }
    store.save_writeoff(record)

    # 同步付款行状态：通过则解除待核阻断进入可抵减；驳回则永久挂起不抵减。
    if action == WRITEOFF_APPROVE:
        status, block_code, block_detail = "approved", "", ""
    else:
        status, block_code, block_detail = "rejected", W_WRITEOFF_REJECTED, "复核驳回，不参与抵减"
    store.update_payment_line_state(run_id, line_key, status, block_code,
                                    block_detail, record["version"])
    return record
