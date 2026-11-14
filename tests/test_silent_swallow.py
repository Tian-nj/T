# -*- coding: utf-8 -*-
"""test_silent_swallow.py —— 第四十四/四十五轮：吞异常必须留痕的防回归。

测五件事：
1. 生产树真干净（VERDICT CLEAN，0 unallowed、0 stale、0 scope_problems）。
2. 判据真的只看「处理器里有没有痕迹」：注入 `except Exception: pass` 必须被抓。
3. 白名单真在起作用，且**烂掉会报**（stale 检测）。
4. 覆盖范围本身烂了会红（漏目录 / 一个 .py 都没扫到不能冒充 CLEAN）。
5. `wb_core` 的两处观测写失败改成计数：源码里不许再出现裸 pass，
   且计数器是类属性（进程级可见）。

为什么第 2 条要单独测：探针若只报生产干净，它可能根本没在工作——
一个永远什么都不抓的探针，输出和「真干净」一模一样。

第四十五轮把探针从「只扫 scripts」扩成「scripts / root / tests 三处作用域」，
`scan()` 与 `report()` 的签名也跟着变了。这里把旧调用一并改掉：旧测试若还按
`ssp.scan(tmp)` / `ssp.report(tmp)` 跑，会先炸在 API 上，而不是验到任何判据。
"""
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import silent_swallow_probe as ssp  # noqa: E402
import wb_core  # noqa: E402

_R = []


def ok(name, cond, detail=""):
    _R.append(bool(cond))
    print("ok   %s" % name if cond else "FAIL %s  %s" % (name, detail))


def t_production_clean():
    rep = ssp.report()
    ok("生产树 VERDICT CLEAN", rep["verdict"] == "SILENT_SWALLOW_CLEAN", rep["verdict"])
    ok("没有未登记的静默处理器", rep["findings"] == [], rep["findings"])
    ok("白名单没有腐烂条目", rep["stale_allowlist"] == [], rep["stale_allowlist"])
    ok("覆盖范围本身对得上账（一个 .py 都没扫到也要红）",
       rep["scope_problems"] == [], rep["scope_problems"])
    ok("三处作用域都真扫了文件",
       rep["scopes"] == ["scripts", "root", "tests"]
       and all((rep["per_scope"].get(k) or {}).get("files", 0) > 0
               for k in ("scripts", "root", "tests")),
       rep["per_scope"])
    ok("扫描到的 Exception 处理器足够多（不是空扫描）",
       rep["scanned"] >= 80, rep["scanned"])
    ok("每条白名单都写了理由",
       all((v or "").strip() for v in ssp.ALLOWED.values()),
       [k for k, v in ssp.ALLOWED.items() if not (v or "").strip()])
    ok("白名单 key 带作用域（全域化后不再允许裸文件名）",
       all(":" in k and "#" in k for k in ssp.ALLOWED),
       [k for k in ssp.ALLOWED if ":" not in k or "#" not in k])


def _tmp_scope(tmp):
    """把临时目录挂成探针的额外作用域，并临时清空生产白名单。

    第四十五轮把探针改成「按作用域扫描 + key 带作用域前缀」之后，
    `scan(tmp)` / `report(tmp)` 这种第四十四轮的旧调用全部失效。
    `SCAN_SCOPES` 里的值允许是绝对路径（os.path.join(ROOT, abs) 返回 abs），
    所以把临时目录挂成一个真作用域，比给探针开一个「裸路径后门」干净。
    """
    saved_scopes = ssp.SCAN_SCOPES
    saved_allowed = dict(ssp.ALLOWED)
    ssp.SCAN_SCOPES = ssp.SCAN_SCOPES + (("tmp", tmp),)
    ssp.ALLOWED.clear()
    return saved_scopes, saved_allowed


def _restore_scope(saved):
    saved_scopes, saved_allowed = saved
    ssp.SCAN_SCOPES = saved_scopes
    ssp.ALLOWED.clear()
    ssp.ALLOWED.update(saved_allowed)


