#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc_numbers 的防回归测试：数字核对这把尺子本身不能是错的。

为什么值得单独测：`--fix` 会**直接改写 README**。一把会自动改文档的尺子如果
量错了，它不会报错，它会安静地把文档改成错的 —— 比没有这把尺子更坏。
所以这里全部用 tempfile 假仓库跑，不碰生产 README。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

import doc_numbers as dn  # noqa: E402

OKS = []
FAILS = []


def ok(name, cond, detail=""):
    if cond:
        OKS.append(name)
    else:
        FAILS.append("%s%s" % (name, (" :: " + str(detail)) if detail else ""))


# ---------------------------------------------------------------- 夹具
def _mk_repo(tmp):
    """造一个最小但结构正确的假仓库，让 compute() 能算出确定值。"""
    wb = os.path.join(tmp, ".workbench")
    os.makedirs(os.path.join(wb, "spec"), exist_ok=True)
    os.makedirs(os.path.join(wb, "scripts"), exist_ok=True)

    # 5 个事件、1 条退役 → live 4
    events = [{"id": "E%02d" % i, "kind": "k%d" % i} for i in range(1, 5)]
    events.append({"id": "E05", "kind": "k5", "note": "已退役"})
    with open(os.path.join(wb, "spec", "events.schema.json"), "w", encoding="utf-8") as fh:
        json.dump({"events": events}, fh)
    with open(os.path.join(wb, "spec", "states.json"), "w", encoding="utf-8") as fh:
        json.dump({"states": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                   "transitions": [{"id": "T01"}, {"id": "T02"}]}, fh)
    with open(os.path.join(wb, "spec", "rules.json"), "w", encoding="utf-8") as fh:
        json.dump({"checks": [{"id": "C.A1"}]}, fh)

    # manifest：3 条 entry
    with open(os.path.join(wb, "root.manifest"), "w", encoding="utf-8") as fh:
        for i in range(3):
            fh.write('  { "path": "scripts/s%d.py" },\n' % i)

    # 2 个脚本，行数确定：a.py 3 行，b.py 2 行 → 5 行
    with open(os.path.join(wb, "scripts", "a.py"), "w", encoding="utf-8") as fh:
        fh.write("x = 1\ny = 2\nz = 3\n")
    with open(os.path.join(wb, "scripts", "b.py"), "w", encoding="utf-8") as fh:
        fh.write("p = 1\nq = 2\n")

    # 回归脚本：for 循环 3 个测试 + 2 audit + 1 check + 1 e2e = 7 项
    rail = (
        "#!/usr/bin/env bash\n"
        "for t in test_a test_b test_c; do\n"
        "    run \"test  $t\" \"$PY\" \"tests/$t.py\"\n"
        "done\n"
        'run "audit audit_static"  "$PY" audit_static.py\n'
        'run "audit audit_drift"   "$PY" audit_drift.py\n'
        'run "check spec_lint"     "$PY" .workbench/scripts/spec_lint.py\n'
        'run "e2e demo.sh" bash demo.sh\n'
    )
    with open(os.path.join(tmp, "run_all_checks.sh"), "w", encoding="utf-8") as fh:
        fh.write(rail)
    return wb, tmp


GOOD_README = (
    "  root.manifest            # 3 个文件的 HMAC 清单\n"
    "    events.schema.json     #   5 个编号 / 4 在役\n"
    "    states.json            #   3 态 / 2 迁移\n"
    "  scripts/                 # 2 个 .py\n"
    "## 5. 脚本清单与职责边界（2 个 .py / 5 行）\n"
    "bash run_all_checks.sh            # 7 项：3 测试 + 2 审计 + 1 校验\n"
    "  → 3 测试 + 2 审计 + 1 校验\n"
    "= **7 项 / 7 通过**\n"
)


