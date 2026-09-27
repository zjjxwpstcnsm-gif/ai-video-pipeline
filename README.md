# AI Video Pipeline

公开的通用视频执行代码。私有素材、真实提示词、项目参数、任务 ID 和成品留在已配置的私有来源；公开源码不包含旧私有 Git 历史或生产媒体。

## 新会话入口

先读 [AGENTS.md](AGENTS.md)、[完整项目指令](docs/AGENT_SYSTEM_INSTRUCTIONS.md)、[运行契约](docs/PRODUCTION_RUNTIME.md) 和实际工作流，再核对实时 HEAD、相关 run 和私有检查点。仓库文件不会自动修改 ChatGPT 项目设置；项目指令需复制到该设置中，新 Agent 则应主动读取这些文件。

**默认 imagegen 生图 → 关键帧审核与私有导入 → Agnes Video → 回收 → 剪辑 → 验收。** Agnes Image 是有证据的备选，不能因 Actions 内没有 imagegen 而自动使用。

## 实际能力与限制

| 能力 | 当前状态 |
|---|---|
| 会话 imagegen 优先 | 默认 `images` 阶段准备私有提示词并标记 awaiting_imagegen；Agent 实际调用原生工具，Actions 不伪造该能力 |
| imagegen 图像导入 | `import_images` 核对来源、传递授权、原图和视频输入 SHA-256，私有保存；视觉审核单独执行 |
| Agnes 图片备选 | 必须明确选择并记录真实失败证据或用户指定；安全拒绝不允许切换 |
| 私有检查点与媒体 | 已实测写入、读回、上传及下载；媒体保存私有 draft Release，小型状态存私有 JSON |
| Agnes 视频提交与续查 | 已接入；此前一次实际 POST 返回 503，未获 video_id，可按已授权的有限 HTTP 恢复策略续作，没有成片验收通过 |
| RPM=1 调度 | 私有 CAS FIFO、租约、跨任务/运行共享冷却；默认所有 Agnes API 请求结束后至少间隔 65 秒 |
| 故障恢复 | 429 有限重试；GET 暂时故障退避；POST 502/503/504 可按授权有限重试并保留未知历史；其他提交不明先对账；响应先持久化；详见运行契约 |
| 剪辑与验收 | 现有本地 FFmpeg 拼接工具；生产执行器尚不自动完成剪辑/视觉验收，不等于完整旧引擎已迁移 |

历史已生成图片保持原模型来源，不能改标 imagegen。未完成/结果未知的生产记录不会因优化代码被自动清空或重投。

## 工作流与秘密变量

- `Public pipeline CI`：main push / PR / 手动验证公开边界、离线测试和合成媒体 FFmpeg 冒烟测试；不读取私有素材或密钥。
- `Check private metadata syntax`：所有者在默认分支手动执行私有 YAML/JSON 语法检查，不调用生成 API。
- `Produce private media`：仅受信任 main 的专用 `requests/current.txt` 变动或手动调用可启动；受所有者校验和 `private-assets` Environment 保护。普通代码/文档提交不会触发生产。

`requests/current.txt` 仅含稳定任务 ID 和可选本次调用 ID，均为 32 位十六进制无敏感串。实际请求在私有默认分支解析。请求阶段、镜头选择和检查点必须先准备好，不能通过反复空提交碰运气。生成成功以实际 Provider 与产物证据为准，CI 绿色或模型目录 200 不能代替。

Environment `private-assets`：

| Secret | 用途与权限 |
|---|---|
| ASSETS_REPOSITORY | 已授权私有来源的 owner/repository |
| ASSETS_PAT | 该私有仓库 Contents 只读 |
| RESULTS_PAT | 仅目标私有仓库 Contents 读写，用于检查点及私有媒体回收 |
| AGNES_API_KEY | Agnes 服务鉴权，仅注入获授权 job |

不导出上述凭证到聊天或当前容器，不改变 Environment 审批，不把私有结果放入公共 Artifact。旧私有工作流或外部客户端若直连同一密钥，不得与新队列并行运行；队列只能约束接入它的调用者。

## 接续和重试

1. 查私有请求与检查点；已完成图片/片段优先回收，已知任务 ID 优先轮询。
2. 默认原生生图；人工或 Agent 完成图片视觉检查后，记录真实来源和批准镜头。未导入的图片不能进入视频提交。
3. 同账户所有接入调用共享持久限流。HTTP 429 至多首次加 3 次重试；GET 暂时错误至多连续 5 次；计数、退避和下一次时间持久保存。
4. POST 502/503/504 无 ID 时，私有 retry_policy 可记录用户授权并启用最多 4 次总尝试；65/130/260 秒退避，保留未知历史与重复可能性。超时、断连或成功响应无 ID 先对账；有 ID 则只续查。未知状态不因租约过期而清除。
5. 本工作流处理显式选中的请求，不是无限后台调度器。GitHub 可能替换 pending concurrency job，需从私有未完成任务续作；不能仅依赖触发事件保存任务。
6. 下载完成不等于可发布：仍需完整解码、镜头/切点审查与可用时的正常速度播放。

运行证据：
- [私有回收、鉴权及单张 Agnes 关键帧成功](https://github.com/zjjxwpstcnsm-gif/ai-video-pipeline/actions/runs/36303164399)
- [视频提交 HTTP 503](https://github.com/zjjxwpstcnsm-gif/ai-video-pipeline/actions/runs/36303296481)
- [历史查询探测 HTTP 404](https://github.com/zjjxwpstcnsm-gif/ai-video-pipeline/actions/runs/36303404046)：该端点未获支持，现已移除自动探测，不以 404 证明任务未创建。

故障恢复代码经离线注入测试验证。用户要求继续成片时，通过受控触发文件运行真实恢复；测试通过不等于提供方已恢复，也不等于成片完成。

## 本地验证

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .
python -m unittest discover -s tests -v
python scripts/public_boundary.py
python scripts/concat_video.py /local/output.mp4 /local/part-1.mp4 /local/part-2.mp4
```

拼接要求 FFmpeg，输入编码/流参数须兼容。`ai_video.metadata` 仅检查私有 YAML/JSON 基础语法，不等于旧完整项目验证器。测试使用合成媒体、假时钟、模拟 CAS 和 Provider 响应，不调用真实生成。

## 公开边界

`scripts/public_boundary.py` 使用精确路径白名单并扫描基本凭证、嵌入媒体和异常源码；新增公开文件须人工审查，不放宽通配。不要把 assets、projects、outputs、实际创作配方或私有历史加入公开仓库。工作流依赖固定到已核验 commit。外部 API 和存储费用不因公开 Actions 免费运行而自动免费。
