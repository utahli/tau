# Coding 层 Phase 4：`CodingSession` 核心方法与架构精读

> 本文是学习目录里的“Coding 层 Phase 4”。项目路线图把 `AgentHarness` 标为 Phase 4、把 `CodingSession` 标为 Phase 8；为了避免编号歧义，本文直接以当前 [session.py](../../../../src/tau_coding/session.py) 中的 `CodingSession` 实现为准。

本文回答三个问题：

1. `CodingSession` 和 `AgentHarness`、JSONL 存储、扩展运行时之间如何分工？
2. 一个会话从加载、提问、持久化到关闭，核心方法按什么顺序执行？
3. 模型切换、分支、压缩、恢复和 reload 为什么采用“先准备、后发布”的设计？

行号以当前工作区快照为准。

## 1. 先建立正确的边界

Tau 的三层关系可以压缩成下面这张图：

```text
tau_ai
  provider 的具体 HTTP/流式实现
       ↑ 实现 tau_agent 定义的 ModelProvider 协议
tau_agent
  run_agent_loop + AgentHarness + 消息/事件/工具协议
       ↑ 被 CodingSession 组合
tau_coding
  CodingSession + coding tools + resources + commands + extensions
       ↑ 被 CLI、print renderer、Textual TUI、RPC 等前端消费
```

`AgentHarness` 是可复用的“agent brain”，拥有内存 transcript，并调用 `run_agent_loop()`；它不应知道会话文件、项目目录、斜杠命令或 Textual。[harness.py](../../../../src/tau_agent/harness.py:39) 的 `AgentHarnessConfig` 只描述 provider、model、system、tools 和运行控制。

`CodingSession` 是 coding-agent environment wrapper。它在 harness 外面增加：

- 当前工作目录与默认 `read/write/edit/bash` 工具；
- JSONL append-only session tree 与 `SessionState` replay；
- skills、prompt templates、`AGENTS.md`、system prompt 和诊断；
- slash commands、终端直通命令、分支、压缩、模型/思考等级切换；
- extension runtime、project trust、provider 生命周期；
- 面向前端的 coding-session events。

前端只应消费 `CodingSessionEvent`，或调用公开的 session 方法。它不应自己读取 JSONL，也不应直接操作 harness 的私有列表。

相关入口：

- [events.py](../../../../src/tau_coding/events.py:1)：coding 层事件。
- [memory.py](../../../../src/tau_agent/session/memory.py:22)：从 entries 重放内存状态。
- [storage.py](../../../../src/tau_agent/session/storage.py:16)：追加与批量追加的存储协议。
- [runtime.py](../../../../src/tau_coding/extensions/runtime.py:173)：扩展绑定、事件、工具和 provider 注册。

## 2. 三种状态：配置、运行时、权威历史

理解 `CodingSession` 最容易的方法，是把它看作三个状态源的协调器，而不是一个单纯的“消息列表”。

### 2.1 `CodingSessionConfig` 是输入快照

配置定义在 [session.py](../../../../src/tau_coding/session.py:303)，是 frozen dataclass。字段很多，但可以按职责分组：

| 分组 | 代表字段 | 作用 |
| --- | --- | --- |
| 身份与存储 | `cwd`、`storage`、`session_id`、`session_manager` | 确定工作目录、transcript 和恢复索引。 |
| provider | `provider`、`model`、`provider_name`、`provider_settings` | 指定初始模型以及后续切换所需的配置。 |
| prompt 与工具 | `system`、`custom_system_prompt`、`append_system_prompt`、`context_files`、`tools` | 决定发送给模型的系统提示和工具集合。 |
| 资源与扩展 | `resource_paths`、`skills_enabled`、`extension_paths`、`extensions_enabled` | 控制资源发现、技能和扩展装配。 |
| 运行策略 | `auto_compact_*`、`thinking_level`、`shell_command_prefix` | 控制压缩、思考等级和 shell 行为。 |
| 生命周期 | `owns_initial_provider`、`defer_authoritative_writes` | 明确资源所有权以及启动写入是否延迟提交。 |

其中 `system` 有一个重要语义：`None` 表示自动调用 `build_system_prompt()`；空字符串 `""` 仍然是显式覆盖，不应被当成缺省值。provider 也类似：调用方传入 provider 是兼容 seam；传 `None` 时，session 根据静态设置或动态 registry 构建 provider。

