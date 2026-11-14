"""保留例外演练引擎。

输入一组保留例外规则，对照数据保留策略、表生命周期策略与回收站记录做静态冲突分析，
输出每张受保护表的处置结果（延期保留 / 转为归档 / 回收站保护）、冲突来源与裁决依据。

引擎只做计算不改数据；落审计、同步治理清单由 service 层负责，保证演练可重复、可追溯。
"""
from __future__ import annotations

from typing import Any

from app.store import store

from .policy import (
    REFERENCE_DATE,
    compliance_rank,
    days_between,
    decision_reason,
    normalize_action,
    parse_date,
    parse_period_days,
    period_label,
    resolve_conflict,
    scope_covers,
    split_scope,
)

# 冲突来源类型：每条冲突都带来源模块与来源编号，三处结果凭此互相追溯。
SOURCE_RETENTION = "保留规则清理"
SOURCE_LIFECYCLE = "生命周期清理"
SOURCE_RECYCLE = "回收站恢复窗口"
SOURCE_DOUBLE_HIT = "例外双重命中"


def _active_exceptions(version: str) -> list[dict[str, Any]]:
    """取某版本下生效中的例外规则，并预解析保留期天数。"""
    rows = store.rows("retention_exception")
    result: list[dict[str, Any]] = []
    for row in rows:
        if row.get("版本") != version or row.get("status") != "生效":
            continue
        enriched = dict(row)
        enriched["_period_days"] = parse_period_days(row.get("保留期限"))
        result.append(enriched)
    return result


def _retention_index() -> dict[str, list[dict[str, Any]]]:
    """按表名索引生效中的保留规则（数据集范围可能是多表字符串）。"""
    index: dict[str, list[dict[str, Any]]] = {}
    for row in store.rows("retention"):
        if row.get("status") not in ("生效中", "已到期"):
            continue
        for table in split_scope(row.get("数据集范围")):
            index.setdefault(table, []).append(row)
    return index


def _lifecycle_index() -> dict[str, dict[str, Any]]:
    """按表名索引生命周期策略。"""
    return {
        str(row.get("表名称")): row
        for row in store.rows("lifecycle")
        if row.get("表名称")
    }


def _recycle_index() -> dict[str, list[dict[str, Any]]]:
    """按表名索引仍在回收站（未物理清理）的记录。"""
    index: dict[str, list[dict[str, Any]]] = {}
    for row in store.rows("recycle"):
        if row.get("status") in ("已清理", "已过期"):
            continue
        index.setdefault(str(row.get("表名称")), []).append(row)
    return index


def _classify_outcome(
    exceptions: list[dict[str, Any]],
    retention_hits: list[dict[str, Any]],
    lifecycle_hit: dict[str, Any] | None,
    recycle_hits: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any], int | None]:
    """决定单张表的处置类型与裁决胜出规则。

    处置分类口径（以保留规则的过期动作为主依据，生命周期策略仅作冲突来源与兜底）：
    - 回收站保护：表已在回收站且可恢复期长于规则期限，禁止永久清理；
    - 转为归档：保留规则过期动作为归档（无保留规则时，生命周期清理方式为纯归档兜底）；
    - 延期保留：保留规则过期动作为删除/回收站，命中例外后优先保留并延期。

    第四个返回值是保留规则期限天数（回收站窗口比较的“规则期限”口径），
    没有可识别的保留规则时为 None。
    """
    winner = resolve_conflict(exceptions)
    exception_days = int(winner.get("_period_days") or 0)
    rule_day_values = [
        days for days in (
            parse_period_days(rule.get("保留期限")) for rule in retention_hits
        )
        if days is not None
    ]
    rule_days = max(rule_day_values) if rule_day_values else None
    rule_actions = {normalize_action(rule.get("过期动作")) for rule in retention_hits}

    if recycle_hits:
        return (
            "回收站保护",
            "命中例外清单的数据必须优先保留，可恢复期内禁止永久清理",
            winner,
            rule_days,
        )

    if "归档" in rule_actions:
        return "转为归档", "保留规则过期动作为归档，例外保护期内转为归档而非删除", winner, rule_days
    if rule_actions:
        # 删除 / 回收站 / 其它动作：命中例外一律优先保留并延期。
        return (
            "延期保留",
            f"保留规则过期动作为{'/'.join(sorted(rule_actions))}，按胜出例外延期保留{exception_days}天",
            winner,
            rule_days,
        )

    # 没有保留规则覆盖时，退化为生命周期清理方式判断；“归档后删除”这类最终删除的
    # 口径仍按延期保留处理，只有纯归档才判转为归档。
    cleanup = str(lifecycle_hit.get("清理方式") or "") if lifecycle_hit else ""
    if "归档" in cleanup and "删除" not in cleanup:
        return "转为归档", "无保留规则覆盖，按生命周期纯归档策略转为归档", winner, rule_days
    return (
        "延期保留",
        f"无保留规则覆盖，命中例外后默认延期保留{exception_days}天",
        winner,
        rule_days,
    )


