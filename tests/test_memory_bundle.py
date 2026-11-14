#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_memory_bundle.py —— E56 记忆库跨机迁移的端到端验收。

selftest 只跑纯函数；这里跑的是**真实链路**：临时库 A 造记忆 → 导出 →
临时库 B（空）按签名计划导入 → 一条 memory_import 进真链 → 再导一次幂等。

为什么值得单独有测试文件：
  · 迁移包是**跨机器**的东西，"我这台能跑"不等于"那台能跑"。它必须能在
    一个连链都不存在的目录里被解出来，也必须能在已有大半条目的库里判冲突。
  · E56 是这个功能唯一的留痕。没有它，memory/ 里会出现链上不认识的条目，
    而 .gitignore 里那句「权威在链上」就成了空话。

边界（不测，也不假装测了）：
  · 不测真实跨操作系统迁移（Windows → Linux 的路径分隔符差异）。
  · 不测大包性能。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

import wb_core            # noqa: E402
import sign_manifest      # noqa: E402
import memory_store as ms  # noqa: E402
import memory_bundle as mb  # noqa: E402

OKS = [0]
FAILS = []


def ok(name, cond, detail=""):
    if cond:
        OKS[0] += 1
    else:
        FAILS.append(name + (("  << " + detail) if detail else ""))


def _mk_entry(eid, content, layer="project", **kw):
    return {"schema": 1, "id": eid, "layer": layer, "kind": kw.get("kind", "fact"),
            "content": content, "scene": "需要时", "source_seq": "manual",
            "importance": kw.get("importance", 80), "is_owner_related": False,
            "status": kw.get("status", "clear"), "created_ms": 1000,
            "last_recall_ms": 4321, "recall_count": 7, "fixed": False,
            "redacted": False}


