# -*- coding: utf-8 -*-
"""外部 AI 执行体 —— 让「本机装着的那些 AI 软件」成为工作台的 worker。

为什么需要它
    工作台原来只有三种 executor：local_process（确定性脚本）、
    harness_subagent（由 DSH 派子代理）、human_cli（人）。前两种都是
    「我们自己养的模型」，第三种是人。而用户装在本机上的 Kimi / WorkBuddy /
    豆包 / 千问是**别人的产品**，它们不在我们的进程树里，也没有对外开放的
    调度协议 —— 但它们各自擅长的事不一样（长文推理、表格编辑、即时问答、
    中文语境），把活按擅长分派出去，比强迫所有活都走同一条模型线更合理。

    所以这里做的不是「再包一个 LLM 客户端」，而是**给它们一个统一的
    worker 接口**：same job file in, same sentinel out。

诚实优先（这是本文件最重要的设计决定）
    「能控制某家 AI」这句话必须可证。本文件用三层把这句话钉住：

    1. `PROVIDERS` 里每一家都带 `available` 与 `evidence`。
       没有对外接口的那几家（WorkBuddy / 豆包 / 千问）**如实写成不可用**，
       并写清探测证据（端口扫遍 404 / 只有 Electron IPC / 目标是 node 运行时）。
       不写「理论上可以」——一个不可用的条目写成可用，等于把整张能力表
       变成一句无法核对的声明。
    2. `consult` 模式**绝不声称改过文件**：`changed` 恒为空。
       它只产出意见（findings / suggestions / plan），那本来就是这类
       「帮我看看」的活该产出东西。
    3. `apply` 模式才写盘，而且写之前逐个校验 write_scope；
       签署里 `by` 写的是本执行体、`provider` 写的是**真正出主意的那个 AI**。
       把「谁想了」和「谁写了」分开记，是因为这两件事出问题时排查方向不同。

两种模式
    consult  → 读 brief，问 AI 要意见。write_scope 可以为空。
    apply    → 读 brief，问 AI 要 {文件: 内容}，落在 write_scope 里，
               然后跑 selfcheck 命令，把真实退出码写进签署。

两种后端
    mock      —— 不需要网络与密钥。真跑 `job.model.mock_cmd` 并如实回退出码。
                 存在的理由：没有它，这个执行体只能在配了密钥的机器上被验证，
                 那等于大多数时候没有验证。
    external  —— 真调供应商的 OpenAI 兼容端点。**缺端点或密钥不降级**：
                 直接返回 ok=false 并点名缺哪个变量。

契约（与 agent_runner.py 的 job 一一对应，不增不减）
    输入   state/agent_jobs/<task>.<role>.json
           {task, role, executor, brief, worktree, done_sentinel,
            base, created_ms, deadline_ms, model, write_scope}
    输出   done_sentinel 指向的文件
           {"ok": true, "summary": "...", "changed": [...],
            "selfcheck": {"cmd": "...", "exit": 0}, "provider": "...", ...}

三条硬边界
    1. 不跑 git。合并归 merge_queue，不归执行体。
    2. 不写 write_scope 之外的文件。空 write_scope = 什么都不许写。
    3. 密钥永不进 sentinel、永不进日志。sentinel 会被收进证据目录。

用法
    python executors/ext_ai_subagent.py --list-providers
    python executors/ext_ai_subagent.py --job .workbench/state/agent_jobs/T-1.coder.json
    python executors/ext_ai_subagent.py --job ... --backend external --provider kimi
    python executors/ext_ai_subagent.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SCHEMA = "ext_ai_subagent_v1"
DEFAULT_TIMEOUT_SEC = 180
DEFAULT_PROVIDER = "kimi"

# 密钥永远从环境或本地凭据文件取，**绝不写死在这个文件里** ——
# 这个文件进 root.manifest、进 git，写死等于把钥匙钉在门上。
ENDPOINT_ENV = "WB_EXT_AI_ENDPOINT"
KEY_ENV = "WB_EXT_AI_KEY"
PROVIDER_ENV = "WB_EXT_AI_PROVIDER"
MODE_ENV = "WB_EXT_AI_MODE"

# Kimi 的凭据在它自己的配置里，我们不复制、只读。
KIMI_CONFIG = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "kimi-desktop",
    "daimon-share", "daimon", "config.json")

# 工作台自己的供应商凭据在 .git/info/ 下（git 不跟踪 .git/info），
# 与 joint_acceptance 用的是同一份。同一台机器上已有钥匙时，再让用户
# 复制一份到环境变量是没有意义的手工劳动。
# 注意 ROOT 是 .workbench（HERE 的父级），仓库根还要再上一层。
WB_ROOT = os.path.dirname(ROOT)
MODEL_CRED_FILE = os.path.join(WB_ROOT, ".git", "info", "model_credentials.json")

# ---------------------------------------------------------------- 能力表
# 这张表是**可核对的**：`available` 为 False 的条目必须给 `evidence`，
# 说明我们凭什么说它不可用。凭据不在这里，在运行时从环境/配置文件读。
PROVIDERS = {
    "kimi": {
        "available": True,
        "kind": "openai_compatible",
        "endpoint": "https://agent-gw.kimi.com/coding/v1/chat/completions",
        "model": "k3-agent",
        "models_seen": ["k3d1-agent", "k2d8-preview", "k3-agent"],
        "context_length": 1000000,
        "strengths": ["长文推理", "代码审读", "中文语境", "大上下文整段复核"],
        "key_sources": ["env:%s" % KEY_ENV, "file:%s" % KIMI_CONFIG],
        "evidence": "实测 POST /chat/completions 返回 200；GET /models 返回 3 个模型",
        "note": "Kimi 自家的 daimon runner 也走这条端点，我们只是复用它，不另开通道。",
    },
    "deepseek": {
        "available": True,
        "kind": "openai_compatible",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-flash",
        "strengths": ["实现", "脚本编写", "结构化输出"],
        "key_sources": ["env:%s" % KEY_ENV],
        "evidence": "工作台 joint_acceptance 的 model 格已用同一端点验通（real_vendor_verified）",
        "note": "端点与密钥默认从环境取；工作台自己的凭据文件在 .git/info/，不混用。",
    },
    "workbuddy": {
        "available": False,
        "kind": "none",
        "strengths": [],
        "evidence": (
            "HTTP 服务 :18488 存在（返回结构化 JSON），但 /health /docs /openapi.json "
            "/api /mcp /sse /v1/chat/completions 等 30 个路径全部 404；"
            "app.asar(172 MB) 里扫 /api/ 前缀只命中 6 条远端 URL，ipcMain.handle( 零命中。"
            "唯一可编程的是内嵌 editor-sdk MCP :39099（290 个表格/文档工具，无推理工具）。"),
        "note": "不是「拒绝接入」，是「没有对外的调度口」。要接只能走 UI 自动化，成本与稳定性另算。",
    },
    "doubao": {
        "available": False,
        "kind": "none",
        "strengths": [],
        "evidence": (
            ":49281 接受 TCP 但立即 RemoteDisconnected（Electron 内部通道）；"
            ":49853 对所有路径返回 404 空体；public_config.json 只有 text_picker（划词取词）。"),
        "note": "Electron 应用，无 HTTP/CLI 入口。",
    },
    "qianwen": {
        "available": False,
        "kind": "none",
        "strengths": [],
        "evidence": (
            "agent_host.exe --help 输出的是 node 自己的 usage（打包的 node 运行时，非 CLI）；"
            "目录只有 agent_node_launcher.exe 与 node_cli_compat_preload.cjs。"),
        "note": "它是千问自己的 agent 执行环境，不是给外部用的接口。",
    },
}


def providers_report():
    """把能力表渲染成「能核对的清单」，而不是一句「支持多家 AI」。"""
    out = []
    for name, p in sorted(PROVIDERS.items()):
        row = {"provider": name, "available": bool(p.get("available")),
               "kind": p.get("kind"), "strengths": list(p.get("strengths") or []),
               "evidence": p.get("evidence") or ""}
        if p.get("available"):
            row["endpoint"] = p.get("endpoint")
            row["model"] = p.get("model")
            row["key_present"] = bool(_provider_key(name))
        out.append(row)
    return out


# ---------------------------------------------------------------- 小工具
def _read_json(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return None


def _write_json_atomic(path, doc):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".extaisent_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _norm(p):
    return str(p).replace("\\", "/").lstrip("./")


def _rel_norm(p):
    """把相对路径整理成可比较的形态，并且**先折叠 ..**。

    只靠字符串前缀比较是不够的：`config.yaml/../evil.py` 的前缀是
    `config.yaml/`，写域 `config.yaml` 会放行它，而 os.path.join 之后
    它实际落在 `evil.py` —— 一个"名字看起来在自己范围内"的穿越洞。
    所以比较之前必须先把 `..` 折叠掉，折叠后仍想跑出仓库的一律拒绝。
    """
    import posixpath
    raw = str(p).replace("\\", "/").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        return "\x00"            # 绝对路径：不可能落在写域里
    norm = posixpath.normpath(raw.lstrip("./"))
    if norm in (".", ""):
        return ""
    if norm.startswith("../") or norm == "..":
        return "\x00"            # 想跑出仓库：同样不可能落在写域里
    return norm


def scope_allows(write_scope, rel_path):
    """写域是前缀列表。空列表 = 什么都不许写（不是"随便写"）。

    这一条容易写反：空列表看着像"没有限制"，实际是"没有授权"。
    外部 AI 执行体里必须取后者 —— 否则它就成了绕过写域的最短路径。

    前缀必须**按路径段对齐**，不能只比字符。第一版写成
    `rel.startswith(pre)`，于是写域 `src` 会放行 `srcx/a.py`、
    写域 `docs/architecture` 会放行 `docs/architecture-evil/x` ——
    一个字面看起来"更宽松"的实现，实际是把写域变成了建议值。
    （这个缺陷是拿 Kimi 跑 consult 复核本文件时它指出来的。）

    三种写法都支持：
        "src/"               目录前缀，按段对齐
        "docs/architecture/**"  glob 目录，等价于"docs/architecture/"
        "config.yaml"        精确文件
    """
    rel = _rel_norm(rel_path)
    if not rel:
        return False
    for s in (write_scope or []):
        pre = _rel_norm(s)
        if not pre or pre == "\x00":
            continue
        if pre.endswith("/**"):
            pre = pre[:-2]               # "docs/x/**" -> "docs/x/"：保留尾斜杠
        if pre.endswith("/"):
            if rel.startswith(pre):
                return True
            continue
        if rel == pre or rel.startswith(pre + "/"):
            return True
    return False


def _provider_key(name):
    """按 key_sources 顺序取密钥。取不到返回空串，**不抛异常** ——
    调用方要根据"有没有密钥"给出不同的结论。"""
    env_key = (os.environ.get(KEY_ENV) or "").strip()
    if env_key:
        return env_key
    if name == "kimi":
        doc = _read_json(KIMI_CONFIG)
        if isinstance(doc, dict):
            cred = ((doc.get("credentials") or {}).get("kimiCode") or {})
            val = (cred.get("apiKey") or "").strip()
            if val:
                return val
    if name == "deepseek":
        # 工作台自己已经有一份（joint_acceptance 用的那份）。同一个仓库、
        # 同一台机器，没必要让用户再 export 一次。
        doc = _read_json(MODEL_CRED_FILE)
        if isinstance(doc, dict) and not doc.get("_placeholder"):
            val = (doc.get("key") or "").strip()
            if val:
                return val
    return ""


def _provider_endpoint(name):
    env_ep = (os.environ.get(ENDPOINT_ENV) or "").strip()
    if env_ep:
        return env_ep
    return (PROVIDERS.get(name) or {}).get("endpoint") or ""


def _provider_model(name, job=None):
    m = ((job or {}).get("model") or {})
    if isinstance(m, dict) and m.get("name") and m.get("provider") == name:
        return m["name"]
    return (PROVIDERS.get(name) or {}).get("model") or ""


def select_provider(job=None, want=None):
    """挑一个 provider。顺序：显式指定 → job.model.provider → 环境 → 默认。

    返回 (name, reason)。挑不到可用的一律给出原因，不默默换成别的。
    """
    for cand in (want,
                 (os.environ.get(PROVIDER_ENV) or "").strip() or None,
                 ((job or {}).get("model") or {}).get("provider")
                 if isinstance((job or {}).get("model"), dict) else None,
                 DEFAULT_PROVIDER):
        if not cand:
            continue
        p = PROVIDERS.get(cand)
        if not p:
            return None, "unknown provider %r（不在能力表里）" % (cand,)
        if not p.get("available"):
            return None, ("provider %r 不可用：%s" % (cand, p.get("evidence") or "无证据"))
        if not p.get("endpoint"):
            return None, "provider %r 没有端点" % (cand,)
        return cand, "ok"
    return None, "没有可用的 provider"


# ---------------------------------------------------------------- 后端
def run_mock(job):
    """真跑一条命令，如实回报退出码。

    没有它，这个执行体只能在配了密钥的机器上被验证 —— 那等于大多数时候没验证。
    """
    cmd = ((job.get("model") or {}).get("mock_cmd") or "").strip()
    if not cmd:
        return {"ok": True, "summary": "外部 AI 执行体 mock 回路：无待办", "changed": [],
                "selfcheck": {"cmd": "(noop)", "exit": 0}}
    t0 = time.time()
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=DEFAULT_TIMEOUT_SEC, cwd=ROOT)
        rc = p.returncode
        tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-1:] or [""]
    except subprocess.TimeoutExpired:
        rc = 124
        tail = ["timeout after %ds" % DEFAULT_TIMEOUT_SEC]
    except OSError as exc:
        rc = 127
        tail = ["%s: %s" % (type(exc).__name__, exc)]
    return {"ok": rc == 0,
            "summary": "外部 AI 执行体 mock 回路：`%s` → rc=%d（%.1fs）"
                       % (cmd, rc, time.time() - t0),
            "changed": [],
            "selfcheck": {"cmd": cmd, "exit": rc, "tail": tail[0][:200]}}


def _brief_text(job):
    p = job.get("brief") or ""
    if p and os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""
    return ""


def _system_prompt(mode):
    """给外部 AI 的指令。两条模式要求不同的输出形态，因为它们的
    产物被下游消费的方式完全不同：一个进复核清单，一个进水落盘。"""
    base = ("你是一个构建工作台里的 worker。你不能运行 git 命令，也不要建议运行。"
            "回答必须是**一个 JSON 对象**，不要包代码块、不要额外散文。")
    if mode == "apply":
        return base + (
            ' 输出格式：{"files": {"<相对路径>": "<完整文件内容>"}, '
            '"summary": "<一句话>", "selfcheck_cmd": "<可选，验证命令>"}。'
            "只输出你确实要写的文件；不确定就不要写。")
    return base + (
        ' 输出格式：{"verdict": "pass|rework", '
        '"findings": [{"severity": "high|mid|low", "where": "<文件或章节>", '
        '"what": "<问题>"}], "suggestions": ["<可直接照做的修改建议>"], '
        '"summary": "<一句话结论>"}。'
        "找不到问题就把 verdict 写 pass、findings 留空 —— 不要为了显得有用而编问题。")


def _parse_ai_json(text):
    """从模型回复里取出 JSON。模型的常见失手是包一层 ```json 或者前后带话，
    所以先试整体解析，再退化为「第一个 { 到最后一个 }」。"""
    t = (text or "").strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except ValueError:
            return None
    return None


def _authorize_outbound(connector, target):
    """出站闸门（阶段 5）。返回 (allowed, reason)。

    为什么执行体要自己问一遍：登记表写在 spec 里，但 spec 不是运行时。
    一个供应商的端点可能来自 `WB_EXT_AI_ENDPOINT`（env），那是**运行期**
    才确定的值 —— 光看 spec 谁也不知道这次要连哪。所以判据必须在发请求
    之前现问一次。

    为什么闸门模块 import 失败要 fail-closed：拿不到判据时，「放行」和
    「没判」在日志里长得一样，而前者的代价是一条数据真的出去了。

    为什么回环放行：本仓所有自证探针都用本地桩（`test_ref_subagent_model_e2e`
    与 capability_probes 都是 127.0.0.1），要求回环也进白名单，等于让
    「证明闸门有效」这件事本身过不了闸门。
    """
    try:
        scripts = os.path.join(ROOT, "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import connector_registry
        allowed, why, _info = connector_registry.authorize(connector, target)
        return allowed, why
    except Exception as exc:                       # noqa: BLE001
        return False, ("connector_registry 不可用（%s: %s）；出站动作按 fail-closed 拒绝"
                       % (type(exc).__name__, exc))


def call_provider(name, mode, job, timeout=DEFAULT_TIMEOUT_SEC):
    """调供应商的 OpenAI 兼容端点。缺配置直接失败，不降级。

    为什么不降级成 mock：那会让调用方以为自己验证了模型回路，实际验证的是 mock。
    """
    endpoint = _provider_endpoint(name)
    key = _provider_key(name)
    if not endpoint:
        return {"ok": False, "error": "%s 未设置且能力表里没有 %r 的端点；"
                                     "拒绝假装模型跑过" % (ENDPOINT_ENV, name)}
    if not key:
        return {"ok": False, "error": "%r 没有可用密钥（试过 env:%s 与它自己的配置）；"
                                     "拒绝假装模型跑过" % (name, KEY_ENV)}

    # 先过闸门再动手：连 endpoint 都没问过就发请求，等于闸门写在事后。
    allowed, why = _authorize_outbound("ext_ai_call", endpoint)
    if not allowed:
        return {"ok": False, "error": "出站被拦：%s" % why, "denied_by": "connector_registry"}

    brief = _brief_text(job) or ("task %s / role %s"
                                 % (job.get("task"), job.get("role")))
    # 刻意**不传 temperature**：Kimi 的网关对 k3-agent 只接受 1，
    # 传 0 会得到 400 invalid temperature。而 1 又不是我们要的语义
    # （我们要的是"别发散"），所以干脆不表达这个偏好 —— 由供应商默认。
    # 一条能跑的错误配置，比一条跑不起来的"正确"配置更有价值。
    body = json.dumps({
        "model": _provider_model(name, job),
        "messages": [
            {"role": "system", "content": _system_prompt(mode)},
            {"role": "user", "content": brief},
        ],
    }).encode("utf-8")

    try:
        import urllib.request
        req = urllib.request.Request(endpoint, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + key)
        # Kimi 的网关会看 UA 决定走哪条通道；不带它容易被当成匿名脚本。
        req.add_header("User-Agent", "Desktop Kimi Work")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as exc:                       # noqa: BLE001
        # 只报类型与消息，绝不把请求头（含密钥）带进日志或签署。
        return {"ok": False, "error": "调用失败：%s: %s"
                                     % (type(exc).__name__, str(exc)[:200])}

    text = ""
    try:
        doc = json.loads(raw)
        choices = doc.get("choices") or []
        if choices:
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
    except ValueError:
        text = raw.strip()[:2000]

    if not text:
        return {"ok": False, "error": "provider 返回空内容"}
    return {"ok": True, "text": text, "provider": name}


def run_external(job, mode, want=None):
    """consult / apply 两种模式共用一条取数与校验链。"""
    name, why = select_provider(job, want)
    if not name:
        return {"ok": False, "error": why, "unavailable": True}

    r = call_provider(name, mode, job)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "provider 调用失败"}

    doc = _parse_ai_json(r["text"])
    if not isinstance(doc, dict):
        return {"ok": False,
                "error": "provider 的回复不是 JSON（无法当签署或改动消费）",
                "provider": name, "model_output": r["text"][:2000]}

    if mode != "apply":
        # consult：只产出意见。**changed 恒为空** —— 它没写盘，就不能说改了文件。
        findings = doc.get("findings") or []
        sugg = doc.get("suggestions") or []
        verdict = (doc.get("verdict") or "").strip().lower() or "unknown"
        return {"ok": True, "provider": name,
                "summary": doc.get("summary") or ("%s 复核：%s"
                                                  % (name, verdict)),
                "changed": [],
                "selfcheck": {"cmd": "(consult)", "exit": 0},
                "verdict": verdict,
                "findings": findings if isinstance(findings, list) else [],
                "suggestions": sugg if isinstance(sugg, list) else [],
                "model_output": r["text"][:4000]}

    files = doc.get("files")
    if not isinstance(files, dict) or not files:
        return {"ok": False, "provider": name,
                "error": "apply 模式要求 provider 返回非空 files 映射"}

    ws = job.get("write_scope") or []
    bad = [p for p in files.keys() if not scope_allows(ws, p)]
    if bad:
        return {"ok": False, "provider": name,
                "error": "refusing to sign: files outside write_scope: %r" % (bad,)}

    written = []
    try:
        for rel, content in files.items():
            dest = os.path.join(job.get("worktree") or ROOT, _norm(rel))
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content if isinstance(content, str) else str(content))
            written.append(_norm(rel))
    except OSError as exc:
        return {"ok": False, "provider": name,
                "error": "写盘失败（已写 %r）：%s: %s"
                         % (written, type(exc).__name__, exc)}

    # 占位串**不能**借用 states.json 那个「任务尚无状态」哨兵值：全仓只许
    # gen_spec.py 承载它一次（test_state_sentinel 会扫所有 .py）。
    # 借用别人的哨兵当自己的占位，会把那条「只有一个真相源」的判据撞红，
    # 而它红得完全正确 —— 你确实又多抄了一份那个字符串。
    # 这条注释自己也不能把它原样写出来，否则扫到的就是注释。
    selfcheck = {"cmd": "(no selfcheck)", "exit": 0}
    cmd = (doc.get("selfcheck_cmd")
           or ((job.get("model") or {}).get("selfcheck_cmd") or "")).strip()
    if cmd:
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=DEFAULT_TIMEOUT_SEC,
                               cwd=job.get("worktree") or ROOT)
            tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-1:] or [""]
            selfcheck = {"cmd": cmd, "exit": p.returncode, "tail": tail[0][:200]}
        except subprocess.TimeoutExpired:
            selfcheck = {"cmd": cmd, "exit": 124, "tail": "timeout"}
        except OSError as exc:
            selfcheck = {"cmd": cmd, "exit": 127,
                         "tail": "%s: %s" % (type(exc).__name__, exc)}

    ok = selfcheck["exit"] == 0
    return {"ok": ok, "provider": name,
            "summary": doc.get("summary") or ("%s 写了 %d 个文件" % (name, len(written))),
            "changed": written, "selfcheck": selfcheck,
            "model_output": r["text"][:4000],
            "error": "" if ok else "selfcheck exit=%d" % selfcheck["exit"]}


# ---------------------------------------------------------------- 主流程
def handle(job_path, backend="mock", provider=None, mode=None, now_ms=None):
    """读 job → 干活 → 写签署。返回 (exit_code, sentinel_or_error)。

    退出码语义与 ref_subagent.py 完全一致（同一套契约，不同的大脑），
    只多一个 6：provider 能力表里声明为不可用。
    """
    job = _read_json(job_path)
    if not job:
        return 2, {"ok": False, "error": "job file missing or unparsable: %s" % job_path}

    missing = [k for k in ("task", "role", "done_sentinel") if not job.get(k)]
    if missing:
        return 2, {"ok": False, "error": "job is missing %r" % missing}

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    deadline = job.get("deadline_ms")
    if isinstance(deadline, int) and now > deadline:
        # 过期的活不干：干完也没人等了，签一个"我干完了"只会污染证据。
        return 3, {"ok": False,
                   "error": "job expired %dms ago; refusing to sign"
                            % (now - deadline)}

    use_mode = (mode or (os.environ.get(MODE_ENV) or "").strip() or "consult")
    if use_mode not in ("consult", "apply"):
        return 2, {"ok": False, "error": "unknown mode %r" % (use_mode,)}

    if backend == "external":
        out = run_external(job, use_mode, want=provider)
    else:
        out = run_mock(job)

    if out.get("unavailable"):
        return 6, out
    if not out.get("ok"):
        # 失败也留痕，但**不写 sentinel** —— sentinel 的语义是「我完成了」。
        # 用一份 ok=false 的签署表达失败，会让控制面把「执行体放弃」读成
        # 「执行体交了一份失败的签署」，两种情况的处理不一样。
        return 1, out

    bad = [c for c in (out.get("changed") or [])
           if not scope_allows(job.get("write_scope"), c)]
    if bad:
        return 4, {"ok": False,
                   "error": "refusing to sign: changed files outside write_scope: %r"
                            % (bad,)}

    sentinel = {
        "ok": True,
        "summary": out.get("summary") or "",
        "changed": [_norm(c) for c in (out.get("changed") or [])],
        "selfcheck": out.get("selfcheck") or {"cmd": "", "exit": 0},
        "by": "executors/ext_ai_subagent.py",
        "schema": SCHEMA,
        "backend": backend,
        "mode": use_mode,
        "provider": out.get("provider") or (provider or ""),
        "task": job.get("task"),
        "role": job.get("role"),
        "signed_ms": now,
    }
    for k in ("verdict", "findings", "suggestions", "model_output"):
        if out.get(k):
            sentinel[k] = out[k]
    _write_json_atomic(job["done_sentinel"], sentinel)
    return 0, sentinel


# ---------------------------------------------------------------- 自检
def selftest():
    """在临时目录里跑完整的 mock 与 consult 回路，并验证各条边界。"""
    ok = []

    def ck(name, cond, detail=None):
        ok.append((name, bool(cond)))
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           "" if cond or detail is None else "  <%r>" % (detail,)))

    tmp = tempfile.mkdtemp(prefix="extai_")
    try:
        # 1. 正常的 apply 回路（mock 后端，不碰网络）
        jp = os.path.join(tmp, "T-1.coder.json")
        sent = os.path.join(tmp, "done", "T-1.coder.json")
        _write_json_atomic(jp, {
            "task": "T-1", "role": "coder", "executor": "external_ai",
            "brief": "", "worktree": tmp, "done_sentinel": sent, "base": "",
            "created_ms": 0, "deadline_ms": int(time.time() * 1000) + 60000,
            "model": {"provider": "kimi", "mock_cmd": "echo ext-ai-ok"},
            "write_scope": ["src/"],
        })
        rc, doc = handle(jp, backend="mock", mode="apply")
        ck("mock 回路退出码为 0", rc == 0, (rc, doc))
        ck("签署已落盘", os.path.isfile(sent))
        got = _read_json(sent) or {}
        ck("签署声明 backend=mock", got.get("backend") == "mock", got.get("backend"))
        ck("签署声明 mode=apply", got.get("mode") == "apply", got.get("mode"))
        ck("selfcheck 记下真实退出码",
           (got.get("selfcheck") or {}).get("exit") == 0, got.get("selfcheck"))
        ck("签署写明由外部 AI 执行体产出",
           got.get("by") == "executors/ext_ai_subagent.py", got.get("by"))
        ck("签署带 schema", got.get("schema") == SCHEMA, got.get("schema"))

        # 2. 命令失败必须如实报告，且不写签署
        jp2 = os.path.join(tmp, "T-2.coder.json")
        sent2 = os.path.join(tmp, "done", "T-2.coder.json")
        _write_json_atomic(jp2, {
            "task": "T-2", "role": "coder", "done_sentinel": sent2,
            "deadline_ms": int(time.time() * 1000) + 60000,
            "model": {"mock_cmd": "exit 9"}, "write_scope": [],
        })
        rc2, doc2 = handle(jp2, backend="mock", mode="apply")
        ck("命令失败时退出码非 0", rc2 != 0, rc2)
        ck("命令失败时不写签署", not os.path.isfile(sent2))
        ck("失败原因不是空话", "rc=9" in json.dumps(doc2, ensure_ascii=False), doc2)

        # 3. 过期 job 不签
        jp3 = os.path.join(tmp, "T-3.coder.json")
        _write_json_atomic(jp3, {"task": "T-3", "role": "coder",
                                 "done_sentinel": os.path.join(tmp, "done", "T-3.json"),
                                 "deadline_ms": 1000})
        rc3, doc3 = handle(jp3, backend="mock", mode="consult", now_ms=5000)
        ck("过期 job 拒绝签署", rc3 == 3, (rc3, doc3))

        # 4. 写域边界（含 Kimi 复核时指出的同前缀越权）
        ck("空写域不许写", scope_allows([], "src/a.py") is False)
        ck("前缀写域生效", scope_allows(["src/"], "src/a.py") is True)
        ck("前缀不误伤同级同名前缀", scope_allows(["src/"], "srcx/a.py") is False)
        ck("反斜杠写域也能比对", scope_allows(["src\\"], "src/a.py") is True)
        ck("无尾斜杠写域不越权同前缀兄弟",
           scope_allows(["docs"], "docs2/a.py") is False,
           scope_allows(["docs"], "docs2/a.py"))
        ck("无尾斜杠写域仍放行自己的子路径",
           scope_allows(["docs"], "docs/a.py") is True)
        ck("无尾斜杠写域精确匹配文件自己",
           scope_allows(["docs"], "docs") is True)
        ck("** 后缀写域按目录对齐",
           scope_allows(["docs/arch/**"], "docs/arch/a.md") is True)
        ck("** 后缀写域不越权兄弟目录",
           scope_allows(["docs/arch/**"], "docs/archx/a.md") is False)
        ck("精确文件名写域只放行它自己",
           scope_allows(["config.yaml"], "config.yaml") is True)
        ck("精确文件名写域不放行同名目录下文件",
           scope_allows(["config.yaml"], "config.yaml/../x") is False)

        # 5. consult 模式绝不声称改过文件（这是本文件的核心诚实约束）
        jp5 = os.path.join(tmp, "T-5.reviewer.json")
        sent5 = os.path.join(tmp, "done", "T-5.reviewer.json")
        _write_json_atomic(jp5, {
            "task": "T-5", "role": "reviewer", "done_sentinel": sent5,
            "deadline_ms": int(time.time() * 1000) + 60000,
            "model": {"mock_cmd": "echo reviewed"}, "write_scope": [],
        })
        rc5, doc5 = handle(jp5, backend="mock", mode="consult")
        ck("consult 回路退出码为 0", rc5 == 0, (rc5, doc5))
        ck("consult 签署 changed 恒为空",
           (_read_json(sent5) or {}).get("changed") == [], _read_json(sent5))
        ck("consult 签署写明 mode",
           (_read_json(sent5) or {}).get("mode") == "consult", _read_json(sent5))

        # 6. 能力表必须是可核对的：不可用的条目必须有证据
        rep = providers_report()
        ck("能力表非空", len(rep) >= 4, len(rep))
        ck("不可用的 provider 都写了证据",
           all(r["evidence"] for r in rep if not r["available"]),
           [r["provider"] for r in rep if not r["available"] and not r["evidence"]])
        ck("kimi 在能力表里且声明可用",
           any(r["provider"] == "kimi" and r["available"] for r in rep), rep)
        ck("workbuddy 如实写成不可用",
           any(r["provider"] == "workbuddy" and not r["available"] for r in rep), rep)

        # 7. 挑 provider：不可用的要被拒并说清原因
        name, why = select_provider({"model": {"provider": "doubao"}})
        ck("挑不可用 provider 会被拒", name is None, name)
        ck("拒绝理由不是空话", "不可用" in why, why)
        name2, why2 = select_provider({"model": {"provider": "kimi"}})
        ck("挑 kimi 成功", name2 == "kimi", (name2, why2))

        # 8. 缺密钥必须失败，不许降级。
        #    这里**只能测 deepseek**：kimi 的密钥能从它自己的配置文件读到，
        #    所以「pop 掉环境变量」并不会让它缺密钥，反而会真发一次网络请求。
        #    自检必须是离线的 —— 联网的自检会在没网的机器上红，在限流的机器上假红。
        #    同理 deepseek 现在也会读 .git/info/model_credentials.json，
        #    所以要连这份文件路径一起指向不存在的文件，才真的「没有钥匙」。
        saved = {k: os.environ.pop(k, None) for k in (ENDPOINT_ENV, KEY_ENV)}
        global MODEL_CRED_FILE
        saved_cred = MODEL_CRED_FILE
        MODEL_CRED_FILE = os.path.join(tmp, "no-such-credentials.json")
        try:
            r8 = call_provider("deepseek", "consult", {"task": "T", "role": "r"})
            ck("缺密钥时 call_provider 失败", r8.get("ok") is False, r8)
            ck("失败信息点名缺哪把钥匙",
               KEY_ENV in (r8.get("error") or ""), r8.get("error"))
            ck("失败信息不冒充已跑过模型",
               "拒绝假装" in (r8.get("error") or ""), r8.get("error"))
        finally:
            MODEL_CRED_FILE = saved_cred
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

        # 9. JSON 抽取要能容忍模型包代码块
        ck("能解析裸 JSON", isinstance(_parse_ai_json('{"a":1}'), dict))
        ck("能解析包着代码块的 JSON",
           (_parse_ai_json('```json\n{"a":1}\n```') or {}).get("a") == 1)
        ck("非 JSON 返回 None", _parse_ai_json("抱歉我不能") is None)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, c in ok if not c]
    print("\next_ai_subagent: %s (%d checks, %d failed)"
          % ("PASS" if not bad else "FAIL", len(ok), len(bad)))
    return 0 if not bad else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="外部 AI 执行体：读 job 写 sentinel")
    ap.add_argument("--job", help="job 文件路径")
    ap.add_argument("--backend", default="mock", choices=("mock", "external"))
    ap.add_argument("--provider", default=None, help="指定供应商（kimi/deepseek/...）")
    ap.add_argument("--mode", default=None, choices=("consult", "apply"))
    ap.add_argument("--list-providers", action="store_true",
                    help="打印能力表（含不可用的证据）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.list_providers:
        print(json.dumps({"schema": SCHEMA, "providers": providers_report()},
                         ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if a.selftest:
        return selftest()
    if not a.job:
        ap.error("--job is required (or use --selftest / --list-providers)")
    rc, doc = handle(a.job, backend=a.backend, provider=a.provider, mode=a.mode)
    print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
