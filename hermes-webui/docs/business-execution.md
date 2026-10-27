# 轻量执行过程与可选业务检查

保留 Hermes 原有规划、工具权限、澄清、恢复和调度。工具卡片展开后增加“执行过程”：任务清单/委派目标、工具状态，以及实际存在的业务检查、结果核验、重试计数和耗时。澄清继续仍使用原有交互卡片。

默认只展示过程，不改变调用结果，也不增加模型请求。历史记录没有检查证据时显示“无业务检查记录”，不会补写“通过”。业务检查仅在对应 Profile 的 `config.yaml` 显式启用。

## 配置示例

以下示例中的工具名、字段名必须按实际业务接口替换。新配置在下一次 WebUI 发送消息时读取。

```yaml
webui_execution_policy:
  enabled: true
  max_tool_calls: 100
  max_concurrent_calls: 4
  time_budget_seconds: 600
  max_retries: 2                 # 0 到 5；首次执行之外允许的业务重试次数
  max_argument_bytes: 2097152
  max_artifact_bytes: 20971520
  allowed_profiles: [default]   # 当前是 Profile 范围，不是多用户账号隔离
  # allowed_tools: [实际允许的工具名]   # 不填则沿用原有工具范围；空列表拒绝所有
  denied_tools: []
  tools:
    mcp__documents__generate:   # 示例；替换成实际工具名
      required_args: [title]
      argument_types: {title: string}
      required_result_fields: [output_path]
      artifact_fields: [output_path]
      retry_safe: false        # 默认不允许失败后重复执行有副作用的操作
```

单个工具还可配置 `path_args: [path]`，检查指定参数路径是否位于当前工作区内。参数类型支持 string、integer、number、boolean、object、array；这不是完整 JSON Schema 引擎。

## 执行语义

- 已有原生权限/审批先执行，业务检查不会授予额外工具权限。
- 本轮业务预算在主 Agent 和子 Agent 间共享，不修改原有子 Agent 工具继承规则。
- 参数、工具范围或预算不符合时拒绝实际调用，并展示原因。
- 配置结果字段/产物字段后，才核验相应业务结果。普通聊天和未配置的工具保持原流程。
- 产物核验只读取当前工作区下的本地文件，检查非空、大小以及简单格式。Office 检查压缩包结构，PDF 检查文件头，JSON 检查可解析性；不代表内容、排版或业务合规已通过。
- 核验失败作为真实工具错误交给 Hermes，由现有规划器决定如何修正。不会在中间件里再次执行工具，也不会强制重跑整个任务。
- 只有显式 `retry_safe: true` 才允许该工具失败后再次尝试。预算按本轮、工具名累计；成功核验后清零。首次执行加 N 次重试，最多执行 N+1 次。
- 工具调用次数、并发和时间预算为本轮准入控制；时间预算不会强杀已经运行的进程。CPU/内存、操作系统隔离继续依赖原执行后端。
- 此入口覆盖原生工具执行路径；启用规则时，原生页面会拒绝 ACP/Codex 托管模型模式。外部 Gateway 有独立执行入口，本轮未接入此检查；不要将此配置当作 Gateway 或业务 API 的安全边界。
- 指定路径参数的检查不等于 shell 沙箱、多用户文件隔离或抗文件系统竞态的安全边界。这些能力仍需单独实施。

## 验证与使用

标准库单元测试不访问模型或真实 Hermes 数据：

```bash
python -B tests/test_execution_policy.py
```

开发时使用 `compose.yaml` 与 `compose.dev.yaml` 挂载源码。Python 修改需重启开发容器，JS/CSS 修改需浏览器强制刷新：

```powershell
cd D:\pythonCode\testhermes
docker compose -f compose.yaml -f compose.dev.yaml restart hermes-webui
```

正式发布仍需重新构建镜像，再重新创建正式容器。此改动不会自动更新 GHCR 中的既有镜像。
