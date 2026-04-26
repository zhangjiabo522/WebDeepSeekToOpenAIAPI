# WebDeepSeekToOpenAIAPI

将 DeepSeek 网页端免费对话反代为 OpenAI 兼容接口，支持工具调用、多账号管理、前端对话和实时日志。

## 一键部署

```bash
# 克隆仓库
git clone git@github.com:zhangjiabo522/WebDeepSeekToOpenAIAPI.git
cd WebDeepSeekToOpenAIAPI

# 安装依赖并启动
bash deploy.sh
```

或手动部署：

```bash
pip install -r requirements.txt
python proxy.py
```

默认端口 `8000`，可通过环境变量修改：

```bash
PROXY_PORT=8080 python proxy.py
```

启动后访问 http://localhost:8000/admin 登录账号。

## 功能特性

- **OpenAI 兼容 API**：完整端点支持，兼容 OpenAI SDK 及各类客户端
- **多账号管理**：支持手机号、邮箱、cURL 三种方式添加多个账号
- **账号策略**：随机 (random) / 轮询 (round-robin) 切换账号调用
- **Token 自动刷新**：账号过期后自动重新登录
- **系统提示词**：在设置中配置，每次 API 调用自动带上
- **API 密钥认证**：可设置自定义 API Key，保护接口安全
- **前端管理页面**：
  - **账号管理**：登录、添加新账号、退出
  - **日志**：SSE 实时推送服务端日志
  - **设置**：系统提示词、多账号策略、默认模型、API 密钥
  - **对话**：直接在页面与模型流式对话
- **工具调用 (Tool Calling)**：支持 OpenAI 标准的 `tools` / `tool_calls`

## 使用方式

### 1. 启动服务

```bash
python proxy.py
```

### 2. 打开管理后台

浏览器访问 http://localhost:8000/admin

- 选择「手机号登录」或「邮箱登录」输入账号密码
- 支持添加多个账号
- 在「设置」Tab 配置系统提示词、API 密钥等

### 3. 客户端配置

| 配置项 | 值 |
|--------|-----|
| API 地址 | `http://localhost:8000/v1` |
| API Key | 管理页面「设置」中配置（默认 `sk-default`） |
| 模型 | 管理页面刷新后自动获取 |

支持任何 OpenAI 兼容客户端：Chatbox、LobeChat、NextChat、RikkaHub 等。

## API 端点

### 模型列表

```bash
# 获取模型列表
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer sk-default"

curl http://localhost:8000/models \
  -H "Authorization: Bearer sk-default"

# 获取单个模型
curl http://localhost:8000/v1/models/deepseek-chat \
  -H "Authorization: Bearer sk-default"

# 强制刷新模型列表
curl -X POST http://localhost:8000/v1/models/refresh \
  -H "Authorization: Bearer sk-default"
```

### 对话（OpenAI 兼容）

```bash
# 非流式对话
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-default" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'

# 流式对话 (SSE)
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-default" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "讲个笑话"}],
    "stream": true
  }'

# 使用 system 消息
curl http://localhost:8000/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-default" \
  -d '{
    "model": "deepseek-chat-reasoner",
    "messages": [
      {"role": "system", "content": "你是专业的 Python 程序员"},
      {"role": "user", "content": "写一个快速排序"}
    ],
    "stream": true
  }'

# 带工具调用
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-default" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "user", "content": "北京今天天气怎么样"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "获取指定城市天气",
          "parameters": {
            "type": "object",
            "properties": {
              "city": {"type": "string", "description": "城市名"}
            },
            "required": ["city"]
          }
        }
      }
    ],
    "stream": false
  }'
```

### 健康检查

```bash
curl http://localhost:8000/health
# {"status":"ok","configured":true,"accounts":2}
```

