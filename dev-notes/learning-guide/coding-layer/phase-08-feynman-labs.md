# Coding 层 Phase 8：费曼复述、实验与架构审查

这一章不是新功能介绍，而是把前七章变成可验证理解。每个练习都要求先口述，再用一个小
测试或源码锚点纠正自己。不要为了完成练习改生产代码；可以使用临时目录、fake provider 和
现有测试。

## 复述卡片

每张卡必须在两分钟内用非术语语言说清；卡住时回到括号中的模块。

1. **Tau 为什么需要 `CodingSession`？**
   Harness 是可移植的决策循环；session 增加真实 cwd、工具、资源、持久化、commands、
   provider 生命周期和前端事件（`session.py`）。
2. **为什么 session 历史是 append-only tree？**
   分支、压缩、repair 追加 entry/leaf，恢复时选择 root-to-leaf 路径；不覆盖过去就能审计
   和回退（`tau_agent.session` + `session.py`）。
3. **为什么先 trust 后加载项目扩展？**
   extension 是任意 Python；“仅显示警告”已太晚，import/setup 可能执行代码
   （`project_trust.py`、`extensions/runtime.py`）。
4. **为什么 provider config 不保存 API key？**
   配置可见且可合并，secret 有独立 store/环境生命周期（`provider_config.py`、
   `credentials.py`）。
5. **什么是 event adapter boundary？**
   session 发 domain event，adapter 更新 display state，widget 只渲染；因此 Textual 没有
   进入 agent core（`tui/adapter.py`、`tui/state.py`）。
6. **为什么 MessageEnd 是持久化的好时机？**
   一条模型/工具消息已完整，不会把中间 delta 误当历史；listener 仍可在中断后 reconcile
   （`session.py:_persist_on_message_end`）。

## 实验 A：从 CLI 到 JSONL 的一次回合

1. 打开 `cli.py:run_print_mode()`，找到其创建 `CodingSessionConfig` 与 renderer 的位置。
2. 打开 `session.py:prompt()`，标记交给 harness、yield event、持久化 listener 和 settled
   的位置。
3. 打开 `tau_agent/harness.py` 的 `prompt_message()`，把事件回流箭头补全。
4. 运行：

```bash
uv run pytest tests/test_cli.py tests/test_coding_session.py -q
```

交付：画一张图并注明“谁拥有 provider”“谁写 messages”“谁只渲染”。若把 renderer 写成
JSON，解释为什么 session 行为不变。

## 实验 B：安全边界反例

为以下输入分别写下预期和实际防线：项目中的 `AGENTS.md`、项目中的 extension、模型请求
`bash`、模型请求将图片读进无视觉模型。

| 输入 | 主要防线 | 不负责什么 |
| --- | --- | --- |
| `AGENTS.md`/skill/prompt | project trust + resource filter | 不限制已获准 shell 的命令。 |
| project extension | trust 在 loader 执行前生效 | 不审计 extension 内全部业务逻辑。 |
| bash tool call | 宿主/extension hook、取消/进程组处理 | trust 不是 shell sandbox。 |
| 图片 | image validator + `ImageSupportState` | 不替代 provider payload 验证。 |

然后运行：

```bash
uv run pytest tests/test_project_trust.py tests/test_coding_tools.py tests/test_image_processing.py
```

交付：向同伴解释为什么“项目可信”不能简化成“用户信任模型”。前者是资源代码/指令来源，
后者是 agent 行为风险，边界不同。

## 实验 C：故障排查演练

针对三个假想事故，先写观察点，再查源码验证：

| 事故 | 第一观察点 | 第二观察点 | 可能修复层 |
| --- | --- | --- | --- |
| provider stream 报错 | `AssistantErrorEvent`、TUI error | diagnostic run log、retry 判断 | `tau_ai` 或 provider runtime，不是 widget。 |
| resume 后工具结果缺失 | durable entries/repair | `_persist_on_message_end`、reconcile | session persistence / tool history。 |
| `/reload` 后旧工具还在 | reload summary | ExtensionRuntime generation/tool composition | extension lifecycle。 |

参考测试：`test_provider_runtime.py`、`test_tool_history.py`、`test_extensions.py`。交付：每种
事故用“症状 → 证据 → 最小负责模块”三句话讲完，避免“先改最大文件”。

## 实验 D：新增一个能力前的设计问卷

假设要增加 `/review` command 或一个新的 remote provider，先回答：

1. 它是 core command、prompt template，还是 extension command？为什么？
2. 是否需要 durable entry 才能在 resume 后解释结果？
3. 是否会引入 secret、网络、项目代码执行或长任务？对应哪个边界？
4. 新状态应属于 `CodingSession`、extension runtime、TUI state，还是 renderer？
5. 它如何在 print、TUI、RPC 里表现？没有 UI 时怎么降级？
6. 最窄的确定性测试文件是什么？

这份问卷的价值是阻止“为了 TUI 按钮而在 harness 加状态”或“为了 CLI 快捷而绕过 session
持久化”两种架构倒置。

## 最终验收清单

- [ ] 能从 `TauPaths` 解释资源、凭据、session 的文件归属。
- [ ] 能区分 resource discovery、project trust、system prompt build 三个阶段。
- [ ] 能解释四个 coding tools 的 schema 与关键失败/取消语义。
- [ ] 能追踪 catalog → settings → credential → runtime provider 的路径。
- [ ] 能说明 extension generation、provider layer 和 close 语义。
- [ ] 能画出 `CodingSession.load()` 与 `prompt()` 的 publication/persistence 边界。
- [ ] 能说明 renderer、RPC、TUI 都是 event consumer 的证据。
- [ ] 能为一次失败指出一个测试和一个诊断入口。

完成全部项目后，再阅读 `website/content/internals/architecture.md` 与
`website/content/internals/agent-loop.md`：你应能将面向用户的架构图映射回这里的具体模块，
而不是只会背图。
