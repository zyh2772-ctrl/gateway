# 验收记录：超预算拒绝与容错展示

日期：2026-04-04

目录：

- 项目目录：`/Users/zyh/Desktop/ceshi123/ollamashiyong/local-llm-stack`
- 统一网关：`http://127.0.0.1:4000/v1`
- 控制台 / 状态接口：`http://127.0.0.1:4060/`

本次验收目标：

1. 真实验证“超预算拒绝”链路
2. 真实验证“不是一拒全拒，而是按预算精确放行”的容错行为
3. 验证 4060 控制台和状态接口是否能展示口语化拒绝原因

---

## 一、验收前置状态

先启动 supervisor：

```bash
cd /Users/zyh/Desktop/ceshi123/ollamashiyong/local-llm-stack
./start_stack_supervisor.sh --profile core
```

验收前 `core` 常驻服务：

- `litellm`
- `embed-m3`
- `qwen2.5-1.5b`

验收前状态摘要：

```json
{
  "current_profile": "core",
  "desired_services": [
    "embed-m3",
    "litellm",
    "qwen2.5-1.5b"
  ],
  "running_budget_gb": 5.0
}
```

---

## 二、超预算拒绝场景验收

### 场景设计

先把两个重型模型作为“手工常驻”保活：

- `huihui-27b`
- `gemma-4-26b`

然后再请求第三个重型模型：

- `qwen3.5-27b`

按当前预算表：

- `huihui-27b = 35GB`
- `gemma-4-26b = 36GB`
- `core = 5GB`
- `qwen3.5-27b = 40GB`

预计总预算：

- `35 + 36 + 5 + 40 = 116GB`

同时重型模型并存数将从 `2` 变成 `3`。

### 步骤 1：手工常驻两个重型模型

请求 1：

```bash
curl -s -X POST http://127.0.0.1:4060/control \
  -H 'Content-Type: application/json' \
  -d '{"service":"huihui-27b","action":"start"}'
```

请求体：

```json
{"service":"huihui-27b","action":"start"}
```

结果要点：

- `huihui-27b` 被标记为 `desired_reason = "manual"`
- `eviction_protected = true`

请求 2：

```bash
curl -s -X POST http://127.0.0.1:4060/control \
  -H 'Content-Type: application/json' \
  -d '{"service":"gemma-4-26b","action":"start"}'
```

请求体：

```json
{"service":"gemma-4-26b","action":"start"}
```

结果要点：

- `gemma-4-26b` 被标记为 `desired_reason = "manual"`
- `eviction_protected = true`

两者健康后，状态摘要如下：

```json
{
  "desired_services": [
    "embed-m3",
    "gemma-4-26b",
    "huihui-27b",
    "litellm",
    "qwen2.5-1.5b"
  ],
  "running_budget_gb": 76.0,
  "huihui-27b": {
    "desired_reason": "manual",
    "healthy": true,
    "eviction_protected": true
  },
  "gemma-4-26b": {
    "desired_reason": "manual",
    "healthy": true,
    "eviction_protected": true
  }
}
```

### 步骤 2：直接测试 `ensure-service` 的拒绝返回

请求：

```bash
curl -s -X POST http://127.0.0.1:4060/ensure-service \
  -H 'Content-Type: application/json' \
  -d '{"service":"qwen3.5-27b","timeout_seconds":180}'
```

请求体：

```json
{"service":"qwen3.5-27b","timeout_seconds":180}
```

真实返回体关键字段：

```json
{
  "ok": false,
  "error": "自动拉起 qwen3.5-27b 后，重型模型并存数将达到 3，超过上限 2，已拒绝本次自动拉起；当前受保护服务：embed-m3, gemma-4-26b, huihui-27b, litellm, qwen2.5-1.5b",
  "admission": {
    "service": "qwen3.5-27b",
    "current_budget_gb": 76.0,
    "target_budget_gb": 40.0,
    "projected_budget_gb": 116.0,
    "soft_limit_gb": 88.0,
    "hard_limit_gb": 96.0,
    "current_heavy_count": 2,
    "projected_heavy_count": 3,
    "max_auto_heavy_models": 2
  },
  "evicted_services": []
}
```

