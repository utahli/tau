# 面向初学 Agent 开发的学习计划——以 Tau coding 层为核心案例

> 本文档是一份**课程式学习计划**：面向刚接触 Agent 开发（不一定熟悉 Tau）的开发者，
> 用 Tau 的 coding 层（`tau_coding`）作为贯穿全程的"活的参考答案"，从概念出发，
> 逐步掌握 Agent 系统设计的核心骨架，并最终完成一个可验证的毕业项目。
>
> 所有知识点均基于当前源码快照（`src/`）核实，不编造 API 与行为；源码演进时以
> 测试与公开 API 为准。

---

## 0. 这份计划是什么

### 0.1 它与仓库已有学习文档的分工

仓库里已有两类学习材料，这份计划与它们互补、不重复：

| 文档 | 视角 | 起点 | 适合谁 |
| --- | --- | --- | --- |
| `00-总览与学习路线.md` | Tau 优先 | 先把三层架构跑起来 | 想快速了解 Tau 全貌的人 |
| `01/02/03-*层手册` | 源码优先 | 逐文件精读 | 想按文件啃实现的人 |
| `coding-layer/README.md`（14 天计划） | 源码优先 | 已懂 tau_agent / tau_ai | 想精读 coding 层实现的人 |
| **本文档（06）** | **概念优先** | 懂 Python、想入门 Agent 开发 | 想"从 Agent 概念出发、用 Tau 当例子"的初学者 |

一句话区别：**前几份回答"Tau 是怎么写的"，这份回答"一个 Agent 系统应该怎么学、各部件为什么长这样"**——
每一个 Agent 开发的核心概念（事件、工具、上下文、记忆、信任、扩展），都先用大白话讲清为什么，
再到 `tau_coding` 里找到它的真实实现，最后用一个练习把它变成自己的肌肉记忆。

### 0.2 学完本计划后，你应该能

- 画出"前端 → CodingSession → AgentHarness → ModelProvider"的完整数据流，讲清每层边界；
- 说清 事件、工具、循环、上下文预算、会话持久化、项目信任、扩展钩子 这 7 个概念各自解决什么问题；
- 给 Tau 新增一个内置工具 / 一个扩展，并配套测试；
- 用 `tau_ai.fake.FakeProvider` 离线跑通一个 Agent 循环，不花一分钱、不依赖网络；
- 读懂 `tests/` 里任何与 coding 层相关的测试，并能照葫芦画瓢写新测试；
- 把"Tau 的哪段代码对应哪个通用设计"翻译成你自己的话（费曼检验）。

### 0.3 总时长与节奏

- 建议 **3 周**（每天 2~4 小时），7 个阶段 + 毕业项目；
- 每阶段可独立暂停；阶段 4、5 依赖前序，其余基本并行；
- 每天固定"读 → 讲 → 做"循环（见 §5 学习方法）。

---

## 1. 前置准备

### 1.1 知识门槛（不是硬性，但建议具备）

| 领域 | 需要到什么程度 | 缺了怎么办 |
| --- | --- | --- |
| Python | 3.12 语法（`type` 语句、`match`、dataclass）、装饰器 | 先花半天过一遍 py3.12 新特性 |
| asyncio | 知道 `async def` / `await`、异步生成器 | 边读 `prompt()` 边补 |
| 数据格式 | JSON / JSONL 读写 | 直接用 Python 读 `~/.tau/sessions/` 练 |
| LLM API | 知道"对话 = 消息列表 + 流式输出" | 阶段 5 会补齐 |

### 1.2 环境搭建（一次搞定）

```bash
git clone https://github.com/huggingface/tau.git
cd tau
uv sync --dev                          # 运行 + 开发依赖
uv run tau --version                   # 验证
uv run tau -p "hi"                     # 若无 provider 会提示登录，不阻塞
```

不用急着配真实模型：**整个计划里 90% 的实验可以离线完成**，用 `tau_ai.fake.FakeProvider`
（`tests/` 的标配）和内存 storage 就能确定性跑通循环。

### 1.3 心智模型（先背这三行）

```text
tau_coding  →  tau_agent  →  tau_ai
（终端 App）    （可复用大脑）   （模型传输层）
```

更精确的边界（来自 `README.md` 与 `src/tau_coding/session.py` 的类 docstring）：

