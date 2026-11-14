"""通过真实 HTTP 接口验证并发演练的 409 拒绝（确定性持锁，不依赖时序）。

运行：.venv/bin/python -I /workspace/tests/verify_http_409.py
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
import urllib.error

sys.path.insert(0, "/workspace/backend")

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402
from app.retention_drill import service as svc  # noqa: E402

config = uvicorn.Config(app, host="127.0.0.1", port=8011, log_level="warning")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    pass


def post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# 准备两个同范围、不同版本的生效例外
scope = "http_concurrency_table"
for idx, version in enumerate(("va", "vb"), start=1):
    created, missing = svc.service.create_exception({
        "例外编号": f"EXC-H{idx}", "数据集范围": scope,
        "保留期限": "1年", "过期动作": "延期保留", "合规依据": "内控制度",
        "版本": version,
    })
    assert created, missing
    svc.service.transition(created["id"], "提交生效")

# 让先到请求在持锁段内停留，保证第二个请求一定撞上锁
original = svc.run_drill


def hold_drill(version, drill_no, triggered_at):
    import time
    time.sleep(1.0)
    return original(version, drill_no, triggered_at)


svc.run_drill = hold_drill
results: list[tuple[int, str]] = []


def worker(version: str) -> None:
    status, body = post(
        "http://127.0.0.1:8011/api/retention-drill/drills",
        {"values": {"版本": version}},
    )
    detail = body.get("detail") or body.get("message", "")
    results.append((status, str(detail)))


t1 = threading.Thread(target=worker, args=("va",))
t2 = threading.Thread(target=worker, args=("vb",))
t1.start()
# 确保第一个请求已进入持锁段
import time
time.sleep(0.3)
t2.start()
t1.join(); t2.join()
svc.run_drill = original

print("并发结果：")
for status, detail in sorted(results):
    print(f"  HTTP {status} — {detail}")
statuses = sorted(code for code, _ in results)
assert statuses == [201, 409], f"期望 [201,409]，实际 {statuses}"
rejected = [d for c, d in results if c == 409][0]
assert "后到请求被拒绝" in rejected, rejected
print("PASS：同范围并发演练，后到有效版本被 HTTP 409 拒绝")
server.should_exit = True
