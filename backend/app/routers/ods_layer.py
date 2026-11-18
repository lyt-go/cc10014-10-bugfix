"""贴源层管理接口：维护贴源表，覆盖提交校验、确认上线、标记下线等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.ods_layer import OdsLayerService

router = APIRouter(prefix="/api/ods_layer", tags=["贴源层管理"])

service = OdsLayerService()

LIST_FIELDS = ["表编号", "来源系统", "表名称", "分区字段", "存储格式", "保留天数", "压缩算法", "创建批次"]
STATUSES = ["草稿", "待校验", "已上线", "已下线"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按表编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已上线、已下线"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、表编号关键字与状态过滤贴源层管理列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ods_layer.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条贴源表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"贴源表 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条贴源表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="贴源表已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条贴源表执行提交校验、确认上线、标记下线；状态只允许按顺序单向推进。

    标记下线支持 conflict_policy：下线优先（默认，ODS立即下线且DWD记录等待父层清单）
    或刷新优先（DWD完成一次刷新后ODS状态才变为已下线）。
    """
    from app.services.ods_manifest import POLICY_OFFLINE_FIRST
    policy = (payload.conflict_policy or POLICY_OFFLINE_FIRST).strip()
    entry, message = service.run_action(entry_id, payload.action.strip(), conflict_policy=policy)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/retry", response_model=ActionResult)
def retry_offline(entry_id: int) -> ActionResult:
    """「先完成当前刷新再提交下线」重试：中断后贴源表回到完整版本，清单只续写未完成部分。"""
    entry, message = service.retry_offline(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/interrupt", response_model=ActionResult)
def simulate_interrupt(entry_id: int) -> ActionResult:
    """模拟清单写入异常中断（演练用）。"""
    entry, message = service.simulate_interrupt(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/batches")
def get_related_batches(entry_id: int) -> dict[str, Any]:
    """返回与该ODS表关联的入库批次。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"贴源表 {entry_id} 不存在")
    batches = service.get_batches_for_table(entry.get("表名称", ""))
    return {"items": batches}


@router.get("/{entry_id}/data")
def get_table_data(entry_id: int) -> dict[str, Any]:
    """返回该ODS表的模拟数据。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"贴源表 {entry_id} 不存在")
    data = service.get_table_data(entry.get("表名称", ""))
    return {"items": data}
