# 本地 LLM 栈说明

这个目录是一套面向 macOS 本机长期使用的本地模型运行栈，当前最终定位如下：

- `main_agent_runtime.py`：当前默认的主代理 v1.1 运行时入口，负责记忆召回、上下文注入、审批写回与 `RunState`
- `llama.cpp`：主运行时
- `LiteLLM`：统一 OpenAI 兼容网关
- `stack_supervisor.py`：守护、健康检查、按需加载、状态页与控制台
- `Ollama`：备用 / 兼容运行时，不是当前主路径

当前主策略已经固定：

- 客户端始终传固定模型名
- `LiteLLM` 只按模型名路由
- 不做“任务类型自动路由”
- 默认常驻最小集合，其他模型按需拉起

## 当前最终架构

当前推荐链路如下：

1. 客户端调用统一网关 `http://127.0.0.1:4000/v1`
2. 主代理记忆增强不再推荐走额外 HTTP 前置网关
3. 真实主代理通过 `~/.codex/AGENTS.md` 调用本地 CLI：
   - `main_agent_runtime.py recall`
   - `main_agent_runtime.py approve`
   - `main_agent_runtime.py runstate`
4. 该 CLI 负责：
   - 主代理唯一记忆入口
   - `before_task_start / after_failure / on_workspace_switch / before_subagent_spawn` 触发召回
   - 构造 `[LONG-TERM CONTEXT]`
   - 对结构化 `state_delta` 执行审批与写回
5. 若上游是本地 `LiteLLM`，`LiteLLM` 收到请求后，先根据模型名调用 `http://127.0.0.1:4060/ensure-service`
6. `supervisor` 负责：
   - 判断目标模型是否已经健康
   - 做内存预算判断
   - 必要时驱逐其他按需模型
   - 启动目标模型
   - 等待健康后再放行
7. 就绪后再由 `LiteLLM` 转发到对应的 `llama.cpp` 服务

备注：

- `4000` 保持为统一模型入口
- `4011` 仅保留为早期 PoC，不再是默认接法

也就是说：

- `main_agent_runtime.py` 是当前默认主代理增强入口
- `4000` 是统一 API 入口
- `4060` 是统一控制台和内部控制入口
- `4011` 仅保留为历史 PoC / 对照审计对象，不再接入默认链路
- 每个模型自己的 `18xxx` 端口只作为下游内部服务，不建议客户端直接调用

## 目录内关键文件

- `litellm.config.yaml`：统一网关配置
- `main-agent-runtime.toml`：主代理运行时 CLI 的 v1.1 配置
- `main_agent_runtime.py`：当前默认主代理运行时 CLI
- `main-agent-gateway.toml`：历史 PoC 网关配置，仅保留审计参考
- `main_agent_gateway.py`：历史 PoC 网关服务，不是默认链路
- `start_main_agent_gateway.sh`：启动主代理入口服务
- `start_litellm.sh`：启动 LiteLLM
- `start_llama_model.sh`：启动单个 `llama-server`
- `stack-supervisor.toml`：supervisor 服务、profile、内存策略配置
- `stack_supervisor.py`：守护主程序，提供状态页、控制接口、按需加载、内存准入
- `start_stack_supervisor.sh`：启动 supervisor
- `create_ollama_models.sh`：从本地 GGUF 注册 Ollama 模型
- `Modelfile.huihui-qwen3.5-27b`：可选的 Ollama 本地导入配方
- `Modelfile.gemma-4-26b`：可选的 Ollama 本地导入配方
- `install_launch_agent.sh`：生成 macOS `launchd` 自启动配置
- `launchd/com.zyh.local-llm-stack.supervisor.plist.template`：LaunchAgent 模板
- `.env.example`：默认环境变量模板

## 当前标准模型名

现在正式使用的新标准模型名如下：

- `embed-m3`
- `qwen2.5-1.5b`
- `qwen3.5-9b`
- `qwen3.5-27b`
- `omnicoder-9b`
- `huihui-27b`
- `gemma-4-26b`

兼容别名如下：

- `uncensored-fallback -> huihui-27b`
- `gemma-fallback -> gemma-4-26b`

短期兼容旧名如下：

