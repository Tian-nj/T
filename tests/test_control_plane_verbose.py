# -*- coding: utf-8 -*-
"""第四十轮：`--verbose` 下沉到 lead.py / merge_queue.py 的防回归。

只收敛「纯进展回显」。机器读（--json）与契约行（REFUSE / WARN / RESULT /
MERGED / SKIP / WAIT / GATED / FROZEN / VERIFY / chain: / ran: / 备料完成）
必须保留裸 print —— 它们是 run_task.py、run_all_checks.sh、audit_closure.py
和一批测试的输入。把它们一并收进 --verbose，等于让「通过」与「没跑」
在调用方眼里长得一样。

在临时仓里真跑，不碰生产链：WB_HOME 指到 tempdir，events/members 都新建。
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

import lead            # noqa: E402
import merge_queue     # noqa: E402
import run_task        # noqa: E402

FAILED = []


def t(name, got, want=True):
    if got != want:
        FAILED.append("%s (got=%r want=%r)" % (name, got, want))
    return got == want


def src(name):
    with io.open(os.path.join(SCRIPTS, name), "r", encoding="utf-8",
                 newline="") as fh:
        return fh.read()


def fresh():
    tmp = tempfile.mkdtemp(prefix="wb_cp_verbose_")
    wb = os.path.join(tmp, ".workbench")
    os.makedirs(wb)
    for d in ("spec", "scripts", "roles", "templates"):
        s = os.path.join(ROOT, ".workbench", d)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(wb, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("config.yaml", "Constitution.md", "constitution.rules.yaml"):
        s = os.path.join(ROOT, ".workbench", f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(wb, f))
    s = os.path.join(ROOT, "Constitution.md")
    if os.path.isfile(s):
        shutil.copy2(s, os.path.join(tmp, "Constitution.md"))
    os.makedirs(os.path.join(wb, "events"), exist_ok=True)
    return tmp, wb


def env_for(wb):
    return dict(os.environ, WB_HOME=wb, PYTHONIOENCODING="utf-8",
                WORKBENCH_ROOT_KEY="dev-root-key-2026-please-replace")


def run_lead(wb, tmp, argv):
    c = [sys.executable, os.path.join(SCRIPTS, "lead.py")] + argv
    p = subprocess.run(c, env=env_for(wb), cwd=tmp,
                       capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def t_source_truth():
    for name, mod in (("lead.py", lead), ("merge_queue.py", merge_queue)):
        s = src(name)
        t(name + " 开关是单元素列表", "VERBOSE = [False]" in s)
        t(name + " 有 def log(", "def log(" in s)
        t(name + " log 受 VERBOSE[0] 约束",
          "def log(" in s and "if VERBOSE[0]:" in s.split("def log(")[1][:200])
        t(name + " argparse 真有 --verbose", '--verbose", action="store_true"' in s)
        t(name + " main 真拨开关", "VERBOSE[0] = bool(" in s)
    ls, ms = src("lead.py"), src("merge_queue.py")
    t("lead --json 仍裸 print", ls.count("print(json.dumps(") >= 4)
    t("lead chain/ran/RESULT 仍裸 print",
      all(('print("' + k) in ls for k in ("chain:", "ran:", "RESULT:")))
    t("lead 备料完成仍裸 print（audit_closure 读它）",
      'print("%s 备料完成' in ls or "备料完成" in ls)
    t("lead REFUSE/WARN 仍裸 print",
      'print("  REFUSE' in ls or "REFUSE" in ls.split("def log(")[0])
    t("merge_queue --json 仍裸 print", "print(json.dumps(result" in ms)
    # WAIT / GATED 是同一个 %-8s 格式串按 tag 动态填出来的；
    # REVERT FAILED 带前导空格。把格式串当字面量找，会报「结果行丢了」——
    # 那是尺子错，不是生产缺陷。
    for k in ("  MERGED", "  REJECTED", "  SKIP", "  FROZEN"):
        t("merge_queue 结果行 %r 仍裸 print" % k,
          ('print("' + k) in ms)
    t("merge_queue WAIT/GATED 动态 tag 仍裸 print",
      'tag = "WAIT" if g.get("wait_sign") else "GATED"' in ms
      and 'print("  %-8s %s' in ms)
    t("merge_queue REVERT FAILED 仍裸 print",
      'print("           REVERT FAILED' in ms)
    t("merge_queue 纯进展行走 log", 'log("merge_queue →' in ms)
    rts = io.open(os.path.join(ROOT, "run_task.py"), encoding="utf-8").read()
    t("run_task 仍解析四个结果行",
      all(x in rts for x in ("MERGED\\s+", "WAIT\\s+", "GATED\\s+", "SKIP\\s+")))


def t_log_behavior():
    for mod in (lead, merge_queue, run_task):
        buf = io.StringIO()
        mod.VERBOSE[0] = False
        with contextlib.redirect_stdout(buf):
            mod.log("PROBE_SILENT")
        t(mod.__name__ + " 默认 log 不打", "PROBE_SILENT" not in buf.getvalue())
        buf = io.StringIO()
        mod.VERBOSE[0] = True
        with contextlib.redirect_stdout(buf):
            mod.log("PROBE_LOUD")
        t(mod.__name__ + " 开 verbose log 才打", "PROBE_LOUD" in buf.getvalue())
        mod.VERBOSE[0] = False


def t_lead_real_run():
    tmp1, wb1 = fresh()
    tmp2, wb2 = fresh()
    try:
        argv = ["task-new", "--task", "T-V1", "--title", "verbose probe",
                "--scope", "src/**", "--cmd", "python tests/test_calc.py",
                "--priority", "P2"]
        rc0, out0 = run_lead(wb1, tmp1, argv)
        rc1, out1 = run_lead(wb2, tmp2, ["--verbose"] + argv)
        t("默认 task-new rc=0", rc0 == 0)
        t("默认 task-new 不打过程行", "T-V1 created" not in out0, True)
        t("默认 task-new 不打 digest", "digest=" not in out0, True)
        t("verbose task-new rc=0", rc1 == 0)
        t("verbose task-new 打 created", "T-V1 created" in out1, True)
        t("verbose task-new 打 digest", "digest=" in out1, True)

        # 机器读输出不受开关影响：默认（不传 --verbose）仍出 JSON
        rc2, out2 = run_lead(wb1, tmp1, ["status", "--json"])
        t("status --json rc=0", rc2 == 0)
        try:
            doc = json.loads(out2.strip())
            t("status --json 可解析且非空", isinstance(doc, dict) and bool(doc))
        except Exception as exc:
            t("status --json 可解析", False, True)
            FAILED.append("status --json parse: %s :: %r" % (exc, out2[:200]))

        # check 的 chain:/ran:/RESULT: 是 run_all_checks 的输入，必须还在
        rc3, out3 = run_lead(wb1, tmp1, ["check"])
        t("check 打 chain:", "chain:" in out3, True)
        t("check 打 ran:", "ran:" in out3, True)
        t("check 打 RESULT:", "RESULT:" in out3, True)
    finally:
        shutil.rmtree(tmp1, ignore_errors=True)
        shutil.rmtree(tmp2, ignore_errors=True)


def t_merge_queue_switch():
    # --dry-run 自动开 verbose（预演要看得见过程）；结果行不受影响
    s = src("merge_queue.py")
    t("merge_queue 标题行已降级", 'log("merge_queue →' in s)
    t("merge_queue 合计行已降级", 'log("merged %d' in s)
    t("merge_queue 冒烟尾巴已降级", 'log("           tail:' in s)
    # 不走 --help 子进程：Windows 控制台是 GBK，subprocess(text=True) 会
    # UnicodeDecodeError 崩掉读线程，把「尺子读不了」误判成「开关不存在」。
    t("merge_queue main 真有 --verbose 参数",
      'ap.add_argument("--verbose", action="store_true"' in s)
    t("merge_queue dry-run 自动开 verbose", "a.verbose or a.dry_run" in s)


def main():
    t_source_truth()
    t_log_behavior()
    t_lead_real_run()
    t_merge_queue_switch()
    if FAILED:
        for line in FAILED:
            print("FAIL: %s" % line)
        print("test_control_plane_verbose: FAIL (%d failed)" % len(FAILED))
        return 1
    print("test_control_plane_verbose: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
