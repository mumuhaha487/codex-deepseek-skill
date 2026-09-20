# deepseek

把父 Provider 已经可以访问的模型配置成 Codex 原生可写 `CustomAgent`。子智能体在主 Agent 管理的隔离 Git worktree 中直接修改代码；主 Agent 负责功能验收、自动整合、回退和清理。配置页只包含三个字段：

- 子代理模型
- 思考强度：`low`、`medium`、`high`
- 支持识图：`yes`、`no`

本 Skill 不收集 API URL 或 API Key，不创建独立 Provider，也不修改父 `model_provider`、父认证或 `auth.json`。子代理始终复用当前 Codex 顶层 Provider 和凭据。

用户可以把本仓库交给 Codex，然后说：

> 阅读这个项目仓库，帮我配置这个技能

[AGENTS.md](AGENTS.md) 会引导 Codex 在全局 `deepseek` Skill 目录安装、打开本机设置页、执行静态检查和原生子代理验收。安装和排障只能操作全局 Skill、`CODEX_HOME` 或隔离临时目录，不得修改用户当前业务项目。

创建或修改 `CustomAgent.toml`、模型目录、角色注册或设置 profile 前，Codex 必须先展示当前配置、目标配置、持久影响、修改范围以及能力/费用风险，并等待用户在下一条独立消息中只回复 `已确认`。单次请求中的“已确认”无效。没有第二次确认时，脚本也会拒绝 `setup`、`repair`、`disable` 和 `uninstall`。

每个可写任务在修改前创建独立 worktree 和任务记录，每轮修改形成 Git 检查点。功能验收通过后，最终代码自动压缩为单个提交并整合到主工作区，复测成功后删除全部临时 worktree、分支、检查点和记录。整合后失败会自动 revert；任一失败计数达到 5 时丢弃隔离修改并由主 Agent 接管，不发起第 6 次子智能体修订。

## 识图

选择 `yes` 会在模型目录中声明 `input_modalities = ["text", "image"]`，并要求 `CustomAgent` 直接检查委派中附带的图片。选择 `no` 时只声明文本输入，由主 Agent 查看图片后提供文本观察。

开关只是声明已知能力，不会探测模型，也不能为不支持图片的模型增加视觉能力。无法确认时应选 `no`。

## 要求

- Codex 桌面应用
- Python 3.11+
- Node.js 22.18+
- 当前父 Provider 的凭据能访问所选子代理模型

## 验收标准

只有以下证据全部一致才报告成功：

1. 父 Provider 直连返回 `CUSTOM_AGENT_DIRECT_OK`。
2. 原生子代理返回 `NATIVE_CUSTOM_AGENT_OK` 并在临时 Git 仓库成功写入验收文件。
3. 子线程数据库记录为父 Provider、精确子模型、所选思考强度和 `CustomAgent`。
4. 模型目录中的输入模态与识图开关一致。

完整流程见 [SKILL.md](SKILL.md)，故障原因见 [references/troubleshooting.md](references/troubleshooting.md)。
