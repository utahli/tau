# Coding 层精读 Phase 3：系统提示层

本文覆盖两个模块：

- [system_prompt.py](../../../../src/tau_coding/system_prompt.py)：把工具、技能、项目上下文、扩展贡献和自文档路由确定性地拼成一个 system prompt。
- [self_docs.py](../../../../src/tau_coding/self_docs.py)：提供随 `tau_coding` 包安装的 Tau 文档和示例路径。

行号以当前工作区快照为准。

## 文件定位

`system_prompt.py` 是纯 representation 层：它接收已经加载好的 `Skill`、`ProjectContextFile`、`AgentTool`、扩展 guideline/section 和显式 prompt 配置，然后生成一个字符串。它不负责文件发现、权限判断、路径信任或模型请求。

`self_docs.py` 是路径层：基于 `__file__` 找到当前安装包内的 `data/docs` 与 `data/examples`。它也不读文档内容。

这两个模块合起来体现的是 Pi 式 progressive disclosure：

```text
基础 system prompt
  只放极简身份 + 工具索引 + 守则 + 文档路径 + 技能索引

完整项目指令 / 完整技能 / Tau 文档正文
  按需读取或显式展开
```

## 依赖关系

### `system_prompt.py`

上游依赖：

- `tau_agent.tools.AgentTool`：只读取工具的 prompt 元数据，不执行工具。
- `tau_coding.skills.Skill`：读取名称、描述、路径和是否允许模型主动调用。
- `tau_coding.self_docs`：获取安装内 Tau 文档路径。
- 标准库 `date`、`Path`、`escape`。

下游使用方：

- `CodingSession.load()` 初始构建 system prompt。
- `CodingSession.reload()` 在资源、工具或扩展 prompt 贡献变化时重建。
- TUI 复用 `format_skills_for_prompt()` 估算技能在 prompt 中的 footprint。
- 测试和 CLI 用 `build_system_prompt()` 作为预期 prompt 的构造器，避免手写脆弱的长字符串。

它不依赖 `tau_ai`、CLI、Textual、Rich 或 provider 实现，方向上符合 Coding 层内聚、可测试的边界。

### `self_docs.py`

上游是 Python 包安装布局：

```text
src/tau_coding/
  self_docs.py
  data/
    docs/
    examples/
```

下游只有 `system_prompt.format_tau_documentation()` 直接消费这三个路径函数。打包配置按包目录携带 `tau_coding`，因此这些资源会进入安装产物；包元数据测试也锁定了关键文档和示例必须出现在 wheel 中。

## 核心数据结构

### `ProjectContextFile`

```python
path: str
content: str
```

表示一个已经读取完成的项目指令文件。`path` 用于 prompt 中的来源标注，`content` 是原文。这里刻意不保存 `Path`，因为 discovery 层已经把来源解析成最终路径。

### `PromptSection`

```python
title: str | None
body: str
```

表示扩展注册的自由结构化 prompt 段。`title=None` 渲染为无标题块；非空 title 渲染为二级 Markdown 标题。它由扩展运行时拥有和排序，`system_prompt.py` 只负责格式化。

### `BuildSystemPromptOptions`

这是拼装输入的完整快照：

| 字段 | 来源 | 语义 |
| --- | --- | --- |
| `cwd` | 会话配置 | 最终写入 prompt 的当前工作目录。 |
| `tools` | 默认工具 + 扩展工具 | 决定可见工具、工具 guideline，以及是否注入技能索引。 |
| `skills` | 资源发现 | 可见技能索引；不注入完整 SKILL.md 正文。 |
| `custom_prompt` | 显式配置或发现文件 | 非 `None` 时替换默认骨架；空字符串也算显式替换。 |
| `append_system_prompt` | 显式配置或发现文件 | 追加在骨架之后。 |
| `context_files` | 显式配置 + 项目发现 | 包进 `<project_context>`。 |
| `current_date` | 测试注入或当前日期 | 固定输出 ISO 日期，保证测试确定性。 |
| `extra_guidelines` | 扩展运行时 | 追加进默认 prompt 的 Guidelines。 |
| `extra_sections` | 扩展运行时 | 追加在用户 append 之后。 |

注意 `custom_prompt is not None` 与 `custom_prompt` 是否为空无关。`custom_prompt=""` 仍会替换默认 Tau 身份。

