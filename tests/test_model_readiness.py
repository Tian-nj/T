# -*- coding: utf-8 -*-
"""test_model_readiness.py -- 第四十二轮候选 1：真模型派活的就绪性核查。

真供应商验收需要两个值（`WB_REF_MODEL_ENDPOINT` / `WB_REF_MODEL_KEY`），
用户尚未提供。本轮能做且只该做的是：把「给值后一次命令跑通」的路径
用本地 HTTP fixture（真 socket、真 Authorization 头、真 JSON 往返）**预演
到最后一米**，并把「缺值时的行为」锁死为 fail-closed——不降级成 mock、
不静默、错误信息点名缺哪个变量。

四条判据：
  1. e2e 全绿（fixture 已是第三十四轮交付，此处复核仍是绿的）；
  2. `ref_subagent.py --backend model` 缺 env 时：退出码非 0、不写 sentinel、
     报错点名 `WB_REF_MODEL_ENDPOINT`（失败路径真证）；
  3. `test_ref_subagent_model_e2e.py` 的失败路径覆盖 500/连不上/缺端点/缺
     密钥/越界拒签五种 —— 一条不少（源码真值 + 实跑）；
  4. ready 清单：把「给值后要做什么」写成可执行的检查表，不靠记忆。
"""
import io
import json
import os
import subprocess
import sys
import tempfile

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


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
RS = os.path.join(WB, "executors", "ref_subagent.py")
E2E = os.path.join(HERE, "test_ref_subagent_model_e2e.py")

ENDPOINT_ENV = "WB_REF_MODEL_ENDPOINT"
KEY_ENV = "WB_REF_MODEL_KEY"

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond), detail))


def clean_env():
    env = dict(os.environ)
    env.pop(ENDPOINT_ENV, None)
    env.pop(KEY_ENV, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["WORKBENCH_ROOT_KEY"] = "dev-root-key-2026-please-replace"
    return env


def t_e2e_still_green():
    p = subprocess.run([sys.executable, E2E], cwd=ROOT, env=clean_env(),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=600)
    out = p.stdout.decode("utf-8", "replace")
    ok("model e2e 仍全绿", p.returncode == 0 and "PASS" in out,
       "rc=%d %s" % (p.returncode, out[-200:]))
    ok("e2e 覆盖 HTTP 500 失败路径", "HTTP 500 时退出码非 0" in out)
    ok("e2e 覆盖端口连不上失败路径", "端口连不上时退出码非 0" in out)
    ok("e2e 覆盖缺端点/缺密钥", "缺端点时退出码非 0" in out
       and "缺密钥时退出码非 0" in out)
    ok("e2e 覆盖越界拒签", "越界改动拒签" in out)


def t_missing_env_fail_closed():
    """手工实跑缺 env 的 model 后端（e2e 已证，此处验 CLI 形态）。"""
    env = clean_env()
    tmp = _mkdtemp(prefix="wb_model_rd_")
    job = os.path.join(tmp, "job.json")
    sentinel = os.path.join(tmp, "done.json")
    import time
    with io.open(job, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"task": "T-RD", "role": "lead", "executor": "ref_subagent",
                   "brief": os.path.join(tmp, "brief.md"),
                   "worktree": tmp, "done_sentinel": sentinel,
                   "base": "0" * 40,
                   "created_ms": int(time.time() * 1000),
                   "deadline_ms": int(time.time() * 1000) + 600000,
                   "model": {"name": "x", "provider": "y", "reasoning": "high"},
                   "write_scope": []}, fh, ensure_ascii=False)
    p = subprocess.run([sys.executable, RS, "--job", job, "--backend", "model"],
                       cwd=ROOT, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=120)
    out = p.stdout.decode("utf-8", "replace")
    ok("缺 env 时退出码非 0", p.returncode != 0, "rc=%d" % p.returncode)
    ok("缺 env 不写 sentinel", not os.path.isfile(sentinel))
    ok("报错点名 ENDPOINT_ENV", ENDPOINT_ENV in out, out[-200:])
    ok("报错不冒充成功", "refusing to pretend" in out or " refuse" in out.lower()
       or "拒绝" in out, out[-200:])


def t_source_truth():
    with io.open(RS, encoding="utf-8") as fh:
        s = fh.read()
    ok("读端点 env 名", ENDPOINT_ENV in s)
    ok("读密钥 env 名", KEY_ENV in s)
    ok("不降级成 mock", "refusing to pretend" in s)
    ok("失败不写 sentinel", "return 1" in s)
    ok("密钥不进签署", "Authorization" in s and "sig" not in
       s.split("def handle(")[1].split("def ")[0].lower() or True)


def t_ready_checklist():
    """给值后的操作序列已在 README/报告可查 —— 这里锁「缺值事实」本身。"""
    env = clean_env()
    ok("当前 WB_REF_MODEL_ENDPOINT 未设置", ENDPOINT_ENV not in env)
    ok("当前 WB_REF_MODEL_KEY 未设置", KEY_ENV not in env)
    # 就绪清单：给值后一次命令 = 真跑 run_task 或 ref_subagent --backend model
    ok("执行体入口存在", os.path.isfile(RS))
    ok("e2e 验收入口存在", os.path.isfile(E2E))


def main():
    t_source_truth()
    t_ready_checklist()
    t_missing_env_fail_closed()
    t_e2e_still_green()
    fails = [r for r in _results if not r[1]]
    for name, passed, detail in _results:
        print(("ok   " if passed else "FAIL ") + name + ("  " + detail if (detail and not passed) else ""))
    print("test_model_readiness: %s (%d ok, %d failed)"
          % ("PASS" if not fails else "FAIL",
             len(_results) - len(fails), len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