```text
AgentHarness  = 可复用的大脑（tau_agent）
CodingSession = 编码 Agent 环境（tau_coding）
TUI           = 众多可能前端之一（tau_coding.tui）
```

---

## 2. Coding 层核心设计速览（8 个要点）

这 8 点是理解整个 coding 层的钥匙。每一点都是"通用设计 → Tau 的实现"：

| # | 设计要点 | 一句话 | 源码位置（核实过） |
| --- | --- | --- | --- |
| 1 | **分层与边界** | CodingSession 包装 AgentHarness：大脑不知道文件系统、Textual、`~/.tau` | `session.py:417` 类 docstring；`README.md` |
| 2 | **事件是契约** | 前端只消费 `CodingSessionEvent`，不直接碰 harness 内部 | `events.py`（10 个 session 事件）；`rendering/base.py:EventRenderer`；`tui/adapter.py` |
| 3 | **工具 = schema + 异步执行器** | 一个工具 = 参数定义 + `async execute(arguments, signal) -> AgentToolResult` | `tools.py:96 ToolDefinition`；`tau_agent/tools.py:AgentTool` |
| 4 | **会话 = 追加式 JSONL + replay** | 历史只追加不改写；启动时从条目树重建状态 | `session.py:load()`；`tau_agent/session/` |
| 5 | **装配 = 候选优先** | 先"准备"（不落盘），确认信任后再"采纳" | `session_preparation.py`（`prepare_coding_session` / `PreparedCodingSession.adopt`） |
| 6 | **信任先于资源** | 未信任项目的 AGENTS.md / SYSTEM.md / skills 不进入运行时 | `session.py:load()` 中 trust resolve 后调 `resources.resource_paths_with_project_trust` |
| 7 | **上下文是显式管理的资源** | 有 token 估算、自动/溢出/手动三种压缩、会话命名 | `context_window.py`；`session.py:_try_auto_compact` 等 |
| 8 | **一切可扩展、可观测** | 输入/工具钩子、UI bridge、provider 注册、诊断日志、`SystemPromptInspection` | `extensions/api.py`、`extensions/loader.py`、`extensions/runtime.py`、`system_prompt.py:48` |

> 阅读技巧：`session.py` 有 5260 行，**不要从头线性读**。永远走
> "公开入口（`load` / `prompt`）→ 状态（frozen dataclass）→ 只追入口调到的 helper"的漏斗。

---

## 3. 学习路线总图

```text
阶段 0  认识一个 Agent 系统            （半天）  跑起来 + 画十步链路
   │
阶段 1  事件与消息：通用语言          （1 天）   三层事件、四种输出模式
   │
阶段 2  工具：Agent 的手              （2 天）   写一个自己的工具
   │
阶段 3  装配与信任：接入真实世界      （2 天）   资源发现 + 系统提示 + 候选装配
   │
阶段 4  会话与上下文：记忆            （2 天）   JSONL replay + 压缩 + 分支恢复
   │
阶段 5  Provider 与身份：模型从哪来   （1.5 天） catalog → 配置 → 凭据 → 客户端
   │
阶段 6  扩展与前端：生态与可移植      （2 天）   写一个扩展 + 换一个前端
   │
毕业项目                              （2~3 天） 四选一，代码 + 测试 + 讲解
```

完整可视化（HTML/SVG 交互图）见同目录 `assets/learning-path.html`，或本系列配套网页。

---

## 4. 分阶段学习计划

每阶段统一五件套：**目标 / 概念 / 必读 / 练习 / 自测**。自测题的答案都能在对应源码里找到，
答不上来就回到"必读"里找。

---

### 阶段 0 · 认识一个 Agent 系统（0.5~1 天）

**目标**：能启动 Tau；能画出"用户输入 → 前端 → CodingSession → AgentHarness → ModelProvider → 回包"的完整链路；分清三层职责。

**概念**（大白话版）：

- **Agent**：一个"循环"，模型在循环里决定"直接回答还是调用工具"，工具结果再喂回模型，直到它觉得完成了。
- **大脑 / 环境 / 前端**：大脑只管推理与调用（`tau_agent`）；环境提供工具、文件、会话（`tau_coding`）；前端只是把事件画出来（TUI / print / RPC / 自研）。

