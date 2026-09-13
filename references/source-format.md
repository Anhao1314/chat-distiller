# 豆包 Work 会话缓存格式（阶段 A 输入说明书）

> 给执行 agent：理解缓存长什么样、哪些是知识、哪些是噪声。提取动作已由
> `scripts/extract_sessions.py` 确定性完成，通常无需手写解析；本文件用于排查与调整。

## 目录

- 根位置与会话目录
- 每个文件的作用
- trajectory.jsonl 消息结构
- 噪声与清洗规则
- 边界情况

## 根位置与会话目录

```
<Application Support>/DoubaoWork/Default/.doubaowork/agent_mode/workspace/.sessions/
└── <session_id>/                         # 一个会话一个目录，id 为纯数字
    ├── board.md                          # 多 agent 任务公告板（常为空表头）
    ├── memory/MEMORY.md                  # 会话记忆
    └── agents/<agent_id>/
        ├── system/assignment.md          # ★ 用户每轮需求（append-only，最干净）
        ├── system/trajectory.jsonl       # ★ 完整消息轨迹（含工具噪声）
        └── socket/{briefing.md,findings.jsonl}  # 内部通信，常为空
```

- 默认根路径已写进脚本 `--sessions-root` 默认值；换机器/换账号时用参数覆盖。
- 每个会话当前都是**单 agent**；脚本仍兼容多 agent（遍历 `agents/*` 并按时间合并）。

## assignment.md：用户需求时间线（首选来源）

- 顶部是 Agent ID / Role / Created（UTC，ISO8601）。
- 每轮需求一个块，格式：`` ## [2026-09-12T15:00:55.804Z] 需求 ``，**最新追加在末尾**。
- 块正文就是用户当轮真实输入，不含工具结果，噪声最少。脚本以它作为「用户需求」主线。
- 该文件缺失或为空时，脚本回退到 trajectory 里的 `user` 消息兜底，并在转录顶部与
  `sessions_index.md` 的「标记」列标 `⚠️降级`——回退来的内容可能残留系统注入，浓缩时需甄别。

## trajectory.jsonl：消息流

每行一个 JSON（JSONL），OpenAI 风格，无外层 type，靠 `role` 区分：

| role | 关键字段 | 含义 | 浓缩取舍 |
| --- | --- | --- | --- |
| `user` | `content`(str) | 用户消息，但常夹带系统注入/上下文重放 | 优先用 assignment；assignment 缺失时回退到这里（会标 ⚠️降级） |
| `assistant` | `content`(str，可空) | 助手面向用户的文本结论 | ★ 保留，是「助手关键回复」 |
| `assistant` | `tool_calls`(list) | 工具调用请求（function.name/arguments） | 默认丢弃；`--keep-tool-trail` 时只留工具名序列 |
| `tool` | `tool_call_id`+`content` | 工具返回结果，体积大、噪声高 | 一律丢弃 |

- `content` 偶尔是多模态分段 list（`[{type:text,text:...}]`），脚本只抽 text。
- 纯工具执行会话可能「有用户轮次、0 条文本回复（A0）」，浓缩时通常判为低价值。

## 噪声与清洗规则（脚本已实现）

只剥离**固定白名单**，避免误删用户粘贴的合法 XML/代码：

1. 成对系统注入块：`<system-reminder>…</system-reminder>`、`<retained_skills>`、
   `<artifact_reload>`、`<tool_search_remind>`、`<installed-skills>`、`<current-state>`、
   `<account-state>`、`<connector-usage>`、`<project-directories>`、`<agent-workspace>`、
   `<user-preferences>`、`<current-user-location>`、`<os-and-device>`、`<current-date>`、
   `<permission-and-authorization>`、`<usage_guide>`。
2. 上下文压缩重放段：`=== TASK_FOCUS ===`、`TASK_GOAL/TASK_NARRATIVE/KEY_FACTS_AND_IDS/
   ARTIFACTS/REFERENCED_IMAGES/SKILLS_LOADED/OPEN_ITEMS_AND_RESUME_NOTES` 等，
   从该标题删到下一个 `=== 标题 ===` 或文末。
3. `<think>…</think>` 思考块；统一换行；3+ 连续空行折叠为 1 个空行。
4. **不要**按「所有尖括号标签」一刀切——用户代码里的 `<service>`、`<script>` 等是合法内容。

## 边界情况

- **超大会话**：个别 transcript 可达数十万字。浓缩时只读「用户需求时间线 + 每条助手回复的
  开头结论段」，跳过冗长过程；必要时按用户需求轮次切分主题。
- **坏 JSON 行**：脚本跳过并在转录末尾注释计数，不中断整体。
- **空会话 / A0**：清单「标记」列里标 `⚠️空`，一般 value=弃。
- **时间**：缓存内为 UTC，脚本统一转北京时间（UTC+8）`YYYY-MM-DD HH:MM`。
- **幂等**：重复提取只覆盖 `_kb_staging/`，绝不修改 `.sessions` 原始缓存与 Obsidian 库。
