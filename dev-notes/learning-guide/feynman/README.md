# Tau 会话模块：费曼学习计划

本目录是一套独立材料，只以当前源码和测试为依据，聚焦
`src/tau_coding/session.py` 及它直接协作的 session 模块。它不依赖同级任何已有学习资料。

目标不是背下 4,000 多行 `session.py`，而是能用自己的话向新人解释：

> 一个 `CodingSession` 怎样把临时的 agent loop 变成可恢复、可分支、可压缩、可替换且不会
> 泄漏 provider/extension 资源的编码会话？

## 先用一句话建立模型

把 `CodingSession` 想成机场塔台。

- `AgentHarness` 是已经会飞的飞机：它持有内存 messages，运行模型与工具循环。
- `SessionStorage` 是飞行记录仪：它只追加 JSONL entry，不直接知道 UI 或模型。
- `CodingSession` 是塔台：在正确时机写记录、指向当前航线、替换飞机/跑道、通知前端，并在
  飞机退役时关闭资源。

```text
前端 input / command
       │
       ▼
CodingSession
  ├─ 资源、trust、tools、provider、ExtensionRuntime 的装配
  ├─ AgentHarness 的运行和队列
  ├─ append-only entries 的持久化与 replay
  ├─ branch / compact / resume / reload / new session
  └─ CodingSessionEvent 给前端
       │
       ▼
tau_agent.AgentHarness ──> provider / tool loop
```

关键结论：**harness 的 messages 是“下一次请求要带什么”；持久 entries 才是“重启后相信
什么”。** Session 的责任就是让两者在受控边界重新对齐。

## 10 天学习计划

每次按费曼四步做：① 先读指定入口；② 合上源码用日常语言讲 3 分钟；③ 标出讲不清的
术语；④ 只回到相关函数和测试补洞。每天最后写一个 5 行“给新人”的小结。

| 天 | 核心问题 | 源码入口 | 费曼交付 |
| --- | --- | --- | --- |
| 1 | session 与 harness 各自拥有哪种状态？ | `session.py` 的 `CodingSessionConfig`、`__init__`；`tau_agent/harness.py` | 画出“配置、live runtime、durable history”三层。 |
| 2 | 一次 `load()` 如何成为完整候选会话？ | `CodingSession.load()` | 不看源码按顺序说出读历史、trust、resources、provider、harness 的步骤。 |
| 3 | JSONL 为什么是树，不是一条消息数组？ | `tau_agent/session/{entries,memory,tree,storage}.py` | 画出 `LeafEntry` 如何选择一条 root-to-leaf 路径。 |
| 4 | 一条 prompt 的事件从哪来、往哪去？ | `CodingSession.prompt()`、`AgentHarness._run()` | 讲清 input hook、expand、queue、agent event、settled。 |
| 5 | 为什么在 `MessageEndEvent` 持久化？ | `_attach_persistence_listener()` 到 `_reconcile_run_persistence()` | 描述消息 entry + leaf 两条写入的失败重试。 |
| 6 | 取消、队列和中断工具结果怎样仍可恢复？ | `cancel()`、queue 方法、harness repair | 写出“前端停止消费事件”时仍写盘的原因。 |
| 7 | compact 如何减上下文却不删除历史？ | `context_window.py`、`compact*()`、`_append_compaction()` | 用三条消息模拟 replay 后被摘要替换。 |
| 8 | branch 如何回退又保留未来？ | `tree_choices()`、`branch_to_entry()`、`branch_summary.py` | 画出回到旧节点后新 leaf 与旧分支共存。 |
| 9 | reload/resume/new 怎样“先准备，后发布”？ | `reload()`、`resume()`、`new_session()`、`_adopt_replacement()`、`session_preparation.py` | 用“换发动机不停机”的比喻解释 candidate-first。 |
| 10 | 怎么审查与修改 session 代码？ | `tests/test_coding_session.py` | 选一条不变量，为它定位最窄测试和事件边界。 |

建议测试顺序：

```bash
uv run pytest tests/test_session.py tests/test_session_manager.py -q
uv run pytest tests/test_coding_session.py -q
uv run pytest tests/test_context_window.py tests/test_tool_history.py -q
```

测试使用 fake provider、临时目录与内存 storage；学习会话机制时不需要真实 API key。

## 阅读导航

| 文档 | 解决的问题 |
| --- | --- |
| [01-state-and-load.md](01-state-and-load.md) | 三种状态、entry tree、`load()` 的候选装配。 |
| [02-turns-and-persistence.md](02-turns-and-persistence.md) | prompt/continue、事件、队列、push persistence 与错误收敛。 |
| [03-history-transformations.md](03-history-transformations.md) | compact、branch、模型切换、会话替换和生命周期。 |
| [04-feynman-labs.md](04-feynman-labs.md) | 复述卡、纸上演算、测试导读和设计审查题。 |

## 必须守住的六条不变量

1. **append-only**：会话的历史不覆盖写；新事实以新 entry 表达。
2. **活动路径唯一**：`LeafEntry.entry_id` 指向当前 root-to-leaf 路径的端点。
3. **消息完整才持久化**：stream delta 不是 durable transcript；`MessageEndEvent` 才是边界。
4. **失败可重试但不重复**：待写消息保留稳定 entry id，补写时先核对磁盘。
5. **替换原子可见**：可取消工作在 publication 前；新 snapshot 一旦公开，旧资源只做清理。
6. **资源恰好关闭一次**：session 显式登记自己拥有的 provider/runtime，关闭任务幂等。

学习全程都可以反问一句：这段代码在维护上面的哪条不变量？若答不出来，先不要继续读
helper 的细节。