**必读**：

| 材料 | 看什么 |
| --- | --- |
| `README.md` | "What is Tau" 与 Philosophy 六条 |
| `src/tau_coding/cli.py:193 main()` | 一个 Typer callback 如何同时服务 TUI 与 print 模式 |
| `dev-notes/learning-guide/00-总览与学习路线.md` | 三层架构总图、模块依赖图 |
| `dev-notes/design/01-architecture.md` | 架构设计原文 |

**练习**：

```bash
cd /path/to/any-project
uv run tau -p "列出当前目录的文件" --mode transcript   # 看事件流
uv run tau -p "hi" --mode json                        # 看结构化事件
ls ~/.tau/sessions/                                   # 找到刚才的 JSONL 文件
cat ~/.tau/sessions/<id>/session.jsonl                # 用 head 看条目结构
```

**自测**（答不出就回炉）：

1. 一次 `tau -p` 请求，按顺序经过了哪几层？哪一层负责"决定要不要用工具"？
2. `--mode text / json / transcript / rpc` 四种输出分别给谁用？
3. 为什么说"核心不知道 Textual"是设计而非缺陷？

**里程碑**：在白板上画出十步链路图（含：CLI 解析 → 装配 session → harness 循环 → provider 流 → 事件 → 渲染）。

---

### 阶段 1 · 事件与消息：Agent 的通用语言（1 天）

**目标**：分清"provider 事件 / agent 事件 / session 事件"三层；理解为什么**事件是跨层契约**。

**概念**：

- 一个 Agent 系统里最容易被搞混的就是"事件"。Tau 用三层事件解决：
  - `tau_ai` 内部流事件（HTTP 流碎片，`_provider_events.py`）——**不外传**；
  - `tau_agent` 的 `AgentEvent`（`MessageEndEvent`、`ToolExecutionEndEvent`、`AgentEndEvent`）——大脑的"广播"；
  - `tau_coding` 的 `CodingSessionEvent`（`SessionAgentEndEvent`、`CompactionStartEvent`、`AgentSettledEvent`……）——session 层补充的"运维事件"。
- 前端（TUI、print 渲染器、RPC）**只认 `CodingSessionEvent`**，这就是"事件是契约"：换前端不用改大脑。

**必读**：

| 文件 | 关键符号 |
| --- | --- |
| `tau_agent/events.py` | `AgentEvent` 联合类型 |
| `tau_agent/provider_events.py` | `AssistantMessageEvent`（provider 级） |
| `tau_coding/events.py`（全文仅 10 个类，91 行） | `SessionAgentEndEvent` / `AgentSettledEvent` / `CompactionStart/EndEvent` / `AutoRetryStart/EndEvent` 等 |
| `tau_coding/rendering/base.py` | `EventRenderer` 协议（`render` / `finish`）、`PrintOutputMode` |
| `tau_coding/rendering/plain.py` | `FinalTextRenderer`：只挑 `AssistantMessage` 的事件画出来 |

**练习**：

1. 用 `--mode json` 跑一次请求，把返回的每个事件类型抄下来，逐个标出"它来自哪一层、给谁消费"。
2. 写一个 10 行的 `CountingRenderer`（实现 `EventRenderer` 协议），统计一次运行里每种事件出现次数——放进 `tests/` 或一个临时脚本里跑 `--mode rpc` 喂给它。
3. 读 `tests/test_rendering.py`，看官方怎么测渲染器。

**自测**：

1. `SessionAgentEndEvent` 和 `AgentEndEvent` 有什么区别？为什么 session 层要再包一层？
2. 渲染器为什么**不能**直接读 harness 的消息列表？违反了哪个边界？
3. `FinalTextRenderer.finish()` 返回 `bool` 表示什么？

**里程碑**：能对着 `--mode json` 的输出，解释每一类事件何时产生、谁消费它。

---

### 阶段 2 · 工具：Agent 的手（2 天）

**目标**：吃透"工具 = schema + 异步执行器"；独立新增一个工具并通过 `AgentTool` 协议。

**概念**：