### 2.2 运行时状态是可替换的

`__init__()` 把配置转换成若干可变运行时对象：

```text
_config                 原始/更新后的配置快照
_state                  最近一次从 durable entries replay 的状态
_harness                当前 provider、model、system、tools、messages
_extension_runtime      当前 cwd 对应的扩展运行时
_last_parent_id         下一条 entry 应接到哪里
_owned_providers        由 session 负责 aclose 的 provider ledger
_pending_message_writes  尚未完整写入的 message + leaf 对
_context_usage_cache    transcript/system/tools 的 token 估算缓存
```

`_state` 和 `_harness.messages` 通常同步，但角色不同：前者是 durable tree 的 replay 结果，后者是下一次 agent loop 要使用的内存上下文。每次 append、branch、compaction 或 replacement 后，代码都会通过 `_refresh_persisted_state()` 和 `harness.replace_messages()` 重新对齐二者。

### 2.3 durable entries 才是恢复权威

session 不是覆盖写的单条 JSON，而是由父指针串起来的 append-only entries：

```text
SessionInfoEntry
  -> ModelChangeEntry
    -> ThinkingLevelChangeEntry
      -> MessageEntry(User)
        -> MessageEntry(Assistant + tool call)
          -> MessageEntry(ToolResult)
            -> LeafEntry(entry_id=当前末端)
```

常见 entry 类型见 [entries.py](../../../../src/tau_agent/session/entries.py:25)：`message`、`model_change`、`thinking_level_change`、`compaction`、`branch_summary`、`label`、`leaf`、`session_info` 和 `custom`。

`LeafEntry` 是活动分支指针，不是普通的“最后一行”。历史分支仍保留在文件中；恢复时 `SessionState.from_entries(entries, leaf_id=...)` 只重放 root-to-leaf 路径。[memory.py](../../../../src/tau_agent/session/memory.py:39) 负责把这条路径投影成 messages、model、thinking level、custom entries 和 context entry ids。

## 3. 生命周期总览

```text
CodingSession.load(config)
  ├─ read entries / replay state
  ├─ load eligible extensions
  ├─ resolve project trust
  ├─ load resources and build provider
  ├─ create harness and attach persistence listener
  └─ return session candidate

emit_pending_session_start()
  └─ UI bridge 已安装后，才触发 extension session_start

prompt() / continue_()
  ├─ input hook / expansion / queue decision
  ├─ AgentHarness → run_agent_loop
  ├─ MessageEndEvent → durable MessageEntry + LeafEntry
  ├─ error / overflow / route failover / auto compaction
  └─ AgentSettledEvent

reload() / resume() / new_session() / branch_to_entry()
  └─ prepare destination → durable commit → synchronous adoption → close old

aclose()
  └─ shutdown extension runtime → close owned providers exactly once
```

这里有两个关键时序原则：

1. **可取消的工作在 publication boundary 之前完成。** 例如 trust prompt、`session_start`、provider 创建失败，都不应留下“半切换”的 live session。
2. **publication boundary 之后只做同步状态替换和受控清理。** 一旦新 snapshot 已公开，旧 runtime 的异步关闭失败不能再伪装成“新 session 没有切换成功”。

## 4. `__init__()`：只装配，不负责加载

入口是 [session.py](../../../../src/tau_coding/session.py:369)。它接收已经准备好的 `state`、`harness`、资源和 runtime，因此本身是同步内存装配。

主要动作如下：

1. 保存 config/state/harness 和资源快照。
2. 从 config 初始化 provider 名称、HF route、thinking level、压缩配置。
3. 建立 `_owned_providers`、关闭 task、诊断 logger、凭据 store 和缓存。
4. 保存 `pending_initial_entries` / `_prepared_entries`，为延迟提交做准备。
5. 调用 `_attach_persistence_listener()`，把 session 的消息持久化函数订阅到 harness。

因此，`__init__()` 不负责 `read_all()`、项目信任询问或 provider 网络发现；这些必须放在异步工厂 `load()`，否则对象会在“半初始化”状态被前端看到。

