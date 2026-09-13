---
name: chat-distiller
description: >-
  把豆包 Work（豆包办公/agent_mode）本地的历史对话、会话缓存、聊天记录浓缩（蒸馏/沉淀）
  成可直接放进 Obsidian 的结构化知识库笔记。当用户说「把历史对话/聊天记录/会话缓存浓缩成
  知识库」「对话转 Obsidian 笔记」「沉淀这些会话」「整理 .sessions / trajectory 对话」
  「把以前的对话做成知识卡片/MOC」时使用。流水线为：脚本确定性提取干净转录 → agent 语义
  浓缩为 distill.json → 脚本确定性渲染成 Obsidian 会话笔记、原子知识卡片、MOC、Bases 视图、
  知识索引（.md / .jsonl）与操作日志 → 脚本做结构一致性与证据核验，agent 按规则做矛盾与过期
  判断。分类词表住在知识库里、跟着数据走。另附压缩触发器（scripts/compact_hook.py）：在
  Codex / Claude Code 的上下文压缩事件上，压缩后注入「先查索引」的提醒、压缩前记下待蒸馏会话；
  当用户说「压缩后 agent 就忘了之前的决定」「让 agent 自己想起知识库」时也用它。输出与既有
  vault 相同的 frontmatter / 双链 / callout 规范。不用于实时对话、飞书云文档导出或网页采集。
---

# 对话缓存 → Obsidian 知识库

把豆包 Work 本地 `.sessions` 里的历史会话，蒸馏成 Obsidian 里「会话笔记 + 原子知识卡片 +
MOC + Bases」的可生长知识网络。**确定性的事交给脚本，需要判断的事（取舍/浓缩/双链）交给 agent。**

## 产物结构（写入目标 vault 的独立子目录，默认名 `对话沉淀`）

```
对话沉淀/
├── 00 · 对话沉淀 MOC.md      # 给人的总导航：会话一览 + 按领域聚合 + 全部卡片
├── 知识索引.md / .jsonl      # 给 agent 与程序化检索的紧凑索引（脚本生成，勿手改）
├── 操作日志.md               # append-only，记录每次摄入新增/更新了哪些 S/C
├── 沉淀索引.base              # 全部/按主题/按领域/高价值/卡片/待跟进 等视图
├── .chat-distiller/          # 本库自带配置：taxonomy.md（词表）、distill.json（渲染源）、pending/（待蒸馏）
├── 会话笔记/Snn - 标题.md      # Snn 稳定短 ID（日期留在 frontmatter），H1 为「# Snn · 标题」
└── 知识卡片/Cnn - 标题.md      # method/fact/decision/lesson/resource，H1 为「# Cnn · 标题」
```

**两套正交分类，别混淆**：`kind` 按知识性质（方法/事实/决策/教训/资源）；`categories` 按内容领域
（**vault 内** `.chat-distiller/taxonomy.md` 的受控两级树，可多属）。一个会话跨多个领域时，**在一篇笔记内用 `threads`
按主题分段**，而不是拆成多篇；每段、每卡各标自己的领域。

**词表跟着数据走**：分类词表住在知识库里，不在 skill 里。首次渲染会从
`references/taxonomy.template.md` 播种一份起始词表；之后增删分类**只改 vault 里那一份**。

命名与排版与既有 vault 对齐：文件名统一 `ID - 标题`、正文 H1 统一 `ID · 标题`；脚本自动在中英文/
数字间补空格、把标签收纳进 `对话沉淀/` 命名空间、普通 frontmatter 值不加多余引号。agent 只需提供
结论性标题与短标签，不要手写 S/C 编号，也不必手工对齐空格。

## 四阶段流水线

| 阶段 | 谁做 | 输入 → 输出 |
| --- | --- | --- |
| A 提取清洗（确定性） | `scripts/extract_sessions.py` | `.sessions/` → `_kb_staging/`（干净转录 + 会话清单） |
| B 语义浓缩（判断） | agent 本人 | staging 转录 → `distill.json` |
| C 渲染落库（确定性） | `scripts/render_notes.py` | `distill.json` → 笔记 + 索引 + 日志 + 校验报告 |
| D 体检（确定性 + 判断） | `scripts/lint_notes.py`，判断项由 agent 按 `references/lint-rules.md` | vault → 结构 / 证据 / 判断三档报告 |

## 标准工作流

### 1. 提取：扫描全部会话，生成干净转录与清单

```bash
SK="<本 skill 目录>"
python3 "$SK/scripts/extract_sessions.py" \
  --out "<工作目录>/_kb_staging" --keep-tool-trail
# 只处理单个会话加：--only <session_id>
```

产出 `_kb_staging/sessions_index.md`（人读清单：时间/首轮需求/轮次/字数/标记）与
`transcripts/<id>.transcript.md`。stdout 的 JSON 给出会话数与字数。脚本零第三方依赖、幂等，
只读 `.sessions`、不修改原始缓存。清单「标记」列出现 `⚠️降级` 时，说明该会话没有
`assignment.md`、用户轮次回退自 trajectory，浓缩时要额外甄别其中的系统注入残留。