- `qwen-fast -> qwen3.5-9b`
- `qwen-fast-vl -> qwen3.5-9b`
- `qwen-deep -> qwen3.5-27b`
- `qwen-deep-vl -> qwen3.5-27b`
- `qwen-extract -> qwen2.5-1.5b`
- `code-fast -> omnicoder-9b`

建议：

- 平时新增配置、客户端、脚本、文档，全部只写新标准名
- 旧名只用于兼容历史配置，不建议继续作为主要入口

## 当前模型与下游端口映射

- `qwen3.5-9b` -> `18081`
- `qwen2.5-1.5b` -> `18082`
- `omnicoder-9b` -> `18083`
- `embed-m3` -> `18084`
- `qwen3.5-27b` -> `18085`
- `huihui-27b` -> `18088`
- `gemma-4-26b` -> `18089`
- `litellm` -> `4000`
- `supervisor` -> `4060`

## 当前推荐启动方式

优先启动 supervisor，不建议再手工一个个拉服务。

1. 如有需要，先复制环境变量模板：
   - `cp .env.example .env`
2. 启动推荐 profile：
   - `./start_stack_supervisor.sh --profile core`
3. 查看运行状态：
   - `python3 ./stack_supervisor.py status`
   - `curl http://127.0.0.1:4060/status`
   - 浏览器打开 `http://127.0.0.1:4060/`

## 当前 profile 语义

现在的 profile 只表示“预热 / 常驻组合”，不表示按需加载规则。

### `core`

默认推荐 profile，只常驻 3 个服务：

- `litellm`
- `embed-m3`
- `qwen2.5-1.5b`

这是日常长期运行的主模式。

### `large-hot`

预热“中型常用模型 + 一个重型模型”：

- `litellm`
- `embed-m3`
- `qwen2.5-1.5b`
- `qwen3.5-9b`
- `omnicoder-9b`
- `qwen3.5-27b`

适合你明确知道接下来要持续使用这组模型时手动切换。

### `large-fallback`

预热备用大模型组合：

- `litellm`
- `embed-m3`
- `qwen2.5-1.5b`
- `huihui-27b`

### `full`

当前不是“所有模型全开”。

当前语义是：

- `litellm`
- `embed-m3`
- `qwen2.5-1.5b`
- `qwen3.5-9b`
- `omnicoder-9b`
- `gemma-4-26b`

注意：

- 不要把多个重型模型全部塞进同一个 profile
- profile 切换属于“手动强制启动路径”，不会像按需加载那样严格走准入决策

### 常驻保护和自动按需的区别

现在 supervisor 会区分 3 种装载方式：

- `预设常驻`
  - 来自 profile 的服务
  - 会显示为“常驻保护”
  - 不参与自动腾退
- `手工常驻`
  - 通过 4060 控制台手工 `启动 / 重启` 的服务
  - 也会显示为“常驻保护”
  - 不参与自动腾退
- `自动按需`
  - 由 LiteLLM 请求触发 `ensure-service` 自动拉起
  - 必要时允许被 supervisor 自动腾退

可以简单理解为：

- 常驻保护 = 你明确要求保活，supervisor 不会为了别的请求偷偷把它关掉
- 自动按需 = 由 supervisor 调度，必要时会被自动换下场

## 4060 控制台的启动、停止、重启命令

以下以推荐的 `core` 为例。

### 启动

```bash
cd /Users/zyh/Desktop/ceshi123/ollamashiyong/local-llm-stack
./start_stack_supervisor.sh --profile core
```

### 停止

```bash
pkill -f "stack_supervisor.py run --profile core"
```

### 重启

```bash
pkill -f "stack_supervisor.py run --profile core"
cd /Users/zyh/Desktop/ceshi123/ollamashiyong/local-llm-stack
./start_stack_supervisor.sh --profile core
```

## 控制台功能

`http://127.0.0.1:4060/` 是 supervisor 自带页面，不依赖额外前端服务。当前支持：

- 查看当前哪些模型正在运行、哪些已停止
- 查看每个服务的端口、健康状态、进程情况、日志和问题说明
- 查看每个服务的预算内存、按需 / 常驻属性、最近自动拉起时间
- 查看当前总预算、软阈值、硬阈值、当前内存快照
- 切换 `core / large-hot / large-fallback / full`
- 对每个服务执行 `启动 / 停止 / 重启`
- 执行 `网关测试`
- 查看最近一次 probe 结果
- 查看统一 API 调用示例

