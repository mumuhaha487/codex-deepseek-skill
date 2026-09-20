# 原生子 Agent 路由故障排查

以下结论在 `2026-09-20` 的 Codex 桌面运行环境中验证；后续版本仍以 `test --json`、实际写入验收和子线程数据库元数据为准。

## 为什么不能设置独立 URL 和 Key

原生子 Agent 实际使用父任务的 Provider 路由。即使 Agent 文件曾声明独立 Provider，运行时也可能继承父 Provider。把只支持 DeepSeek 的 Key 写入父认证会让主 Codex 失去父模型访问能力。

因此当前 Skill 采用单一路径：

- 子 Agent 的 `model_provider` 等于顶层父 `model_provider`。
- API URL、API Key 和认证由父 Provider 统一管理。
- Skill 只设置子模型、思考强度和识图能力。
- 父凭据必须同时允许访问父模型和子模型。

`route_mode = inherited_parent_provider`、`credential_source = parent_provider` 和 `uses_dedicated_credential = false` 是预期结果，不是回退。

## 注册角色不等于默认选择角色

安装 `[agents.CustomAgent]` 只让角色可被显式选择。需要默认路由时，在全局或项目 `AGENTS.md` 中写明：

```markdown
# Default Custom Subagent Routing

- When the user explicitly requests a subagent or delegation, call `spawn_agent` with `agent_type = "CustomAgent"` and `fork_turns = "none"`.
- Do not select a model or reasoning effort directly; the `CustomAgent` role owns them.
- Do not fall back to `worker` or another standard subagent unless the user explicitly authorizes it.
- Before delegation, create a managed isolated Git worktree with `scripts/task_worktree.py start`. Give its absolute path and approved write scope to `CustomAgent`; never let it write the parent checkout.
- Checkpoint every attempt. Integrate and re-test accepted work, then finalize cleanup; abort isolated changes at the fifth failure before the parent agent takes over.
- For each distinct delegated task, track CustomAgent attempt failures, parent redirects, and review rejections from zero. If any count reaches five, do not request a sixth revision; the parent agent must implement and verify that task directly.
- Rephrasing or retrying the same acceptance goal does not reset those counts. Reset all three only for a genuinely new task, which should again start with `CustomAgent`.
```

更新后必须打开新任务。旧任务不会动态采用新指令。

## 识图没有生效

1. 运行 `status --json`，确认 `supports_vision = true` 和 `checks.model_modalities_valid = true`。
2. 确认上游模型本身确实支持图片输入。Skill 不会根据模型名自动判断。
3. 委派时把图片作为 `image` 或 `local_image` 输入传给 `CustomAgent`，不要只在父任务中提到文件名。
4. 完全重启 Codex 并创建新任务，避免旧会话缓存模型目录。

如果设置为 `false`，主 Agent 应先检查图片，再把视觉观察以文本发给子 Agent。

## `features.thread_tools` 警告

当前运行时不识别旧字段 `thread_tools`。运行 `repair --json` 会在备份后清除它。如果磁盘配置已无该字段但 UI 仍警告，完全退出 Codex 并重新打开。

`multi_agent_v2 = false` 是本 Skill 当前派发路径的一部分，不能一并删除。

## 成功证据

必须同时确认：

1. 父 Provider 直连口令为 `CUSTOM_AGENT_DIRECT_OK`。
2. 原生子 Agent 返回 `NATIVE_CUSTOM_AGENT_OK`。
3. 子线程数据库包含父 Provider、目标模型、所选思考强度和 `CustomAgent`。
4. 模型目录的输入模态与识图开关一致。
5. 子 Agent 在临时 Git 仓库写入指定验收文件，主工作区未被直接修改。

## 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 子 Agent 报模型无权限 | 父凭据不能访问目标模型 | 给父账号组增加目标模型权限，不替换父认证 |
| 主 Codex 修改后无法使用 | 父认证被替换成只支持子模型的 Key | 恢复原父 Provider 和认证，再运行 `repair` |
| 子任务仍是父模型或 `worker` | 调用没有指定 `CustomAgent` | 添加默认路由规则，在新任务重试 |
| 思考强度仍是旧值 | 旧 Agent 文件或旧任务缓存 | 运行 `repair`，重启并检查数据库元数据 |
| 识图开启但子 Agent 收不到图片 | 委派没有附带图片输入，或上游模型不支持 | 附带 `image`/`local_image`，并核实模型能力 |
| 子 Agent 仍只返回补丁 | Agent 文件仍是 `read-only` 或旧任务缓存 | 二次确认后运行 `repair --confirmed --replace-agent`，完全重启并新建任务 |
| 无法创建隔离任务 | 主工作区不干净或任务 ID/分支冲突 | 保留用户修改，不自动 stash/reset；清理冲突或完成当前修改后重试 |

## 恢复

1. 不修改用户业务项目。
2. 使用 `$CODEX_HOME/codex-custom-subagent/backups/` 中的备份恢复受管文件。
3. 不读取、回显或提交 `auth.json`、系统凭据或 API Key。
4. 先验证父模型仍可通过父 Provider 使用，再运行 `repair --json` 和 `test --json`。
