# AI Video Pipeline

公开的通用视频工具与私有数据检查入口。这里使用独立 Git 历史，不包含私有素材、角色图、真实提示词、项目配置、旧 Git 历史或生成结果。

## 当前能力与迁移边界

本次是**首批工具和公开 Actions 上线，不是完整生产迁移完成**。

| 已提供 | 尚未迁移／尚未验收 |
| --- | --- |
| 通用 Shot / YAML 读取、连续性字段检查 | 原有完整项目验证规则、角色解析、MV 与合成引擎 |
| 本地 FFmpeg 无损拼接 | 图像／视频 Provider、提交、轮询、人工审核与续作 |
| 公开 CI、合成数据测试、真实 FFmpeg 冒烟测试 | 私有成品／检查点存储与恢复 |
| 仅所有者手动触发的私有 YAML/JSON 语法检查 | 跨仓库检查实际通过（需要账号侧 Secret） |

旧私有仓库仍是生产与素材的权威来源，旧代码和工作流保留，不删除、不停用、不改公开。
本项目的 `ai_video.metadata` 是新增的有限语法检查器，**不等价于旧 `validate_project.py --all --metadata-only`**；不会验证全部生产约束、图片/音频内容或生成效果。
不要把普通 CI 绿色当作私有生产流程已经验收。

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

### 首次配置

在本仓库 Settings → Environments 创建 `private-assets`，限制仅默认分支部署，并设置可用的审批保护。工作流中的 owner / default-branch 检查已经提交；Environment 保护需要仓库管理员实际设置，不能只写在文档里。

在该 Environment 添加两个 **Secrets**：

| 名称 | 值 |
| --- | --- |
| `ASSETS_REPOSITORY` | 你的私有数据仓库，格式为 `owner/repository` |
| `ASSETS_PAT` | Fine-grained PAT：只选该私有仓库，Contents: Read-only，设置到期日 |

不要把 PAT 发到聊天、写进代码、workflow 输入或公开 Issue。公开仓库的默认 GITHUB_TOKEN 不负责跨私有仓库授权。
配置完成后，Actions → **Check private metadata syntax** → **Run workflow**，选择 main。
没有 Secret 时会明确失败，不会退回匿名访问、不扩大权限，也不会调用真实生成服务。

### 生产切换条件

需要继续迁移并审查完整通用引擎，实际通过完整元数据规则与生产回归，再实现私有结果存储和检查点恢复，最后才切换旧生产入口。
只读 ASSETS_PAT 不能回写成品；需要另行配置最小权限的私有存储授权，不能默认扩大它的权限。

## 公开边界

`scripts/public_boundary.py` 使用精确文件白名单，并检测基本的凭证、嵌入媒体、二进制和异常源码。它不是完备的隐私证明：每次新增公开文件仍需审核。不要把 assets、projects、创作配方、输出或 .git 历史加入本库。

所有 Actions 依赖使用已核验的固定 commit。公开标准 runner 的免费计算不代表外部生成 API、存储、较大 runner 或任意工作负载免费；本项目不豁免 GitHub 的使用条款。
