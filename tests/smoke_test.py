# -*- coding: utf-8 -*-
"""API 全链路冒烟测试。

针对一个正在运行的工作台实例做端到端验证，覆盖「写 → 改 → 记 → 完成 → 清理」
的完整链路，用于确认应用整体健康、且真实数据未被污染。

用法:
    python tests/smoke_test.py [base_url] [--allow-remote]

默认 http://127.0.0.1:5000。

注意：本脚本会**创建并删除**数据，因此默认只允许指向本机地址；
确需指向其它实例时必须显式加 --allow-remote。

与单元/回归测试的分工：
    tests/unit_dates_kind.py   纯函数级，无需服务
    tests/api_reminders.py     接口级，用临时空库
    tests/api_kind_repeat.py   接口级，用临时空库
    tests/vm_regression.js     前端渲染契约，需服务运行
    tests/smoke_test.py        端到端全链路，需服务运行，会写入并清理数据
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:5000"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "0.0.0.0")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

cleanup = []
failures = []


def parse_args(argv):
    base = DEFAULT_BASE
    allow_remote = False
    for a in argv[1:]:
        if a == "--allow-remote":
            allow_remote = True
        else:
            base = a
    return base.rstrip("/"), allow_remote


BASE, ALLOW_REMOTE = parse_args(sys.argv)
if not ALLOW_REMOTE and not any(h in BASE for h in LOCAL_HOSTS):
    print("拒绝执行：%s 不是本机地址。" % BASE)
    print("本脚本会写入并删除数据，确认目标无误后再加 --allow-remote 重跑。")
    sys.exit(2)


def req(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, (resp.read().decode() or "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def jget(s, b):
    try:
        return json.loads(b) if b else None
    except Exception:
        return None


def check(label, cond, detail=""):
    """记录一项断言结果，返回布尔值。"""
    if cond:
        print("  [PASS] %s" % label)
    else:
        print("  [FAIL] %s %s" % (label, detail))
        failures.append(label)
    return bool(cond)


def declared_read_routes():
    """从 workbench.py 解析出全部无路径参数的 GET 路由。

    用途：防止巡检清单与真实路由表漂移 —— 本脚本早期版本曾探一个
    并不存在的 /api/report/activity，而其 FAIL 未被断言、长期静默。
    """
    try:
        src = open(os.path.join(ROOT, "workbench.py"), encoding="utf-8").read()
    except OSError:
        return None
    routes = re.findall(r'@app\.route\("([^"]+)",\s*methods=\["GET"\]\)', src)
    return {r for r in routes if "<" not in r}


def cleanup_all():
    """无论中间是否失败，都清掉本脚本造出的数据。"""
    if not cleanup:
        return
    print("\n=== 清理测试数据 ===")
    for kind, i in cleanup:
        if kind == "task":
            st, _ = req("DELETE", "/api/tasks/%s" % i)
        else:
            st, _ = req("DELETE", "/api/chronicle/%s" % i)
        print("  DELETE %-10s %-6s -> %s" % (kind, i, st))


def main():
    print("目标实例: %s" % BASE)

    print("\n=== 1) 新增任务 ===")
    st, b = req("POST", "/api/tasks", {
        "title": "冒烟测试任务-待删除", "status": "todo", "priority": "high",
        "project": "冒烟测试项目",
        "description": "这是一段用于冒烟测试的描述文本，用于验证编辑与展示链路是否正常。",
        "is_recurring": 0})
    print("  POST /api/tasks ->", st)
    obj = jget(st, b)
    tid = obj.get("id") if isinstance(obj, dict) else None
    print("  new task id =", tid)
    if not check("新增任务返回 200 且带 id", st == 200 and bool(tid), "status=%s" % st):
        return 1
    cleanup.append(("task", tid))

    print("\n=== 2) 编辑任务(PUT) ===")
    st, _ = req("PUT", "/api/tasks/%s" % tid, {
        "title": "冒烟测试任务-已编辑", "status": "in_progress", "priority": "medium",
        "project": "冒烟测试项目", "description": "编辑后的描述", "is_recurring": 0})
    print("  PUT /api/tasks/<id> ->", st)
    check("编辑任务返回 200", st == 200)

    print("\n=== 3) 加进展记录 ===")
    st, _ = req("POST", "/api/tasks/%s/progress" % tid,
                {"type": "里程碑", "content": "完成核心方案评审"})
    print("  POST progress ->", st)
    check("新增进展返回 200", st == 200)

    print("\n=== 4) 标记完成(带完成原因) ===")
    st, _ = req("PUT", "/api/tasks/%s" % tid, {
        "status": "done", "completion_note": "冒烟测试完成原因",
        "completed_date": "2026-09-02"})
    print("  PUT done+note ->", st)
    check("标记完成返回 200", st == 200)

    print("\n=== 5) 记一笔流水账 ===")
    st, b = req("POST", "/api/chronicle",
                {"content": "冒烟测试流水账-待删除", "category": "工作"})
    print("  POST chronicle ->", st)
    obj = jget(st, b)
    cid = obj.get("id") if isinstance(obj, dict) else None
    print("  new chronicle id =", cid)
    if cid:
        cleanup.append(("chronicle", cid))

    print("\n=== 6) 只读接口全量巡检 ===")
    read_only = [
        "/api/tasks", "/api/chronicle", "/api/notes", "/api/project-links",
        "/api/reminders", "/api/daily-ledger", "/api/daily-logs",
        "/api/daily-summary", "/api/today", "/api/export",
        "/api/report/overview", "/api/report/by-project",
        "/api/report/by-status", "/api/report/by-priority",
    ]
    for ep in read_only:
        st, _ = req("GET", ep)
        print("  GET %-32s -> %s" % (ep, st))
        check("只读接口 %s 返回 200" % ep, st == 200, "status=%s" % st)

    st, _ = req("GET", "/api/tasks/%s/progress" % tid)
    print("  GET %-32s -> %s" % ("/api/tasks/<id>/progress", st))
    check("任务子资源 /api/tasks/<id>/progress 返回 200", st == 200,
          "status=%s" % st)

    # 清单漂移检查：巡检列表必须覆盖 workbench.py 里全部只读路由
    declared = declared_read_routes()
    if declared is None:
        print("  [SKIP] 未能读取 workbench.py，跳过清单漂移检查")
    else:
        probed = set(read_only) | {"/api/report/export"}
        missing = sorted(declared - probed)
        extra = sorted(probed - declared)
        print("  workbench.py 只读路由 %d 个 | 本次巡检 %d 个"
              % (len(declared), len(probed)))
        if extra:
            print("  巡检了未声明的路由（疑似幽灵路由）: %s" % extra)
        check("巡检覆盖全部已声明只读路由", not missing, "未覆盖: %s" % missing)
        check("巡检清单无幽灵路由", not extra, "多余: %s" % extra)

    print("\n=== 7) 流水账列表 ===")
    st, b = req("GET", "/api/chronicle")
    n = len(jget(st, b) or []) if st == 200 else -1
    print("  GET /api/chronicle -> %s | 条数=%s" % (st, n))
    check("流水账列表返回 200", st == 200)

    print("\n=== 8) 述职导出(year=2026) ===")
    try:
        with urllib.request.urlopen(BASE + "/api/report/export?year=2026",
                                    timeout=15) as r:
            sz = len(r.read())
        print("  export year=2026 -> %s | %d bytes" % (r.status, sz))
        check("述职导出返回 200 且非空", r.status == 200 and sz > 0)
    except Exception as e:
        print("  export FAIL", e)
        check("述职导出返回 200 且非空", False, str(e))

    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        cleanup_all()

    print("\n=== 9) 复测任务列表恢复原状 ===")
    st, b = req("GET", "/api/tasks")
    tasks = jget(st, b) or []
    leak = [t for t in tasks
            if "冒烟测试" in ((t.get("title") or "") + (t.get("project") or ""))]
    print("  任务数 = %d | 测试残留 = %d" % (len(tasks), len(leak)))
    check("测试数据已清理干净", not leak,
          "残留: %s" % [t.get("title") for t in leak])

    if failures or rc:
        print("\n冒烟测试失败：%d 项断言未通过 -> %s" % (len(failures), failures))
        sys.exit(1)
    print("\n冒烟测试全部通过，应用全链路健康，真实数据无污染。")
    sys.exit(0)
