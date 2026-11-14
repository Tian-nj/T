# -*- coding: utf-8 -*-
"""D-54：worker 进程环境不得包含 WORKBENCH_ROOT_KEY 或任何密钥类变量。

定稿 §2.3.1（智能工作台-完整方案-v3.2（实现定稿）.md:128）原文：
    「密钥：WORKBENCH_ROOT_KEY 环境变量注入……仅 Lead 进程与 human_cli.py
      可见；worker 进程的环境不包含该变量（启动 worker 时显式从 env 里剔除）」

事故经过：agent_runner.dispatch_job 原为 `env = dict(os.environ)` 全量复制，
根密钥连同父进程的 CI/云凭据一起递给了执行体；platform_ops.spawn_group
又原样透传 env。而 gen_spec.py / enums.json 却仍声明「worker 不注入任何密钥
环境变量」——声明与实作正面冲突（声明悬空，且这次悬空的是安全前提）。

为什么要真起一个子进程来验，而不是只测函数返回值：
    scrub_env 返回 dict 正确，不等于「交给 Popen 的 env 就是那份 dict」。
    中间只要有一处 `env = dict(os.environ)` 或 env_allow 把值要回来，
    函数全绿而密钥照样漏。唯一能证伪的是从 worker 侧把环境变量读出来。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

import platform_ops as po  # noqa: E402

FAIL = []
N = [0]


def ck(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def t_name_patterns():
    """[1] 密钥类变量名必须命中，工作台自用变量不得被误杀。"""
    secrets = [
        "WORKBENCH_ROOT_KEY", "workbench_root_key",       # 点名 + 大小写
        "MY_SECRET", "DB_PASSWORD", "PASSWD",
        "AWS_PRIVATE_KEY", "STRIPE_API_KEY", "ACCESS_KEY",
        "AUTH_TOKEN", "GH_TOKEN",                          # _TOKEN 命中 GH_TOKEN
        "CREDENTIAL", "SESSION_KEY", "SIGNING_KEY",
    ]
    for k in secrets:
        ck("secret[%s]" % k, po.is_secret_env_name(k), "应判为密钥类")
    plain = ["PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "PYTHONIOENCODING",
             "WB_HOME", "WB_TASK", "WB_ROLE", "WB_SENTINEL", "WB_WORKTREE",
             "PORT", "TMP", "TEMP", "COMSPEC"]
    for k in plain:
        ck("plain[%s]" % k, not po.is_secret_env_name(k), "不该被剔")


def t_scrub_pure():
    """[2] scrub_env 剔除密钥、保留普通变量、且不改入参。"""
    env = {
        "PATH": "/usr/bin", "WB_TASK": "T-0008", "WB_HOME": "/wb",
        "WORKBENCH_ROOT_KEY": "root-canary", "MY_SECRET": "s-canary",
        "GH_TOKEN": "t-canary", "DB_PASSWORD": "p-canary",
    }
    before = dict(env)
    out = po.scrub_env(env)
    ck("out is new dict", out is not env)
    ck("input unmutated", env == before, "scrub_env 改了入参")
    ck("PATH kept", out.get("PATH") == "/usr/bin")
    ck("WB_TASK kept", out.get("WB_TASK") == "T-0008")
    ck("WB_HOME kept", out.get("WB_HOME") == "/wb")
    for k in ("WORKBENCH_ROOT_KEY", "MY_SECRET", "GH_TOKEN", "DB_PASSWORD"):
        ck("drop[%s]" % k, k not in out)
    ck("empty is empty", po.scrub_env({}) == {})


def t_real_worker_cannot_see_secrets():
    """[3] 真起 worker：从子进程视角把环境变量读回来，密钥必须不在。"""
    probe = (
        "import os,json;"
        "keys=['WORKBENCH_ROOT_KEY','MY_SECRET','GH_TOKEN','DB_PASSWORD',"
        "'WB_TASK','WB_HOME','PATH'];"
        "print('WBPROBE='+json.dumps({k:os.environ.get(k) for k in keys}))"
    )
    env = dict(os.environ)
    env["WORKBENCH_ROOT_KEY"] = "ROOT-CANARY"
    env["MY_SECRET"] = "SECRET-CANARY"
    env["GH_TOKEN"] = "TOKEN-CANARY"
    env["DB_PASSWORD"] = "PASS-CANARY"
    env["WB_TASK"] = "T-CANARY"
    env["WB_HOME"] = "/wb-canary"
    p = po.spawn_group([sys.executable, "-c", probe], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, _ = p.communicate(timeout=60)
    text = (out or b"").decode("utf-8", "replace")
    line = next((l for l in text.splitlines() if l.startswith("WBPROBE=")), None)
    ck("worker produced probe line", line is not None,
       "子进程没吐出探针：%r" % text[-300:])
    if line is None:
        return
    seen = json.loads(line[len("WBPROBE="):])
    for k in ("WORKBENCH_ROOT_KEY", "MY_SECRET", "GH_TOKEN", "DB_PASSWORD"):
        ck("worker-no[%s]" % k, seen.get(k) is None,
           "泄漏值 %r" % seen.get(k))
    ck("worker-kept[WB_TASK]", seen.get("WB_TASK") == "T-CANARY",
       "普通变量被误剔了")
    ck("worker-kept[WB_HOME]", seen.get("WB_HOME") == "/wb-canary")
    ck("worker-kept[PATH]", bool(seen.get("PATH")))


def t_default_env_is_scrubbed():
    """[4] 不传 env 时 spawn_group 也必须净化 os.environ（兜底那条）。"""
    probe = "import os;print('HASROOT='+str('WORKBENCH_ROOT_KEY' in os.environ))"
    # 只在当前进程临时塞一个 canary，验证 spawn_group 用的是 scrub(os.environ)
    # 而不是裸 os.environ。finally 还原，不污染父进程环境。
    old = os.environ.get("WORKBENCH_ROOT_KEY")
    os.environ["WORKBENCH_ROOT_KEY"] = "DEFAULT-CANARY"
    try:
        p = po.spawn_group([sys.executable, "-c", probe],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = p.communicate(timeout=60)
        text = (out or b"").decode("utf-8", "replace")
        ck("default-env probe", "HASROOT=False" in text,
           "env=None 时未走 scrub_env：%r" % text[-200:])
    finally:
        if old is None:
            os.environ.pop("WORKBENCH_ROOT_KEY", None)
        else:
            os.environ["WORKBENCH_ROOT_KEY"] = old


def t_network_env():
    """[6] E4b：正向代理变量不是密钥但必须一并剔除，NO_PROXY 不能误杀。

    定稿 §9.3「verify 环境无代理变量」此前只覆盖 verify_harness.base_env，
    worker 侧漏了 —— 演练实测注入 HTTP(S)/ALL_PROXY 后子进程里仍看得见。
    这里把「代理出口」与「凭据」分开判：代理不进 is_secret_env_name，
    NO_PROXY / no_proxy 是本地 webhook（E5）要用的白名单，误杀会断演练。
    """
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        ck("proxy[%s]" % k, po.is_network_env_name(k), "应判为代理出口")
        ck("proxy-not-secret[%s]" % k, not po.is_secret_env_name(k),
           "代理不是密钥，别揉进 is_secret_env_name")
    for k in ("NO_PROXY", "no_proxy", "PATH", "WB_HOME"):
        ck("no-proxy-kept[%s]" % k, not po.is_network_env_name(k),
           "NO_PROXY 是本地 webhook 白名单，不能被误杀")
    env = {
        "PATH": "/usr/bin", "WB_HOME": "/wb",
        "HTTP_PROXY": "http://evil.proxy:8080", "HTTPS_PROXY": "http://evil.proxy:8443",
        "ALL_PROXY": "socks5://evil.proxy:1080", "FTP_PROXY": "http://evil.proxy:2121",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    out = po.scrub_env(env)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY"):
        ck("scrub-drop[%s]" % k, k not in out)
    ck("scrub-kept[NO_PROXY]", out.get("NO_PROXY") == "127.0.0.1,localhost")
    ck("scrub-kept[PATH]", out.get("PATH") == "/usr/bin")
    # 真起子进程：函数返回对了不等于 Popen 的 env 就是那份字典。
    probe = (
        "import os,json;"
        "keys=['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','FTP_PROXY','NO_PROXY'];"
        "print('WBPROBE='+json.dumps({k:os.environ.get(k) for k in keys}))"
    )
    p = po.spawn_group([sys.executable, "-c", probe], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = (p.communicate(timeout=60)[0] or b"").decode("utf-8", "replace")
    line = next((l for l in text.splitlines() if l.startswith("WBPROBE=")), None)
    ck("proxy-worker produced probe", line is not None, "%r" % text[-200:])
    if line:
        seen = json.loads(line[len("WBPROBE="):])
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY"):
            ck("proxy-worker-no[%s]" % k, seen.get(k) is None,
               "泄漏值 %r" % seen.get(k))
        ck("proxy-worker-kept[NO_PROXY]",
           seen.get("NO_PROXY") == "127.0.0.1,localhost")


def t_source_contracts():
    """[5] 两个调用点都显式收口，验收文件的 env_allow 不是万能钥匙。"""
    runner = open(os.path.join(SCRIPTS, "agent_runner.py"), encoding="utf-8").read()
    ck("runner uses scrub_env",
       "platform_ops.scrub_env(os.environ)" in runner)
    ck("runner no raw dict(os.environ) in dispatch",
       "env = dict(os.environ)" not in runner)
    vh = open(os.path.join(SCRIPTS, "verify_harness.py"), encoding="utf-8").read()
    ck("verify_harness guards env_allow",
       "if is_secret_env_name(k):" in vh and "continue" in vh)
    ck("platform_ops spawn_group scrubs",
       "scrub_env(env if env is not None else os.environ)" in
       open(os.path.join(SCRIPTS, "platform_ops.py"), encoding="utf-8").read())


if __name__ == "__main__":
    for fn in (t_name_patterns, t_scrub_pure, t_real_worker_cannot_see_secrets,
               t_default_env_is_scrubbed, t_network_env, t_source_contracts):
        fn()
    print("worker_env: %s (%d/%d)" % (
        "PASS" if not FAIL else "FAIL", N[0] - len(FAIL), N[0]))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1 if FAIL else 0)
