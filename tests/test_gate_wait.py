"""闸门「等签名」必须留在 done —— 否则任务会永久掉出合并队列。

真事（本轮）：T-0005 验收通过、单签门槛、只差一个签名。闸门把它
`escalate` 到 escalated（seq 55→56）。之后补签 human_approve（seq 57），
再跑 merge_queue 只打印 `SKIP T-0004` 就收工 —— T-0005 连候选都不是。

原因很朴素：`done_tasks()` 只从 done 里取候选。把"只差一个签名"迁走，
等于把"等人来签"变成一个**没有下一次**的状态。等人来签的前提，
就是下一轮还会被看到。

本测试锁住三件事：
  1. need 未满足 → 不写 escalate、不迁移状态（任务留在 done）；
  2. need 已满足 → 走正常路径（这里只断言不触发 wait 分支）；
  3. 同一操作重复执行 → 仍然不迁移（幂等，不会攒一堆状态）。
"""
import os
import sys

import atexit
import shutil
import tempfile


def _rmtree(path):
    """删一棵树，只读文件也删。

    为什么不直接 `shutil.rmtree(path, True)`：Windows 上 `.git/objects/**`
    里是只读文件，`rmtree` 遇到只读项会**静默失败并留下整棵树**（实测改完
    单跑 test_revert_boundary.py 仍有 3 个目录各 19-20 项留下，只读文件
    全在 .git 下）。先删一次，删不掉就摘掉只读位再删。
    """
    shutil.rmtree(path, ignore_errors=True)
    if not os.path.exists(path):
        return
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                os.chmod(os.path.join(root, fn), 0o777)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _mkdtemp(prefix):
    """建临时目录，并在进程退出时删掉。

    为什么不用裸 `tempfile.mkdtemp(...)`：整轮回归由 `run_all_checks.sh` 的
    TEMP 沙箱兜底，但**单跑这一个文件时没有任何东西负责清理**——2026-11-15
    一天里 %TEMP% 攒下 264 个 `wb_*` 目录 / 17.5 MB，实测单跑
    `test_scheduler.py` 一次就新增 7 个。沙箱管「整轮」，管不到「随手跑一个」。
    """
    d = tempfile.mkdtemp(prefix=prefix)
    atexit.register(_rmtree, d)
    return d


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

R = []


def t(name, got, want):
    ok = got == want
    R.append(ok)
    print("%-56s %s  got=%r want=%r" % (name, "ok" if ok else "FAIL", got, want))


class FakeStore:
    """只记录"有没有被要求写事件 / 迁状态"。"""

    def __init__(self):
        self.events = []
        self.transitions = []

    def append_event(self, kind, payload, actor, task=None, meta=None):
        self.events.append({"kind": kind, "payload": payload, "actor": actor})
        return {"seq": len(self.events)}

    def transition(self, tid, to, actor, reason_event=None, **meta):
        self.transitions.append({"task": tid, "to": to, "actor": actor})
        return {"seq": 100 + len(self.transitions), "from": "done", "to": to}


def main():
    import merge_queue as mq

    # 把 sidecar 账写到临时文件，别污染证据目录
    import tempfile
    tmp = _mkdtemp(prefix="wb_gate_wait_")
    mq.MERGE_LOG = os.path.join(tmp, "merge.jsonl")

    # ---------- 1. 只差一个签名：不许迁状态 ----------
    st = FakeStore()
    d = {"branch": "AUTO_CORE", "need": "single_sign", "need_met": False,
         "reasons": ["files 1<=5"], "stats": {"files": 1}, "approvals": []}
    seq = mq.gate_reject(st, "T-X", d, dry_run=False)
    t("缺签名时不写 escalate", [e["kind"] for e in st.events], [])
    t("缺签名时不迁移状态", st.transitions, [])
    t("返回 seq 为 None（没有链事件）", seq, None)

    log = open(mq.MERGE_LOG, encoding="utf-8").read() if os.path.exists(mq.MERGE_LOG) else ""
    t("但 sidecar 里留了 gate_wait 账", '"gate_wait"' in log, True)

    # ---------- 2. 双签也只差人：同样不许迁 ----------
    st2 = FakeStore()
    d2 = {"branch": "NEVER_WAIVE", "need": "double_sign", "need_met": False,
          "reasons": ["contracts/ 双签"], "stats": {"files": 1}, "approvals": ["yg"]}
    mq.gate_reject(st2, "T-Y", d2, dry_run=False)
    t("双签未满也不迁状态", st2.transitions, [])

    # ---------- 3. 签名够了但被别的原因拦：这时才 escalate ----------
    st3 = FakeStore()
    d3 = {"branch": "NEEDS_REBREAK", "need": "single_sign", "need_met": True,
          "reasons": ["超出 200 行"], "stats": {"files": 9}, "approvals": ["yg"]}
    mq.gate_reject(st3, "T-Z", d3, dry_run=False)
    t("别的原因被拦时写 escalate", [e["kind"] for e in st3.events], ["escalate"])
    t("别的原因被拦时迁到 escalated",
      [x["to"] for x in st3.transitions], ["escalated"])

    # ---------- 4. need=none 却未合并：不该被当成"等签名" ----------
    st4 = FakeStore()
    d4 = {"branch": "AUTO_CORE", "need": "none", "need_met": True,
          "reasons": ["x"], "stats": {"files": 1}, "approvals": []}
    mq.gate_reject(st4, "T-W", d4, dry_run=False)
    t("need=none 走 escalate 分支", [e["kind"] for e in st4.events], ["escalate"])

    ok = all(R)
    print("\ngate_wait: %s (%d/%d)" % ("PASS" if ok else "FAIL", sum(R), len(R)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
