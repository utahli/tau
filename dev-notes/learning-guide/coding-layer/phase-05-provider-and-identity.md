# Coding 层 Phase 5：provider、模型目录与身份

本章的目标是区分四件经常混在一起的事：**模型目录、用户偏好、秘密凭据、运行时客户端**。
它们分别有不同的刷新频率、持久化位置和失败方式。

```text
catalog.toml / models.dev cache     “有什么模型、有哪些能力”
        │
ProviderSettings                    “用户想默认用什么”
        │                 credentials.json / 环境变量
        └───────────────┬───────────────┘
                        ▼
          create_model_provider()  “这一次请求用谁、怎样认证”
                        ▼
     tau_ai Anthropic/OpenAI/... provider 或 DynamicProvider runtime
```

## 1. 模型目录：数据驱动，而非 if/else 堆砌

- `provider_catalog.py` 定义不可变的 `ProviderCatalogEntry`、模型元数据和分级价格计算；
  `builtin_provider_entry()` 是消费者的轻量查询入口。
- `catalog_loader.py` 读取随包 `data/catalog.toml`，合并 `~/.tau/catalog.toml` 覆盖和
  models.dev 生成 overlay。它用 Pydantic 私有 schema 先验证，再转成公开 dataclass；删除
  模型使用 tombstone 而不是要求用户复制整份内置目录。
- `models_dev.py` 把远端文档转换为受控 overlay：过滤不适合的模型，标准化 reasoning、
  cost 和 context metadata。
- `models_dev_store.py` 负责下载、缓存、原子写入和失败回退，不让 catalog loader 直接依赖
  网络。
- `thinking.py` 把跨厂商的 `ThinkingLevel` 映射为 reasoning effort 或 Anthropic budget；
  它是纯策略函数，不发网络请求。

费曼解释：catalog 是餐厅菜单，settings 是客人的常点菜，credential 是付款方式，runtime
provider 才是厨师实际拿到的一张订单。菜单更新不能覆盖信用卡，换默认菜也不该重新下载菜单。

学习时沿着一个 model 的字段追踪：`context_window` 影响 `context_window.py` 的估算，
`supports_images` 影响 `ImageSupportState`，thinking metadata 影响可选等级，cost tier 进入
`session_stats.py`/`session_usage.py`。这说明 catalog 不只是 UI 下拉列表。

## 2. ProviderSettings：定义与偏好分层保存

`provider_config.py` 定义三种静态配置：

| 配置 | 对应场景 | 特别字段 |
| --- | --- | --- |
| `OpenAICompatibleProviderConfig` | OpenAI-compatible、Google、Mistral、代理网关 | base URL、headers、模型 compat、API 类型。 |
| `AnthropicProviderConfig` | 原生或代理的 Anthropic Messages | API key/OAuth、cache 和 thinking 参数。 |
| `OpenAICodexProviderConfig` | Codex OAuth/API key | account id、reasoning effort。 |

`ProviderSettings` 同时容纳 provider 定义、default provider、scoped models 和 thinking
defaults；`ProviderSelection` 是解析后的“一次选择”。`load_provider_settings()` 合并目录，
处理 legacy migration；`save_provider_settings()` 和 catalog 写入采用临时文件/备份策略。
关键解析入口是 `resolve_provider_selection()`，而不是 CLI 自己拼 provider/model。

`provider_config_from_catalog_entry()` 说明 catalog 是 provider 定义的默认来源；用户保存的
settings 是偏好/覆盖层。阅读时特别检查：`validate_provider_model()`、
`provider_thinking_levels()`、`provider_model_supports_images()` 如何将不可信 JSON 变成
受校验的决策。

## 3. 凭据：引用名称，不把 secret 写进 provider 设置

`credentials.py` 的 `FileCredentialStore` 保存 `ApiKeyCredential` 或 `OAuthCredential`，并
用 `CredentialStore` protocol 使测试可替换。名称和 OAuth metadata 都经过验证，JSON 也只
允许 JSON-compatible 值。