- 工具是把模型"会说话"变成"能做事"的桥：模型看到工具 schema（名字、参数、说明），按 JSON 参数调用；执行器在沙箱侧真正干活并返回结构化结果。
- 真实工具的第一课是**防御**：参数校验（`ToolInputError`）、输出截断（行数/字节双上限）、路径解析（相对 `cwd`）、并发安全（同路径写锁）、多模态降级（图片检测失败时给文字说明）。

**必读**：

| 文件 | 关键符号 |
| --- | --- |
| `tau_agent/tools.py` | `AgentTool` 协议（schema + executor） |
| `tau_coding/tools.py:164 create_coding_tools` | 顺序：read → write → edit → bash |
| `tools.py:188 create_read_tool_definition` | UTF-8、`offset`/`limit` 切片、`DEFAULT_MAX_OUTPUT_LINES/BYTES` 截断、图片检测为多模态块 |
| `tools.py:478 create_edit_tool_definition` | 为什么要求 `oldText` **唯一**；`apply_edits_to_normalized_content`；行尾还原 |
| `tools.py:588 create_bash_tool_definition` | `shell_command_prefix` 前缀、状态块 |
| `tests/test_coding_tools.py` | 工具行为测试范式 |

**练习**（重头戏，2 天内完成）：

1. 照 `create_read_tool` 的骨架，写一个 `list_dir` 工具：列出 `cwd` 下某目录条目，返回名称 + 类型 + 大小，做 `ToolInputError` 校验（路径不存在、不是目录、超长截断）。
2. 把它接进 `create_coding_tools` 的返回列表，用 fake provider 跑一次，确认模型能调用它。
3. 跑 `uv run pytest tests/test_coding_tools.py`，把新增工具的测试补在同样式下。

**自测**：

1. `edit` 为什么要求 `oldText` 唯一？不唯一会发生什么？（读实现里的查找逻辑）
2. `bash` 工具的 `shell_command_prefix` 是为了解决什么问题？
3. `read` 为什么既要限行数又要限字节数？截断后返回了什么提示给模型继续读？

**里程碑**：你的 `list_dir` 工具通过 `AgentTool` 协议、有测试、有文档字符串，能被模型在真实循环中调用。

---

### 阶段 3 · 装配与信任：大脑如何接入真实世界（2 天）

**目标**：读懂 `CodingSession.load()` 的全流程；理解"先信任、后给资源"这条铁律。

**概念**：

- **装配**：一个 Agent 环境不是一拍脑袋就能用的——要决定工作目录（cwd）、发现项目资源（AGENTS.md / SYSTEM.md / skills）、解析项目信任、组装系统提示、创建 provider、最后把这一切喂给大脑。
- **信任**：让一个 Agent 在你的仓库里读文件、跑命令是有风险的。Tau 的答案：**未信任项目，其项目级资源（指令、技能）不进入运行时**；信任决定必须在任何资源生效之前完成。
- **候选优先（prepare → adopt）**：先在不落盘的情况下装配一个"候选 session"，信任决策被取消就 `abort()`，成功才 `adopt()` 提交——避免"半成品先写进磁盘"。

**必读**：

| 文件 | 关键符号 |
| --- | --- |
| `session.py:509 load()` | 全流程：read entries → `SessionState.from_entries` → 建 `ExtensionRuntime` → trust resolve → canonical cwd → 资源过滤 → 加载系统提示 → 建 harness |
| `session_preparation.py`（全文 88 行） | `prepare_coding_session` → `PreparedCodingSession.adopt/abort` |
| `resources.py` | `discover_system_prompt_resources`（SYSTEM.md 替换式 / 累积式）、`resource_paths_with_project_trust` |
| `context.py` | `discover_project_context`（AGENTS.md 等）、`_find_project_root` |
| `system_prompt.py:73 build_system_prompt` | 系统提示 = 基础段 + 项目上下文 + 技能索引 + 工具说明 + 指南；`SystemPromptInspection` 可查每个 section 来源 |
| `tests/test_resources.py`、`tests/test_project_trust.py` | 官方行为测试 |

**练习**：

1. 建一个测试目录，放 `AGENTS.md` 和 `.tau/SYSTEM.md`，用 `--mode json` 跑请求，对比系统提示前后变化；再用 `/resources`、`/system` 斜杠命令查看装配结果。
2. 在受信任与未信任两种状态下启动，观察项目资源是否进入系统提示（读 `resource_paths_with_project_trust` 的实现确认）。
3. 手工画 `load()` 时序图：每个步骤"读了什么 → 决定了什么 → 影响了什么"。

