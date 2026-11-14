# -*- coding: utf-8 -*-
"""把「一条命令全流程」的证明写成可重跑的测试。

为什么不是"跑一遍 run_task.py 看输出对不对"：
    那个证明依赖仓当前状态（有没有 T-0007、分支在不在），换个工作区就全错。
    真正要固定下来的是**驱动器的判定逻辑**：给定状态，它下一步该干什么；
    以及它明确拒绝的那些情况（缺 entrypoint、P0、不能推的状态）。

    端到端那一次已经在 evidence/round11_full.txt 里跑过，本测试负责的是
    "下次改动 run_task.py 时，它还能不能算出同样的下一步"。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import run_task as rt  # noqa: E402

FAIL = []
N = [0]


def ck(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))
    else:
        print("  ok   %s" % name)


# ---------------------------------------------------------------- 1 状态 → 下一步
def t_next_map():
    print("[1] 状态 → 下一步 的映射")
    want = {None: "new", "draft": "approve", "pending": "dispatch",
            "rejected": "dispatch", "claimed": "prepare",
            "in_progress": "finish", "submitted": "verify", "done": "merge"}
    for st, step in want.items():
        ck("NEXT[%r]==%r" % (st, step), rt.NEXT.get(st) == step, "got %r" % rt.NEXT.get(st))
    # 终点状态不在表里 —— 驱动器遇到它们必须停，不能猜
    for terminal in ("cancelled", "archived", "blocked", "escalated"):
        ck("NEXT 不含 %r" % terminal, terminal not in rt.NEXT)


# ---------------------------------------------------------------- 2 编号
class FakeStore:
    def __init__(self, rows, tasks=None):
        self._r = rows
        self._t = tasks or {}

    def _rows(self):
        for r in self._r:
            yield r

    def rebuild(self):
        return {"tasks": self._t}


def t_next_id():
    print("[2] next_task_id 取最大号 +1（不是第一个空号）")
    # 目录也要隔离：`next_task_id` 会扫 `.workbench/tasks/` 里的残留号，
    # 而上一轮测试若留下过 T-9999，这里就会算出 T-10000。
    # 测试撞到自己上一轮的副作用，是"测试之间互相污染"的典型形态 ——
    # 断言会神秘地只在第二次跑时失败，而第一次是绿的。
    import tempfile
    tmp = tempfile.mkdtemp(prefix="rt-id-")
    os.makedirs(os.path.join(tmp, "tasks"))
    real_wb = rt.WB
    rt.WB = tmp
    try:
        rows = [{"kind": "draft_created", "payload": {"task": "T-0002"}},
                {"kind": "draft_created", "payload": {"task": "T-0007"}}]
        st = FakeStore(rows, {"T-0007": {"state": "done"}})
        got = rt.next_task_id(st)
        ck("max+1 == T-0008", got == "T-0008", "got %r" % got)
        ck("不会回收 T-0002", got != "T-0002")

        # 空链 + 空目录 → T-0001
        ck("空链空目录 → T-0001", rt.next_task_id(FakeStore([])) == "T-0001",
           "got %r" % rt.next_task_id(FakeStore([])))

        # 空链 + 目录有残留 → 必须看得见目录（链被重置过、目录还在）
        os.makedirs(os.path.join(tmp, "tasks", "T-0042"))
        ck("空链时看得见 tasks/ 目录", rt.next_task_id(FakeStore([])) == "T-0043",
           "got %r" % rt.next_task_id(FakeStore([])))
    finally:
        rt.WB = real_wb
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t_latest():
    print("[3] latest_task_id 取最后一个 draft_created")
    rows = [{"kind": "draft_created", "payload": {"task": "T-0002"}},
            {"kind": "submit", "payload": {"task": "T-0005"}},
            {"kind": "draft_created", "payload": {"task": "T-0007"}}]
    ck("== T-0007", rt.latest_task_id(FakeStore(rows)) == "T-0007")
    ck("空链 == None", rt.latest_task_id(FakeStore([])) is None)


# ---------------------------------------------------------------- 4 拒绝路径
def t_refuse():
    print("[4] 驱动器明确拒绝的情况")
    import argparse
    # 4a 没有 --title 就想建任务
    a = argparse.Namespace(task="T-9999", title=None, scope=["src/"], priority="P2",
                           cmd='python -c "print(1)"', role="coder", objective="",
                           executor="local_process", entrypoint=None, base=None,
                           wait=10, sign_as=None, resume=False, dry=False, job_path=None)
    st = FakeStore([])
    rc = rt.drive(a, st)
    ck("无 --title 建任务 → 非 0", rc != 0, "rc=%r" % rc)

    # 4b local_process 但没有 entrypoint：必须在派发前拒绝，不能"跑了但什么也没跑"
    #
    # 这一条**绝不能**从 None 状态驱动：那会走 do_new，真的往主链写一条
    # draft_created。第一版就是那样写的，于是每跑一次测试就往生产链里多塞
    # 一个 T-NOENTRY（实测留下 seq98-101 四条）。测试可以造临时仓库、可以
    # 打桩；它不该有写主链的权力 —— 那份链是审计对象，不是测试夹具。
    # 所以这里让任务一上来就处于 claimed，驱动器直接走到 prepare 前的那道闸。
    class Claimed(FakeStore):
        def __init__(self, tid, rows=None):
            FakeStore.__init__(self, rows or [])
            self._tid = tid

        def rebuild(self):
            return {"tasks": {self._tid: {"state": "claimed"}}}

    a2 = argparse.Namespace(**dict(a.__dict__))
    a2.title = "x"
    a2.task = "T-FAKE-NOENTRY"
    wt_probe = os.path.join(ROOT, ".workbench", "wt", "T-FAKE-NOENTRY")
    import shutil
    if os.path.isdir(wt_probe):
        shutil.rmtree(wt_probe, ignore_errors=True)
    rc2 = rt.drive(a2, Claimed("T-FAKE-NOENTRY"))
    ck("local_process 缺 entrypoint → 非 0", rc2 != 0, "rc=%r" % rc2)
    # 拒绝必须发生在 prepare 之前：一个被拒的调用不该留下 worktree。
    ck("被拒时不建 worktree", not os.path.isdir(wt_probe), "残留 %s" % wt_probe)

    # 4c 终结态：驱动器必须停，不能自己猜
    for term in ("cancelled", "blocked", "escalated"):
        class T(FakeStore):
            def rebuild(self):
                return {"tasks": {"T-9999": {"state": term}}}
        rc3 = rt.drive(a, T([]))
        ck("%r 停在原地" % term, rc3 != 0, "rc=%r" % rc3)


# ---------------------------------------------------------------- 5 签名契约
def t_sign_split():
    print("[5] 签名是两步：取签名 与 提交签名 不是同一次调用")
    src = open(os.path.join(ROOT, "run_task.py"), encoding="utf-8").read()
    body = src[src.index("def do_sign"):]
    body = body[:body.index("\n# ----") if "\n# ----" in body else len(body)]
    ck("先要签名", '"--sub", "sign"' in body)
    ck("再提交", '"--nonce"' in body and '"--sig"' in body)
    ck("两次调用分开", body.count("sh(") >= 2, "count=%d" % body.count("sh("))


# ---------------------------------------------------------------- 6 D-51 占位符
def t_fmt_filled():
    print("[6] D-51：do_run 打印的 executor=%s 必须真的填了参数")
    src = open(os.path.join(ROOT, "run_task.py"), encoding="utf-8").read()
    body = src[src.index("def do_run"):]
    body = body[:body.index("def do_finish")]
    ck("占位符已用 % a.executor 填充", "% a.executor" in body,
       "漏填时真跑会原样吐出 executor=%s")


# ---------------------------------------------------------------- 7 D-52 resume 短路
def t_resume_sentinel():
    print("[7] D-52：resume 时按 sentinel 判定 prepare/finish，不能死循环")
    import argparse
    import json
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="rt-sentinel-")
    os.makedirs(os.path.join(tmp, "state", "agent_done"))
    sentinel = os.path.join(tmp, "state", "agent_done", "T-SENT.coder.json")
    with open(sentinel, "w", encoding="utf-8") as fh:
        json.dump({"ok": True}, fh)

    real_wb = rt.WB
    real_prepare, real_finish = rt.do_prepare, rt.do_finish
    rt.WB = tmp
    calls = {"prepare": 0, "finish": 0}

    def fake_prepare(a, st):
        calls["prepare"] += 1
        return 0

    def fake_finish(a, st):
        calls["finish"] += 1
        return 99  # 让 drive 立即收敛，避免状态不动导致空转

    rt.do_prepare, rt.do_finish = fake_prepare, fake_finish
    try:
        class Claimed(FakeStore):
            def rebuild(self):
                return {"tasks": {"T-SENT": {"state": "claimed"}}}

        a = argparse.Namespace(task="T-SENT", title=None, scope=None,
                                priority="P2", cmd=None, role="coder", objective="",
                                executor="harness_subagent", entrypoint=None, base=None,
                                wait=10, sign_as=None, resume=True, dry=False,
                                job_path=None)
        # 有签署 → 跳过 prepare，直接 finish
        rt.drive(a, Claimed([]))
        ck("有 sentinel → 走 finish", calls["finish"] == 1,
           "prepare=%d finish=%d" % (calls["prepare"], calls["finish"]))
        ck("有 sentinel → prepare 未被调用", calls["prepare"] == 0,
           "prepare=%d（D-52 死循环的根源）" % calls["prepare"])

        # 反向：无签署 → 正常 prepare，不能把所有 resume 都短路掉
        os.remove(sentinel)
        calls["prepare"] = calls["finish"] = 0
        rt.drive(a, Claimed([]))
        ck("无 sentinel → 正常 prepare", calls["prepare"] == 1,
           "prepare=%d finish=%d" % (calls["prepare"], calls["finish"]))
        ck("无 sentinel → 不调 finish", calls["finish"] == 0,
           "finish=%d" % calls["finish"])
    finally:
        rt.WB = real_wb
        rt.do_prepare, rt.do_finish = real_prepare, real_finish
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in (t_next_map, t_next_id, t_latest, t_refuse, t_sign_split,
               t_fmt_filled, t_resume_sentinel):
        fn()
    print("\n%d/%d passed" % (N[0] - len(FAIL), N[0]))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  " + f)
        sys.exit(1)
    print("run_task: PASS (%d/%d)" % (N[0], N[0]))