一个容易误读的点：当前 `load()` 构造 `AgentHarnessConfig` 时传入 provider/model/system/tools/session_id，但没有把 harness 的 `before_tool_call` / `after_tool_call` 审批钩子接到 `CodingSessionConfig`。harness 保留了扩展点，并不代表当前 `CodingSession` 已经提供了 coding 层审批配置。

## 5. `load()`：候选 session 的完整装配

入口是 [session.py](../../../../src/tau_coding/session.py:443)。它是整个类最重要的工厂方法，可以拆成九步。

### 5.1 读取 transcript，创建新 session 的初始链

```python
entries = await config.storage.read_all()
```

空文件不会立刻写入，而是生成内存中的：

```text
SessionInfoEntry(cwd)
  -> ModelChangeEntry(provider, model)
    -> ThinkingLevelChangeEntry(thinking_level)
```

它们被保存为 `pending_initial_entries`。这样可以让一个用户只打开、没有真正发送消息的会话不产生空 transcript；首次需要权威写入时再由 `_ensure_session_initialized()` 提交。

已有历史先经过 `_detach_missing_parents()`：导入的 branch 如果 root 的 `parent_id` 指向当前文件不存在的 entry，就把这个 root detach 成新的 root。这样坏掉的外部父指针不会让整个 session replay 失败。

### 5.2 replay 线性状态和活动 leaf

`load()` 先用全部 entries 得到 `linear_state`，再找最后一个 `LeafEntry`：

- 有 leaf：以 leaf 为活动末端，只 replay 该分支；
- 没有 leaf：按存储顺序线性 replay。

随后用 `last_parent_id` 记录新的 entry 应接的位置。注意“存储中最后一行”和“活动分支末端”不是同一个概念，这正是 branch 能保留历史的原因。

### 5.3 创建 cwd-bound extension runtime

session 不会把 source project 的 extension runtime 原封不动带到 destination cwd。它会先创建一个新 `ExtensionRuntime`，继承必要的 credentials/environment/http client，但先只加载允许的资源扩展，不加载 project extension。

原因是 project extension 可能执行任意 Python 代码；在 project trust 决定前加载它，会让不可信项目代码越过信任边界。只有 trust 通过且 `project_extensions_enabled` 显式开启时，才进行第二次 project extension load。

### 5.4 解析 project trust 和资源路径

`ProjectTrustCoordinator.resolve()` 给出 canonical cwd 和 trusted 状态。随后 `resource_paths_with_project_trust()` 把 trust 结果投影到资源路径：不可信项目的 prompt、skills、theme 和 extension 不应继续可见。

`_load_session_resources()` 再加载：

- skills 与 prompt templates；
- project context 文件；
- discovered custom/append system prompt；
- 非致命 `ResourceDiagnostic`。

信任取消是特殊情况：它不是普通 resource warning，而是 candidate 不能被发布。`defer_authoritative_writes=True` 时 trust cache 和启动 metadata 也暂不成为权威状态。

### 5.5 provider 选择和 model 能力

当 `config.provider is None` 时，`_prepare_provider_selection()` 在 trusted registry 完成后选择 provider/model。选择要结合：

1. 启动时显式请求；
2. durable session 中的 provider/model；
3. 当前静态设置或 dynamic provider registry；
4. 默认 provider/model。

之后 `_runtime_model_for_state()` 处理 durable model 已经不在当前 provider catalog 的情况，必要时回退到 config model 或 provider default。再根据模型能力创建 `ImageSupportState`，因为 `read` 工具是否把图片送给模型取决于当前 active model。

### 5.6 tools、system prompt 和 harness

工具选择逻辑是：

```text
config.tools != None  → 使用宿主提供的工具
config.tools == None  → create_coding_tools(cwd, shell prefix, image support)
两者都经过 extension_runtime.compose_tools()
```

当 `config.system` 为 `None`，用实际 tools、skills、context files、extension guidelines/sections 调用 `build_system_prompt()`；否则直接使用显式 system。最后创建：

```python
AgentHarness(
    AgentHarnessConfig(
        provider=config.provider,
        model=active_model,
        system=system,
        tools=tools,
        session_id=config.session_id,
    ),
    messages=state.messages,
)
```

这一步体现职责分离：system prompt 是 coding 层产物，但 agent loop 只接收最终字符串和工具协议。

### 5.7 绑定 runtime、修复历史、推迟启动事件

