# 01：三类状态、replay 与 `CodingSession.load()`

本章回答三个问题：

1. `CodingSession` 的“三类状态”分别是什么？
2. 文档中的 `replay()` 在代码里对应什么？
3. `load()` 执行后，哪些状态被创建、哪些内容只是暂存、哪些内容写入磁盘？

下面的解释以当前源码为准，主要入口是：

- `src/tau_coding/session.py`
- `src/tau_agent/session/memory.py`
- `src/tau_agent/session/entries.py`
- `src/tau_agent/session/tree.py`

## 1. 先把“三类状态”分开

把一个编码会话想象成一张工作台：

| 层次 | 主要对象 | 它回答的问题 | 是否跨进程保留 |
| --- | --- | --- | --- |
| 配置蓝图 | `CodingSessionConfig` | “这个 session 应该怎样启动？” | 不直接保存，是一次启动输入 |
| live runtime | `_harness`、`_extension_runtime`、provider、队列、缓存等 | “当前进程下一步怎样执行？” | 不保留，进程结束后需要关闭 |
| durable history | JSONL 中的 `SessionEntry`，以及 replay 得到的 `SessionState` | “重启后应该相信什么、模型下一次应看到什么？” | 是 |

判断某个字段属于哪一层，可以问：

> 进程崩溃后，还需要知道它吗？

需要恢复的事实进入 history；只服务于本次运行的对象属于 runtime；控制启动方式的输入属于配置。

## 2. 配置蓝图：`CodingSessionConfig`

`CodingSessionConfig` 是 frozen dataclass，描述创建 session 所需的输入，例如：

```python
CodingSessionConfig(
    provider=...,
    model="...",
    storage=...,
    cwd=Path(...),
    tools=...,
    provider_name="...",
    thinking_level="...",
    skills_enabled=True,
    extensions_enabled=True,
    project_extensions_enabled=False,
    trust_override=...,
    session_manager=...,
)
```

它不是运行中的 session，也不是历史记录。

由于它是 frozen 的，`load()` 或模型切换不会原地修改调用方传入的对象，而是使用：

```python
config = replace(config, ...)
```

生成新的配置快照。

`load()` 可能在这个快照中补充或修正：

- canonical cwd；
- 根据 trust 计算的 resource paths；
- extension runtime；
- provider、model 和 provider name；
- inference provider；
- runtime provider config；
- dynamic provider；
- `owns_initial_provider` 等 ownership 信息。

最终保存进 `CodingSession` 的，是这个规范化后的 config 副本，而不是原始对象。

## 3. live runtime：当前进程的可执行状态

`CodingSession.__init__()` 接收已经准备好的 state、harness 和资源，然后保存运行时对象：

```python
self._config
self._state
self._harness
self._extension_runtime
self._owned_providers
self._resource_paths
self._skills
self._prompt_templates
self._context_files
self._pending_message_writes
self._context_usage_cache
self._thinking_level
```

其中：

- `_harness` 保存内存中的 messages，并负责 agent loop；
- `_extension_runtime` 管理 extension、command 和 UI bridge；
- provider 是真正调用模型的客户端；
- `_owned_providers` 列出 session 负责关闭的 provider；
- `_pending_message_writes` 保存尚未完整写入的消息；
- `_context_usage_cache` 是可以重新计算的缓存；
- 队列、监听器和 diagnostic logger 也属于 runtime。

这些对象代表“现在如何工作”。它们不会直接成为重启后的数据来源。

## 4. durable history：`SessionEntry` 和 `SessionState`

### 4.1 `SessionEntry` 是可追加的事实记录

每条 entry 都有共同字段：

```python
id: str
parent_id: str | None
timestamp: float
```

当前支持的 entry 类型包括：

- `MessageEntry`：user、assistant、tool result 等完整消息；
- `CustomMessageEntry`：extension 注入、并参与模型上下文的消息；
- `ModelChangeEntry`：模型/provider 选择变化；
- `ThinkingLevelChangeEntry`：思考级别变化；
- `CompactionEntry`：用摘要替代较旧上下文；
- `BranchSummaryEntry`：离开分支时带回的摘要；
- `LabelEntry`：给 entry 添加或移除书签标签；
- `LeafEntry`：旧格式中记录的末端指针；
- `SessionInfoEntry`：cwd、创建时间、标题等 metadata；
- `CustomEntry`：extension/application 自定义数据。

