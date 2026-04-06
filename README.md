# 本地 LLM 网关 / 运行时

> 本 README 是对外分享用的公开版本，描述的是“本地推理网关 + 主代理运行时”的整体结构，不包含任何个人信息或密钥。

## 项目角色

这个仓库承载的是“本地推理栈 + 主代理运行时”：

- 对外暴露 **OpenAI 兼容 HTTP 接口**（例如 `http://127.0.0.1:4000/v1`）
- 在本机上编排不同的模型后端（llama.cpp / Ollama / 远程 API 等）
- 提供一个主代理运行入口，用来 **调用记忆系统和多代理系统**，但**不直接实现**记忆存储或多代理协议本身

与之配套、负责长期记忆与多代理协议实现的是 **另一个仓库**：`memory-multiagent`（你已经单独拆出来了）。

## 核心文件

- `main_agent_gateway.py`：
  - 本地 HTTP 网关入口
  - 提供 OpenAI 兼容的 `/v1/chat/completions` 等接口
  - 负责把请求交给底层模型路由 / 主代理运行时

- `main_agent_runtime.py`：
  - 主代理运行时 CLI 入口
  - 负责在本机上串联：网关 ⇆ 记忆系统 ⇆ 多代理系统
  - 提供 `recall` / `approve` / `runstate` 等子命令
  - **注意**：记忆的存储与 schema 定义在 `memory-multiagent` 仓库，这里只是调用方

- `stack_supervisor.py`：
  - 守护 / 进程管理
  - 负责按需拉起模型（llama.cpp / Ollama 等），以及健康检查和状态展示

- `runtime/`：
  - 辅助运行时模块（HTTP 客户端、进程管理等）

- `launchd/`：
  - 在 macOS 上通过 launchd 管理网关 / supervisor 的示例配置

- `Modelfile.*` / `*.toml`：
  - 本地模型的示例配置文件
  - 可以根据自己的硬件与模型选择进行修改

## 脱敏与忽略规则

本仓库已经对公开分享做了基础脱敏处理：

- 不包含任何真实 `.env` / API Key
- 不包含 `.venv/` 虚拟环境和大体积依赖库
- 不包含内部设计文档（例如 `意见.md`、`现有*.md`、`最终实施文档草案-按需加载与内存准入.md`），这些只保留在你的私有工作区
- `.gitignore` 中忽略了：
  - `.venv/`
  - `litellm.config.yaml`（真实配置）
  - 各类缓存和编辑器本地文件

如果你要在此基础上继续改造：

- 所有包含密钥 / Token 的配置（如真实的 `litellm.config.yaml`）只保留本地，不要提交到仓库
- 如需添加新的内部文档，可以照样加到 `.gitignore` 中，保持公共仓库“干净 + 去隐私”。

## 运行示例（示意）

```bash
python3 -m venv .venv
source .venv/bin/activate
# 安装你需要的依赖（根据实际 requirements）

# 准备配置
cp litellm.config.example.yaml litellm.config.yaml
# 编辑 litellm.config.yaml，填入你自己的路由与密钥

# 启动 supervisor / 网关
python3 stack_supervisor.py
# 或使用 start_*.sh 脚本按你现有流程启动
```

## 与 memory-multiagent 仓库的关系

- `gateway` 仓库：
  - 聚焦于“本地 LLM 运行栈 + 主代理运行入口 + HTTP 网关”
  - 负责进程管理、模型路由、与上游客户端的协议对接

- `memory-multiagent` 仓库：
  - 聚焦于记忆系统（facts 存储 / 检索 / 写回）与多代理协议（Planner / Retriever / Verifier / Synthesizer 等）
  - 提供 schema、prompt、验收脚本与运行样例

主代理的运行路径可以理解为：

1. 客户端调用 `gateway` 提供的 HTTP 接口
2. 网关将请求交给 `main_agent_runtime.py`
3. 主代理运行时按协议调用 `memory-multiagent` 提供的能力（记忆召回、多代理规划等）
4. 聚合后的结果再通过网关返回给客户端

这样，两边职责清晰：
- 这个仓库负责“怎么跑起来、怎么接外部请求”；
- 另一个仓库负责“记忆怎么存、代理怎么协作”。
