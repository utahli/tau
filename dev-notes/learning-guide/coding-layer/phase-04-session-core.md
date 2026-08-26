# Coding 层精读 Phase 4：Session 核心层

本文覆盖 coding 层的会话核心：

- [events.py](../../../../src/tau_coding/events.py)：coding session 暴露给前端的补充事件。
- [session.py](../../../../src/tau_coding/session.py)：`CodingSession` 的环境装配、交互编排、持久化、模型运行时、压缩、分支、恢复与生命周期。
- [session_manager.py](../../../../src/tau_coding/session_manager.py)：用户目录下的会话索引与 transcript 路径管理。
- [session_export.py](../../../../src/tau_coding/session_export.py)：HTML/JSONL 导出与会话树/用量可视化。
- [session_stats.py](../../../../src/tau_coding/session_stats.py)：当前分支的生命周期用量统计。
- [session_usage.py](../../../../src/tau_coding/session_usage.py)：导出用的逐请求 token/cost 分析。
- [session_preparation.py](../../../../src/tau_coding/session_preparation.py)：前端共享的 candidate-first 启动边界。

行号以当前工作区快照为准。这里的“逐行”按执行路径逐段推进，并对决定行为的关键行做细读；`session.py` 已经增长到约 4400 行，逐字符列出所有行会淹没真正的结构。

## 先修正旧计划里的三个判断

### 1. `CodingSession` 当前没有装配审批钩子

`AgentHarnessConfig` 支持 `before_tool_call` / `after_tool_call`，`tau_agent.loop` 也会调用这些钩子；但是当前 `CodingSessionConfig` 没有对应字段，`CodingSession.load()` 构造 `AgentHarnessConfig` 时只传入：

- `provider`
- `model`
- `system`
- `tools`
- `session_id`

因此不能再说 “`CodingSession` 把审批钩子传进 harness”。正确表述是：**harness 有这个扩展点，当前 coding app 没有使用它**。

### 2. 持久化已经变成 push-based

旧计划里“前端消费事件时顺带持久化”的描述已经过时。当前 `CodingSession.__init__()` 会调用 `_attach_persistence_listener()`，直接订阅 `harness.subscribe`。每个 `MessageEndEvent` 到达时立即持久化，持久化不依赖 TUI/print 前端是否继续读取自己的事件流。

### 3. 启动和替换多了 preparation 边界

当前前端可以通过 [session_preparation.py](../../../../src/tau_coding/session_preparation.py) 先加载 candidate session，再调用 `adopt()` 提交 staged entries。这个设计服务于 project trust、动态 provider、索引写入失败回滚以及“未采纳 candidate 不发布”的 durability 语义。

## 架构总览

`CodingSession` 是 coding-agent 的环境外壳，不是 agent brain：

```text
AgentHarness
  provider/model/system/tools/messages
  agent loop
  low-level AgentEvent stream
        |
        | subscribe: message persistence / extension listeners
        v
CodingSession
  durable append-only session tree
  resources, skills, commands, extensions
  trust boundary
  provider/model operations
  compaction, branching, resume, reload
  coding-specific event adaptation
        |
        v
print CLI / TUI / SDK frontend
```

它保持了 Pi 的核心分层：

- **可复用 brain**：`tau_agent.AgentHarness`，不依赖 CLI、Textual、Rich 或用户目录。
- **coding 环境**：`tau_coding.CodingSession`，负责把 brain 放进一个可持续、可恢复、可扩展的编码工作区。
- **前端**：只消费 `CodingSessionEvent` 并调用公开 API，不参与 transcript 权威写入。

## `events.py`：Coding 事件语言

入口在 [events.py](../../../../src/tau_coding/events.py:1)。

### 事件结构

`events.py` 在 `tau_agent.events.AgentEvent` 之上定义 coding 层事件：

| 事件 | 意义 |
| --- | --- |
| `SessionAgentEndEvent` | 一轮 agent 运行结束，额外携带 `will_retry`。 |
| `AgentSettledEvent` | 事件流关闭、持久化 reconciliation 和自动恢复处理都完成。 |
| `QueueUpdateEvent` | steering/follow-up 队列变化。 |
| `CompactionStartEvent` / `CompactionEndEvent` | 手动、阈值或 overflow 压缩的边界。 |
| `EntryAppendedEvent` | durable entry 已追加。 |
| `SessionInfoChangedEvent` | 会话名等元数据变化。 |
| `ThinkingLevelChangedEvent` | thinking 模式变化。 |
| `AutoRetryStartEvent` / `AutoRetryEndEvent` | overflow 压缩后重试或 Hugging Face route failover。 |

类型别名最后收束为：

```python
type SessionOwnEvent = ...
type CodingSessionEvent = AgentEvent | SessionOwnEvent
type AgentSessionEvent = CodingSessionEvent
```

