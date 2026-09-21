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

## 附：2026-09-21 深夜加测（hard v3）新证据

- **更新/更正场景旧值残留**：八岁→九岁、爬山→游泳、咖啡→美式后，旧节点仍保留，
  召回可能新旧同现（实测 1 例：周末查询直连含“爬山”）→ 建议为 supersede /
  近似去重提升优先级（如按 source session 时间序标注“最新/已更正”）。
- 其余全部通过：26 条矩阵 0 失败、噪声行 0、p95 259ms、设备杂音未入库、稳定性一致。
- graph 通道结果无 workspace 字段——已由调用方（xiaozhi provider 严格白名单）过滤，
  CR 侧如需支持可加开关（见建议 2）。

## 标准语义化提案（2026-09-21，方向：修在源头、兼容演进）

**原则**：不给调用方留“永久 workaround”；语义在 CR 侧归一；所有行为变更用**新参数保默认**，
调用方显式 opt-in；下一大版本再评估翻默认。xiaozhi 的客户端增强（拆词/白名单）保留为
防御层，CR 修复落地后用 battery/hard 回归证明可安全简化。

### P1：scope 语义归一（消“exact 却含全局桶”的错位）
- 请求新增 `strict_workspace: bool = False`：True 时不并入 `ws=''` 全局桶
  （当前 `_match` 把 `ws=''` 写死在 exact 分支）；False 保持现行为（extension 不受影响）。
- 响应诚实化：含全局桶结果时 `scope_match` 记为 `exact+global`（或新增
  `global_included: N` 计数字段），不再用 `exact` 掩盖。
- README 补术语表：scope = workspace × repo × session；`''` 的全局桶语义显式化。

### P2：CJK 通道与 ASCII 对齐（本职的标准语义）
- 现状：ASCII 有“AND+OR 并联”（`_search_once` 注释自述的设计哲学），CJK 只有
  phrase AND 单通道 → 同库两种语义。按同一哲学补 CJK 的 OR 回退通道
  （bigram `fts_any`，仅在 phrase 0 命中时启用，保住“无关查询 0 召回”）。
- 验收：新增 CJK 用例入 tests/；xiaozhi battery+hard 全量回归（当前 16/16、26 条）。

### P3：graph 通道可控
- 请求新增 `include_graph: bool = True`（默认兼容）；xiaozhi 设 false，
  消除“图实体无 workspace 渗入任意检索”。
- 正解（后续）：graph 节点 upsert 时带 workspace 属性 + graph 检索过滤（含迁移）。

### P4：更新/更正语义（已记 #4，优先级上升）
- 旧值残留（爬山↔游泳）→ supersede 或时间序“最新/已更正”标注。

### 兼容性矩阵
| 变更 | 默认 | extension | xiaozhi |
|---|---|---|---|
| strict_workspace | False | 不传 → 现行为 | 传 True（白名单可退为防御） |
| scope_match 诚实化 | — | 观察字段语义变化 | 无感 |
| CJK OR 回退 | 建议默认开 | 中文召回提升（回归测试护航） | 拆词可评估简化 |
| include_graph | True | 不传 → 现行为 | 传 False |
