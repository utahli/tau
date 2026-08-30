# 03：压缩、分支、替换与资源所有权

这篇讨论看似不同的操作：`compact()`、`branch_to_entry()`、`reload()`、`resume()`、
`new_session()`。它们共享一个设计：**先准备 candidate/entry，再在一个小的 publication
boundary 把它变成 live truth，之后只清理旧资源。**

## 1. compaction：替换 active context，不删除历史

`context_window.py` 提供 provider-neutral token 估算，也会尽可能锚定最近成功 assistant 的
provider-reported usage；之后新增 message/tool 的部分才使用近似估算。`CodingSession` 缓存
`ContextUsageEstimate`，任何消息、tool、system 或 replacement 变化都会失效缓存。

`compact()` 总结整个 active context；`compact_detailed()` 保留最近一段 token，使用
`_recent_preserving_compaction_plan()` 选 `replace_entry_ids`。两者都执行：

```text
生成 summary（独立、tools=[] 的 provider 请求）
  -> CompactionEntry(parent=current tip, replaces_entry_ids=...)
  -> LeafEntry(entry_id=CompactionEntry.id)
  -> replay active path
  -> harness.replace_messages(replayed messages)
```

replay `CompactionEntry` 时，`SessionState._apply_compaction()` 将首个被替代 row 放成
`"Previous conversation summary: ..."`，删去其余被替代 row。原 `MessageEntry` 仍在 JSONL，
故导出/审计/其他 branch 仍可见。自动压缩在 prompt 前后或 `continue_()` 后尝试；overflow
压缩成功后最多再 continue 一次。

## 2. branch：改变 Leaf，而不是剪掉未来

`tree_choices()` 读取 entries，按 parent 关系找可选节点；`branch_to_entry()` 仅在 idle 时
执行。它把目标 entry 作为新的活动端点（必要时生成 `BranchSummaryEntry`），追加 `LeafEntry`
并 replay 该路径。旧后代没有删除，因此可再次选择。

一个精确的思考练习：

```text
I -> U1 -> A1 -> U2 -> A2 -> Leaf(A2)
          \-> U3 -> A3 -> Leaf(A3)
```

当 leaf 指向 `A3`，`SessionState.from_entries(..., leaf_id=A3)` 不会包含 `U2/A2`，但磁盘中
仍有它们。若带 branch summary 回退，summary 作为一个明确 entry，避免模型悄悄丢失离开分支
的重要进度。

## 3. 模型切换也需要 durable 与 runtime 两个动作

`apply_startup_model_override()`、`set_thinking_level()`、`select_provider_model()` 等路径会
持久化相应的 `ModelChangeEntry`/`ThinkingLevelChangeEntry` 与 leaf，或显式更新 session index。
同时需要刷新 model-dependent provider、thinking capability、inference route 与 image support。

只更新 harness config 会造成重启后“历史说模型 A、实际曾用 B”；只追加 entry 又不替换 runtime
会造成当前请求仍打给旧客户端。学习者应在这些方法中寻找这两个动作是否成对出现。

## 4. replacement：`reload`、`resume`、`new_session`

三个操作都先 `_require_idle()` 并 `_flush_pending_message_writes()`，以免将一个不完整的写入
带进另一个 snapshot。

| 操作 | candidate 的来源 | publication 后要保留什么 |
| --- | --- | --- |
| `reload()` | 当前 cwd 的新 resources/runtime，重新 trust | 当前 transcript/harness；替换 tools、prompt、runtime。 |
| `resume(id)` | index record 的 cwd/path/provider 重新 `load()` | 目标 transcript 与 destination-bound resources。 |
| `new_session()` | manager 准备的新 record + 新 storage | 默认选择的 model/provider 与当前 UX 配置。 |

`reload()` 特别展示 transaction 写法：先构造 `staged_runtime`，先加载非项目扩展、resolve trust、
加载资源/项目扩展、组 tools/system；再对 staged runtime 执行可取消 lifecycle work。直到准备
完成才同步赋值替换 `_resource_paths`、resources、command registry、runtime、harness tools/system。
发布后关闭 old runtime 的取消/错误不能谎称“reload 未成功”。

`resume/new_session` 则调用 `CodingSession.load()` 产生 replacement，再走
`_adopt_replacement()`。该方法先提交 replacement 的 staged entries、转移 provider ownership，
再替换 config/state/harness/runtime，最后关闭旧 runtime/provider。失败发生在 adoption 前，
`_finish_aborted_session_close()` 只关闭候选，原 session 不变。

`session_preparation.py` 将这个模式公开为 `prepare_coding_session()`：它总设置
`defer_authoritative_writes=True`，返回 `PreparedCodingSession`。`adopt()` 先调用内部 commit，
成功后才交出 session；`abort()` 幂等关闭未发布 candidate。CLI、TUI、RPC 因而共享同一安全
边界。

## 5. close：所有权账本比“谁创建的”更重要

`CodingSessionConfig.owns_initial_provider` 表示 session 是否负责调用 initial provider 的
`aclose()`；并不等于调用者是否把对象作为参数传进来。session 把需要负责的对象放入
`_owned_providers`，`aclose()` 创建一个唯一 `_close_task`，多次调用等待同一任务。

关闭顺序是：extension `session_shutdown` → 清 UI contribution → runtime `aclose()` → 对每个
owned provider 尝试 `aclose()`。即使一个 close 失败，也会尝试余下资源；取消只在全部尝试后
重新向调用者传播。这避免取消使 token refresh/后台任务/网络 client 永久悬挂。

## 6. 用失败路径理解设计

| 失败时间 | 应留下的真实状态 |
| --- | --- |
| trust 被取消或 staged provider 创建失败 | 旧 live session 不变；candidate 被关闭；没有新的权威 entry。 |
| candidate 已 publish、清理 old runtime 时失败 | 新 session 已生效；错误被 containment，不回滚半个 snapshot。 |
| compaction summary 为空或 provider 报错 | 原 messages/leaf 保持，不能写伪 summary。 |
| message entry 已写、leaf 未写 | pending write 用同一 ids 补 leaf，不重复 message。 |

运行并阅读这些测试：

```bash
uv run pytest tests/test_coding_session.py -q
uv run pytest tests/test_project_trust.py tests/test_session_manager.py -q
```

特别关注测试名：`test_session_branches_to_previous_entry_without_destroying_history`、
`test_session_compact_persists_summary_and_rebuilds_context`、
`test_aborted_replacement_closes_only_candidate_provider_once`、
`test_failed_reload_preserves_complete_live_snapshot`。若能从每个测试名推导不变量，再去读断言，
你就不再是被动浏览大文件。
