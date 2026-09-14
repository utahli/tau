# 02：一条 prompt 是怎样跑完并保存的

本章只跟踪一件事：用户输入 `hi` 后，Tau 到底做了什么。先记住一个原则：

> 前端负责展示事件；session 的 persistence listener 负责保存事实。

两者同时订阅 harness，所以即使 TUI 停止读取 async iterator，保存也不会依赖渲染循环。

## 1. 从文本到 agent loop

`CodingSession.prompt()` 的主路径如下：

```text
原始文本
  -> extension input hook（可拦截/改写）
  -> expand_prompt_text（prompt template 优先，再查 /skill:）
  -> 若正在运行：steer/follow_up 入队，否则报错
  -> 补完上一次未完成的写入
  -> 刷新模型限制，必要时先自动 compact
  -> 构造 UserMessage 或 CustomMessage
  -> AgentHarness.prompt_message()
  -> 事件同时送给 persistence listener 和前端
  -> overflow/failover/after-turn compact
  -> finally 中 reconcile，最后发 AgentSettledEvent
```

这里的 `async generator` 可以先按“边工作边吐出事件的迭代器”理解。`AgentHarness._run()` 调用
`run_agent_loop()`，每拿到一个事件，先通知所有 listener，再交给调用方 `yield`。

## 2. 输入还不是消息

输入先经过 extension hook；hook 可以说“我处理了，不要启动模型”，也可以返回改写后的文本。
随后 `expand_prompt_text()` 统一处理 prompt template 和 `/skill:`，因此 CLI、TUI、RPC 和
extension 发起的输入规则一致。

如果 harness 正在跑：

- 默认 prompt 会报错，避免并发修改同一条循环；
- `streaming_behavior="steer"` 放进 steering 队列；
- `streaming_behavior="follow_up"` 放进 follow-up 队列。

队列是两个 `deque`，由当前 agent loop 在安全时机取出，不会另起一个 agent。

## 3. 为什么在 `MessageEndEvent` 保存？

一次完整消息可能经历 start、多个 delta、tool call，甚至以 error 结束。只有 end 到达时，消息对象
才稳定。session 收到它后执行：

```text
完成的 message M
  -> MessageEntry(parent_id=上一个末端, message=M)
  -> LeafEntry(parent_id=M.id, entry_id=M.id)
  -> 写入并刷新 SessionState
```

`LeafEntry` 看起来多余，却是分支选择器：没有它，重启时无法知道这条消息是否是 active path 的末端。

不用 `MessageStartEvent`：那只是半成品；不用 `AgentEndEvent`：一轮里 user、assistant、tool result
都应分别恢复，而且 agent end 已经太晚。

## 4. push persistence 如何避免重复

`CodingSession.__init__()` 会把 `_persist_on_message_end` 注册到 harness。写入时先在内存创建
`_PendingMessageWrite`，其中固定保存同一组 `MessageEntry.id` 和 `LeafEntry.id`：

1. 首次写入直接使用新 id，不必扫描整个文件；
2. 如果 message 已写、leaf 写失败，下一次重试先读取 durable ids；
3. 已存在的那一条跳过，只补缺的另一条；
4. 全部成功后才移除 pending 记录。

这就是“重投同一个包裹”，而不是重新生成一个包裹。`_reconcile_run_persistence()` 在 `finally`
中重试本轮结束但尚未持久化的消息；下一个 prompt、resume、branch 或 compact 前，
`_flush_pending_message_writes()` 会强制清空 pending。

## 5. 取消时 tool call 也要有结局

假设 assistant 发出 `read` tool call，用户按 Ctrl-C，TUI 随后不再消费事件：

1. `AgentHarness._run()` 的 `finally` 发现取消，给每个没有结果的 call 合成
   `ToolResultMessage("Tool call interrupted by user", is_error=True)`；
2. 它主动向 listener 发送对应的 `MessageStartEvent`/`MessageEndEvent`；
3. persistence listener 仍会收到 end 并写入 entry；
4. 下一次请求看到的是有结果的 tool call，不是 dangling call。

加载旧 transcript 或切换分支时，`_persist_active_tool_history_repairs()` 也会用一个 repair branch
补齐历史中的悬空调用；这是 session 事务，不是 renderer 的临时补丁。

## 6. “结束”有两个层次

session 把底层 `AgentEndEvent` 转成 `SessionAgentEndEvent`。如果还会自动 compact/retry，
`will_retry=True`，前端不要立即解锁全部 UI。所有清理和 pending persistence 完成后，session
才发 `AgentSettledEvent`；前端应把它当作“这次工作真正收敛”的信号。

context overflow 的典型事件顺序是：

```text
SessionAgentEnd(will_retry=True)
 -> CompactionStart(reason=overflow)
 -> CompactionEnd(will_retry=True)
 -> AutoRetryStart(attempt=1)
 -> harness.continue_()
 -> AutoRetryEnd
 -> AgentSettled
```

## 7. 用测试验证

```bash
uv run pytest tests/test_coding_session.py -q
uv run pytest tests/test_tool_history.py tests/test_agent_harness.py -q
```

优先阅读：`test_prompt_persists_user_assistant_and_leaf_entries`、`test_tool_results_are_persisted`、
`test_message_persistence_retry_is_idempotent`、`test_cancelled_prompt_teardown_persists_interrupted_tool_result`。

复述题：如果 UI 在 assistant 输出一半时消失，哪三个对象仍然把完整结果留下来？答案应包括
`AgentHarness` 的取消修复、`CodingSession` 的 persistence listener，以及 `finally` 中的 reconcile。
