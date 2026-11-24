"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    EntryPayload,
    EvidencePayload,
    PageResult,
    ReplayPayload,
    SignOffPayload,
)
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


def shift_service():
    """交接表专用路由：只允许分派到登记过的交接服务。"""
    from app.services.shift_handover import ShiftHandoverError, ShiftHandoverService

    service = service_for("shift_handover")
    if not isinstance(service, ShiftHandoverService):
        raise HTTPException(status_code=503, detail="班次交接服务未就绪")
    return service, ShiftHandoverError


@router.get("/shift_handover/{entry_id}/sheet", response_model=dict)
def shift_sheet(entry_id: int, role: str | None = Query(default=None)) -> dict[str, Any]:
    service, error_type = shift_service()
    try:
        return service.sheet(entry_id, role=role)
    except error_type as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/shift_handover/{entry_id}/heartbeat-summary", response_model=dict)
def shift_heartbeat_summary(entry_id: int, role: str | None = Query(default=None)) -> dict[str, Any]:
    service, error_type = shift_service()
    try:
        return service.heartbeat_summary(entry_id, role=role)
    except error_type as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/device_health/shift_summary", response_model=dict)
def device_health_shift_summary(
    handover_id: int | None = Query(default=None),
    role: str | None = Query(default=None),
) -> dict[str, Any]:
    """采集心跳汇总：带 handover_id 看单个班次，不带则汇总全部交接落盘点。"""
    from app.services.device_health import DeviceHealthService
    from app.services.shift_handover import ShiftHandoverError

    service = service_for("device_health")
    if not isinstance(service, DeviceHealthService):
        raise HTTPException(status_code=503, detail="采集心跳服务未就绪")
    try:
        if handover_id is None:
            return service.summarize_all_shifts(role=role)
        return service.summarize_shift(handover_id, role=role)
    except ShiftHandoverError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/shift_handover/{entry_id}/evidence", response_model=ActionResult)
def shift_submit_evidence(entry_id: int, payload: EvidencePayload) -> ActionResult:
    service, error_type = shift_service()
    try:
        result = service.submit_evidence(
            entry_id,
            moment=payload.moment,
            value=payload.value,
            source_type=payload.source_type,
            role=payload.role,
            submitter=payload.submitter,
        )
    except error_type as exc:
        return ActionResult(ok=False, message=str(exc))
    entry = service.get_entry(entry_id)
    return ActionResult(ok=True, message=result["message"], entry=entry)


@router.post("/shift_handover/{entry_id}/signoff", response_model=ActionResult)
def shift_sign_off(entry_id: int, payload: SignOffPayload) -> ActionResult:
    service, error_type = shift_service()
    try:
        result = service.sign_off(
            entry_id,
            evidence_id=payload.evidence_id,
            shift=payload.shift,
            role=payload.role,
            signer=payload.signer,
        )
    except error_type as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=result["message"], entry=service.get_entry(entry_id))


@router.post("/shift_handover/{entry_id}/replay", response_model=ActionResult)
def shift_record_replay(entry_id: int, payload: ReplayPayload) -> ActionResult:
    service, error_type = shift_service()
    try:
        result = service.record_replay(
            entry_id,
            samples=payload.samples,
            role=payload.role,
            submitter=payload.submitter,
        )
    except error_type as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(
        ok=bool(result["accepted"]),
        message=f"补采登记 {len(result['accepted'])} 条，拒绝 {len(result['rejected'])} 条",
        entry=service.get_entry(entry_id),
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    if getattr(service, "ROLE_AWARE", False):
        entry, message = service.run_action(entry_id, payload.action.strip(), role=payload.role)
    else:
        entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