`AgentSessionEvent` 是兼容别名，新代码应优先使用 `CodingSessionEvent`。

### 为什么需要包装 `AgentEndEvent`

harness 的 `AgentEndEvent` 只知道 agent loop 结束；coding session 可能在结束后继续做：

1. overflow compaction；
2. 自动重试；
3. Hugging Face route failover；
4. persistence reconciliation；
5. `AgentSettledEvent` dispatch。

所以 `prompt()` / `continue_()` 会把底层 `AgentEndEvent` 替换成 `SessionAgentEndEvent`：

- `messages` 继续传给前端；
- `will_retry` 告诉前端这不是最终 settle；
- 最终稳定事件是 `AgentSettledEvent`。

## `CodingSessionConfig`：输入快照

定义在 [session.py](../../../../src/tau_coding/session.py:303)。

### 核心身份

| 字段 | 作用 |
| --- | --- |
| `provider` | 已存在的 provider 对象；可为 `None`，由 session 根据配置动态构建。 |
| `model` | 初始或回退模型名。 |
| `provider_name` | provider 逻辑名。 |
| `storage` | append-only `SessionStorage`。 |
| `cwd` | coding 工作目录，也是 trust/resource 解析中心。 |
| `session_id` / `session_manager` | durable transcript 与 resume 索引关联。 |

### Prompt 与资源

| 字段 | 作用 |
| --- | --- |
| `system` | exact override；非 `None` 时完全跳过系统提示构建。 |
| `custom_system_prompt` / `append_system_prompt` | 显式覆盖 discovered resource。 |
| `context_files` | 显式项目上下文，会和 discovered context 合并。 |
| `tools` | 宿主注入工具；`None` 表示创建默认 coding tools。 |
| `resource_paths` | Tau home/agents/resource 路径与 project trust 过滤结果。 |
| `skills_enabled` | 是否发现技能并支持 `/skill:` 展开。 |
| `extension_paths` / `extensions_enabled` / `project_extensions_enabled` | 扩展加载与项目扩展 opt-in。 |

`project_extensions_enabled` 不是“信任即加载”。项目扩展同时需要：

1. project trust 判定通过；
2. 该配置显式 opt-in。

### Provider 与运行时

这组字段是当前代码里最容易混淆的部分：

| 字段 | 语义 |
| --- | --- |
| `requested_provider` / `requested_model` | 启动时显式选择的 provider/model。 |
| `session_provider_name` | durable session 里记录的历史 provider 名。 |
| `provider_settings` | 静态 provider 配置集合。 |
| `runtime_provider_config` | 当前静态 provider 的运行时配置。 |
| `dynamic_provider` | extension/dynamic provider 定义。 |
| `inference_provider` | Hugging Face 当前 pinned route。 |
| `inference_provider_mode` | `automatic` 或 `fixed`。 |
| `owns_initial_provider` | session 是否负责关闭初始 provider。 |

`owns_initial_provider` 是所有权边界，不等于 provider 是谁创建的。即使调用方传入 provider，也可以把所有权交给 session。

### 延迟权威写入

`defer_authoritative_writes=True` 时，初始 metadata、tool history repair 等启动写入先进入 `_prepared_entries`，直到 `_commit_prepared_entries()` 或 `PreparedCodingSession.adopt()` 才落盘。它保护的是：

- trust 取消时未发布 candidate；
- provider 创建成功但 transcript 初始化失败；
- destination 索引尚未准备好；
- replacement 尚未被外层 session 采用。

## `CodingSession.load()`：装配路径

入口在 [session.py](../../../../src/tau_coding/session.py:443)。

### 1. 读 durable entries

```python
entries = await config.storage.read_all()
```

空 transcript 会合成初始链：

```text
SessionInfoEntry(cwd=...)
  -> ModelChangeEntry(model, provider)
    -> ThinkingLevelChangeEntry
```

这些 entry 先保存在 `pending_initial_entries`，默认在第一次权威写入时提交。新会话因此可以延迟创建 transcript 文件。

非空 transcript 会先 `_detach_missing_parents()`：如果导入分支的 root `parent_id` 指向不存在的 entry，就把该 root detach 为新的 root，避免导入历史破坏树解析。

### 2. 重放状态并选择 active branch

先得到 linear state，再找最后一个 `LeafEntry`：

- 有 leaf：以 `leaf.entry_id` 重放目标分支；
- 无 leaf：线性重放全部 entries。

这意味着历史从不删除。分支切换和 repair 都是追加新的 leaf/summary/repair entry，再重放当前路径。

### 3. 构造 cwd-bound extension runtime

`load()` 不直接复用 source project runtime，而是为 destination cwd staging 一个 fresh runtime：

