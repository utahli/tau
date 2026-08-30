# `tau_coding` 费曼学习路线

> 目标：不是记住 46K 行实现，而是能在白板上解释：**Tau 如何把可复用的
> `tau_agent` 大脑，接到一个可信项目、真实工具、可恢复会话和多个前端上。**
>
> 前置阅读：`01-AI层手册-tau_ai.md`、`02-Agent层手册-tau_agent.md`。本目录在
> 当前源码快照（`src/tau_coding/`）上整理；实现演进时，应以测试和公开 API 为准。

## 先背下来的模型

把 Coding 层看成机场地勤，而不是飞行员：`tau_agent.AgentHarness` 决定下一步
“飞向哪里”（模型、消息、工具调用循环）；`tau_coding.CodingSession` 负责把飞机接到
正确跑道（cwd、工具、资源、持久化、provider 和 UI）。

```text
用户 / 自动化客户端
       │
 CLI ──┼── print renderer / RPC / Textual TUI
       │                 只消费 CodingSessionEvent
       ▼
CodingSession ── tools, prompt, trust, extensions, JSONL session
       ▼
AgentHarness / run_agent_loop              (tau_agent)
       ▼
ModelProvider                              (tau_ai 或扩展 provider)
```

三个必须保持的边界：

1. `tau_agent` 不知道 Textual、`~/.tau`、项目资源路径或编码工具实现。
2. TUI 不直接修改 harness 消息或读写 JSONL；它调用 session 公共方法并消费事件。
3. 不可信项目的指令、skills、主题、扩展不能在 trust 决定之前进入运行时。

## 按依赖排序的 14 天计划

每天采用同一个费曼循环：先只读本日目标文件，再合上源码用自己的话讲 3 分钟；
讲不清的名词回到代码补洞；最后做一个小实验并运行关联测试。每次笔记只回答
“它接收什么、保证什么、失败时怎样、谁依赖它”。

| 天 | 阶段 | 必读模块 | 当日交付物（费曼检验） |
| --- | --- | --- | --- |
| 1 | 总图 | `cli.py`、`session.py` 的 imports 和公开方法 | 画出一次 `tau -p` 的十个步骤。 |
| 2 | 文件归属 | `paths.py`、`resources.py`、`context.py` | 向新人解释为何 cwd 不是普通字符串。 |
| 3 | 项目信任 | `project_trust.py`、`shell_config.py` | 写出“未信任项目仍能做什么”的表。 |
| 4 | 可复用输入 | `skills.py`、`prompt_templates.py`、`system_prompt.py` | 手工展开一条 `/skill:name` 和一条模板。 |
| 5 | 真实工具 | `tools.py`、`image_processing.py` | 解释 edit 为什么要求 `oldText` 唯一。 |
| 6 | 模型目录 | `provider_catalog.py`、`catalog_loader.py`、`models_dev*.py` | 从 catalog 到一个可选 model 画数据流。 |
| 7 | 配置与凭据 | `provider_config.py`、`credentials.py`、`thinking.py` | 解释定义、偏好、秘密为何分开保存。 |
| 8 | 运行时认证 | `provider_runtime.py`、`oauth*.py` | 讲清 token 刷新为何发生在请求边界。 |
| 9 | 扩展协议 | `extensions/api.py`、`loader.py`、`providers.py` | 写一个只注册工具的最小扩展草图。 |
| 10 | 扩展运行时 | `extensions/runtime.py`、`provider_registry.py`、`local_backends.py` | 说明 overlay/layer 为何比全局注册表安全。 |
| 11 | 会话装配 | `session.py:load`、`session_preparation.py` | 解释“先准备、后采纳”避免了什么半成品。 |
| 12 | 会话运行 | `session.py:prompt`、持久化/压缩/分支方法、`commands.py` | 给同伴讲事件何时写进 JSONL。 |
| 13 | 非交互前端 | `rendering/`、`rpc.py`、`session_export.py` | 比较 text、JSONL RPC、HTML export 的消费者契约。 |
| 14 | TUI 与运营 | `tui/`、诊断、更新、本地 llama.cpp | 做一次端到端演练，列出故障观测点。 |

每天先运行最窄的测试，例如：

```bash
uv run pytest tests/test_resources.py
uv run pytest tests/test_coding_tools.py
uv run pytest tests/test_coding_session.py
```

不要在学习时请求真实 provider；`tests/` 中的 fake provider 和内存 storage 已经能让
循环完全确定性地运行。

## 章节与依赖

