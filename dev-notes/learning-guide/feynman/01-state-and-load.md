# 01：先弄清楚“会话状态”是什么

本章回答两个入门问题：`CodingSession` 里面到底保存了什么？程序重启后又从哪里恢复？

## 1. 三份东西，不要混成一份

把一次编码会话想成一张工作台：

| 日常比喻 | 代码 | 作用 |
| --- | --- | --- |
| 操作说明 | `CodingSessionConfig` | cwd、存储、provider、模型、是否启用 skills 等“应该怎样运行”的输入。它是 frozen dataclass，修改时用 `dataclasses.replace()`。 |
| 工作台上的物品 | `_harness`、`_extension_runtime`、provider、缓存和队列 | 当前进程马上要用的对象。它们在内存中，结束时要关闭。 |
| 工作日志 | `SessionEntry` + `SessionState` | 写入 JSONL 的事实。重启时从这些 entry 重放出状态。 |

一个简单判断法：问“进程崩溃后还要不要知道它？”要知道的内容必须进 history；只服务于当前运行
的对象属于 runtime。

`CodingSession.__init__()` 只接收已经准备好的 state、harness 和资源，它不会偷偷读磁盘、弹 trust
询问或创建 provider。这样做是为了让对象一旦交给前端，就已经可用。异步工厂
`CodingSession.load()` 负责剩下的启动工作。

## 2. entry 为什么像树？

`src/tau_agent/session/entries.py` 定义了多种 entry。每条都有唯一 `id`、`parent_id` 和时间戳。
消息只是其中一种；模型切换、压缩、标签、extension 自定义数据也各有 entry 类型。

`LeafEntry` 是“当前选中的末端”，不是删除标记。假设先聊出 A、B，后来回到 A 继续聊 C：

```text
Info -> Model -> Think -> A -> B -> Leaf(B)
                              \
                               C -> Leaf(C)   # 当前活动分支
```

磁盘仍保留 B；最后一个 `LeafEntry` 告诉 Tau 当前应该沿哪条路线读取。

## 3. replay：从树还原成模型看到的消息

`SessionState.from_entries(entries, leaf_id=...)` 的工作可以拆成四步：

1. `path_to_entry()` 从 leaf 沿 `parent_id` 向上找根，再反转成 root-to-leaf 顺序；
2. 检查重复 id、缺少 parent 和 cycle，坏数据直接报 `SessionTreeError`；
3. 按顺序处理 entry：消息进入列表，模型/思考级别更新为最新值；
4. `CompactionEntry` 把指定旧消息替换成摘要，`BranchSummaryEntry` 变成一条带固定前缀的 user message。

因此 `SessionState.messages` 不是 JSONL 原样切片，而是“当前 active path 的派生结果”。这就是为什么
文件里有分支，但 harness 只接收一条线性的 messages 序列。

## 4. `load()` 的启动顺序

打开 `src/tau_coding/session.py` 的 `CodingSession.load()`，按下面顺序对照代码：

```text
读取 storage
  ├─ 空文件：先在内存准备 Info -> Model -> Thinking
  └─ 非空：整理缺失的外部 parent，找到最新 Leaf 并 replay
创建只加载“允许范围”的 extension runtime
解析项目 trust，再决定是否加载项目目录的 extension
加载 skills、prompt templates、AGENTS/context 文件
选择或创建 provider，确定 active model 和图片能力
创建 coding tools，组合 extension tools
生成 system prompt
用 state.messages 创建 AgentHarness
绑定 persistence listener；把 session_start 留到前端准备好以后再发
```

### 空 session 为什么先不写盘？

空文件会先生成三条初始 entry，但放在 `pending_initial_entries` 中。第一次权威写入（通常是第一条
完成的消息）时才提交；如果用户只是打开后退出，就不会留下没有内容的 transcript。
配置
`defer_authoritative_writes=True` 时，这些 entry 进入 `_prepared_entries`，等待
`PreparedCodingSession.adopt()` 统一提交。

### 为什么 trust 在加载项目 extension 之前？

extension 可以执行 Python。`load()` 先创建 `include_project_dir=False` 的 runtime，解析 trust 后，
只有在“可信且配置允许”时才加载项目目录。用户拒绝 trust 时，代码在 import 之前就结束；skills、
context 文件和默认 coding tools 的过滤则由资源/trust 规则分别决定，不等于整个 agent 消失。

### 为什么 `session_start` 延迟？

extension 的启动处理器可能要弹通知或对话框。`load()` 只设置 `_session_start_pending=True`；前端装好
UI bridge 后调用 `emit_pending_session_start()`，随后才提交 staged trust decision。这样尚未被采用的
candidate 不会污染 trust cache。

## 5. 什么时候 state 和 harness 对齐？

普通新消息由 harness 追加到内存；persistence listener 写完 entry 后刷新 `_state`。需要改变上下文
形状的操作（分支、压缩、tool-history repair）会先刷新 state，再调用
`harness.replace_messages(_state.messages)`。

想想这个问题：`append_custom_entry()` 为什么既写 `CustomEntry` 又写 `LeafEntry`？因为只有成为
root-to-leaf 路径的一部分，resume 时 replay 才能看到它。

## 6. 纸上练习

假设存储为空，cwd 是 `P`，模型最终选为 `m`：

1. 画出内存中的三条初始 entry 及 parent 关系；
2. 说明为什么此刻 JSONL 仍可能是空的；
3. 追加一条 user message 后，画出 `MessageEntry + LeafEntry`；
4. 再创建第二条分支，指出 harness 为什么只看到最新 leaf 路径。

核对入口：`tests/test_coding_session.py` 中的 `test_load_restores_existing_transcript`、
`test_load_restores_active_leaf_branch`，以及 `tests/test_session.py` 的 tree/replay 测试。