## `build_system_prompt()` 逐段精读

入口在 [system_prompt.py](../../../../src/tau_coding/system_prompt.py:47)。

### 1. 固定时间与路径表示

```python
current_date = options.current_date or date.today()
cwd = _format_path(options.cwd)
```

测试可以显式传日期，正常运行取当天。`_format_path()` 把 Windows 反斜杠换成 `/`，减少模型把 `\` 当转义符或拼接错误的概率。

### 2. 先组装 append 区

```python
append_parts = [options.append_system_prompt] if options.append_system_prompt else []
append_parts.extend(format_prompt_section(section) for section in options.extra_sections)
append_section = "".join(f"\n\n{part}" for part in append_parts)
```

顺序是：

```text
用户/显式 append_system_prompt
扩展 extra_sections（注册顺序）
```

因此扩展 section 一定在用户 append 后面。这个顺序由测试锁定，也让用户提供的全局追加优先被读到。

### 3. custom prompt 分支

当 `custom_prompt is not None`：

```text
custom_prompt
append_system_prompt
extension PromptSections
project_context
available_skills（仅有 read 工具）
current date
cwd
```

它会替换：

- Tau 默认 coding assistant 身份
- Available tools
- Guidelines
- Tau 自文档路由
- 扩展的 standalone guidelines

但不会替换：

- append 区
- 项目上下文
- 技能索引
- 日期和 cwd
- 扩展的结构化 `PromptSection`

这是一个折中：用户明确接管基础人格与工作方式，但会话仍保留必要的运行环境和资源上下文。

### 4. 默认分支

默认 prompt 的顺序是：

```text
1. Tau coding assistant 身份
2. Available tools
3. “可能还有 custom tools”提示
4. Guidelines
5. Tau documentation routing
6. append_system_prompt
7. extension PromptSections
8. project_context
9. available_skills
10. current date
11. cwd
```

身份段落明确说 Tau 是 coding agent harness，并列举核心行为：读文件、执行命令、编辑代码、写新文件。工具与 guideline 都基于实际传入的 `tools`，不是硬编码“永远有四件套”。

“可能有其他 custom tools”这句话很重要：provider 收到的工具 schema 可能包含扩展工具，prompt 中的提示允许模型理解工具面不只来自内置工具。

## 各 formatter 精读

### `format_prompt_section()`

有标题时：

```markdown
## Title

body
```

无标题时直接返回 body。扩展运行时已经过滤空 body、空 title 和多行 title，因此这里保持最小格式化职责。

### `format_tau_documentation()`

它不是“注入全部文档”，而是注入路由规则：

- Main documentation: `data/docs/README.md`
- Additional docs: `data/docs`
- Examples: `data/examples`
- 明确 `docs/...` 和 `examples/...` 的解析根
- 按主题路由到 extensions、skills、models、CLI、TUI、architecture
- 要求读取相关 `.md` 和交叉链接后再实现 Tau 相关任务

初始指令还限定了使用时机：“只有当用户询问 Tau 本身、SDK、扩展、技能、providers、models、commands 或 TUI 时才读取”。这避免每个普通编码任务都消耗文档 token。

路径来自当前安装的 `tau_coding`，不是当前项目。因此即使 Tau 不是运行在自己的源码仓库，也知道去哪里读自己的文档。

### `format_available_tools()`

规则：

```text
tool.prompt_snippet 非空 -> 显示
tool.prompt_snippet 空/None -> 不显示
没有任何可显示项 -> "(none)"
```

`AgentTool` 的 `description` 和 schema 仍会由 provider 层传给模型；`prompt_snippet` 是系统提示中的短索引。隐藏 snippet 不等于隐藏工具，只是不在 system prompt 里重复描述。

### `collect_prompt_guidelines()`

收集顺序固定：

1. 根据 bash/exploration 工具组合合成一条基础 guideline。
2. 按 tools 顺序收集每个工具的 `prompt_guidelines`。
3. 按注册顺序收集扩展 `extra_guidelines`。
4. 追加通用工程守则。

每条 guideline 会 `strip()`，空文本和重复文本被忽略。去重基于 normalize 后的完整字符串，不折叠语义相似项。

bash 相关逻辑：

- 有 `bash`，没有 `grep/find/ls`：建议用 bash 做 `ls/rg/find` 类操作。
- 同时有 `bash` 和探索工具：建议优先用专用探索工具，因为更快且通常能尊重 `.gitignore`。

Tau 当前默认四件套没有独立 `grep/find/ls` 工具，所以通常走第一条；如果宿主或扩展提供这些工具，prompt 会自动调整。

### `format_guidelines()`

只负责把列表转成 Markdown bullets。真正的顺序与去重都在 `collect_prompt_guidelines()`，这让两者都容易单独测试。

### `format_project_context()`

输出结构：

```xml
<project_context>

