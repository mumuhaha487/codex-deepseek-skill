# 兼容性与安全边界

## 支持范围

- macOS、Windows、Python 3.11+，Codex 桌面应用至少启动过一次。
- 精确模型 ID 长度不超过 128，只含字母、数字、点、下划线、冒号、斜杠或连字符。
- 思考强度只允许 `low`、`medium`、`high`。
- 识图能力只允许 `yes` 或 `no`。
- 父 Provider 必须能够通过当前认证访问目标模型，并支持 Codex 所需的 Responses 工具调用。

## 受管位置

- Codex 配置：`$CODEX_HOME/config.toml`
- 合并模型目录：`$CODEX_HOME/models-with-custom-agent.json`
- Agent 文件：`$CODEX_HOME/agents/CustomAgent.toml`
- 状态与备份：`$CODEX_HOME/codex-custom-subagent/`
- 项目任务记录：Git 公共目录下的 `codex-custom-agent/tasks/`
- 项目隔离 worktree：项目根目录下临时的 `.codex-worktrees/<task-id>/`

状态目录沿用旧名称只为兼容历史备份。公开 Skill 名称是 `deepseek`。

## Provider 与认证

程序从顶层 `model_provider` 读取父 Provider，并把同一个值写入 `CustomAgent.toml`。它不会：

- 创建 `[model_providers.custom_agent]`。
- 修改顶层 `model_provider`。
- 修改父 Provider 表或认证子表。
- 读取或写入 `auth.json`。
- 保存、替换或删除 API Key。

旧版 Skill 自己用 `BEGIN/END CODEX-CUSTOM-SUBAGENT PROVIDER` 标记包围的 Provider 块会在升级时移除。没有该标记的用户配置不会被当作受管 Provider 删除。

## 模型目录

自定义条目从当前父模型条目深拷贝，只替换子模型标识、说明、默认思考强度、输入模态和 `multi_agent_version = "v1"`。

- 识图 `yes`：`input_modalities = ["text", "image"]`，`supports_image_detail_original = true`。
- 识图 `no`：`input_modalities = ["text"]`，`supports_image_detail_original = false`。

父模型的 `multi_agent_version` 同步设为 `v1`，`features.multi_agent_v2` 设为 `false`，以使用当前可验证的原生派发路径。`repair` 会清除已失效的 `features.thread_tools`，但保留其他有效功能标志。

## 原生验收

日常调用必须显式使用：

```text
spawn_agent(agent_type="CustomAgent", fork_turns="none", ...)
```

`CustomAgent.toml` 固定使用 `sandbox_mode = "workspace-write"`，但日常写入只能发生在 `task_worktree.py start` 创建的隔离 worktree。主工作区不干净时管理器拒绝启动可写任务，不会自动 stash、提交、reset 或清理用户修改。

实时验收检查子 Agent 口令、临时 Git 仓库中的实际写入和 `$CODEX_HOME/state_*.sqlite` 的 `threads` 元数据：父 Provider、精确模型、所选思考强度和 `CustomAgent` 角色。子 Agent 自述或 UI 标签不能替代这些证据。