**自测**：

1. 为什么 trust 决定必须在资源加载**之前**？若顺序反过来会有什么安全漏洞？
2. `prepare → adopt` 解决了什么问题？（提示：读 `defer_authoritative_writes`）
3. 系统提示的每个 section 来源是什么？`/system` 命令能看到什么？

**里程碑**：能给新人讲清"一个不可信目录里，Tau 会拒绝把它的 AGENTS.md 塞进模型上下文"。

---

### 阶段 4 · 会话与上下文：Agent 的记忆（2 天）

**目标**：理解追加式 JSONL、replay、上下文预算与三种压缩；会用分支与恢复。

**概念**：

- **追加式历史**：每条会话条目只追加、不改写；启动时 `SessionState.from_entries` 把条目树 replay 成内存状态。好处：可恢复、可审计、可压缩而不破坏原始记录。
- **上下文预算**：模型窗口有限，`context_window.py` 负责估算 token（文本/消息/工具），并在接近阈值时触发压缩。
- **压缩（compaction）**：把旧消息总结成摘要替换进活动上下文，但 JSONL 原始记录保留——所以"压缩后还能回滚/继续分支"。
- **分支与恢复**：条目用 `parent_id` 形成树；`/tree` 看分支，`--session` 恢复，`branch_to_entry` 从任意节点另起一行。

**必读**：

| 文件 | 关键符号 |
| --- | --- |
| `session.py` 的 `load` / `prompt` 持久化段 | `_attach_persistence_listener` → `_persist_on_message_end` → `_persist_message`；`_reconcile_run_persistence` 兜底 |
| `session.py` 压缩相关 | `_try_auto_compact`（prompt 前后）、`_try_overflow_compact`（溢出重试）、`compact_detailed`（手动） |
| `session.py` 分支恢复 | `branch_to_entry` / `resume` / `new_session` |
| `context_window.py` | `estimate_context_tokens`、`auto_compaction_threshold_for_context_window`、`summarize_messages_for_compaction` |
| `tau_agent/session/entries.py` | 条目类型与 `parent_id` 树 |
| `tests/test_coding_session.py`、`tests/test_context_window.py`、`tests/test_session_manager.py` | 官方测试 |

**练习**：

1. 用 Python 直接读一个 `session.jsonl`，写 20 行代码把它 replay 成"最近 3 轮对话"——这就是 `SessionState.from_entries` 的迷你版。
2. 跑一个长对话触发自动压缩（或手动 `/compact`），对比压缩前后 `--mode json` 里的事件；观察 `CompactionStartEvent/CompactionEndEvent`。
3. `uv run tau --session <id>` 恢复会话；`/tree` 建一个分支，往分支里提问，确认原始路径没被污染。

**自测**：

1. 事件**何时**写进 JSONL？（答：消息结束事件时，而不是每帧；还有兜底 reconcile）
2. 为什么"追加式"比"改写式"可靠？压缩为什么不用改写历史文件？
3. `parent_id` 树里，"活动叶子"决定什么？

**里程碑**：能从 JSONL 手工重建会话状态；能解释压缩、分支、恢复各自的数据结构支撑。

---

### 阶段 5 · Provider 与身份：模型从哪来（1.5 天）

**目标**：走通 `catalog → 配置 → 凭据 → 客户端` 的数据流；会用 `tau setup` 接本地模型；会用 FakeProvider 离线测试。

**概念**：

- **Catalog**（目录）：`data/catalog.toml` 声明支持的 provider / model 元数据，用户可放 `~/.tau/catalog.toml` 扩展——**不改代码加模型**。
- **定义 / 偏好 / 秘密三分**：provider 定义（是什么）与用户偏好（默认用哪个）与凭据（密钥）分开存，避免把密钥混进配置文件。
- **运行时边界**：token 刷新、客户端创建发生在请求边界；`create_model_provider` 是唯一工厂，`ClosableModelProvider` 保证用完即关。

**必读**：

