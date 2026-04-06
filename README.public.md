# 本地 LLM 网关 / 运行时（公开版）

> 这是一个可公开分享的简化说明文档，对路径和配置做了脱敏处理。

## 项目概览

`ollamashiyong-gateway` 提供一套本地运行的 LLM 网关 / 主代理运行时，用于：

- 统一对外提供 OpenAI 兼容的 HTTP 接口（默认 `http://127.0.0.1:4000/v1`）
- 在本地编排多个模型（llama.cpp / Ollama / 远程提供商等）
- 挂接长期记忆、上下文注入、多代理协调等上游组件

本仓库包含：

- `main_agent_runtime.py`：主代理运行时入口
- `main_agent_gateway.py`：HTTP 网关入口
- `stack_supervisor.py`：进程守护与按需加载
- 一组 `run_*.py` / `*_acceptance.py`：用于回归和协议验证的脚本
- 若干 `Modelfile.*` / `*.toml`：本地模型与路由配置（已脱敏，可作为示例）

## 仓库结构（简要）

- `main_agent_runtime.py`：主代理运行时，实现 recall / approve / runstate 等子命令
- `main_agent_gateway.py`：统一网关入口，暴露 OpenAI 兼容 API
- `stack_supervisor.py`：守护进程，负责模型按需加载与健康检查
- `runtime/`：运行时辅助模块
- `launchd/`：在 macOS 上以 launchd 管理进程的示例 plist 文件
- `Modelfile.*`：示例模型配置（如 Gemma / Qwen 等）

## 安装与运行（示例流程）

> 下面的命令是一个推荐示例，具体参数请根据你的环境和 README 进行调整。

1. 准备 Python 环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt  # 如你需要，可按实际依赖调整
```

2. 配置本地/远程模型

- 根据 `litellm.config.example.yaml` 创建你的本地配置：

```bash
cp litellm.config.example.yaml litellm.config.yaml
# 然后编辑 litellm.config.yaml，填入你的 API Key / 本地模型路由
```

> 注意：实际使用时 **不要** 提交包含真实 API Key 的 `litellm.config.yaml`，本仓库的 `.gitignore` 已忽略该文件。

3. 启动本地网关

```bash
python3 stack_supervisor.py   # 或使用对应的 start_* 脚本
```

4. 从客户端调用

```bash
curl http://127.0.0.1:4000/v1/models
curl http://127.0.0.1:4000/v1/chat/completions -d '{...}'
```

## 脱敏说明

为了适合公开分享，本仓库做了以下脱敏处理：

- 不包含任何真实 `.env` / API Key / 账号信息
- `litellm.config.yaml` 被改为示例文件 `litellm.config.example.yaml`，真实配置文件名被加入 `.gitignore`
- 未包含本地 `.venv` 虚拟环境和任何 >100MB 的二进制依赖
- 去除了指向你本机的绝对路径，只保留相对路径和概念性说明

如果你在本地进一步改造：

- 建议所有包含密钥 / 访问令牌的配置都只保留 `.example` 版本
- 把真实配置加入 `.gitignore`

## 与其他仓库的关系

- 记忆系统与多代理协调逻辑放在单独的仓库（建议名：`ollamashiyong-memory-multiagent`），通过统一网关进行集成。
- 本仓库专注于本地运行栈与 HTTP 网关，不直接包含长期记忆存储或多代理调度逻辑。