这些 entry 以 JSONL 形式追加到 storage。旧行不会被覆盖。

### 4.2 `SessionState` 是 replay 后的派生对象

`SessionState` 当前包含：

```python
SessionState(
    messages=...,
    model=...,
    provider=...,
    thinking_level=...,
    labels_by_id=...,
    label_timestamps_by_id=...,
    active_leaf_id=...,
    session_info=...,
    custom_entries=...,
    compaction_entries=...,
    context_entry_ids=...,
    entries=...,
)
```

它不是 JSONL 文件的原样拷贝，而是对历史进行解释后的结果，回答：

> 当前 active path 上有哪些消息？当前模型、provider、thinking level 和 session metadata 是什么？

## 5. `replay()` 在当前代码中对应什么？

当前源码没有叫 `replay()` 的公开函数。文档中的 replay，实际由：

```python
SessionState.from_entries(entries, leaf_id=...)
```

实现，代码位于 `src/tau_agent/session/memory.py`。

它不会修改：

- 原始 `entries` 列表；
- JSONL 文件；
- `CodingSessionConfig`；
- `_harness`；
- provider 或 extension runtime。

它做的是读取 entry，并创建一个新的 `SessionState`。

### 5.1 先确定回放路径

当传入 `leaf_id` 时，`path_to_entry(entries, leaf_id)` 会：

1. 建立 `id -> entry` 索引；
2. 从目标 entry 沿 `parent_id` 向上找根；
3. 检测重复 id、缺失 entry 和 cycle；
4. 反转结果，得到 root-to-leaf 顺序。

当前实现中，`LeafEntry` 是旧格式的兼容记录。`SessionState.from_entries()` 默认使用最后一个非 leaf entry
作为 active tip，历史 leaf 记录不会自动选择新的 tip。需要选择特定路径时，应显式传入 `leaf_id`。

### 5.2 再按顺序投影字段

`from_entries()` 遍历回放路径：

| entry | replay 后的变化 |
| --- | --- |
| `MessageEntry` | 加入 `messages` |
| `CustomMessageEntry` | 转成 `CustomMessage` 后加入 `messages` |
| `ModelChangeEntry` | 更新 `model` 和 `provider` |
| `ThinkingLevelChangeEntry` | 更新 `thinking_level` |
| `SessionInfoEntry` | 更新 `session_info` |
| `CustomEntry` | 加入 `custom_entries` |
| `CompactionEntry` | 用固定前缀的摘要消息替换旧消息 |
| `BranchSummaryEntry` | 转成一条带分支摘要前缀的 `UserMessage` |
| `LabelEntry` | 更新 `labels_by_id` 和时间戳映射 |
| `LeafEntry` | 兼容读取，但不直接决定默认 tip |

最后，函数重新构造一个新的 `SessionState`，并计算 `context_entry_ids` 与当前路径的 `entries`。

因此可以把 replay 记成：

```text
SessionEntry[] --replay--> 新的 SessionState
```

而不是：

```text
SessionEntry[] --修改--> 原始历史
```

## 6. `CodingSession.load()` 做了什么？

入口是 `src/tau_coding/session.py` 中的 `CodingSession.load()`。

### 第一步：读取或准备 history

```python
entries = await config.storage.read_all()
```

如果 storage 非空，`load()` 会调用 `_detach_missing_parents()`，把指向当前文件之外的 parent 在内存中脱钩，
然后执行：

```python
state = SessionState.from_entries(entries)
```

如果 storage 为空，则先在内存创建：

```text
SessionInfoEntry
  -> ModelChangeEntry
      -> ThinkingLevelChangeEntry
```

这三条 entry 放入 `pending_initial_entries`。它们此时可能还没有写进 JSONL。

### 第二步：创建候选 runtime

`load()` 接着创建一个新的 `ExtensionRuntime`，并先以
`include_project_dir=False` 加载允许的资源目录 extension。

之后解析 project trust，生成 canonical cwd 和新的 resource paths。只有在项目可信并且配置允许时，才加载
项目目录 extension。

这样做的结果是：用户拒绝 trust 时，项目 extension 在 import 前就不会被加载。

### 第三步：准备资源、provider 和 tools

`load()` 继续完成：

1. skills、prompt templates、context/AGENTS 文件加载；
2. provider/model 选择或创建；
3. 图片能力判断；
4. coding tools 创建；
5. extension tools 组合；
6. system prompt 构造。

