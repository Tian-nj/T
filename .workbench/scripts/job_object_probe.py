#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""job_object_probe.py — Windows 原生隔离原语实机探测（第二十二轮 / ③）。

性质：**探，不接**。用户 m10967「继续」承接的 pending 是「shim 往 Windows
Job Object / AppContainer 探」。这一轮只回答三件事：
  1. 本机这些原生原语能不能用；
  2. 它们**能**覆盖什么（进程数上限 / kill-on-close = 更强的 kill-tree）；
  3. 它们**覆盖不了**什么（网络 / 文件系统 —— 必须真跑出来，不能凭知识断言）。

刻意不做的事，理由写在明处：
  · 不接进 spawn_group。用户说的是「探」不是「接入」；接入会动 52/52 全绿的
    执行链，且一个只挡进程数、不挡网络的原语接进去容易被误读成「沙箱已就位」。
  · 不改 capability_evidence/ledger.json 六项的 effective，不把 os_sandbox 从
    absent(compensated_absent) 涂成 enforced。改判据不等于宣布通过，这是本仓
    W4 双轨化那轮钉死的底线。
  · AppContainer 只查导出符号，不真建 profile。CreateAppContainerProfile 会创建
    系统级可持久化对象，属有副作用动作，本轮「探」不越界；如实标 EXPORT_ONLY。

最大陷阱（尺子做错）：CREATE_BREAKAWAY_FROM_JOB 对**不在 job 里**的进程本就
失败（负控也是 winerror=5），所以「breakaway 被挡」是伪信号。真判据只有一组
带负控的差值：ActiveProcessLimit=1 时，负控 normal_spawn=OK / job 内
normal_spawn=BLOCKED(1816) / 拿掉 limit 后恢复 OK。三者齐才算进程遏制成立。

用法：
  python job_object_probe.py --json            # 打印结构化结论（默认）
  python job_object_probe.py --write           # 写 capability_evidence/native_isolation.json
  python job_object_probe.py --check           # 校验登记里的 driver_wiring 与源码真值一致
  python job_object_probe.py --selftest        # 自检（真跑一次 + 断言）

`--json` / `--write` / `--selftest` 的探测结论本身不因环境差异返非零：
NOT_WINDOWS / PROBE_INCONCLUSIVE 都是「可判定结论」，判它们该不该红是回归
测试的事，不是探针的事（把环境差异读成代码坏了，会让下次真坏时没人信红灯
—— 第二十轮 scheduler 探针的教训）。唯一例外是第二十三轮新增的 `--check`：
它是「接线真值」门禁，源码被改坏时返 1，那不是环境结论而是代码缺陷。

历史注记（第二十三轮 / m11112「继续下一步工作」）：
  上面的「探，不接」是**第二十二轮落笔时的真实状态**，按 K.9 纪律不回头涂改。
  第二十三轮把探明的 Job Object 以 spawn_group(..., contain=True) opt-in 接进
  真实 local_process worker，只设 KILL_ON_JOB_CLOSE，不设 ActiveProcessLimit；
  默认仍 contain=False。接入的是「更强 kill-tree / 后代进程归属」，不是内核级
  沙箱：CAPABILITY_IMPACT 与 isolation_gaps 的实机结论一字未改，os_sandbox 仍
  absent / compensated_absent，本文件仍不参与 W4 三证判定。接线真值见
  capability_evidence/native_isolation.json 的 driver_wiring 字段（由源码算出，
  不是手抄布尔），防回归见 tests/test_spawn_containment.py。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

SCHEMA = 1
PROBE = "native_isolation/v1"
THREE = ("real", "injection", "bypass")
HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.path.dirname(HERE)
OUT_REL = "capability_evidence/native_isolation.json"
OUT_ABS = os.path.join(WB, OUT_REL)