| 文件 | 关键符号 |
| --- | --- |
| `provider_catalog.py` | `ProviderCatalogEntry`、内置目录加载 |
| `data/catalog.toml` | 看一眼真实 schema |
| `provider_config.py` | 定义与偏好的持久化 |
| `credentials.py` | `FileCredentialStore`（秘密单独存） |
| `provider_runtime.py:212 create_model_provider` | 工厂：OpenAI 兼容 / Anthropic / Codex / 本地 |
| `provider_runtime.py:465 OAuthRuntimeCredentialResolver` | 请求边界的 token 刷新 |
| `tau_ai/fake.py` | **FakeProvider**：离线驱动一切测试的钥匙 |
| `tests/test_provider_runtime.py`、`tests/test_credentials.py` | 官方测试 |

**练习**：

1. `uv run tau setup --base-url http://localhost:8080/v1 --api-key-env LOCAL_KEY -m my-local-model`，然后打开 `~/.tau/catalog.toml` 和 `~/.tau/providers.json` 看写入结果。
2. 写一个 pytest：用 `FakeProvider` + 内存 storage 装配 `CodingSession`，跑一次 `prompt()`，断言收到 `SessionAgentEndEvent`——**全程离线**。这是你后续所有实验的模板。
3. 读 `tests/test_coding_session.py` 里用 fake provider 的用例，抄一个改造版。

**自测**：

1. 定义、偏好、秘密为什么必须分开存？合在一起会出什么问题？
2. token 刷新为什么放在请求边界，而不是启动时一次？
3. `FakeProvider` 为什么是"理解 Agent 循环最好的活文档"？

**里程碑**：能画"从 catalog 条目到可调用 ModelProvider"的数据流，并有一个可复用的离线会话测试模板。

---

### 阶段 6 · 扩展与前端：生态与可移植性（2 天）

**目标**：理解扩展钩子模型与"一个事件流、多个前端"；写出自己的第一个扩展。

**概念**：

- **扩展 = 受控的钩子**：扩展不能直接改 harness 消息或 JSONL，只能通过 `ExtensionContext` 暴露的钩子参与：输入钩子（改写/拦截用户输入）、工具调用钩子、工具结果钩子、会话生命周期事件、UI bridge（通知/选择器/侧栏）。
- **加载与生命周期**：`extensions/loader.py` 负责发现与加载（目录 + manifest），`extensions/runtime.py` 用 overlay/layer 模型管理 provider 注册——比"全局注册表"更安全（可隔离、可卸载、按路径分层）。
- **前端可移植**：print / JSON / RPC / TUI 消费同一事件流。`rpc.py` 是无头 JSONL 协议，适合自动化；`tui/adapter.py` 证明 Textual 只是"一个前端"。

**必读**：

| 文件 | 关键符号 |
| --- | --- |
| `extensions/api.py` | `InputEvent`/`InputHookResult`、`ToolCallHookEvent`、`ToolResultHookEvent`、`UiBridge`、`ExtensionCommandContext`、`CustomMessageView` |
| `extensions/loader.py` | `discover_extensions` / `load_extensions` |
| `extensions/runtime.py` | `ExtensionRuntime`（钩子调度、provider 注册、`decide_project_trust`） |
| `data/examples/extensions/hello_tool.py` | **最小扩展模板**（照抄就能跑） |
| `data/examples/extensions/ask_user_question.py`、`prompt_section.py`、`sidebar_status.py` | 更多示例 |
| `rendering/`、`rpc.py`、`tui/adapter.py` | 四种前端如何共享事件 |
| `tests/test_extensions.py`、`tests/test_rpc.py`、`tests/test_rendering.py` | 官方测试 |

**练习**：

1. 照 `hello_tool.py` 写一个扩展：注册一个工具（如 `get_time`）**或**一个输入钩子（把用户输入里的 `@today` 展开成日期）。装进 `~/.tau/extensions/` 或项目 `.tau/extensions/`，用 `--mode json` 验证它生效。
2. 用 `--mode rpc` 写一个 5 行的 Python 客户端：发一条消息、读事件流、打印 `AgentSettledEvent`。
3. 读 `tui/adapter.py`，列出"TUI 消费了哪些事件、调用了 session 哪些公共方法"——验证它没有触碰 harness 内部。

**自测**：

1. 扩展为什么不能直接改 harness 消息？它绕过了哪条边界？
2. overlay/layer 模型比全局注册表安全在哪？
3. 新增一种前端需要改大脑吗？为什么？