- 复用上一 runtime 的 UI bridge；
- 可复用 provider credentials/environment/http client；
- durable providers 来自 `provider_settings`；
- 先只加载 eligible extensions，不加载 project dir。

这一点是信任边界：**source session 的项目扩展代码不能因为 resume/new session 泄漏到另一个 destination cwd**。

### 4. 解析 project trust

`ProjectTrustCoordinator.resolve()` 决定：

- canonical cwd；
- project resources 是否可见；
- project extensions 是否可加载；
- trust decision 是否被取消。

当 `defer_authoritative_writes=True` 时，trust 结果不会立即持久提交；candidate 被采纳时才 commit。

### 5. 加载 resources

`_load_session_resources()` 加载：

- skills；
- prompt templates；
- project context；
- discovered custom/append system prompt；
- resource diagnostics。

如果 trust summary 存在，会追加 project-trust diagnostic。资源加载失败尽量变成 diagnostic 而不是让整个 session 失败；但 trust 的失败语义是 fail closed。

### 6. 按需解析 provider

当 `config.provider is None` 时，`_prepare_provider_selection()` 在 trusted extension registry 建好后运行。

解析顺序：

1. `requested_provider`；
2. durable `state.provider`；
3. `session_provider_name`；
4. 默认 provider selection。

动态 provider 分支会：

- 在 registry 中找 `DynamicProvider`；
- 解析默认或请求的 model；
- 必要时联网刷新模型列表；
- 创建 dynamic provider runtime；
- 返回 automatic HF-style inference mode 语义（对 dynamic provider 通常无 pinned route）。

静态 provider 分支会：

- 用 provider settings 解析 provider/model；
- 校验 Hugging Face route；
- 计算 startup thinking level；
- 创建 runtime provider。

如果初始 entries 尚未落盘，解析出的实际 model/provider 会回写到 pending `ModelChangeEntry`，避免 transcript 首条记录与真实运行时不一致。

### 7. 确定 model 能力并组装 tools

`active_model = _runtime_model_for_state(config, state)` 处理 durable model 与当前 provider catalog 不匹配的情况：

- durable model 可用：继续使用；
- 不可用：回退 config model；
- config model 也不可用：provider default。

随后计算 image support，并：

1. 使用调用方 `tools`，或创建默认 coding tools；
2. `extension_runtime.compose_tools(base_tools)` 加入扩展工具。

默认工具不是硬编码进 prompt；它们进入 system prompt builder 和 provider tool schema 的路径一致。

### 8. 构建 system prompt

只有 `config.system is None` 时才调用 `build_system_prompt()`。exact system override 的优先级高于：

- discovered `SYSTEM.md`
- discovered `APPEND_SYSTEM.md`
- skills
- extension prompt sections/guidelines

### 9. 构造 harness 与 session

`AgentHarness` 拿到：

- provider；
- model；
- system；
- tools；
- session id；
- replayed messages。

`CodingSession.__init__()` 再初始化：

- resource snapshots；
- provider/route state；
- context usage cache；
- owned provider ledger；
- diagnostic logger；
- credential store；
- runtime model limits cache；
- pending writes；
- persistence listener。

### 10. 加载后的修复与同步

session 对象构造完成后，`load()` 继续：

1. 如果拥有初始 provider，先记入 ownership ledger；
2. 修复 malformed tool history；
3. 同步 thinking level；
4. 必要时刷新 runtime provider；
5. 发现 runtime model limits；
6. bind extension runtime；
7. attach harness listener；
8. 标记 `session_start_pending=True`；
9. 标记 project trust commit pending。

`session_start` 不在 `load()` 里直接 emit。宿主先安装 UI bridge，再调用 `emit_pending_session_start()`，避免扩展启动时拿不到可用 UI。

任何后半段失败都会通过 `_finish_aborted_session_close()` 关闭 candidate，避免 provider泄漏。

## `prompt()`：一轮交互的执行路径

入口在 [session.py](../../../../src/tau_coding/session.py:2709)。

### 1. Input hooks

`ExtensionRuntime.run_input_hooks()` 可以：

- 修改文本；
- 完全处理输入；
- 返回用户可见消息。

如果 handled，`prompt()` 直接返回，不进入 harness。

`source` 标记输入来源是 interactive 还是 extension；`custom_type`/`details` 可把扩展输入持久化为 `CustomMessage`，用于保留渲染元数据。

### 2. 展开 prompt 资源

`expand_prompt_text()` 依次尝试：

1. `/prompt-template` 命令；
2. `/skill:` 命令；
3. 原文本。

所以 prompt template 和 skill 的正文是在进入 agent 前展开，而不是让模型自己猜 slash command。

### 3. Running session 的队列分支

如果 harness 正在运行：

