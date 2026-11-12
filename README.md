# Varin Evals

[Varin](https://github.com/Youzini-afk/Varin) 原生 Agent Harness 的公开评测接入。这里保存适配代码、可复现配置和实测记录；Varin 本身继续负责模型调用、工具、上下文、派发和耐久执行。

主线接入 **MCPMark Verified Filesystem、AppWorld MCP 与 τ-bench text**。Harbor / Terminal-Bench 适配保留为可选入口，需要具备容器能力的执行环境。

接入代码正在真实环境中验证，尚未发布正式评测成绩。

当前 MCP 实测使用带有本地记录的临时 Varin 0.9.44 运行副本，产品侧修复仍由 Varin 仓库处理。正式发布成绩时会明确运行产物、改动与配置；当前接线结果单独保留。

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

`configs/full.example.json` 配齐普通 LLM 辅助位：16 个 Harness 模型槽及标题等通用辅助请求使用同一 DeepSeek、高思考；嵌入使用 `Qwen/Qwen3-Embedding-0.6B`，typed decision 使用 `cloudflare/clef-flash`。功能启用状态沿产品原默认，配置模型本身不强制开启记忆组织、建议或自动选型。`--private-aux-provider` 读取仓库外另一份含 `baseUrl`、`apiKey` 的 JSON，注入 `VARIN_AUX_BASE_URL`、`VARIN_AUX_API_KEY`。决策接口按所选服务的实际协议配置，示例使用 `typesafe-system-one`。

完整配置的主渠道采用 Varin 原生 Responses 适配器（`type: "openai"`），直接连接私有 API；主模型、utility 和普通 LLM 辅助位共用这个渠道。渠道须支持 `/responses`。`configs/model.example.json` 保留原生 Chat Completions 配置，适用于提供该协议的渠道。嵌入、typed decision 以及官方评测侧模拟器和 judge 继续使用各自的原协议。无需额外运行 HTTP 转发服务。

使用完整配置时，把前述配置路径换成 `configs/full.example.json` 并传入 `--private-aux-provider /path/outside/repo/aux-provider.json`。结果 manifest 保存实际模型绑定；隐私检查器的 `--private-provider` 可以同时传入两份私有文件。

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

## 运行 AppWorld MCP

固定官方 AppWorld 源码，在独立 Python 环境安装其 `mcp` extra，再按官方命令安装应用代码和下载数据。当前验证的源码提交为 `42b5bcf3cd334fee33f0c37c02070a9f5807add5`。该提交与新依赖存在实际兼容问题，本轮环境使用 `pydantic==2.12.5`、`fastapi==0.154.0`、`sqlalchemy==2.0.44`；原任务、应用与评分源码保持不变。运行环境的完整依赖列表随私有运行记录留存。

```sh
uv run python -m varin_evals.appworld \
  --source /path/to/appworld --data-root /path/to/appworld-data \
  --appworld-python /path/to/appworld/.venv/bin/python \
  --runtime artifacts/runtime --config configs/full.example.json \
  --private-provider /path/outside/repo/provider.json \
  --private-aux-provider /path/outside/repo/aux-provider.json \
  --split test_normal --tasks all --workers 1 \
  --output artifacts/appworld-normal-001
```

先用 `--split dev --tasks <dev-task-id>` 验证接线，再分别完整运行 `test_normal`、`test_challenge`。每个 worker 拥有独立的官方 environment、API 与 MCP 服务，同一世界不并行运行任务。适配器调用官方 initialize、save、evaluate、close，最后用原 evaluator 汇总 TGC、SGC。工作目录不包含任务答案，Varin 通过 MCP 操作应用，原生文件和 shell 工具不开放。

## 运行 τ-bench text

当前目标是 τ-bench 2.0 的 `text-v1`：六语言、三领域、每组合 50 个冻结任务，共 900 个任务、每题一次。官方命令中的 preset 名为 `text`。源码固定在 `c06b07dd7c4d4ec61ba6ff9bcdf0b15dd7ef36d4`，在其独立 Python 环境安装项目及 `mcp==2.3.0`。跨平台传输应保留官方文件的 LF 字节；原 planner 会检查冻结任务、语言材料和运行规则的摘要。

```sh
PYTHONPATH=/path/to/varin-evals /path/to/tau2-bench/.venv/bin/python \
  -m varin_evals.tau --source /path/to/tau2-bench \
  --runtime artifacts/runtime --config configs/full.example.json \
  --private-provider /path/outside/repo/provider.json \
  --private-aux-provider /path/outside/repo/aux-provider.json \
  --concurrency 1 --output artifacts/tau-text-001 --dry-run
```

去掉 `--dry-run` 开始实际运行；开发接线可另选 `--languages en --domains retail --num-tasks 1`，输出单独记录为部分任务。适配器复用原 planner 的冻结选择、原 orchestrator、用户模拟、环境状态与 reward；进程内使用官方 worker 线程，任务失败不自动重跑。模型请求的网络重试仍由 Varin 或官方模拟器负责；Host 的只读状态查询也会对连接中断重试，次数与退避采用配置值并保存回执。

Varin 通过一个持续的原生会话接收客户消息。领域工具的原始 schema 通过 MCP 披露，调用逐个交给官方 orchestrator 执行，原结果回到尚在执行的原生 Run；下一个客户消息进入同一线程的新 Run。用户模拟器接收本轮完成后的最终回复，中间过程保留在原生历史里。完整用量使用 `native/.../native/session/usage.json` 的会话账本，逐轮导出为累计值，不能重复相加。

会话层在首轮受理后拥有最终导出和 Host 关闭责任，首轮失败或取消也走同一收尾；取消后等待原生终态，再记录实际退出。

本轮是明确的自定义模型配置：客户模拟器、语义通信判分和自然语言断言判分均使用指定的 DeepSeek、高思考。判分提示和规则保留官方实现；manifest 记录模型替换、工具传输方式及统计口径，与官方默认模型配置分开识别。官方 LiteLLM 对未知模型可能记录零费用，实际费用应以原账本和渠道计费为准。

## 后续范围

继续原调研计划中具备运行条件的研究、办公和长期助手评测。不同评测可以在资源允许时并行；各自先验证真实接入、产物与评分，再展开完整选题。新增 [FrontierHarness Eval](https://github.com/frontier-harness-eval/eval)：实际执行前固定源码与任务定义，核对隔离环境和 verifier。容器、VM、账号、数据访问或评分渠道确实缺失的项目，记录具体原因后跳过，不计作已完成的评测。

下一项准备的是 DeepResearch Bench II。固定官方源码 `7c7a9a8cda9dad4e6728f4d6a89bb04541feb222`，当前有效范围为 130 题（官方暂时排除 112、114）。输入保留官方 `prompt`，包括禁止访问的参考来源说明；rubric 保留在评分端。Varin 使用原生 research 工作模式和完整辅助配置，将实际报告写到 `report.md`，缺失产物会明确记录。

```sh
uv sync --extra research
uv run python -m varin_evals.deepresearch \
  --source /path/to/DeepResearch-Bench-II --runtime artifacts/runtime \
  --config configs/full.example.json \
  --private-provider /path/outside/repo/provider.json \
  --private-aux-provider /path/outside/repo/aux-provider.json \
  --tasks all --output artifacts/deepresearch-001 --dry-run
```

本入口使用 DeepSeek 高思考作为自定义文本 judge，复用原 rubric、完整性检查及指标。另有一项显式评分解析修正：原 `_try_clean_and_load` 的引号修复正则会破坏合法 JSON，适配器将其替换为严格 `json.loads`，错误 JSON 仍由原评分重试处理。该项写入 manifest；judge 响应回执单独私有留存。无费用的原生写报告与原评分链验证已完成；真实运行先用单题验证报告生成和判分，再继续其余选题，可与独立评测并行。

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

MCPMark 和 AppWorld 可用 `--continue-from <旧运行目录>` 接续到新的 `--output`。已经记录的结果全部保留，包括正常执行但判分失败的题；确有接入故障需要重跑时，用 `--retry-tasks <逗号分隔的任务 ID>` 明确选择。原运行目录保留，manifest 记录继承与重跑来源。AppWorld 另需新的 `--experiment`，会复制官方已保存的任务状态，让原 evaluator 汇总完整选题。

τ-bench 的 `--continue-from` 将官方 frame、plan 和检查点复制到新输出目录，续跑仍由原 checkpoint 逻辑负责：保留普通终态，仅重新执行 `infrastructure_error` 和未完成任务。旧原生轨迹通过来源路径关联。接续时检查任务版本与模型绑定，官方 planner 继续检查冻结 frame 和配置。

若私有渠道配置声明 `evaluationTransport`，原生执行记录会保留该传输调整说明及实现哈希。渠道地址、认证和请求内容仍由私有配置与原始私有记录管理。实际规范化或人工处理应随结果说明，不能将修改后的运行环境描述为原版。

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
