#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""connector_absence.py —— 给 designed / unavailable 的连接器出「我没做」的证据。

为什么需要这个文件

    一张登记表里最容易混进来的东西，是「空着的槽位」。空槽位在 JSON 里
    看起来和「已填但内容为空」一模一样，而它的含义有三种完全不同的可能：

        代码还没写（designed）
        环境不具备（unavailable）
        忘了填（漏项）

    前两种是**结论**，第三种是**缺陷**。要让人一眼分开，就必须给前两种
    各留一份带成因的证据文件 —— 这就是 absence.json 的作用。

为什么 absence 也要有指纹

    「我没做」这句话同样可以被伪造：先标 designed 拿到豁免，然后再也不管它。
    所以 absence.json 里记三件事：**谁做的判断**（判据，不是结论）、
    **判据在哪一行**（可复核）、**指纹**（改过就发现）。

    这不是仪式。第二个月有人想把它改成 implemented 时，他得先删掉这份
    文件 —— 而删文件是个能被 `git diff` 看见的动作。

用法：
    python connector_absence.py            # 生成/刷新全部 absence 证据
    python connector_absence.py --check    # 只校验
退出码 0 = 一致；1 = 有不符
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
EVDIR = os.path.join(WB, "capability_evidence", "connectors")

FIELDS = ("connector", "state", "criterion", "evidence", "why_absent", "next_step")


def _sha(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def _canon(body):
    return json.dumps({k: body.get(k) for k in FIELDS},
                      ensure_ascii=False, sort_keys=True)


def _scan(needles):
    """数出「真 import 了某个第三方模块」的地方，用来证明「0 处使用」。

    这里刻意用 **AST 找 import 语句**，不是在源码里 grep 名字 —— 两者的
    差别在本文件第一次跑的时候就把我耍了一次：grep 
equests 命中了 7 个
    文件，逐个看下去全是 server_requests / self.requests / 测试里手写的
    
equests==2.31.0 文本。它们一个都没 import requests。

    「提到一个名字」与「依赖一个模块」是两件事。用 grep 当判据，会把
    「说明为什么不用它」的文档、和「测试桩里恰好用了这个词」的变量，
    统统算成「在用」—— 最后得出的结论是反的。

    也不扫本文件与 gen_spec.py：前者是判据本身，后者是登记表的生成器，
    它们提到这些名字是为了描述，不是为了使用。
    """
    skip = {os.path.abspath(__file__),
            os.path.join(WB, "scripts", "gen_spec.py")}
    hits = {}
    for base in (os.path.join(WB, "scripts"), os.path.join(WB, "executors"),
                 os.path.join(ROOT, "tests"), os.path.join(ROOT, "src")):
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "wt", "state")]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(root, fn)
                if os.path.abspath(p) in skip:
                    continue
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        body = fh.read()
                    tree = ast.parse(body)
                except (OSError, SyntaxError):
                    continue
                imported = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for a in node.names:
                            imported.add(a.name.split(".")[0])
                    elif isinstance(node, ast.ImportFrom):
                        if node.level == 0 and node.module:
                            imported.add(node.module.split(".")[0])
                for n in needles:
                    if n in imported:
                        hits.setdefault(n, []).append(
                            os.path.relpath(p, ROOT).replace(os.sep, "/"))
    return hits


