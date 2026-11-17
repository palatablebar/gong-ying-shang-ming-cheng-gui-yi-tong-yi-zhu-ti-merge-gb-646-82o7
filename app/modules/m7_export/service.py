"""M7 导出编排：主数据、成员分摊、隔离区、冲突、运行清单。"""
from __future__ import annotations

import json

from ... import store
from ...errors import NotFound
from ...manifest import build_manifest
from ...reasons import code_table as reason_code_table
from ...reasons import V_UNKNOWN_RUN
from ..m5_clusters import service as cluster_service
from ..m8_payments import service as payment_service
from . import manifest_out, master_out, quarantine_out


def _context(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise NotFound(V_UNKNOWN_RUN, run_id)
    cluster_service.ensure_clusters(run_id)
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    clusters = store.list_clusters(run_id)
    members = store.list_cluster_members(run_id)
    return {"run": run, "records": records, "clusters": clusters, "members": members}


def _payment_by_cluster(run_id: str):
    """存在付款批次时返回簇级净额映射；否则返回 None，主数据保持原表头。"""
    if not store.latest_payment_batch(run_id):
        return None
    payment_service.ensure_payment(run_id)
    return {row["cluster_id"]: row for row in payment_service.cluster_payment(run_id)}


def master_csv(run_id: str) -> str:
    context = _context(run_id)
    payment_by_cluster = _payment_by_cluster(run_id)
    return master_out.render(context["clusters"], context["members"], context["records"],
                             payment_by_cluster)


def members_csv(run_id: str) -> str:
    context = _context(run_id)
    return master_out.render_members(context["clusters"], context["members"], context["records"])


def quarantine_csv(run_id: str) -> str:
    run = store.get_run(run_id)
    if not run:
        raise NotFound(V_UNKNOWN_RUN, run_id)
    return quarantine_out.render(store.list_quarantine(run_id))


def conflicts_csv(run_id: str) -> str:
    run = store.get_run(run_id)
    if not run:
        raise NotFound(V_UNKNOWN_RUN, run_id)
    return quarantine_out.render_conflicts(store.list_conflicts(run_id))


def manifest_json(run_id: str) -> str:
    run = store.get_run(run_id)
    if not run:
        raise NotFound(V_UNKNOWN_RUN, run_id)
    cluster_service.ensure_clusters(run_id)
    ledger = cluster_service.reconcile(run_id)
    manifest = build_manifest(run, json.loads(run["files_json"]), ledger, reason_code_table())
    # 付款批次存在时附上报销净额对账，保证运行清单与页面、导出同口径。
    if store.latest_payment_batch(run_id):
        payment_service.ensure_payment(run_id)
        manifest["payment_reconcile"] = payment_service.reconcile(run_id)
    return manifest_out.render(manifest)