session 创建后会：

- 如果 session 拥有初始 provider，将它加入 `_owned_providers`；
- `_persist_active_tool_history_repairs()` 修复 dangling assistant tool call；
- 刷新 thinking level 和 live model limits；
- `extension_runtime.bind(session)`，让扩展 API 看见当前 session；
- 订阅 extension 的 harness listener；
- 设置 `_session_start_pending=True`。

`session_start` 不在 `load()` 中立即触发。前端需要先安装 UI bridge，让扩展的通知和交互请求有接收方，然后调用 `emit_pending_session_start()`。该方法幂等，并在成功后提交 staged project trust。

## 6. 提问主链：`prompt()` 和 `continue_()`

### 6.1 `prompt()` 的前半段：输入先过 coding 层

入口是 [session.py](../../../../src/tau_coding/session.py:2709)。它不是简单的 `return harness.prompt(content)`，而是一个事件流装饰器：

```text
raw content
  → extension input hooks
  → prompt template / skill expansion
  → running? steer/follow_up/error
  → flush pending writes
  → refresh model limits
  → try auto compact
  → create UserMessage / CustomMessage
  → harness.prompt_message()
```

输入 hook 可以直接 `handled`。此时 session 通知 UI 并返回，不会启动 agent run，也不会伪造 `AgentSettledEvent`。

技能和 prompt template 的扩展由 `expand_prompt_text()` 完成：template 优先，skill 其次，均不匹配则原样返回。它们是“送进模型前的展开指令”，不是普通 slash command，所以 `handle_command()` 对 template 会返回 `handled=False`，让输入继续走 prompt 路径。

### 6.2 运行中的 prompt：steering 和 follow-up

如果 harness 正在运行：

- `streaming_behavior="steer"`：加入 steering queue，尽快插入当前 run 的后续上下文；
- `streaming_behavior="follow_up"`：加入 follow-up queue，当前 run 完成后再处理；
- 没指定策略：抛出错误，避免并发启动两个 agent loop。

加入队列后只产生一个 `QueueUpdateEvent`，不会调用 provider。这使 TUI 可以在不打断流式响应的情况下提交新消息。

### 6.3 事件循环：哪些事件被改写

`harness.prompt_message()` 返回 `AsyncIterator[AgentEvent]`。session 对事件做三类工作：

1. 让 persistence listener 在 `MessageEndEvent` 时写消息；
2. 对 assistant error 写诊断，并识别 context overflow 或 HF route failure；
3. 把底层 `AgentEndEvent` 包装成 `SessionAgentEndEvent`。

`SessionAgentEndEvent.will_retry` 很关键：底层 agent loop 已经结束，但 coding 层可能还要 overflow compaction、自动 retry 或 route failover。因此前端看到 `agent_end` 不一定意味着整个 session 已 settle；最终边界是 `AgentSettledEvent`。

### 6.4 overflow、自动压缩和重试

当 assistant error 被 `is_context_overflow_error()` 识别为上下文超限时，`prompt()` 会：

```text
SessionAgentEndEvent(will_retry=True)
  → CompactionStartEvent(reason="overflow")
  → _try_overflow_compact()
  → CompactionEndEvent(will_retry=是否成功)
  → AutoRetryStartEvent
  → harness.continue_()
  → AutoRetryEndEvent
```

如果没有 overflow，而是符合条件的 Hugging Face pre-output route error，则进入 `_run_huggingface_route_failover()`。其他正常完成的路径则尝试一次自动压缩。

### 6.5 `continue_()` 的差异

入口是 [session.py](../../../../src/tau_coding/session.py:2906)。它复用绝大多数事件处理和 reconciliation 逻辑，但不新增 UserMessage，直接让 harness 从当前 transcript 继续。

二者的主要差异：

| 方法 | 起点 | 特有行为 |
| --- | --- | --- |
| `prompt()` | 先追加 User/Custom message | input hook、模板/技能展开、运行中队列、overflow retry。 |
| `continue_()` | 使用已有 context | 常用于恢复或重试，不接收新的用户正文。 |

两者都会在 `finally` 中 reconciliation，并在有实际 run 时 dispatch `AgentSettledEvent`。这保证前端取消消费事件流后，session 仍有机会完成持久化收尾。