## 当前统一接口

统一入口：

- 主代理增强入口：
  - `http://127.0.0.1:4011/v1`
- `http://127.0.0.1:4000/v1`

主要调用方式：

- OpenAI Responses：
  - `POST /v1/responses`
- OpenAI Chat Completions：
  - `POST /v1/chat/completions`
- Embeddings：
  - `POST /v1/embeddings`

说明：

- `Codex` 走 `/v1/responses`
- 需要 chat 兼容的客户端走 `/v1/chat/completions`
- 向量接口走 `/v1/embeddings`

## Codex / Claude Code 示例配置

下面给的是按当前本机统一网关写的最小示例。

统一参数：

- 统一网关：`http://127.0.0.1:4000/v1`
- 统一密钥：`sk-local-gateway`
- 推荐日常模型：
  - `qwen3.5-9b`
  - `qwen3.5-27b`
  - `omnicoder-9b`

### Codex 示例配置

当前这台机器上的 Codex 仍使用 `~/.codex/config.toml` 的 `model_provider + wire_api = "responses"` 方式。

示例：

```toml
model_provider = "localstack"
model = "qwen3.5-9b"
model_reasoning_effort = "high"
disable_response_storage = true

[model_providers.localstack]
name = "localstack"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"
requires_openai_auth = true
```

鉴权说明：

- Codex 这里需要一个 OpenAI 风格的 key
- 建议让 Codex 最终使用统一网关 key：`sk-local-gateway`
- 如果你本机已有 `~/.codex/auth.json`，确保其中的 `OPENAI_API_KEY` 对应到这个 key

推荐模型切换：

- 日常快速编码：`model = "qwen3.5-9b"`
- 深度编码 / 长上下文：`model = "qwen3.5-27b"`
- 偏代码生成：`model = "omnicoder-9b"`

### Claude Code 示例配置

推荐直接用 LiteLLM 的统一入口方式。

先设置环境变量：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:4000"
export ANTHROPIC_AUTH_TOKEN="sk-local-gateway"
```

然后启动时指定模型名：

```bash
claude --model qwen3.5-9b
```

也可以切到其他统一模型名：

```bash
claude --model qwen3.5-27b
claude --model omnicoder-9b
```

说明：

- `Claude Code` 这里走的是 LiteLLM 统一入口
- 实际下游仍会按模型名路由到 `llama.cpp`
- 如果目标模型未运行，LiteLLM 会先触发 `4060/ensure-service`

### 使用建议

- `Codex` 优先配 `qwen3.5-9b` 或 `qwen3.5-27b`
- `Claude Code` 优先配 `qwen3.5-9b`
- 如果你正在做重型任务，再手动切到 `qwen3.5-27b`
- 如果请求失败，先打开 `http://127.0.0.1:4060/` 看是否是准入拒绝、未就绪或底层模型错误

## 当前按需加载策略

默认常驻服务：

- `litellm`
- `embed-m3`
- `qwen2.5-1.5b`

按需加载服务：

- `qwen3.5-9b`
- `qwen3.5-27b`
- `omnicoder-9b`
- `huihui-27b`
- `gemma-4-26b`

按需加载规则：

1. 客户端发起请求时，LiteLLM 先调用 `4060/ensure-service`
2. 如果目标模型未运行，supervisor 负责拉起
3. 如果预算不足，supervisor 会先尝试驱逐其他“自动按需”模型
4. 如果仍然不满足条件，则直接拒绝启动

补充说明：

- `预设常驻` 和 `手工常驻` 不会进入自动腾退候选池
- 因此，如果你已经手工保活了两个重型模型，再请求第三个重型模型，系统会直接拒绝，而不是偷偷停掉前两个
- 拒绝原因会写入 `last_denied_reason`，4060 页面和 `/status` 都能看到

## 当前内存准入策略

本机总内存约 `128GB`。

当前采用双重守门：

### 静态预算表

- `embed-m3`：`1.5GB`
- `qwen2.5-1.5b`：`3GB`
- `qwen3.5-9b`：`16GB`
- `omnicoder-9b`：`14GB`
- `qwen3.5-27b`：`40GB`
- `huihui-27b`：`35GB`
- `gemma-4-26b`：`36GB`

### 阈值