def t_probe_actually_catches():
    """注入一个静默处理器，探针必须报出来 —— 否则它等于没跑。"""
    tmp = tempfile.mkdtemp(prefix="ssp_")
    saved = _tmp_scope(tmp)
    try:
        src = '''def f():
    try:
        return 1
    except Exception:
        pass

def g():
    try:
        return 2
    except Exception as exc:
        print('boom', exc)
'''
        with io.open(os.path.join(tmp, "sample.py"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(src)
        findings, scanned, per_scope = ssp.scan(only="tmp")
        ok("注入的静默处理器被抓到", len(findings) == 1, findings)
        ok("只抓静默那一个（打了 print 的不算）",
           bool(findings) and findings[0]["ordinal"] == 1, findings)
        ok("扫描计数包含两个 Exception 处理器", scanned == 2, scanned)
        ok("作用域标对了", bool(findings) and findings[0]["scope"] == "tmp", findings)
        ok("per_scope 如实记了文件数", len(per_scope["tmp"]["files"]) == 1,
           per_scope)

        # 白名单一挪，同样的代码就不该再被报。
        # 查的是 report(only=...) 而不是 scan：scan 只做发现，
        # 白名单过滤在 report 里 —— 用 scan 验白名单永远过不了。
        # key 现在必须带作用域前缀。
        ssp.ALLOWED["tmp:sample.py#1"] = "测试用"
        rep = ssp.report(only="tmp")
        ok("登记后不再报（白名单真在生效）", rep["findings"] == [],
           rep["findings"])
        ok("登记后 silent_total 仍如实统计（发现 ≠ 判红）",
           rep["silent_total"] == 1, rep["silent_total"])
        ok("登记后白名单没有腐烂条目", rep["stale_allowlist"] == [],
           rep["stale_allowlist"])
    finally:
        _restore_scope(saved)
        shutil.rmtree(tmp, ignore_errors=True)


def t_stale_allowlist_is_loud():
    """白名单腐烂必须报 —— 否则它会长成一堆指向空气的豁免。"""
    tmp = tempfile.mkdtemp(prefix="ssp2_")
    saved = _tmp_scope(tmp)
    try:
        with io.open(os.path.join(tmp, "ok.py"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write('''def f():
    try:
        pass
    except Exception:
        print('x')
''')
        ssp.ALLOWED["tmp:ok.py#1"] = "该处理器其实没静默"
        ssp.ALLOWED["tmp:gone.py#1"] = "文件根本不存在"
        rep = ssp.report(only="tmp")
        keys = [s["key"] for s in rep["stale_allowlist"]]
        ok("「已不再静默」的条目被报为 stale", "tmp:ok.py#1" in keys, keys)
        ok("「文件不存在」的条目被报为 stale", "tmp:gone.py#1" in keys, keys)
        ok("stale 存在时 VERDICT 不为 CLEAN",
           rep["verdict"] != "SILENT_SWALLOW_CLEAN", rep["verdict"])
    finally:
        _restore_scope(saved)
        shutil.rmtree(tmp, ignore_errors=True)


def t_scope_audit_is_loud():
    """覆盖范围本身烂了也要红：漏一个目录不能算 CLEAN。"""
    saved_scopes = ssp.SCAN_SCOPES
    try:
        ssp.SCAN_SCOPES = ssp.SCAN_SCOPES + (("missing", "no_such_dir_xyz"),)
        problems = ssp.scope_audit()
        ok("作用域目录不存在会被报出来",
           any("missing" in p and "目录不存在" in p for p in problems),
           problems)
        rep = ssp.report()
        ok("目录不存在时 VERDICT 不为 CLEAN",
           rep["verdict"] != "SILENT_SWALLOW_CLEAN", rep)
        ok("scope_problems 写进了 report",
           bool(rep["scope_problems"]), rep["scope_problems"])
    finally:
        ssp.SCAN_SCOPES = saved_scopes


def t_wb_core_counts_losses():
    """wb_core 的观测写失败：不许再裸 pass，必须有进程级计数。"""
    with io.open(os.path.join(WB, "scripts", "wb_core.py"), "r",
                 encoding="utf-8") as fh:
        src = fh.read()
    ok("_Lock 有 NOTE_LOSSES 与 HOLD_LOSSES 两个类属性",
       "NOTE_LOSSES = 0" in src and "HOLD_LOSSES = 0" in src)
    ok("_note 的失败分支递增 NOTE_LOSSES",
       "type(self).NOTE_LOSSES += 1" in src)
    ok("持锁样本的失败分支递增 HOLD_LOSSES",
       "type(self).HOLD_LOSSES += 1" in src)
    # 只查**代码行**：这行判据的注释里就写着反例，按整份源码查会命中自己。
    # （第一版就是这么错的——注释里的反例把断言变成了恒假。）
    code_only = "\n".join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#"))
    ok("代码里不用 self.X += 1 自增（那会创建实例属性把类属性遮住）",
       "self.NOTE_LOSSES += 1" not in code_only
       and "self.HOLD_LOSSES += 1" not in code_only,
       "found in code")

    lock = wb_core.Store._Lock
    ok("计数器是类属性（进程级可见，不是实例私有）",
       isinstance(getattr(lock, "NOTE_LOSSES", None), int)
       and isinstance(getattr(lock, "HOLD_LOSSES", None), int),
       (getattr(lock, "NOTE_LOSSES", None),
        getattr(lock, "HOLD_LOSSES", None)))
    ok("第四十五轮：丢失台账前缀与 atexit 真在写入路径上",
       "LOSS_LOG_PREFIX" in src and "atexit.register" in src
       and "_write_loss_final" in src)
    ok("第四十六轮：丢失台账写入进程身份 proc_key/proc_start_ms",
       "PROC_KEY = " in src and "PROC_START_MS = " in src
       and "def process_identity(cls):" in src
       and "row.update(type(self).process_identity())" in src
       and "row.update(cls.process_identity())" in src)

    # 真造一次写失败：把 log_dir 指到一个不是目录的路径。
    # 这里故意把 stderr 也接住：第四十五轮新增的「痕迹也写不进去」WARN
    # 会在同一路径上打，若放着不管，测试输出会像生产真的坏了一样。
    tmp = tempfile.mkdtemp(prefix="ssp3_")
    try:
        bogus = os.path.join(tmp, "not_a_dir")
        with io.open(bogus, "w", encoding="utf-8") as fh:
            fh.write("x")
        before_n = lock.NOTE_LOSSES
        before_h = lock.HOLD_LOSSES
        err = io.StringIO()
        _old = sys.stderr
        sys.stderr = err
        try:
            lk = lock(os.path.join(tmp, "x.lock"), timeout_ms=100, log_dir=bogus)
            lk._note(1, True)
            after_first = lock.NOTE_LOSSES
            first_warn = err.getvalue()
            lk._note(2, True)          # 第二次不该再报 WARN，但计数照加
            second_warn = err.getvalue()
            lk._record_hold(3)
        finally:
            sys.stderr = _old

        ok("写台账失败时计数加一",
           after_first == before_n + 1, (before_n, after_first))
        ok("第二次失败计数继续加（丢几条记几条）",
           lock.NOTE_LOSSES == before_n + 2, (before_n, lock.NOTE_LOSSES))
        ok("写台账失败不抛（不阻断业务）", True)
        ok("首次丢失打一行 WARN（不是完全无声）",
           "样本写失败" in first_warn, repr(first_warn))
        ok("第二次丢失不再刷屏（只报一次）",
           second_warn.count("样本写失败") == 1,
           second_warn.count("样本写失败"))
        ok("持锁样本写失败也计数",
           lock.HOLD_LOSSES == before_h + 1, (before_h, lock.HOLD_LOSSES))

        # 本测试进程退出时 atexit 不该带着这些 injected losses 再写一行，
        # 否则 stderr 会多出无关 WARNING，像是测试自己制造了生产故障。
        lock.NOTE_LOSSES = before_n
        lock.HOLD_LOSSES = before_h
        lock._LOSS_DIR = None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    t_production_clean()
    t_probe_actually_catches()
    t_stale_allowlist_is_loud()
    t_scope_audit_is_loud()
    t_wb_core_counts_losses()
    n = sum(_R)
    print("test_silent_swallow: %s (%d ok, %d failed)"
          % ("PASS" if all(_R) else "FAIL", n, len(_R) - n))
    return 0 if all(_R) else 1


if __name__ == "__main__":
    sys.exit(main())
