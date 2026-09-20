---
name: deepseek
description: "配置、维护并使用用户指定模型作为 Codex 原生只读 CustomAgent；模型复用当前父 Provider 与认证，可设置 low/medium/high 思考强度和是否支持识图。任何持久化子智能体配置变更都必须经过独立第二轮“已确认”。普通 API 咨询或不需要子 Agent 的任务不触发。"
---

# deepseek

本 Skill 管理 Codex 原生 `CustomAgent`。它只设置子代理模型、思考强度和输入模态；始终继承当前 `config.toml` 顶层 `model_provider` 及其认证。

## 安全边界

- 不收集、不保存、不修改 API URL 或 API Key。
- 不修改顶层 `model_provider`、父 Provider 表、父 Provider 认证或 `auth.json`。
- 不注册独立子代理 Provider。升级时只移除旧版 Skill 自己用标记包围的 Provider 块。
- 模型必须能通过父 Provider 的现有凭据访问；否则原生子代理不能工作。
- 安装和排障只操作全局 Skill、`CODEX_HOME` 或隔离临时目录，不在用户业务项目中创建调试文件。

原因和恢复方法见 [原生路由故障排查](references/troubleshooting.md)。配置字段说明见 [子代理设置](references/configuration.md)，模型目录规则见 [兼容性与安全边界](references/compatibility.md)。

## 持久化配置二次确认

创建、修改、覆盖、修复、停用、删除或重建子智能体配置前，必须执行二次确认。此规则覆盖但不限于：

- `$CODEX_HOME/agents/CustomAgent.toml` 的任何字段。
- `config.toml` 中的 `agents.CustomAgent` 注册、受管功能标志或与子智能体有关的 Provider/路由配置。
- 受管模型目录中的子模型条目、模型 ID、思考强度、识图能力和角色指令。
- 本机设置页保存的模型、思考强度和识图设置，以及之后会据此重建配置的 profile。
- `setup`、`repair`、`disable`、`uninstall`，以及未来新增的任何持久化配置命令。

`status`、只读检查和不写配置的 `test` 不需要确认。仅调用一个已经配置好的 `CustomAgent` 也不需要确认。

确认协议是强制顺序，不能在一条用户消息中完成：

1. 第一次收到创建或变更请求时，只运行只读检查。不要打开设置页、保存 profile、运行写命令或手工编辑文件。
2. 向用户说明当前配置、请求的目标配置、将修改的文件/字段，以及修改后所有后续 `CustomAgent` 调用都会使用新配置。明确提示模型能力下降、费用上升和行为变化风险。
3. 要求用户在下一条独立消息中只回复 `已确认`。第一次请求里即使已经写了“已确认”“直接改”“不用问”等内容，也不能视为第二次确认。
4. 只有收到精确回复 `已确认` 后，才可执行刚才完整描述的变更，并向写命令传入 `--confirmed`。
5. 确认只对已描述的目标、字段和文件有效。目标模型、Provider、API 地址、费用层级或修改范围发生变化时，必须重新说明并再次等待新的 `已确认`。

如果用户只是说“调用 DeepSeek 子智能体”“把智能体改成 DeepSeek”或类似含糊表述，不得推断为授权修改持久配置。先对比当前角色与请求，例如：

> 当前 `CustomAgent` 使用 Gemini；你的提示词要求调用 DeepSeek。若修改持久配置，今后所有 `CustomAgent` 调用都会从 Gemini 切换为 DeepSeek，可能改变能力、费用和输出行为。计划修改：`CustomAgent.toml` 的模型字段及对应模型目录条目。是否执行此持久变更？如确认，请在下一条消息中只回复“已确认”。

当前 Skill 不修改 API URL、API Key 或父 Provider；这类请求仍应报告为超出当前能力，不能因为用户确认而绕过“安全边界”。若未来 Skill 增加这些能力，也必须受上述二次确认协议约束。

## 配置流程

1. 运行 `status --json`，读取当前模型、思考强度、识图能力、父 Provider 和受管文件状态。
2. 首次安装、字段缺失或用户要求更换时，先按“持久化配置二次确认”展示当前值、目标值、影响和文件范围，并等待下一条消息精确回复 `已确认`。
3. 确认后运行 `node <skill-dir>/scripts/credential-ui/src/profile.ts setup default --confirmed`，展示 localhost 页面并让用户亲自填写：
   - 子代理模型：精确模型 ID。
   - 思考强度：`low`、`medium` 或 `high`。
   - 支持识图：`yes` 或 `no`。
4. 页面保存后，经包装器运行：

```text
node <skill-dir>/scripts/credential-ui/src/profile.ts run default -- python3 <skill-dir>/scripts/codex_custom_agent.py setup --model-env --effort-env --vision-env --confirmed --json
```