- 自动准入优先阈值：`88GB`
- 条件准入上限：`96GB`
- 超过 `96GB`：直接拒绝

### 动态条件准入

当预测预算处于 `88GB ~ 96GB` 区间时，supervisor 还会继续检查：

- `memory_pressure`
- 当前可用内存比例
- `swap used`

### 重型模型规则

当前把 `>= 30GB` 预算级别视为重型模型：

- `qwen3.5-27b`
- `huihui-27b`
- `gemma-4-26b`

规则如下：

- 常态建议只热载 1 个重型模型
- 条件允许 2 个重型模型并存
- 不允许自动启动第 3 个重型模型

### 超预算拒绝行为

当自动拉起目标模型时，supervisor 会优先检查以下条件：

- 重型模型并存数是否超限
- 预计总预算是否超过硬上限 `96GB`
- 如果预算落在 `88GB ~ 96GB`，还会继续检查系统实时内存压力

如果检查不通过：

- `4060/ensure-service` 会返回拒绝原因、准入预算报告、已腾退服务列表
- 统一网关 `/v1/responses` / `/v1/chat/completions` 会把这个拒绝转成客户端可读错误
- 4060 控制台和 `/status` 会记录最近一次 `last_denied_reason`

真实验收样例：

- 手工常驻 `huihui-27b` + `gemma-4-26b`
- 此时再请求 `qwen3.5-27b`
- supervisor 返回：
  - 重型模型并存数将达到 `3`
  - 超过上限 `2`
  - 已拒绝本次自动拉起
  - `evicted_services = []`

这说明系统没有偷偷驱逐手工常驻的大模型，而是进入了明确拒绝路径

## 当前实现已验证通过的能力

本轮已经做过真实联调，当前确认通过：

- `core` profile 可正常拉起
- `http://127.0.0.1:4060/status` 正常
- `http://127.0.0.1:4000/v1/models` 正常
- `qwen3.5-9b` 可通过 `/v1/responses` 冷启动自动拉起
- `qwen3.5-9b` 可通过 `/v1/chat/completions` 正常返回
- 旧兼容名 `qwen-fast` 在冷启动场景下也可触发自动拉起
- 真实主代理在 `workspace-write` 模式下可自动先执行 `main_agent_runtime.py recall`
- 多代理链路 `before_subagent_spawn -> 子代理结构化 JSON -> approve --writeback` 已在隔离 workspace 下全绿通过
- `approve --writeback` 已修复写回门禁：
  - 只有整体验收 `ok: true` 时才允许落库
  - 若存在 `approved` 项但整体验收失败，会返回 `writeback_blocked_reason = writeback_requires_ok_true`

## 日常使用约定

建议长期遵守下面几条：

1. 优先使用新标准模型名
   - 例如用 `qwen3.5-9b`
   - 不建议再长期用 `qwen-fast`

2. 平时只开 `core`
   - 这是最稳、最省内存的日常模式

3. 需要大模型时直接发请求
   - 让 supervisor 自动按需拉起
   - 不要先手工把一堆模型都开起来

4. 只有在明确需要连续使用某组模型时，才切 `large-hot` 或 `large-fallback`

5. 不要把 profile 理解成“模型分类”
   - profile 只是“预热组合”

6. 如果调用失败，先看 `4060`
   - 看是模型未就绪
   - 还是预算被拒绝
   - 还是底层服务本身报错

## 新增或删除模型的标准流程

在这套架构里，模型真正可用，不是只看 GGUF 文件是否下载完成，而是要同时接入下面 4 层：

1. 模型文件层
   - 模型文件放在 `/Users/zyh/.lmstudio/models`
2. `llama.cpp` 启动层
   - 在 `start_llama_model.sh` 里配置模型名、文件路径、上下文、并发、额外参数
3. supervisor 管理层
   - 在 `stack-supervisor.toml` 里注册服务，并给出预算、按需属性、profile 归属
4. LiteLLM 网关层
   - 在 `litellm.config.yaml` 里把“模型名 -> 后端端口”映射好

### 新增一个模型的标准流程

1. 先把 GGUF 模型文件放到：
   - `/Users/zyh/.lmstudio/models/...`
