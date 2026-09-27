# AI Video Pipeline

公开的通用视频工具与私有数据检查入口。这里使用独立 Git 历史，不包含私有素材、角色图、真实提示词、项目配置、旧 Git 历史或生成结果。

## 新会话从这里开始

先读 [AGENTS.md](AGENTS.md) 与 [完整项目指令（可复制到 ChatGPT 项目设置）](docs/AGENT_SYSTEM_INSTRUCTIONS.md)，再检查实时 workflow、HEAD 和任务检查点。仓库文档不会自动修改 ChatGPT 的项目设置；用户可复制完整指令，新 Agent 应主动读取仓库入口。

**执行原则：本地无密钥不等于 Actions 无密钥；没有 dispatch 工具不等于不能触发 Actions；鉴权成功不等于视频已交付。**

当前文档强化不新增生产工作流，实际迁移状态仍以下表与源码为准。

## 当前能力与迁移边界

**配置连通已验证：私有仓库读取与 Agnes 只读鉴权均已通过 Actions 实测。完整视频生产迁移尚未完成。**

不要以本地容器缺少 `AGNES_API_KEY` 推断 GitHub Actions 未配置密钥；两个运行环境相互独立。当前验证范围、运行证据与后续入口见下文。

| 已提供 | 尚未迁移／尚未验收 |
| --- | --- |
| 通用 Shot / YAML 读取、连续性字段检查 | 原有完整项目验证规则、角色解析、MV 与合成引擎 |
| 本地 FFmpeg 无损拼接 | 图像／视频 Provider、提交、轮询、人工审核与续作 |
| 公开 CI、合成数据测试、真实 FFmpeg 冒烟测试 | 私有成品／检查点存储与恢复 |
| 仅所有者手动触发的私有 YAML/JSON 语法检查，跨仓读取已实测通过 | 完整项目规则与媒体内容验收 |
| Actions 中三个 Secret 可用、Agnes 模型目录鉴权已实测通过 | 图像／视频生成端点、额度、生成结果与成片验收 |

旧私有仓库仍是生产与素材的权威来源，旧代码和工作流保留，不删除、不停用、不改公开。
本项目的 `ai_video.metadata` 是新增的有限语法检查器，**不等价于旧 `validate_project.py --all --metadata-only`**；不会验证全部生产约束、图片/音频内容或生成效果。
不要把普通 CI 绿色当作私有生产流程已经验收。

## 已验证状态与证据（2026-09-27）

以下结论来自实际 Actions job、步骤结果与脱敏日志，不是仅根据设置页面或普通 CI 推断。

| 检查项 | 实测结果 | 范围 |
| --- | --- | --- |
| `ASSETS_REPOSITORY`、`ASSETS_PAT`、`AGNES_API_KEY` | 均为 available | 验证任务能够取得三个 Secret，未输出其值 |
| 跨仓读取 | 私有元数据语法检查成功 | 检查目标仍为 private，读取 YAML/JSON 并运行公开检查器 |
| Agnes 只读鉴权 | 无效凭证 HTTP 401；配置密钥 HTTP 200；`AGNES_AUTH_RESULT: verified` | 仅 GET `/v1/models`，没有提交图像或视频生成 |
| 公开 CI | success | 公开边界、离线测试及合成媒体 FFmpeg 冒烟测试 |

