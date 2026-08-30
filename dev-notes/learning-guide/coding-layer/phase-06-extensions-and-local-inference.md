# Coding 层 Phase 6：扩展、动态 provider 与本地推理

扩展是 Tau 最需要克制的开放面。正确的模型不是“把 Python 文件 import 进来就行”，而是：
**发现 → 信任过滤 → 加载 → 注册 → 组合 → 生命周期清理**。本章同时覆盖 built-in
llama.cpp，因为它正好验证了这套抽象可以承载长时间、本地、可取消的操作。

## 1. 五层扩展结构

| 层 | 文件 | 责任 |
| --- | --- | --- |
| 公共契约 | `extensions/api.py` | `ExtensionAPI`、hook event/result、UI bridge、工具/命令注册、diagnostic 模型。 |
| 发现与加载 | `extensions/loader.py` | 在资源目录、显式路径和项目目录中发现 manifest/Python，隔离模块名并产生诊断。 |
| provider 契约 | `extensions/providers.py` | `DynamicProvider`、`ProviderModel`、认证和 transport 的不可变验证模型。 |
| 动态注册表 | `extensions/provider_registry.py` | 分层 overlay、刷新任务、credential-aware 可用性与关闭。 |
| 运行时编排 | `extensions/runtime.py` | 生成隔离、load/bind、工具合成、hook dispatch、session lifecycle 和 cleanup。 |

`extensions/__init__.py` 只导出上述 public surface；阅读它可以确认扩展作者被允许依赖什么。
`extension_installer.py` 处理来源安装和强制覆盖，但不会让“安装”绕过项目 trust：安装第三方
代码本身已是用户明确动作，项目自动发现仍需信任路径。

## 2. ExtensionAPI 不是直接操作 session 的后门

`ExtensionAPI` 允许扩展声明工具、slash command、input/tool hooks、provider、prompt section、
sidebar/main-view 贡献；它把调用收集到当前 `ExtensionRuntime`。扩展 UI 经 `UiBridge`：
非 TUI 环境可用 `NullUiBridge`，print 模式用 `StderrUiBridge`，所以 extension 不能假设
Textual 存在。

典型单回合顺序如下（省略错误诊断）：

```text
ExtensionRuntime.load() -> loader -> setup(api) -> registrations
CodingSession.load()    -> runtime.bind(session) -> compose_tools/system sections
session.prompt(text)    -> dispatch_input_hooks
AgentHarness tool call  -> dispatch_tool_call_hooks -> executor -> result hooks
agent events            -> runtime harness listener -> turn hooks
session.aclose()        -> session_shutdown -> unregister/close
```

`InputHookResult`、`ToolCallHookResult`、`ToolResultHookResult` 明确把“观察”“转换”“阻止”
建模为结果，而不是靠异常偷偷控制流。复述题：为什么 hook 不应让 extension 直接编辑
`harness.messages`？因为那会跳过消息持久化、event 发送和 history repair 的统一边界。

## 3. 发现、信任和 generation

`loader.extension_dirs()` 列出候选目录；`discover_extensions()` 只发现；
`load_extensions()` 才执行模块。`runtime.load()` 将结果安装为一个 generation，reload 时旧
generation 的注册和模块可被清理。把“发现”与“执行”拆开，才能让 UI 显示诊断并让 trust
在执行项目前生效。

`CodingSession.load()` 特意先创建 cwd-bound runtime，先加载 eligible（非项目）扩展，调用
`ProjectTrustCoordinator` 后才可加载项目扩展。session resume/new session 也会 fresh-stage
runtime，避免 source 项目的 Python registrations 泄漏到 destination cwd。

## 4. 动态 provider 的 overlay

`DynamicProviderRegistry` 不是一个全局 `dict[name, provider]`：它有 layer token，较新的
runtime 可以覆盖较旧的同名 provider，关闭 layer 后自动露出下层。`ProviderModelSnapshot`
和 refresh operation 让模型发现可以后台进行、可取消、可诊断。

动态 provider 的 author 要提供：稳定 id、model snapshot、认证需求、runtime factory 和可选
refresh。`resolve_provider_auth()` 将 `RequiredApiKey`、`OptionalApiKey`、`NoAuth` 统一为
已解析的 header/secret，拒绝 extension 任意伪造 `Authorization` header。最终
`create_dynamic_model_provider()` 仍回到 Coding 层的 provider 生命周期账本。

这回答“为什么 provider registry 属于 Coding 层”：它涉及用户凭据、extension 生命周期和
TUI 刷新，远超 `tau_agent.ModelProvider` 的可移植协议。

## 5. local backend 是扩展能力的长期任务模板

`local_backends.py` 定义面向 UI 的通用抽象：

- `LocalBackend` 描述配置、状态、模型、search、install/remove/select 等动作；
- `LocalBackendRegistry` 为 backend 叠层、监督 operation task、记录 progress、支持取消；
- `LocalOperationContext` 是受限的进度/取消通道；
- `_safe_operation_result()` 和 redaction 避免异常或 secret 直接进入 UI。

`extensions/builtins/llama_cpp/` 是实现：

| 文件 | 白话职责 |
| --- | --- |
| `__init__.py` | 将服务注册为 extension/provider/local backend。 |
| `state.py` | 原子保存 endpoint、选择模型和 credential reference。 |
| `router.py` | 对 llama.cpp router 的探测、模型列表和下载进度协议。 |
| `service.py` | 最大的编排层：endpoint 健康检查、模型操作、动态 provider 和 backend action。 |
| `huggingface.py` | 搜索 GGUF repository/variant，发现 token，验证远端回复。 |
| `tui/local_backends.py` | 把通用 backend 状态变成 picker/config/progress screens。 |

读 `service.py` 的正确切口是 public `LlamaCppService` methods，再追 endpoint/状态/provider
adapter；不要先陷在 JSON response helper。它是“外部本地服务也不可信”的例子：每个 HTTP
回复都经 object/type/HTTP error helper 校验。

## 6. 最小实验与边界检查

先阅读 `src/tau_coding/data/examples/extensions/`：`hello_tool.py`、`prompt_section.py`、
`sidebar_status.py` 分别演示工具、prompt、UI 的最窄 API。然后：

```bash
uv run pytest tests/test_example_extensions.py tests/test_extensions.py
uv run pytest tests/test_extension_providers.py tests/test_local_backends.py
uv run pytest tests/test_llama_cpp_extension.py
```

最后用自己的话完成以下审计：

1. 项目 extension 在哪一个语句之后才可能执行？
2. reload 后旧 extension 的工具为什么不会残留？
3. 当两个 layer 都注册同名 provider，关闭上层后哪一个生效？
4. 为什么下载进度必须经 `LocalOperationContext`，而不是 backend 直接改 widget？

能回答这四题，就理解了 Tau “开放但仍可替换、可关闭、可测试”的扩展边界。