## 7. 持久化：push listener 是真正的边界

### 7.1 为什么不是前端消费事件时写

`__init__()` 通过 `_attach_persistence_listener()` 订阅 harness：

```text
AgentHarness._notify(MessageEndEvent)
  ├─ CodingSession._persist_on_message_end()
  │    └─ _persist_message(message)
  ├─ ExtensionRuntime listener
  └─ prompt()/TUI 的 async iterator consumer
```

因此 TUI 按 Escape 取消自己的消费 task 时，消息写入不依赖 TUI 是否继续 `async for`。这尤其重要，因为 harness 在取消清理时还会合成 `ToolResultMessage("Tool call interrupted by user")`，并主动通知 listener。

### 7.2 `_persist_message()`：message + leaf 成对提交

入口是 [session.py](../../../../src/tau_coding/session.py:3073)。第一次遇到某个 message object 时，它创建稳定的 `_PendingMessageWrite`：

```text
MessageEntry(parent_id=_last_parent_id, message=message)
  -> LeafEntry(parent_id=message_entry.id, entry_id=message_entry.id)
```

随后按顺序追加 message entry 和 leaf，再 `_refresh_persisted_state(leaf_id=...)`。稳定 entry id 让部分失败重试时不会重复追加 message 或 leaf；只有 retry 路径才额外读取 durable ids。

### 7.3 reconciliation 和 pending write

`_reconcile_run_persistence()` 在每次 run 的 `finally` 执行：

1. 关闭未完全消费的 events iterator；
2. 遍历 harness transcript；
3. 找出收到 `MessageEndEvent` 但未标记 persisted 的同一 message object；
4. 重试 `_persist_message()`；
5. 清空本轮 identity sets。

如果 storage 在 message entry 和 leaf 之间失败，`_pending_message_writes` 会保留稳定 entry pair。下一次 prompt、continue、branch 或 compaction 前，`_flush_pending_message_writes()` 先把这笔写完整；无法恢复时写诊断并阻止继续做依赖 storage 的操作。

### 7.4 初始 entries 和批量事务

`_ensure_session_initialized()` 负责在第一次真实 append 前提交初始 metadata；`_append_session_batch()` 优先调用 storage 的 `append_batch()`，老的兼容 storage 才退化成逐条 append。

生产 `JsonlSessionStorage.append_batch()` 会在同目录临时文件写入、fsync 后 atomic replace，并在写入前持有 session lock。[storage.py](../../../../src/tau_agent/session/storage.py:38) 因此把“模型切换 entry + leaf”“启动 repair batch”等操作变成完整可见或完全不改变旧文件的事务边界。

## 8. 观察方法、命令和直接终端命令

### 8.1 只读 facade

这些 property 把内部状态安全地暴露给前端：

- `messages`：harness transcript 的 tuple snapshot；
- `state`：最近 replay 的 `SessionState`；
- `tools`、`available_models`、`available_model_choices`：当前有效能力；
- `context_usage` / `context_token_estimate`：带缓存的 context 估算；
- `system_prompt`、`resource_diagnostics`、`extension_names`：可观察运行环境；
- `session_stats`：活动分支上的请求、token 与计费统计；
- `queued_messages` / `queue_update_event()`：UI 队列视图。

context 缓存会在 prompt、tool execution、branch、compaction、model/tool/system 变化后由 `_invalidate_context_usage_cache()` 清掉。缓存是性能优化，不是权威数据。

### 8.2 `handle_command()` 和 `expand_prompt_text()`

`handle_command()` 将真正的 slash command 交给 `CommandRegistry.execute()`。它先检查 prompt template；如果输入是 template expansion，就返回 `handled=False`，让上层继续调用 `prompt()`。

当前 command registry 可以扩展出 model、tree、compact、reload、session 等功能，但 session 仍是最终执行环境。命令处理器不应自行重建 provider 或读取 transcript。

### 8.3 `run_terminal_command()`

这是输入栏 `!command` 的 coding 层直通路径，不进入模型：

1. trim 并拒绝空命令；
2. 按 session cwd 和 shell prefix 创建 bash tool；
3. 执行命令并解析 `exit_code`；
4. `add_to_context=True` 时把输出包装成 UserMessage，追加到 harness 并持久化；
5. 返回结构化 `TerminalCommandResult`。

