#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/_memiso.py —— 让测试不再写生产记忆的最小工具。

## 为什么需要它

`memory_store.recall()` 在返回结果前会**回写**被召回条目的
`last_recall_ms` / `recall_count` / 升一档 `status`（`memory_store.py:854-857`）。
也就是说：**「召回」这件事本身会改记忆**。这在生产里是对的（想起来一次就该记一次），
但它让任何「只是读一下人格/造一份 brief」的测试路径顺带改了真记忆。

第一百四十三轮之前有两处踩了这个坑：
  · `test_recall_injection.py` —— 直接对着真根调 `assemble_report(query=...)`
  · `test_boss_seat.py` 的 `_brief()` —— 换了 `ar.TASKS_DIR` 到临时目录，
    却没换记忆根，`write_brief → assemble_report → build_recall → recall` 照样写真的

两处都被新加的 `prod_drift_guard` 抓到（`M memory/project/... 503 -> 503`：
大小不变、内容变，正是最难发现的那种）。

## 为什么复制副本而不是指到空目录

这两个测试里有断言要求「召回真的有内容」（显忆段非空、内容行存在）。
把 `MEM` 指到空目录会让它们变红 —— **那不是修好，是把断言改弱**。
复制副本：行为照旧，写只落在副本上。

## 用法

    from _memiso import TempMemCopy
    with TempMemCopy():
        ...  # 这里面的 recall 只写副本

或手动：

    iso = TempMemCopy()
    try:
        ...
    finally:
        iso.restore()
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                ".workbench", "scripts"))

import memory_store as ms  # noqa: E402


def _rmtree(path):
    shutil.rmtree(path, ignore_errors=True)


class TempMemCopy:
    """把 `ms.MEM` 指到真记忆的一份临时副本，退出时还原。"""

    def __init__(self, tag="memiso"):
        self.dir = tempfile.mkdtemp(prefix="%s_" % tag)
        atexit.register(_rmtree, self.dir)
        src_root = ms.MEM
        if os.path.isdir(src_root):
            for name in os.listdir(src_root):
                src = os.path.join(src_root, name)
                dst = os.path.join(self.dir, name)
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
        self.saved = ms.MEM
        ms.MEM = self.dir

    def restore(self):
        ms.MEM = self.saved

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.restore()
        return False
