# Coding 层精读 Phase 1：`cli.py`

本文精读 [cli.py](../../../../src/tau_coding/cli.py)。行号以当前工作区快照为准，后续代码演进后可能漂移。

## 文件定位

`cli.py` 是 Tau 命令行边界。它不实现 agent loop、真实工具、会话持久化、provider API，也不实现交互组件。它负责：

1. 解析并校验命令行参数。
2. 分派 `setup`、`providers`、`sessions`、`export`、`update` 等工具型命令。
3. 在三种真实前端之间路由：TUI、print 模式、RPC 模式。
4. 把 provider 偏好和会话记录转换成 `CodingSessionConfig`。
5. 维护进程级契约：UTF-8 输出、stderr 提示、失败时非零退出、provider 清理。

最简心智模型：

```text
Typer main()
  -> 工具型命令，或
  -> run_openai_tui() -> run_tui_app()
  -> run_openai_print_mode() -> run_print_mode()
  -> run_openai_rpc_mode() -> RpcServer(session).run()
```

## 依赖关系

### 上游依赖

`cli.py` 引用了 Coding 层大部分子系统：

- Provider 配置与运行时：`provider_config`、`provider_runtime`。
- 会话管理与装配：`session`、`session_manager`、`session_preparation`。
- 前端：`tui`、`rendering`、`rpc`。
- 资源与项目信任：`project_trust`、`resources`，以及经由 `CodingSession.load` 的间接依赖。
- 工具系统：凭据存储、扩展安装、导出、更新、shell 设置。

它对 `tau_agent` 和 `tau_ai` 的直接依赖很窄：

- `ModelProvider`、`JsonlSessionStorage`、`SessionEntry`、`SessionStorage`，服务于 print 模式的测试与嵌入式调用边界。
- OpenAI-compatible `setup` 命令需要的环境默认值。

因此 CLI 依赖的是 Coding 层抽象，而不是直接伸手进 agent loop 内部。

### 下游使用方

`pyproject.toml` 声明了真正的控制台入口：

```toml
tau = "tau_coding.cli:app"
```

所有常规 `tau ...` 调用都从这里开始。测试也直接使用 `app` 和 `run_print_mode`。

## 结构概览

| 行号 | 区域 | 职责 |
| --- | --- | --- |
| 1-76 | imports | 汇集 provider、会话、前端、导出、更新、项目信任 API。 |
| 79-100 | UTF-8 初始化 | 为非 UTF-8 Windows 控制台重配 `stdout` / `stderr`。 |
| 102-121 | Typer app | 创建 callback 式 CLI，并允许额外位置参数。 |
| 124-187 | 工具型 helper | 实现 `providers`、`install`、`setup`。 |
| 190-588 | `main` | 解析参数、拒绝冲突组合、分派工具命令与三种前端。 |
| 591-626 | TUI wrapper | 组装 release notes 与更新提示，转入 `run_tui_app`。 |
| 629-721 | 更新与导出 | 实现 Tau 更新、模型目录刷新、会话导出。 |
| 724-779 | prompt helper | 解析系统提示文件、合并管道 stdin。 |
| 782-885 | 导出/provider helper | 解析导出参数、定位输出和来源、渲染 provider 设置。 |
| 888-973 | RPC 路径 | 解析会话与 provider，运行 Pi 兼容 JSONL RPC server。 |
| 976-1117 | print wrapper | 解析 durable/dynamic provider 状态，转入 `run_print_mode`。 |
| 1120-1181 | 会话记录 helper | 恢复或独占创建 print/RPC 会话 transcript。 |
| 1184-1297 | print 核心 runner | 准备并采纳会话、渲染事件或命令、关闭会话。 |
| 1300-1318 | 测试辅助 | 内存会话存储与终端命令结果格式化。 |

## 核心数据与对象

这个文件刻意没有大的领域模型。关键值包括：

