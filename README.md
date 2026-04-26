# WebDeepSeekToOpenAIAPI

将 DeepSeek 网页端免费对话反代为 OpenAI 兼容接口，支持工具调用、多账号管理、前端对话和实时日志。

## 功能特性

- **OpenAI 兼容 API**：`/v1/chat/completions`、`/v1/models` 等
- **多账号管理**：支持手机号、邮箱、cURL 三种方式添加多个账号
- **账号策略**：随机 (random) / 轮询 (round-robin) 切换账号调用
- **Token 自动刷新**：账号过期后自动重新登录
- **系统提示词**：在设置中配置，每次 API 调用自动带上
- **前端管理页面**：
  - **账号管理**：登录、添加新账号、退出
  - **日志**：SSE 实时推送服务端日志
  - **设置**：系统提示词、多账号策略、默认模型
  - **对话**：直接在页面与模型流式对话
- **工具调用 (Tool Calling)**：支持 OpenAI 标准的 `tools` / `tool_calls`

## 一键部署

```bash
pip install -r requirements.txt
python proxy.py
```

默认端口 `8000`，可通过环境变量修改：

```bash
PROXY_PORT=8080 python proxy.py
```

## 使用方式

### 1. 启动服务

```bash
python proxy.py
```

### 2. 打开管理后台

访问 http://localhost:8000/admin

#### 添加账号（三种方式）

- **手机号登录**：输入区号、手机号、密码
- **邮箱登录**：输入邮箱、密码
- **cURL 粘贴**：从浏览器 F12 复制 completion 请求的 cURL

支持添加多个账号，系统会根据设置的策略自动分配。

#### 日志页面

打开「日志」Tab，通过 SSE 实时查看服务端运行日志。

#### 设置页面

- **系统提示词**：设置后每次 API 调用自动作为 system 消息追加
- **多账号调用策略**：
  - `random` — 每次随机选择一个账号
  - `round-robin` — 按顺序轮询每个账号
- **默认模型**：设置前端对话和 API 的默认模型

#### 对话页面

直接在页面与 DeepSeek 模型对话，支持流式输出和思考过程显示。

### 3. 客户端配置

| 配置项 | 值 |
|--------|-----|
| API 地址 | `http://localhost:8000/v1` |
| API Key | 任意填写 |
| 模型 | 管理页面刷新后自动获取 |

支持任何 OpenAI 兼容客户端：Chatbox、LobeChat、NextChat、RikkaHub 等。

## API 端点

| 端点 | 说明 |
|------|------|
| `GET /health` | 健康检查 |
| `GET /v1/models` | 模型列表 |
| `POST /v1/models/refresh` | 强制刷新模型列表 |
| `POST /v1/chat/completions` | 对话（OpenAI 兼容） |
| `GET /admin` | 管理页面 |
| `GET /api/log/stream` | 实时日志 SSE |
| `POST /api/chat` | 前端对话接口 |

## 模型说明

模型列表通过 DeepSeek 服务端自动探测，常见模型：

| 模型名 | 功能 |
|--------|------|
| `deepseek-default（v4-flash基础）` | 快速对话 |
| `deepseek-reasoner（v4-flash思考模式）` | 深度思考 |
| `deepseek-search（v4-flash联网搜索）` | 联网搜索 |
| `deepseek-reasoner-search（v4-flash思考+联网）` | 思考+联网 |

## 项目结构

```
.
├── proxy.py           # 主服务（FastAPI + 前端页面）
├── tool_call.py       # 工具调用解析模块
├── pow_native.py      # PoW 挑战求解（Node.js WASM + Python fallback）
├── pow_solver.js      # Node.js WASM 求解器
├── sha3_wasm_bg.wasm  # SHA3 WASM 模块
├── accounts.json      # 多账号配置（自动创建）
├── settings.json      # 系统设置（自动创建）
├── requirements.txt   # Python 依赖
└── deploy.sh          # 部署脚本
```

## 依赖

- Python >= 3.10
- Node.js >= 18（用于 PoW WASM 求解，可选）
- 主要 Python 包：
  - `fastapi`
  - `uvicorn[standard]`
  - `curl-cffi`

## 常见问题

### Token 过期 / 401 错误

代理会自动尝试重新登录。如果失败，到管理页面重新登录该账号。

### PoW 失败

推荐安装 Node.js 以使用 WASM 快速求解。如果没有 Node.js，会自动回退到纯 Python 实现。

### 工具调用不生效

确认客户端发送了 `tools` 参数。如果客户端不支持原生工具调用，可以在设置中配置系统提示词引导模型输出工具调用格式。

## License

MIT
