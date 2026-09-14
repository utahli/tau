# 04：费曼实验室——先猜，再看代码

每个实验都按四步进行：

1. 不看实现，先写下预测；
2. 用日常语言解释“为什么”；
3. 打开指定源码核对；
4. 运行测试，把预测改成准确结论。

这些练习不要求改源码。

## 实验 1：画出第一棵树

在纸上写：

```text
Info -> Model -> Think -> U1 -> Leaf(U1)
                      \
                       A1 -> Leaf(A1)
```

问题：

1. 当前 leaf 是 `Leaf(A1)` 时，`SessionState.messages` 包含哪条路径？
2. 追加 `U2 -> Leaf(U2)` 后，为什么不删除 `Leaf(U1)`？
3. 如果 `A1.parent_id` 指向外部不存在的 id，导入时的 `_detach_missing_parents()` 和
   `path_to_entry()` 分别负责什么？

参考结论：只 replay root-to-active-leaf；旧 leaf 是可回到的历史；缺失的外部根可在导入时脱钩，
但路径中间缺 entry、重复 id 或 cycle 仍应报错。

源码：`src/tau_agent/session/{memory,tree}.py`；测试：`tests/test_session.py`。

## 实验 2：追踪 `hi` 的两条写入

画出一条 user message 完成后的顺序：

```text
AgentHarness.prompt_message
  -> MessageEndEvent
  -> CodingSession._persist_on_message_end
  -> MessageEntry
  -> LeafEntry
  -> _refresh_persisted_state
```

故障注入：假设 `MessageEntry` 已成功，`LeafEntry` 写入失败。下一次重试为什么不会再添加一条相同
消息？

参考结论：`_PendingMessageWrite` 保留同一组 entry id；重试先读取 durable ids，只补缺的 entry。

测试：`test_message_persistence_retry_is_idempotent`、`test_prompt_persists_user_assistant_and_leaf_entries`。

## 实验 3：TUI 消失以后谁还在工作？

场景：assistant 发出 tool call，用户取消，前端停止迭代事件。

请用三句话回答：

1. 谁合成了 `Tool call interrupted by user`？
2. 为什么 persistence listener 仍能收到它？
3. 为什么下一轮不会带着悬空 tool call 请求 provider？

参考结论：`AgentHarness._run()` 的 `finally` 修复并主动通知 start/end；listener 独立订阅 harness，
不依赖前端继续消费；修复后的 `ToolResultMessage` 会成为可 replay 的历史。

测试：`test_cancelled_prompt_teardown_persists_interrupted_tool_result`、`tests/test_tool_history.py`。

## 实验 4：手算 compact replay

假设 active path 是 `M1, M2, M3`，随后追加：

```text
Compaction(summary=S, replaces_entry_ids=[M1, M2]) -> Leaf(compaction)
```

先写出模型下一次看到的 messages，再打开 `SessionState._apply_compaction()` 校对。

参考结论：结果是 `Previous conversation summary:\nS` 加上 `M3`；`M1/M2` 仍在 JSONL，便于审计、导出
或其他分支使用。

测试：`test_session_compact_persists_summary_and_rebuilds_context`、`tests/test_context_window.py`。

## 实验 5：比较两种替换顺序

```text
A: close(old); load(new); publish(new)
B: new = load(candidate); publish(new); close(old)
```

分别假设 trust 被取消、provider 创建失败、以及发布后 old runtime 关闭失败。写出用户能看到的状态。

参考结论：Tau 采用 B。发布前失败只关闭 candidate，旧 session 不变；发布后失败不能回滚已公开的新快照，
但清理错误会被隔离。`PreparedCodingSession.abort()` 和 `_finish_adopted_runtime_close()` 正是为这两类
资源问题服务的。

源码：`session.py` 的 `reload()`、`resume()`、`new_session()`、`_adopt_replacement()`，以及
`session_preparation.py`。

## 实验 6：新增“可恢复动作”前的检查表

假设要加入一个新命令，逐项写答案：

- 重启后还要存在吗？如果要，应该新增哪种 entry？
- 它会改变 active messages 吗？何时调用 `replace_messages()`、何时失效 token cache？
- 新 entry 的 parent 和 leaf 怎样连接？
- storage 部分失败时，重试复用什么稳定 id？
- 是否创建 provider/runtime/task？ownership ledger 由谁登记？
- 哪一步是 publication boundary？取消发生在哪里才不会破坏旧快照？
- 前端应该等待哪个 `CodingSessionEvent` 才算完成？

如果答案是“在 TUI 按钮回调里改一个变量”，通常说明逻辑放错了层。

## 结业口试

不看文件，连续回答：

1. `load()` 为什么不塞进 `__init__()`？
2. 为什么最后一行 JSONL 不一定是当前对话末端？
3. `MessageEndEvent` 比 `AgentEndEvent` 更适合作为持久化边界的两个原因？
4. compact 如何减少上下文却保留审计历史？
5. branch、resume、reload 分别替换什么？
6. `owns_initial_provider` 表示什么，为什么不能看“谁创建对象”？
7. 新前端为什么消费 `CodingSessionEvent`，而不是读取 `_harness` 私有字段？

答完后再跑：

```bash
uv run pytest tests/test_session.py tests/test_agent_harness.py tests/test_tool_history.py -q
```

能用自己的话解释每题，并指出一个对应测试，才算真正掌握，而不是记住术语。