- `app`：暴露为控制台入口的 Typer application。
- `main`：实际承担路由职责的 callback。因为设置了 `invoke_without_command=True`，无子命令时也会执行。
- `_MemorySessionStorage`：最小 append-only 存储实现，让测试和嵌入式调用可以不落盘直接运行 `run_print_mode`。
- 导入的配置对象，尤其是 `ProviderSettings`、`ProviderConfig`、`CodingSessionConfig`、`TrustOverride`。

这里没有新增有状态的 CLI 类，意义在于：命令行状态应尽快转换成结构化 config，交给会话或前端层处理。

## 启动细节

### UTF-8 输出流

`_is_utf8_encoding` 会先小写化并移除 `-`、`_`，所以 `UTF-8`、`utf_8`、`utf8` 等价。`_force_utf8_streams` 在需要时把两个输出流重配为 UTF-8，并使用 `errors="replace"`。

模块在 import 时就执行 `_force_utf8_streams()`。这是刻意的：必须在第一条模型回复可能输出非 ASCII 字符之前完成保护。某些测试替身或嵌入式 stream 没有 `reconfigure`，因此相关异常会被抑制。

### Typer callback 与位置命令

Typer app 开启了：

- `allow_extra_args=True`
- `ignore_unknown_options=True`
- `invoke_without_command=True` 的 callback

于是 `tau` 后面的普通文字会进入 `prompt_args`。只有未选择 print/export/RPC 模式时，`main` 才把第一个词当作工具型命令。多个位置参数会用空格拼成一个 prompt。这解释了两个表面冲突的行为：

```text
tau hello world   # TUI，initial prompt 为 "hello world"
tau sessions      # 列出会话的工具命令
```

大多数工具命令是手动分派，而不是注册成独立 Typer 子命令。这让 `main` 能统一处理 `--export`、`--mode` 和旧参数兼容规则。

## 工具型命令

### `providers`

`providers_command` 加载 durable provider settings，再用 `render_provider_settings` 渲染。这里的 `FileCredentialStore` 只用于判断凭据是否存在，不做认证。

每个 provider 输出一行 tab 分隔数据：

- 默认 provider 标记
- provider 名称和类型
- 默认模型与可用模型
- API key 环境变量
- 凭据来源：`stored:...`、`env:...` 或 `missing`
- base URL 与重试配置

OpenAI Codex provider 有一个 OAuth 特例：先检查 `get_oauth`，再走普通 API key 凭据逻辑。

### `install`

`install_command` 手动解析一个 source 和可选 `--force`，拒绝未知 flag 与多余参数。安装前明确警告：扩展会以当前用户权限执行任意 Python。这个警告放在安装前是合理的，因为信任第三方代码是用户在代码执行前做的决定。

### `setup`

`setup_command` 构造 `OpenAICompatibleProviderConfig`，upsert 进 provider settings，并保存 catalog 与运行时偏好。它会去掉 base URL 末尾斜杠；如果 API key 环境变量不存在，则输出提醒。

setup 专属参数（`--base-url`、`--api-key-env`、timeout/retry 控制）声明在 `main` 上，但只有位置命令是 `setup` 时才转发。

### `update`

有两种更新：

- `tau update`：执行能识别当前安装环境的自更新器。
- `tau update --models`：强制刷新并缓存 models.dev catalog。

更新器会输出 stdout、stderr、失败原因，以及是否把动作移交给另一个安装器。`--models` 既支持 Typer option，也支持位置参数后缀，兼容手动命令解析路径。

### `sessions` 与 `export`

`render_session_list` 以 tab 分隔输出会话索引记录，刻意保持简短、适合脚本处理。

导出支持两种形式：

```text
tau export REF [DEST] [--format html|jsonl]
tau --export REF [DEST] [--format html|jsonl]
```

`export_session_command` 把引用解析成已有 JSONL 文件或被索引的会话，用 `JsonlSessionStorage` 读取全部条目，规范化格式，计算目标路径，再把具体格式化交给 `session_export`。

目标路径规则：

