"""保留例外演练应用服务。

承担四件事：
1. 例外规则登记与严格状态机（草稿→生效→到期→废止，单向、不可回退、不可跳级）；
2. 演练执行：同版本幂等（重复演练只保留一份）、同范围并发互斥（后到请求拒绝）；
3. 演练结果写入审计依据，并同步到生命周期治理清单与回收站保护标记；
4. 三处结果用同一演练单号串联，保证可追溯。
"""
from __future__ import annotations

import threading
from typing import Any

from app.store import store

from .engine import run_drill
from .policy import utc_stamp

EXCEPTION_MODULE = "retention_exception"
DRILL_MODULE = "retention_drill"
GOVERNANCE_MODULE = "governance"

REQUIRED_FIELDS = ["例外编号", "数据集范围", "保留期限", "过期动作", "合规依据"]

# 状态机：只允许沿序列前进到相邻状态，回退或跳级一律拒绝。
STATUS_ORDER = ["草稿", "生效", "到期", "废止"]
# 动作名 -> 目标状态；目标必须是当前状态的下一个相邻状态。
ACTION_RULES = {
    "提交生效": "生效",
    "标记到期": "到期",
    "执行废止": "废止",
}

# 数据集范围级别的并发互斥锁：同一范围同时只允许一个演练在跑。
_scope_locks_guard = threading.Lock()
_scope_locks: dict[str, threading.Lock] = {}


def _lock_for(scope_key: str) -> threading.Lock:
    with _scope_locks_guard:
        lock = _scope_locks.get(scope_key)
        if lock is None:
            lock = threading.Lock()
            _scope_locks[scope_key] = lock
        return lock


