#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker_shim/sitecustomize.py — worker 进程级隔离（仅 WB_ISOLATE=1 生效）。

被 platform_ops.spawn_group 通过 PYTHONPATH 注入到 worker Python 进程。它给
network_deny 与 path_isolation 提供**运行时 enforcement**：不是只靠提交期
diff_sandbox_check / 启动期 scrub_env，而是 worker 真跑起来之后也拦得住常规
绕过。

诚实边界（登记进定稿附录 N）：这是 Python 进程级、非内核级。它拦得住
「直接 socket.connect 非回环」「open()/os.open() 写到 worktree 之外」
「reload(socket) 换回真实现」「import ctypes 去往下层」，**拦不住**
把闸门拆掉后重新 import 的极端手法、以及任何不经过本进程的路径（独立二进制、
已存在的 fd、内核级 socket）。os_sandbox 仍登记为 absent 并配补偿控制，
本 shim 不把 absent 涂成 enforced。

界面上区分两件事（这是 v2 相对 v1 的核心修正）：
  · **主闸门**（THREE 槽：real / injection / bypass）—— 决定能力是否够格
    `enforced` 的证据，只走常规路线。
  · **逃逸清单**（escapes）—— 逐条实测「哪些已知逃逸手法被挡、哪些没有」，
    写进 `capability_evidence/escapes/`，**不参与 enforced 判定**。
把已知拦截不了的手法从判定里藏起来，等于把「我挡住了常规路线」说成
「我挡住了全部」，那是本仓最反对的那种声明。