- 未给 destination：写到当前工作目录。
- destination 有后缀：视为明确文件路径，原样使用。
- destination 无后缀：视为目录，并在其中生成 artifact 文件名。

## `main`：参数与分派精读

### 参数分组

很长的 `main` 签名可以按语义拆开：

- 运行模式：`--print`、`--mode`，以及隐藏的旧参数 `--prompt` / `--output`。
- Provider/model：`--provider`、`--model`，以及 setup 使用的默认值。
- 工作目录：`--cwd`。
- 会话：`--session`、`--new-session`、仅 print 可用的 `--session-id`。
- 系统提示：替换型 `--system-prompt` 与可重复的 `--append-system-prompt`。
- 上下文维护：TUI 的 `--auto-compact-threshold`。
- 扩展：可重复 `--extension`、`--no-extensions`、`--project-extensions`。
- 项目信任：`--approve` 与 `--no-approve`。
- 工具控制：`--version`、`--models`。

### 兼容与冲突校验

在真正 I/O 之前，`main` 拒绝：

- 同时 approve 和 decline
- 旧参数 `--resume`
- `--session` 搭配 `--new-session`
- `--session` 搭配 `--session-id`
- 旧参数 `--prompt`
- 旧参数 `--output`
- 旧参数 `-x`
- `--export` 搭配 RPC 或 print
- `tau update` 之外使用 `--models`
- 非 print 模式使用 `--session-id`
- 非法 `--session-id`

这些检查把旧拼写转换成迁移提示，而不是静默猜测用户意图。

### 模式推导

```python
rpc_requested = mode is PrintOutputMode.rpc
print_requested = print_mode or (mode is not None and not rpc_requested)
effective_output = mode or PrintOutputMode.text
```

因此：

- `tau -p "x"` 与 `tau --mode text "x"` 都进入 print。
- `--mode json` 与 `--mode transcript` 选择不同 renderer。
- `--mode rpc` 是持久前端，不是 print renderer。
- 未选择 print/RPC 时回落到 TUI。

### 系统提示输入解析

`_resolve_prompt_input` 的规则是：如果路径存在，就读取 UTF-8 文件；否则视为字面文本。这种歧义是命令行易用性的刻意取舍。已存在但无法检查或解码的文件会报 `BadParameter`；不存在则保持字面量。

重复的 append 输入保持顺序，用空行分隔。这个解析发生在前端分派之前，所以 print、TUI、RPC 收到的是同一套提示值。

## TUI 路径

`main` 先计算有效 cwd、启动更新提示、扩展路径、系统提示与信任 override，然后调用：

```text
anyio.run(run_openai_tui, ...)
  -> run_tui_app(...)
       -> resolve provider selection
       -> create or prepare session record
       -> prepare_coding_session()
       -> prepared.adopt()
       -> TauTuiApp(...)
```

`run_openai_tui` 本身是薄适配层：读取 release notes 提示，与 update notice 合并，然后把所有决定转发给 `tui.app.run_tui_app`。TUI 专属的 provider fallback、项目信任询问、会话索引和 Textual 启动都留在 `run_tui_app`。

TUI 退出时，如果会话已经持久化，会返回可恢复的 session id。`main` 在 stdout 打印精确恢复命令。这让 shell history 本身成为持久化提示，同时避免 TUI 在恢复终端状态后自己输出。

## Print 路径

Print 模式是学习完整 agent 生命周期的最佳路径：一个 prompt、一条事件流、一个 renderer、一次会话关闭。

### 获取 prompt

只有 print 模式调用 `_merge_stdin_prompt`。如果 stdin 不是 terminal，就读取全部内容；管道内容放在位置 prompt 前面，中间用空行分隔：

```text
cat notes.md | tau -p "summarize"
cat notes.md | tau -p
tau --print "summarize"
```

读取 stdin 出错时按没有管道输入处理，而不是直接失败。完全为空的 prompt 仍然是 usage error。

### 启动提示