验收结论：

- 拒绝链路已真实触发
- `evicted_services = []`
- 说明系统没有偷偷驱逐手工常驻的大模型
- 拒绝原因可直接被人读懂

### 步骤 3：测试统一网关 `/v1/responses` 的客户端可读错误

请求：

```bash
curl -s -X POST http://127.0.0.1:4000/v1/responses \
  -H 'Authorization: Bearer sk-local-gateway' \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5-27b","input":"只回复OK"}'
```

请求体：

```json
{"model":"qwen3.5-27b","input":"只回复OK"}
```

真实返回体：

```json
{
  "error": {
    "message": "模型 qwen3.5-27b 未就绪：自动拉起 qwen3.5-27b 后，重型模型并存数将达到 3，超过上限 2，已拒绝本次自动拉起；当前受保护服务：embed-m3, gemma-4-26b, huihui-27b, litellm, qwen2.5-1.5b",
    "type": "None",
    "param": "None",
    "code": "503"
  }
}
```

验收结论：

- 统一网关已把 supervisor 的拒绝转换成客户端可读错误
- 这条链路对 `Codex`、OpenAI SDK 风格客户端都是可感知的

---

## 三、容错放行场景验收

### 场景设计

在两个手工常驻重型模型仍然保活时，再请求一个中型模型：

- `qwen3.5-9b`

此时预算：

- `76GB + 16GB = 92GB`

结果应该是：

- 不超过硬上限 `96GB`
- 虽然超过软阈值 `88GB`
- 但如果实时内存压力健康，就应允许启动

### 请求

```bash
curl -s -X POST http://127.0.0.1:4000/v1/responses \
  -H 'Authorization: Bearer sk-local-gateway' \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5-9b","input":"只回复PASS"}'
```

请求体：

```json
{"model":"qwen3.5-9b","input":"只回复PASS"}
```

真实返回体关键结果：

```json
{
  "model": "qwen3.5-9b",
  "status": "completed",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "PASS"
        }
      ]
    }
  ]
}
```

同时状态摘要：

```json
{
  "desired_services": [
    "embed-m3",
    "gemma-4-26b",
    "huihui-27b",
    "litellm",
    "qwen2.5-1.5b",
    "qwen3.5-9b"
  ],
  "running_budget_gb": 92.0,
  "memory_snapshot": {
    "free_percent": 68,
    "swap_used_gb": 0.36,
    "ok": true
  }
}
```

验收结论：

- 系统不是“一拒全拒”
- 在软阈值以上、硬阈值以下时，会继续看动态内存健康
- 当前机器内存健康，因此 `qwen3.5-9b` 被成功放行

---

## 四、4060 控制台展示验收

本轮已验证以下展示项已可见：

- 服务表中可看到：
  - `装载方式`
  - `常驻保护`
  - `自动腾退策略`
  - `最近准入拒绝`
- Profile 摘要中可看到：
  - `最近拒绝`
- 顶部说明文案已明确：
  - “常驻保护”不会被自动腾退
  - “自动按需”可能被自动停止以腾出内存

验收结论：

- 控制台已能从运维视角解释“为什么拒绝”
- 不再只是技术字段堆叠

---

## 五、收尾恢复

验收完成后，已恢复到 `core`：

```bash
curl -s -X POST http://127.0.0.1:4060/profile \
  -H 'Content-Type: application/json' \
  -d '{"profile":"core"}'
```

最终状态摘要：

```json
{
  "current_profile": "core",
  "desired_services": [
    "embed-m3",
    "litellm",
    "qwen2.5-1.5b"
  ],
  "running_budget_gb": 5.0
}
```

---

## 六、最终验收结论

本次“超预算拒绝场景”和“容错展示”验收通过。

结论如下：

1. 手工常驻的重型模型不会再被自动驱逐
2. 第三个重型模型请求会进入真实拒绝路径
3. `4060/ensure-service` 和 `4000/v1/responses` 两条链路都能返回可读错误
4. 中型模型在预算允许、内存健康时仍可被正常放行
5. 4060 控制台已经能直观看到装载方式、常驻保护和最近拒绝原因
