"""M8 付款批次：抵扣锁主体、待核不抵减、核销后净额、阻断原因、隔离与幂等重跑。"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest

from app import store
from app.errors import Conflict, DomainError
from app.modules.m5_clusters import service as cluster_service
from app.modules.m8_payments import pcolumns, service, writeoff
from tests.fixtures import expected as EXP


@pytest.fixture()
def payment(run):
    """名录批次 + 已导入的付款批次。"""
    batch = service.import_payment(run["run_id"])
    return run, batch


def _cluster(run_id, cluster_id):
    return next(c for c in service.cluster_payment(run_id) if c["cluster_id"] == cluster_id)


def _line(run_id, supplier_id):
    return next(x for x in service.list_lines(run_id) if x["supplier_id"] == supplier_id)


# ---------- 采信与隔离 ----------

def test_import_counts(payment):
    run, batch = payment
    counts = service.summary(run["run_id"])["counts"]
    assert counts["input_rows"] == EXP.PAY_INPUT_ROWS
    assert counts["accepted"] == EXP.PAY_ACCEPTED
    assert counts["payable_lines"] == EXP.PAY_PAYABLE_LINES
    assert counts["deduction_lines"] == EXP.PAY_DEDUCTION_LINES
    assert counts["quarantined"] == EXP.PAY_QUARANTINED
    assert batch["rule_version"] == "pay-1.0.0"
    assert batch["batch_id"].startswith("PAY-")


def test_quarantine_reasons(payment):
    run, _ = payment
    rows = store.list_payment_quarantine(run["run_id"])
    codes = {row["reason_code"] for row in rows}
    assert codes == EXP.PAY_QUARANTINE_CODES
    # 未知供应商隔离，绝不落进任何主体。
    unknown = [r for r in rows if r["reason_code"] == "W_SUPPLIER_UNKNOWN"][0]
    assert unknown["detail"] == EXP.PAY_UNKNOWN_SUPPLIER


def test_unknown_supplier_never_enters_cluster(payment):
    """未核/无效抵扣不能减到别的主体：未知供应商根本不产生付款行。"""
    run, _ = payment
    suppliers = {line["supplier_id"] for line in service.list_lines(run["run_id"])}
    assert EXP.PAY_UNKNOWN_SUPPLIER not in suppliers


def test_row_conservation(payment):
    run, _ = payment
    rec = service.reconcile(run["run_id"])
    check = next(c for c in rec["checks"] if c["name"] == "付款行守恒")
    assert check["ok"] is True
    assert int(check["left"]) == EXP.PAY_INPUT_ROWS


# ---------- 抵扣锁定本主体 ----------

def test_deduction_bound_to_own_cluster(payment):
    """每条抵扣都锁在其供应商号所在记录的簇上，不跨主体。"""
    run, _ = payment
    rec = service.reconcile(run["run_id"])
    bound = next(c for c in rec["checks"] if c["name"] == "抵扣均锁定本主体")
    assert bound["ok"] is True
    # 同主体多成员的抵扣并入同一簇 C-0001。
    c1 = _cluster(run["run_id"], "C-0001")
    assert c1["pending_deduction"] == EXP.PAY_CLUSTER_0001_DEDUCTION


def test_pending_deduction_not_in_net(payment):
    """待核抵扣不得冲低整簇：净额只含应付，未核抵扣单列。"""
    run, _ = payment
    summary = service.summary(run["run_id"])
    assert summary["net_payable"] == str(EXP.PAY_PAYABLE_TOTAL)
    assert Decimal(summary["pending_deduction"]) != 0
    c1 = _cluster(run["run_id"], "C-0001")
    # 应付 12000，两笔抵扣 -2500.50 仍待核，净额保持 12000。
    assert c1["payable_total"] == Decimal("12000.00")
    assert c1["net_payable"] == Decimal("12000.00")
    assert c1["block_reason"]  # 带待核阻断原因


def test_conflict_cluster_deduction_blocked(payment):
    """税号冲突簇的抵扣直接阻断，复核页可见阻断原因且不能核销。"""
    run, _ = payment
    blocked = [p for p in service.pending_deductions(run["run_id"]) if p["status"] == "blocked"]
    assert len(blocked) == EXP.PAY_BLOCKED
    line = blocked[0]
    assert line["cluster_id"] == EXP.PAY_CONFLICT_CLUSTER
    assert line["block_code"] == "W_CLUSTER_CONFLICT"
    with pytest.raises(DomainError) as err:
        writeoff.decide(run["run_id"], line["line_key"], "approve", 0)
    assert err.value.code == "W_CLUSTER_CONFLICT"


def test_name_mismatch_payable_warns_not_blocks(payment):
    """应付行名称不符只预警，仍可按净额付款；预警码随行可见。"""
    run, _ = payment
    line = _line(run["run_id"], EXP.PAY_NAME_MISMATCH_SUPPLIER)
    assert line["warning_code"] == "W_NAME_MISMATCH"
    assert line["status"] == "payable"
    c20 = _cluster(run["run_id"], "C-0020")
    assert "W_NAME_MISMATCH" in c20["warning_codes"]
    assert c20["net_payable"] == Decimal("7000.00")


# ---------- 核销与净额 ----------

def test_approve_deduction_reduces_net(payment):
    """核销后在同一主体内统一抵减；只减本簇，不影响其他主体。"""
    run, _ = payment
    target = _line(run["run_id"], "S-0002")
    writeoff.decide(run["run_id"], target["line_key"], "approve", 0, "李核")
    c1 = _cluster(run["run_id"], "C-0001")
    assert c1["approved_deduction"] == Decimal("-2000.00")
    assert c1["net_payable"] == Decimal("10000.00")
    # 另一主体净额纹丝不动。
    c7 = _cluster(run["run_id"], "C-0007")
    assert c7["net_payable"] == Decimal("8000.00")


def test_reject_deduction_never_reduces_net(payment):
    run, _ = payment
    target = _line(run["run_id"], "S-0021")
    writeoff.decide(run["run_id"], target["line_key"], "reject", 0)
    c7 = _cluster(run["run_id"], "C-0007")
    assert c7["rejected_deduction"] == EXP.PAY_CLUSTER_0007_DEDUCTION
    assert c7["approved_deduction"] == Decimal("0.00")
    assert c7["net_payable"] == Decimal("8000.00")


def test_writeoff_version_lock(payment):
    run, _ = payment
    target = _line(run["run_id"], "S-0024")
    first = writeoff.decide(run["run_id"], target["line_key"], "approve", 0)
    assert first["version"] == 1
    with pytest.raises(Conflict) as err:
        writeoff.decide(run["run_id"], target["line_key"], "approve", 0)
    assert err.value.code == "V_STALE_DECISION"
    # 仅抵扣行可核销。
    payable = _line(run["run_id"], "S-0001")
    with pytest.raises(DomainError) as err:
        writeoff.decide(run["run_id"], payable["line_key"], "approve", 0)
    assert err.value.code == "V_BAD_DECISION"


def test_only_deduction_line_type_allowed(payment):
    run, _ = payment
    line = _line(run["run_id"], "S-0003")
    assert line["line_type"] == "payable"


def test_matching_name_mismatch_flags_block(run):
    """名称不符的抵扣应锁簇但标记阻断，不允许核销抵减。"""
    from app.modules.m8_payments import matching

    cluster_service.ensure_clusters(run["run_id"])
    records = {r["supplier_id"]: r for r in store.list_records(run["run_id"])}
    supplier_cluster = {m["supplier_id"]: m["cluster_id"]
                        for m in store.list_cluster_members(run["run_id"])}
    clusters_by_id = {c["cluster_id"]: c for c in store.list_clusters(run["run_id"])}
    result = matching.match("S-0001", "完全不同的名称有限公司", records,
                            supplier_cluster, clusters_by_id)
    assert result["ok"] is True
    assert result["block_code"] == "W_NAME_MISMATCH"
    assert result["cluster_id"]  # 仍锁回本主体，不悬空


# ---------- 幂等重跑 ----------

def test_rerun_keeps_confirmed_attribution(payment):
    """同批重跑：批次号不变、已核/驳回状态保留、净额不被改变。"""
    run, batch = payment
    approve_key = _line(run["run_id"], "S-0002")["line_key"]
    reject_key = _line(run["run_id"], "S-0021")["line_key"]
    writeoff.decide(run["run_id"], approve_key, "approve", 0)
    writeoff.decide(run["run_id"], reject_key, "reject", 0)
    net_before = service.summary(run["run_id"])["net_payable"]

    rerun = service.import_payment(run["run_id"], force=True)
    assert rerun["batch_id"] == batch["batch_id"]
    assert service.summary(run["run_id"])["net_payable"] == net_before
    assert _line(run["run_id"], "S-0002")["status"] == "approved"
    assert _line(run["run_id"], "S-0021")["status"] == "rejected"
    # 核销决定没有被重复写入。
    assert len(store.list_writeoffs(run["run_id"])) == 2


def test_rerun_no_double_count(payment):
    """重跑后抵扣金额只计一次，行键稳定，簇级与行级仍然守恒。"""
    run, _ = payment
    service.import_payment(run["run_id"], force=True)
    service.import_payment(run["run_id"], force=True)
    rec = service.reconcile(run["run_id"])
    assert rec["ok"] is True
    deductions = [x for x in service.list_lines(run["run_id"]) if x["line_type"] == "deduction"]
    assert len(deductions) == EXP.PAY_DEDUCTION_LINES


def test_non_force_reimport_reuses_batch(payment):
    """同内容非强制重跑直接复用批次，核销状态与净额保持不变。"""
    run, batch = payment
    key = _line(run["run_id"], "S-0070")["line_key"]
    writeoff.decide(run["run_id"], key, "approve", 0)
    net_before = service.summary(run["run_id"])["net_payable"]
    reused = service.import_payment(run["run_id"], force=False)
    assert reused["batch_id"] == batch["batch_id"]
    assert _line(run["run_id"], "S-0070")["status"] == "approved"
    assert service.summary(run["run_id"])["net_payable"] == net_before
    assert len(store.list_writeoffs(run["run_id"])) == 1


# ---------- 解析单元 ----------

def test_amount_parser_tolerates_fullwidth_and_sign():
    amount, err = pcolumns.parse_amount("－1，234.56")
    assert err is None and amount == Decimal("-1234.56")
    bad, err = pcolumns.parse_amount("abc")
    assert bad is None and err == "W_AMOUNT_INVALID"


def test_classify_line_type():
    assert pcolumns.classify_line_type("退款抵扣", Decimal("-1"))[0] == "deduction"
    assert pcolumns.classify_line_type("应付", Decimal("1"))[0] == "payable"
    assert pcolumns.classify_line_type("", Decimal("-5"))[0] == "deduction"
    assert pcolumns.classify_line_type("奇怪类型", Decimal("1"))[1] == "W_TYPE_UNKNOWN"


def test_line_key_stable():
    k1 = pcolumns.line_key("f.csv", 2, "S-1", "D-1", "deduction", Decimal("-2.00"))
    k2 = pcolumns.line_key("f.csv", 2, "S-1", "D-1", "deduction", Decimal("-2.00"))
    k3 = pcolumns.line_key("f.csv", 3, "S-1", "D-1", "deduction", Decimal("-2.00"))
    assert k1 == k2 and k1 != k3


# ---------- 导出与对账一致 ----------

def test_export_net_matches_reconcile(payment):
    """复核页面、正式导出与对账三处净额必须一致。"""
    run, _ = payment
    approve_key = _line(run["run_id"], "S-0002")["line_key"]
    writeoff.decide(run["run_id"], approve_key, "approve", 0)
    from app.modules.m8_payments import payment_out
    csv_text = payment_out.render_cluster_payment(service.cluster_payment(run["run_id"]))
    rows = list(csv.reader(io.StringIO(csv_text)))
    header = rows[0]
    body = {row[0]: dict(zip(header, row)) for row in rows[1:]}
    rec = service.reconcile(run["run_id"])
    exported_net = sum(Decimal(r["可付净额(元)"]) for r in body.values())
    assert exported_net == Decimal(rec["net_payable"])
    # 未核抵扣列与对账披露一致，且不等于净额（证明未被抵减）。
    exported_pending = sum(Decimal(r["未核抵扣(元)"]) for r in body.values())
    assert exported_pending == Decimal(rec["pending_deduction"])
    assert exported_pending != 0


def test_reconcile_all_checks_pass(payment):
    run, _ = payment
    rec = service.reconcile(run["run_id"])
    assert rec["ok"] is True
    names = {c["name"] for c in rec["checks"]}
    assert {"应付行级=簇级", "已核抵扣行级=簇级", "未核抵扣不进净额",
            "可付净额口径一致", "付款行守恒", "抵扣均锁定本主体"} <= names


def test_master_data_net_matches_payment_ledger(payment):
    """正式主数据导出的净额列必须与付款复核台账逐分相等。"""
    import json

    from app.modules.m7_export import service as export_service

    run, _ = payment
    approve_key = _line(run["run_id"], "S-0002")["line_key"]
    writeoff.decide(run["run_id"], approve_key, "approve", 0)

    text = export_service.master_csv(run["run_id"])
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    assert "可付净额(元)" in header and "未核抵扣(元)" in header
    net_index = header.index("可付净额(元)")
    exported_net = sum((Decimal(row[net_index]) for row in rows[1:]), Decimal("0"))
    assert exported_net == Decimal(service.reconcile(run["run_id"])["net_payable"])

    manifest = json.loads(export_service.manifest_json(run["run_id"]))
    assert manifest["payment_reconcile"]["ok"] is True
    assert manifest["payment_reconcile"]["net_payable"] == service.reconcile(run["run_id"])["net_payable"]


def test_master_csv_unchanged_without_payment(run):
    """没有付款批次时主数据表头保持原契约，不追加净额列。"""
    from app.modules.m7_export import service as export_service

    text = export_service.master_csv(run["run_id"])
    header = next(csv.reader(io.StringIO(text)))
    assert "可付净额(元)" not in header
    assert header[0] == "主体编号"