如果 provider 是动态选择的，`config` 会被 `replace()` 成包含真实 provider 和实际 model 的新快照；空 session
的 pending model entry 也会同步修正，并重新 replay state，使 history 元数据和 runtime 选择一致。

### 第四步：创建 harness 并对齐内存上下文

`load()` 使用 replay 得到的 `state.messages` 创建 harness：

```python
harness = AgentHarness(
    AgentHarnessConfig(
        provider=config.provider,
        model=active_model,
        system=system,
        tools=tools,
    ),
    messages=state.messages,
)
```

此时数据流是：

```text
JSONL entries
    -> SessionState.messages
    -> AgentHarness.messages
```

### 第五步：创建 `CodingSession` 并绑定生命周期

随后构造 `CodingSession`，并完成：

- 根据 `owns_initial_provider` 登记 provider ownership；
- 必要时修复 active path 中悬空的 tool call；
- 应用 runtime model catalog；
- 应用 thinking-level override；
- 刷新 runtime model limits；
- 绑定 extension runtime 和 harness listener；
- 设置 `_session_start_pending=True`；
- 将 trust commit 延迟到 session 被前端采用之后。

`load()` 返回的是一个已准备好的 session candidate，而不是只读 history 的简单对象。

## 7. `load()` 对三类状态的具体影响

| 阶段 | 配置蓝图 | live runtime | durable history / state |
| --- | --- | --- | --- |
| 调用前 | 调用方提供原始 `CodingSessionConfig` | 本 session 的 runtime 尚未创建 | storage 中已有 JSONL，或为空 |
| 读取后 | 调用方对象不变 | 仍未完整创建 | 读取 entries，生成新的 `SessionState` |
| 准备后 | 生成 canonical cwd/provider 等规范化 config 副本 | 创建 runtime、provider、tools、harness | 空 session 的初始 entry 仍可能只是 pending |
| 返回时 | 保存到 `session._config` | 保存到 `_harness`、`_extension_runtime` 等字段 | 保存到 `session._state`，并把 messages 交给 harness |
| 首次权威写入后 | 通常不变 | harness 继续产生新消息 | pending 初始 entry、消息 entry 和其他 entry 才写入 storage |

### 空 session 为什么 `load()` 后 JSONL 仍可能为空？

因为 `load()` 只准备 `pending_initial_entries`。第一次真正追加 entry 时，
`_ensure_session_initialized()` 才会把它们批量写入。

如果配置了 `defer_authoritative_writes=True`，entry 会进入 `_prepared_entries`，等待
`PreparedCodingSession.adopt()` 提交。

### `load()` 会不会写 history？

通常不会立即写入空 session 的三条初始 entry，但有两个例外需要注意：

- active history 存在悬空 tool call 时，`_persist_active_tool_history_repairs()` 可能追加修复 entry；
- trust、model catalog 等其他持久化机制可能写各自的存储，但它们不等于 transcript history。

## 8. 最后再看 state 和 harness 何时同步

普通新消息由 harness 追加到内存；persistence listener 在消息完成后写 entry，并刷新 `_state`。

需要改变上下文形状的操作，例如：

- branch；
- compaction；
- tool-history repair；
- resume/new session replacement；

会重新 replay，然后调用：

```python
harness.replace_messages(_state.messages)
```

因此：

> `SessionState` 是 durable history 的解释结果；`AgentHarness.messages` 是当前模型请求实际使用的内存上下文。

## 9. 纸上练习

假设 storage 为空，cwd 是 `P`，最终选择模型 `m`：

1. 画出三条初始 entry 及 parent 关系；
2. 说明为什么 `load()` 返回后 JSONL 仍可能为空；
3. 追加一条 user message，画出新增消息 entry 和后续 leaf/parent 关系；
4. 假设存在一个旧分支，说明为什么 replay 后 harness 只看到一条路径；
5. 指出 replay 改变了哪些内存对象，又没有改变哪些持久对象。

建议核对：

- `tests/test_session.py` 的 tree/replay 测试；
- `tests/test_coding_session.py` 的 `test_load_restores_existing_transcript`；
- `tests/test_coding_session.py` 的 `test_load_restores_active_leaf_branch`；
- `tests/test_coding_session.py` 的 tool-history repair 测试。
