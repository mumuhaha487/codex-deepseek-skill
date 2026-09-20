# 子代理设置

本机页面只保存三个非认证设置：模型 ID、思考强度和识图能力。它不显示 API URL 或 API Key，也不会修改 Codex 父 Provider。

## 二次确认门槛

打开设置页会允许覆盖后续用于生成 `CustomAgent.toml` 的持久设置，因此也属于受保护的配置变更。第一次收到创建或修改请求时，只能运行 `status` 并展示：当前值、目标值、修改文件/字段、后续调用的持久影响，以及模型能力和费用风险。必须等用户在下一条独立消息中只回复 `已确认` 后，才能运行带 `--confirmed` 的设置或管理命令。首次请求里自带的确认无效。

## 打开页面

将当前 `SKILL.md` 所在绝对目录记为 `SKILL_DIR`：

```bash
npm --prefix "$SKILL_DIR/scripts/credential-ui" ci --ignore-scripts
node "$SKILL_DIR/scripts/credential-ui/src/profile.ts" status default
node "$SKILL_DIR/scripts/credential-ui/src/profile.ts" setup default --confirmed
```

页面只有一个“更新”按钮，三个字段同页提交：

| 字段 | 环境变量 | 可选值 |
| --- | --- | --- |
| 子代理模型 | `CUSTOM_AGENT_MODEL` | 精确模型 ID |
| 思考强度 | `CUSTOM_AGENT_REASONING_EFFORT` | `low`、`medium`、`high` |
| 支持识图 | `CUSTOM_AGENT_VISION` | `yes`、`no` |

设置保存在当前用户的系统凭据后端，只是为了避免普通配置文件被其他工具随意改写；这些值不是 API 凭据。已有项留空会保留，页面不会回填原值。

## 应用设置

```bash
node "$SKILL_DIR/scripts/credential-ui/src/profile.ts" run default -- python3 "$SKILL_DIR/scripts/codex_custom_agent.py" setup --model-env --effort-env --vision-env --confirmed --json
```

管理程序验证三个值后写入受管模型目录、`workspace-write` 的 `CustomAgent.toml`、角色注册和状态清单。它从当前 `config.toml` 只读获取父模型和父 Provider。若现有 Agent 文件发生冲突，只有已明确二次确认完整覆盖时才可额外传入 `--replace-agent`；原文件会先进入配置备份。

无法确认模型是否支持图片时选择 `no`。选择 `yes` 仅声明能力，不执行自动探测。