ABSENCE = {
    "doc_export_office": {
        "state": "designed",
        "criterion": "grep 仓库执行面 + import 实测",
        "why_absent": (
            "导出 xlsx/docx/pptx 的三件库在本机**是有的**（DSH 运行时自带的 "
            "site-packages：openpyxl / python-docx / python-pptx，实测 import "
            "成功），但工作台代码里 0 处使用。所以这不是「环境不具备」，是"
            "**刻意不引依赖**：生成器、校验器、审计脚本全部只 import stdlib，"
            "可信根里没有一处外部代码。要多引一个依赖，得先说服为什么值得让"
            "可信根多一段别人写的代码。"),
        "next_step": (
            "若真要导出 Office 文件：在 executors/ 下新写一个导出器，把"
            "第三方 import 收在函数内部（不在模块顶层），并在 connectors 表里"
            "把它从 designed 改成 implemented、补三证。注意三证里的 real 必须"
            "真产出一个文件再用 zipfile 读回来校验结构，不能只看「没抛异常」。"),
        "needles": ["openpyxl", "docx", "pptx", "xlsxwriter", "pandas"],
    },
    "sqlite_local": {
        "state": "designed",
        "criterion": "grep 仓库执行面 + stdlib 可用性实测",
        "why_absent": (
            "方案 §阶段 5 的最后一步是「数据库/本地知识库」。本机 stdlib 自带 "
            "sqlite3（实测 3.53.1 可用），但工作台代码里 0 处使用 —— 目前的"
            "持久化是 `memory/**` 的 JSON 文件 + 主链 events（追加式 JSONL）。"
            "换成 SQLite 会引入一套新的真相来源，而现有 JSON + 链的组合已经"
            "满足「小规模、可 diff、可签字」三个要求。"),
        "next_step": (
            "若记忆量真的涨到 JSON 读不动：先在 memory_store 里加一个"
            " `--backend sqlite` 的等价实现并让两套后端跑同一份 selftest，"
            "再切默认值 —— 直接换掉会让历史记忆与链上追溯对不上。"),
        "needles": ["sqlite3"],
    },
    "message_im_push": {
        "state": "unavailable",
        "criterion": "grep 仓库执行面 + 环境探测",
        "why_absent": (
            "Slack / 飞书 / 企微推送需要该平台的凭据与端点，本机都没有（也没有"
            "租户）。通用的 http_post webhook 已经覆盖「能推一条 HTTP 通知」这个"
            "真实需求 —— 告警通道用的就是它。平台 SDK 那一层不做假实现：一个"
            "标成 implemented、实际从没连过的连接器，比一条标成 unavailable 的"
            "更危险，因为前者会让后续的人以为这条通道有人维护。"),
        "next_step": (
            "等真有租户时：把域名写进 allow_hosts、把令牌走 env、补三证"
            "（real 用该平台的测试工作区真发一条，bypass 用未白名单的域名验拒），"
            "再改 state。"),
        "needles": ["slack", "feishu", "lark", "smtplib", "requests"],
    },
}


def build(name):
    spec = ABSENCE[name]
    hits = _scan(spec["needles"])
    body = {
        "connector": name,
        "state": spec["state"],
        "criterion": spec["criterion"],
        "why_absent": spec["why_absent"],
        "next_step": spec["next_step"],
        # 这就是「判据在哪一行」：数出每个字面量命中几个文件、分别是哪几个。
        "evidence": {
            "needles": spec["needles"],
            "hits": {k: sorted(v) for k, v in sorted(hits.items())},
            "hit_files": sorted({p for v in hits.values() for p in v}),
        },
    }
    body["evidence_sha256"] = _sha(_canon(body))
    return body


def write_all():
    os.makedirs(EVDIR, exist_ok=True)
    for name in sorted(ABSENCE):
        body = build(name)
        d = os.path.join(EVDIR, name)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "absence.json")
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(body, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        hit_n = len(body["evidence"]["hit_files"])
        print("%-20s %-12s 命中文件 %d 个" % (name, body["state"], hit_n))
    return 0


def check():
    bad = []
    for name in sorted(ABSENCE):
        p = os.path.join(EVDIR, name, "absence.json")
        if not (os.path.isfile(p) and os.path.getsize(p) > 0):
            bad.append("%s/absence.json 缺失" % name)
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                body = json.load(fh)
        except (OSError, ValueError) as exc:
            bad.append("%s/absence.json 不可读: %s" % (name, exc))
            continue
        if body.get("evidence_sha256") != _sha(_canon(body)):
            bad.append("%s/absence.json 指纹不符（被改过？）" % name)
        if body.get("state") != ABSENCE[name]["state"]:
            bad.append("%s/absence.json 的 state 与脚本不一致" % name)
    for b in bad:
        print("  - %s" % b)
    print("\nRESULT: %s — %d 份 absence 证据%s"
          % ("PASS" if not bad else "FAIL", len(ABSENCE),
             "全部一致" if not bad else "，%d 项不符" % len(bad)))
    return 0 if not bad else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="designed/unavailable 连接器的 absence 证据")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    return check() if args.check else write_all()


if __name__ == "__main__":
    sys.exit(main())