class RetentionDrillService:
    # ------------------------------------------------------------------ 例外规则
    def list_exceptions(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        version: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(EXCEPTION_MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("例外编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if version:
            rows = [row for row in rows if row.get("版本") == version]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_exception(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(EXCEPTION_MODULE, entry_id)

    def create_exception(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(EXCEPTION_MODULE)
        entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        entry: dict[str, Any] = {"id": entry_id}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["版本"] = str(values.get("版本") or "v1").strip() or "v1"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["创建时间"] = utc_stamp()
        rows.append(entry)
        return entry, []

    def transition(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """状态机入口：校验动作合法性与相邻流转，拒绝任何回退或跳级。"""
        entry = store.find(EXCEPTION_MODULE, entry_id)
        if entry is None:
            return None, f"例外规则 {entry_id} 不存在"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于保留例外规则可执行范围"
        current = str(entry.get("status"))
        if current not in STATUS_ORDER:
            return None, f"当前状态「{current}」不在状态序列中，无法流转"
        current_index = STATUS_ORDER.index(current)
        target = ACTION_RULES[action]
        target_index = STATUS_ORDER.index(target)
        if target_index <= current_index:
            return None, (
                f"状态机只允许前进：当前「{current}」不能回退到「{target}」，"
                "规则不能回退或跳过中间状态"
            )
        if target_index != current_index + 1:
            return None, (
                f"状态机不允许跳级：「{current}」必须先进入「{STATUS_ORDER[current_index + 1]}」"
                f"，不能直接切换到「{target}」"
            )
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["最后流转时间"] = utc_stamp()
        return entry, f"例外规则已{action}：{current} → {target}"

    # ------------------------------------------------------------------ 演练
    def list_drills(
        self,
        *,
        version: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(DRILL_MODULE)
        if version:
            rows = [row for row in rows if row.get("版本") == version]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_drill(self, drill_id: int) -> dict[str, Any] | None:
        return store.find(DRILL_MODULE, drill_id)

    def _next_drill_no(self) -> str:
        rows = store.rows(DRILL_MODULE)
        seq = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        return f"DRL-2026-{seq:04d}"

    def execute_drill(
        self, version: str, operator: str | None = None
    ) -> tuple[dict[str, Any] | None, str, int]:
        """执行演练。

        返回 (演练单, 消息, http状态码)。
        - 同版本重复演练：不重算，直接返回已存在的唯一一份，状态码 200；
        - 同范围并发：后到请求拿不到锁，拒绝并返回 409；
        - 正常演练：落演练单、写审计、同步治理清单，状态码 201。
        """
        version = (version or "").strip()
        if not version:
            return None, "演练必须指定规则版本", 400

        existing = [
            row for row in store.rows(DRILL_MODULE) if row.get("版本") == version
        ]
        if existing:
            return existing[0], f"版本 {version} 已存在演练单，重复演练只保留一份，直接返回既有结果", 200

        active = [
            row for row in store.rows(EXCEPTION_MODULE)
            if row.get("版本") == version and row.get("status") == "生效"
        ]
        if not active:
            return None, f"版本 {version} 下没有生效中的例外规则，无法演练", 400

        scope_key = "|".join(
            sorted(str(row.get("数据集范围") or "") for row in active)
        )
        lock = _lock_for(scope_key)
        if not lock.acquire(blocking=False):
            return None, "同一数据集范围已有演练在执行，后到请求被拒绝（有效版本并发争用）", 409

        try:
            # 拿到锁后再次检查，防止等待期间别的请求已经完成同版本演练。
            existing = [
                row for row in store.rows(DRILL_MODULE) if row.get("版本") == version
            ]
            if existing:
                return existing[0], "版本演练已由并发请求完成，复用唯一有效版本", 200

            drill_no = self._next_drill_no()
            triggered_at = utc_stamp()
            report = run_drill(version, drill_no, triggered_at)
            report["操作人"] = operator or "治理管理员"
            report["status"] = "已完成"
            report["pending"] = False
            report["abnormal"] = report["阻止永久清理数"] > 0
            report["id"] = max(
                (int(row.get("id", 0)) for row in store.rows(DRILL_MODULE)), default=0
            ) + 1
            store.rows(DRILL_MODULE).append(report)

            self._write_audit(report)
            self._sync_governance(report)
            self._mark_recycle_protection(report)
            return report, f"演练 {drill_no} 完成，结果已写入审计依据并同步治理清单", 201
        finally:
            lock.release()

    def _write_audit(self, report: dict[str, Any]) -> None:
        """演练结果写审计：每张受保护表一条审计依据，锚定演练单号与来源记录。"""
        audit_rows = store.rows("audit")
        next_id = max((int(row.get("id", 0)) for row in audit_rows), default=0) + 1
        for item in report["演练明细"]:
            audit_rows.append({
                "id": next_id,
                "status": "已记录",
                "pending": True,
                "abnormal": bool(item.get("禁止永久清理")),
                "记录编号": f"AUDI-DRL-{report['演练单号']}-{item['表名称']}",
                "变更对象": f"保留例外演练/{item['表名称']}",
                "变更类型": f"演练处置-{item['处置结果']}",
                "变更前值": "；".join(
                    str(conflict["冲突说明"]) for conflict in item["冲突来源"]
                ) or "无前置冲突",
                "变更后值": (
                    f"胜出例外 {item['裁决胜出例外']}，保护截止 {item['保护截止日']}；"
                    f"{item['裁决依据']}"
                ),
                "操作人": report["操作人"],
                "操作时间": report["演练时间"],
                "复核状态": "待复核",
                "演练单号": report["演练单号"],
                "版本": report["版本"],
                "审计依据": report["演练单号"],
            })
            next_id += 1

    def _sync_governance(self, report: dict[str, Any]) -> None:
        """同步到生命周期治理清单：一表一行，重复演练同版本覆盖而不是新增。"""
        current = store.rows(GOVERNANCE_MODULE)
        synced_tables = {item["表名称"] for item in report["演练明细"]}
        # 保留与本次演练无关的既有行；同表同版本的旧行被新结果覆盖。
        retained = [
            row for row in current
            if not (
                row.get("版本") == report["版本"]
                and row.get("表名称") in synced_tables
            )
        ]
        all_governance = store.rows(GOVERNANCE_MODULE)
        next_id = max((int(row.get("id", 0)) for row in all_governance), default=0) + 1
        for item in report["演练明细"]:
            retained.append({
                "id": next_id,
                "status": "生效",
                "pending": False,
                "abnormal": bool(item.get("禁止永久清理")),
                "治理编号": f"GOV-{report['演练单号']}-{item['表名称']}",
                "表名称": item["表名称"],
                "版本": report["版本"],
                "处置结果": item["处置结果"],
                "保护来源": item["裁决胜出例外"],
                "合规依据": item["裁决合规依据"],
                "保护截止日": item["保护截止日"],
                "禁止永久清理": "是" if item.get("禁止永久清理") else "否",
                "冲突来源": "；".join(
                    f"{conflict['来源类型']}:{conflict['来源编号']}"
                    for conflict in item["冲突来源"]
                ),
                "演练单号": report["演练单号"],
                "同步时间": report["演练时间"],
            })
            next_id += 1
        store.replace_rows(GOVERNANCE_MODULE, retained)

    def _mark_recycle_protection(self, report: dict[str, Any]) -> None:
        """把保护来源回写到回收站记录，业务页面回收记录可直接展示保护来源。"""
        protected = {
            item["表名称"]: item
            for item in report["演练明细"]
            if item["处置结果"] == "回收站保护"
        }
        for record in store.rows("recycle"):
            item = protected.get(str(record.get("表名称")))
            if item is None:
                continue
            record["保护来源"] = item["裁决胜出例外"]
            record["保护演练单号"] = report["演练单号"]
            record["保护截止日"] = item["保护截止日"]
            record["禁止永久清理"] = "是"
            record["保护合规依据"] = item["裁决合规依据"]

    def governance_list(
        self, *, keyword: str | None = None, page: int = 1, size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(GOVERNANCE_MODULE))
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("表名称", ""))
                or keyword in str(row.get("演练单号", ""))
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total


service = RetentionDrillService()