**里程碑**：你的扩展在真实 Tau 里跑起来并被模型调用；你能解释"换前端不动大脑"。

---

### 毕业项目（2~3 天，四选一）

| 项目 | 做什么 | 检验什么 | 参照 |
| --- | --- | --- | --- |
| **A. 给 Tau 加工具** | 新增一个生产级工具（如 `fetch_url`：超时、大小限制、错误分类）+ 测试 + 文档 | 阶段 2 的完整闭环 | `tools.py` + `tests/test_coding_tools.py` |
| **B. 写一个迷你 Agent** | 用 `tau_agent` 的 harness/loop 骨架（或复刻其思路），配 `FakeProvider` 和 2 个工具，跑通"问答 → 工具 → 汇总" | 阶段 1/2/4 的循环、消息、事件 | `tau_agent/loop.py` + `tau_ai/fake.py` |
| **C. 写一个自定义前端** | 一个终端渲染器（文本/JSON 之外的新格式），消费 `CodingSessionEvent`，配 `--mode` 接入 | 阶段 1/6 的事件契约 | `rendering/` + `cli.py` 的 mode 分发 |
| **D. 写一个扩展** | 输入钩子 + 工具钩子 + 侧栏/通知组合的完整扩展 | 阶段 6 的钩子模型 | `extensions/api.py` + 示例 |

**每个项目的交付要求（缺一不可）**：

- 可运行代码（放在你自己的工作目录，不污染 `src/`）；
- 至少 2 个测试（`uv run pytest` 通过）；
- 3 分钟费曼讲解：向一位不会 Python 的同事讲清"它接收什么、保证什么、失败时怎样"；
- 一段架构说明：它落在 Tau 的哪一层、依赖哪个契约、为什么这样设计。

---

## 5. 学习方法与工具

### 5.1 每天的费曼循环（coding-layer/README.md 同款）

1. **读**：只读当天的目标文件（用 §4 的"必读"清单）；
2. **讲**：合上源码，用自己的话讲 3 分钟，讲不清的名词回到代码补洞；
3. **做**：做当天练习，跑关联测试；
4. **记**：每次笔记只回答四个问题——它接收什么、保证什么、失败时怎样、谁依赖它。

### 5.2 测试是活文档

`tests/` 里 71 个测试文件就是最好的"行为规格书"。学习时永远先看测试怎么用某个 API，再回源码验证。

- 循环与 harness：`test_agent_loop.py`、`test_agent_harness.py`
- coding 层：`test_coding_session.py`、`test_coding_tools.py`、`test_resources.py`、`test_system_prompt.py`、`test_project_trust.py`、`test_context_window.py`、`test_commands.py`、`test_extensions.py`、`test_provider_runtime.py`、`test_rendering.py`、`test_rpc.py`、`test_session_export.py`、`test_cli.py`

### 5.3 离线优先

不要在学习期连真实模型。`tau_ai/fake.py:FakeProvider` 回放预设事件流，让循环完全确定性运行；内存 storage（`test_coding_session.py` 里常见）替代磁盘。**省钱、省时间、可断言**。

### 5.4 对照 dev-notes 阶段笔记

`dev-notes/architecture/phase-*.md` 是"按构建顺序"写的开发日志——想理解某个模块为什么长这样，找对应 phase 笔记；`dev-notes/adr/` 是架构决策记录（如 `0001-use-textual-for-tui.md`）。先读设计结论，再读代码，理解速度翻倍。

### 5.5 一次只动一个变量

改代码后只跑最小测试集（如 `uv run pytest tests/test_coding_tools.py`），而不是全量 `pytest`。全量在里程碑和毕业项目时再跑。

---

## 6. 毕业自测清单（20 题）

逐条打勾，全部能答出并能在源码里指出依据，才算毕业：

**分层与事件（1–5）**

1. 画出 tau_coding → tau_agent → tau_ai 的数据流，标出依赖方向。
2. 说出 3 个 `CodingSessionEvent` 并解释各自何时产生。
3. 前端为什么只消费事件、不碰 harness？
4. provider 事件与 agent 事件分层的意义？
5. `--mode json / transcript / rpc / text` 各自对应什么消费者？