Print 模式运行前会检查更新。提示只写到 stderr，且只在 text 模式输出；JSON 与 transcript 是机器可解析或事件导向的流，不能被人类提示污染。

### 会话与 provider 装配

`run_openai_print_mode` 做四件事：

1. 加载 durable provider settings 与 shell settings。
2. 用 `_print_session_record` 恢复或创建 session record。
3. 解析 provider/model 状态；显式 CLI 选择优先于 resumed-session 状态。
4. 把剩余生命周期交给 `run_print_mode`。

这里比简单的 `load -> resolve -> create` 更细：

- 新会话时，`_print_session_record` 先尝试 durable selection。
- 如果用户显式同时给出 provider 和 model，但 durable 解析失败，请求的名称仍会被记录。这为可信内置或项目扩展注册的 dynamic provider 留出路径。
- 恢复会话且没有显式选择时，record 可以提供上一次 provider/model。
- 如果 resumed provider 已不在 durable settings 中，static selection 可为 `None`；`CodingSession.load` 可再通过 provider registry 解析。
- 找到 static provider 时，`create_model_provider` 立即构造，`run_openai_print_mode` 在 `finally` 中关闭它。
- 如果是 dynamic provider，`initial_provider` 与 `runtime_config` 保持 `None`，由 staged `CodingSession` 创建并拥有。

Hugging Face 的 inference provider 另有保持规则：恢复同一 provider/model 时，record 中保存的 inference provider 和 mode 会延续；否则配置出的 inference provider 变为 `fixed`，没有配置则保持 `automatic`。

### `_print_session_record`

这个 helper 把恢复/创建行为写得很明确：

- Resume：`SessionManager.get_session` 必须找到 id。
- 新的 durable selection：以解析出的名称独占创建会话。
- 新的显式 dynamic candidate：即使 durable selection 失败，也允许请求的 provider/model 通过。

`create_session_exclusive` 拒绝 transcript 碰撞。这很重要，因为 print 模式允许用户指定精确 `--session-id`。

### `run_print_mode`

`run_print_mode` 是一次性 CLI run 的前端中立核心。

它先调用 `prepare_coding_session`。该 helper 会把 `CodingSessionConfig.defer_authoritative_writes` 改成 true，加载 candidate session，并返回 `PreparedCodingSession`。`adopt()` 提交 staged startup/repair entries；`abort()` 关闭未发布的 candidate。这个边界保证项目信任取消或初始准备失败时，不会发布半个 transcript。

有一个 adopt 前特例：`/system` 是信息查询，且明确承诺不保存任何内容。candidate 处理命令后被 abort，输出消息并返回成功，不进入正常采纳和发布流程。

普通输入的流程：

1. `prepared.adopt()` 返回已提交的 `CodingSession`。
2. 可选地应用 startup model override。
3. 安装 `StderrUiBridge`，让扩展消息走 stderr。
4. 项目信任诊断输出到 stderr。
5. `emit_pending_session_start()` 发布 pending session-start 事件。
6. `create_event_renderer` 选择 text/JSON/transcript，并注入扩展的 custom-message renderer。
7. `!` 输入交给 `run_terminal_command`。
8. 斜杠命令交给 `handle_command`；`/local` 被解释为交互专用，`/reload` 在这里真正执行。
9. 其余输入进入 `async for event in session.prompt(prompt)`。
10. 每个事件同步传给 `renderer.render`。
11. `renderer.finish()` 决定进程是否成功。
12. `session.aclose()` 总是在 `finally` 中执行。

事件方向很关键：`cli.py` 不自己解释所有 agent 事件，只选择 renderer；renderer 消费事件契约。

## RPC 路径

`--mode rpc` 启动持久、Pi 兼容的 JSONL 前端。它拒绝位置 prompt，因为 stdin 本身就是命令流。

wrapper 的流程：

