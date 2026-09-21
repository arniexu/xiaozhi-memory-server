# CJK 检索召回待办（来自 xiaozhi 真机联调）

日期：2026-09-21 ｜ 来源：xiaozhi-esp32-server 记忆集成联调（真实 DeepSeek + CR v0.1.7，实例 10.112.229.254:8765）
证据：xiaozhi 仓库 continuity 决策日志（2026-09-21）；复现探针见该仓库联调记录

## 现象（实测）

1. **词法门 = 连续子串 AND**：`memory_store.cjk_phrase` 把查询里每个连续 CJK
   片段做成 `"字 字 字"` 短语并以 AND 连接——查询片段必须**逐字连续**出现在记忆
   文本中。`儿子` 命中，`儿子叫什么` / `我儿子叫什么名字` 0 命中 → 自然语言
   整句话召回大概率失败（设备用户在真实场景说的就是整句）。
2. **语义门需要调用方携带 embedding**：`/v1/search` 仅当 `request.embedding`
   非空时才跑向量检索（`source_kind='knowledge'`，按 `min_similarity` 过滤）；
   无语义能力的调用方（xiaozhi provider）实际拿不到语义召回。
3. **空 workspace 单元全库参与**：`_match` 的 workspace 过滤含
   `m.workspace_id=''`（不受 `scope_fallback` 约束）——continuity 导入的
   `ws=None` 历史决策会出现在任何 workspace 的检索结果里（实测单字查询
   "光"把 LVGL 决策拉进客厅灯检索）。

## 建议改进（待排期）

1. **CJK 词法 OR 回退**（对齐 ASCII 已有设计）：`cjk_phrase` 0 命中时，追加
   bigram/trigram 的 `fts_any` OR 通道（保住"无关查询 0 召回"的同时提升自然句
   召回）；或对长 CJK run 先按疑问词/助词切分再 AND。
2. **严格 scope 开关**：为空 workspace 的参与提供调用方开关（如
   `strict_workspace=true` 时排除 `ws=''`），支撑多租户/多实例隔离诉求。
3. **memory 单元向量化（可选）**：导入时为 memory 类单元生成向量（或暴露
   `/v1/embed`），让无语义能力的调用方也能启用 `semantic_memories`。
4. **近似重复去重（观察项）**：LLM 每次总结措辞不同（"客厅灯 / 客厅暖光灯 /
   开灯默认客厅暖光灯"），节点 ID 含文本 → 跨会话累积近义节点；考虑 fingerprint
   归一或近似合并策略。

## 当前规避（xiaozhi 侧）

`core/providers/memory/context_recall/query_utils.py`：问句拆词 + 多路检索 +
客户端合并（模拟 OR）；单字候选仅兜底（规避空 workspace 噪声）。
自测 74/74；真机三条整句问句（家人/灯光/作息）全部命中且无跨 scope 噪声。
