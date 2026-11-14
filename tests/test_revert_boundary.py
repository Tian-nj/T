"""回滚边界测试：`revert_to` 只许动这次合并碰过的路径。

真事（不是假想）：本轮给 `merge_queue.py` 打的补丁，被它自己触发的一次
冒烟失败回滚 `git reset --hard` 抹掉了。日志写着"smoke failed → reverted
to <base> (ok=True)"，一切看起来都对，只有一件事没说：**它顺手删了
别人未提交的工作**。

这不是"回滚写错了"，而是"回滚的边界写错了"。
回滚应该等于这次合并的边界，而不是这棵树里所有未提交的改动。

第三十九轮：删掉 `revert_to` 的路径 2/3（reset --hard / checkout -B）。
第四十轮：补上**可证明**的兜底 —— 从 merge_commit 的第一父 diff 反推
touched。`result.json` 是执行体自述，可能漏写；git 对象里第一父与合并
提交之差才是这次合并实际落地的边界。三种情形必须分别验：

  1. 有显式 touched、没有 merge_commit → 照常按显式边界回滚；
  2. 没有显式 touched、有 merge_commit → 兜底算出边界并成功回滚，
     detail 里写清来自哪个 commit、算了几条路径；
  3. 两个来源都没有 → REVERT_FAILED，HEAD / 别人的活 / a.txt 全都不动。

第 3 条是底线：连 git 对象都问不出边界时停下来，不许再退到整树 reset。
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

R = []


def t(name, got, want):
    ok = got == want
    R.append(ok)
    print("%-56s %s  got=%r want=%r" % (name, "ok" if ok else "FAIL", got, want))


def g(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args),
                          capture_output=True, encoding="utf-8", errors="replace")


def w(p, s):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)


def rd(p):
    return open(p, encoding="utf-8").read()


def build(tmp):
    """建 base + task 分支 + --no-ff 合并，返回 (base, merge_commit)。

    必须 --no-ff：快进合并没有第二父，`<merge>^1` 会指到 task 的父提交，
    diff 出来就不是「主干 → 合并结果」这条边了。
    """
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    g(tmp, "init", "-q", "-b", "master")
    w(os.path.join(tmp, "a.txt"), "A0\n")
    w(os.path.join(tmp, "b.txt"), "B0\n")
    w(os.path.join(tmp, "ctrl.py"), "print('v1')\n")
    g(tmp, "add", "-A")
    g(tmp, *ident + ["commit", "-q", "-m", "base"])
    base = g(tmp, "rev-parse", "HEAD").stdout.strip()

    g(tmp, "checkout", "-q", "-b", "task/T-1")
    w(os.path.join(tmp, "a.txt"), "A1 by task\n")
    g(tmp, "add", "-A")
    g(tmp, *ident + ["commit", "-q", "-m", "task change"])
    g(tmp, "checkout", "-q", "master")
    g(tmp, *ident + ["merge", "--no-ff", "-q", "-m", "merge: T-1", "task/T-1"])
    merge_commit = g(tmp, "rev-parse", "HEAD").stdout.strip()
    return base, merge_commit


def dirty_ctrl(tmp):
    """模拟「人正在改控制面」：未提交的活。"""
    w(os.path.join(tmp, "ctrl.py"), "print('v2 work in progress')\n")
    t("ctrl.py 是脏的（未提交）",
      "ctrl.py" in g(tmp, "status", "--porcelain").stdout, True)


def main():
    import merge_queue as mq

    # ---------------------------------------------------------------- 情形 1
    tmp = tempfile.mkdtemp(prefix="wb_revert_")
    base, merge_commit = build(tmp)
    t("情形1 合并后 HEAD != base", merge_commit != base, True)
    t("情形1 a.txt 已被任务改成 A1",
      rd(os.path.join(tmp, "a.txt")).strip(), "A1 by task")
    dirty_ctrl(tmp)
    mq.ROOT = tmp
    ok, detail = mq.revert_to(None, "T-1", base, {"branch": "master"},
                              touched=["a.txt"])
    t("情形1 revert_to 报告成功", ok, True)
    t("情形1 HEAD 回到 base", g(tmp, "rev-parse", "HEAD").stdout.strip(), base)
    t("情形1 a.txt 回到 A0", rd(os.path.join(tmp, "a.txt")).strip(), "A0")
    t("情形1 ctrl.py 的未提交改动仍在",
      rd(os.path.join(tmp, "ctrl.py")).strip(), "print('v2 work in progress')")
    t("情形1 detail 里没有 LOST", "LOST" in detail, False)
    t("情形1 detail 写明兜底没启用（未传 merge_commit）",
      "no merge_commit supplied" in detail, True)

    # ---------------------------------------------------------------- 情形 2
    tmp2 = tempfile.mkdtemp(prefix="wb_revert2_")
    base2, merge2 = build(tmp2)
    dirty_ctrl(tmp2)
    mq.ROOT = tmp2
    ok2, detail2 = mq.revert_to(None, "T-1", base2, {"branch": "master"},
                                touched=None, merge_commit=merge2)
    t("情形2 兜底让回滚成功", ok2, True)
    t("情形2 HEAD 回到 base", g(tmp2, "rev-parse", "HEAD").stdout.strip(), base2)
    t("情形2 a.txt 回到 A0（边界不是空的）",
      rd(os.path.join(tmp2, "a.txt")).strip(), "A0")
    t("情形2 ctrl.py 的活没被抹掉",
      rd(os.path.join(tmp2, "ctrl.py")).strip(), "print('v2 work in progress')")
    t("情形2 detail 点名 merge_commit 第一父",
      "fallback=merge_commit@%s^1" % merge2[:12] in detail2, True)
    t("情形2 detail 写明 diff 出 1 条路径",
      "diff 1 path(s)" in detail2, True)
    t("情形2 detail 写明两个来源各贡献多少",
      "explicit=0 fallback=1 union=1" in detail2, True)
    t("情形2 detail 里没有 reset --hard / checkout -B",
      ("reset --hard" in detail2 or "checkout -B" in detail2), False)

    # 兜底函数本身也得单独验：问的是 git 对象，不是谁自述的列表
    files, note = mq.touched_from_merge(merge2)
    t("touched_from_merge 算出 a.txt", files, ["a.txt"])
    t("touched_from_merge 的 note 带 commit",
      merge2[:12] in note, True)
    empty, why = mq.touched_from_merge("")
    t("没给 merge_commit 时返回空而非假装没触碰", empty, [])
    t("没给 merge_commit 时写明原因", "no merge_commit" in why, True)

    # ---------------------------------------------------------------- 情形 3
    tmp3 = tempfile.mkdtemp(prefix="wb_revert3_")
    base3, merge3 = build(tmp3)
    dirty_ctrl(tmp3)
    head3_before = merge3
    mq.ROOT = tmp3
    ok3, detail3 = mq.revert_to(None, "T-1", base3, {"branch": "master"},
                                touched=None, merge_commit="")
    t("情形3 无边界时明确返回失败", ok3, False)
    t("情形3 detail 写明 REVERT_FAILED", "REVERT_FAILED" in detail3, True)
    t("情形3 detail 写明边界为空", "boundary is empty" in detail3, True)
    t("情形3 HEAD 没动（停在坏状态等人处理）",
      g(tmp3, "rev-parse", "HEAD").stdout.strip(), head3_before)
    t("情形3 ctrl.py 的活没被抹掉",
      rd(os.path.join(tmp3, "ctrl.py")).strip(), "print('v2 work in progress')")
    t("情形3 a.txt 也没被偷偷回滚",
      rd(os.path.join(tmp3, "a.txt")).strip(), "A1 by task")

    # ------------------------------------------------------------ 源码真值
    src = open(os.path.join(ROOT, ".workbench", "scripts", "merge_queue.py"),
               encoding="utf-8").read()
    t("源码含 def touched_from_merge", "def touched_from_merge(" in src, True)
    t("源码用 git diff --name-only <merge>^1",
      'git("diff", "--name-only", first_parent, merge_commit)' in src, True)
    t("主流程把 merge_commit 传给 revert_to",
      "merge_commit=merge_commit" in src, True)
    t("源码不含 git(\"reset\", \"--hard\"", 'git("reset", "--hard"' not in src, True)
    t("源码不含 git(\"checkout\", \"-B\"", 'git("checkout", "-B"' not in src, True)
    t("源码含 REVERT_FAILED 显式分支", "REVERT_FAILED" in src, True)

    ok = all(R)
    print("\nrevert_boundary: %s (%d/%d)"
          % ("PASS" if ok else "FAIL", sum(R), len(R)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
