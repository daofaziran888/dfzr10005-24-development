"""保留例外演练域的端到端规则验证（直连服务层，不依赖 httpx/TestClient）。

运行方式（在 backend 目录下）：
    .venv/bin/python -I /workspace/tests/verify_retention_drill.py

覆盖需求点：
1. 三类处置结果（延期/归档/回收站）与冲突来源；
2. 命中例外优先保留 + 回收站窗口长于规则期限时禁止永久清理；
3. 双重命中按合规依据 + 较长保留期共同裁决；
4. 演练写审计、同步治理清单、回写回收站保护来源，三处同一演练单号可追溯；
5. 同版本重复演练幂等（只保留一份）；
6. 同范围并发演练后到请求被拒绝（409）；
7. 例外规则状态机单向相邻流转，拒绝回退与跳级。
"""
from __future__ import annotations

import sys
import threading

sys.path.insert(0, "/workspace/backend")

from app.store import store  # noqa: E402
from app.retention_drill import service as svc  # noqa: E402
from app.retention_drill.policy import (  # noqa: E402
    compliance_rank,
    parse_period_days,
)

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- 1. 期限与权重口径
check("相对期限解析 3年=1095天", parse_period_days("3年") == 1095)
check("相对期限解析 6个月=180天", parse_period_days("6个月") == 180)
check("绝对日期按基准日换算", parse_period_days("2027-02-10") == 89)
check("合规权重 法律法规<监管规定<行业标准",
      compliance_rank("法律法规") < compliance_rank("监管规定") < compliance_rank("行业标准"))

# ---------------------------------------------------------------- 2. 执行 v1 演练
report, message, code = svc.service.execute_drill("v1")
check("v1 演练创建返回 201", code == 201, str(code))
check("演练单号生成", report["演练单号"].startswith("DRL-2026-"), report["演练单号"])
check("覆盖 3 张表", report["覆盖表数"] == 3, str(report["覆盖表数"]))
check("双重命中数为 1", report["双重命中数"] == 1, str(report["双重命中数"]))

detail = {item["表名称"]: item for item in report["演练明细"]}

# 2a. 延期保留 + 双重命中裁决
ods = detail["ods_order_detail"]
check("ods 处置为延期保留", ods["处置结果"] == "延期保留", ods["处置结果"])
check("ods 双重命中两条例外", set(ods["命中例外"]) == {"EXC-0001", "EXC-0002"})
# 同合规依据（法律法规），保留期 5 年 > 3 年 → EXC-0002 胜出
check("双重命中由较长保留期裁决 EXC-0002 胜出",
      ods["裁决胜出例外"] == "EXC-0002", ods["裁决胜出例外"])
check("裁决保留天数 1825", ods["裁决保留天数"] == 1825, str(ods["裁决保留天数"]))
check("裁决依据同时说明合规权重与保留期",
      "合规依据" in ods["裁决依据"] and "保留期" in ods["裁决依据"], ods["裁决依据"])
source_types = {c["来源类型"] for c in ods["冲突来源"]}
check("ods 冲突含保留规则+生命周期+双重命中",
      {"保留规则清理", "生命周期清理", "例外双重命中"} <= source_types, str(source_types))
check("ods 保护截止日=基准日+1825天", ods["保护截止日"] == "2031-11-12", ods["保护截止日"])

# 2b. 转为归档
ads = detail["ads_user_profile"]
check("ads 处置为转为归档", ads["处置结果"] == "转为归档", ads["处置结果"])
check("ads 胜出例外 EXC-0003", ads["裁决胜出例外"] == "EXC-0003")

# 2c. dwd 延期（删除类动作）
dwd = detail["dwd_payment_flow"]
check("dwd 处置为延期保留", dwd["处置结果"] == "延期保留", dwd["处置结果"])

# ---------------------------------------------------------------- 3. 同版本幂等
report2, _, code2 = svc.service.execute_drill("v1")
check("重复演练返回 200", code2 == 200, str(code2))
check("重复演练返回同一演练单号", report2["演练单号"] == report["演练单号"])
drill_rows, drill_total = svc.service.list_drills(version="v1")
check("同版本演练单只有一份", drill_total == 1, str(drill_total))

