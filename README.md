# chat-distiller

把豆包 Work（Doubao Work / agent_mode）本地的历史会话缓存，**蒸馏（distill）**成结构化、可生长的
Obsidian 个人知识库：会话笔记 + 原子知识卡片 + MOC 导航 + Bases 索引，并带受控两级领域分类与多主题分段。

> 确定性的事交给脚本，需要判断的事（取舍 / 浓缩 / 归类 / 双链）交给 agent。

## 流水线

| 阶段 | 执行者 | 输入 → 输出 |
| --- | --- | --- |
| A 提取清洗（确定性） | `scripts/extract_sessions.py` | `.sessions/` → 干净转录 + 会话清单 |
| B 语义浓缩（判断） | agent（规则见 `references/`） | 转录 → `distill.json` |
| C 渲染落库（确定性） | `scripts/render_notes.py` | `distill.json` → Obsidian 笔记 + 校验报告 |

## 目录结构

```
chat-distiller/
├── SKILL.md                        # 入口：触发条件与标准工作流
├── scripts/
│   ├── extract_sessions.py         # 阶段 A：确定性提取/清洗，零第三方依赖
│   └── render_notes.py             # 阶段 C：确定性渲染/校验，零第三方依赖
├── references/
│   ├── source-format.md            # 豆包 Work 会话缓存格式说明书
│   ├── distillation-schema.md      # 浓缩规则与 distill.json schema
│   └── taxonomy.md                 # 受控两级领域分类树
└── assets/
    └── distill.example.json        # distill.json 完整样例（含多主题分段）
```

## 特性

- **零第三方依赖**：两个脚本只用 Python 标准库；同一输入幂等产出同一结果。
- **两套正交分类**：`kind`（方法 / 事实 / 决策 / 教训 / 资源）× `categories`（受控两级领域，可多属）。
- **杂糅会话分段**：一个会话跨多个领域时，在一篇笔记内按 `threads` 分主题段，而不是拆成多篇。
- **与既有 vault 对齐**：统一 frontmatter、双链、callout 与命名；渲染只增 / 覆盖、不删除手写内容，
  并自动做死链、孤儿文件、新分类与 YAML 校验。
- **只读原始缓存**：阶段 A 绝不修改 `.sessions`。

## 用法

完整工作流见 `SKILL.md`。脚本也可独立运行：

```bash
# A. 提取干净转录
python3 scripts/extract_sessions.py --out ./_kb_staging

# B.（由 agent 依据 references/distillation-schema.md 与 taxonomy.md 把转录浓缩为 distill.json）

# C. 先预览再正式写入 vault
python3 scripts/render_notes.py --distill ./distill.json --dry-run
python3 scripts/render_notes.py --distill ./distill.json \
  --vault "/path/to/your/vault" --subdir 对话沉淀
```

## 适用范围与边界

- 面向豆包 Work 本地 `.sessions` 缓存结构；不用于实时对话、在线文档导出或网页采集。
- 产物为本地 Markdown，可纳入 Git 版本管理，自主可控、可迁移到任意兼容 Markdown / 双链的笔记工具。
