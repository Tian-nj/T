# -*- coding: utf-8 -*-
"""test_sched_install_verbose.py -- 第四十一轮候选 5 防回归。

锁三件事：
  1. `doctor` 的 15 行逐项诊断默认收掉，只留 RESULT 行；
  2. `--verbose` 时诊断真打出来（且 root_key / registered 这些真值还在）；
  3. `status` / `install` / `uninstall` / `once` 的输出一字不动 —— 它们是
     `run_all_checks.sh` 的 `run_warn "check scheduler status"` 与人工排障
     的对外契约，verbose 收敛不许波及。

判据原则：不是「print 越少越好」，而是「默认输出只剩结论」；
把 RESULT 行也收掉等于把 --verbose 做成 --quiet 的反面极端。
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SI = os.path.join(WB, "scripts", "sched_install.py")

sys.path.insert(0, os.path.join(WB, "scripts"))

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond), detail))


def t_source_truth():
    with io.open(SI, encoding="utf-8") as fh:
        s = fh.read()
    ok("模块级 VERBOSE 开关存在", "VERBOSE = [False]" in s, "")
    ok("log() 受开关约束", "def log(*parts, **kw):" in s
       and "if VERBOSE[0]:" in s.split("def log(")[1].split("def ")[0])
    ok("doctor 的明细走 log", s.count("log(\"") >= 12
       or s.count("log('") >= 12, "")
    ok("doctor 仍有 RESULT 行（结论不许静默）",
       'print("RESULT: %s (rc=%d)"' in s)
    ok("--verbose 参数已接", "--verbose" in s and "action=\"store_true\"" in s)
    ok("--verbose 挂在 doctor 子解析器上（argparse subcommand 语义）",
       "p.add_argument(\"--verbose\", action=\"store_true\"" in s)
    ok("main 拨开关", "VERBOSE[0] = bool(getattr(a" in s)
    ok("doctor help 写明默认只打 RESULT", "默认只打 RESULT" in s)
    # 对外契约不许被波及
    ok("status 的字段行仍是裸 print", 'print("task      : %s" % TASK_NAME)' in s
       and 'print("registered: %s"' in s)
    ok("install 的 RESULT 行仍是裸 print", "RESULT: OK — 已注册" in s)
    ok("run_warn 消费的 status 输出未动", "ATTENTION — 执行点缺失或已逾期" in s)


def t_doctor_default_and_verbose():
    """真跑 doctor 两次：默认只见 RESULT；--verbose 见明细。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["WORKBENCH_ROOT_KEY"] = "dev-root-key-2026-please-replace"

    p1 = subprocess.run([sys.executable, SI, "doctor"], cwd=ROOT, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        timeout=240)
    out1 = p1.stdout.decode("utf-8", "replace")
    ok("doctor 默认 rc 是 0 或 1（不崩）", p1.returncode in (0, 1),
       "rc=%d" % p1.returncode)
    ok("doctor 默认仍有 RESULT", "RESULT:" in out1, out1[:200])
    ok("doctor 默认收掉逐项明细", "python    :" not in out1
       and "root_key  :" not in out1, out1[:200])

    p2 = subprocess.run([sys.executable, SI, "doctor", "--verbose"], cwd=ROOT,
                        env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, timeout=240)
    out2 = p2.stdout.decode("utf-8", "replace")
    ok("doctor --verbose rc 与默认一致", p2.returncode == p1.returncode,
       "rc=%d vs %d" % (p2.returncode, p1.returncode))
    ok("doctor --verbose 打出 python 行", "python    :" in out2, out2[:200])
    ok("doctor --verbose 打出 wb_home 行", "wb_home   :" in out2, out2[:200])
    ok("doctor --verbose 打出 root_key 行", "root_key  :" in out2, out2[:200])
    ok("doctor --verbose 也有 RESULT", "RESULT:" in out2, out2[:200])


def t_status_untouched():
    """status 的字段行是 run_all_checks 的契约，verbose 不许波及。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["WORKBENCH_ROOT_KEY"] = "dev-root-key-2026-please-replace"
    p = subprocess.run([sys.executable, SI, "status"], cwd=ROOT, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=240)
    out = p.stdout.decode("utf-8", "replace")
    ok("status rc 是 0 或 1", p.returncode in (0, 1), "rc=%d" % p.returncode)
    ok("status 仍有 task/registered 字段行", "task      :" in out
       and "registered:" in out, out[:200])
    ok("status 仍有 RESULT/ATTENTION", "RESULT:" in out, out[:200])


def main():
    t_source_truth()
    t_doctor_default_and_verbose()
    t_status_untouched()
    fails = [r for r in _results if not r[1]]
    for name, passed, detail in _results:
        print(("ok   " if passed else "FAIL ") + name + ("  " + detail if (detail and not passed) else ""))
    print("test_sched_install_verbose: %s (%d ok, %d failed)"
          % ("PASS" if not fails else "FAIL",
             len(_results) - len(fails), len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
