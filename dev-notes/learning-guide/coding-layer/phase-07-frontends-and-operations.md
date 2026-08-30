# Coding 层 Phase 7：前端、导出、诊断与发布运维

本章要建立的直觉是：**同一个 session 事件流可以有不同展示，不同展示不能发明不同的
agent 语义。** CLI、print、RPC 和 TUI 都是 `CodingSession` 的消费者/调用者。

## 1. CLI 只是路由边界

`cli.py` 的 Typer `app` 是包的 console entry（见 `pyproject.toml`）。它解析参数、选择
session/provider/模式，并将控制交给：

```text
普通启动        -> run_openai_tui()   -> run_tui_app()
--print / --mode -> run_openai_print_mode() -> run_print_mode()
--mode rpc       -> run_openai_rpc_mode()   -> RpcServer.run()
export / update  -> 独立工具命令路径
```

`session_preparation.py` 的 `prepare_coding_session()` 是很重要的小 transaction：先产生一个
`PreparedCodingSession` candidate，只有外层调用 `adopt()` 后才成为权威 session。这样项目
trust 取消、provider 创建失败或前端启动失败不会覆盖当前 session/索引。

`session_manager.py` 是持久会话索引，不是 transcript parser：它创建互斥 id、列举/
查找 `CodingSessionRecord`、验证 id 和维护索引元数据。真正的 messages 仍由 `tau_agent`
JSONL storage replay；这避免一个“索引数据库”变成第二个历史权威。

## 2. 三种 print renderer：同一输入，三种意图

`rendering/base.py` 定义 `PrintOutputMode` 与 `EventRenderer` protocol；工厂在
`rendering/__init__.py`。

| renderer | 文件 | 给谁用 | 关键行为 |
| --- | --- | --- | --- |
| final text | `plain.py` | shell script / 人 | 只在完成时输出最终助手文本；失败写 stderr 并返回失败。 |
| JSON event | `json.py` | 机器消费者 | 一事件一行 JSON，保留流式事件。 |
| transcript | `transcript.py` | 调试/可读 CLI | Rich 流式显示文本、工具调用与结果。 |

这就是 `tau --mode text|json|transcript` 共享同一 agent 行为的原因。若要增加 renderer，
实现 protocol，而不要 fork `run_print_mode()`。

## 3. RPC：一个可替换的前端协议

`rpc.py` 的 `RpcSession` 是 session 的窄 protocol，`RpcServer` 从 stdin 读 Pi-compatible
JSONL command，向 stdout 写 JSONL response/event。它处理 prompt、command、cancel、模型、
tree、session 信息等，转换函数如 `_model_wire()`/`_entry_wire()` 刻意把 Python domain
object 投影为 wire JSON。

这意味着远程/自定义前端不需要 import Textual，也不需要知道 `AgentHarness` 内部结构。
阅读时把每个 RPC command 对应到一个 `RpcSession` method；若一个新功能无法从该窄接口表达，
先考虑扩展 coding event/protocol，而不是在 RPC 里摸私有字段。

## 4. TUI：adapter + state + widget 三层

`tui/app.py` 很大，但不应成为第一个读的文件。先按这个顺序：

1. `tui/state.py`：`TuiState` 是前端的可变投影，保存 chat items、流式 buffer、tool groups、
   thinking、running/queue 状态；它不调用模型。
2. `tui/adapter.py`：`TuiEventAdapter.apply(event)` 把 agent/coding event 翻译成 state 更新。
   这正是 UI 与 session 的 adapter boundary。
3. `tui/widgets.py`：从 state/session summary 渲染 transcript、sidebar、tool content 等纯
   视觉组件；长 transcript 的增量/虚拟化优化在这里而非 agent loop。
4. `tui/app.py:TauTuiApp`：Textual lifecycle、键位、modal picker、提交 prompt、消费事件、
   把 extension `UiBridge` 接到真实 UI。

辅助模块各自保持窄职责：`autocomplete.py` 仅构造 completion state；`file_drop.py` 只规范化
粘贴路径；`config.py` 读写 TUI settings/keybindings；`project_trust.py` 是 modal prompt；
`terminal_title.py`/`terminal_notification.py` 只生成和管理 terminal escape 序列；
`themes/__init__.py` 发现/校验主题。`tui/__init__.py` 是给 CLI 的小门面。

费曼检验：为何文本 delta 不应该从 `TauTuiApp` 直达 widget？因为 adapter/state 可被单测，
也可被另一个 UI 复用；widget 不必理解每一种 agent event。

## 5. 会话导出、统计和上下文是旁路消费者

| 模块 | 输入 | 输出 / 作用 |
| --- | --- | --- |
| `context_window.py` | system、tools、messages、model limit | `ContextUsageEstimate`，提示何时压缩。 |
| `session_stats.py` | entries 与 provider pricing | token、耗时、成本统计。 |
| `session_usage.py` | durable entries | usage event 序列和可嵌入 HTML dashboard。 |
| `session_export.py` | entries + theme | JSONL artifact 或包含树/usage/内容的 HTML。 |
| `branch_summary.py` | 要丢弃的分支消息 + provider | 创建保持语义的 branch summary。 |
| `reload.py` | reload 计数/diagnostic | UI/command 友好的 category summary。 |

这些模块从 durable history 或 session snapshot **读取**，不应悄悄修改 agent messages。这使
导出失败、统计错误不会毁掉一个活跃会话。

## 6. 可观测性与升级不进入主循环

- `events.py` 添加 Coding 层自己的事件（settled、queue、compaction、entry appended、retry），
  不污染 `tau_agent` 的通用事件协议；
- `diagnostics.py` 将 provider stream/error 的安全详情写为按 run id 划分的日志；
- `logging_config.py` 仅在 debug 环境配置 logging，避免 library import 修改全局日志；
- `update_check.py` 有缓存/禁用开关，读取 `data/release-notes/releases.json` 后在启动时给
  notice，而非阻塞启动等待网络；
- `updater.py` 识别安装方法、执行可解释的更新或 handoff；`version.py` 读取包版本。

原则：诊断应该帮助解释失败，但绝不能成为 agent 回合成功的前提。

## 7. 端到端演练

```bash
uv run pytest tests/test_rendering.py tests/test_rpc.py
uv run pytest tests/test_session_export.py tests/test_session_usage.py tests/test_session_stats.py
uv run pytest tests/test_tui_adapter.py tests/test_tui_app.py tests/test_tui_components.py
uv run pytest tests/test_update_check.py tests/test_updater.py tests/test_debug_logging.py
```

最后画出这条链并标注每一处失败向谁报告：

```text
TUI input -> CodingSession.prompt -> AgentHarness events
          -> TuiEventAdapter -> TuiState -> widgets
          -> MessageEndEvent listener -> JSONL storage
          -> diagnostic logger (only on relevant failure)
```

如果能说明为什么 `AgentSettledEvent` 比底层 `AgentEndEvent` 更适合 UI（session 可能还要
自动压缩、重试或 failover），就完成了本章。