5. `setup` 和 `repair` 使用桌面应用内置 Codex。若返回 `new_task_required` 或 `restart_required`，完全重启 Codex 并打开新任务。
6. 验收必须同时确认父 Provider 直连口令 `CUSTOM_AGENT_DIRECT_OK`、原生口令 `NATIVE_CUSTOM_AGENT_OK`，以及子线程数据库中的父 Provider、精确模型、所选思考强度和 `CustomAgent` 角色。
7. 最终只汇报模型、思考强度、识图能力、父 Provider、角色和备份位置。不要读取或输出认证内容。

入口命令：

```text
python3 <skill-dir>/scripts/codex_custom_agent.py <command> --json
```

- `status`：只读检查角色、模型目录、父 Provider 和桌面运行时。
- `setup`：写入所选模型、思考强度和识图能力，并执行验收；必须二次确认并传入 `--confirmed`。
- `test`：执行父 Provider 直连与原生 `spawn_agent` 验收。
- `repair`：按已保存设置和当前父模型/Provider 重建配置；必须二次确认并传入 `--confirmed`。
- `disable`：停用角色，保留模型目录和父认证；必须二次确认并传入 `--confirmed`。
- `uninstall`：移除本 Skill 管理的角色和模型目录；不删除任何认证；必须二次确认并传入 `--confirmed`。

## 识图行为

- `supports_vision = true` 时，模型目录写入 `input_modalities = ["text", "image"]`。有图片的委派应把图片作为 `image` 或 `local_image` 输入直接交给 `CustomAgent`；不要先转交主 Agent 做视觉解读。子 Agent 的开发指令也会要求它直接检查收到的图片。
- `supports_vision = false` 时，模型目录只声明 `input_modalities = ["text"]`。主 Agent 负责查看图片，并把与实现相关的视觉观察作为文本交给子 Agent；子 Agent 不得声称看过图片。
- 此开关声明用户已确认的模型能力，不会根据模型名称猜测，也不能让本来不支持图片的端点获得视觉能力。

## 主 Agent 工作流

主 Agent 负责计划、派发、验收和最终写入；`CustomAgent` 只读分析并返回候选补丁。

1. 阅读项目约束，把需求拆成有依赖顺序的计划点，并为每一点写明范围、验收标准和测试。
2. 调用 `spawn_agent(agent_type="CustomAgent", fork_turns="none")`。一次只派发一个边界明确的计划点，要求完整 unified diff、测试命令和假设。
3. 不显式指定模型或 reasoning effort；`CustomAgent` 角色配置拥有这些字段。
4. 不静默回退到 `worker`、`default` 或其他子 Agent。失败时保留原始结构化错误；只有用户明确授权后才可改用其他子 Agent。
5. 主 Agent 在隔离副本或临时 worktree 中应用候选补丁并测试。失败时把具体文件位置、命令证据、预期行为和修改方向发回同一个子 Agent，要求完整替换补丁。
6. 候选补丁通过该计划点全部验收后，才应用到真实工作区并复测。

### 同一任务失败预算

主 Agent 对每个独立派发的计划点维护三个从 `0` 开始的任务级计数器：

- `attempt_failures`：`CustomAgent` 启动或执行失败、没有返回可用的完整候选补丁，或候选实现无法完成该任务时加 `1`。
- `parent_redirects`：因模型跑偏、过度思考、忽略范围或验收标准，主 Agent 必须介入并重新给出修改方向时加 `1`。
- `review_rejections`：候选补丁在主 Agent 的代码审核、应用或测试中未通过，因而被拒绝时加 `1`。

一次失败循环可以同时增加多个计数器。任一计数器达到 `5`（包括第 5 次）后，立即停止对该任务的子智能体修订，不得发起第 6 次调用或要求第 6 版补丁；主 Agent 必须直接使用自己的模型编写、测试并完成该任务。该接管是主 Agent 直接实现，不是切换到其他子 Agent，也不修改 `CustomAgent` 的持久配置，因此不触发配置二次确认。

计数以任务的实际范围和验收目标为准。重新措辞提示词、重复派发、拆换说法或对同一未通过实现做小幅调整，都不能重置计数。只有当前计划点已经完成或明确终止，并开始一个范围和验收目标明确不同的新计划点时，三个计数器才重新从 `0` 开始；下一个任务仍按正常流程优先调用 `CustomAgent`。

注册角色不等于默认选择角色。用户要求默认使用 `CustomAgent` 时，按 [故障排查文档](references/troubleshooting.md#注册角色不等于默认选择角色) 配置 `AGENTS.md`，然后在新任务中验证数据库元数据。

## 状态处理

- `ready`：静态配置、父 Provider 直连、原生路由和数据库元数据均通过。
- `configured`：静态配置完整，尚未完成实时验收。
- `partial`：检查 `checks`；常见原因是旧功能标志、模型目录或 Agent 文件不一致。
- `configuration_missing`、`model_selection_required`：回到本机设置页补齐三项。
- `operation_in_progress`：等待当前操作结束，不并发写配置。
- `conflict`：报告冲突文件和字段，等待用户决定。
- `native_child_failed`：检查父 Provider 是否允许访问目标子模型。

默认使用当前 `CODEX_HOME`；只有用户明确指定其他 Codex Home 时才传 `--codex-home`。