# 这是**设计判定**，不是探测输出。探测负责支撑它（isolation_gaps 真跑出
# CONNECTED/WROTE），它自己不随某次探测翻绿。测试与登记文件都引用这一份，
# 不另抄一份 —— 两处各写一份会慢慢漂成两个都叫「沙箱结论」的东西。
CAPABILITY_IMPACT = {
    "os_sandbox_state": "unchanged",
    "os_sandbox_track": "compensated_absent",
    "promotes_to_enforced": False,
    "isolates_process_count": True,
    "isolates_network": False,
    "isolates_filesystem": False,
    "usable_as": "附加进程遏制原语：进程数上限 + kill-on-close（更强的 kill-tree）",
    "not_usable_as": "网络隔离 / 文件系统隔离 / 内核级沙箱",
    "reason": ("Job Object 是进程分组 + 资源限额，不是安全边界；网络与文件系统"
               "未隔离的实机证据见 isolation_gaps。据 §9.3 补偿控制矩阵，"
               "os_sandbox 保持 absent，安全主张仍标 unproven。"),
}

# 子进程脚本：读一行 JSON 指令，按 mode 做两件事之一，输出一行 JOCHILD:<json>。
# 只在 win32 被 spawn，故 creationflags 直接用；非 win 走 NOT_WINDOWS 分支不跑它。
CHILD = r'''
import json, sys
cmd = json.loads(sys.stdin.readline())
r = {"mode": cmd["mode"]}
if cmd["mode"] == "spawn":
    try:
        subprocess_ok = __import__("subprocess")
        subprocess_ok.run([sys.executable, "-c", "pass"], capture_output=True, timeout=8)
        r["normal_spawn"] = "OK"
    except OSError as e:
        r["normal_spawn"] = "BLOCKED:winerror=%s" % getattr(e, "winerror", None)
    except Exception as e:
        r["normal_spawn"] = "BLOCKED:%s" % type(e).__name__
    try:
        sp = __import__("subprocess")
        sp.Popen([sys.executable, "-c", "pass"], creationflags=0x01000000)
        r["breakaway_spawn"] = "OK"
    except OSError as e:
        r["breakaway_spawn"] = "BLOCKED:winerror=%s" % getattr(e, "winerror", None)
    except Exception as e:
        r["breakaway_spawn"] = "BLOCKED:%s" % type(e).__name__
elif cmd["mode"] == "iso":
    try:
        s = socket.create_connection(("127.0.0.1", cmd["port"]), timeout=5)
        s.close()
        r["loopback_connect"] = "CONNECTED"
    except Exception as e:
        r["loopback_connect"] = "BLOCKED:%s" % type(e).__name__
    try:
        with open(cmd["outside"], "w", encoding="utf-8") as fh:
            fh.write("x")
        r["outside_write"] = "WROTE"
    except Exception as e:
        r["outside_write"] = "BLOCKED:%s" % type(e).__name__
# 必须把结果打回父进程。父进程只读这一行；漏掉它时 res 恒 None，
# 而「没读到」和「被挡住」在旧判据里长得一样 —— 尺子做错的同型。
print("JOCHILD:" + json.dumps(r))
'''.replace(
    "import json, sys\n",
    "import json, sys, socket, subprocess\n",
    1,
)

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_ALL = 0x0001 | 0x0100 | 0x0400  # TERMINATE | SET_QUOTA | QUERY_INFORMATION
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class _Win:
    """延迟加载 Win32 符号与结构。模块顶部不 import ctypes.wintypes / WinDLL，
    让非 win32 平台 import 本模块也不炸（NOT_WINDOWS 分支根本用不到它）。"""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.wintypes = wintypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        LONGLONG = ctypes.c_longlong
        DWORD = wintypes.DWORD
        SIZE_T = ctypes.c_size_t

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", LONGLONG),
                        ("PerJobUserTimeLimit", LONGLONG),
                        ("LimitFlags", DWORD),
                        ("MinimumWorkingSetSize", SIZE_T),
                        ("MaximumWorkingSetSize", SIZE_T),
                        ("ActiveProcessLimit", DWORD),
                        ("Affinity", SIZE_T),
                        ("PriorityClass", DWORD),
                        ("SchedulingClass", DWORD)]

        class IOCOUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC),
                        ("IoInfo", IOCOUNTERS),
                        ("ProcessMemoryLimit", SIZE_T),
                        ("JobMemoryLimit", SIZE_T),
                        ("PeakProcessMemoryUsed", SIZE_T),
                        ("PeakJobMemoryUsed", SIZE_T)]

        self.EXTENDED = EXTENDED
        self.struct_sizes = {"BASIC": ctypes.sizeof(BASIC),
                             "IOCOUNTERS": ctypes.sizeof(IOCOUNTERS),
                             "EXTENDED": ctypes.sizeof(EXTENDED)}

    def api_available(self):
        names = ("CreateJobObjectW", "SetInformationJobObject",
                 "QueryInformationJobObject", "AssignProcessToJobObject",
                 "IsProcessInJob", "TerminateJobObject", "CreateProcessW",
                 "OpenProcess", "GetCurrentProcess", "CloseHandle")
        return {n: hasattr(self.k32, n) for n in names}

    def appcontainer_exports(self):
        out = {}
        for dllname in ("kernel32", "userenv", "api-ms-win-core-appcontainer-l1-1-0"):
            try:
                d = self.ctypes.WinDLL(dllname)
                out[dllname] = {
                    "CreateAppContainerProfile": hasattr(d, "CreateAppContainerProfile"),
                    "DeriveAppContainerSidFromAppContainerName":
                        hasattr(d, "DeriveAppContainerSidFromAppContainerName"),
                    "DeleteAppContainerProfile": hasattr(d, "DeleteAppContainerProfile"),
                }
            except OSError as exc:
                out[dllname] = "LOAD_FAIL:%s" % exc
        return {
            "exports": out,
            "assembly_tested": False,
            "verdict": "EXPORT_ONLY_NOT_VALIDATED",
            "note": ("create/derive/delete 只在 userenv 有导出；kernel32 无、"
                     "api-ms 加载失败。未真建 profile：CreateAppContainerProfile "
                     "创建系统级可持久化对象，属有副作用动作，本轮『探』不越界。"
                     "因此不能把 AppContainer 记成『可用隔离』。"),
        }

    def parent_in_job(self):
        flag = self.wintypes.BOOL()
        self.k32.IsProcessInJob(self.k32.GetCurrentProcess(), None,
                                self.ctypes.byref(flag))
        return bool(flag.value)

    def run_child(self, child_path, cmd, in_job, active_limit):
        """返回 (res_dict, meta)。in_job 决定要不要建 Job；active_limit=None 表示
        不设进程数上限（注入组 / 隔离组用它，避免 limit 干扰那两组的观测）。"""
        ct = self.ctypes
        wt = self.wintypes
        p = subprocess.Popen([sys.executable, child_path], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        hjob = hproc = None
        meta = {"in_job": in_job, "active_limit": active_limit}
        if in_job:
            hjob = self.k32.CreateJobObjectW(None, None)
            info = self.EXTENDED()
            flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if active_limit:
                flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                info.BasicLimitInformation.ActiveProcessLimit = active_limit
            info.BasicLimitInformation.LimitFlags = flags
            meta["set_info_ok"] = bool(self.k32.SetInformationJobObject(
                wt.HANDLE(hjob), JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ct.byref(info), ct.sizeof(info)))
            hproc = self.k32.OpenProcess(PROCESS_ALL, False, p.pid)
            meta["assign_ok"] = bool(self.k32.AssignProcessToJobObject(
                wt.HANDLE(hjob), wt.HANDLE(hproc)))
            if not meta["assign_ok"]:
                meta["assign_last_error"] = ct.get_last_error()
            inflag = wt.BOOL()
            self.k32.IsProcessInJob(wt.HANDLE(hproc), None, ct.byref(inflag))
            meta["child_in_job"] = bool(inflag.value)
        p.stdin.write(json.dumps(cmd) + "\n")
        p.stdin.flush()
        out, err = p.communicate(timeout=40)
        res = None
        for line in (out or "").splitlines():
            if line.startswith("JOCHILD:"):
                try:
                    res = json.loads(line[len("JOCHILD:"):])
                except ValueError:
                    res = None
        meta["stderr_tail"] = (err or "")[:300]
        if in_job and hjob:
            try:
                self.k32.TerminateJobObject(wt.HANDLE(hjob), 9)
            except Exception:
                pass
        for h in (hproc, hjob):
            if h:
                try:
                    self.k32.CloseHandle(wt.HANDLE(h))
                except Exception:
                    pass
        return res, meta


def observe_gaps(loopback, fwrite):
    """把子进程的原始观测翻成三态。抽成纯函数是为了**可注入测试**：
    尺子自己必须能被喂 `None` 验证，否则「没测到」被读成「已隔离」这类
    尺子做错只能等真出事才发现（本轮第一版就踩了这个坑）。

    三态而不是布尔：
      · NOT_ISOLATED —— 真连上 / 真写成功（Job 没挡住，是实机证据）
      · ISOLATED     —— 被挡住（若真发生，反而要复查是不是环境干扰）
      · UNTESTED     —— 子进程没吐结果 / 值为 None，**不算证据，也不许读成已隔离**
    返回 (network_observed, filesystem_observed, gaps_proven)。
    """
    net = ("NOT_ISOLATED" if loopback == "CONNECTED"
           else "ISOLATED" if str(loopback).startswith("BLOCKED")
           else "UNTESTED")
    fs = ("NOT_ISOLATED" if fwrite == "WROTE"
          else "ISOLATED" if str(fwrite).startswith("BLOCKED")
          else "UNTESTED")
    return net, fs, net == "NOT_ISOLATED" and fs == "NOT_ISOLATED"


def _empty(verdict, reason):
    return {
        "schema": SCHEMA, "probe": PROBE, "platform": sys.platform,
        "verdict": verdict, "reason": reason,
        "capability_impact": dict(CAPABILITY_IMPACT),
        "isolation_gaps": {"status": "not_tested"},
        "three_evidence": {s: {"status": "skipped", "reason": reason} for s in THREE},
    }


def probe():
    """实机探测，返回结构化 dict。任何环境都不抛异常到调用方。"""
    if sys.platform != "win32":
        return _empty("NOT_WINDOWS", "非 win32，原生原语不适用；os_sandbox 仍 absent")
    try:
        win = _Win()
    except Exception as exc:
        return _empty("PROBE_INCONCLUSIVE",
                      "Win32 加载失败：%s: %s" % (type(exc).__name__, exc))

    out = {
        "schema": SCHEMA, "probe": PROBE, "platform": sys.platform,
        "windows_version": str(sys.getwindowsversion())
        if hasattr(sys, "getwindowsversion") else "n/a",
        "api_available": win.api_available(),
        "appcontainer": win.appcontainer_exports(),
        "struct_sizes": win.struct_sizes,
        "capability_impact": dict(CAPABILITY_IMPACT),
    }
    tmp = tempfile.mkdtemp(prefix="wb_joprobe_")
    listener = None
    try:
        out["parent_in_job"] = win.parent_in_job()
        child_path = os.path.join(tmp, "child.py")
        with open(child_path, "w", encoding="utf-8") as fh:
            fh.write(CHILD)
        outside = os.path.join(tmp, "outside", "escape.txt")
        os.makedirs(os.path.dirname(outside), exist_ok=True)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]

        # 三证之一 real：负控 vs 实验的差值。
        ctrl, m_ctrl = win.run_child(child_path, {"mode": "spawn"}, False, None)
        job, m_job = win.run_child(child_path, {"mode": "spawn"}, True, 1)
        # 三证之二 injection：拿掉 ActiveProcessLimit，若 normal_spawn 恢复 OK，
        # 说明上面那个 BLOCKED 确实来自 limit，不是环境或 Job 本身的副作用。
        inj, m_inj = win.run_child(child_path, {"mode": "spawn"}, True, None)
        # 三证之三 bypass：job 内（无 active limit，避免它挡住 connect）真连
        # loopback + 真写越界文件。两者成功 = Job Object 不隔离网络/文件。
        iso, m_iso = win.run_child(
            child_path, {"mode": "iso", "port": port, "outside": outside}, True, None)
        try:
            listener.settimeout(1)
            conn, _ = listener.accept()
            conn.close()
        except Exception:
            pass

        ctrl = ctrl or {}
        job = job or {}
        inj = inj or {}
        iso = iso or {}
        control_ok = ctrl.get("normal_spawn") == "OK"
        job_blocked = str(job.get("normal_spawn", "")).startswith("BLOCKED")
        inj_ok = inj.get("normal_spawn") == "OK"
        differential_ok = bool(control_ok and job_blocked and inj_ok)

        out["process_containment"] = {
            "control_normal_spawn": ctrl.get("normal_spawn"),
            "job_normal_spawn": job.get("normal_spawn"),
            "injection_normal_spawn": inj.get("normal_spawn"),
            "control_breakaway_spawn": ctrl.get("breakaway_spawn"),
            "job_breakaway_spawn": job.get("breakaway_spawn"),
            # 负控的 breakaway 也 BLOCKED → 伪信号，明写出来，别让它冒充证据。
            "breakaway_is_pseudo_signal": bool(
                str(ctrl.get("breakaway_spawn", "")).startswith("BLOCKED")),
            "active_process_limit": 1,
            "differential_ok": differential_ok,
            "signal": ("负控 OK / job 内 BLOCKED(ActiveProcessLimit) / 拿掉 limit 后 OK"
                       " 三者齐，才认进程遏制成立；breakaway 单独不算证据。"),
            "meta": {"control": m_ctrl, "job": m_job, "injection": m_inj},
        }
        loopback = iso.get("loopback_connect")
        fwrite = iso.get("outside_write")
        # 三态，不用布尔。抽成 observe_gaps 纯函数，可被测试直接喂 None
        # 验证「没测到 ≠ 已隔离」（见该文件 docstring 与本轮两处尺子修正）。
        net_obs, fs_obs, gaps_proven = observe_gaps(loopback, fwrite)
        out["isolation_gaps"] = {
            "loopback_connect_in_job": loopback,
            "outside_write_in_job": fwrite,
            "network_isolation_observed": net_obs,
            "filesystem_isolation_observed": fs_obs,
            "gaps_proven": gaps_proven,
            "meta": m_iso,
            "note": ("在 Job 内真连 127.0.0.1 + 真写 worktree 之外文件。两者成功"
                     "不是缺陷，是**实机依据**：证明 Job Object 不能当网络/文件"
                     "隔离，因此不能把 os_sandbox 涂成 enforced。UNTESTED 时"
                     "不算证据，也不许读成『已隔离』。"),
        }
        out["three_evidence"] = {
            "real": {"ran": differential_ok, "control": ctrl.get("normal_spawn"),
                     "job": job.get("normal_spawn"),
                     "signal": "带负控的 ActiveProcessLimit 差值"},
            "injection": {"ran": inj_ok, "result": inj.get("normal_spawn"),
                          "signal": "拿掉 limit 后恢复 OK，自证因果不是环境"},
            "bypass": {"ran": bool(loopback and fwrite),
                       "loopback": loopback, "outside_write": fwrite,
                       "signal": "job 内网络/文件均未隔离 = 绕过成功 = 不能称沙箱"},
        }
        if not control_ok:
            out["verdict"] = "PROBE_INCONCLUSIVE"
        elif not differential_ok:
            out["verdict"] = "JOB_OBJECT_INEFFECTIVE"
        else:
            out["verdict"] = "JOB_OBJECT_PROCESS_CONTAINMENT_ONLY"
        out["reason"] = {
            "PROBE_INCONCLUSIVE": "负控都不成立，环境里没法做差值判定",
            "JOB_OBJECT_INEFFECTIVE": "差值不成立，进程遏制未证实",
            "JOB_OBJECT_PROCESS_CONTAINMENT_ONLY":
                "进程数/kill 遏制成立；网络与文件未隔离（见 isolation_gaps）",
        }.get(out["verdict"], "")
    except Exception as exc:
        out["verdict"] = "PROBE_INCONCLUSIVE"
        out["reason"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def selftest():
    """真跑一次 + 断言诚实边界。返回 0/1。"""
    r = probe()
    fails = []
    nchecks = [0]

    def ck(name, cond):
        nchecks[0] += 1
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            fails.append(name)

    ck("impact 不涂 enforced", r["capability_impact"]["promotes_to_enforced"] is False)
    ck("impact 标明不隔离网络", r["capability_impact"]["isolates_network"] is False)
    ck("impact 标明不隔离文件", r["capability_impact"]["isolates_filesystem"] is False)
    ck("impact os_sandbox 未变", r["capability_impact"]["os_sandbox_state"] == "unchanged")
    ck("三证键齐", all(k in r.get("three_evidence", {}) for k in THREE))
    if sys.platform == "win32" and r["verdict"] == "JOB_OBJECT_PROCESS_CONTAINMENT_ONLY":
        pc = r["process_containment"]
        ck("差值判据成立", pc["differential_ok"] is True)
        ck("负控 OK", pc["control_normal_spawn"] == "OK")
        ck("job 内被挡", str(pc["job_normal_spawn"]).startswith("BLOCKED"))
        ck("注入组恢复 OK", pc["injection_normal_spawn"] == "OK")
        ck("breakaway 标为伪信号", pc["breakaway_is_pseudo_signal"] is True)
        g = r["isolation_gaps"]
        ck("loopback 在 job 内连通（网络未隔离）", g["loopback_connect_in_job"] == "CONNECTED")
        ck("越界写在 job 内成功（文件未隔离）", g["outside_write_in_job"] == "WROTE")
        ck("网络观测=NOT_ISOLATED（不许读成已隔离）", g["network_isolation_observed"] == "NOT_ISOLATED")
        ck("文件观测=NOT_ISOLATED（不许读成已隔离）", g["filesystem_isolation_observed"] == "NOT_ISOLATED")
        ck("gaps_proven=True 才是不涂 enforced 的实机依据", g["gaps_proven"] is True)
    else:
        ck("非 win32/未定论时也不谎报隔离", r["capability_impact"]["promotes_to_enforced"] is False)
    # 计数据实：不手写常数。手写 14/6 曾在本仓多次 off-by-one。
    print("job_object_probe selftest: %s (%d checks, %d failed)" % (
        "PASS" if not fails else "FAIL", nchecks[0], len(fails)))
    return 1 if fails else 0


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _ast_default(tree, func_name, arg_name):
    """从函数定义里读默认参数值；读不到就返回可判定字符串，不猜。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            defaults = node.args.defaults
            names = [a.arg for a in node.args.args[-len(defaults):]] if defaults else []
            for n, d in zip(names, defaults):
                if n == arg_name:
                    try:
                        return ast.literal_eval(d)
                    except Exception:
                        return "UNRESOLVED"
    return "MISSING"


def _ast_call_kw(tree, func_def_name, call_name, kw_name):
    """在指定函数体内找某个调用的 keyword 值（用于验真实 worker 是否 opt-in）。"""
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == func_def_name):
            continue
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name != call_name:
                continue
            for kw in call.keywords:
                if kw.arg == kw_name:
                    try:
                        return ast.literal_eval(kw.value)
                    except Exception:
                        return "UNRESOLVED"
        return "MISSING"
    return "NO_FUNC"


def driver_wiring():
    """从源码与台账算出「第二十三轮 opt-in 接线」真值，不手抄布尔。

    为什么用 AST + 源码哈希：native_isolation.json 在 manifest COVER 内，
    若里面写一句 `wired: true` 而没人再核源码，那正是这个仓反复修的
    「声明悬空」——接线被改坏了，登记文件还会继续说已接。这里每次重新
    解析 platform_ops.py / agent_runner.py 的函数定义与调用参数，并带上
    源文件哈希；源码一变，稳定登记就变，必须重新写并重新签可信根。
    """
    paths = {
        "scripts/job_object_probe.py": os.path.join(HERE, "job_object_probe.py"),
        "scripts/platform_ops.py": os.path.join(HERE, "platform_ops.py"),
        "scripts/agent_runner.py": os.path.join(HERE, "agent_runner.py"),
        "scripts/worker_shim/sitecustomize.py":
            os.path.join(HERE, "worker_shim", "sitecustomize.py"),
        "capability_evidence/ledger.json":
            os.path.join(WB, "capability_evidence", "ledger.json"),
    }
    data = {rel: _read_bytes(p) for rel, p in paths.items()}
    text = {rel: b.decode("utf-8") for rel, b in data.items()}
    po_tree = ast.parse(text["scripts/platform_ops.py"],
                        filename=paths["scripts/platform_ops.py"])
    ar_tree = ast.parse(text["scripts/agent_runner.py"],
                        filename=paths["scripts/agent_runner.py"])

    default_contain = _ast_default(po_tree, "spawn_group", "contain")
    real_worker_contain = _ast_call_kw(ar_tree, "dispatch_job", "spawn_group", "contain")
    po_text = text["scripts/platform_ops.py"]
    shim_text = text["scripts/worker_shim/sitecustomize.py"]

    # 只认探针已实证的 ExtendedLimitInformation(9)，且生产里不设 ActiveProcessLimit。
    active_limit_assigned = (
        "info.BasicLimitInformation.ActiveProcessLimit" in po_text
        or "_JOB_OBJECT_LIMIT_ACTIVE_PROCESS" in po_text)
    kill_only = (
        "_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9" in po_text
        and "info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in po_text
        and not active_limit_assigned)
    off_marker = ('contain=False' in po_text and '"mode": "off"' in po_text)
    shim_no_kernel_job = "CreateJobObject" not in shim_text

    ledger = json.loads(text["capability_evidence/ledger.json"])
    caps = ledger.get("capabilities", {})
    ledger_participates = "native_isolation" in caps
    os_track = caps.get("os_sandbox", {}).get("track")

    wired = bool(
        default_contain is False
        and real_worker_contain is True
        and kill_only
        and active_limit_assigned is False
        and off_marker
        and shim_no_kernel_job
        and CAPABILITY_IMPACT["promotes_to_enforced"] is False
        and ledger_participates is False
        and os_track == "compensated_absent")
    return {
        "schema": 1,
        "computed_by": "ast + source_sha256 + ledger",
        "wired": wired,
        "default_contain": default_contain,
        "default_contain_false": default_contain is False,
        "real_worker_contain": real_worker_contain,
        "real_worker_contain_true": real_worker_contain is True,
        "kill_on_job_close_only": kill_only,
        "active_process_limit_in_production": active_limit_assigned,
        "off_marker_present": off_marker,
        "worker_shim_no_kernel_job": shim_no_kernel_job,
        "promotes_to_enforced": CAPABILITY_IMPACT["promotes_to_enforced"],
        "w4_ledger_participates": ledger_participates,
        "ledger_os_sandbox_track": os_track,
        "source_sha256": {
            rel: "sha256:" + hashlib.sha256(b).hexdigest()
            for rel, b in sorted(data.items())},
        "note": ("第二十三轮 opt-in 接线的源码真值。只接进程遏制 / kill-tree，"
                 "不把 os_sandbox 涂成 enforced；本字段仍不参与 W4 三证判定。"),
    }


def stable_registration(r):
    """从 probe() 结果里挑**可复现**的结论字段，组成静态登记存档。

    为什么不整份 dump：probe() 的 `meta`（assign_last_error / stderr_tail /
    每次 spawn 的 winerror 细节）会随环境轻微变化，加上 generated_at 时间戳，
    每次 --write 都产生一个新字节序列。而 capability_evidence/** 在 manifest
    COVER 内 —— 误跑一次 --write 就会制造一次「看起来像有人改了结论」的假
    drift。登记文件要的是「本机探测结论」这个稳定事实，不是每次运行的日志。
    """
    pc = r.get("process_containment", {})
    g = r.get("isolation_gaps", {})
    return {
        "schema": SCHEMA,
        "probe": PROBE,
        "platform": r["platform"],
        "windows_version": r.get("windows_version"),
        "verdict": r["verdict"],
        "reason": r.get("reason"),
        "capability_impact": dict(CAPABILITY_IMPACT),
        "appcontainer_verdict": r.get("appcontainer", {}).get("verdict"),
        "appcontainer_assembly_tested": r.get("appcontainer", {}).get("assembly_tested"),
        "process_containment": {
            "control_normal_spawn": pc.get("control_normal_spawn"),
            "job_normal_spawn": pc.get("job_normal_spawn"),
            "injection_normal_spawn": pc.get("injection_normal_spawn"),
            "differential_ok": pc.get("differential_ok"),
            "breakaway_is_pseudo_signal": pc.get("breakaway_is_pseudo_signal"),
            "signal": pc.get("signal"),
        },
        "isolation_gaps": {
            "loopback_connect_in_job": g.get("loopback_connect_in_job"),
            "outside_write_in_job": g.get("outside_write_in_job"),
            "network_isolation_observed": g.get("network_isolation_observed"),
            "filesystem_isolation_observed": g.get("filesystem_isolation_observed"),
            "gaps_proven": g.get("gaps_proven"),
            "note": g.get("note"),
        },
        "driver_wiring": driver_wiring(),
        "note": ("探针结论的静态登记。os_sandbox 保持 absent / "
                 "compensated_absent；本文件不参与 W4 三证判定，"
                 "capability_evidence/ledger.json 六项 effective 未改动。"
                 "driver_wiring 记录第二十三轮 opt-in 接线的源码真值，"
                 "仍不改变 W4 判定。"),
    }


def write_registration(r, force=False):
    """写登记，结论未变则保持文件（含 generated_at）原样 → 幂等。"""
    body = stable_registration(r)
    # 幂等判据：除 generated_at 外的稳定字段全同就不重写。
    if os.path.isfile(OUT_ABS) and not force:
        try:
            old = json.load(open(OUT_ABS, encoding="utf-8"))
            old.pop("generated_at", None)
            if old == body:
                print("UNCHANGED %s (verdict=%s，结论未变，不刷新时间戳)" % (
                    OUT_REL, r["verdict"]))
                return "unchanged"
        except (ValueError, OSError):
            pass
    os.makedirs(os.path.dirname(OUT_ABS), exist_ok=True)
    body["generated_at"] = datetime.now(timezone.utc).astimezone().isoformat()
    with open(OUT_ABS, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print("WROTE %s (verdict=%s)" % (OUT_REL, r["verdict"]))
    return "written"


def check_registration():
    """门禁：登记里的 driver_wiring 必须与当前源码真值逐字一致。

    只看接线真值，不跑 Win32 探测：环境差异（NOT_WINDOWS / 未定论）不该让
    「代码接线被改坏」这种真缺陷混过去。源码或台账一变，source_sha256 就变，
    这里返 1，提示重生成登记并重签可信根 —— 手抄 wired:true 的声明悬空到此为止。
    """
    if not os.path.isfile(OUT_ABS):
        print("FAIL: %s 不存在；先跑 --write --force 重生成登记" % OUT_REL)
        return 1
    try:
        old = json.load(open(OUT_ABS, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("FAIL: 登记不可读：%s" % exc)
        return 1
    want = driver_wiring()
    if old.get("driver_wiring") != want:
        print("FAIL: driver_wiring 与源码真值不一致（源码/台账改了，或登记被手改）")
        print("  先跑 `job_object_probe.py --write --force`，再重签 manifest")
        return 1
    print("PASS: driver_wiring 与源码真值一致 (wired=%s)" % want["wired"])
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if "--check" in argv:
        return check_registration()
    r = probe()
    if "--write" in argv:
        write_registration(r, force="--force" in argv)
    if "--json" in argv or not argv:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
