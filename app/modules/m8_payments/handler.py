"""M8 路由：付款批次导入、付款行与待核抵扣、核销决定、簇净额与对账。"""
from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from ... import store
from ..m1_intake import service as intake_service
from . import payment_out, service, writeoff

router = APIRouter(prefix="/api", tags=["m8-payments"])

CSV_TYPE = "text/csv; charset=utf-8"


def _csv_response(content: str, filename: str) -> Response:
    return Response(
        content="﻿" + content,
        media_type=CSV_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class PaymentImportRequest(BaseModel):
    files: list[str] | None = None
    force: bool = False


class WriteoffRequest(BaseModel):
    action: str = Field(min_length=2, max_length=10)
    expected_version: int = Field(ge=0)
    operator: str = Field(default="付款复核组", max_length=40)


def _run():
    return intake_service.ensure_run()


@router.post("/payments/import")
def import_payments(body: PaymentImportRequest | None = None):
    payload = body or PaymentImportRequest()
    run = _run()
    batch = service.import_payment(run["run_id"], files=payload.files, force=payload.force)
    return service.summary(run["run_id"]) | {"batch_id": batch["batch_id"]}


@router.get("/payments/import")
def payment_import_status():
    run = _run()
    service.ensure_payment(run["run_id"])
    return service.summary(run["run_id"])


@router.get("/payments/lines")
def payment_lines(status: str = "", line_type: str = "", limit: int = 200):
    run = _run()
    service.ensure_payment(run["run_id"])
    rows = service.list_lines(run["run_id"], status, line_type)
    return {"run_id": run["run_id"], "total": len(rows), "items": rows[:limit]}


@router.get("/payments/pending")
def payment_pending():
    run = _run()
    service.ensure_payment(run["run_id"])
    items = service.pending_deductions(run["run_id"])
    return {"run_id": run["run_id"], "total": len(items), "items": items}


@router.get("/payments/clusters")
def payment_clusters():
    run = _run()
    service.ensure_payment(run["run_id"])
    return {"run_id": run["run_id"], "items": service.cluster_payment(run["run_id"])}


@router.get("/payments/reconcile")
def payment_reconcile():
    run = _run()
    service.ensure_payment(run["run_id"])
    return service.reconcile(run["run_id"])


@router.get("/payments/quarantine")
def payment_quarantine():
    run = _run()
    service.ensure_payment(run["run_id"])
    rows = store.list_payment_quarantine(run["run_id"])
    return {"run_id": run["run_id"], "total": len(rows), "items": rows}


@router.post("/payments/{line_key}/writeoff")
def submit_writeoff(line_key: str, body: WriteoffRequest):
    run = _run()
    service.ensure_payment(run["run_id"])
    record = writeoff.decide(run["run_id"], line_key, body.action,
                             body.expected_version, body.operator)
    return {"run_id": run["run_id"], "writeoff": record,
            "summary": service.summary(run["run_id"])}


@router.get("/payments/writeoffs")
def payment_writeoffs():
    run = _run()
    service.ensure_payment(run["run_id"])
    return {"run_id": run["run_id"], "items": store.list_writeoffs(run["run_id"])}


@router.get("/payments/export/clusters.csv")
def export_payment_clusters():
    run = _run()
    service.ensure_payment(run["run_id"])
    rows = service.cluster_payment(run["run_id"])
    return _csv_response(payment_out.render_cluster_payment(rows),
                         f"pay-clusters-{run['run_id']}.csv")


@router.get("/payments/export/lines.csv")
def export_payment_lines():
    run = _run()
    service.ensure_payment(run["run_id"])
    rows = service.list_lines(run["run_id"])
    return _csv_response(payment_out.render_lines(rows),
                         f"pay-lines-{run['run_id']}.csv")


@router.get("/payments/export/quarantine.csv")
def export_payment_quarantine():
    run = _run()
    service.ensure_payment(run["run_id"])
    rows = store.list_payment_quarantine(run["run_id"])
    return _csv_response(payment_out.render_quarantine(rows),
                         f"pay-quarantine-{run['run_id']}.csv")