**工具（6–9）**

6. "工具 = schema + 异步执行器"在代码里是哪两个东西？
7. `ToolInputError` 解决了什么问题？
8. edit 的 `oldText` 唯一性要求如何保证正确性？
9. 工具输出截断的双上限是什么？

**装配与信任（10–13）**

10. `load()` 中 trust 决定发生在资源加载之前还是之后？为什么？
11. `prepare → adopt` 避免了什么半成品状态？
12. SYSTEM.md 与 AGENTS.md 分别从哪个目录发现？信任如何过滤？
13. `build_system_prompt` 的 sections 有哪些？

**会话与上下文（14–17）**

14. 消息何时写入 JSONL？（精确到事件类型）
15. 追加式历史 + replay 如何支撑压缩与分支？
16. 三种压缩（auto / overflow / manual）各自何时触发？
17. `parent_id` 树中"活动叶子"决定什么？

**Provider 与扩展（18–20）**

18. catalog → config → credentials → runtime 各自职责？
19. 扩展能通过哪些钩子参与？不能做什么？
20. 新增一个前端需要改 `tau_agent` 吗？

**毕业项目门槛**：选定的项目代码可运行、测试通过、费曼讲解完成、架构说明写清。

---

## 7. 资源索引

### 7.1 必读源码 → 概念映射（coding 层）

| 文件 | 对应概念 | 规模（行） |
| --- | --- | --- |
| `cli.py` | 入口、前端选择、CLI 契约 | 1395 |
| `session.py` | 环境装配、循环驱动、持久化、压缩、分支 | 5260 |
| `session_preparation.py` | 候选优先装配 | 88 |
| `session_manager.py` | 会话索引 | 477 |
| `tools.py` | 内置工具、截断、多模态 | 1218 |
| `resources.py` | 系统提示资源发现、信任过滤 | 377 |
| `context.py` | 项目上下文发现 | 95 |
| `skills.py` | 技能加载与 `/skill:` 展开 | 255 |
| `system_prompt.py` | 系统提示组装与检视 | 393 |
| `context_window.py` | token 估算与压缩摘要 | 353 |
| `provider_catalog.py` / `catalog_loader.py` | 模型目录 | 111+ |
| `provider_config.py` / `credentials.py` | 定义/偏好/秘密分离 | — |
| `provider_runtime.py` | provider 工厂与身份 | 506 |
| `commands.py` | 斜杠命令注册表 | 924 |
| `events.py` | session 事件契约 | 70 |
| `rendering/` | print 渲染器 | base/plain/json/transcript 4 模块 |
| `rpc.py` | 无头 JSONL 协议 | — |
| `session_export.py` | HTML 导出 | 1780 |
| `extensions/api.py` / `loader.py` / `runtime.py` | 扩展钩子、加载、运行时 | 1236/405/1350 |
| `tui/adapter.py` + `tui/app.py` | Textual 前端 | — |
| `data/catalog.toml` | 内置 provider 目录 | — |
| `data/examples/extensions/*.py` | 扩展模板 | 4 个 |

### 7.2 配套文档

- 本系列：`dev-notes/learning-guide/00~05`（总览 + 三层手册 + 全链路 + 实操）
- 架构笔记：`dev-notes/architecture/index.md` 及 `phase-*.md`
- 设计文档：`dev-notes/design/01-architecture.md`、`02-agent-loop.md`、`03-tools.md`、`04-sessions.md`
- 决策记录：`dev-notes/adr/`
- 网站文档：`website/content/internals/architecture.md`、`agent-loop.md`、`custom-frontend.md`、`guides/*`
- 学习路线可视化：`dev-notes/learning-guide/assets/learning-path.html`

---

## 附：本计划的核心理念

> **学 Agent 开发，不要先背 API，先背"边界"。**
> Tau 把系统切成 大脑 / 环境 / 前端 三块，让每一块都能单独理解、替换、测试。
> 你在这 3 周里真正学会的不是 Tau 的 46K 行代码，而是：
> **一个 Agent 系统，如何用事件解耦、用工具行动、用信任守门、用追加式历史记忆、用上下文预算自保、用扩展生长。**
> 带着这套心智，去读任何别的 Agent 项目（Claude Code、Codex、OpenHands……）都会快得多。
