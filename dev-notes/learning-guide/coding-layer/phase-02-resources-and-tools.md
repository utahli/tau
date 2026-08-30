# Coding 层 Phase 2：项目资源、信任与真实工具

这一阶段回答一个根本问题：**模型的上下文和文件权限从哪里来，为什么不是把 cwd 下所有
内容直接塞给模型？** 先读本章，再读系统提示与 `CodingSession.load()`，后者只是把这里
产生的结果装配进去。

## 1. 路径是配置的根，而不是散落的 `Path.home()`

`src/tau_coding/paths.py` 的 `TauPaths` 集中定义 Tau home、session、凭据、provider、
resource、diagnostic 等位置。它也从 cwd 生成稳定 session slug。这个很朴素的对象避免了
不同模块各自猜 `~/.tau`，所以测试可以注入临时 home。

费曼说法：像办公室的收发室地址簿；业务人员只要说“档案室”，不用各自记街道门牌。

读完应能回答：为什么 `SessionManager` 和 `FileCredentialStore` 应接受路径/`TauPaths`，
而不读取全局环境？答案是隔离测试、可迁移配置和单一的文件布局权威。

## 2. 资源发现与信任是两步

`resources.py` 定义 `TauResourcePaths`：它描述候选目录和资源来源；
`resource_paths_with_cwd()` 绑定当前目录，`resource_paths_with_project_trust()` 再决定
项目目录是否保留。`discover_system_prompt_resources()` 负责找自定义/追加系统提示，解析
Markdown frontmatter 的通用函数是 `parse_markdown_resource()`。

`context.py` 则专门发现 `AGENTS.md`：先找 project root，再从根到 cwd 收集祖先文件，
并去重解析后的真实路径。顺序很重要：上层通用指令先出现，离当前目录越近的指令后出现。

```text
候选路径（home / agents / project）
        │
        ├── resources, skills, prompts, themes, extensions
        ▼
ProjectTrustCoordinator.resolve(cwd)
        │  未信任：移除 project 来源；信任：保留
        ▼
各资源 loader 返回 内容 + ResourceDiagnostic
```

`project_trust.py` 是此阶段的安全核心：

- `canonicalize_project_path()` 处理符号链接和 macOS 路径规范化，避免同一项目以不同字符串
  绕过判断；
- `ProtectedResourceDetector` 列举会带来指令或执行代码的项目资源；
- `ProjectTrustStore` 原子读写已保存的决定；
- `ProjectTrustCoordinator.resolve()` 汇总 override、默认、交互 prompt 和 extension decider，
  并采取 fail-closed 行为；
- `ProjectTrustEvent`/diagnostic 让前端显示决定，而不是把安全决定藏在 UI 内。

这里的边界尤其重要：信任的是“在这个 canonical 项目目录中加载项目级资源”，并不是给
`bash` 另加 sandbox。`bash` 的执行能力由工具/宿主策略决定；trust 防的是无意执行项目
提供的 prompt、skill 或 Python extension。

`shell_config.py` 读 shell 前缀和 trust 默认值，是一个小而关键的“用户偏好 → 结构化
`ShellSettings`”转换器；不要把其 JSON 解析复制到 CLI。

## 3. 三种可复用输入如何汇合

| 模块 | 最小模型 | 用户文本中的入口 | 失败策略 |
| --- | --- | --- | --- |
| `skills.py` | `Skill(name, description, content, ...)` | `/skill:name args` | 诊断并跳过坏文件；未知调用保留给正常输入。 |
| `prompt_templates.py` | `PromptTemplate` | `/template args` | 解析参数、替换 `$1`/`$@`；参数不匹配给明确错误。 |
| `context.py` | `ProjectContextFile` | 自动发现 | 读取失败生成 `ResourceDiagnostic`。 |
| `system_prompt.py` | `BuildSystemPromptOptions` | session 装配 | 纯函数确定性拼装。 |

技能和模板的共同点是“磁盘上的可重用文本”；不同点是技能有专门的 `/skill:` 语法、可选择
禁止模型自行调用，模板则是更一般的参数替换。两者都不能直接在 UI 展开：
`CodingSession.expand_prompt_text()` 才是统一的解释位置，保证 TUI、print、RPC 一致。

`system_prompt.py` 将工具说明、工具 guidelines、项目上下文、技能索引、扩展 prompt
section、日期和 cwd 排成单一文本。`format_project_context()` 对 XML 包裹内容转义，避免
项目文本逃出它的边界；这提高可读性和抗混淆能力，但不是把不可信项目变安全的替代品。

## 4. 工具：定义与执行器分离

`tools.py` 的 `ToolDefinition` 同时保留给系统提示使用的 `prompt_snippet`/guideline，
和给循环使用的 schema/executor；`to_agent_tool()` 投影为 `tau_agent.AgentTool`。这解释了
为什么 coding 层可以构造更好的提示，却不让 agent core 依赖 `ToolDefinition`。

`create_coding_tools()` 的默认次序为 `read`、`write`、`edit`、`bash`。次序会出现在系统
提示和 UI 中，因此不要把它当无意义细节。

| 工具 | 主入口 | 必须解释的保证 |
| --- | --- | --- |
| read | `create_read_tool_definition()` | 相对路径以 cwd 为根；文本支持 offset/limit；按行和字节截断；图片转 `ImageContent` 或在模型无视觉能力时降级。 |
| write | `create_write_tool_definition()` | 创建父目录、写完整内容；每个解析后路径使用 `_file_lock`，避免并发交错写。 |
| edit | `apply_edits_to_normalized_content()` | 每个非空 `oldText` 必须恰好匹配一次，编辑不重叠；统一 LF 后从后往前替换，再还原原换行/BOM。 |
| bash | `create_bash_tool_definition()` | stdout/stderr 合并、尾部截断；取消/超时杀整个进程组；可选 prefix 在 shell 前加入命令。 |

两个截断函数故意不同：`read` 用 `truncate_head()`，因为文件开头更利于理解；`bash` 用
`truncate_tail()`，因为报错通常在最后。被截断的 bash 输出落到临时文件，因此模型仍能
根据路径请求后续操作。

## 5. 图片与取消不是附属细节

`image_processing.py` 是 read 工具的防御层。它先检查文件签名和 Pillow 元数据，再对尺寸、
动画格式、编码结果施加界限，最终返回 `ProcessedImage` 或可展示的 `ImageProcessingFailure`。
所以“read 图片”不是把任意字节 base64 后发给模型。

`_communicate_with_cancellation()` 同时等待子进程和 `ToolCancellationToken`；POSIX 上
`_kill_process_tree()` 杀进程组，避免 shell 的子孙进程残留。费曼检验题：只 `terminate()`
shell 父进程为什么不够？因为已 fork 的编译器/服务器可能继续占用端口和写文件。

## 6. 40 分钟实验

1. 读 `tests/test_resources.py`，为同名 resource 在多个目录的优先级写一张小表。
2. 用 `uv run pytest tests/test_coding_tools.py -q` 验证 edit 的“零/多次匹配”反例。
3. 在纸上模拟 `oldText="a\na"` 出现两次时的 edit；说明为何“随便替第一个”会让模型
   的意图不可预测。
4. 找 `tests/test_project_trust.py` 中取消 trust 的 case，复述它为何不只是一个 warning。

完成标准：不用看源码，能画出 `cwd → trust filter → resources → system prompt/tools` 的数据流，
并能指出 project trust 与 shell execution 分别保护什么。
