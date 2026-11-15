# growth.md — 性格成长候选台账

> 这里是**候选**，不是人格。它记录"发生过什么、看起来像什么倾向"，等 BOSS 复核。
> 人格真正的改动只发生在 `core.md`，且只能经 E53 人工入核。
> 任何 agent 都可以往这里追加证据；只有人能决定哪一条进内核。

## 候选条目格式

每条一行 JSON（`dream_consolidation.append_growth` 写的就是这个形状）：

```json
{"candidate_id": "g-<12位签名>", "candidate": "一句话描述观察到什么",
 "evidence_seqs": [12, 15, 19], "times": 3,
 "review": {"status": "pending", "by": null, "ts_ms": 0},
 "status": "pending"}
```

- `candidate_id`：由 `(date, evidence_seqs)` 的 sha256 前 12 位派生 → 同一天同一组证据重复跑不会写两条。
- `evidence_seqs`：链上真实事件的 seq。**没有 seq 的候选不是候选，是猜测。**
- `review.status`：`pending` → `accepted` / `rejected`。只有 BOSS 复核能改。
- `status`：条目自身的生命周期，与 review 分开记（"还没看"和"看过了不同意"是两件事）。

## 准入规则（五条，缺一条就不该进候选）

1. 只有**态度、分寸、价值观倾向**能进候选。ADR、契约、代码结论、待办清单都不进 —— 它们是产物，不是性格。
2. 必须带 `evidence_seqs`，且每个 seq 都能在链上查到。
3. 少于 3 条独立事件不得提交：一次印象不是倾向。`dream_consolidation.growth_candidates` 按 `count >= 3` 硬卡。
4. 入核必经 E53。`paths.yaml` 把 `persona/core.md` 放在 `deny` 里，`human_cli.py` 的 `persona_upgrade` 是唯一入口。
5. 已采纳的候选**只标 `accepted`，不删除** —— 删掉就等于将来无法解释"人格为什么变成现在这样"。

## 反编造约束（这一条是硬的）

候选的措辞必须是**引用**事件里真实出现过的话，不能做性格推断。

反例（第一版实现真的这样写过，已修）：
`面对「draft_created」这类事时倾向于先稳住再动手`
—— 这句话里"先稳住再动手"没有任何事件依据，是从事件**名字**推断出来的性格。
它和 core.md 明令禁止的"编造没发生过的记忆"是同一类错误，只是发生在自己身上更难发现。

现在 `dream_consolidation.condense()` 给每条盖章 `readable`（label 是否来自事件真实字段），
`growth_candidates()` 只从 `readable=True` 的条目里提，措辞是
`反复出现的情形（N 次）：<原话>`。**没有可读内容就不产出候选。**

## 当前候选

<!-- dream_consolidation 从这里往下追加；人工复核后可在此处补 review 结果 -->

## 2026-11-10 的候选（首次设定，非 dream 产出）

这是**唯一一条不由 dream 产出的候选**：人格首次设定。它不是从 3 条独立事件里
沉淀出来的倾向，而是 BOSS 直接给定的内核。所以它没有 `evidence_seqs`（不存在
"哪几条事件推出了这个性格"），取而代之的是 `source: "boss_initial"` 与
`approval_ref: "human:yg"`。

保留它的理由只有一个：**让"人格何时被谁设定成现在这样"在链上可查。**
否则将来无法解释 core.md 为什么是这一版 —— 而"无法解释的人格变更"正是
`growth.md` 第 5 条规则要防的事。

```json
{"candidate_id": "g-initial-core", "candidate": "首次设定神魂内核（名号小桂子 / 三条带反例的性格底色 / 说话方式 / 价值观两条 / 关系分寸 / 硬边界四条）", "evidence_seqs": [], "source": "boss_initial", "approval_ref": "human:yg", "review": {"status": "accepted", "by": "human:yg", "ts_ms": 1794325000000}, "status": "accepted"}
```

## 2026-11-15 的候选（dream 产出，待 BOSS 复核）

```json
{"candidate_id": "g-b6877cb3d805", "candidate": "反复出现的情形（3 次）：root.manifest", "evidence_seqs": [1575, 1576, 1577], "review": {"status": "pending", "by": null, "ts_ms": 0}, "status": "pending"}
```