def _build_conflicts(
    table: str,
    exceptions: list[dict[str, Any]],
    retention_hits: list[dict[str, Any]],
    lifecycle_hit: dict[str, Any] | None,
    recycle_hits: list[dict[str, Any]],
    winner: dict[str, Any],
    rule_days: int | None,
) -> list[dict[str, Any]]:
    """汇总一张表的全部冲突来源，每条都写明来源模块、编号与冲突事实。"""
    conflicts: list[dict[str, Any]] = []

    for rule in retention_hits:
        rule_days = parse_period_days(rule.get("保留期限"))
        exception_days = int(winner.get("_period_days") or 0)
        gap = (
            exception_days - rule_days
            if rule_days is not None
            else None
        )
        conflicts.append({
            "来源类型": SOURCE_RETENTION,
            "来源模块": "retention",
            "来源编号": rule.get("规则编号"),
            "来源记录ID": rule.get("id"),
            "冲突说明": (
                f"保留规则 {rule.get('规则编号')} 计划{normalize_action(rule.get('过期动作'))}"
                f"（期限{period_label(rule.get('保留期限'))}），早于例外保护期"
                + (f"，差距{gap}天" if gap is not None else "")
            ),
        })

    if lifecycle_hit is not None:
        conflicts.append({
            "来源类型": SOURCE_LIFECYCLE,
            "来源模块": "lifecycle",
            "来源编号": lifecycle_hit.get("策略编号"),
            "来源记录ID": lifecycle_hit.get("id"),
            "冲突说明": (
                f"生命周期策略 {lifecycle_hit.get('策略编号')} 的清理方式为"
                f"「{lifecycle_hit.get('清理方式')}」，可能在例外保护期内提前处理表 {table}"
            ),
        })

    for record in recycle_hits:
        window_days = days_between(record.get("删除时间"), record.get("可恢复期"))
        # 硬约束口径：回收站可恢复期长于“保留规则期限”时，不允许直接永久清理。
        # 规则期限缺失时退化为“窗口仍未关闭即保护”，并在说明里标注口径退化。
        if rule_days is None:
            blocked = window_days is not None and window_days > 0
            compare_text = "保留规则期限无法解析，按可恢复窗口未关闭处理"
        else:
            blocked = window_days is not None and window_days > rule_days
            compare_text = f"规则期限{rule_days}天"
        conflicts.append({
            "来源类型": SOURCE_RECYCLE,
            "来源模块": "recycle",
            "来源编号": record.get("记录编号"),
            "来源记录ID": record.get("id"),
            "冲突说明": (
                f"回收记录 {record.get('记录编号')} 可恢复期为{window_days}天，{compare_text}"
                + (
                    "，可恢复期长于规则期限，禁止直接永久清理"
                    if blocked
                    else "，仍处于可恢复窗口，命中例外须优先保留"
                )
            ),
            "禁止永久清理": blocked,
        })

    if len(exceptions) > 1:
        conflicts.append({
            "来源类型": SOURCE_DOUBLE_HIT,
            "来源模块": "retention_exception",
            "来源编号": "、".join(str(item.get("例外编号")) for item in exceptions),
            "来源记录ID": None,
            "冲突说明": decision_reason(winner, exceptions),
        })

    return conflicts