Project-specific instructions and guidelines:

<project_instructions path="/repo/AGENTS.md">
...原始正文...
</project_instructions>

</project_context>
```

多个文件按传入顺序输出，中间用空行分隔。`path` 属性经过 `escape()`，避免引号、尖括号等字符破坏 XML-like 标签结构。

正文没有 XML 转义。这不是遗漏：项目指令需要作为 Markdown/自然语言被模型读取，转义后会破坏其可用性。因此它是信任边界内的原文注入，而不是“已消毒”的不可信文本。

### `format_skills_for_prompt()`

先过滤：

```python
skill.disable_model_invocation == False
```

禁止模型主动调用的技能不会出现在模型可见索引里，但仍能通过 `/skill:name` 显式调用。

索引包含：

- name
- description，缺省为 `No description`
- SKILL.md 的绝对路径

技能按名称排序。即使上游已经排序，这里仍保证 prompt 顺序稳定。

前面的说明告诉模型：

1. 任务匹配 description 时读取完整 SKILL.md。
2. 技能内相对路径要相对技能目录解析。
3. 工具命令中使用解析后的绝对路径。

这里采用索引而非全文注入，是 token 效率和上下文准确性的折中：模型先根据短描述判断相关性，再通过 read 工具加载完整技能。

### `_has_tool()` 与技能门槛

只有存在名为 `read` 的工具时才注入技能索引。原因是索引的后续动作依赖文件读取；没有 read 工具时，让模型看到技能路径只会制造无法完成的调用意图。

## `self_docs.py` 逐段精读

模块级常量：

```python
_PACKAGE_ROOT = Path(__file__).resolve().parent
_DATA_ROOT = _PACKAGE_ROOT / "data"
```

三个函数分别返回：

- `tau_readme_path()`：`data/docs/README.md`
- `tau_docs_path()`：`data/docs`
- `tau_examples_path()`：`data/examples`

这层小函数的价值在于：

1. `system_prompt.py` 不需要知道安装布局。
2. 测试或后续系统可以 mock 一个稳定 API。
3. 未来文档目录迁移时只改这里。

它没有做 existence check，也没有截断文档内容；真正的读取由 read 工具完成。

## 与 `CodingSession` 的交互

### 初始装配

[session.py](../../../../src/tau_coding/session.py:606) 传入：

- canonical `cwd`
- extension-composed tools
- discovered `skills`
- 显式或发现的 custom/append prompt
- explicit + discovered context files
- extension guidelines
- extension sections

注意 `CodingSessionConfig.system` 是更强的 exact override。如果调用方直接传 `system`，`CodingSession` 完全跳过 `build_system_prompt()`，扩展贡献也不会自动进入该字符串。

### 输入优先级

实际 custom/append prompt 选择发生在 `session.py`，大致优先级是：

```text
显式 config/CLI 值
  > 项目 .tau/SYSTEM.md 或 .tau/APPEND_SYSTEM.md
  > 用户 ~/.tau/SYSTEM.md 或 ~/.tau/APPEND_SYSTEM.md