# ---------------------------------------------------------------- 4. 审计依据可追溯
drill_audits = [
    row for row in store.rows("audit") if row.get("演练单号") == report["演练单号"]
]
check("审计写入 3 条（每表一条）", len(drill_audits) == 3, str(len(drill_audits)))
check("审计记录带演练单号锚点",
      all(row.get("审计依据") == report["演练单号"] for row in drill_audits))
ods_audit = next(row for row in drill_audits if "ods_order_detail" in row["变更对象"])
check("审计变更后值含胜出例外与截止日",
      "EXC-0002" in ods_audit["变更后值"] and "2031-11-12" in ods_audit["变更后值"])

# ---------------------------------------------------------------- 5. 治理清单同步
_, gov_total = svc.service.governance_list(page=1, size=200)
v1_gov = [row for row in store.rows("governance") if row["版本"] == "v1"]
check("治理清单同步 3 行", len(v1_gov) == 3, str(len(v1_gov)))
check("治理行带演练单号可回溯",
      all(row["演练单号"] == report["演练单号"] for row in v1_gov))
gov_ods = next(row for row in v1_gov if row["表名称"] == "ods_order_detail")
check("治理行保护来源为胜出例外", gov_ods["保护来源"] == "EXC-0002")
check("治理行冲突来源含三类标识", "保留规则清理" in gov_ods["冲突来源"])

# 再次演练同版本不会新增治理行（幂等覆盖）
svc.service.execute_drill("v1")
v1_gov_after = [row for row in store.rows("governance") if row["版本"] == "v1"]
check("重复演练后治理行不膨胀", len(v1_gov_after) == 3)

# ---------------------------------------------------------------- 6. 回收站保护 + 禁止永久清理
# 先把 v2 的草稿例外推进到生效（同时验证状态机相邻流转）
exc4_rows, _ = svc.service.list_exceptions(version="v2")
exc4 = exc4_rows[0]
check("EXC-0004 初始为草稿", exc4["status"] == "草稿")
entry, msg = svc.service.transition(exc4["id"], "提交生效")
check("草稿→生效成功", entry is not None and entry["status"] == "生效", msg)

report_v2, _, code_v2 = svc.service.execute_drill("v2")
check("v2 演练成功", code_v2 in (200, 201), str(code_v2))
dim = report_v2["演练明细"][0]
check("dim 处置为回收站保护", dim["处置结果"] == "回收站保护", dim["处置结果"])
# 可恢复期 2026-10-04→2026-12-13 = 70 天 > 保留规则 60 天
check("回收站窗口长于规则期限→禁止永久清理",
      dim["禁止永久清理"] is True, str(dim["冲突来源"]))
recycle_conflict = next(c for c in dim["冲突来源"] if c["来源类型"] == "回收站恢复窗口")
check("冲突说明写明 70 天与规则期限 60 天",
      "70天" in recycle_conflict["冲突说明"] and "60天" in recycle_conflict["冲突说明"],
      recycle_conflict["冲突说明"])

# 回收站记录回写保护来源，业务页面可直接展示
dim_row = next(row for row in store.rows("recycle") if row["表名称"] == "dim_region")
check("回收记录回写保护来源", dim_row.get("保护来源") == "EXC-0004", str(dim_row.get("保护来源")))
check("回收记录标记禁止永久清理", dim_row.get("禁止永久清理") == "是")
check("回收记录带演练单号", dim_row.get("保护演练单号") == report_v2["演练单号"])

# ---------------------------------------------------------------- 7. 并发争用：后到请求 409
v3_scope = "concurrent_table"
for idx, version in enumerate(("v3a", "v3b"), start=1):
    created, missing = svc.service.create_exception({
        "例外编号": f"EXC-C{idx}", "数据集范围": v3_scope,
        "保留期限": "1年", "过期动作": "延期保留", "合规依据": "内控制度",
        "版本": version,
    })
    assert created is not None, missing
    svc.service.transition(created["id"], "提交生效")

outcomes: list[str] = []
entered = threading.Event()
original_run = svc.run_drill


