# 本机设置页

这个组件在 localhost 上显示一个短期设置页面，把单行配置值保存到当前用户的系统凭据后端，再按声明映射为业务子进程环境变量。当前 `deepseek` profile 只包含：

- `CUSTOM_AGENT_MODEL`
- `CUSTOM_AGENT_REASONING_EFFORT`
- `CUSTOM_AGENT_VISION`

它不包含 API URL 或 API Key。

## 使用

```bash
npm ci --ignore-scripts
node src/profile.ts status default
node src/profile.ts setup default --confirmed
node src/profile.ts run default -- your-program your-arguments
```

`status` 退出码 0 表示三项都存在，2 表示至少一项缺失。`setup` 只有在完成独立第二轮“已确认”后才允许传入 `--confirmed`，并返回 30 分钟有效的 localhost 链接；用户亲自填写，Agent 不自动操作页面。`run` 只向指定子进程注入当前 profile 的值。

## 字段声明

字段文件支持 `password`、`url`、`text` 和 `select`。`select` 必须提供 2 至 12 个唯一的 `options`：

```json
{
  "version": 1,
  "id": "sample-setting",
  "label": "思考强度",
  "credential": "sample/setup/effort",
  "ui": {
    "inputType": "select",
    "options": ["low", "medium", "high"],
    "saveLabel": "更新"
  }
}
```

同一页面支持 1 至 16 项。已有项留空会保留；输入新值时需要确认替换。页面和 API 不回填已保存值，提交完成或离开页面会清空输入。

系统后端：macOS Keychain、Windows Credential Manager、Linux Secret Service。不可用时停止，不回退到明文文件。

## 验证

```bash
npm run check
npm run build
npm test
```

前端源码位于 `web/app.ts`，构建产物为 `public/app.js`。修改前端后必须重新构建并提交产物。
