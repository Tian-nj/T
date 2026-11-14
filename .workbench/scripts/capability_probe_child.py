#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capability_probe_child.py — 绕过探针的「被隔离 worker」子进程。

capability_probes.py 的 network_deny / path_isolation 绕过槽不再在探针自己的
进程里直接建 socket / 写临时文件（那样永远 BYPASS_OK，等于没 enforcement），
而是通过 platform_ops.spawn_group 起这个子进程，并带 WB_ISOLATE=1，让
worker_shim/sitecustomize.py 真正安装闸门后，子进程再试绕过。

故意不 catch：闸门生效时异常冒到 stderr、退出码非 0；外层探针据此判定
「绕过被挡」。闸门没装好时就真的 CONNECTED / WROTE，外层如实报 BYPASS_OK，
W4 保持 NOT_MET —— 绝不把降级伪装成通过。

net 用 198.51.100.9（RFC 5737 文档保留地址）避免真实外联；被 shim 拦时
在 connect 前就 PermissionError，根本不进网络栈。
"""
import os
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else ""

if mode == "net":
    import socket
    s = socket.socket()
    s.settimeout(3)
    s.connect(("198.51.100.9", 80))
    print("CONNECTED")
    s.close()
elif mode == "path":
    p = os.path.join(os.environ["WB_OUTSIDE"], "outside.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("1")
    print("WROTE", p)
else:
    raise SystemExit("unknown probe mode: %r" % mode)
