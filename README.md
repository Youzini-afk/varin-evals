# Varin Evals

[Varin](https://github.com/Youzini-afk/Varin) 原生 Agent Harness 的公开评测接入。这里保存适配代码、可复现配置和实测记录；Varin 本身继续负责模型调用、工具、上下文、派发和耐久执行。

当前优先接入 **MCPMark Verified Filesystem**，后续是 AppWorld MCP 与 τ-bench text。Harbor / Terminal-Bench 适配保留为可选入口，需要具备容器能力的执行环境。

接入代码正在真实环境中验证，尚未发布正式评测成绩。

## 接入方式

```text
官方任务状态和判分 owner
  评测适配器：准备任务、传递配置、收集结果
    Varin Application Host → Rust 原生执行循环 → 官方任务工具
  原始历史 / 任务状态 / 全量用量 → 脱敏导出与 ATIF
```

任务控制器调用现有 `/api/threads/*`，Varin 负责完整的 Agent loop。已有 `varin thread` CLI 也使用这组接口；这里直接调用 API，以读取固定历史切面、任务家族和全量用量。MCPMark 复用其原状态管理器、任务描述与 verifier；Harbor 入口继承 `BaseInstalledAgent`，保留官方任务镜像。

首版支持 Linux 任务环境。评测控制端可以独立部署；产品运行产物的 OS/架构必须匹配任务环境。浏览器、Computer Use 和外部 Harness 尚未纳入本仓库的接入验证。

## 准备

需要 Python ≥3.12、[uv](https://docs.astral.sh/uv/)、[crane](https://github.com/google/go-containerregistry/tree/main/cmd/crane)。运行 Harbor Docker 环境还需要可用的 Docker daemon 和容器隔离权限。`crane` 仅用于提取官方产品，无需 Docker daemon。

```sh
git clone https://github.com/Youzini-afk/varin-evals.git
cd varin-evals
uv sync

python scripts/prepare_runtime.py \
  --image ghcr.io/youzini-afk/varin:0.9.44 \
  --platform linux/amd64 \
  --output artifacts/varin-runtime.tgz
```

提取脚本先把镜像引用解析成平台 digest，再从同一 digest 提取已安装的应用、Node 和 Bun，记录 `bundle.json`。不会在任务容器里构建 Varin，也不打包桌面程序。`artifacts/` 不进入 Git。

记录中的镜像 digest 和 Varin `sourceRevision` 才是复现依据；复跑时建议直接传入该 digest。不同任务镜像中的 libc/native 依赖仍须实际验证，安装错误保留为错误。

## 无费用接线验证

提取 bundle 到一个新目录，然后执行：

```sh
mkdir -p artifacts/runtime
tar -xzf artifacts/varin-runtime.tgz -C artifacts/runtime
uv run python scripts/smoke.py \
  --runtime artifacts/runtime \
  --output artifacts/smoke-001
```

这会启动真实的 Varin Host 和 Rust 内核，使用本地确定性 OpenAI-compatible 模拟服务，调用真实进程工具写出文件，并核验终态、原始轨迹和 ATIF。模拟服务报告的 token 是测试值，结果属于 integration smoke。此验证不需要 Docker，也不提供模型能力分数。

## 私有渠道与自动重试

`configs/model.example.json` 只声明环境变量名，示例中的模型是 `deepseek-v4.1-flash`、思考强度 `high`。实际地址通过 `VARIN_EVAL_BASE_URL` 注入，Key 通过 `VARIN_EVAL_API_KEY` 注入。也可在仓库外保存权限为 `0600` 的私有 JSON，字段为 `baseUrl`、`apiKey`、`model`、`thinkingLevel`，通过 `--private-provider` 传给评测入口。`baseUrl` 是 OpenAI-compatible API 根路径，包含渠道要求的版本前缀。

仓库忽略运行目录、`.env`、`.mcp_env` 和 `*.local.json`。实际原生配置和原始记录写在独立的私有临时目录；对外导出的 JSON 对地址、域名、Key 和自定义认证头做精确遮蔽。公开生成的结果前，仍要用下面的检查器扫描候选文件；它只输出命中文件名。

```sh
uv run python scripts/check_private_values.py --private-provider /path/outside/repo/provider.json
```

模型重试使用 Varin 原生 `agentSettings.retry`：默认启用，失败后最多重试两次，起始等待 1 秒、指数退避上限 30 秒，遵守服务端 `Retry-After`。网络连接错误、流中断、408/409/425/429 与临时 5xx 按原策略处理。配置可调整次数、等待和规则。取消、撤权及认证失败按原执行语义结束。

每次模型尝试独立留存回执和用量，已完成工具不随之后的模型请求重跑。评测适配器不根据判分自动重试整个任务。无费用 smoke 会注入 503 和工具执行后的流中断，再检查真实文件只追加一次。

## 运行 MCPMark Filesystem

先克隆官方源码并固定提交。适配器只导入原任务与状态管理器，不启动其内置模型循环；所需 Python 基础依赖已包含在当前环境中。首次任务准备会按原 owner 下载官方文件环境。

```sh
git clone https://github.com/eval-sys/mcpmark.git artifacts/mcpmark
git -C artifacts/mcpmark checkout cd45b7f57923b9b3985467f5139927575f83141c

uv run python -m varin_evals.mcpmark \
  --source artifacts/mcpmark \
  --runtime artifacts/runtime \
  --config configs/model.example.json \
  --private-provider /path/outside/repo/provider.json \
  --suite easy --tasks file_property/largest_rename \
  --output artifacts/filesystem-001
```

`--tasks all` 跑所选套件的完整任务；`--suite standard` 使用标准集。每个任务仅有一次 attempt，使用原文件状态备份与重置机制，并保存 verifier 结果及最终文件。

`--mcp-exposure deferred` 可选择 Varin 原有的按需发现与调用模式；默认 `direct` 披露准备好的 MCP 工具目录。所选模式写入 manifest，对比结果时保持连接模式一致。

此赛道通过 Varin 原 Agent profile 将原生工具选择设为空，直接披露官方固定版本 `@modelcontextprotocol/server-filesystem@2025.12.18` 的 MCP 工具，保持 MCP 文件操作条件。任务输入只包含原始描述与工作目录，评分代码和答案保留在评测侧。模型、思考、重试及原生执行仍属于 Varin。完整系统开放工具的实验应另行命名和记录。

## 可选：运行 Terminal-Bench

复制 `configs/openai.example.json` 为 `configs/run.local.json`，按实际模型填写渠道、模型和思考配置。渠道格式沿用 Varin 的 `models.json`；`agentSettings` 沿用其 `agent-settings.json`。密钥使用渠道的环境变量认证。例子中的 `bypass` 是无人值守任务的明确权限配置，OS、网络及文件隔离由评测环境负责。

```sh
export OPENAI_API_KEY='...'
uv run harbor run \
  -d terminal-bench@2.1 \
  -a varin_evals.harbor:VarinAgent \
  --ak config=configs/run.local.json \
  --ak runtime_bundle=artifacts/varin-runtime.tgz \
  -m eval/gpt-5.4
```

`-m` 是 Varin 已登记的 `providerId/modelId`，例如配置中渠道 ID 为 `eval`。不传时使用配置中的选择。认证由 Varin 的原模型渠道执行；适配器只传入配置声明的环境变量及 Harbor 显式提供的 Agent 环境。`--ak directory=/path` 可显式指定任务目录；省略时使用任务镜像的实际工作目录。

首次按 Harbor 的 `--include-task-name` 选择具体任务诊断接入。任务修复、人工介入和每次重跑应留下记录。正式结果使用官方完整数据版本、任务资源和重复运行协议；配置变更后单独记录新运行。

官方资料：[Harbor 自定义 Agent](https://docs.harborframework.com/agents/custom-agents)、[TB2.1 说明](https://www.tbench.ai/news/terminal-bench-2-1)、[榜单规则](https://github.com/harbor-framework/terminal-bench-2-1/blob/main/leaderboard/SUBMIT.md)。截至 2026-11-12，TB2.1 社区提交关闭，由维护者运行的结果才进入官方榜单。本仓库先公开项目实测及可复现证据。

## 结果与执行语义

每个 trial 的 `agent/varin/` 包含：

| 文件 | 内容 |
| --- | --- |
| `run.json` | 原始受理回执、Run 终态、配置/任务哈希、官方产物来源、耗时、导出错误 |
| `states.jsonl` | 实际观察到的状态变化 |
| `conversation.json` | 原 Host 的 JSON v2 对话导出 |
| `histories.json`、`family.json` | 原始分页历史、子任务与分支关系 |
| `usage.json` | 全新 Catalog 的 Host 用量账本，包含子任务、压缩和辅助调用 |
| `branch-usage.json` | 主分支原生用量，便于核对口径 |

Harbor 收集日志后生成 `agent/trajectory.json`。子任务另有 `child-trajectory-*.json`，与原始 `family.json` 一同查看。ATIF 只投影原始历史，工具结果按原 request/call 身份匹配。计费输入使用原生 `completeInputTokens`，缓存不重复相加；缺失计数和未知费用保留为空。

HTTP 受理、Run 完成、任务判分是三个阶段。Run 终态来自原生状态；导出错误会让接入失败并保留已有记录。任务超时、资源及 trial 重试沿 Harbor 配置，适配器不增加任务 attempt。仍在执行或回执不完整的部分可以从原状态与账本核查。

**成功执行后 Host 保留到评分结束。** 部分终端任务依赖 Agent 启动的服务；提前关闭 Host/内核会破坏它们。Harbor 在 verifier 结束后销毁任务环境，承担最后清理。独立 smoke 默认关闭自己启动的 Host。取消请求和实际停止分别记录，不能用断开观察代替停止执行。

公开结果关联两个仓库的 commit、配置、任务 revision、镜像 digest、模型选择、完整成功/失败分母、重跑和人工介入。完整原始结果保留在运行目录；发布前审查工具输出与轨迹中的凭据和个人数据，并说明脱敏范围。开发机器的模型账号和日常 Varin 数据目录不进入 trial。

## 开发验证

```sh
uv run python -m unittest discover -s tests -v
artifacts/runtime/bin/node --test tests/configuration.test.mjs
```

真正的任务执行与轨迹验证走前述 smoke。容器接线再由具备 Docker 隔离能力的机器执行 Harbor；单纯导入适配器不证明官方任务已跑通。

产品执行 bug 回到 Varin 仓库修复；本仓库负责评测接入和结果。后续优先 AppWorld MCP、τ-bench text，再按需要扩展研究、办公和长期助手任务。
