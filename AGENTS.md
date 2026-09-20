# Repository Instructions

当用户要求“阅读这个项目仓库，帮我配置这个技能”或表达同等意图时：

1. 阅读根目录 `SKILL.md`、`references/configuration.md`、`references/compatibility.md` 和 `references/troubleshooting.md`。
2. 安装和排障不得修改用户当前业务项目。需要修改本仓库时，使用系统临时目录的独立克隆或隔离 worktree。
3. 将仓库安装到当前 `CODEX_HOME/skills/deepseek`，未设置时使用 `~/.codex/skills/deepseek`。复制时排除 `.git`、`node_modules`、`__pycache__` 和 `*.pyc`；不得覆盖其他 Skill。
4. 若旧 `codex-custom-subagent` 目录存在，先安装并验证 `deepseek`，再停用旧目录。状态目录继续使用 `$CODEX_HOME/codex-custom-subagent/` 以兼容旧备份。
5. 在安装目录运行 `npm --prefix <skill-dir>/scripts/credential-ui ci --ignore-scripts`。
6. 运行 `scripts/codex_custom_agent.py status --json`。缺少设置或用户要求更改时，先向用户展示当前配置、目标配置、将修改的文件/字段、对后续所有子智能体调用的持久影响，以及能力和费用风险。此时不得打开设置页或写入配置。
7. 必须等待用户在下一条独立消息中只回复 `已确认`。首次请求中出现的“已确认”“直接改”或同义表达无效；目标或范围改变后必须重新确认。
8. 收到有效确认后，运行 `node <skill-dir>/scripts/credential-ui/src/profile.ts setup default --confirmed`，展示 localhost 链接，让用户亲自填写模型、思考强度和识图能力。
9. 保存后通过包装器运行：

```text
node <skill-dir>/scripts/credential-ui/src/profile.ts run default -- <python3> <skill-dir>/scripts/codex_custom_agent.py setup --model-env --effort-env --vision-env --confirmed --json
```

10. 若尚未完成实时验收，运行 `test --json`。如需 `repair`，必须先重新说明具体修复写入范围并等待新的 `已确认`，然后传入 `--confirmed`。不得手工改受管 Agent 文件或模型目录。
11. 不请求、不读取、不修改 API URL、API Key、父 Provider、父 Provider 认证或 `auth.json`。旧版带标记的独立 Provider 只能由管理脚本在升级时移除。
12. 检查模型、思考强度、识图能力、父 Provider、`CustomAgent` 角色和子线程数据库元数据。识图开启时，图片应直接作为子代理输入；关闭时由主 Agent 提供视觉观察文本。
13. 安装角色不会自动替换标准 worker。只有用户明确要求默认使用 `CustomAgent` 时，才按 `references/troubleshooting.md` 配置全局或项目 `AGENTS.md`；这也是持久化变更，必须先说明范围并等待独立的 `已确认`。失败时不得静默回退。

任何时候都不得把认证内容写入仓库、命令参数、日志或最终回复。
