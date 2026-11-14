#!/usr/bin/env python3
"""test_lock_losses.py —— 观测丢失必须留痕（第四十五轮；第四十六轮 proc_key）。

要防的事：台账写不进去 → `_Lock` 只把计数加在内存里 → 进程退出计数消失 →
报表里「只有 3 条样本」和「样本丢了 300 条只剩 3 条」完全同形。

第四十六轮补一条：OS 会复用 pid。仅按 pid 聚合会把两个不同进程合并成一个，
计数偏小 —— 所以用 proc_key（进程级 UUID）当真相键，pid 复用时照样分开。

五组断言：
  1. 台账目录不可写时，子进程退出后至少对 stderr 说了话（观测链条尽头）；
  2. 目录可写时 first/final 带 proc_key/proc_start_ms，read_losses 读得回；
  3. 零丢失不许凭空产生行；
  4. pid 复用不合并、同 proc_key 的 first/final 取 max、legacy 行不并入新行；
  5. check()/report() 把丢失说出来，loss_rows 可追 proc_key。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import lock_calibrate as lc  # noqa: E402
import wb_core  # noqa: E402

fails = []


def ok(name, got, want=True, detail=None):
    good = (got == want)
    if not good:
        fails.append("%s: got=%r want=%r %s"
                     % (name, got, want, "" if detail is None else detail))
    print("%s %s" % ("  ok  " if good else " FAIL ", name))


CHILD = r'''
import json, os, sys
# 临时仓里只有 events/ 与 state/，控制面源码必须从**生产仓**导入。
sys.path.insert(0, %(scripts)r)
import wb_core
st = wb_core.Store(%(wb)r, "0" * 32)
lk = st._lock(timeout_ms=500)
lk._note(11, True)
print("note_losses=%%d hold_losses=%%d proc_key=%%s"
      %% (wb_core.Store._Lock.NOTE_LOSSES,
          wb_core.Store._Lock.HOLD_LOSSES,
          wb_core.Store._Lock.PROC_KEY))
'''


def blow_up(log_dir):
    """把 log_dir 弄成一个**文件**：os.makedirs(exist_ok=True) 必然失败。

    比 chmod 可靠：Windows 上目录权限位跟 POSIX 不是一回事，用权限造的
    「不可写」在 CI 上时灵时不灵，会让人学会重跑而不是查为什么。
    """
    os.makedirs(os.path.dirname(log_dir), exist_ok=True)
    with open(log_dir, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("not a directory\n")
    return log_dir


def make_wb(tag):
    root = tempfile.mkdtemp(prefix="lockloss-%s-" % tag)
    wb = os.path.join(root, ".workbench")
    os.makedirs(os.path.join(wb, "events"))
    os.makedirs(os.path.join(wb, "state"))
    os.makedirs(os.path.join(wb, "spec"))
    # check() 会读 spec/lock_semantics.json。缺了它红的是「规格读不到」
    # 而不是「丢失有没有被说出来」—— 一次失败冒充另一次失败。
    with open(os.path.join(wb, "spec", "lock_semantics.json"), "w",
              encoding="utf-8", newline="\n") as fh:
        json.dump({"schema": 1, "criterion": "lock_semantics_v1",
                   "equivalent": False,
                   "common": {"timeout_ms_default": 30000,
                              "backoff_start_ms": 2,
                              "backoff_max_ms": 50,
                              "timeout_exception": "wb_core.LockTimeout"}}, fh)
    return wb


def _reset_class_counters():
    cls = wb_core.Store._Lock
    cls.NOTE_LOSSES = 0
    cls.HOLD_LOSSES = 0
    cls._LOSS_HOOKED = False
    cls._LOSS_DIR = None


def t_loss_row_survives_process_exit():
    """真丢一次 → 真退出 → 台账目录本身也不可写，只剩 stderr 一行。"""
    wb = make_wb("exit")
    try:
        bad_dir = blow_up(os.path.join(wb, "state", "lock_telemetry"))
        ok("log_dir 是个文件（makedirs 必失败）", os.path.isfile(bad_dir))
        env = dict(os.environ)
        env.pop("WB_LOCK_TELEMETRY", None)
        env["PYTHONIOENCODING"] = "utf-8"
        code = CHILD % {"wb": wb, "scripts": os.path.join(WB, "scripts")}
        # encoding 显式 utf-8：本机默认 gbk，子进程 WARN 是中文，默认解码
        # 会在**父进程**抛 UnicodeDecodeError，伪装成「子进程失败」。
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=env, cwd=ROOT, timeout=120)
        ok("子进程正常退出", p.returncode, 0, (p.stderr or "")[-600:])
        ok("子进程里计数真的加了（进程级可见）",
           "note_losses=1" in (p.stdout or ""), True, (p.stdout or "")[-200:])
        ok("子进程有非空 proc_key（第四十六轮）",
           "proc_key=" in (p.stdout or "")
           and "proc_key=\n" not in (p.stdout or ""),
           True, (p.stdout or "")[-200:])
        warned = ("WARN lock_waits 样本写失败" in (p.stderr or "")
                  or "WARN lock_losses 痕迹也写不进去" in (p.stderr or ""))
        ok("写不进去时至少对 stderr 说了话", warned, True,
           (p.stderr or "")[-400:])
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def t_loss_row_when_dir_ok_but_file_fails():
    """目录可写时直接走 _record_loss / _write_loss_final，验行内容。"""
    wb = make_wb("record")
    try:
        d = os.path.join(wb, *lc.LOG_DIR_REL.split("/"))
        os.makedirs(d, exist_ok=True)
        lk = wb_core.Store._Lock(os.path.join(wb, "state", ".lock"),
                                 timeout_ms=1000, log_dir=d)
        type(lk).NOTE_LOSSES = 0
        type(lk).HOLD_LOSSES = 0
        type(lk).NOTE_LOSSES += 7
        type(lk).HOLD_LOSSES += 2
        # _write_loss_final 只读类属性 _LOSS_DIR，它由这里设置。
        lk._hook_loss_atexit()
        lk._record_loss("lock_waits")
        lk._write_loss_final()

        files = [n for n in os.listdir(d)
                 if n.startswith(wb_core.Store._Lock.LOSS_LOG_PREFIX)]
        ok("真的写出了一个 lock_losses 文件", len(files), 1, files)
        rows = []
        with open(os.path.join(d, files[0]), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
        ok("写了 first 与 final 两行", [r.get("phase") for r in rows],
           ["first", "final"])
        fin = [r for r in rows if r.get("phase") == "final"][0]
        ok("final 行带 note_losses>0（写 0 等于没记）",
           fin.get("note_losses", 0) > 0, True, fin)
        ok("final 行带 hold_losses", fin.get("hold_losses"), 2)
        ok("final 行带 pid", isinstance(fin.get("pid"), int), True, fin)
        ok("final 行带 proc_key（第四十六轮）",
           isinstance(fin.get("proc_key"), str) and bool(fin.get("proc_key")),
           True, fin)
        ok("final 行带 proc_start_ms（第四十六轮）",
           isinstance(fin.get("proc_start_ms"), int), True, fin)
        ok("first/final 用同一个 proc_key",
           rows[0].get("proc_key") == fin.get("proc_key"), True, rows)

        ls = lc.read_losses(wb)
        ok("read_losses 读得回来", ls["note_losses"], 7)
        ok("read_losses 的 hold 也读得回来", ls["hold_losses"], 2)
        ok("read_losses processes=1", ls["processes"], 1)
        ok("read_losses legacy_rows=0（新行都有 proc_key）",
           ls["legacy_rows"], 0)
        ok("first/final 按 proc_key 取 max 不相加", ls["note_losses"], 7)
        ok("rows 带 proc_key", ls["rows"][0].get("proc_key"),
           fin.get("proc_key"))
    finally:
        _reset_class_counters()
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def t_pid_reuse_not_merged():
    """第四十六轮核心：OS 复用 pid 时两个不同进程不能合并成一个。"""
    wb = make_wb("reuse")
    try:
        d = os.path.join(wb, *lc.LOG_DIR_REL.split("/"))
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, wb_core.Store._Lock.LOSS_LOG_PREFIX + "2026-01-01.jsonl")
        with open(f, "w", encoding="utf-8", newline="\n") as fh:
            # 同一个 pid=555，两个不同进程（proc_key 不同）
            print(json.dumps({"schema": 1, "pid": 555, "proc_key": "p1",
                              "proc_start_ms": 1000, "phase": "final",
                              "note_losses": 10, "hold_losses": 2}), file=fh)
            print(json.dumps({"schema": 1, "pid": 555, "proc_key": "p2",
                              "proc_start_ms": 8000, "phase": "final",
                              "note_losses": 6, "hold_losses": 3}), file=fh)
        ls = lc.read_losses(wb)
        ok("pid 复用时两个进程按 proc_key 相加", ls["note_losses"], 16)
        ok("pid 复用时 hold 相加", ls["hold_losses"], 5)
        ok("pid 复用时 processes=2（不是 1）", ls["processes"], 2)
        ok("pid 复用时 pids=1", ls["pids"], 1)
        ok("pid 复用时 legacy_rows=0", ls["legacy_rows"], 0)
        ok("rows 带两个 proc_key",
           {r["proc_key"] for r in ls["rows"]}, {"p1", "p2"})

        # 同一 proc_key 的 first/final 取 max 不相加
        with open(f, "a", encoding="utf-8", newline="\n") as fh:
            print(json.dumps({"schema": 1, "pid": 555, "proc_key": "p1",
                              "proc_start_ms": 1000, "phase": "first",
                              "note_losses": 4, "hold_losses": 1}), file=fh)
        ls2 = lc.read_losses(wb)
        ok("同 proc_key 的 first/final 取 max 不相加", ls2["note_losses"], 16)
        ok("同 proc_key 后 processes 仍为 2", ls2["processes"], 2)

        # legacy 行（无 proc_key）不合并进 proc_key 行
        with open(f, "a", encoding="utf-8", newline="\n") as fh:
            print(json.dumps({"schema": 1, "pid": 555, "phase": "final",
                              "note_losses": 9, "hold_losses": 0}), file=fh)
        ls3 = lc.read_losses(wb)
        ok("legacy 行不并入 proc_key", ls3["note_losses"], 25)
        ok("legacy_rows=1", ls3["legacy_rows"], 1)
        ok("legacy 单独成组（processes=3）", ls3["processes"], 3)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def t_no_loss_means_no_row():
    """没有丢失时不许有这一行：否则「有损失」就不再是信号。"""
    wb = make_wb("clean")
    try:
        d = os.path.join(wb, *lc.LOG_DIR_REL.split("/"))
        os.makedirs(d, exist_ok=True)
        cls = wb_core.Store._Lock
        _reset_class_counters()
        lk = cls(os.path.join(wb, "state", ".lock"),
                 timeout_ms=1000, log_dir=d)
        lk._write_loss_final()
        files = [n for n in os.listdir(d)
                 if n.startswith(cls.LOSS_LOG_PREFIX)]
        ok("零丢失不产生 lock_losses 文件", files, [])
        ok("read_losses 对空目录报 0", lc.read_losses(wb)["note_losses"], 0)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def t_check_mentions_losses():
    """check()/report() 必须把丢失说出来，loss_rows 可追 proc_key。"""
    wb = make_wb("check")
    try:
        d = os.path.join(wb, *lc.LOG_DIR_REL.split("/"))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, wb_core.Store._Lock.LOSS_LOG_PREFIX + "2026-01-01.jsonl"),
                  "w", encoding="utf-8", newline="\n") as fh:
            print(json.dumps({"schema": 1, "pid": 4242, "proc_key": "K4242",
                              "proc_start_ms": 1234, "kind": "final",
                              "phase": "final", "note_losses": 9,
                              "hold_losses": 0}), file=fh)
        rc = lc.check(wb)
        ok("有丢失时 check 仍退 0（样本少了不等于判据崩了）", rc, 0)
        rep = lc.report(wb)
        ok("report 里带 loss_rows",
           isinstance(rep.get("loss_rows"), list) and bool(rep["loss_rows"]), True)
        ok("loss_rows 指到那个 pid", rep["loss_rows"][0].get("pid"), 4242)
        ok("loss_rows 指到 proc_key", rep["loss_rows"][0].get("proc_key"),
           "K4242")
        ok("recommend.losses 带 processes", rep["losses"].get("processes"), 1)
        ok("recommend.losses 带 legacy_rows",
           rep["losses"].get("legacy_rows"), 0)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def main():
    print("== 观测丢失必须留痕（proc_key 版） ==")
    t_loss_row_survives_process_exit()
    t_loss_row_when_dir_ok_but_file_fails()
    t_pid_reuse_not_merged()
    t_no_loss_means_no_row()
    t_check_mentions_losses()
    print("test_lock_losses: %s (%d failed)"
          % ("PASS" if not fails else "FAIL", len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
