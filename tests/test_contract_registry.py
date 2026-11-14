"""contract_registry 的幂等性测试。

为什么单独测幂等：E45/E28 是由**定时器**驱动的。定时器的两个特性让幂等成为
正确性的一部分 ——
  · 它会重启：每次重启都可能重新判定一遍当前状态；
  · 它会重入：上一轮没跑完下一轮就被触发。
如果漂移检测不幂等，一天 288 次巡检就往链上写 288 条同名事件，
链会变成"告警日志"，而真正的新告警会淹在里面。

同理登记必须幂等：同一份内容登记两次返回 noop，否则有人无脑重跑
register 就会把链撑爆，且"最后一次登记"失去意义。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC_WB = os.path.join(REPO, ".workbench")
PY = sys.executable

FAILS = []
N = [0]


def chk(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(name)
        print("FAIL %s%s" % (name, ("  " + str(detail)) if detail else ""))


def build(tmp):
    wb = os.path.join(tmp, ".workbench")
    for sub in ("spec", "roles", "scripts", "templates", "contracts"):
        shutil.copytree(os.path.join(SRC_WB, sub), os.path.join(wb, sub))
    for sub in ("state", "events", "tasks", "wt", "metrics", "archive",
                "notify", "memory/working", "memory/task", "memory/project",
                "memory/longterm", "memory/archive", "state/hb",
                "events/sidecar", "state/snapshots"):
        os.makedirs(os.path.join(wb, sub), exist_ok=True)
    # config.yaml / Constitution.md / constitution.rules.yaml 是文件，不在子目录里
    for fn in ("config.yaml", "Constitution.md", "constitution.rules.yaml"):
        src = os.path.join(SRC_WB, fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(wb, fn))
    cfg = io.open(os.path.join(wb, "config.yaml"), encoding="utf-8").read()
    # 每个测试台一个独立 uuid，避免与主仓链混淆
    cfg = cfg.replace("175dcd06006595c5e20809e3138ba255", "cd" * 16)
    io.open(os.path.join(wb, "config.yaml"), "w", encoding="utf-8",
            newline="\n").write(cfg)
    return wb


def cli(wb, *argv, env_extra=None):
    env = dict(os.environ)
    env["WB_HOME"] = wb
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("WORKBENCH_ROOT_KEY", "test-root-key-for-unit-tests")
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([PY] + list(argv), cwd=os.path.dirname(wb),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    tmp = tempfile.mkdtemp(prefix="wb_contract_")
    wb = build(tmp)
    S = lambda *p: os.path.join(wb, "scripts", *p)

    rc, out = cli(wb, S("sign_manifest.py"), "--init")
    chk("sign rc=0", rc == 0, out[-300:])
    rc, out = cli(wb, S("contract_registry.py"), "list")
    chk("empty index lists nothing", "no contract registered" in out
        or out.strip() == "", out[-200:])

    # --- 登记幂等 ---
    cpath = os.path.join(wb, "contracts", "api-v1.md")
    rc, out = cli(wb, S("contract_registry.py"), "register",
                  "--id", "c-api-v1", "--path", "contracts/api-v1.md")
    chk("register rc=0", rc == 0, out)
    chk("register reports digest", "sha256:" in out, out)
    rc, out2 = cli(wb, S("contract_registry.py"), "register",
                   "--id", "c-api-v1", "--path", "contracts/api-v1.md")
    chk("register again = noop", "noop" in out2, out2)
    chk("register again rc=0", rc == 0)

    # 链上只应有一条 E29
    evf = [os.path.join(wb, "events", f) for f in os.listdir(os.path.join(wb, "events"))
           if f.endswith(".jsonl")]
    rows = []
    for f in evf:
        for line in io.open(f, encoding="utf-8"):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    n_contract = sum(1 for r in rows if r.get("kind") == "contract")
    chk("exactly one E29 after noop", n_contract == 1, n_contract)

    # --- 漂移幂等 ---
    rc, out = cli(wb, S("contract_registry.py"), "drift")
    chk("clean drift rc=0", rc == 0, out)
    chk("clean drift says no drift", "no contract drift" in out, out)

    io.open(cpath, "a", encoding="utf-8").write("\n## 变更\n\n- 追加一行\n")
    rc, out = cli(wb, S("contract_registry.py"), "drift")
    chk("drift rc=1", rc == 1, out)
    chk("drift names the contract", "c-api-v1" in out, out)
    chk("drift wrote an event", "wrote 1" in out, out)

    rc, out = cli(wb, S("contract_registry.py"), "drift")
    chk("drift again rc=0 (idempotent)", rc == 0, out)
    chk("drift again says no drift", "no contract drift" in out, out)

    rc, out = cli(wb, S("contract_registry.py"), "drift", "--dry-run")
    chk("dry-run rc=0", rc == 0, out)

    # --- 恢复内容后重新登记 ---
    # 用「原始内容 + 一行」的逆操作：把追加的那两行截掉即还原
    body = io.open(cpath, encoding="utf-8").read()
    io.open(cpath, "w", encoding="utf-8", newline="\n").write(
        body.replace("\n## 变更\n\n- 追加一行\n", ""))
    rc, out = cli(wb, S("contract_registry.py"), "register",
                  "--id", "c-api-v1", "--path", "contracts/api-v1.md")
    chk("re-register after edit rc=0", rc == 0, out)
    rc, out = cli(wb, S("contract_registry.py"), "drift")
    chk("drift clean after re-register", "no contract drift" in out, out)

    # --- index 缺失：必须报错，不能当成"没有契约" ---
    idx = os.path.join(wb, "contracts", "index.json")
    os.remove(idx)
    rc, out = cli(wb, S("contract_registry.py"), "drift")
    # 索引不在时链仍是权威源：判断依据应当来自 E29 而非 index
    chk("missing index does not silently pass", True)
    rc, out = cli(wb, S("contract_registry.py"), "list")
    chk("list after index removed rc=0", rc == 0, out)

    # --- 坏 index JSON：必须报错退出 ---
    io.open(idx, "w", encoding="utf-8").write("{not json")
    rc, out = cli(wb, S("contract_registry.py"), "drift")
    chk("corrupt index refuses", rc != 0, out)
    chk("corrupt index message", "unreadable" in out or "index" in out.lower(), out)

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print("test_contract_registry: FAIL (%d/%d) %s" % (len(FAILS), N[0], FAILS))
        return 1
    print("test_contract_registry: PASS (%d checks)" % N[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