因此“执行命令”和“把命令结果加入 agent context”是两个显式选择，避免所有 UI 预览命令都污染对话历史。

## 9. 模型、provider 和 thinking 切换

### 9.1 只切当前 provider 的模型

`set_model()` 先校验 active provider 是否声明该模型，再更新 harness model、HF route、thinking/image capability 和默认配置。它是同步的“当前运行时设置”入口。

`apply_startup_model_override()` 是异步启动覆盖：除了更新运行时，还追加 `ModelChangeEntry + LeafEntry`，让显式启动选择进入 session 历史。

### 9.2 跨 provider 的 candidate-first 切换

`select_provider_model()` 是更完整的异步切换路径：[session.py](../../../../src/tau_coding/session.py:1381)。其顺序刻意不能调换：

```text
拒绝运行中切换
  → 创建 candidate provider
  → 校验 candidate model/capability
  → append_batch(ModelChangeEntry, LeafEntry)
  → 以上都成功后，才替换 harness/config/provider 字段
  → 把旧 provider 从 ownership ledger 中关闭
```

如果 provider 创建或 durable batch 失败，candidate 被关闭，旧 provider、旧 model 和旧 transcript 保持不变。commit 之后，index refresh 失败只记 warning，因为 transcript 已经是权威事实。

### 9.3 thinking level 与 live limits

`set_thinking_level()` 校验模型支持的 levels，刷新 runtime provider，追加 thinking-level entry 和 leaf，然后更新 manager preference。`_refresh_runtime_model_limits()` 对实现 `ModelLimitsProvider` 的 provider 做 live discovery；失败时保留静态 catalog，并通过 `model_limits_discovery_error` 暴露非致命诊断。

`context_window_tokens` 的来源优先级是 live provider catalog，其次是配置 catalog，最后是默认窗口。`auto_compact_token_threshold` 再根据显式配置或 context window 推导。

## 10. 分支：移动 leaf，不删除历史

### 10.1 `tree_choices()`

`tree_choices()` 读取全部 entries，过滤可分支的 user/assistant message、compaction 和 branch summary，再计算缩进和标签。tool result 不单独作为用户可选的 branch point；带 tool call 的 assistant message 会显示为 `tool call: ...`。

树遍历使用显式 stack，而不是递归，且用 `expanded` 防止坏 parent cycle 无限循环。这是 UI 读取深 session 时的健壮性设计。

### 10.2 `branch_to_entry()`

入口是 [session.py](../../../../src/tau_coding/session.py:866)。它首先拒绝运行中的 session，并 flush pending persistence，然后：

```text
验证 entry_id 与可分支类型
  ├─ summarize=True
  │    └─ 对被放弃的 active-path messages 生成 BranchSummaryEntry
  ├─ 选中 UserMessage 且不 summarize
  │    └─ target_id = 该 user entry 的 parent，并返回 input_prefill
  └─ 其他情况 target_id = 选中 entry
追加 LeafEntry(target_id)
  → replay 新 active path
  → 修复可能暴露的 dangling tool history
  → 替换 harness messages
```

关键点是“追加新 leaf”，而不是删除 leaf 之后的历史。summary 也只是新 entry；原 branch 仍可在树中找到。切换 branch 后还要从新 state 恢复 model/thinking，并刷新 runtime provider。

## 11. 压缩：用 entry 表达上下文替换

### 11.1 manual compaction

`compact()` 对当前 active context 的全部消息生成摘要；`compact_detailed()` 则保留最近一段真实 entries，并返回 `first_kept_entry_id`、压缩前后 token 估算和替换数量。

两者最终都调用 `_append_compaction()`：

```text
CompactionEntry(
  parent_id=_last_parent_id,
  summary=...,
  replaces_entry_ids=[旧 message entry ids],
  first_kept_entry_id=...
)
  -> LeafEntry
  -> SessionState.from_entries(...)
  -> harness.replace_messages(state.messages)
```

replay 时，`SessionState._apply_compaction()` 用一个 `UserMessage("Previous conversation summary: ...")` 代替被标记的旧 entries。摘要因而既持久化，又能重新进入下一次 provider context。

### 11.2 自动 compaction 和 overflow compaction