1. 加载 provider 与 shell 设置。
2. 恢复或创建 session record。
3. 解析 durable selection；没有显式选择时使用 resumed 值。
4. 立即创建 model provider。
5. 用 JSONL storage 和解析出的 config 加载 `CodingSession`。
6. 安装 `StderrUiBridge`。
7. 运行 `RpcServer` 直到 stdin EOF。
8. 在 `finally` 中关闭 provider。

RPC 使用 `CodingSession.load`，没有走 `prepare_coding_session`。它仍接收相同的 trust override/default 与扩展控制，但相比 print/TUI 的 staged startup 是较老的构造路径。`RpcServer` 自己发布 pending session-start，并把会话操作与事件流序列化为 JSONL。

## 退出与错误语义

Typer exception 处理 usage error。前端 wrapper 抛出的 `ValueError` / `RuntimeError` 会转换成 `typer.BadParameter`，让 CLI 输出保持可读。

真实 prompt 是否成功由 `renderer.finish()` 决定：

- final text renderer：流结束时没有最终 assistant 消息，或携带错误，则返回 false。
- JSON/transcript renderer：遇到不可恢复 agent error 时返回 false。

`main` 把 false 转成 `typer.Exit(1)`。错误文本通常已经渲染到 stderr，因此脚本可以同时依赖输出流和退出码。

Provider ownership 是刻意拆分的：

- `run_openai_print_mode` 关闭自己构造的 compatibility provider。
- staged `CodingSession` 拥有并关闭自己发现的 dynamic provider。
- RPC 关闭自己构造的 provider。
- TUI 把初始构造 provider 的所有权交给 prepared session。

## 安全与健壮性要点

- 扩展安装前警告其会执行任意 Python。
- 项目扩展需要 `--project-extensions` opt-in，并经过项目信任生命周期。
- `--approve` 与 `--no-approve` 是互斥的一次运行信任决定。
- 系统提示文件必须能以 UTF-8 解码。
- 机器可解析输出不输出 update notice。
- provider 凭据只报告是否存在，不打印值。
- 精确 print session id 先校验，再创建 transcript。
- 会话创建是独占的，避免覆盖已有 JSONL。
- candidate session 先 staging，权威写入后才 commit。
- `try/finally` 边界保证 provider 与 session 在失败路径上关闭。

## 对应测试

[tests/test_cli.py](../../../../tests/test_cli.py) 覆盖了：

- UTF-8 stream 重配
- version、setup、providers、sessions、install、update、export
- 旧参数迁移与会话参数冲突
- prompt 文件解析与 append 顺序
- print 模式 stdin 合并
- session-id 校验、恢复、创建、碰撞拒绝
- text/JSON/transcript 渲染与失败退出码
- 项目上下文、技能、终端命令、`/system`

[tests/test_rpc.py](../../../../tests/test_rpc.py) 另外覆盖 RPC 命令分派，以及无 prompt 时的 CLI 路由。

聚焦验证命令：

```text
uv run pytest tests/test_cli.py tests/test_rpc.py
```

## 学习思考题

1. 为什么 `--mode rpc` 和 `--print` 都是非交互模式，但 stdin 处理完全不同？
2. 追踪 `tau --session old --provider other -p "continue"`：哪些选择来自 record，哪些来自 CLI，dynamic provider 在哪里改变结果？
3. 为什么 `/system` 在 `prepared.adopt()` 之前处理，而普通斜杠命令在采纳之后处理？
4. 如果 JSON 模式把 update notice 写到 stdout，会破坏什么？
5. print、TUI、RPC 三种模式中，分别由哪个对象拥有并关闭 provider？

## 收束

`cli.py` 是翻译层，不是应用大脑。它把命令行转换成三种前端生命周期之一，并尽快转入 `CodingSessionConfig`。最重要的架构性质是：print、TUI、RPC 最终都消费 `CodingSession` 事件，变化的只是呈现契约。

Print 路径是学习主线，因为它让顺序完全显式：

```text
CLI options
  -> provider/session resolution
  -> staged CodingSession
  -> session.prompt()
  -> event renderer
  -> durable session close
```