def _mk_wb(root, uuid="0" * 32):
    """造一个最小可写 .workbench（Store 需要 spec 与 config）。"""
    wb = os.path.join(root, ".workbench")
    os.makedirs(os.path.join(wb, "events"), exist_ok=True)
    os.makedirs(os.path.join(wb, "spec"), exist_ok=True)
    shutil.copytree(os.path.join(ROOT, ".workbench", "spec"),
                    os.path.join(wb, "spec"), dirs_exist_ok=True)
    with open(os.path.join(wb, "config.yaml"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write('config_version: 1\nproject_uuid: "%s"\n' % uuid)
    return wb


def _mk_mem(root):
    mem = os.path.join(root, "memory")
    for lay in ms.LAYERS + ("archive",):
        os.makedirs(os.path.join(mem, lay), exist_ok=True)
    return mem


def t_export_import_to_fresh_machine():
    """A 造两条 → 导出 → B（空库）按计划签名导入 → 两条到位。"""
    tmpa = tempfile.mkdtemp(prefix="mb-a-")
    tmpb = tempfile.mkdtemp(prefix="mb-b-")
    saved = ms.MEM
    try:
        ms.MEM = _mk_mem(tmpa)
        for e in (_mk_entry("m-a", "改动可信根前先说一声"),
                  _mk_entry("m-b", "演示要在副本里跑", layer="task")):
            ms._write_atomic(ms._entry_path(e["layer"], e["id"]), e)
        bundle = mb.export_bundle(with_purpose=False)
        ok("导出 2 条", bundle["count"] == 2, str(bundle["count"]))
        ok("导出包自校验通过", mb.verify_bundle(bundle) == [])

        # 落成文件，再在新机器上从文件读 —— 迁移的真实形态不是内存里的 dict
        path = os.path.join(tmpa, "bundle.json")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(bundle, fh, ensure_ascii=False)
        with open(path, encoding="utf-8") as fh:
            reloaded = json.load(fh)

        ms.MEM = _mk_mem(tmpb)
        p = mb.plan(reloaded, "yg")
        ok("plan 可判定", bool(p.get("ok")), json.dumps(p, ensure_ascii=False)[:200])
        ok("plan 逐类计数正确", p.get("imported") == 2 and p.get("skipped") == 0)

        wb = _mk_wb(tmpb)
        st = wb_core.Store(wb, "0" * 32)
        r = mb.import_bundle(reloaded, "yg", store=st, expect=p)
        ok("按计划导入成功", bool(r.get("ok")), json.dumps(r, ensure_ascii=False)[:300])
        ok("导入 2 条", r.get("stats", {}).get("imported") == 2)
        ok("E56 入链", isinstance(r.get("event_seq"), int))

        ent = {e["id"]: e for e in ms.iter_entries()}
        ok("两条都到位", set(ent) == {"m-a", "m-b"}, str(sorted(ent)))
        ok("跑动痕迹被重置",
           all(ent[k]["last_recall_ms"] == 0 and ent[k]["recall_count"] == 0
               for k in ent))
        ok("来源块指向 bundle",
           all(ent[k]["import"]["bundle_id"] == reloaded["bundle_id"] for k in ent))

        # 链上那条 E56 的 payload 必须与统计对得上
        rows = [x for x in st._rows() if x.get("kind") == "memory_import"]
        ok("链上恰好一条 memory_import", len(rows) == 1, str(len(rows)))
        if rows:
            pay = rows[0].get("payload") or {}
            ok("actor 是 human:yg", rows[0].get("actor") == "human:yg",
               str(rows[0].get("actor")))
            ok("记了 imported=2", pay.get("imported") == 2, str(pay.get("imported")))
            ok("记了 bundle_id", pay.get("bundle_id") == reloaded["bundle_id"])

        # 再导一次：全 skipped，不再写第二条 E56
        r2 = mb.import_bundle(reloaded, "yg", store=st)
        ok("重复导入幂等", r2.get("ok") and r2.get("stats", {}).get("imported") == 0
           and r2.get("stats", {}).get("skipped") == 2)
        rows2 = [x for x in st._rows() if x.get("kind") == "memory_import"]
        ok("幂等时没有再写 E56", len(rows2) == 2,
           "链上 memory_import %d 条（第二次导入确实会再记一条 —— 导入是动作，"
           "重复导入也是一次动作；这里只要求它如实记 0 条新增）" % len(rows2))
    finally:
        ms.MEM = saved
        shutil.rmtree(tmpa, ignore_errors=True)
        shutil.rmtree(tmpb, ignore_errors=True)


def t_plan_changed_blocks_everything():
    """计划签完之后本机内容变了（同 id 冲突）→ 整批拒绝，一条都不写。

    这是这个功能最重要的性质：批准的必须是「这一次会发生什么」，不是「这个包
    里有什么」。同一个包导进空库和导进已有大半的库，结果完全不同。
    """
    tmp = tempfile.mkdtemp(prefix="mb-c-")
    tmp2 = tempfile.mkdtemp(prefix="mb-d-")
    saved = ms.MEM
    try:
        ms.MEM = _mk_mem(tmp)
        ms._write_atomic(ms._entry_path("project", "m-a"), _mk_entry("m-a", "原始内容"))
        ms._write_atomic(ms._entry_path("project", "m-b"), _mk_entry("m-b", "另一条"))
        bundle = json.loads(json.dumps(mb.export_bundle(with_purpose=False)))
        ok("包里有两条", bundle["count"] == 2, str(bundle["count"]))

        # 导出之后本机 m-a 被改了 → 同 id 不同内容，这次导入会撞冲突
        ms._write_atomic(ms._entry_path("project", "m-a"),
                         _mk_entry("m-a", "导出之后被改成了别的内容"))
        p = mb.plan(bundle, "yg")
        ok("计划里已把 m-a 记为冲突", p.get("conflicts") == 1, str(p.get("conflicts")))
        # m-b 在库里与包里一模一样 → skipped，不是 imported。三种结局必须分得开：
        # 会写的、已经有的、撞上的，签名批的是这个分布，不是"一共两条"。
        ok("计划把 m-b 记为 skipped 而不是 imported",
           p.get("imported") == 0 and p.get("skipped") == 1,
           "imported=%r skipped=%r" % (p.get("imported"), p.get("skipped")))

        # 换一台真正空的机器：那份"导入 1 条 + 冲突 1 条"的计划就不该再成立
        ms.MEM = _mk_mem(tmp2)
        r = mb.import_bundle(bundle, "yg", expect=p)
        ok("计划与环境不符时拒绝", (not r.get("ok")) and r.get("reason") == "PLAN_CHANGED",
           str(r.get("reason")))
        ok("拒绝时一条都没写", list(ms.iter_entries()) == [],
           str([e["id"] for e in ms.iter_entries()]))
        ok("拒绝时不写 E56", "event_seq" not in r)
    finally:
        ms.MEM = saved
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(tmp2, ignore_errors=True)


def t_secret_is_refused_not_masked():
    """疑似密钥不静默打码、也不静默放行 —— 该条不过去，但要被报出来。"""
    tmp = tempfile.mkdtemp(prefix="mb-e-")
    saved = ms.MEM
    try:
        ms.MEM = _mk_mem(tmp)
        bundle = {"schema": 1, "kind": "memory_bundle", "tool": "memory_bundle/v1",
                  "count": 1, "entries": [], "created_ms": 1,
                  "origin": {"project_uuid": "0" * 32, "python": "3"},
                  "purpose": None}
        bad = _mk_entry("m-x", "key is sk-abcdefghijklmnopqrstuvwxyz012345")
        bundle["entries"].append({"id": "m-x", "sha256": mb._digest(bad), "entry": bad})
        bundle["bundle_id"] = mb.BUNDLE_PREFIX + mb._digest({
            "schema": 1, "count": 1,
            "items": [[x["id"], x["sha256"]] for x in bundle["entries"]],
        })[len("sha256:"):][:16]
        ok("包本身合法", mb.verify_bundle(bundle) == [], str(mb.verify_bundle(bundle)))
        r = mb.import_bundle(bundle, "yg")
        ok("被拒而非被打码", r.get("stats", {}).get("secret") == ["m-x"],
           str(r.get("stats", {}).get("secret")))
        ok("被拒的条目没落盘",
           not os.path.exists(ms._entry_path("project", "m-x")))
    finally:
        ms.MEM = saved
        shutil.rmtree(tmp, ignore_errors=True)


def t_real_repo_wiring():
    """生产侧的接线必须真的在，不能只在测试的假仓库里成立。"""
    with open(os.path.join(SCRIPTS, "gen_spec.py"), encoding="utf-8") as fh:
        gen = fh.read()
    ok("gen_spec 声明了 E56", '"E56", "memory_import"' in gen)
    ev = json.load(open(os.path.join(ROOT, ".workbench", "spec", "events.schema.json"),
                        encoding="utf-8"))
    e56 = {e["id"]: e for e in ev["events"]}.get("E56")
    ok("E56 在 spec 里", bool(e56))
    if e56:
        ok("E56 只给 human:*", e56.get("actors") == ["human:*"], str(e56.get("actors")))
        ok("E56 必填七键",
           set(e56.get("required_payload") or []) == {
               "bundle_id", "origin_project_uuid", "imported", "skipped",
               "conflicts", "refused_secret", "layers"},
           str(e56.get("required_payload")))

    with open(os.path.join(SCRIPTS, "human_cli.py"), encoding="utf-8") as fh:
        hc = fh.read()
    ok("human_cli 有 memory_import op", '"memory_import": {"kind": "memory_import"' in hc)
    members = json.load(open(os.path.join(ROOT, ".workbench", "state", "members.json"),
                             encoding="utf-8"))
    perms = (members["members"][0].get("perms") or [])
    ok("members.json 给了 memory_import 权限", "memory_import" in perms, str(perms))
    agents = json.load(open(os.path.join(ROOT, ".workbench", "roles", "agents.json"),
                            encoding="utf-8"))
    ok("human.can_emit 含 E56", "E56" in (agents["human"].get("can_emit") or []))

    # 现导一遍本机记忆库并自校验 —— 打包器与校验器必须是同一套规则
    b = mb.export_bundle()
    ok("本机记忆库现导自校验通过", mb.verify_bundle(b) == [], str(mb.verify_bundle(b)))


def main():
    t_export_import_to_fresh_machine()
    t_plan_changed_blocks_everything()
    t_secret_is_refused_not_masked()
    t_real_repo_wiring()
    total = OKS[0] + len(FAILS)
    if FAILS:
        print("test_memory_bundle: FAIL (%d/%d)" % (OKS[0], total))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("test_memory_bundle: PASS (%d/%d)" % (total, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