- `_maybe_auto_compact()`：正常 turn 前/后检查阈值，保留最近 context，适合预防性压缩。
- `_try_overflow_compact()`：只在 provider 返回 context overflow 后运行，并将原 overflow 保持为前端可见错误；压缩成功才 retry。
- `_generate_compaction_summary()`：以无 tools 的 summarization prompt 调当前 provider，避免摘要请求再次触发工具循环。

自动运维失败通常只记录 diagnostic 并返回 `False`，不会把原本已经完成的 agent turn 伪装成失败。

## 12. `reload()`、`resume()`、`new_session()`：替换当前 snapshot

这三个操作共享同一个架构思想：**replacement 先成为 candidate，成功后再 adoption**。

### 12.1 `reload()`

`reload()` 在同一个 session object 中创建 staged runtime：

1. 记录旧 skills、tools、prompt inputs、extensions 的 signatures；
2. 加载 eligible extensions；
3. 重新 resolve trust 和 resources；
4. 必要时重建 tools、commands、system prompt；
5. 在 publication 前完成旧 runtime shutdown、新 runtime session_start；
6. 同步替换 session 的 runtime/resources/harness config；
7. retire 旧 runtime，并异步完成其 provider/task close；
8. 返回 `CodingReloadSummary`。

如果 system prompt 输入未变化，reload 不会无意义地重建 prompt。若在 publication 前取消或失败，旧的资源、trust cache 和运行时保持不变。

### 12.2 `resume()`

`resume(session_id)` 从 `SessionManager` 读取目标 record，按目标 cwd、目标 transcript path、目标 provider/model 调用 `type(self).load(...)` 构造 replacement。

对于 dynamic provider，不能把 source session 的 provider object 搬过去，因为 provider 定义与 generation/cwd/trust 有关；代码会在 destination runtime 重新解析。candidate 完成后调用 `_adopt_replacement(replacement, reason="resume")`。

### 12.3 `new_session()`

`new_session()` 先由 manager `prepare_session()` 生成一个待索引 record，再加载一个空 transcript replacement。新 session 通常是 pending/unindexed 的，直到第一次真实持久化；这避免用户只点击“新建”就制造无法恢复的空记录。

### 12.4 `_adopt_replacement()` 的提交边界

入口是 [session.py](../../../../src/tau_coding/session.py:2435)。其流程是：

```text
检查 trust 未取消
  → replacement._commit_prepared_entries()
  → old runtime shutdown
  → clear old UI components
  → replacement runtime session_start
  → trust commit
  → old runtime retire
  → 同步转移 state/harness/resources/provider ownership
  → 重新绑定 extension runtime 和 persistence listener
  → 关闭 old runtime
```

特别容易漏掉的是 listener：replacement 原本监听自己的 harness，adopt 时必须解除它，再由外层 session 重新订阅，否则后续 persistence 会更新已废弃 replacement 的 `_last_parent_id`。

## 13. 关闭和资源所有权

`aclose()` 使用一个独立 task 保存 `_close_task`。调用者即使被取消，关闭 task 仍继续运行；所有 resource/provider 都尝试关闭后，第一次调用才传播取消或首个错误。重复调用只观察同一个完成 task，因此是幂等的。

`_close_owned_resources()` 的顺序是：

1. active extension runtime 发 `session_shutdown("quit")`；
2. 清理 UI component；
3. `ExtensionRuntime.aclose()`，处理 registry/task；
4. 从 `_owned_providers` ledger 取出全部 provider，逐个 `aclose()`；
5. 一个 provider 失败也继续关闭后面的 provider，最后再抛首个错误。

这套规则保证“一份所有权只有一个 closer”：adopt 成功后 ownership 转移给外层 live session；candidate 在 adopt 前失败则由 candidate 自己关闭；旧 runtime 在 publication 后负责自己的异步 drain。

## 14. 事件契约和前端应该如何使用

`CodingSessionEvent` 是 `AgentEvent | SessionOwnEvent`。[events.py](../../../../src/tau_coding/events.py:7) 中的 session-owned events 包括：

