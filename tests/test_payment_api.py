"""M8 HTTP 接口：导入、待核、核销决定、对账、导出与页面渲染。"""
from __future__ import annotations

import csv
import io


def _pending(client, supplier_id):
    items = client.get("/api/payments/pending").json()["items"]
    return next(x for x in items if x["supplier_id"] == supplier_id)


def test_import_endpoint_and_batch(client):
    resp = client.post("/api/payments/import", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch_id"].startswith("PAY-")
    assert body["counts"]["accepted"] == 14
    assert body["net_payable"] == "53300.00"


def test_payment_page_renders(client):
    client.post("/api/payments/import", json={})
    page = client.get("/payments")
    assert page.status_code == 200
    assert "付款复核" in page.text
    assert "可付净额" in page.text
    assert "待核抵扣" in page.text
    assert "W_DEDUCTION_PENDING" in page.text


def test_writeoff_flow_via_api(client):
    client.post("/api/payments/import", json={})
    target = _pending(client, "S-0002")
    # 陈旧版本被拒。
    stale = client.post(f"/api/payments/{target['line_key']}/writeoff",
                        json={"action": "approve", "expected_version": 9})
    assert stale.status_code == 409
    assert stale.json()["reason_code"] == "V_STALE_DECISION"
    # 正常核销，净额下降。
    ok = client.post(f"/api/payments/{target['line_key']}/writeoff",
                     json={"action": "approve", "expected_version": 0})
    assert ok.status_code == 200
    assert ok.json()["summary"]["net_payable"] == "51300.00"


def test_blocked_deduction_cannot_approve(client):
    client.post("/api/payments/import", json={})
    blocked = next(x for x in client.get("/api/payments/pending").json()["items"]
                   if x["status"] == "blocked")
    resp = client.post(f"/api/payments/{blocked['line_key']}/writeoff",
                       json={"action": "approve", "expected_version": 0})
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "W_CLUSTER_CONFLICT"


def test_reconcile_endpoint(client):
    client.post("/api/payments/import", json={})
    rec = client.get("/api/payments/reconcile").json()
    assert rec["ok"] is True
    assert rec["net_payable"] == "53300.00"


def test_export_endpoints_consistent(client):
    client.post("/api/payments/import", json={})
    for path in ("/api/payments/export/clusters.csv", "/api/payments/export/lines.csv",
                 "/api/payments/export/quarantine.csv"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "attachment" in resp.headers["content-disposition"]
        assert resp.text.startswith("﻿")
    clusters = client.get("/api/payments/export/clusters.csv").text
    rows = list(csv.reader(io.StringIO(clusters.lstrip("﻿"))))
    assert rows[0][0] == "主体编号"
    assert "可付净额(元)" in rows[0]


def test_rerun_idempotent_via_api(client):
    first = client.post("/api/payments/import", json={"force": True}).json()
    target = _pending(client, "S-0070")
    client.post(f"/api/payments/{target['line_key']}/writeoff",
                json={"action": "approve", "expected_version": 0})
    second = client.post("/api/payments/import", json={"force": True}).json()
    assert first["batch_id"] == second["batch_id"]
    # 首次导入尚无核销；重跑后已核抵扣保留为 -150，未翻倍或丢失。
    assert first["approved_deduction"] == "0.00"
    assert second["approved_deduction"] == "-150.00"
    assert second["net_payable"] == "53150.00"


def test_quarantine_endpoint(client):
    client.post("/api/payments/import", json={})
    body = client.get("/api/payments/quarantine").json()
    assert body["total"] == 7
    codes = {item["reason_code"] for item in body["items"]}
    assert "W_SUPPLIER_UNKNOWN" in codes