| 章节 | 问题 | 依赖 | 配套测试 |
| --- | --- | --- | --- |
| [01 CLI](phase-01-cli.md) | 参数如何选择前端和会话？ | 后续所有装配模块 | `test_cli.py` |
| [02 资源和工具](phase-02-resources-and-tools.md) | 模型为何能看见项目并安全操作文件？ | 路径、信任 | `test_resources.py`、`test_coding_tools.py` |
| [03 系统提示](phase-03-system-prompt.md) | 多个指令如何成一个确定的 prompt？ | skills、工具 | `test_system_prompt.py` |
| [04 会话](phase-04-session-core.md) + `phase-04-coding-session-methods.md` | session 怎样装配、运行、恢复？ | 前面所有核心模块 | `test_coding_session.py` |
| [05 provider 与身份](phase-05-provider-and-identity.md) | 配置如何变成可关闭的模型客户端？ | catalog、凭据 | `test_provider_*.py`、`test_oauth*.py` |
| [06 扩展和本地推理](phase-06-extensions-and-local-inference.md) | 第三方能力怎样扩展而不破坏边界？ | trust、provider | `test_extensions.py`、`test_local_backends.py` |
| [07 前端与运营](phase-07-frontends-and-operations.md) | 同一事件流怎样服务四种输出？ | CodingSession | `test_rendering.py`、`test_rpc.py`、`test_tui_*.py` |
| [08 复述与实战](phase-08-feynman-labs.md) | 怎样证明自己真正理解？ | 全部 | 按实验逐项运行 |

## 模块覆盖索引

下表是“逐个精读”的核对表。`__init__.py` 只作为 re-export 门面阅读：检查它公开了
什么，不把它当作另一套业务逻辑。`data/` 不是 Python 模块，但会参与运行时行为，因此
一并覆盖。

| 组 | 文件 | 先问的问题 |
| --- | --- | --- |
| 基础 | `paths.py`、`resources.py`、`context.py`、`project_trust.py`、`shell_config.py` | 文件从哪里发现，哪些被信任过滤？ |
| 输入 | `skills.py`、`prompt_templates.py`、`system_prompt.py`、`self_docs.py` | 用户文本怎样变成系统提示或模型输入？ |
| 工具 | `tools.py`、`image_processing.py` | 工具 schema、并发、截断、取消、图片降级如何工作？ |
| provider 数据 | `provider_catalog.py`、`catalog_loader.py`、`models_dev.py`、`models_dev_store.py`、`thinking.py` | 元数据怎样合并、校验和刷新？ |
| 身份与 runtime | `credentials.py`、`provider_config.py`、`provider_runtime.py`、`oauth.py`、`oauth_anthropic.py`、`oauth_github_copilot.py`、`oauth_device.py`、`oauth_registry.py`、`oauth_types.py` | 何时读取 secret，何时创建或关闭客户端？ |
| 扩展 | `built_in_extensions.py`、`extension_installer.py`、`extensions/{api,loader,providers,provider_registry,runtime}.py` | 扩展能做什么，谁监督其生命周期？ |
| 本地推理 | `local_backends.py`、`extensions/builtins/llama_cpp/{__init__,state,router,service,huggingface}.py`、`tui/local_backends.py` | 长任务如何进度化、取消、持久化？ |
| 会话 | `events.py`、`session.py`、`session_preparation.py`、`session_manager.py`、`branch_summary.py`、`context_window.py`、`session_stats.py`、`session_usage.py`、`session_export.py`、`commands.py`、`reload.py`、`diagnostics.py` | 内存、事件、append-only 历史如何一致？ |
| 前端 | `cli.py`、`rendering/*.py`、`rpc.py`、`tui/{__init__,adapter,state,autocomplete,config,file_drop,project_trust,terminal_title,terminal_notification,themes,widgets,app}.py` | 前端如何只消费 session 事件？ |
| 运营 | `logging_config.py`、`update_check.py`、`updater.py`、`version.py` | 诊断、版本检查和升级如何避免妨碍 agent？ |
| 资源数据 | `data/catalog.toml`、`data/models-dev-catalog.json`、`data/release-notes/releases.json`、`data/docs/*`、`data/examples/extensions/*` | 哪些是发行时输入，谁读取它们？ |

## 读大文件的办法

`session.py`、`provider_config.py`、`tui/app.py`、`tui/widgets.py`、`session_export.py` 和
llama.cpp `service.py` 很长。不要从第一行线性读到底；按下面的“公开入口 → 状态 →
协作者 → 辅助函数 → 测试”漏斗读：

```text
公开入口（load/prompt/run_tui_app/export）
  → 保存的 dataclass 和 Protocol
  → 一个主循环或 transaction boundary
  → 只追入口实际调用到的 private helper
  → 为反例寻找测试名称
```

如果能向一位不会 Python 的同事解释“为什么这里不能先写磁盘、后询问信任”，就已经比
逐行背诵更接近理解。
