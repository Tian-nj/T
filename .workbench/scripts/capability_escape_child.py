#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capability_escape_child.py — 逃逸清单的「被隔离 worker」子进程。

与 capability_probe_child.py 的分工：
  · probe_child 跑的是**主闸门**的三个槽（real / injection / bypass），
    它的结论决定能力够不够格 enforced。
  · 本文件跑的是**已知逃逸手法**的逐条实测，结论写进 escapes/，
    **不参与 enforced 判定**。

每条逃逸都用一个独立 mode，因为它们的失败方式不同：有的该被拦
（BLOCKED_GOOD），有的注定拦不住（REACHED → BLOCKED_BAD 的反面），
分不清就会把「拦不住」写成「没测」。

刻意不 catch：闸门生效时异常冒到 stderr、退出码非 0；闸门没装好时真的
做到那件事并打印 REACHED，外层如实记 —— 绝不把降级伪装成通过。
"""
import os
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else ""
OUT = os.environ.get("WB_OUTSIDE", "")
HOME_DIR = os.environ.get("WB_HOME", "")


def reach(what):
    print("REACHED", what)


def main():
    if mode == "socket_via__socket":
        # 绕开 socket.socket，用 CPython 内建 _socket.socket 直接建
        import _socket
        s = _socket.socket()
        s.settimeout(3)
        s.connect(("198.51.100.9", 80))
        reach("network via _socket.socket")
        s.close()

    elif mode == "socket_reload":
        # reload(socket) 把 socket.socket 换回未包装的真类
        import importlib
        import socket
        importlib.reload(socket)
        s = socket.socket()
        s.settimeout(3)
        s.connect(("198.51.100.9", 80))
        reach("network after reload(socket)")
        s.close()

    elif mode == "os_open_write":
        # os.open 绕过 builtins.open
        p = os.path.join(OUT, "via-os-open.txt")
        fd = os.open(p, os.O_CREAT | os.O_WRONLY)
        os.write(fd, b"1")
        os.close(fd)
        reach("file write via os.open")

    elif mode == "ctypes_write":
        # ctypes 直接调 libc/msvcrt 的 open
        import ctypes
        lib = ctypes.CDLL(None)
        reach("import ctypes succeeded (below-Python hatch reachable)")
        p = os.path.join(OUT, "via-ctypes.txt")
        try:
            lib.fopen(p.encode(), b"w")
        except Exception:
            pass

    elif mode == "pathlib_write":
        # pathlib 的 Path.write_text 最终也走 io.open
        import pathlib
        pathlib.Path(os.path.join(OUT, "via-pathlib.txt")).write_text("1")
        reach("file write via pathlib.Path.write_text")

    elif mode == "subprocess_child":
        # 起子进程：shim 靠 PYTHONPATH 注入，子进程会继承，但换个解释器参数就没了
        import subprocess
        r = subprocess.run([sys.executable, "-c", "print('CHILD_OK')"],
                           capture_output=True, timeout=30)
        print("SUBPROCESS_RC", r.returncode,
              (r.stdout or b"").strip()[:40].decode("utf-8", "replace"))
        reach("subprocess spawn (shim inherited or not — see RC)")

    elif mode == "subprocess_isolated_flag":
        # 父进程是**被隔离的** worker；它用 -I / -E / -S 起子进程时，PYTHONPATH
        # 被解释器忽略 → sitecustomize 不装 → 闸门整段消失。
        # 判定不看网络可达性（198.51.100.9 本来就连不上，连不上与"被挡住"在
        # 超时上长得一样），只看子进程里闸门在不在：这是可达性无关的硬指标。
        import subprocess
        probe = ("import socket;"
                 "print('CHILD_SHIM', getattr(socket.socket,'__name__',''))")
        out = []
        for flags in (["-I"], ["-E"], ["-S"]):
            r = subprocess.run([sys.executable] + flags + ["-c", probe],
                               capture_output=True, timeout=60)
            txt = (r.stdout or b"").decode("utf-8", "replace").strip()
            out.append("%s:%s" % (" ".join(flags), txt))
        joined = " | ".join(out)
        print("SUBPROC_FLAGS", joined)
        # 只要有一个子进程没装闸门，就是真逃逸
        if any("CHILD_SHIM GuardedSocket" not in x for x in out):
            reach("shim absent in subprocess started with -I/-E/-S")

    elif mode == "shim_present":
        # 自证：闸门是否真的装上了（这条永远是 BLOCKED_GOOD/否，只用来区分
        # 「逃逸被挡」和「闸门根本没装所以什么都通」）
        import socket
        print("SOCKET_IS", type(socket.socket).__name__)
        print("SHIM_MARKER", bool(getattr(socket.socket, "__name__", "") == "GuardedSocket"))

    else:
        raise SystemExit("unknown escape mode: %r" % mode)


main()