- `streaming_behavior="steer"`：调用 `harness.steer()`，当前轮立即受新输入影响；
- `streaming_behavior="follow_up"`：调用 `harness.follow_up()`，等待当前轮后继续；
- 其他情况：抛错，避免隐式排队造成输入丢失。

### 4. 空闲路径的启动准备

真正发起 agent loop 前：

1. flush 上次失败的 pending persistence；
2. refresh runtime model limits；
3. 尝试 pre-prompt auto compaction；
4. 清空本轮 message id ledger；
5. 构造 `UserMessage` 或 `CustomMessage`；
6. 调用 `harness.prompt_message()`；
7. invalidate context usage cache。

`id()` ledger 每轮清空是必要的：Python 对象释放后 id 可复用，跨轮保留会误判消息身份。

### 5. 主事件循环

对每个 harness event：

- 第一个已结束 user message 先交给前端渲染，再触发 auto naming 的独立 provider 请求；
- `ToolExecutionEndEvent` invalidate context usage；
- assistant error 写 diagnostic log；
- context overflow 记录待恢复错误；
- retryable Hugging Face route failure 记录待 failover 错误；
- `AgentEndEvent` 包装为 `SessionAgentEndEvent`。

这里对 `AgentEndEvent` 的替换不是装饰：前端看到 `will_retry=True` 时就知道还要等待 compaction/auto retry/settled。

### 6. Overflow 恢复

overflow 分支顺序固定：

```text
CompactionStartEvent(reason="overflow")
compact older context
CompactionEndEvent(..., will_retry=compacted)
AutoRetryStartEvent(attempt=1, max_attempts=1)
harness.continue_()
AutoRetryEndEvent
```

只重试一次。重试中 tool end 仍然 invalidate usage，assistant error/abort 仍写 diagnostic。

### 7. Hugging Face route failover

只有满足以下条件才自动 failover：

- provider 是 Hugging Face；
- mode 是 automatic；
- 已从 response header pin 到某个 route；
- assistant 尚无输出内容；
- provider diagnostic 显示可重试状态：408、409、425、429 或 5xx。

`_run_huggingface_route_failover()` 会：

1. 取消 pinned route，重建 automatic routing provider；
2. 发出 `AutoRetryStartEvent`；
3. `continue_()` 一次；
4. 记录 failover diagnostic；
5. 发出 `AutoRetryEndEvent`。

如果 automatic 响应头返回了新 route，provider observer 会重新 pin；这个 pin 也会更新 session index metadata。

### 8. Finally 收口

无论正常、异常还是取消，`finally` 都会：

1. close harness event iterator；
2. retry ended 但未 persisted 的消息；
3. dispatch `AgentSettledEvent`。

persistence reconciliation 失败会被记录，但不会取消 `AgentSettledEvent` 的派发；settled 表示本轮运行时状态收口，不等价于“所有可修复缓存写入永远成功”。

## 持久化：append-only tree 与幂等写入

核心区域在 [session.py](../../../../src/tau_coding/session.py:3054)。

### Push listener

`_attach_persistence_listener()` 订阅 harness：

```text
harness.subscribe(...)
  -> MessageEndEvent
     -> _persist_on_message_end()
        -> _persist_message()
```

这解决了前端生命周期与 durability 的耦合：TUI 中断、销毁 iterator 或停止渲染，都不应导致已完成消息不落盘。

### 一个消息如何落盘

每个 completed message 会产生：

```text
MessageEntry(parent_id=current tip, message=...)
LeafEntry(parent_id=message_entry.id, entry_id=message_entry.id)
```

`LeafEntry` 不是内容，而是 active branch 指针。之后读取 transcript 时，从最后一个 leaf 反推 active path。

### 幂等重试

`_PendingMessageWrite` 保存稳定的：

- message object；
- message entry；
- leaf entry。

第一次失败后，下次重试不会重新生成 id。重试前读取 durable ids：

- message entry 已存在：跳过；
- leaf 已存在：跳过；
- 都不存在：按原 id append。

这可以修复“message 写入成功、leaf 写入失败”的半完成状态，而不会复制同一条消息。

### Run reconciliation

`_reconcile_run_persistence()` 做三件事：

1. `aclose()` harness event iterator；
2. 扫描 harness transcript，重试 ended 但未 persisted 的消息；
3. 清空本轮 id ledger。

注释中特别说明：loop 的 `message_end` 早于 assistant message append 到 transcript。如果 message 既没有 append 到 harness，也没有 durable entry，这里无法凭空恢复，只能依赖下一次启动时的 tool history repair。

### Staged startup commit

`_commit_prepared_entries()`：

1. 检查 durable ids；
2. 过滤 missing entries；
3. 优先调用 `storage.append_batch()`；
4. 对旧 storage 逐条 append fallback；
5. 清空 staged 状态；
6. 关闭 `defer_authoritative_writes`；
7. 需要时建立 resume index。

