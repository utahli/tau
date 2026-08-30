# 04：会话模块费曼实验室

先口述再验证。以下练习均不要求改源码；用现有 fake provider 与测试即可。每题的完成标准是
能向不会 Python 的人解释因果，而不是复述函数名。

## 实验 1：手算一个 session tree

在纸上创建以下 entry（用 `X -> Y` 表示 `Y.parent_id == X.id`）：

```text
Info -> Model -> Think -> U1 -> Leaf(U1)
                      \-> A1 -> Leaf(A1)
```

回答：

1. 最后一个 leaf 是 `Leaf(A1)` 时，`SessionState.messages` 是什么？
2. 若追加 `U2 -> Leaf(U2)`，早期 `Leaf(U1)` 是否需要删除？为什么？
3. 如果 `A1.parent_id` 指向不存在的外部 entry，`_detach_missing_parents()` 解决的是哪种导入
   场景，`path_to_entry()` 又仍会拒绝什么坏数据？

验证入口：`tests/test_session.py`、`tests/test_coding_session.py` 的 active leaf / imported
branch tests。目标是理解 leaf 是“选择器”，不是垃圾回收标记。

## 实验 2：模拟一次完整持久化

写出 `UserMessage("hi")` 从 prompt 到 JSONL 的顺序，至少包含：

```text
AgentHarness.prompt_message
MessageEndEvent
_persist_on_message_end
MessageEntry + LeafEntry
_refresh_persisted_state
```

然后插入故障：“`MessageEntry` 已成功，但写 `LeafEntry` 时 storage 临时失败”。解释下一次
`_persist_message()` 为什么不创建第二条同内容 message。提示：检查 `_PendingMessageWrite`
和 retry 时读取的 `durable_ids`。

运行：

```bash
uv run pytest tests/test_coding_session.py -q
```

完成标准：能解释“event 的可见性”和“entry 的原子性”不是同一层的保证。

## 实验 3：为什么 UI 不能承担持久化？

假设用户在 assistant 发出 tool call 后关闭 TUI 的 async event consumer。请用三句话解释：

1. agent loop 最终如何合成 interrupted tool result；
2. persistence listener 为何仍会收到它；
3. 为什么下一次请求不会携带 dangling call。

源码锚点：`tau_agent/harness.py:_run()`、`CodingSession._attach_persistence_listener()`、
`CodingSession._reconcile_run_persistence()`。

运行：

```bash
uv run pytest tests/test_agent_harness.py tests/test_tool_history.py -q
```

## 实验 4：模拟 compaction replay

假设 active path 有 message entries `M1, M2, M3`，随后写：

```text
CompactionEntry(summary=S, replaces_entry_ids=[M1, M2]) -> Leaf(compaction)
```

不看源码写出 replay 后的 messages。再打开 `SessionState._apply_compaction()` 校正：应有一个
带固定前缀的 summary message 和原 `M3`，而不是删除整个 transcript。接着解释
`first_kept_entry_id` 对调试/导出的价值。

运行：

```bash
uv run pytest tests/test_context_window.py tests/test_coding_session.py -q
```

## 实验 5：candidate-first 的故障注入思考

比较两个伪代码：

```text
A: close(old); load(new); self.runtime = new
B: new = load(candidate); publish(self, new); close(old)
```

若 `load(new)` 因 trust 取消或 provider 创建失败，A 与 B 的用户可见状态各是什么？Tau 选择 B，
但还做了两件事：对未发布 candidate 调 `aclose()`；发布后把 cleanup 的取消/异常 containment
起来。说明这两件事分别修补了 B 的哪种资源问题。

源码锚点：`session.py:reload`、`resume`、`new_session`、`_adopt_replacement`、
`session_preparation.py:PreparedCodingSession`。

## 实验 6：改动前的审查清单

若你准备往 `CodingSession` 新增一个“可恢复的会话动作”，先逐项回答：

- 它是临时 UI 反馈，还是必须跨重启存在？后者需要什么 entry？
- 它是否改变 active context？若是，何时 `replace_messages()` 和 invalidate cache？
- 它与 `last_parent_id`、`LeafEntry` 的关系是什么？
- 若 storage 部分失败，稳定 id 与重试方式是什么？
- 它是否创建 provider/runtime/task？谁把它加入 ownership ledger？
- 可取消工作在哪里结束，publication boundary 在哪里？
- 前端应等待哪一种 `CodingSessionEvent` 才认为操作完成？

如果回答只是“在 TUI 按钮回调里改个变量”，说明能力放错了层。

## 结业口试

不看任何文件，连续回答以下问题：

1. `CodingSession.load()` 为什么要在 `__init__()` 外？
2. 为什么最后一条 JSONL entry 不一定等于当前 active conversation 的末端？
3. `MessageEndEvent` 比 `AgentEndEvent` 更适合作为消息持久化边界的两个原因？
4. compaction 如何降低下一轮上下文，却不损失审计历史？
5. branch、resume、reload 三者各自替换什么？
6. `owns_initial_provider` 的语义为什么不能从“谁创建对象”推断？
7. 一个新前端为何应消费 `CodingSessionEvent`，而不读取 `_harness` 私有字段？

能稳定回答这七题，就已经拥有阅读 `session.py` 大部分演进提交和设计新能力所需的心智模型。
