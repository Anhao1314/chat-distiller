# chat-distiller

![tests](https://github.com/Anhao1314/chat-distiller/actions/workflows/tests.yml/badge.svg)

把豆包 Work 本地的历史会话缓存，**蒸馏**成结构化、可生长的 Obsidian 知识库：会话笔记 +
原子知识卡片 + 检索索引 + 操作日志，并可随时体检。

> 确定性的事交给脚本，需要判断的事（取舍 / 浓缩 / 归类 / 双链）交给 agent。

## 为什么是「对话」而不是「文档」

Karpathy 的 [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
描述的是一种比 RAG 更划算的知识组织方式：知识在**摄入时**编译一次、之后持续维护，
而不是每次提问都从原始文档里重新推导一遍。

但它默认你要**自己去策展素材**——文章、论文、PDF、网页剪辑。

对话是个例外：**它每天都在自动产生，不需要你收集。**

chat-distiller 就是把这套方法论用在你和 agent 的对话上：对话产生知识，蒸馏抓住它，
再交给检索消费。你不需要额外做任何素材整理。

它和 RAG 的区别也正在这里——**知识是编译出来的，不是每次查询临时拼的**。这对
「agent 工作时回想历史决策」这种场景尤其重要：长对话被反复压缩后必然失真，
而一份外置、可检索、带状态的知识库能让压缩变得无害。

## 流水线

| 阶段 | 执行者 | 输入 → 输出 |
| --- | --- | --- |
| A 提取清洗（确定性） | `scripts/extract_sessions.py` | `.sessions/` → 干净转录 + 会话清单 |
| B 语义浓缩（判断） | agent（规则见 `references/`） | 转录 → `distill.json` |
| C 渲染落库（确定性） | `scripts/render_notes.py` | `distill.json` → 笔记 + 索引 + 日志 + 校验报告 |
| D 体检（确定性 + 判断） | `scripts/lint_notes.py` + agent | 结构一致性、证据核验、矛盾与过期 |

## 产物结构

```
<vault>/对话沉淀/
├── 00 · 对话沉淀 MOC.md      # 给人的总导航
├── 知识索引.md / .jsonl      # 给 agent 与程序化检索的紧凑索引
├── 操作日志.md               # append-only，记录每次摄入与变化
├── 沉淀索引.base             # Obsidian Bases 视图
├── .chat-distiller/          # 本库自带：taxonomy.md 词表、distill.json 渲染源、pending/ 待蒸馏
├── 会话笔记/Snn - 标题.md     # Snn 稳定短 ID（日期在 frontmatter）
└── 知识卡片/Cnn - 标题.md     # 五类卡片，带状态字段
```

## 特性

- **零第三方依赖**：四个脚本只用 Python 标准库；同一输入幂等产出同一结果。
- **自带回归测试**：`python3 -m unittest discover -s tests`，28 个用例覆盖提取回退、
  YAML 转义、死链校验、索引与日志、状态标记、证据核验、压缩触发边界。
- **压缩时自动接回**：agent 的上下文被压缩后，`SessionStart` hook 会把「先查索引」的提醒
  注入模型上下文；压缩前记下一条待蒸馏标记。Codex 与 Claude Code 同一套配置。
- **知识库自描述**：分类词表、源数据、索引、日志都住在 vault 里，跟数据走而不是跟工具走；
  换机器、换工具版本都不会分裂。
- **两套正交分类**：`kind`（方法 / 事实 / 决策 / 教训 / 资源）× `categories`
  （受控两级领域，可多属），后者由渲染脚本校验。
- **杂糅会话分段**：一个会话跨多个领域时，在一篇笔记内按 `threads` 分主题段，而不是拆成多篇。
- **知识会过期，但不删**：卡片带 `status`（现行 / 已过期 / 有争议）与 `superseded_by`，
  旧结论保留「当时为什么这么想」，同时不污染现行视图。
- **只增不删**：渲染只覆盖/新增它自己生成的文件；旧笔记列为孤儿待人工确认，绝不自动删除。
- **只读原始缓存**：阶段 A 绝不修改 `.sessions`。

## 安装

它是一个 **Agent Skill**：一份 `SKILL.md` + 三份零依赖脚本。装进你的 agent 能读到的 skills 目录即可：

```bash
# Codex
git clone https://github.com/Anhao1314/chat-distiller.git ~/.codex/skills/chat-distiller

# Claude Code
git clone https://github.com/Anhao1314/chat-distiller.git ~/.claude/skills/chat-distiller
```

装好后对 agent 说「把这些历史对话浓缩进我的知识库」即可触发。它不会擅自开跑——
`.sessions` 位置、vault 路径、收哪些会话，都会先跟你确认。

不想装成 skill 也行：三个脚本可以直接单独跑，见下。

## 用法

完整工作流见 `SKILL.md`。脚本也可独立运行：

```bash
# A. 提取干净转录
python3 scripts/extract_sessions.py --out ./_kb_staging

# B.（由 agent 依据 references/distillation-schema.md 与词表把转录浓缩为 distill.json）

# C. 先预览再正式写入 vault
python3 scripts/render_notes.py --distill ./distill.json --dry-run
python3 scripts/render_notes.py --distill ./distill.json \
  --vault "/path/to/your/vault" --subdir 对话沉淀

# D. 体检：结构一致性 + 证据核验（配合 A 产出的转录）
python3 scripts/lint_notes.py --vault "/path/to/your/vault" \
  --transcripts ./_kb_staging/transcripts
```

`render_notes.py` 的 `--dry-run` 同样会给出完整的死链、孤儿与新分类报告——预览是真的预览。
报告里 `created`/`updated` 只算知识内容，索引与日志这类派生文件单列在 `meta_changed`。

### 让 agent 在看不清的时候想起它

知识库有个尴尬之处：它最该被用到的时刻，恰恰是 agent 在做别的工作、压根没加载这个 skill 的时候。
两层接法（细节见 `SKILL.md`）：

- **常态**：往项目 `AGENTS.md` 加一句「涉及历史决策先读 `知识索引.md`」。
- **压缩时**：AGENTS.md 表达不了「当……的时候」。上下文压缩是最该想起知识库的一刻——agent 刚
  丢掉细节、最容易凭印象编。把 `assets/hooks.example.json` 填好路径放进 `~/.codex/hooks.json`
  或 `~/.claude/settings.json`，`scripts/compact_hook.py` 就会在两个工具的同名事件上工作：
  压缩后注入提醒（`SessionStart` / `compact`），压缩前记下待蒸馏标记（`PreCompact`）。

## 分类词表

词表住在**你的知识库**里（`<vault>/对话沉淀/.chat-distiller/taxonomy.md`），不在本仓库。
首次渲染时会从 `references/taxonomy.template.md` 播种一份起始词表，之后按你的领域随意增删。
渲染与体检会校验取值是否在词表内，并把未登记的分类列进报告。

## 体检：三档不同的权威等级

| 档位 | 谁执行 | 典型项 | 能否自动修 |
| --- | --- | --- | --- |
| T1 结构一致性 | `lint_notes.py` | 编号空洞、字段缺失、反链断裂、索引不一致 | 重跑渲染即可 |
| T2 证据核验 | `lint_notes.py` | 卡片里的日期 / 数字 / 版本号 / 路径在转录中逐字找不到 | 否，需判断 |
| T3 判断项 | agent，见 `references/lint-rules.md` | 矛盾、过期未标注、缺失交叉引用、粒度失当 | 否，只报告 |

T2 是治幻觉的机制：卡片声称的事实必须能在原始转录里逐字找到。候选集是**封闭且冻结**的
（ISO 日期、版本号、具体数字、路径、文件名），命中的是「存疑」而不是「错误」——
卡片是转述而非引用，判断留给人。

报告里的 `evidence_checked` 给出本次**实际核对了几处**字面量：`0 存疑` 和「压根没跑」
必须能区分开，跳过核验时报告会写明 `evidence_skipped` 及原因。

## 与 LLM Wiki 生态的关系

方法论来自 Andrej Karpathy 的 [llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)。
本项目的差异点有三个：

1. **素材源是 agent 对话缓存**，不是需要人工策展的文档——`raw/` 这层由对话自动生成；
2. **分类受控**：领域取自可维护的两级词表，而不是让模型自由造页面，检索时的标签才可能一致；
3. **渲染与校验是确定性的**：索引、日志、frontmatter、双链都由脚本生成与校验，
   不依赖 agent 每次记得做对（对比之下，多数同类实现的 index/log 是 agent 手写的）。

## 适用范围与边界

- 面向豆包 Work 本地 `.sessions` 缓存结构；**不用于**实时对话、在线文档导出或网页采集。
- 目前只适配豆包 Work 这一个来源。接其他 chat agent 需要另写提取器——
  契约很薄（产出一份带 `session_id` 的转录 + 一份清单），阶段 B/C/D 无需改动。
- 产物为本地 Markdown，可纳入 Git 版本管理，自主可控、可迁移到任意兼容 Markdown / 双链的笔记工具。

## License

[MIT](LICENSE)