索引失败只产生 `session-index` diagnostic，因为索引是可重建缓存；transcript 才是权威数据。

## 模型与 Provider 操作

核心区域在 [session.py](../../../../src/tau_coding/session.py:1335)。

### `set_model()`：同 provider 内切换

流程：

1. 校验 provider config 中存在该 model；
2. 更新 harness model；
3. 重算 inference provider/mode；
4. 同步 thinking level；
5. 重建 runtime provider；
6. 同步 image support；
7. 保存默认 provider/model；
8. touch session index。

模型切换会改变 tool 行为，例如 read 工具是否接受图片输入。

### `select_provider_model()`：candidate-first 跨 provider 切换

跨 provider 切换比 `set_model()` 严格：

1. 必须空闲；
2. 构建 candidate provider；
3. 校验/调整 dynamic thinking level；
4. 确定 image support；
5. 先把 `ModelChangeEntry + LeafEntry` 作为一个 batch 写入 transcript；
6. 写入成功后同步替换 harness/config/runtime state；
7. refresh persisted state；
8. 关闭被替换 provider。

持久化 entry 写入成功是 publication boundary。之后 index 修复失败不会回滚已权威提交的模型切换，只记录 diagnostic。

### Provider ownership

`_owned_providers` 是显式 ledger：

- session 创建的 dynamic provider 记入；
- session 接管的外部 provider 记入；
- refresh route/thinking 产生的新 provider 记入；
- candidate replacement 的 provider 在 adoption 时转移到外层 session；
- 替换掉的 provider 从 ledger 移除并关闭一次。

这样避免两类问题：

1. provider client 泄漏；
2. 同一 provider 被两个 owner 各关一次。

### Hugging Face automatic route

automatic 模式下 response header observer 读取 `x-inference-provider`：

- 合法则立即构建 staged provider；
- 先记入 owned providers；
- 更新 session config/index；
- 激活 pinned route。

如果 pinned route 随后出现 retryable pre-output HTTP failure，session 会取消 pin 并走 automatic routing 重试一次。

## Reload：完整快照替换

入口在 [session.py](../../../../src/tau_coding/session.py:1991)。

`reload()` 先收集旧资源签名：

- skills；
- prompt templates；
- context files；
- diagnostics；
- system prompt inputs；
- extensions；
- tools；
- extension guidelines/sections。

然后完整 staging：

1. 新 extension runtime；
2. 重新 resolve trust；
3. 重新加载 resources；
4. trusted opt-in 时加载项目扩展；
5. 重新 compose tools；
6. 仅在 prompt 输入变化时重建 system prompt；
7. 旧 runtime shutdown；
8. 新 runtime 绑定 session；
9. 同步发布所有新状态；
10. 关闭旧 runtime。

失败或取消时，live session 和 trust cache 保留旧快照；成功后 publication 是同步的，避免调用方看到半新半旧状态。

## Resume / New / Branch：替换语义

### `resume()`

`resume()` 只在空闲时运行，流程：

1. flush pending writes；
2. 从 index 找目标 transcript；
3. 解析目标 provider/model/route；
4. 加载 replacement session；
5. 动态 provider 会在目标 runtime 中重新解析，而不是跨 cwd 复用对象；
6. `_adopt_replacement()` 发布目标 session。

失败时 replacement 作为 candidate 被关闭；成功后外层对象保留同一 Python 身份，但内部 config/state/harness/storage 全部替换。

### `new_session()`

新会话：

1. 优先使用当前默认 provider/model；
2. `prepare_session()` 只创建 metadata/path，不进 resume index；
3. 加载 replacement；
4. `index_on_first_persist=True`；
5. adoption 后第一条持久消息或显式命名才索引。

这避免启动即产生大量空会话。

### `_adopt_replacement()`

提交顺序很关键：

1. 检查 trust cancellation；
2. commit destination staged entries；
3. shutdown old runtime；
4. start replacement runtime；
5. commit trust；
6. 取消 pending session start；
7. 同步替换所有状态；
8. 重绑 persistence listener；
9. 转移 provider ownership；
10. 完成旧 runtime close。

durable transcript commit 必须在 old runtime shutdown 之前成功，否则旧 session 可能已被破坏而新 session 又未成为权威。

### `branch_to_entry()`

分支不删除旧历史：

1. flush pending writes；
2. 校验目标 entry 可分支；
3. 可选生成 abandoned branch summary；
4. 写入新的 `LeafEntry`；
5. 重放 durable state；
6. 修复 malformed tool history；
7. replace harness messages；
8. 同步 thinking/provider/image 支持。

