"""保留例外演练接口：例外规则维护、状态机流转、演练执行与治理清单查询。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from app.schemas import ActionResult, EntryPayload, PageResult
from app.retention_drill.service import STATUS_ORDER, service

router = APIRouter(prefix="/api/retention-drill", tags=["保留例外演练"])

EXCEPTION_FIELDS = ["例外编号", "数据集范围", "保留期限", "过期动作", "合规依据", "版本"]


@router.get("/exceptions", response_model=PageResult[dict])
def list_exceptions(
    keyword: str | None = Query(default=None, description="按例外编号检索"),
    status: str | None = Query(default=None, description="草稿、生效、到期、废止"),
    version: str | None = Query(default=None, description="按规则版本过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """分页查询例外规则；支持按编号、状态与版本过滤。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_exceptions(
        keyword=keyword, status=status, version=version, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/exceptions/{entry_id}", response_model=dict)
def get_exception(entry_id: int) -> dict:
    entry = service.get_exception(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"例外规则 {entry_id} 不存在")
    return entry


@router.post("/exceptions", response_model=ActionResult)
def create_exception(payload: EntryPayload) -> ActionResult:
    """登记一条保留例外规则，初始状态固定为草稿。"""
    entry, missing = service.create_exception(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="例外规则已登记为草稿", entry=entry)


@router.post("/exceptions/{entry_id}/actions", response_model=ActionResult)
def transition_exception(entry_id: int, payload: EntryPayload) -> ActionResult:
    """状态机流转：只允许 草稿→生效→到期→废止 相邻前进，回退或跳级会被拒绝。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.transition(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/drills", response_model=PageResult[dict])
def list_drills(
    version: str | None = Query(default=None, description="按规则版本过滤演练单"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """查询演练单列表；同版本只会有一份有效演练单。"""
    items, total = service.list_drills(version=version, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/drills/{drill_id}")
def get_drill(drill_id: int) -> dict:
    """读取单张演练单的完整明细（含每张表的冲突来源与裁决依据）。"""
    drill = service.get_drill(drill_id)
    if drill is None:
        raise HTTPException(status_code=404, detail=f"演练单 {drill_id} 不存在")
    return drill


@router.post("/drills", response_model=ActionResult)
def execute_drill(payload: EntryPayload, response: Response) -> ActionResult:
    """执行保留例外演练。

    - 同版本重复演练：幂等返回既有演练单（HTTP 200）；
    - 首次演练成功：返回 HTTP 201，结果写审计依据、同步生命周期治理清单、回写回收站保护来源；
    - 同范围并发争用：后到请求被拒绝（HTTP 409）；
    - 版本无生效例外：返回 HTTP 400。
    """
    version = str(payload.values.get("版本") or "").strip()
    operator = payload.values.get("操作人")
    report, message, status_code = service.execute_drill(
        version, str(operator) if operator else None
    )
    if report is None:
        raise HTTPException(status_code=status_code, detail=message)
    response.status_code = status_code
    return ActionResult(ok=True, message=message, entry=report)


@router.get("/governance", response_model=PageResult[dict])
def governance_list(
    keyword: str | None = Query(default=None, description="按表名或演练单号检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """生命周期治理清单：演练同步结果，每行带演练单号可回溯到演练单与审计依据。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.governance_list(keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/status-meta")
def status_meta() -> dict:
    """对外暴露状态机序列与字段口径，供前端渲染流转按钮与状态说明。"""
    return {
        "status_order": STATUS_ORDER,
        "actions": ["提交生效", "标记到期", "执行废止"],
        "fields": EXCEPTION_FIELDS,
    }