def _protected_until(winner: dict[str, Any], recycle_hits: list[dict[str, Any]]) -> str:
    """计算保护截止日：例外保留期锚定参考日；回收站场景同时不早于可恢复期末日。"""
    exception_days = int(winner.get("_period_days") or 0)
    exception_end = REFERENCE_DATE.fromordinal(
        REFERENCE_DATE.toordinal() + max(exception_days, 0)
    )
    for record in recycle_hits:
        recover_end = parse_date(record.get("可恢复期"))
        if recover_end is not None and recover_end > exception_end:
            exception_end = recover_end
    return exception_end.isoformat()


def run_drill(version: str, drill_no: str, triggered_at: str) -> dict[str, Any]:
    """执行一次演练并返回完整演练单（不落库，由 service 层持久化）。"""
    exceptions = _active_exceptions(version)
    retention_index = _retention_index()
    lifecycle_index = _lifecycle_index()
    recycle_index = _recycle_index()

    # 以全部生效例外规则覆盖的表为演练范围。
    targets: list[str] = []
    seen: set[str] = set()
    for rule in exceptions:
        for table in split_scope(rule.get("数据集范围")):
            if table not in seen:
                seen.add(table)
                targets.append(table)

    results: list[dict[str, Any]] = []
    counters = {"延期保留": 0, "转为归档": 0, "回收站保护": 0}
    blocked_cleanup = 0
    double_hits = 0

    for table in targets:
        table_exceptions = [
            rule for rule in exceptions if scope_covers(rule.get("数据集范围"), table)
        ]
        retention_hits = retention_index.get(table, [])
        lifecycle_hit = lifecycle_index.get(table)
        recycle_hits = recycle_index.get(table, [])

        outcome, outcome_reason, winner, rule_days = _classify_outcome(
            table_exceptions, retention_hits, lifecycle_hit, recycle_hits
        )
        conflicts = _build_conflicts(
            table, table_exceptions, retention_hits, lifecycle_hit,
            recycle_hits, winner, rule_days,
        )
        protected_until = _protected_until(winner, recycle_hits)
        is_blocked = any(item.get("禁止永久清理") for item in conflicts)
        if is_blocked:
            blocked_cleanup += 1
        if len(table_exceptions) > 1:
            double_hits += 1
        counters[outcome] += 1

        results.append({
            "表名称": table,
            "处置结果": outcome,
            "处置原因": outcome_reason,
            "命中例外": [str(item.get("例外编号")) for item in table_exceptions],
            "裁决胜出例外": winner.get("例外编号"),
            "裁决合规依据": winner.get("合规依据"),
            "合规权重": compliance_rank(winner.get("合规依据")),
            "裁决保留天数": int(winner.get("_period_days") or 0),
            "保护截止日": protected_until,
            "禁止永久清理": is_blocked,
            "冲突来源": conflicts,
            "裁决依据": decision_reason(winner, table_exceptions),
            "可追溯锚点": {
                "演练单号": drill_no,
                "版本": version,
                "例外记录ID": winner.get("id"),
                "来源记录ID": [
                    item.get("来源记录ID")
                    for item in conflicts
                    if item.get("来源记录ID") is not None
                ],
            },
        })

    return {
        "演练单号": drill_no,
        "版本": version,
        "演练时间": triggered_at,
        "基准日期": REFERENCE_DATE.isoformat(),
        "生效例外数": len(exceptions),
        "覆盖表数": len(targets),
        "延期保留数": counters["延期保留"],
        "转为归档数": counters["转为归档"],
        "回收站保护数": counters["回收站保护"],
        "双重命中数": double_hits,
        "阻止永久清理数": blocked_cleanup,
        "演练明细": results,
    }