如果目标 entry 是 user message 且不生成 summary，Tau 会分支到该 user message 的 parent，并把原文作为输入 prefill。这让“重新提问”保留原始输入，又不会让同一条 user message 在 durable tree 里被复制。

## Compaction：保留近期上下文

核心区域在 [session.py](../../../../src/tau_coding/session.py:3411)。

### 手动与自动压缩

手动 `compact()`：

1. flush pending writes；
2. 获取 active context rows；
3. 计算保留近期消息的边界；
4. 用当前 provider 生成 summary；
5. 写入 `CompactionEntry + LeafEntry`；
6. 用重放后的 messages 替换 harness context。

自动压缩发生在：

- pre-prompt：超过 threshold；
- post-prompt：本轮结束；
- overflow：作为重试前恢复。

### `_first_recent_context_index()`

从后往前累计 token，找到满足 `DEFAULT_COMPACTION_KEEP_RECENT_TOKENS` 的候选边界，然后调整：

- 候选是 user message：尽量不切断用户请求；
- 候选后面有 user message：切到下一个 user 边界；
- 否则切到第一个非 toolResult。

目标是让模型重试时仍拥有完整的人类请求和必要响应边界，而不是机械按 token 数切割。

### Compaction entry

`CompactionEntry` 记录：

- summary；
- `replaces_entry_ids`；
- optional `first_kept_entry_id`；
- optional `tokens_before`。

旧 entries 仍在 transcript tree 里，因此 stats/export 能看到原始活动；`SessionState.from_entries()` 只会把被替换消息从 active context 中移除。

## 命令、终端与会话索引

### Slash commands

`handle_command()` 先让 command registry 执行命令。Prompt template slash command 特殊：它不是立即执行的动作，而是返回未处理状态，让文本进入 `prompt()` 后展开。

### `!` terminal commands

`parse_terminal_command()`：

- `!command`：执行并默认加入 context；
- `!!command`：执行但不加入 context。

`run_terminal_command()` 每次创建新的 bash tool 实例，而不是复用 agent tool set：

- 使用 session cwd；
- 使用 `shell_command_prefix`；
- 提取 exit code；
- 只有 `add_to_context=True` 时才构造 user message 并持久化。

这让“用户直接执行命令”和“模型决定调用工具”有清楚的来源边界。

### Session indexing

`ensure_session_indexed()`、`_ensure_session_initialized()` 和 `_index_current_session()` 协作实现：

- unindexed new session 可先运行；
- 第一次持久写入或用户命名时创建 transcript/index；
- index 失败记录 diagnostic，不阻断主流程。

## `session_manager.py`：索引层

入口在 [session_manager.py](../../../../src/tau_coding/session_manager.py:1)。

### 数据模型

- `SessionRecordModel`：Pydantic JSONL 模型，`extra="ignore"` 兼容未来/额外 metadata。
- `CodingSessionRecord`：代码内 frozen dataclass，路径是 `Path`。
- `SessionManager`：负责 create/prepare/index/list/resume metadata。

### Session id 安全

自定义 id 必须满足：

- 只含 alphanumeric、`-`、`.`、`_`；
- 首尾必须是 alphanumeric；
- 不超过 128 UTF-8 bytes；
- 不使用 reserved id；
- 不是 Windows 保留文件 stem。

这些规则让 id 可以安全映射到文件名，并尽量跨平台。

### 创建路径

- `prepare_session()`：生成 metadata/path，不索引；
- `create_session()`：prepare + index；
- `create_session_exclusive()`：`open("x")` 原子保留 transcript，冲突报错；失败时回滚 reservation/index。

### 索引布局

当前按 project cwd 分目录，每个项目一个 `index.jsonl`；同时兼容 legacy global index。

`list_sessions(cwd)` 会：

1. 读当前 project index；
2. 合并 legacy global index 中同 cwd records；
3. 按 id 去重；
4. 按 `updated_at` 新者优先。

### JSONL 解析细节

读取 index 时使用：

```python
text.split("\n")
```

而不是 `splitlines()`。原因是 JSON 字符串里可能合法包含 U+2028/U+2029，`splitlines()` 会把它们当行边界，导致 JSON 解析失败。这是一个很容易忽略的持久化细节。

## `session_stats.py`：生命周期统计

入口在 [session_stats.py](../../../../src/tau_coding/session_stats.py:1)。

`calculate_session_stats()` 遍历传入 entries，统计：

- user/custom turn 数；
- tool call 数；
- input/output/cache read/cache write tokens；
- latest request cache 数据；
- estimated cost。

重要语义：

- 输入是当前分支的原始 entries，包括已被 compaction 替换的消息；
- prompt tokens = fresh input + cache read + cache write；
- cache hit rate 在没有任何 cache 活动时返回 `None`，不是 0%；
- pricing 不完整时 total cost 返回 `None`，避免制造虚假精度；
- provider 已报告 cost 时可作为 fallback；
- 1-hour cache write 使用更高费率，缺省回退 5-minute write 费率。