此处分离的安全收益是：provider catalog/settings 可以被查看、导出、同步，而实际 token
不会跟着传播；动态 provider 也能只声明 `credential_name`。环境变量仍可作为另一条凭据
来源，但消费者必须明确地查询它，而非把整个 `os.environ` 序列化。

## 4. 运行时 provider：在最后一刻连接到 `tau_ai`

`provider_runtime.py:create_model_provider()` 根据已验证的配置和所选模型构建实际客户端，
返回带 `aclose()` 的 `ClosableModelProvider`。它是 Coding 层到 `tau_ai` 的唯一关键桥：

```text
Anthropic config        -> tau_ai.AnthropicProvider
OpenAICodex config      -> tau_ai.OpenAICodexProvider
compatible config + api -> OpenAICompatible / Google / Mistral / Anthropic provider
DynamicProvider         -> create_dynamic_model_provider()
```

不要把这个表理解为“通过 class name 选择”。实际选择还合并 per-model base URL、headers、
compat、thinking、OAuth runtime auth 和 Hugging Face inference route。

两个 resolver 是并发正确性的重点：

- `OAuthRuntimeCredentialResolver` 在请求前读/刷新 OAuth credential；
- `OpenAICodexCredentialResolver` 还处理 Codex account id；
- `_refresh_lock(credential_name)` 在同一 event loop 内按 credential 串行化刷新，避免两个
  并发请求同时消费同一 refresh token。

`CodingSession` 记录其拥有的 provider 并在 replacement/`aclose()` 时恰好关闭一次；故
runtime provider 不是可随意共享的全局单例。

## 5. OAuth 家族：协议通用层与厂商薄层

| 文件 | 责任 |
| --- | --- |
| `oauth_types.py` | UI 回调、认证信息、`OAuthProvider` protocol 等无厂商数据契约。 |
| `oauth_registry.py` | 内置 OAuth provider 的注册/替换，便于扩展和测试。 |
| `oauth.py` | OpenAI Codex PKCE、loopback callback、manual code、exchange/refresh。 |
| `oauth_anthropic.py` | Anthropic subscription 流程与 token refresh。 |
| `oauth_github_copilot.py` | GitHub device flow、企业域名与 Copilot base URL。 |
| `oauth_device.py` | 可取消、带 interval/backoff 的通用 device-code polling。 |

复述时要区分：登录是用户驱动的、可显示 URL/代码的流程；运行时 refresh 是 provider
发请求前的透明维护。前者经由 TUI/RPC command 的 callbacks，后者不能依赖 Textual。

## 6. 会话层如何使用这些对象

`session.py:_prepare_provider_selection()` 在 extension runtime 建好且 trust 已判定后选择
静态或动态 provider。优先级大致为显式请求、durable session 状态、session provider name、
默认选择；选择后再决定 model、thinking 和 inference route。这样 `/model`、resume 和
extension provider 使用同一套规则，而非三套分支。

模型切换包含两个动作：记录 `ModelChangeEntry`（可恢复历史）并替换 runtime provider
（可关闭资源）。只改一个会导致“历史说是 A、实际在用 B”或资源泄漏。

## 7. 验证与复述

```bash
uv run pytest tests/test_provider_catalog.py tests/test_provider_config.py
uv run pytest tests/test_provider_runtime.py tests/test_credentials.py
uv run pytest tests/test_oauth.py tests/test_oauth_providers.py
```

自测题：

1. 为什么 `models_dev_store` 的网络失败不应阻止已有 catalog 启动？
2. 何种改动进 catalog，何种改动进 `ProviderSettings`，何种改动进 credential store？
3. 两个并发 stream 同时发现 token 过期时，锁保护的是什么，锁不保护什么？
4. provider 的 `aclose()` 所有权为何必须明确？

若能画出一次 `/model provider/model` 从命令到 `ModelChangeEntry`、新 provider、图片支持
状态更新的路径，就完成本阶段。