2. 修改 `start_llama_model.sh`
   - 在 `case "$MODEL_NAME"` 中新增一个分支
   - 写入：
     - `MODEL_PATH`
     - `CTX_SIZE`
     - `PARALLEL`
     - 如果是视觉模型，再写 `MMPROJ_PATH`
     - 如果需要特殊采样参数，再写 `EXTRA_ARGS`
3. 修改 `stack-supervisor.toml`
   - 新增一个 `[[services]]`
   - 写入：
     - `name`
     - `command`
     - `port`
     - `health_url`
     - `memory_budget_gb`
     - `on_demand`
     - `pinned`
     - 如果是重型模型，再写 `heavy_group`
4. 决定它属于哪些 profile
   - 一般不要直接塞进 `core`
5. 修改 `litellm.config.yaml`
   - 在 `model_list` 中新增一个模型映射
   - 写入：
     - `model_name`
     - `api_base`
     - `api_key`
     - `timeout`
     - 如果要兼容 `/v1/responses`，继续保留 `responses_via_chat_completions: true`
6. 重启 `litellm` 或重启 supervisor
7. 打开 `http://127.0.0.1:4060/`
   - 检查模型是否出现
   - 再执行一次“网关测试”

### 删除模型的标准流程

#### 1. 只是不想让它常驻运行

- 从 `stack-supervisor.toml` 的 profile 中移除它
- 或者直接在 `4060` 页面把它停止
- 这种情况下可以先不删除模型文件

#### 2. 彻底删除

建议按下面顺序删除：

1. 从 `litellm.config.yaml` 删除这个模型的网关映射
2. 从 `stack-supervisor.toml` 删除这个服务定义，以及它在 profile 中的引用
3. 从 `start_llama_model.sh` 删除对应的 `case` 分支
4. 最后再删除 `/Users/zyh/.lmstudio/models` 里的模型文件

### 最简单的接入检查方法

每次新增模型后，检查这 4 个问题：

1. 模型文件是否存在？
2. `start_llama_model.sh` 是否认识这个模型名？
3. `stack-supervisor.toml` 是否已经注册这个服务？
4. `litellm.config.yaml` 是否已经把这个模型名映射到正确端口？

如果这 4 个问题里有一个答案是否，那么这个模型就还不算真正接入完成。

## 可选的 macOS 常驻启动

如果不希望控制台随着终端关闭而停止，可以使用 `launchd`：

1. 生成 LaunchAgent：
   - `./install_launch_agent.sh core`
2. 加载到 `launchd`：
   - `launchctl unload ~/Library/LaunchAgents/com.zyh.local-llm-stack.supervisor.plist 2>/dev/null || true`
   - `launchctl load ~/Library/LaunchAgents/com.zyh.local-llm-stack.supervisor.plist`
   - `launchctl start com.zyh.local-llm-stack.supervisor`
3. 完成后，macOS 会负责自动拉起 supervisor

## 说明与注意事项

- 当前主架构是多实例 `llama-server` + `LiteLLM` + `supervisor`
- `Ollama` 当前不是主调用路径
- qwen 与 gemma 现在都按“统一文本 + 视觉模型”接入，不再拆成单独视觉模型名
- `start_llama_model.sh` 现在会在端口冲突或模型文件缺失时快速失败
- `LiteLLM` 当前已经在真正转发前调用 `ensure-service`
- 如果将来换到非 macOS 环境，`memory_pressure` 相关逻辑需要重评估

## Supervisor 当前能力

- 持续健康检查
- 异常后自动重启与退避
- 自动接管已存在的本地进程
- 启动脚本、`.env`、LiteLLM 配置变更后的自动重载
- `http://127.0.0.1:4060/status` 状态接口
- `POST http://127.0.0.1:4060/control` 控制接口
- `POST http://127.0.0.1:4060/profile` profile 切换接口
- `POST http://127.0.0.1:4060/probe` 模型探测接口
- `POST http://127.0.0.1:4060/ensure-service` 按需拉起接口
- `http://127.0.0.1:4060/` 本地控制台
- 页面内 profile 一键切换
- 页面内服务级 `启动 / 停止 / 重启`
- 页面内服务级 `网关测试`
- 状态层持久化最近一次 probe 的时间、结果、摘要、payload
- 页面内显示预算内存、装载方式、是否常驻保护、最近自动拉起、最近拒绝原因
- 每个服务独立日志文件，目录位于 `local-llm-stack/runtime/logs/`