## `session_usage.py`：导出用量面板

入口在 [session_usage.py](../../../../src/tau_coding/session_usage.py:1)。

`collect_session_usage()` 生成：

- `RequestUsage`：每个 assistant response 的 token、reasoning、stop reason 和估算成本；
- `UsageEvent`：压缩、模型变化、thinking 变化、branch summary；
- `SessionUsage`：requests、tool call 聚合、compaction 数和 events。

notable event 会挂到下一个 assistant request 上；如果之后没有 request，则挂到最后一个 request。这样图表能表达“这个事件影响了下一请求的上下文”。

成本策略：

1. 内置 provider catalog；
2. provider-reported cost fallback；
3. 仍无价格则 `None`。

`render_usage_dashboard()` 是内联 SVG/JS/HTML 的自包含渲染层，不依赖外部网络资源。大 session 会限制 hover point 数量和 HTML 体积。

## `session_export.py`：自包含导出

入口在 [session_export.py](../../../../src/tau_coding/session_export.py:1)。

### 公共 API

- `export_session_jsonl()`：按 entry model 序列化为 JSONL。
- `export_session_html()`：生成单文件 HTML。
- `export_session_artifact()`：按扩展名或显式 format 分派。
- `normalize_export_format()`：只支持 `html/htm` 与 `jsonl`。
- `default_session_export_artifact_path()`：基于 source stem 和 format 生成默认路径。

### HTML 结构

`render_session_html()` 会计算：

- 最新 leaf；
- active path；
- visible entries（过滤 leaf 指针）；
- branch tree；
- entry details；
- tool/event filters；
- JSONL download data；
- active-path usage dashboard。

如果 active path 无法解析，导出不直接失败，而是保留目标 id 的有限标记；tree 中的 dangling/unreachable entries 也有单独呈现。

### Branch tree 渲染

`_render_tree_chain()` 会把只有一个 child 的链压平，只在真实 fork 时嵌套 `<ol>`。这让普通长会话的 sidebar 不是无限右移的深度树。

树渲染使用循环和 rendered set，避免 malformed cycle 无限递归。

### 安全与可读性

HTML 输出对：

- title/source；
- entry id/parent id；
- user/assistant/tool 文本；
- thinking；
- tool arguments/details；
- system prompt；
- aria label 和 attribute；

做 HTML escaping。Pygments 只负责 JSON token 高亮，输出仍然是文档内片段，不引入外链。

System prompt 单独折叠显示，并提示 “May include project instructions”。这提醒导出文件本身可能携带项目机密；导出安全不只是 XSS，也包括内容泄露。

### JSONL download

HTML 内嵌 base64 JSONL。文件名优先来自 source stem，缺省由 title slug 生成。导出因此可以离线阅读和离线还原原始 entry JSON。

## `session_preparation.py`：candidate-first 启动

入口在 [session_preparation.py](../../../../src/tau_coding/session_preparation.py:1)。

`prepare_coding_session(config)`：

1. 复制 config；
2. 强制 `defer_authoritative_writes=True`；
3. 调用 `CodingSession.load()`；
4. 返回 `PreparedCodingSession`。

`adopt()`：

- 已 prepared 才能采纳；
- trust cancelled 则 abort；
- `_commit_prepared_entries()` 失败则 abort；
- 成功后状态改为 adopted 并返回 session。

`abort()` 只执行一次 close。状态机防止重复 close 或已采纳对象被误关。

这层让 print/TUI/SDK 前端共享同一条 startup durability boundary，而不是各自手写 trust/adopt 逻辑。

## 生命周期收口：`aclose()`

入口在 [session.py](../../../../src/tau_coding/session.py:2569)。

`aclose()`：

1. 只创建一个 close task；
2. shield 等待全部资源处理完成；
3. extension `session_shutdown`；
4. 清理 UI components；
5. close extension runtime；
6. 逐个 close owned providers；
7. 首次调用传播最终错误或取消；
8. 后续调用观察同一个 task，幂等。

单个 provider 或 extension 失败不会跳过后续资源。调用方取消会被记住，但不能中断 durable close pass。

## 设计意图总结

### 1. Transcript 是权威，index 是缓存

Session tree、branch、resume、model change 都以 append-only entries 为权威。index 记录 path/model/title/time，只用于发现和展示；损坏时可诊断、可重建。

### 2. Candidate-first publication

跨 provider 切换、reload、resume/new、startup preparation 都遵循：

```text
构建完整 candidate
  -> 可失败/可取消 staging
  -> durable commit boundary
  -> synchronous publication
  -> 异步清理旧资源
```

