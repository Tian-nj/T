# -*- coding: utf-8 -*-
"""D-48 自检：append_event 的 per-event actors 校验。"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓根
sys.path.insert(0, os.path.join(HERE, ".workbench", "scripts"))
import wb_core  # noqa: E402

import atexit
import shutil
import tempfile


def _rmtree(path):
    """删一棵树，只读文件也删。

    为什么不直接 `shutil.rmtree(path, True)`：Windows 上 `.git/objects/**`
    里是只读文件，`rmtree` 遇到只读项会**静默失败并留下整棵树**（实测改完
    单跑 test_revert_boundary.py 仍有 3 个目录各 19-20 项留下，只读文件
    全在 .git 下）。先删一次，删不掉就摘掉只读位再删。
    """
    shutil.rmtree(path, ignore_errors=True)
    if not os.path.exists(path):
        return
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                os.chmod(os.path.join(root, fn), 0o777)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _mkdtemp(prefix):
    """建临时目录，并在进程退出时删掉。

    为什么不用裸 `tempfile.mkdtemp(...)`：整轮回归由 `run_all_checks.sh` 的
    TEMP 沙箱兜底，但**单跑这一个文件时没有任何东西负责清理**——2026-11-15
    一天里 %TEMP% 攒下 264 个 `wb_*` 目录 / 17.5 MB，实测单跑
    `test_scheduler.py` 一次就新增 7 个。沙箱管「整轮」，管不到「随手跑一个」。
    """
    d = tempfile.mkdtemp(prefix=prefix)
    atexit.register(_rmtree, d)
    return d


# Store 读 spec 用相对路径，必须站在仓根
os.chdir(HERE)

PASS = []
FAIL = []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("ok  " if cond else "FAIL", name, ("  <" + detail + ">") if detail and not cond else ""))


def fresh():
    tmp = _mkdtemp(prefix="wb_act_")
    os.makedirs(os.path.join(tmp, "events"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    return tmp


tmp = fresh()
st = wb_core.Store(tmp, "0" * 32)

# ── 0. 先把任务造成一个真实存在的状态 ────────────────────────────────
# 第三十四轮给 51 个事件补了 from_states（E17 escalate 只允许从
# draft/pending/blocked/... 出发）。夹具若在「任务尚无状态」时直接写
# escalate，测的就不是 actor 校验，而是"绕过状态机" —— 用一条假数据
# 证明一句真话，与第三十三轮修 `from=None` 是同一个教训。
prime = st.append_event("state", {"task": "T-1", "from": None, "to": "draft"},
                        "lead", "T-1")
ok("夹具把任务推到 draft", prime["seq"] == 1 and st.current_state("T-1") == "draft",
   str(prime.get("seq")))

# ── 1. 合法 actor 必须放行 ────────────────────────────────────────────
e = st.append_event("escalate", {"task": "T-1", "reason_code": "ENV_BROKEN"},
                    "lead", "T-1")
ok("escalate by lead 放行", e["seq"] == 2, str(e.get("seq")))
e = st.append_event("escalate", {"task": "T-1", "reason_code": "ENV_BROKEN"},
                    "system:verifier", "T-1")
ok("escalate by system:verifier 放行", e["seq"] == 3, str(e.get("seq")))

# ── 2. 越权 actor 必须被拒 ────────────────────────────────────────────
try:
    st.append_event("escalate", {"task": "T-1", "reason_code": "X"}, "system:gc", "T-1")
    ok("escalate by system:gc 被拒", False, "被接受了")
except wb_core.SpecViolation as exc:
    ok("escalate by system:gc 被拒", "not in declared actors" in str(exc), str(exc))

try:
    st.append_event("log_repair", {"task": "T-1"}, "human:yg", "T-1")
    ok("log_repair by human:yg 被拒", False, "被接受了")
except wb_core.SpecViolation as exc:
    ok("log_repair by human:yg 被拒", "not in declared actors" in str(exc), str(exc))

# ── 3. 拒绝之后链没有多一行 ───────────────────────────────────────────
# 3 行合法写入：起始迁移 + 两次 escalate；两次越权各被拒，链上不该多出来。
rows = list(st._rows())
ok("被拒的写入不落行", len(rows) == 3, "lines=%d" % len(rows))

# ── 4. `human:*` 通配 ────────────────────────────────────────────────
# E27 constitution_change 声明 ["human:*"]。
# 注意 `_actor_ok` 的成员表是**仓内固定路径** `.workbench/state/members.json`
# （不是 tmp 里的那份），所以这里验的是仓库真实成员 yg —— 这也正是生产路径。
# 踩过的坑：曾试图 `wb_core._ACTOR_CACHE.clear()` 来"刷新"，那会抹掉 dict 的键，
# 下一次 `_actor_ok` 直接 `KeyError: 'stamp'`。缓存按 mtime 失效，不需要手动清。
try:
    e = st.append_event("constitution_change", {"file": "spec/enums.json", "digest": "sha256:x"},
                        "human:yg")
    ok("constitution_change by human:yg 放行（human:* 通配）", e["seq"] == 4, str(e.get("seq")))
except wb_core.SpecViolation as exc:
    ok("constitution_change by human:yg 放行（human:* 通配）", False, str(exc))

try:
    st.append_event("constitution_change", {"file": "spec/enums.json", "digest": "sha256:x"}, "lead")
    ok("constitution_change by lead 被拒（只声明 human:*）", False, "被接受了")
except wb_core.SpecViolation as exc:
    ok("constitution_change by lead 被拒（只声明 human:*）", "not in declared actors" in str(exc), str(exc))

# ── 5. `*authority*` 事件不走这条校验 ────────────────────────────────
decl = wb_core._actors_of("E08")
ok("E08 state 声明为 *authority*", decl == ["*authority*"], str(decl))
# 注意：state 事件走 append_event 时**不校验迁移合法性**（那是 check_transition
# 的职责，只有 Store.transition 会调）。这里只验"per-event actors 不拦截它"。
# 但 payload 的 from 必须与链上当前态一致 —— 第三十三轮加了锁内二次校验，
# 原先这里写的 `from="draft"` 在任务尚无状态时是一条 rebuild() 会判
# `ChainTampered` 的坏行（∅→draft 才是合法起始迁移）。夹具写坏行来证明
# "authority 不拦"，等于用一条假数据证明一句真话。
# 第三十四轮补：T-1 已在第 0 段被推到 draft，这里换一个新任务 T-2 做起始迁移，
# 否则 from=None 与链上 'draft' 不符，测的就变成并发校验了。
e = st.append_event("state", {"task": "T-2", "from": None, "to": "draft"},
                    "lead", "T-2")
ok("state by lead 放行（authority 管，actor 校验不拦）", e["seq"] == 5, str(e.get("seq")))

# ── 6. 退役事件的空 actors 不会被当成"谁都能写" ──────────────────────
try:
    st.append_event("verify_fail", {"task": "T-1"}, "lead", "T-1")
    ok("退役 E11 verify_fail 写不进", False, "被接受了")
except wb_core.SpecViolation as exc:
    ok("退役 E11 verify_fail 写不进", "no live id" in str(exc), str(exc))

# ── 7. _actors_of 的三种返回值 ───────────────────────────────────────
# 不硬编码 E17 的完整名单：那是 spec 的内容，不是本测试要证明的事实。
# 要证明的是"按 id 能取到一份非空声明，且里面的每一项都是真实注册的 actor"。
e17 = wb_core._actors_of("E17")
ok("_actors_of('E17') 返非空声明", bool(e17) and isinstance(e17, list), str(e17))
ok("E17 声明的每一项都已注册", all(wb_core._actor_ok(x) for x in e17), str(e17))
ok("E17 声明含 lead", "lead" in e17, str(e17))
ok("_actors_of('EZZZ') 返空表", wb_core._actors_of("EZZZ") == [], str(wb_core._actors_of("EZZZ")))

print("\nactors: PASS (%d/%d)" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("FAILED: %s" % (FAIL,))
    sys.exit(1)
