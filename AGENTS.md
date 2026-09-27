# Agent 接续入口

开始任务前读取默认分支的 [README.md](README.md)、[完整项目指令](docs/AGENT_SYSTEM_INSTRUCTIONS.md) 和实际 `.github/workflows/`；以实时文件和运行证据判断能力，不依赖旧聊天结论。

- 关键帧默认使用会话的 imagegen 工具；Agnes Image 仅在明确失败证据或用户指定下作备选。Actions 无 imagegen 工具不是降级理由。`images` 默认只准备私有提示词，`import_images` 接收经哈希验证的 imagegen 产物。见 [运行契约](docs/PRODUCTION_RUNTIME.md)。
- Agnes 按账户 RPM=1 调度：接入同一私有存储的调用共享 CAS FIFO 队列，默认所有 API 请求完成后至少间隔 65 秒，含轮询和备选生图；`concurrency` 只是附加保护。未接入队列的历史入口不得并行使用同一密钥。
- 429 最多重试 3 次并尊重 Retry-After；GET 暂时故障最多尝试 5 次。POST 503/超时无任务 ID 时保存 outcome_unknown，禁止盲重投；响应与错误先保存私有检查点再解析。
- 此前已完成的 Agnes 关键帧保留真实来源，不改标 imagegen、不自动重生成；状态未知的视频任务必须对账后续作。

- 视频任务默认推进到生成、回收、剪辑和验收；本轮仅要求文档时不要触发生产。
- 容器没有密钥不代表 Actions 没有密钥。复用获授权的 Environment，不索取/导出密钥。
- 无 dispatch 工具不代表不能触发：核查受限 `on: push`，提交匹配触发条件的公开安全内容后确认真实 run。保留身份、分支和审批保护。
- CI 成功、鉴权成功、生成成功与成片验收是不同状态。当前实际能力看 README 与源码，文档不构成生产实现。
- 生成前确认私有输入、私有结果回收与持久检查点；只读凭证不能回写。不把私有内容放进公共日志、Artifact 或缓存。
- 先续查已有 Provider task ID，后考虑重生成；重试须有限，防止重复费用。
- 修改公开文件时更新 `scripts/public_boundary.py` 中的精确白名单，仅增加已审查的具体路径，禁止放宽为通配。运行边界检查和必要验证，提交后复读远端。
- 发现真实阻塞时报告 run/job/step、脱敏错误、已尝试动作及最小下一步；不得提前把方案或关键帧称作成片。
