# Tau 会话模块：给新手的费曼学习指南

这组资料带你读懂 Tau 的 `CodingSession`。你不需要先读完几千行
`src/tau_coding/session.py`；我们从一个最小问题开始：

> 用户输入一句话后，Tau 怎样让 agent 工作、把结果保存下来，并在下次启动时恢复？

所有说明都以当前仓库的代码和测试为准。文中的函数名是“路标”，不是需要背诵的答案。

## 先补齐四个词

- **provider**：真正调用模型服务的对象，例如 OpenAI、Anthropic 或 fake provider。
- **message**：一次对话消息，可能是 user、assistant 或 tool result。
- **entry**：写入 session 存储的一行结构化记录。它不只是消息，也可以记录模型切换、压缩或分支。
- **harness**：可复用的 agent 大脑，负责模型循环、工具调用和内存中的 messages。

## 一张总图

```text
用户 / TUI / CLI
       │ 输入、取消、选择分支
       ▼
CodingSession（编码环境）
  ├─ 读取资源、trust、provider 和工具
  ├─ 调用 AgentHarness
  ├─ 把事件和已完成消息写入 SessionStorage
  └─ 给前端发送 CodingSessionEvent
       │
       ▼
AgentHarness（可复用的 agent 大脑）
  ├─ 内存中的 messages
  ├─ provider + model
  ├─ 工具循环
  └─ steering/follow-up 队列
       │
       ▼
SessionStorage（JSONL 或内存实现）
  └─ append-only entries，重启后 replay
```

最容易混淆的是两份“历史”：`harness.messages` 是下一次请求马上要使用的内存列表；JSONL
entries 是重启时相信的持久记录。`SessionState` 是把后者 replay 后得到的派生视图。

## 推荐阅读顺序

### 第 0 步：先运行测试

```bash
uv run pytest tests/test_session.py tests/test_coding_session.py -q
```

测试使用 fake provider 和临时存储，不需要 API key。看到测试名时，先猜它要保护的行为，再读实现。

### 第 1 步：状态与启动

阅读 [01-state-and-load.md](01-state-and-load.md)。你会学到配置、runtime、持久历史的区别，entry 树，
以及 `CodingSession.load()` 怎样从空文件或旧文件准备一个可运行会话。

### 第 2 步：一次回合与保存

阅读 [02-turns-and-persistence.md](02-turns-and-persistence.md)。沿着 `prompt()` 走一遍，弄清楚
事件从哪里产生、谁负责写盘、取消时为什么仍能留下完整的 tool result。

### 第 3 步：改变上下文和运行环境

阅读 [03-history-transformations.md](03-history-transformations.md)。这里解释压缩、分支、模型切换、
`reload()`、`resume()`、`new_session()` 以及资源关闭。

### 第 4 步：动手复述

阅读 [04-feynman-labs.md](04-feynman-labs.md)。每个实验都先让你预测，再给源码入口和检查方式。

## 六条必须守住的不变量

1. **历史只追加，不覆盖旧事实。** 新变化用新 entry 表达。
2. **active path 只有一条。** 当前实现默认从最后一个非 `leaf` entry 推导 tip；`LeafEntry` 主要作为旧格式兼容记录，显式 replay 时仍可传入 `leaf_id`。
3. **完整消息才是持久化边界。** stream 中间的 delta 不能当作 transcript。
4. **失败可以重试，但不能重复。** 待写消息会复用原 entry id。
5. **候选先准备，成功后发布。** 取消或加载失败不能破坏当前 live session。
6. **谁拥有资源，谁负责关闭，而且只关闭一次。** `owns_initial_provider` 表示 ownership，不表示“谁写了 `new`”。

读任何 helper 时都问一句：它在保护哪条不变量？如果答不出来，先回到本指南的主流程。

## 代码与测试地图

| 要回答的问题 | 主要代码 | 推荐测试 |
| --- | --- | --- |
| entry 如何保存和重放？ | `src/tau_agent/session/{entries,memory,tree,storage}.py` | `tests/test_session.py` |
| agent 如何发出事件？ | `src/tau_agent/harness.py`、`loop.py` | `tests/test_agent_harness.py` |
| session 如何装配和持久化？ | `src/tau_coding/session.py` | `tests/test_coding_session.py` |
| tool call 中断如何修复？ | `src/tau_agent/harness.py`、`tau_coding/session.py` | `tests/test_tool_history.py` |
| 替换失败如何保持旧会话？ | `session.py`、`session_preparation.py` | `tests/test_coding_session.py` 与 `tests/test_project_trust.py` 中 replacement/reload 测试 |