- [配置、跨仓读取与 Agnes 鉴权验证](https://github.com/zjjxwpstcnsm-gif/ai-video-pipeline/actions/runs/36299930846)，验证源码：`f9143a29157f8ec8ebbc18f862cc511ce40d760e`，job：`108565585776`。
- [恢复手动检查后的公开 CI](https://github.com/zjjxwpstcnsm-gif/ai-video-pipeline/actions/runs/36299994027)，源码：`295d3f86c37662594f358eb53975795d6d0e4fe7`。

这是上述运行时点的验证记录，不保证凭证未来不会过期或被撤销，也不证明 Environment 分支／审批保护的全部管理员设置已经审计。
一次性验证曾使用受限的 push 触发；验证后已恢复为仅 `workflow_dispatch`。当前 `private-check.yml` 只执行私有元数据检查，不再包含一次性 Secret 可用性与 Agnes 鉴权探测。因此，重新运行当前检查不会再次验证 Agnes。

## 本地使用

使用独立虚拟环境，避免与旧版同名 `ai_video` Python 包混装：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .
python -m unittest discover -s tests -v
python scripts/concat_video.py /local/output.mp4 /local/part-1.mp4 /local/part-2.mp4
python -I -m ai_video.metadata /local/private-workspace
```

拼接需要系统安装 FFmpeg，输入视频的编码与流参数须兼容；它不是自动转码或完整剪辑器。
Shot/YAML 读取工具兼容首批原接口，仅用于本地可信配置。媒体测试只使用 FFmpeg 生成的合成片段，不使用角色素材，也不调用生成 API。

## GitHub Actions

`Public pipeline CI` 在 main push / PR / 手动触发时验证公开文件边界、安装公开包、运行测试和 FFmpeg 冒烟测试。没有私有 Secret，没有私有素材 checkout，没有公共 Artifact。

`Check private metadata syntax` 只允许仓库所有者从默认分支手动触发及重跑。它先确认数据仓库仍 private，再通过临时只读凭证 sparse checkout YAML/JSON。只运行已安装的公开检查器，不执行私有 Python、Shell、工作流或安装脚本，不打印私有文件名、配置内容和详细异常，不上传 Artifact、不共享缓存、不回写素材仓库。

### 首次配置与已有配置复用

在本仓库 Settings → Environments 创建 `private-assets`，限制仅默认分支部署，并设置可用的审批保护。工作流中的 owner / default-branch 检查已经提交；Environment 保护需要仓库管理员实际设置，不能只写在文档里。

本仓库已有上述成功验证记录；继续使用时先核对运行证据，无需因本地缺少变量而重复索取密钥。新部署时在该 Environment 配置：

| 名称 | 值 |
| --- | --- |
| `ASSETS_REPOSITORY` | 你的私有数据仓库，格式为 `owner/repository` |
| `ASSETS_PAT` | Fine-grained PAT：只选该私有仓库，Contents: Read-only，设置到期日 |
| `AGNES_API_KEY` | Agnes 服务凭证；已用于一次性只读鉴权验证，当前元数据检查不引用它 |

不要把 PAT 发到聊天、写进代码、workflow 输入或公开 Issue。公开仓库的默认 GITHUB_TOKEN 不负责跨私有仓库授权。
配置完成后，Actions → **Check private metadata syntax** → **Run workflow**，选择 main。
没有 Secret 时会明确失败，不会退回匿名访问、不扩大权限，也不会调用真实生成服务。

### Agent 接续与视频执行入口

1. 先读取默认分支工作流和最近的对应 Actions job，分别判断 Secret 可用、跨仓访问、服务鉴权、生成成功和最终成片验收；不要把它们合并成一个“已通／未通”。
2. GitHub Environment secrets 只提供给获授权且显式引用它们的 Actions job，不会自动注入聊天工具或本地容器。不要要求用户把密钥贴到聊天，也不要尝试导出密钥供本地运行。
3. 当前公开仓库只有 `Public pipeline CI` 与 `Check private metadata syntax` 两个工作流，没有“生成视频”入口；配置凭证不会自动增加 Provider、轮询或剪辑能力。
4. 制作任务应继续检查旧私有仓库中现有的生产入口及可执行性；若要迁移到公开 Actions，先接入经过审查的通用生成代码、任务 ID 续查、私有输入与私有结果存储，再实际完成一次生成与成片验收。不能仅修改 README 宣称生产已完成。
5. 后续生成任务通过对应 job 的环境变量引用 `AGNES_API_KEY`。不得把私有提示词、素材路径、临时素材 URL、API 响应正文或产物放进公开日志、公共 Artifact 或公开仓库。

### 没有手动触发工具时如何继续

先核查实际 workflow 的 `on`、分支、`paths`、job 条件和 Environment。当前 `ci.yml` 的 main push 可触发普通 CI；`private-check.yml` 只有 `workflow_dispatch`，不能靠任意 push 触发，也不负责生产。

生产任务授权内，可接入受限 `on: push` 入口：限定受信任默认分支与专用任务文件，保留身份校验及 Environment 审批，使用无敏感含义的请求 ID 在私有环境解析任务。不将私有提示词或素材地址提交到公开触发文件，不让 README 更新自动生成视频。

提交后确认对应 HEAD 的真实 run；没有 run 时排查过滤条件、事件来源、权限和配置。由 `GITHUB_TOKEN` 发起的 push 通常不会递归触发新工作流，不应假定任何提交必定触发。连接器缺少 dispatch 方法时，不能直接据此要求用户改用浏览器或停止。

### 生产闭环与续作要求

1. 生成前核实私有输入读取、服务入口、私有结果回收及持久检查点均可用；缺失的通用实现应在任务范围内接入。
2. 每镜提交后保存 Provider task ID 和输入版本；重跑先续查，使用幂等策略避免重复生成和费用。检查点不能只存 runner 临时磁盘。
3. 失败定位到 run/job/step；区分排队/审批、配置、权限、额度、参数和服务故障。有限重试，不重复空提交。
4. 回收片段、剪辑后做技术与视觉验收，再交付可播放 MP4。实际验证到哪一步就报告到哪一步。
5. 接续记录的通用格式为：任务标识、源码版本、阶段、运行证据、镜头状态、下一动作；包含 Provider task ID、素材位置或实际参数的记录仅存私有环境。

### 生产切换条件

需要继续迁移并审查完整通用引擎，实际通过完整元数据规则与生产回归，再实现私有结果存储和检查点恢复，最后才切换旧生产入口。
只读 ASSETS_PAT 不能回写成品；需要另行配置最小权限的私有存储授权，不能默认扩大它的权限。

## 公开边界

`scripts/public_boundary.py` 使用精确文件白名单，并检测基本的凭证、嵌入媒体、二进制和异常源码。它不是完备的隐私证明：每次新增公开文件仍需审核。不要把 assets、projects、创作配方、输出或 .git 历史加入本库。

所有 Actions 依赖使用已核验的固定 commit。公开标准 runner 的免费计算不代表外部生成 API、存储、较大 runner 或任意工作负载免费；本项目不豁免 GitHub 的使用条款。
