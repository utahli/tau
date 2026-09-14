# 03：压缩、分支和“换环境”

前两章讲的是“读”和“写”。本章讲会话发生变化时怎样保持安全。四种操作看起来不同，但都遵循同一套路：

```text
准备 candidate 或新 entry
  -> 在一个清晰的 publication boundary 发布
  -> 发布后再清理旧资源
```

这样，取消或 provider 失败时，旧的 live session 仍然完整。

## 1. compact：缩短上下文，不删历史

模型上下文有长度限制。`context_window.py` 估算 token 数；`CodingSession` 缓存这个估算，消息、工具、
system prompt 或替换发生后会失效缓存。

手动 `compact()`（或保留最近 token 的 `compact_detailed()`）会：

1. 用同一个 provider 发起一次独立的摘要请求（`tools=[]`）；
2. 写入 `CompactionEntry(summary, replaces_entry_ids=...)`；
3. 再写指向它的 `LeafEntry`；
4. replay active path，并用 `harness.replace_messages()` 替换内存上下文。

replay 时，`SessionState._apply_compaction()` 将第一条被替代消息的位置换成：

```text
Previous conversation summary:
<summary>
```

未被替代的较新消息继续保留。旧 `MessageEntry` 仍在 JSONL，所以导出、审计和其他分支都能看到完整历史；
只是下一次模型请求携带更短的 messages。

## 2. branch：换路线，不抹掉未来

`tree_choices()` 从 entry 树生成可选节点；`branch_to_entry()` 要求 harness 空闲，然后追加新的
`LeafEntry`，把目标节点设为 active。旧后代不删除，稍后仍可再次选择。

```text
I -> U1 -> A1 -> U2 -> A2 -> Leaf(A2)
          \
           U3 -> A3 -> Leaf(A3)   # 当前 leaf
```

如果选中一个 user message，代码会回到它的 parent，并返回 `input_prefill`，方便前端把原问题放回输入框。
如果 `summarize=True`，则先生成 `BranchSummaryEntry`，把离开分支时的重要进度作为一条受控的 user message
带回新路线，而不是让模型“猜”丢失了什么。

## 3. 模型切换必须同时改两处

`set_model()` 只改变未来请求的 runtime，并更新默认选择；`apply_startup_model_override()` 和
`select_provider_model()` 还会持久化 `ModelChangeEntry + LeafEntry`。切换 provider/model 时，代码还会
同步 thinking level、inference route、图片能力和 runtime provider。

只改 harness：重启后 history 仍说旧模型。只写 entry：当前请求仍可能打给旧 provider。阅读这些方法时，
检查“durable 记录”和“live runtime”是否成对更新。

## 4. reload、resume、new_session 的共同边界

三者都先要求 idle，并先 flush pending message writes。

| 操作 | candidate 从哪里来 | 发布后保留什么 |
| --- | --- | --- |
| `reload()` | 当前 cwd 重新加载资源、trust 和 extension runtime | 原 transcript/harness，替换 tools、system prompt、资源和 runtime |
| `resume(id)` | session manager 的记录，按目标 cwd/path 再次 `load()` | 目标 transcript 与目标环境 |
| `new_session()` | manager 准备的新记录和新 storage | 默认 provider/model 与当前 UX 配置 |

`reload()` 的关键顺序是：创建 staged runtime → 只加载允许的资源 → resolve trust → 构造 tools/system →
执行可取消的 lifecycle work → 同步发布字段 → 关闭 old runtime。发布前取消，旧快照不变；发布后关闭旧资源
失败，只记录 containment 错误，不假装 reload 没发生。

`resume()` 和 `new_session()` 由 `CodingSession.load()` 生成 replacement，再交给 `_adopt_replacement()`：
先提交 candidate 的 staged entries、转移 provider ownership，再替换 config/state/harness/runtime，最后
关闭旧对象。`session_preparation.py` 把同样的边界提供给 CLI、TUI 和 RPC：

- `prepare_coding_session()` 总是设置 `defer_authoritative_writes=True`；
- `PreparedCodingSession.adopt()` 才提交 staged entries；
- `abort()` 幂等关闭尚未发布的 candidate。

## 5. 谁负责关闭 provider？

`CodingSessionConfig.owns_initial_provider` 表示 session 是否拥有关闭责任，而不是谁在调用者代码里创建了
这个对象。load 成功后，拥有的 provider 放进 `_owned_providers`。

`aclose()` 创建唯一 `_close_task`，多次调用都等待同一任务。关闭顺序是：extension `session_shutdown` →
清 UI contribution → runtime `aclose()` → 逐个关闭 owned provider。某个资源关闭失败，也会继续尝试其他资源；
取消只在全部尝试后再传播，避免后台任务或网络 client 悬挂。

## 6. 四个故障场景

| 失败点 | 稳定状态 |
| --- | --- |
| trust 被取消、provider 创建失败 | 旧 session 不变；candidate 被关闭；不产生新的权威 entry |
| candidate 已发布，旧 runtime 清理失败 | 新 session 已生效；清理错误被隔离 |
| 摘要为空或 provider 报错 | 原 messages 和 leaf 保留，不写伪 summary |
| message 已写、leaf 未写 | pending write 用相同 id 补 leaf，不重复 message |

推荐测试：`test_session_compact_persists_summary_and_rebuilds_context`、
`test_session_branches_to_previous_entry_without_destroying_history`、
`test_failed_reload_preserves_complete_live_snapshot`、
`test_aborted_replacement_closes_only_candidate_provider_once`。
