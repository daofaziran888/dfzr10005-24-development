"""保留例外演练的领域口径：合规依据权重、期限解析、范围匹配与裁决规则。

所有「优先级与期限计算方式」集中在本模块，演练引擎、治理清单与审计依据共用同一份
口径，保证三处结果可追溯且不会各算各的。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# 参考基准日：演练以“今天”作为期限比较的锚点，统一在这里取值，方便测试与追溯。
REFERENCE_DATE = date(2026, 11, 13)

# 合规依据优先级：位序越小优先级越高。命中多条例外时先比合规依据，再比保留期长度。
COMPLIANCE_ORDER: list[str] = [
    "法律法规",
    "监管规定",
    "行业标准",
    "内控制度",
    "业务约定",
    "无",
]
COMPLIANCE_RANK: dict[str, int] = {name: idx for idx, name in enumerate(COMPLIANCE_ORDER)}

# 过期动作归一化口径：保留规则里写法可能不同，统一映射到三类处置意图。
ACTION_ALIAS: dict[str, str] = {
    "删除": "删除",
    "清理": "删除",
    "永久删除": "删除",
    "物理删除": "删除",
    "归档": "归档",
    "转归档": "归档",
    "冷归档": "归档",
    "回收": "回收站",
    "移入回收站": "回收站",
    "进回收站": "回收站",
    "回收站": "回收站",
}

_PERIOD_PATTERN = re.compile(r"(\d+)\s*(年|个月|月|天|日)")
_DATE_PATTERN = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_SPLIT_PATTERN = re.compile(r"[,，;；、\n]+")


def parse_date(value: Any) -> date | None:
    """把 'YYYY-MM-DD' / 'YYYY/MM/DD' 等写法解析成 date，无法识别返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _DATE_PATTERN.search(text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_period_days(value: Any) -> int | None:
    """解析保留期限。

    支持两种口径：
    1. 相对期限：'180天'、'6个月'、'3年'，按 1 个月=30 天、1 年=365 天换算；
    2. 绝对到期日：'2027-06-30'，换算成距参考日的天数。
    无法识别时返回 None，由调用方按缺省口径处理。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _PERIOD_PATTERN.search(text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "年":
            return amount * 365
        if unit in ("个月", "月"):
            return amount * 30
        return amount
    absolute = parse_date(text)
    if absolute is not None:
        return (absolute - REFERENCE_DATE).days
    return None


def period_label(value: Any) -> str:
    """保留期限的展示口径：相对期限直接展示原始写法，绝对日期补注距基准日天数。"""
    days = parse_period_days(value)
    raw = str(value or "").strip() or "未填写"
    if days is None:
        return raw
    # 原始写法本身已经是“N天/月/年”时不再重复注明天数。
    if _PERIOD_PATTERN.fullmatch(raw.replace(" ", "")):
        return raw
    return f"{raw}（{days}天）"


def split_scope(scope: Any) -> list[str]:
    """数据集范围可能是逗号/顿号/换行分隔的多张表，拆成表名清单。"""
    if scope is None:
        return []
    parts = _SPLIT_PATTERN.split(str(scope))
    return [part.strip() for part in parts if part.strip()]


def scope_covers(scope: Any, table_name: str) -> bool:
    """判断规则的数据集范围是否覆盖某张表（按表名精确匹配拆分后的条目）。"""
    targets = split_scope(scope)
    return table_name in targets


def normalize_action(action: Any) -> str:
    """把保留规则的过期动作归一化到 删除 / 归档 / 回收站 三类。"""
    text = str(action or "").strip()
    if not text:
        return "未指定"
    for keyword, canonical in ACTION_ALIAS.items():
        if keyword in text:
            return canonical
    return text


def compliance_rank(basis: Any) -> int:
    """合规依据权重：在清单里的按位序，未登记的口径排到最低但高于空值。"""
    text = str(basis or "").strip()
    if not text:
        return len(COMPLIANCE_ORDER)
    for idx, name in enumerate(COMPLIANCE_ORDER):
        if name in text:
            return idx
    return len(COMPLIANCE_ORDER) - 1


def resolve_conflict(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """同一规则同时命中多条例外时的裁决。

    裁决顺序（两项共同决定，全部可在裁决依据字段复盘）：
    1. 合规依据权重高者优先（法律法规 > 监管规定 > 行业标准 > 内控制度 > 业务约定 > 无）；
    2. 权重相同则保留期较长者优先；
    3. 仍相同则取例外编号更小者，保证结果稳定可复现。
    返回胜出的例外规则字典，并附带并列命中的全部规则编号。
    """
    ordered = sorted(
        candidates,
        key=lambda item: (
            compliance_rank(item.get("合规依据")),
            -int(item.get("_period_days") or 0),
            int(item.get("id", 0)),
        ),
    )
    winner = ordered[0]
    tied = [item["例外编号"] for item in ordered[1:]]
    winner = dict(winner)
    winner["_tied"] = tied
    return winner


def decision_reason(winner: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    """生成人类可读的裁决依据，说明合规权重与保留期是如何共同裁决的。"""
    basis = str(winner.get("合规依据") or "无").strip() or "无"
    days = int(winner.get("_period_days") or 0)
    if len(candidates) <= 1:
        return f"仅命中单条例外，按合规依据「{basis}」（权重{compliance_rank(basis)}）与保留期{days}天直接采纳"
    tied = "、".join(winner.get("_tied", []))
    return (
        f"同时命中{len(candidates)}条例外（{tied} 并列），先按合规依据「{basis}」"
        f"权重{compliance_rank(basis)}裁决，权重相同再取较长保留期{days}天"
    )


def utc_stamp() -> str:
    """演练时间戳：精确到毫秒，配合序号保证同版本演练可区分、可追溯。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def days_between(start: str | None, end: str | None) -> int | None:
    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date is None or end_date is None:
        return None
    return (end_date - start_date).days