整段包在 try/except 里：shim 自身出错时 Python 仍启动（降级为无闸门），而
capability_probes 的绕过探针会如实报 BYPASS_OK → W4 保持 NOT_MET，绝不
「看起来通过了」。降级不是隐瞒：探针用的正是这条真实启动路径。
"""
import os as _os


def _install():
    if _os.environ.get("WB_ISOLATE") != "1":
        return

    # 允许的写根：worker 工作树 + WB_HOME（sentinel / brief / job 都在此）。
    roots = []
    for _k in ("WB_WORKTREE", "WB_HOME"):
        _v = _os.environ.get(_k)
        if _v:
            roots.append(_os.path.abspath(_v))
    if not roots:
        return  # 没给 scope 根就不装闸门，避免把整盘都误判越界

    def _in_scope(p):
        try:
            ap = _os.path.abspath(str(p))
        except Exception:
            return False
        for r in roots:
            if ap == r or ap.startswith(r + _os.sep):
                return True
        return False

    def _deny_net(address):
        raise PermissionError(
            "WB_ISOLATE network_deny: outbound connect denied %r" % (address,))

    def _deny_path(target):
        raise PermissionError(
            "WB_ISOLATE path_isolation: write outside scope denied %r" % (target,))

    # ---- 网络：非回环出站 connect 拒绝；本地回环放行（本地 webhook/测试）----
    import _socket as _csock
    import socket as _s

    def _loopback(addr):
        try:
            host = addr[0]
        except Exception:
            return True  # 非常规 address，不误杀
        h = str(host)
        return (h in ("127.0.0.1", "localhost", "::1", "")
                or h.startswith("127."))

    def _mk_guarded(base):
        class _Guarded(base):
            def connect(self, address):
                if not _loopback(address):
                    _deny_net(address)
                return base.connect(self, address)

            def connect_ex(self, address):
                if not _loopback(address):
                    _deny_net(address)
                return base.connect_ex(self, address)

            def sendto(self, data, *a):
                # UDP 没有 connect 也能发：`sendto(('198.51.100.9', 80))`
                if a and len(a[-1]) == 2 and not _loopback(a[-1]):
                    _deny_net(a[-1])
                return base.sendto(self, data, *a)

        _Guarded.__name__ = "GuardedSocket"
        _Guarded.__qualname__ = "GuardedSocket"
        return _Guarded

    _RealSocket = _s.socket
    _RealCSocket = _csock.socket
    _s.socket = _mk_guarded(_RealSocket)
    # 直接用 `_socket.socket()` 建出来的对象没有 socket.socket 那层壳，
    # v1 完全没覆盖 —— 这是逃逸清单里第一条实测通过的手法。
    _csock.socket = _mk_guarded(_RealCSocket)
    _s.SocketType = _s.socket
    _create_connection_real = _s.create_connection

    def _create_connection(address, *a, **kw):
        if not _loopback(address):
            _deny_net(address)
        return _create_connection_real(address, *a, **kw)

    _s.create_connection = _create_connection

    # ---- 写域：builtins.open / io.open / os.open ----
    import builtins as _bi
    import io as _io
    _bo, _ioo = _bi.open, _io.open

    def _is_write(mode):
        m = str(mode)
        return any(ch in m for ch in ("w", "a", "x", "+"))

    def _gopen(orig):
        def f(file, mode="r", *a, **kw):
            if _is_write(mode) and not _in_scope(file):
                _deny_path(file)
            return orig(file, mode, *a, **kw)
        return f

    _bi.open = _gopen(_bo)
    _io.open = _gopen(_ioo)

    # os.open 是绕过 builtins.open 的最短路径：`os.write(os.open(p, O_CREAT|O_WRONLY), b"x")`
    _real_os_open = _os.open
    _WFLAGS = _os.O_WRONLY | _os.O_RDWR | _os.O_CREAT | _os.O_APPEND | _os.O_TRUNC

    def _gos_open(path, flags, *a, **kw):
        if (flags & _WFLAGS) and not _in_scope(path):
            _deny_path(path)
        return _real_os_open(path, flags, *a, **kw)

    _os.open = _gos_open

    # ---- 拆闸门：reload(socket) 会把 socket.socket 换回真类 ----
    import importlib as _il
    _real_reload = _il.reload
    _LOCKED = {"socket", "_socket", "builtins", "io", "os", "sitecustomize"}

    def _greload(module):
        name = getattr(module, "__name__", str(module))
        if name in _LOCKED:
            raise PermissionError(
                "WB_ISOLATE: reload(%r) refused (would uninstall the isolation shim)" % name)
        return _real_reload(module)

    _il.reload = _greload

    # ---- 下层：ctypes 能直接调 libc / 拿 socket 句柄 ----
    # 我们拦不住它，所以**拒绝给它**。这不是"防住了"，是把入口关掉；
    # 逃逸清单会实测这条拒绝确实生效，也实测"从别的路径拿到 ctypes"仍然可能。
    _real_import = _bi.__import__

    def _gimport(name, *a, **kw):
        root = str(name).split(".")[0]
        if root in ("ctypes", "_ctypes"):
            raise PermissionError(
                "WB_ISOLATE: import %r refused (below-Python escape hatch)" % name)
        return _real_import(name, *a, **kw)

    _bi.__import__ = _gimport

    # ---- 子进程：worker 内部再起 Python 时，不许用「关掉 site」的参数 ----
    #
    # 这一条是实测抓出来的逃逸：父进程是被隔离的 worker，它 `subprocess.run(
    # [sys.executable, "-I", "-c", ...])` 时解释器忽略 PYTHONPATH，sitecustomize
    # 不加载 —— 闸门整段消失，子进程可以直接连外网、随便写盘。
    #
    # 拦在 Python 层只能拦「经过 Python 的 spawn」：subprocess.Popen、os.exec*、
    # os.posix_spawn。用编译好的别的解释器、或已在内存里的句柄，仍然绕过。
    # 所以这条**降低**了逃逸面，不消灭它 —— 逃逸清单里照实记。
    import sys as _sys
    _PY_HINTS = ("python", "py.exe", "pythonw")

    def _looks_like_python(exe):
        b = str(exe or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        return any(h in b for h in _PY_HINTS)

    def _bad_interp_flags(argv):
        if not argv:
            return []
        bad = []
        for a in list(argv)[1:]:
            t = str(a)
            if not t.startswith("-") or t.startswith("--"):
                continue
            # -I / -E / -S 单独出现，或合写（-IS / -EIS）都算
            if any(ch in t[1:] for ch in "IES"):
                bad.append(t)
        return bad

    import subprocess as _sp
    _real_popen_init = _sp.Popen.__init__

    def _gpopen_init(self, args, *a, **kw):
        exe = None
        argv = args
        if isinstance(args, (list, tuple)) and args:
            exe, argv = args[0], args
        elif isinstance(args, str):
            exe, argv = args, [args]
        if _looks_like_python(exe):
            bad = _bad_interp_flags(argv)
            if bad:
                raise PermissionError(
                    "WB_ISOLATE: python subprocess with %r refused "
                    "(would disable sitecustomize and drop the isolation shim)" % (bad,))
        return _real_popen_init(self, args, *a, **kw)

    _sp.Popen.__init__ = _gpopen_init

    _real_execv = _os.execv
    _real_execve = _os.execve

    def _gexecv(path, argv):
        if _looks_like_python(path) and _bad_interp_flags(argv):
            raise PermissionError("WB_ISOLATE: exec with %r refused" % (_bad_interp_flags(argv),))
        return _real_execv(path, argv)

    def _gexecve(path, argv, env):
        if _looks_like_python(path) and _bad_interp_flags(argv):
            raise PermissionError("WB_ISOLATE: execve with %r refused" % (_bad_interp_flags(argv),))
        return _real_execve(path, argv, env)

    _os.execv = _gexecv
    _os.execve = _gexecve
    if hasattr(_os, "posix_spawn"):
        _real_ps = _os.posix_spawn

        def _gps(path, argv, env, **kw):
            if _looks_like_python(path) and _bad_interp_flags(argv):
                raise PermissionError("WB_ISOLATE: posix_spawn with %r refused"
                                      % (_bad_interp_flags(argv),))
            return _real_ps(path, argv, env, **kw)

        _os.posix_spawn = _gps


try:
    _install()
except Exception:
    pass  # 降级：探针如实报 BYPASS_OK，W4 保持 NOT_MET