### 2. 浓缩：读清单与转录，产出 distill.json

- 先读 `references/distillation-schema.md`（价值分级、卡片类型、字段规范，必读）与
  **vault 词表** `<vault>/对话沉淀/.chat-distiller/taxonomy.md`（受控两级领域分类，浓缩前先读、
  边浓缩边归类；库里还没有时，渲染脚本会先从 `references/taxonomy.template.md` 播种一份）。
- **先看 `.chat-distiller/pending/`**：里面有标记的会话说明它的上下文被压缩过（塞满过窗口），
  值得沉淀的结论大概率就在那几个里，优先处理。
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
  --vault "<vault 路径>" --subdir 对话沉淀
```

脚本输出 JSON 报告：写入文件数、`created`/`updated`、`log_entry`、
`new_categories`（不在词表的领域，需回填或改用已有分类）、
`dead_links`（双链目标不存在）、`orphans`（旧笔记本次未覆盖，**不会自动删**）、`warnings`。
`created`/`updated` 只统计**知识内容**（会话笔记、卡片、MOC）；索引与日志是从内容派生的产物，
单独放在 `meta_changed` 里——首次渲染新库时 `created` 可能是空的，而它们会出现。
`--dry-run` 同样会给出完整的 `dead_links`（已把本次将生成的笔记算作已知目标），
所以预览是真的预览，可以放心用它当写入前的闸门。正式渲染后这些项必须清零或逐条确认可接受。

distill.json 里 **`session_id` 必须唯一且非空**：笔记名、S 编号与卡片归属都以它为键，
重复会让前一个会话的笔记被静默覆盖，脚本会直接报错退出。

同一次渲染还会产出 `知识索引.md` / `.jsonl` 与 `操作日志.md`。渲染是幂等的，
**操作日志只在知识内容真的变化时才追加**——重复重跑不会刷屏。

### 4. 增量追加（以后有新对话时）

重跑阶段 1 → 只对新会话浓缩、把对象并入已有 distill.json → 重跑阶段 3（会话/卡片幂等覆盖，
MOC 与 base 重建，会话 S 编号、卡片 C 编号按新顺序连续分配）。注意 S/C 编号按 conversations
顺序生成，插入旧会话会重排编号；要保持稳定就把新会话追加在数组末尾。

### 5. 体检：结构 + 证据 + 判断

```bash
python3 "$SK/scripts/lint_notes.py" --vault "<vault 路径>" \
  --transcripts "<工作目录>/_kb_staging/transcripts"
```

脚本负责两档：**T1 结构一致性**（编号空洞、字段缺失、反链断裂、索引不一致——重跑渲染即可修复）
和 **T2 证据核验**（卡片里的日期、版本号、数字、路径必须能在对应转录里逐字找到）。
报告里的 `evidence_checked` 是本次实际核对的字面量数——**要区分「0 存疑」和「没跑」**；
不传 `--transcripts` 会跳过证据核验，报告写明 `evidence_skipped`。

第三档 **T3 需要判断力**，由你自己按 `references/lint-rules.md` 执行：矛盾、过期未标注、
缺失交叉引用、粒度失当。**只报告，不要自己改**——渲染是从 distill.json 全量重建的，
直接改 vault 里的 md 会被下次渲染覆盖；要改就改 distill.json 的字段再重跑。

## 查阅：这个知识库怎么被用

知识库的读者有两个——**人和 agent**，入口不同：

- 人：从 `00 · 对话沉淀 MOC` 进，按领域浏览，或在 Obsidian 里打开 `.base` 视图。
- agent：**先读 `知识索引.md`**（一行一条、按领域分组、带状态），定位到候选再打开具体笔记。
  需要按条件过滤时读 `知识索引.jsonl`（每行一个 JSON）。

检索时注意：**`status` 非「现行」的卡片不要当结论用**。索引里这类卡片被单独归到
「已过期 / 有争议」分区，就是为了让 agent 一眼避开。

## 接入：让 agent 在该查的时候想起它

**这一步不做，前面全是白搭。** SKILL.md 只在 skill 被触发时加载，而知识库最该发挥作用的时刻，
恰恰是 agent 在做**别的工作**、需要回想你的历史决策的时候——那时候 skill 根本没被触发。

分两层接，因为这是两件性质不同的事。

### 常态：一句话写进 AGENTS.md

```markdown
## 历史决策

