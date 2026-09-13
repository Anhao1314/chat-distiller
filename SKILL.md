---
name: chat-distiller
description: >-
  把豆包 Work（豆包办公/agent_mode）本地的历史对话、会话缓存、聊天记录浓缩（蒸馏/沉淀）
  成可直接放进 Obsidian 的结构化知识库笔记。当用户说「把历史对话/聊天记录/会话缓存浓缩成
  知识库」「对话转 Obsidian 笔记」「沉淀这些会话」「整理 .sessions / trajectory 对话」
  「把以前的对话做成知识卡片/MOC」时使用。流水线为：脚本确定性提取干净转录 → agent 语义
  浓缩为 distill.json → 脚本确定性渲染成 Obsidian 会话笔记、原子知识卡片、MOC 与 Bases 索引，
  输出与既有 vault 相同的 frontmatter / 双链 / callout 规范。不用于实时对话、飞书云文档导出
  或网页采集。
---

# 对话缓存 → Obsidian 知识库

把豆包 Work 本地 `.sessions` 里的历史会话，蒸馏成 Obsidian 里「会话笔记 + 原子知识卡片 +
MOC + Bases」的可生长知识网络。**确定性的事交给脚本，需要判断的事（取舍/浓缩/双链）交给 agent。**

## 产物结构（写入目标 vault，默认 `~/Documents/我的知识库/My RAG/对话沉淀/`）

```
对话沉淀/
├── 00 · 对话沉淀 MOC.md      # 会话一览 + 按领域聚合 + 全部卡片
├── 沉淀索引.base              # 全部/按主题/按领域/高价值/卡片/待跟进 等七个视图
├── 会话笔记/Snn - 标题.md      # Snn 稳定短 ID（日期留在 frontmatter），H1 为「# Snn · 标题」
└── 知识卡片/Cnn - 标题.md      # method/fact/decision/lesson/resource，H1 为「# Cnn · 标题」
```

**两套正交分类，别混淆**：`kind` 按知识性质（方法/事实/决策/教训/资源）；`categories` 按内容领域
（`references/taxonomy.md` 的受控两级树，可多属）。一个会话跨多个领域时，**在一篇笔记内用 `threads`
按主题分段**，而不是拆成多篇；每段、每卡各标自己的领域。

命名与排版与既有 vault 对齐：文件名统一 `ID - 标题`、正文 H1 统一 `ID · 标题`；脚本自动在中英文/
数字间补空格、把标签收纳进 `对话沉淀/` 命名空间、普通 frontmatter 值不加多余引号。agent 只需提供
结论性标题与短标签，不要手写 S/C 编号，也不必手工对齐空格。

## 三阶段流水线

| 阶段 | 谁做 | 输入 → 输出 |
| --- | --- | --- |
| A 提取清洗（确定性） | `scripts/extract_sessions.py` | `.sessions/` → `_kb_staging/`（干净转录 + 会话清单） |
| B 语义浓缩（判断） | agent 本人 | staging 转录 → `distill.json` |
| C 渲染落库（确定性） | `scripts/render_notes.py` | `distill.json` → Obsidian 文件 + 校验报告 |

## 标准工作流

### 1. 提取：扫描全部会话，生成干净转录与清单

```bash
SK="<本 skill 目录>"
python3 "$SK/scripts/extract_sessions.py" \
  --out "<工作目录>/_kb_staging" --keep-tool-trail
# 只处理单个会话加：--only <session_id>
```

产出 `_kb_staging/sessions_index.md`（人读清单：时间/首轮需求/轮次/字数）与
`transcripts/<id>.transcript.md`。stdout 的 JSON 给出会话数与字数。脚本零第三方依赖、幂等，
只读 `.sessions`、不修改原始缓存。

### 2. 浓缩：读清单与转录，产出 distill.json

- 先读 `references/distillation-schema.md`（价值分级、卡片类型、字段规范，必读）与
  `references/taxonomy.md`（受控两级领域分类，浓缩前先读、边浓缩边归类）。
