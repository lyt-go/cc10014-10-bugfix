"""ODS表版本清单接口：跨模块持久化贴源表在线/下线顺序、阶段与DWD父层引用。

清单数据来自已上线贴源表；列表只返回在线与下线顺序及父层引用数据结果，
完整的阶段历史、冲突记录与完整版本信息仅在详情接口暴露。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.ods_manifest import CONFLICT_POLICIES, manifest_service

router = APIRouter(prefix="/api/ods_manifest", tags=["ODS表版本清单"])


@router.get("", response_model=PageResult[dict])
def list_manifest(
    status: str | None = Query(default=None, description="在线、已下线"),
) -> PageResult[dict]:
    """读取清单：只保留在线与下线顺序及父层引用数据结果；空表时 items 为空。"""
    items = manifest_service.list_entries(status=status)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/empty-hint")
def empty_hint() -> dict[str, str]:
    """清单为空时给页面的说明文案。"""
    items = manifest_service.list_entries()
    if items:
        return {"empty": "false", "message": ""}
    return {"empty": "true", "message": "ODS表版本清单暂无数据：当前没有已上线贴源表"}


@router.get("/{ods_code}", response_model=dict)
def get_manifest_entry(ods_code: str) -> dict:
    """清单详情：阶段历史、冲突记录、完整版本、父层引用全量结果。"""
    entry = manifest_service.get_entry(ods_code)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"贴源表 {ods_code} 不在ODS表版本清单中")
    return entry


@router.get("/dwd/{dwd_id}", response_model=dict)
def get_manifest_for_dwd(dwd_id: int) -> dict:
    """明细层管理详情页跨模块读取父层贴源表的清单结果。"""
    entry = manifest_service.get_entry_for_dwd(dwd_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"明细表 {dwd_id} 的父层贴源表暂无清单记录")
    return entry


@router.post("/sync", response_model=ActionResult)
def sync_manifest() -> ActionResult:
    """主动从已上线贴源表刷新清单（已登记条目不重复建档）。"""
    created = manifest_service.sync_from_online_ods()
    return ActionResult(ok=True, message=f"清单刷新完成，新增登记 {created} 张已上线贴源表")


@router.post("/{ods_code}/interrupt", response_model=ActionResult)
def simulate_interrupt(ods_code: str) -> ActionResult:
    """模拟清单写入异常中断（演练用）。"""
    entry, message = manifest_service.simulate_interrupt(ods_code)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{ods_code}/retry", response_model=ActionResult)
def retry_interrupt(ods_code: str) -> ActionResult:
    """「先完成当前刷新再提交下线」重试：贴源表回到完整版本，只续写未完成部分。"""
    entry, message = manifest_service.retry_after_interrupt(ods_code)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