涉及过往项目的方案取舍、技术选型、或你可能已经讨论过的决定之前，
先读 `<vault>/对话沉淀/知识索引.md` 定位相关卡片，不要凭记忆作答。
卡片 `status` 非「现行」的，说明该结论已被推翻，不要当作当前事实。
```

如果用户的 vault 路径已知，主动帮他把这几行加到项目的 `AGENTS.md` 里；加完告诉他加了什么。

### 压缩时：交给 hook，别写在 AGENTS.md 里

AGENTS.md 是**每一轮都在**的静态指令，表达不了「当……的时候」。而上下文压缩恰恰是最该想起
知识库的时刻——那一刻 agent 刚丢掉细节，最容易凭残存的印象编。

Codex 与 Claude Code 都为此提供了同名事件，`scripts/compact_hook.py` 两个都适配：

| 事件 | 何时触发 | 脚本做什么 |
| --- | --- | --- |
| `SessionStart`，matcher `compact` | 压缩刚发生、**下一次模型请求之前** | 把提醒打到 stdout——两个工具都会把这段纯文本注入模型上下文 |
| `PreCompact`，matcher `manual\|auto` | 压缩之前 | 在 `.chat-distiller/pending/` 写一条标记：这个会话的上下文溢出过 |

配置模板见 `assets/hooks.example.json`（两个工具是同一个 `{"hooks": {…}}` 形状），把里面的
skill 路径与 vault 路径换成你的，然后：

- **Codex**：放进 `~/.codex/hooks.json`（或项目里的 `.codex/hooks.json`）。首次要在 `/hooks` 里
  审阅并信任一次——没被信任的 hook 会被跳过。
- **Claude Code**：放进 `~/.claude/settings.json`（或项目里的 `.claude/settings.json`），
  用 `/hooks` 确认已注册（那个菜单是只读的，改动直接编辑 JSON）。

脚本的约定是**失败必须无声**：一切异常都 exit 0，hook 出错绝不能打断用户正在进行的会话；
默认只在**索引确实存在**时才注入，库还没建起来时它不会说废话。排查用 `--debug`。

注入的提醒刻意很短（约 150 token，两个工具的默认上限都在 2500 token 量级）：hook 上下文
会叠加进每一轮，写长了会挤占真正的工作内容。

## 何时读哪个 reference

- 缓存看不懂、要调整提取/清洗规则、排查 transcript 异常：读 `references/source-format.md`。
- 不知道怎么取舍、卡片怎么分类、字段怎么填：读 `references/distillation-schema.md`，
  并对照 `assets/distill.example.json`。
- 不确定内容归哪个领域、要不要新增分类、一个会话该不该分 threads：读 vault 词表
  `<vault>/对话沉淀/.chat-distiller/taxonomy.md`。
- 要跑体检、判断哪两张卡矛盾或哪张卡过期了：读 `references/lint-rules.md`（T3 判断项）。

## 交付前校验清单

1. 阶段 1：`grep -rlE 'system-reminder|TASK_FOCUS|retained_skills' _kb_staging/transcripts`
   应无输出（系统块已剥净，回退路径同样如此）；首轮需求不含系统注入。
2. 阶段 3 报告：`new_categories=[]`（或已把新分类回填 **vault 词表**）、`dead_links=[]`、`warnings=[]`，
   文件数与 distill 会话/卡片数一致。
3. 抽查 1–2 篇生成笔记：frontmatter 含合法 `categories`、杂糅会话正确分主题段、表格内双链竖线已
   转义、callout 正常、卡片能反链回会话；MOC「按领域浏览」里每张卡只出现在其真正所属领域。
4. frontmatter YAML 合法性（系统 python3 无 PyYAML 时用自带 ruby）：
   `ruby -e 'require "yaml";require "date"; … '` 对生成 md 逐个 safe_load。
5. Obsidian 正在运行会自动索引；提示用户从 `00 · 对话沉淀 MOC` 进入、打开 `.base` 确认视图。
6. 索引与日志：`知识索引.md` 里现行卡片都在列、`status` 非现行的卡只在「已过期 / 有争议」
   分区出现；卡片总数与 `知识索引.jsonl` 行数一致。原样重跑一次渲染，`log_entry` 应为 `null`
   （幂等：内容没变就不追加日志）。
7. 阶段 4 体检：`lint_notes.py` 的 T1 应报 0 项（非 0 说明得重跑渲染）；T2 报出的是「存疑」
   候选，需逐条回原始转录核对——命中不等于错误，卡片是转述不是引用。

改动过脚本或规则后，先跑一遍自带回归测试（只用标准库）：
`python3 -m unittest discover -s tests`。

## 边界与安全

- 只读 `.sessions` 原始缓存，**绝不修改或删除**；staging 是中间产物，放在工作目录而非 vault。
- 渲染只覆盖/新增它生成的文件，不删用户手写笔记；要删除请在 Obsidian 内人工进行。
- 浓缩不得编造转录中没有的事实；区分「对话里确认的」与「agent 推断」，拿不准写进 todos 而非结论。
- 对话可能含密钥/隐私（如 API key、令牌）：浓缩时一律不写入笔记，发现则在会话笔记标“含敏感信息，未收录”。
- 默认输出到既有 vault 的独立子目录，不改动其他文件夹；路径以用户实际 vault 为准，勿写死假设。