- 按 `sessions_index.md` 挑「高/中」价值会话，逐篇读 transcript：抓用户目标 + 各轮最终结论，
  跳过命令流水与工具输出；超大会话只读需求与结论段。
- 判定领域与结构：单一主题走顶层字段；**一个会话跨两个及以上二级领域（杂糅）就用 `threads`
  在一篇笔记内分主题段**，要点/决策/待办/卡片各归其段；每段、每卡按 taxonomy 标 `categories`（可多属）。
- 照 `assets/distill.example.json` 的结构写 `distill.json`。`related` 只链接 vault 中真实存在
  的笔记（先 `find "<vault>" -name '*.md'` 列名），不要自己给卡片编号（脚本统一编号）。

### 3. 渲染：先 dry-run 预览，再正式写入 vault

```bash
python3 "$SK/scripts/render_notes.py" --distill "<工作目录>/distill.json" --dry-run        # 预览
python3 "$SK/scripts/render_notes.py" --distill "<工作目录>/distill.json" \
  --vault "$HOME/Documents/我的知识库/My RAG" --subdir 对话沉淀
```

脚本输出 JSON 报告：写入文件数、`new_categories`（不在 taxonomy 的领域，需回填或改用已有分类）、
`dead_links`（双链目标不存在）、`orphans`（旧笔记本次未覆盖，**不会自动删**）、`warnings`。
正式渲染后这些项必须清零或逐条确认可接受。

### 4. 增量追加（以后有新对话时）

重跑阶段 1 → 只对新会话浓缩、把对象并入已有 distill.json → 重跑阶段 3（会话/卡片幂等覆盖，
MOC 与 base 重建，会话 S 编号、卡片 C 编号按新顺序连续分配）。注意 S/C 编号按 conversations
顺序生成，插入旧会话会重排编号；要保持稳定就把新会话追加在数组末尾。

## 何时读哪个 reference

- 缓存看不懂、要调整提取/清洗规则、排查 transcript 异常：读 `references/source-format.md`。
- 不知道怎么取舍、卡片怎么分类、字段怎么填：读 `references/distillation-schema.md`，
  并对照 `assets/distill.example.json`。
- 不确定内容归哪个领域、要不要新增分类、一个会话该不该分 threads：读 `references/taxonomy.md`。

## 交付前校验清单

1. 阶段 1：`grep -rlE 'system-reminder|TASK_FOCUS|retained_skills' _kb_staging/transcripts`
   应无输出（系统块已剥净）；首轮需求不含系统注入。
2. 阶段 3 报告：`new_categories=[]`（或已把新分类回填 taxonomy）、`dead_links=[]`、`warnings=[]`，
   文件数与 distill 会话/卡片数一致。
3. 抽查 1–2 篇生成笔记：frontmatter 含合法 `categories`、杂糅会话正确分主题段、表格内双链竖线已
   转义、callout 正常、卡片能反链回会话；MOC「按领域浏览」里每张卡只出现在其真正所属领域。
4. frontmatter YAML 合法性（系统 python3 无 PyYAML 时用自带 ruby）：
   `ruby -e 'require "yaml";require "date"; … '` 对生成 md 逐个 safe_load。
5. Obsidian 正在运行会自动索引；提示用户从 `00 · 对话沉淀 MOC` 进入、打开 `.base` 确认视图。

## 边界与安全

- 只读 `.sessions` 原始缓存，**绝不修改或删除**；staging 是中间产物，放在工作目录而非 vault。
- 渲染只覆盖/新增它生成的文件，不删用户手写笔记；要删除请在 Obsidian 内人工进行。
- 浓缩不得编造转录中没有的事实；区分「对话里确认的」与「agent 推断」，拿不准写进 todos 而非结论。
- 对话可能含密钥/隐私（如 API key、令牌）：浓缩时一律不写入笔记，发现则在会话笔记标“含敏感信息，未收录”。
- 默认输出到既有 vault 的独立子目录，不改动其他文件夹；路径以用户实际 vault 为准，勿写死假设。