def slow_drill(version: str, drill_no: str, triggered_at: str):  # type: ignore[no-untyped-def]
    # 能进到这里说明已持有范围锁；持锁睡眠，让另一线程在锁外被 409 拒绝。
    entered.set()
    import time
    time.sleep(0.5)
    return original_run(version, drill_no, triggered_at)


svc.run_drill = slow_drill  # type: ignore[assignment]


def worker(version: str) -> None:
    _, _, status = svc.service.execute_drill(version)
    outcomes.append(f"{version}:{status}")


t1 = threading.Thread(target=worker, args=("v3a",))
t2 = threading.Thread(target=worker, args=("v3b",))
t1.start()
# 等先到线程真正进入持锁段，再启动后到线程，保证争用确定发生。
entered.wait(timeout=5)
t2.start()
t1.join(); t2.join()
svc.run_drill = original_run  # type: ignore[assignment]
codes = sorted(outcomes)
check("并发演练一个 201 一个 409",
      any(c.endswith(":201") for c in codes) and any(c.endswith(":409") for c in codes),
      str(codes))
check("后到请求被拒绝的是有效版本", len([c for c in codes if c.endswith(":409")]) == 1)

# 被拒绝的版本不落演练单
versions_with_drill = {row["版本"] for row in store.rows("retention_drill")}
check("被拒绝版本未产生演练单",
      versions_with_drill in ({"v1", "v2"}, {"v1", "v2", "v3a"}, {"v1", "v2", "v3b"}),
      str(versions_with_drill))
check("并发两版本中只有一个版本生成演练单",
      len(versions_with_drill & {"v3a", "v3b"}) == 1, str(versions_with_drill))

# ---------------------------------------------------------------- 8. 状态机：回退/跳级全部拒绝
exc1 = next(
    row for row in store.rows("retention_exception") if row["例外编号"] == "EXC-0001"
)
# 生效状态尝试“提交生效”（目标是更早的状态）→ 拒绝回退
entry, msg = svc.service.transition(exc1["id"], "提交生效")
check("生效态拒绝回退", entry is None and "回退" in msg, msg)
# 生效态直接“执行废止”（跳过到期）→ 拒绝跳级
entry, msg = svc.service.transition(exc1["id"], "执行废止")
check("生效态拒绝跳级到废止", entry is None and "跳级" in msg, msg)
# 合法相邻流转：生效→到期→废止
entry, _ = svc.service.transition(exc1["id"], "标记到期")
check("生效→到期成功", entry is not None and entry["status"] == "到期")
entry, _ = svc.service.transition(exc1["id"], "执行废止")
check("到期→废止成功", entry is not None and entry["status"] == "废止")
# 终态再流转被拒绝
entry, msg = svc.service.transition(exc1["id"], "标记到期")
check("废止终态拒绝任何流转", entry is None and "回退" in msg, msg)

# 未知动作拒绝
entry, msg = svc.service.transition(exc1["id"], "删除规则")
check("未知动作被拒绝", entry is None and "不属于" in msg, msg)

# 不存在的记录
entry, msg = svc.service.transition(99999, "提交生效")
check("不存在记录给出可读错误", entry is None and "不存在" in msg, msg)

# ---------------------------------------------------------------- 9. 异常入参
report, msg, code = svc.service.execute_drill("")
check("空版本演练被 400 拒绝", report is None and code == 400, msg)
report, msg, code = svc.service.execute_drill("v999")
check("无生效例外版本演练被 400 拒绝", report is None and code == 400, msg)
created, missing = svc.service.create_exception({"例外编号": "EXC-X"})
check("缺必填字段被拦截", created is None and set(missing) >= {"数据集范围", "保留期限"}, str(missing))

# ---------------------------------------------------------------- 汇总
failed = [name for name, ok, _ in results if not ok]
print("\n========== 汇总 ==========")
print(f"用例 {len(results)} 条，通过 {len(results) - len(failed)} 条，失败 {len(failed)} 条")
if failed:
    print("失败项：")
    for name in failed:
        print(" -", name)
    raise SystemExit(1)
print("全部通过")