```

显式值存在时，发现的文件会生成 diagnostic，但内容被忽略。

项目上下文的顺序则由 `context.py` 决定：

```text
显式 context_files
用户 ~/.tau/AGENTS.md
用户 ~/.agents/AGENTS.md
项目根到 cwd 的各级 AGENTS.md
cwd/.tau/AGENTS.md
cwd/.agents/AGENTS.md
```

同路径会去重。项目输入是否可见先由 project trust 决定。

### Reload

`reload()` 会比较：

- skills 的名称、路径、描述、`disable_model_invocation`
- context files
- custom/append prompt 与来源路径
- tool 名称序列
- extension guidelines
- extension sections

只有这些输入变化时才重建 system prompt。若输入没变，即使调用 `build_system_prompt()` 会失败，`reload()` 也不应重建；对应测试锁定了这个行为。这样可以避免无谓改变下一次模型调用的上下文。

## 设计意图

### 1. 确定性拼装

同一种输入必须得到同一份 prompt。排序、去重和 append 顺序都显式写在代码里，测试可以直接断言完整结构。

### 2. 发现与表示分离

`resources.py`、`context.py`、`skills.py` 负责文件系统和优先级；`system_prompt.py` 只负责字符串表示。这个拆分让 prompt builder 不需要 mock 大量 I/O。

### 3. Progressive disclosure

默认 prompt 放索引和路由，不预载全部项目指令、技能正文或 Tau 文档。模型按需读取，节省 token，也让长文档在真正相关时保持完整。

### 4. 三层 override

```text
CodingSessionConfig.system        完全接管生成结果
custom_prompt                     替换默认骨架，保留环境资源
append_system_prompt              只追加，不替换
```

这三种控制粒度分别面向嵌入式宿主、强定制用户和普通增量配置。

### 5. 前端中立

print、TUI、RPC 不各自拼 prompt，都通过 `CodingSession` 拿同一份装配结果。TUI 额外复用技能 formatter 做侧栏统计，但不改变 prompt 本身。

## 安全与健壮性要点

- `escape()` 用于 `project_instructions.path` 和技能 name/description/location，防止元数据破坏 XML-like 结构。
- `AGENTS.md` 正文、custom prompt、append prompt 和扩展 section 是原文注入，不能宣称已经被语义消毒。
- 项目资源的信任入口在 `CodingSession`/project trust，而不是这个 formatter。
- 项目扩展需要 opt-in 和信任决策，用户/内置扩展也由各自安装路径界定。
- 技能只注入短索引；完整内容由模型通过 read 工具按需读取。
- `disable_model_invocation` 让用户可以保留显式技能调用，同时避免模型自动选择。
- 空标题、空 body、多行标题等扩展异常由扩展运行时过滤，避免 malformed Markdown。
- 空 custom prompt 是合法替换值，避免把“空文件”误判为“未配置”。
- 日期可测试注入；输出使用 ISO 格式，避免地区格式歧义。

## 对应测试

核心测试在 [tests/test_system_prompt.py](../../../../tests/test_system_prompt.py)，覆盖：

- 默认身份、工具、guidelines、日期和 cwd
- 无 snippet 工具隐藏
- guideline 去重
- custom prompt 替换默认骨架但保留 append/context/date
- 扩展 section 顺序
- 空 custom prompt 仍是替换
- 技能 XML 转义
- 禁止模型主动调用的技能隐藏
- 只有 read 工具存在时才注入技能

相关集成测试还包括：

- `tests/test_context.py`：项目上下文发现顺序。
- `tests/test_resources.py`：SYSTEM/APPEND 文件优先级和诊断。
- `tests/test_skills.py`：技能 frontmatter 和调用开关。
- `tests/test_coding_session.py` 中 system prompt reload、资源变化和 exact override 相关用例。
- `tests/test_extensions.py`：扩展 guideline 和 PromptSection 到达 system prompt。
- `tests/test_package_metadata.py`：安装包携带自文档资源。

聚焦验证：

```text
uv run pytest tests/test_system_prompt.py tests/test_context.py tests/test_resources.py tests/test_skills.py
uv run pytest tests/test_coding_session.py -k "system_prompt"
```

## 学习思考题

1. 为什么 `custom_prompt=""` 会替换默认 prompt，而 `custom_prompt=None` 表示使用默认 prompt？
2. 如果宿主传入 `CodingSessionConfig.system="..."`，扩展注册的 guideline 和 PromptSection 还会生效吗？这是否合理？
3. `format_project_context()` 转义 path 属性但不转义正文。这个设计能防什么，不能防什么？
4. 为什么技能索引依赖 `read` 工具存在，而不是所有工具都注入技能列表？
5. Tau 自文档为什么不做成 Agent Skill，而是打包 docs/examples 并在 prompt 中路由？

## 收束

系统提示层的重点是“约束式组装”：资源发现可以复杂，但最终进入 prompt 的形状必须简单、稳定、可预测。`system_prompt.py` 负责把多来源输入压平成一个字符串，`self_docs.py` 则给 Tau 一条知道自己文档在哪的路由线。