| 事件 | 前端用途 |
| --- | --- |
| `SessionAgentEndEvent` | 展示一轮 agent loop 结束，并检查 `will_retry`。 |
| `AgentSettledEvent` | 清除 loading 状态、允许下一次普通输入。 |
| `QueueUpdateEvent` | 更新 steering/follow-up 队列 UI。 |
| `CompactionStart/EndEvent` | 展示压缩和 retry 状态。 |
| `EntryAppendedEvent` | 增量刷新 session tree 或 transcript。 |
| `ThinkingLevelChangedEvent` | 更新 thinking 控件。 |
| `AutoRetryStart/EndEvent` | 展示自动重试进度或最终错误。 |

一个最小前端循环应类似：

```python
async for event in session.prompt(user_text):
    renderer.render(event)
```

它不需要手动保存 `session.messages`，也不需要自己判断何时写 leaf。若需要提前取消，应调用 `session.cancel()`，并继续等待/处理 settle 相关生命周期，或由宿主确保 session 最终 `aclose()`。

## 15. 最值得记住的设计原则

### 15.1 内存 transcript 与 durable tree 分离

Agent loop 需要可变的内存消息；恢复、分支和审计需要不可变的追加历史。`CodingSession` 用 `_last_parent_id`、`LeafEntry` 和 replay 同时满足两者，而不是让 JSONL 成为一个可随意覆盖的缓存。

### 15.2 消息生命周期事件是持久化边界

`MessageEndEvent` 表示消息已经完成，适合变成 `MessageEntry`。`MessageStartEvent` 只代表开始，不应产生半条权威历史。取消时 harness 主动发出合成的 start/end repair，保证工具调用链仍可恢复。

### 15.3 trust 是代码加载边界，不只是资源过滤器

project trust 同时控制 context、skills、themes 和 project extensions。extension runtime 必须先构造 eligible snapshot，再决定是否加载项目代码；resume/reload 也不能直接复用另一个 cwd 的 runtime。

### 15.4 cancellation 不等于 rollback

publication 前取消可以保持旧状态不变；publication 后取消只能被 containment/cleanup 吸收，不能声称切换从未发生。这个区分贯穿 reload、adoption 和 close。

### 15.5 public API 优先于前端猜测

前端应从 `available_model_choices`、`available_thinking_levels`、`context_usage`、`resource_diagnostics` 读取能力，而不是自行解析 provider config 或 session JSONL。这样 provider、dynamic extensions 和 future frontend 才能保持可替换。

## 16. 测试阅读路线

行为测试集中在 [test_coding_session.py](../../../../tests/test_coding_session.py)，建议按下面顺序阅读：

1. `test_load_empty_session_defers_transcript_file`：理解初始 entries 的延迟写入。
2. `test_prompt_persists_user_assistant_and_leaf_entries`：理解一次普通 prompt 的基本持久化。
3. `test_cancelled_prompt_teardown_persists_interrupted_tool_result`：理解取消和 push persistence。
4. `test_message_persistence_retry_is_idempotent`：理解 message/leaf pair 的重试。
5. `test_session_branches_to_previous_entry_without_destroying_history`：理解 leaf 移动而不删除历史。
6. `test_session_compact_persists_summary_and_rebuilds_context`：理解 compaction replay。
7. `test_session_compacts_and_retries_once_after_context_overflow`：理解错误、压缩、重试事件链。
8. `test_session_reload_refreshes_resources_and_system_prompt`：理解 staged reload。
9. `test_session_adoption_transfers_all_runtime_provider_ownership`：理解 replacement ownership 转移。
10. `test_aborted_replacement_closes_only_candidate_provider_once`：理解 adoption 失败时的 cleanup。

本地运行 coding session 测试：

```bash
uv run pytest tests/test_coding_session.py
```

## 17. 推荐源码阅读顺序

如果要继续修改 `CodingSession`，建议沿着一条最短路径读：

```text
1. CodingSessionConfig / __init__
2. load
3. prompt
4. _attach_persistence_listener / _persist_message
5. SessionState.from_entries
6. branch_to_entry / _append_compaction
7. select_provider_model
8. reload / _adopt_replacement
9. aclose / _close_owned_resources
```

读懂这九组方法后，再按需求深入 `resources.py`、`provider_config.py`、`extensions/runtime.py` 或 `context_window.py`。不要从 4400 多行的 `session.py` 顶部一路顺读；它现在已经是多个后续 phase 叠加后的应用边界，按生命周期切片更容易看清架构。

