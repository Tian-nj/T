#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_spawn_containment.py — 第二十三轮 opt-in 进程遏制三证。

第二十二轮「探，不接」证明 Job Object 只做进程遏制、不隔离网络/文件；
第二十三轮把它以 contain=True 接进 platform_ops.spawn_group，默认关。
本测试钉死四件事：
  real      —— 真起子进程，contain=True 在本机 applied=True，close 能触发 kill；
  injection —— 强制底层失败时必须 degraded 且留具体原因，不许假装已遏制；
  off       —— contain=False 显式 off，没接和没跑不许长得一样；
  bypass    —— 验收命令不自动 opt-in、shim 不碰内核符号、driver_wiring 仍 wired。

运行：python tests/test_spawn_containment.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

import platform_ops as po  # noqa: E402
import job_object_probe as jop  # noqa: E402

FAILS = []


def ck(name, cond):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s" % name)


def _quick_child():
    return [sys.executable, "-c", "print('SPAWN_OK')"]


def _sleeping_child():
    return [sys.executable, "-c", "import time; time.sleep(30)"]


def t_default_off():
    print("[1] contain=False：显式 off，不碰 Job")
    p = po.spawn_group(_quick_child(), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    out, _ = p.communicate(timeout=30)
    ck("默认调用仍正常跑完", p.returncode == 0 and b"SPAWN_OK" in (out or b""))
    c = getattr(p, "_wb_containment", None)
    ck("contain=False 也留下显式状态字段", isinstance(c, dict))
    ck("mode=off", bool(c) and c["mode"] == "off")
    ck("applied=False", bool(c) and c["applied"] is False)
    ck("detail 写明 contain=False，不是空串",
       bool(c) and "contain=False" in c["detail"])
    ck("没有 Job handle", not hasattr(p, "_wb_job_handle"))
    ck("close_containment 明确说没 handle",
       po.close_containment(p).get("closed") is False)


def t_real_opt_in():
    print("[2] contain=True：真起子进程验证进程遏制")
    p = po.spawn_group(_sleeping_child(), contain=True)
    c = getattr(p, "_wb_containment", None)
    try:
        ck("有 _wb_containment 字段", isinstance(c, dict))
        ck("detail 非空（不许静默）", bool(c) and bool(c.get("detail")))
        ck("active_process_limit 生产刻意不设",
           bool(c) and c.get("active_process_limit") is None)
        if sys.platform != "win32":
            ck("非 win32 诚实降级 NOT_WINDOWS",
               bool(c) and c["mode"] == "degraded"
               and "NOT_WINDOWS" in c["detail"])
            ck("非 win32 不挂 Job handle", not hasattr(p, "_wb_job_handle"))
        else:
            # 本机（win32）第二十三轮实测 Extended(9)+EXTENDED 可用；
            # 若哪天被改回 BasicLimitInformation(2)，这里会红 —— 正是要抓的。
            ck("win32 上 applied=True / kill_on_job_close",
               bool(c) and c["applied"] is True
               and c["mode"] == "kill_on_job_close")
            ck("成功时挂 _wb_job_handle", hasattr(p, "_wb_job_handle"))
            closed = po.close_containment(p)
            ck("close_containment 真的关掉 handle",
               closed.get("closed") is True)
            t0 = time.time()
            try:
                p.wait(timeout=10)
                ck("关闭 handle 后子进程被 kill-on-close 终止（<10s）",
                   time.time() - t0 < 10.0)
            except Exception as exc:  # noqa: BLE001
                ck("关闭 handle 后子进程被 kill-on-close 终止（<10s）：%s" % exc,
                   False)
    finally:
        if p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


def t_injection_degrade():
    print("[3] 失败注入：底层原语失败必须降级留痕")
    orig = po._contain_process

    def fake(pid):
        return False, "INJECTED_FAIL: 模拟 OpenProcess/Assign 失败", None

    po._contain_process = fake
    try:
        p = po.spawn_group(_quick_child(), contain=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = p.communicate(timeout=30)
        ck("降级不抛异常，业务照跑",
           p.returncode == 0 and b"SPAWN_OK" in (out or b""))
        c = getattr(p, "_wb_containment", None)
        ck("mode=degraded", bool(c) and c["mode"] == "degraded")
        ck("applied=False", bool(c) and c["applied"] is False)
        ck("detail 带回注入的失败原因",
           bool(c) and "INJECTED_FAIL" in c["detail"])
        ck("失败时不挂 Job handle", not hasattr(p, "_wb_job_handle"))
        ck("没有偷偷设 ActiveProcessLimit",
           bool(c) and c["active_process_limit"] is None)
    finally:
        po._contain_process = orig


def t_bypass_and_source():
    print("[4] 默认 opt-out / shim 不碰内核 / 源码真值仍 wired")
    w = jop.driver_wiring()
    ck("driver_wiring.wired=True", w["wired"] is True)
    ck("默认 contain=False", w["default_contain"] is False)
    ck("真实 worker contain=True", w["real_worker_contain"] is True)
    ck("不参与 W4 台账", w["w4_ledger_participates"] is False)
    ck("os_sandbox 仍 compensated_absent",
       w["ledger_os_sandbox_track"] == "compensated_absent")
    vh = open(os.path.join(SCRIPTS, "verify_harness.py"),
              encoding="utf-8").read()
    ck("验收命令不自动 opt-in（contain=True 不在 verify_harness）",
       "contain=True" not in vh)
    shim = open(os.path.join(SCRIPTS, "worker_shim", "sitecustomize.py"),
                encoding="utf-8").read()
    ck("worker_shim 未引入内核 Job 符号", "CreateJobObject" not in shim)
    ck("job_object_probe --check 门禁通过", jop.check_registration() == 0)
    d = json.load(open(jop.OUT_ABS, encoding="utf-8"))
    ck("登记 driver_wiring 与源码真值逐字一致", d["driver_wiring"] == w)
    ck("登记仍不涂 enforced",
       d["capability_impact"]["promotes_to_enforced"] is False)


def main():
    for fn in (t_default_off, t_real_opt_in, t_injection_degrade,
               t_bypass_and_source):
        fn()
    if FAILS:
        print("test_spawn_containment: FAIL (%d failed)" % len(FAILS))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("test_spawn_containment: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