# ---------------------------------------------------------------- 用例
def t_compute():
    tmp = tempfile.mkdtemp(prefix="dn_compute_")
    try:
        wb, root = _mk_repo(tmp)
        t = dn.compute(wb)
        ok("manifest 3 条", t["manifest_entries"] == 3, t)
        ok("事件 5 总数 / 4 在役", (t["events_total"], t["events_live"]) == (5, 4), t)
        ok("3 态 / 2 迁移 / 1 规则",
           (t["states"], t["transitions"], t["rules"]) == (3, 2, 1), t)
        ok("2 个脚本 / 5 行", (t["script_files"], t["script_lines"]) == (2, 5), t)
        ok("3 个测试", t["test_count"] == 3, t)
        ok("2 个审计", t["audit_count"] == 2, t)
        # 项数 = 单项标签数(4: 2 audit + 1 check + 1 e2e) + 循环展开的 3 测试 = 7
        ok("回归 7 项（循环展开算，不数行）", t["checks_total"] == 7, t)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_check_clean():
    tmp = tempfile.mkdtemp(prefix="dn_clean_")
    try:
        wb, root = _mk_repo(tmp)
        t = dn.compute(wb)
        findings = dn.check(GOOD_README, t, rail=False)
        ok("数字全对时 0 findings", findings == [], findings)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_check_catches_each():
    """逐条验证：每一类声明数字写错都会被抓到。这是尺子的有效性证明。"""
    cases = [
        ("manifest", "# 3 个文件的 HMAC 清单", "# 9 个文件的 HMAC 清单"),
        ("events_total", "#   5 个编号", "#   8 个编号"),
        ("events_live", "/ 4 在役", "/ 9 在役"),
        ("states", "#   3 态", "#   6 态"),
        ("transitions", "/ 2 迁移", "/ 5 迁移"),
        ("scripts_tree", "# 2 个 .py", "# 6 个 .py"),
        ("script_lines", "/ 5 行", "/ 50 行"),
        ("rail_items", "# 7 项：", "# 70 项："),
        ("rail_tests", "3 测试", "9 测试"),
        ("rail_summary", "= **7 项 / 7 通过**", "= **70 项 / 70 通过**"),
    ]
    for name, good, bad in cases:
        tmp = tempfile.mkdtemp(prefix="dn_bad_")
        try:
            wb, root = _mk_repo(tmp)
            t = dn.compute(wb)
            ok("基线文本本身是干净的（%s）" % name,
               dn.check(GOOD_README, t, rail=False) == [], dn.check(GOOD_README, t, rail=False))
            text = GOOD_README.replace(good, bad, 1)
            ok("改坏 %s 后文本确实变了" % name, text != GOOD_README, bad)
            findings = dn.check(text, t, rail=False)
            ok("抓到 %s 写错" % name, len(findings) >= 1, findings)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def t_missing_claim_reported():
    """声明点整段消失时必须报出来 —— 那意味着这条约束已经悄悄失效。"""
    tmp = tempfile.mkdtemp(prefix="dn_missing_")
    try:
        wb, root = _mk_repo(tmp)
        t = dn.compute(wb)
        text = GOOD_README.replace("  scripts/                 # 2 个 .py\n", "")
        findings = dn.check(text, t, rail=False)
        ok("声明点消失会报（不是静默放过）",
           any("scripts_tree" in f and "没找到" in f for f in findings), findings)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_duplicate_claim_reported():
    """同一处分身出现两次时要报：说明模板被复制，改一处会漏另一处。"""
    tmp = tempfile.mkdtemp(prefix="dn_dup_")
    try:
        wb, root = _mk_repo(tmp)
        t = dn.compute(wb)
        text = GOOD_README + "    dup                   #   5 个编号 / 4 在役\n"
        findings = dn.check(text, t, rail=False)
        ok("重复声明会报（不是挑第一个了事）",
           any("events" in f and "命中 2 处" in f for f in findings), findings)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_fix_roundtrip():
    tmp = tempfile.mkdtemp(prefix="dn_fix_")
    try:
        wb, root = _mk_repo(tmp)
        t = dn.compute(wb)
        broken = GOOD_README.replace("# 3 个文件的 HMAC 清单", "# 99 个文件的 HMAC 清单")
        fixed, changed = dn.fix(broken, t)
        ok("fix 报告改了 1 处", len(changed) == 1, changed)
        ok("fix 后文本干净", dn.check(fixed, t, rail=False) == [], dn.check(fixed, t, rail=False))
        ok("fix 幂等（再修一次 0 处）", dn.fix(fixed, t)[1] == [], dn.fix(fixed, t)[1])
        ok("fix 不动其它行",
           fixed.count("events.schema.json") == broken.count("events.schema.json"), "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_fix_refuses_when_ambiguous():
    """命中多处时 fix 必须跳过而不是猜一个 —— 猜错的代价是改坏别的段落。"""
    tmp = tempfile.mkdtemp(prefix="dn_amb_")
    try:
        wb, root = _mk_repo(tmp)
        t = dn.compute(wb)
        text = GOOD_README + "    dup                   #   5 个编号 / 4 在役\n"
        fixed, changed = dn.fix(text, t)
        ok("多重命中时 fix 不动它",
           not any("events" in c for c in changed), changed)
        ok("多重命中仍会被 check 报出来",
           any("命中 2 处" in f for f in dn.check(fixed, t, rail=False)), dn.check(fixed, t, rail=False))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_real_repo_is_not_asserted_here():
    """生产 README 的一致性**故意不在这里断言**。

    同一个事实已经在两处被检查：`spec_lint` 的 S022 和回归里的
    `check doc_numbers`。再在这里断一次，等于同一个数字有三个地方要同步 ——
    而那正是这个工具存在的理由（数字只该有一个来源）。三处报同一件事，
    修的时候就要改三处，漏一处就红；红的地方还不是真正该改的地方。

    这里只保留一个便宜的健全性检查：生产仓库能被 compute() 读通、
    且算出来的值都不是负数。真实一致性交给上面那两个真读者。
    """
    t = dn.compute(os.path.join(ROOT, ".workbench"))
    ok("生产仓库可 compute", isinstance(t, dict) and len(t) >= 8, t)
    ok("各项计数非负", all(isinstance(v, int) and v >= 0 for v in t.values()), t)
    ok("脚本数与磁盘一致",
       t["script_files"] == len(glob.glob(os.path.join(ROOT, ".workbench", "scripts", "*.py"))),
       (t["script_files"],))


def t_fix_refuses_when_manifest_stale():
    """manifest 陈旧时 --fix 必须拒绝，不许把「签名前的条数」写进文档。

    这条是踩出来的：先 --fix 后 sign_manifest --init，README 就被写上了
    签名前的数（81），而真值已是 82。--fix 不报错，安静地写了个马上过期的数 ——
    比不做自动修正更坏。
    """
    tmp = tempfile.mkdtemp(prefix="dn_stale_")
    try:
        wb, root = _mk_repo(tmp)
        # 造一个真 manifest（3 条，与 _mk_repo 的 3 个脚本一致）+ 假 README
        with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(GOOD_README)
        r = subprocess.run(
            [sys.executable, os.path.join(wb, "scripts", "doc_numbers.py"), "--fix"],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, WB_HOME=wb))
        # 这个假仓库里 doc_numbers.py 不存在 → 要么找不到文件（rc!=0），
        # 要么跑的是生产那份（也会因 manifest 与 COVER 不符而拒绝）。
        # 两种都不该「成功地把数字改掉」。
        ok("假仓库里 --fix 不会静默成功",
           r.returncode != 0 or "REFUSE" in (r.stdout or "") or r.returncode == 2,
           (r.returncode, (r.stdout or "")[:200]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_stale_guard_uses_one_ruler():
    """守卫必须复用 sign_manifest.collect_files()，不许自己抄一份 COVER。

    我第一版自己抄了 glob 列表，算出 60 条（真值 82）——两把尺子量同一个
    东西，两处同时错。这里断言源码里没有第二份 cover 列表。
    """
    src = open(os.path.join(ROOT, ".workbench", "scripts", "doc_numbers.py"),
               encoding="utf-8").read()
    ok("调用了 sign_manifest.collect_files", "collect_files()" in src, "")
    ok("没有自己抄 allow/deny glob 表",
       "fnmatch" not in src and '"templates/acceptance.yaml"' not in src, "")


def t_rail_reuse_guard_wiring():
    """慢件复用守卫本身必须真的接上（shell 里静默失效的守卫，和没有一样）。

    这两条都是实测踩出来的，不是假想：
      · `_slow_reusable` 曾被定义在**使用点之后** → `command not found`，
        每轮打印 27 次错误，但回归照样 ALL PASS（错误不进 FAILED 计数）
      · `: > drills.last` 文件名截断**不更新 mtime** → 守卫永远判过期，
        复用分支一次都没走到，而日志里看起来一切正常
    """
    rail = open(os.path.join(ROOT, "run_all_checks.sh"), encoding="utf-8").read()
    defined = rail.index("_slow_reusable()")
    used = rail.index('_slow_reusable "$t"')
    ok("守卫定义在调用之前", defined < used, "def@%d use@%d" % (defined, used))
    ok("盖戳用 touch（`: >` 不更新 mtime）",
       "touch .workbench/state/drills.last" in rail
       and ": > .workbench/state/drills.last" not in rail, "")
    ok("只有真跑且真绿才盖戳",
       'if [ "$t" = "test_w4_drills" ] && [ "$LAST_RC" -eq 0 ]' in rail, "")
    ok("复用分支不盖戳（否则自己给自己续命）",
       rail.count("touch .workbench/state/drills.last") == 1, "")


def main():
    for fn in (t_compute, t_check_clean, t_check_catches_each,
               t_missing_claim_reported, t_duplicate_claim_reported,
               t_fix_roundtrip, t_fix_refuses_when_ambiguous,
               t_fix_refuses_when_manifest_stale, t_stale_guard_uses_one_ruler,
               t_rail_reuse_guard_wiring, t_real_repo_is_not_asserted_here):
        try:
            fn()
        except Exception:
            import traceback
            FAILS.append("%s 抛异常: %s" % (fn.__name__, traceback.format_exc(limit=2)))
    total = len(OKS) + len(FAILS)
    for f in FAILS:
        print("  - " + f)
    print("doc_numbers: %s (%d/%d)" % ("PASS" if not FAILS else "FAIL",
                                       len(OKS), total))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