这比“边改边回滚”更容易保证失败时不出现半采用状态。

### 3. 环境与 brain 分离

`AgentHarness` 保留可移植 agent loop；`CodingSession` 承担 Tau 特有的文件、目录、扩展、索引、信任和恢复策略。该边界避免 `tau_agent` 向 Textual/Rich/CLI 或用户 home 布局泄漏。

### 4. Push durability

持久化由 harness listener 推动完成，而不是依赖某个前端事件循环。UI 的读取进度影响渲染，不影响已完成消息的 durability。

### 5. 明确资源所有权

provider 和 extension runtime 都有 ledger/state machine。创建者不一定永远是 closer，但任一时刻应有明确 owner；candidate adoption 时所有权显式转移。

### 6. 恢复策略分级

- 可丢/可重建：index、cache；
- 需诊断但不应中断主流程：auto compaction、模型 limit discovery；
- 需要自动恢复：overflow、特定 Hugging Face route failure、interrupted tool history；
- 必须 fail closed：project trust。

## 安全与健壮性要点

- Project trust 在 resource loading 和 project extension loading 之前。
- Destination replacement 不复用 source project runtime。
- 项目扩展需要 trust + 显式 opt-in。
- Dynamic provider 在目标 cwd/trust 环境中重新解析。
- Session id 限制成可移植文件名。
- Index JSONL 只按实际 newline 切分。
- Message persistence 使用稳定 entry id 幂等重试。
- `append_batch()` 是启动/repair/模型切换的首选原子路径。
- Running 时 provider/model/reload/resume/new/branch 都要求 idle。
- Overflow retry 和 route failover 都有明确一次性边界。
- HTML 导出做 HTML/attribute escaping，且不依赖外链。
- 导出包含 system prompt 和 transcript，本身要按敏感文件对待。
- Provider close 逐个尝试，避免一个失败拖住全部资源。

## 对应测试

主集成测试在 [tests/test_coding_session.py](../../../../tests/test_coding_session.py)，覆盖：

- empty session 延迟 transcript 初始化；
- persistence retry 幂等；
- interruption 后 tool result repair；
- branch/tree 与 deep session；
- system prompt/resource reload；
- thinking/model/image capability；
- auto compaction、overflow retry、model limits；
- Hugging Face route pin 与 failover；
- provider/model 切换及 ownership；
- resume/new session adoption；
- auto naming 与 index 时机。

[tests/test_session_manager.py](../../../../tests/test_session_manager.py) 覆盖：

- project/global index；
- id 安全和跨平台；
- exclusive creation；
- unindexed prepared session；
- metadata touch 和排序；
- U+2028 title。

[tests/test_session_export.py](../../../../tests/test_session_export.py) 覆盖：

- branch tree 与 active path；
- system prompt escaping；
- tool/event filter；
- JSONL download；
- theme 和 usage tab。

[tests/test_session_usage.py](../../../../tests/test_session_usage.py) 覆盖：

- request/tool/compaction 聚合；
- notable event 归属；
- catalog/provider cost；
- large session HTML 体积控制。

[tests/test_project_trust.py](../../../../tests/test_project_trust.py) 补充覆盖：

- trust store 与 coordinator 的 fail-closed 语义；
- extension、saved decision、默认值的优先级；
- reload/new/resume 时 destination resource 边界；
- cancellation 后源 session 和 trust cache 保持不变。

聚焦验证：

```text
uv run pytest tests/test_coding_session.py
uv run pytest tests/test_session_manager.py tests/test_session_export.py tests/test_session_usage.py
uv run pytest tests/test_project_trust.py
```

## 学习思考题

1. 为什么 `AgentEndEvent` 不能作为 coding 前端的最终 settle 信号？
2. 如果 message entry 已写入而 leaf entry 写入失败，下一次重试如何避免重复消息？
3. `resume()` 为什么不能把 dynamic provider 对象直接从当前 session 带到目标 cwd？
4. `reload()` 为什么要完整 staging，而不是逐项替换 resources/tools/prompt？
5. 为什么 `prepare_session()` 不立即写入 resume index，而 new session 要等第一次持久化？
6. Hugging Face route failover 为什么要求“无输出内容”且只允许特定 HTTP 状态？
7. `CodingSession.aclose()` 为什么要把 close 放进一个不可重复创建的 task，并在内部尝试全部资源？
8. HTML export 已做 escaping，为什么仍要把它视为敏感文件？

## 收束

Phase 4 的核心不是某一个函数，而是一条贯穿始终的原则：**agent brain 可以是可复用的内存状态，coding session 必须是可恢复、可审计、可安全替换的持久环境**。append-only tree、push persistence、candidate-first adoption、显式 provider ownership 和 trust-aware resource loading 共同构成了这层的外壳。
