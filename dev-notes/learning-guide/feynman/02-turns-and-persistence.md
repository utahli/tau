# 02：一次回合、事件队列与 push persistence

本章只追踪一条交互输入如何流过 `CodingSession.prompt()`。核心纪律是：**前端看到事件并不
等于前端负责写历史**。因为前端可能在取消时停止枚举 generator，而 session 仍必须完成已
结束消息的持久化。

## 1. `prompt()` 的主路径

```text
raw input
  -> ExtensionRuntime.run_input_hooks()
  -> expand_prompt_text()               # prompt template 优先，之后 /skill:
  -> running?  queue steer/follow_up or reject
  -> flush earlier partial writes
  -> refresh live model limits / try pre-turn auto compact
  -> AgentHarness.prompt_message(UserMessage or CustomMessage)
  -> agent events ---------------> persistence listener + caller
  -> overflow/failover/auto-compact handling
  -> reconcile persistence -> AgentSettledEvent
```

### 1.1 输入先过 hook，再决定它是不是回合

`run_input_hooks()` 可拦截或改写文本；handled 结果只通知 UI 后返回。未被处理的文本经过
`expand_prompt_text()`：prompt template 的 slash 形式优先于 skill。这个位置很关键，意味着
print、TUI、RPC、extension 发起的输入共享相同展开语义。

如果 harness 正在运行，普通 prompt 会抛错；显式 `streaming_behavior="steer"` 或
`"follow_up"` 则进入 harness queue，立即 yield `QueueUpdateEvent`。这不是另起并发
agent loop：队列会由当前 loop 在其安全时机排空。

### 1.2 session 装饰而非重写 agent loop

空闲时 session 构造 `UserMessage`（或带 display metadata 的 `CustomMessage`）并调用
`harness.prompt_message()`。`AgentHarness._run()` 调用 `run_agent_loop()`，对每个 event 先
`_notify(listener)` 再 yield。这带来一个很好的性质：persistence subscriber 和前端消费者
看到同一事件序列，但前者不依赖后者继续消费。

session 主要做四种转换/附加工作：

| 收到的情况 | session 动作 |
| --- | --- |
| `ToolExecutionEndEvent` | 使 context usage cache 失效。 |
| 失败的 assistant `MessageEndEvent` | 写诊断；检测 context overflow 或可 failover 的 HF route。 |
| `AgentEndEvent` | 改为 `SessionAgentEndEvent(will_retry=...)`，前端知道这可能不是最终结束。 |
| 首个 user `MessageEndEvent` | 异步尝试自动命名，但先 yield 确认的 user event。 |

无论成功、错误还是 generator 被取消，`finally` 都执行 `_reconcile_run_persistence()`，随后
yield `AgentSettledEvent`。所以 UI 应以 settled 判断“真正可以解除工作态”，而不是只看
底层 agent end。

## 2. 消息结束是 durable boundary

在 `__init__()`，`_attach_persistence_listener()` 订阅 harness。每个 `MessageEndEvent` 到达
`_persist_on_message_end()`，再进入 `_persist_message()`：

```text
completed message M
  -> MessageEntry(parent_id = last_parent_id, message = M)
  -> LeafEntry(parent_id = MessageEntry.id, entry_id = MessageEntry.id)
  -> append both / refresh active SessionState
  -> last_parent_id = MessageEntry.id
```

`LeafEntry` 看似冗余，实则是“当前分支是谁”的持久化声明。分支后文件最后一条 message
不一定是当前路径的最后 message；读取时 session 找最后的 `LeafEntry`，用其 `entry_id`
replay。

### 2.1 为什么 `MessageEndEvent`，不是 `MessageStartEvent` 或 `AgentEndEvent`？

- start 之后 message 仍有 delta、tool calls 或最终 error，写下去会把半成品伪装成历史；
- agent end 太晚：一轮中 user、assistant、tool result 都应独立可恢复；
- end 恰好意味着该条 `AgentMessage` 已经稳定，且 harness listener 能在消费端离开后继续
  收到它。

这是 push-based persistence 的含义：写盘由 agent event production 推动，不由 TUI 的渲染
循环拉动。

## 3. 失败重试为什么不会重复写消息？

`_PendingMessageWrite` 固定保存 message object、`MessageEntry` 和 `LeafEntry`。第一次尝试
mint 新 id，不必整文件扫描；若中途 message 成功、leaf 失败，下一次尝试用相同 ids 并读取
durable ids，只补缺的那一条。成功后才从 `_pending_message_writes` 删除。

`_reconcile_run_persistence()` 在 finally 中找“已结束但尚未标记 persisted”的消息重试；
`_flush_pending_message_writes()` 在 resume/new/compact/下一个 prompt 等 storage-dependent
动作前强制清空。写入再次失败时记录 diagnostic，但不会用新的异常遮蔽用户取消。

费曼复述：这像物流的包裹单号。第一次投递失败时重投同一包裹，而不是重新生成一个单号并
希望仓库猜出哪个是真的。

## 4. 中断、工具 repair 和 queue

`CodingSession.cancel()` 仅转发到 `harness.cancel()`。Harness 在 finally 中检查 cancellation：
如果 assistant 已发出 tool call 但没有 result，它合成错误 `ToolResultMessage("Tool call
interrupted by user")`，并通过 listener 发出 start/end。这样 history 对每个 tool call 都有
结局，下一次模型请求不会看到悬空 call。

加载时 session 还运行 `_persist_active_tool_history_repairs()`，修复旧 transcript 的 dangling
tool history。repair 是会写 entry/leaf 并重放的 session 事务，而不是 renderer 的临时补丁。

steering 与 follow-up 都是 `AgentHarness` 的两个 `deque`：默认一次 drain 一个 message；UI
可用 `queued_messages`/`QueueUpdateEvent` 显示待处理内容，`clear_queued_messages()` 与
`pop_latest_*()` 只改队列，不改已持久化历史。

## 5. overflow、retry 与 settle

context overflow 时，session 依次发：

```text
SessionAgentEndEvent(will_retry=True)
-> CompactionStartEvent
-> CompactionEndEvent(will_retry=compacted)
-> AutoRetryStartEvent
-> harness.continue_()
-> AutoRetryEndEvent
-> AgentSettledEvent
```

Hugging Face automatic route failover 走相似的 retry 事件，但只有符合“可重试、尚未输出”的
provider error 才会尝试，并且限制次数。这里把重试编排放在 coding session，而不是 harness，
因为它依赖 context compaction、provider route 与诊断策略。

## 6. 用测试校准心智模型

```bash
uv run pytest tests/test_coding_session.py -q
uv run pytest tests/test_tool_history.py tests/test_agent_harness.py -q
```

优先定位下列测试名：

- `test_tool_results_are_persisted`
- `test_continue_persists_only_new_messages`
- `test_session_compacts_and_retries_once_after_context_overflow`
- `test_session_auto_compacts_after_response_when_threshold_is_exceeded`

自测：若 TUI 因 Ctrl-C 不再读取 async iterator，哪条机制仍使完整 `MessageEndEvent` 写盘？
答案必须同时提到 harness listener、session persistence subscriber 和 finally reconciliation。
