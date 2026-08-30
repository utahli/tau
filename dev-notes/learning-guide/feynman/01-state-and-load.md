# 01：状态、历史树与 `CodingSession.load()`

## 1. 不要把 session 误读成“消息列表”

`CodingSession` 同时协调三个状态源，任何一个都不能替代另外两个。

| 状态 | 主要对象 | 权威问题 | 生命周期 |
| --- | --- | --- | --- |
| 输入蓝图 | `CodingSessionConfig` | “应使用什么 cwd、storage、provider 选择与策略？” | frozen dataclass；可用 `replace()` 产生新快照。 |
| live runtime | `_harness`、`_extension_runtime`、`_owned_providers`、缓存/队列 | “下一次请求现在会怎样执行？” | 内存、可替换、必须关闭。 |
| durable history | `SessionEntry` JSONL 与 `SessionState` | “重启后应该恢复成什么？” | append-only；通过 replay 投影。 |

给新人解释时可以说：蓝图是登机牌，runtime 是现在飞行中的飞机，history 是不可涂改的
飞行记录。把飞机的座位表当成记录仪，会在崩溃后丢失；把 JSONL 当作实时 UI 状态，又会让
每个字符流式落盘并无法正确回退。

`CodingSession.__init__()` 只接受已经构造好的 state/harness/resources，建立 ownership
ledger、缓存、diagnostic logger 与 persistence listener。它没有磁盘读取或 trust prompt。
这正是异步工厂 `load()` 存在的理由：对象只有准备完成才应该被前端看见。

## 2. durable history 是 parent-pointer tree

入口：`src/tau_agent/session/entries.py`、`memory.py`、`tree.py`、`storage.py`。

每个 entry 有唯一 `id`、`parent_id` 与 timestamp。最重要的类型是：

| entry | 给 replay 的意义 |
| --- | --- |
| `SessionInfoEntry` | cwd/创建时间等 session metadata。 |
| `ModelChangeEntry` / `ThinkingLevelChangeEntry` | 在这条路径上当前模型、provider 与 thinking level。 |
| `MessageEntry` | 一条完整 user/assistant/tool/custom message。 |
| `LeafEntry` | 指向当前活跃路径的真实端点，自己也是追加记录。 |
| `CompactionEntry` | 指定被摘要替代的 message entry ids。 |
| `BranchSummaryEntry` | 将被离开的分支的信息以上下文消息带回新路径。 |
| `LabelEntry` / `CustomEntry` | 人类标签和 extension-owned 状态。 |

例如先得到 A、B，再回到 A 继续得到 C：

```text
root -> A -> B -> Leaf(B)       # 第一次的未来仍保留
         \-> C -> Leaf(C)       # 新 Leaf 使 C 分支成为 active
```

`SessionState.from_entries(entries, leaf_id=...)` 并不是“拿最后一行”。它先由
`path_to_entry()` 从 leaf 反向找 parent，检测重复 id、缺 parent、cycle 后再正向 replay。
在 replay 中 `CompactionEntry` 用摘要替代指定 rows；`BranchSummaryEntry` 变成一条受控的
`UserMessage`。所以 `SessionState.messages` 是派生视图，而不是 JSONL 的原样拷贝。

`JsonlSessionStorage.append_batch()` 是 storage transaction boundary：完整 batch 可见或保留
旧文件。它在同目录临时文件写入、fsync、`replace`、directory fsync，并使用每个 transcript
的跨进程锁。这一点解释为什么 session 在写 `entry + leaf` 这种必须成对可见的操作时偏好
batch。

## 3. `load()`：从冷记录到候选 runtime

按下列顺序读 `CodingSession.load()`；每一步都有意放在下一步之前。

```text
1. storage.read_all()
2. 空历史：在内存准备 SessionInfo -> ModelChange -> ThinkingChange
3. 非空历史：detach 外来 root 的 missing parent；找 latest Leaf；replay active path
4. 建立“只含 eligible extension”的新 ExtensionRuntime
5. ProjectTrustCoordinator.resolve(cwd)，再 filter resource paths
6. 若 trusted 且显式开启：才加载 project extension
7. 选择/创建 provider，计算 active model、image support、tools、system prompt
8. 建 AgentHarness(messages=state.messages)，再建 CodingSession
9. 登记 provider ownership；repair tool history；绑定 extension；延迟 session_start
```

### 3.1 为什么空 session 不立即写三条初始 entry？

新 session 的 `SessionInfoEntry → ModelChangeEntry → ThinkingLevelChangeEntry` 先保存在
`pending_initial_entries`，之后由 `_ensure_session_initialized()` 在第一次权威写入时提交。
这避免“仅打开后立刻退出”制造空 transcript；同时 entry id 已经稳定，可在延迟写入时去重。
当 `defer_authoritative_writes=True` 时，它们进入 `_prepared_entries`，由外部 preparation
流程统一提交（见第 03 篇）。

### 3.2 为什么先建 eligible runtime、再做 project trust？

extension 可以执行 Python。`load()` 创建一个 **新的 cwd-bound runtime**，先调用
`extension_runtime.load(... include_project_dir=False)`；trust 解析完成并且配置允许后，才加载
project directory。resume/reload/new session 也不复用 source 项目的 project registration。
这使“用户取消 trust”在 import 前结束，而不是加载后才显示 warning。

### 3.3 provider、tools、system 的装配顺序

若调用方没有给 `config.provider`，`_prepare_provider_selection()` 在 provider registry 已经
准备好后选择静态或动态 provider。之后才根据 active model 产生 `ImageSupportState`；默认
tools 依赖它来决定 read 图片的行为。extension 组合 tools 后，`build_system_prompt()` 才能
准确列出最终工具、skills、context 与 extension sections。最终 harness 只收到 provider、model、
system、tools、messages 这几个 portable 值。

### 3.4 session_start 为什么延迟？

`load()` 末尾只设 `_session_start_pending=True`。宿主在装好 UI bridge 后调用
`emit_pending_session_start()`，此时 extension handler 才可以安全弹窗/通知；随后才提交 staged
trust decision。这也让一个未被采用的 candidate 不会污染 trust cache。

## 4. `SessionState` 与 harness 何时同步？

`_refresh_persisted_state(leaf_id=...)` 读取 storage 并 replay active path，更新 `_state`；
需要重写下一轮上下文的操作（compaction、branch、repair）接着调用
`harness.replace_messages(_state.messages)`。普通新消息由 harness 自己 append，persistence
listener 只刷新 `_state`；二者在下一操作点继续对齐。

用这个问题检查理解：为什么 `append_custom_entry()` 写 entry 后也写 leaf 并 refresh？因为
custom entry 若没有成为 active root-to-leaf 路径的一部分，resume 时 `SessionState` 看不到它。

## 5. 第一天的纸上演算

假设空 storage、模型 `m`、working directory `P`：

1. 写出 `load()` 内存中先出现的三条 entry，标明 parent。
2. 说明 storage 此刻仍可能为空。
3. 假设 P 不可信：skills、AGENTS、project extensions 哪些会被过滤？默认 coding tools 是否
   因此消失？
4. 假设 first prompt 完成：哪一个动作会迫使初始三条 entry 先落盘？

用 `tests/test_coding_session.py` 中 `test_load_restores_existing_transcript`、
`test_load_restores_active_leaf_branch`、`test_load_detaches_missing_root_parent_from_imported_branch`
来核对答案。完成标准是能解释“为什么 history 有分支但 harness 只看一条 messages 序列”。
