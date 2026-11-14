"""外部执行体路径测试：控制面从不 fork 执行体时，这条链还成立吗？

真事：T-0005 由 DSH 子代理执行 —— 它不在本进程树里，控制面没有它的 pid，
也永远不会 `wait()` 到它。这条路径上曾经有四个坑，每一个都让"外部执行体"
变成一句只在文档里成立的话：

  D-40  `prepare` 只写 brief 不写 job → 外部执行体拿不到
        executor / deadline / done_sentinel，只能靠人肉转述；
  D-41  job 与 brief 里的 `write_scope` 是 `<task.scope>` 占位符 →
        执行体读到的是一条模板语法；
  D-42  `announce` 在任务已是 in_progress 时 `tr["seq"]` 必崩 ——
        而那条路径恰恰是唯一会走到它的路径（第 2..n 个执行体接力）；
  D-43  `finish` 从不写 E05 → 状态停在 claimed，
        `task-submit` 报 `illegal transition claimed→submitted`，整条路断掉。

本测试不跑真 git、不依赖真链：它锁的是**这条路径的形状**，不是它的运气。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

R = []


def t(name, got, want):
    ok = got == want
    R.append(ok)
    print("%-58s %s  got=%r want=%r" % (name, "ok" if ok else "FAIL", got, want))


class FakeStore:
    """够用的 Store 影子：只模拟 announce_worker / _record_exit 会碰到的面。"""

    def __init__(self, state="claimed", rows=None):
        self.state = state
        self.rows = list(rows or [])
        self.events = []
        self.transitions = []

    def current_state(self, tid):
        return self.state

    def _rows(self):
        return list(self.rows)

    def append_event(self, kind, payload, actor, task=None, meta=None):
        ev = {"kind": kind, "payload": payload, "actor": actor,
              "task": task, "seq": len(self.events) + 1, "meta": meta or {}}
        self.events.append(ev)
        self.rows.append(ev)
        return ev

    def transition(self, tid, to, actor, reason_event=None, **meta):
        self.transitions.append({"task": tid, "to": to, "actor": actor})
        self.state = to
        return {"seq": 900 + len(self.transitions), "from": "claimed", "to": to}


def main():
    import agent_runner as ar

    # ---------------------------------------------------------------- D-43/D-44
    # 外部执行体交签署时，控制面必须替它写 E05（否则状态停在 claimed），
    # 并且必须标出「本地没有可观测进程」—— pid=0 不是"某个进程"，是"没有"。
    st = FakeStore("claimed")
    sp, tr = ar.announce_worker(st, "T-X", "coder", 0, "", "harness_subagent")
    pl = sp["payload"]
    t("D-43 外部执行体也写了 E05", sp["kind"], "spawn")
    t("D-43 并迁移到 in_progress", tr["to"], "in_progress")
    t("D-44 pid 记为 0", pl["pid"], 0)
    t("D-44 标出 pid_observable=False", pl.get("pid_observable"), False)
    t("D-44 记下 executor", pl.get("executor"), "harness_subagent")

    # 本地子进程有真 pid：pid_observable 必须是 True，且**这个键必须在**。
    # 第三十七轮改判：原断言要求「有真 pid 时不带这个键」，理由是
    # 「H5 会把有真 pid 与没有 pid 当成同一类」。但缺键比 False 更坏 ——
    # 缺键在 JSON 里和「没人写过这件事」长得一样，而 boss_report 的风险判据
    # 只认 `is False`，于是「本机看不到执行体存活」会被静默放过。
    # 现在两个方向都写：pid>0 → True，pid<=0 → False。判据移到值上，
    # 不再用「键存不存在」承载语义。
    st2 = FakeStore("claimed")
    sp2, _ = ar.announce_worker(st2, "T-Y", "coder", 8372, "133000", "local_process")
    t("有真 pid 时 pid_observable=True", sp2["payload"].get("pid_observable"), True)
    t("有真 pid 时确实写了这个键", "pid_observable" in sp2["payload"], True)
    t("有真 pid 时 creation_date 落链", sp2["payload"].get("creation_date"), "133000")

    # ---------------------------------------------------------------- D-42
    # 接力场景：任务已是 in_progress，第二个执行体进来。
    # 此时不该写 E08（单载体：每次状态变化恰好一条 state 事件），
    # 更不能因为 tr is None 就崩。
    st3 = FakeStore("in_progress")
    try:
        sp3, tr3 = ar.announce_worker(st3, "T-Z", "architect", 0, "", "harness_subagent")
        crashed = None
    except Exception as exc:            # noqa: BLE001 —— 崩了就是要断言的事
        crashed = "%s: %s" % (type(exc).__name__, exc)
        sp3, tr3 = None, None
    t("D-42 in_progress 时 announce 不崩", crashed, None)
    t("D-42 仍写 E05", (sp3 or {}).get("kind"), "spawn")
    t("D-42 但返回 tr=None（不写第二条 state）", tr3, None)
    t("D-42 且确实没有迁移", st3.transitions, [])

    # 第三个坑：状态既不是 claimed 也不是 in_progress 时必须明确拒绝，
    # 而不是"顺手写一条 spawn"。
    st4 = FakeStore("done")
    try:
        ar.announce_worker(st4, "T-W", "coder", 1, "", "local_process")
        refused = False
    except SystemExit as exc:
        refused = "only claimed / in_progress" in str(exc) or "in_progress" in str(exc)
    t("done 状态下 announce 被拒", refused, True)

    # ---------------------------------------------------------------- _record_exit
    # 正常完成的 worker 永远没有 kill 记录 —— 它自己退出了，没人杀它。
    # H5 只认 E07，于是任何一次 task-escalate 都会被拦住。
    # 所以正常路径也要写一条 E07，method 说明证据来源。
    st5 = FakeStore("in_progress", rows=[{"kind": "spawn", "task": "T-E",
                                          "payload": {"task": "T-E", "pid": 4242}}])
    ar._record_exit(st5, "T-E", {"mode": "local_process", "rc": 0})
    kills = [e for e in st5.events if e["kind"] == "kill"]
    t("正常完成也写 E07", len(kills), 1)
    t("method=exited（有本地进程）", kills[0]["payload"]["method"], "exited")
    t("killed=False（没人杀它）", kills[0]["payload"]["killed"], False)
    t("rc 落链", kills[0]["payload"].get("rc"), 0)
    t("pid 取自 spawn，不是自己编的", kills[0]["payload"]["pid"], 4242)
    t("meta 标明与 spawn 配对", kills[0]["meta"].get("paired_with"), "spawn")

    # 外部执行体：没有本地进程可等，method=sentinel。
    st6 = FakeStore("in_progress", rows=[{"kind": "spawn", "task": "T-F",
                                          "payload": {"task": "T-F", "pid": 0}}])
    ar._record_exit(st6, "T-F", {"mode": "harness_subagent"})
    k6 = [e for e in st6.events if e["kind"] == "kill"][0]
    t("外部执行体 method=sentinel", k6["payload"]["method"], "sentinel")
    t("外部执行体不带 rc", "rc" in k6["payload"], False)

    # 已有 kill 就不许再补一条：重复的终止记录会让 H5 的读法变成"数事件"。
    st7 = FakeStore("done", rows=[{"kind": "kill", "task": "T-G",
                                   "payload": {"task": "T-G", "pid": 9}}])
    ar._record_exit(st7, "T-G", {"mode": "local_process", "rc": 1})
    t("已有 E07 时不重复写", [e for e in st7.events if e["kind"] == "kill"], [])

    # ---------------------------------------------------------------- D-41 write_job
    # job 是外部执行体的唯一机器可读输入；write_scope 不许留占位符。
    tmp = tempfile.mkdtemp(prefix="wb_job_")
    ar.JOB_DIR = os.path.join(tmp, "jobs")
    ar.DONE_DIR = os.path.join(tmp, "done")
    ar.TASKS_DIR = os.path.join(tmp, "tasks")
    os.makedirs(os.path.join(ar.TASKS_DIR, "T-J"), exist_ok=True)
    with open(os.path.join(ar.TASKS_DIR, "T-J", "acceptance.yaml"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("scope:\n  - src/\n  - tests/\n")

    spec = {"executor": "harness_subagent", "write_scope": ["<task.scope>"],
            "model": {"name": "m", "provider": "p", "reasoning": "medium"}}
    jp = ar.write_job("T-J", "coder", spec, "/tmp/brief.md", "/tmp/wt", "abc123", 1800)
    job = json.load(open(jp, encoding="utf-8"))
    t("D-41 job 里 write_scope 已展开", job["write_scope"], ["src/", "tests/"])
    t("job 里有 executor", job["executor"], "harness_subagent")
    t("job 里有 base", job["base"], "abc123")
    t("job 里有 done_sentinel", job["done_sentinel"].endswith("T-J.coder.json"), True)
    t("job 里有 deadline（= 1800s）", job["deadline_ms"] - job["created_ms"], 1800000)
    t("job 里有 model", (job.get("model") or {}).get("name"), "m")

    # 没有 acceptance 时：不许静默给空列表（"写域为空"与"写域未知"是两件事）。
    spec2 = {"executor": "harness_subagent", "write_scope": ["<task.scope>", "docs/"]}
    jp2 = ar.write_job("T-NOACC", "coder", spec2, "/tmp/b.md", "/tmp/wt", "abc", 60)
    job2 = json.load(open(jp2, encoding="utf-8"))
    t("无可展开 scope 时只留显式项", job2["write_scope"], ["docs/"])

    ok = all(R)
    print("\nexternal_executor: %s (%d/%d)"
          % ("PASS" if ok else "FAIL", sum(R), len(R)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