### 完整端点列表

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/v1/models` `/models` | GET | 获取模型列表 |
| `/v1/models/{id}` `/models/{id}` | GET | 获取单个模型信息 |
| `/v1/models/refresh` `/models/refresh` | POST | 强制刷新模型列表 |
| `/v1/chat/completions` `/chat/completions` | POST | 对话（OpenAI Chat Completions） |
| `/v1/responses` | POST | 对话（OpenAI Responses 兼容） |
| `/admin` | GET | 管理页面 |
| `/api/login` | POST | 登录 DeepSeek 账号 |
| `/api/accounts` | GET | 获取账号列表 |
| `/api/accounts/logout` | POST | 退出指定账号 |
| `/api/config` | POST | 通过 cURL 配置账号 |
| `/api/settings` | GET/POST | 获取/保存系统设置 |
| `/api/log/stream` | GET | 实时日志 SSE 流 |
| `/api/chat` | POST | 前端对话接口 |
| `/health` | GET | 健康检查 |

### 请求体参数（chat completions）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `messages` | array | 是 | 对话消息数组 |
| `model` | string | 否 | 模型名（默认 `deepseek-chat`） |
| `stream` | boolean | 否 | 是否流式输出（默认 false） |
| `tools` | array | 否 | 工具定义列表（OpenAI 格式） |
| `temperature` | number | 否 | 温度参数 |
| `max_tokens` | number | 否 | 最大输出 token 数 |

### Python SDK 示例

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-default"
)

# 普通对话
response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "你好，介绍一下自己"}],
)
print(response.choices[0].message.content)

# 流式对话
stream = client.chat.completions.create(
    model="deepseek-chat-reasoner",
    messages=[{"role": "user", "content": "解释相对论"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")

# 工具调用
response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "北京今天天气怎么样"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "获取指定城市天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名"}
                },
                "required": ["city"]
            }
        }
    }],
)
print(response.choices[0].message.tool_calls)
```

## 模型说明

模型列表通过 DeepSeek 服务端自动探测，常见模型：

| 模型名 | 功能 | thinking | search |
|--------|------|----------|--------|
| `deepseek-chat` | 快速对话（v4-flash） | ✗ | ✗ |
| `deepseek-chat-reasoner` | 深度思考（v4-flash） | ✓ | ✗ |
| `deepseek-chat-search` | 联网搜索（v4-flash） | ✗ | ✓ |
| `deepseek-chat-reasoner-search` | 思考+联网（v4-flash） | ✓ | ✓ |
| `deepseek-expert` | 专家模式（v4-pro） | ✗ | ✗ |
| `deepseek-expert-reasoner` | 专家深度思考（v4-pro） | ✓ | ✗ |
| `deepseek-expert-search` | 专家联网搜索（v4-pro） | ✗ | ✓ |
| `deepseek-expert-reasoner-search` | 专家思考+联网（v4-pro） | ✓ | ✓ |

## 项目结构

```
.
├── proxy.py            # 主服务（FastAPI + 前端页面）
├── tool_call.py        # 工具调用解析模块
├── pow_native.py       # PoW 挑战求解（Node.js WASM + Python fallback）
├── pow_solver.js       # Node.js WASM 求解器
├── sha3_wasm_bg.wasm   # SHA3 WASM 模块
├── accounts.json       # 多账号配置（自动创建）
├── settings.json       # 系统设置（自动创建）
├── requirements.txt    # Python 依赖
├── deploy.sh           # 一键部署脚本
└── README.md           # 本文件
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_PORT` | `8000` | HTTP 服务端口 |

## 依赖

- Python >= 3.10
- Node.js >= 18（用于 PoW WASM 求解，可选，缺失时自动回退 Python 实现）
- 主要 Python 包：
  - `fastapi >= 0.115.0`
  - `uvicorn[standard] >= 0.34.0`
  - `curl-cffi >= 0.15.0`
  - `python-dotenv >= 1.0.0`

## 常见问题

### Token 过期 / 401 错误

代理会自动尝试重新登录。如果失败，到管理页面重新登录该账号。

### 401 Unauthorized (API Key)

确认请求中 `Authorization` header 的值与设置页中的「API 密钥」一致（默认 `sk-default`）。

### PoW 失败

推荐安装 Node.js 以使用 WASM 快速求解。如果没有 Node.js，会自动回退到纯 Python 实现。

### 工具调用不生效

确认客户端发送了 `tools` 参数，且格式符合 OpenAI 标准。

### 模型列表为空

点击管理页面「刷新模型列表」按钮，或调用 `POST /v1/models/refresh`。

## 管理命令

```bash
# 启动（前台）
python proxy.py

# 启动（后台）
bash deploy.sh --bg

# 停止
bash deploy.sh --stop

# 查看状态
bash deploy.sh --status

# 查看日志（后台模式）
tail -f ~/dsapi.log
```

## License

MIT
