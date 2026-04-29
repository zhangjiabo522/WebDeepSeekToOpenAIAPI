"""
DeepSeek 网页 → API 代理（纯 HTTP 转发，无浏览器依赖）
用法: python proxy.py [--port PORT]  例如: python proxy.py --port 8080
"""
import json, os, shlex, time, uuid, webbrowser, base64, re, secrets, asyncio, random, threading, queue, sys
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from curl_cffi import requests as cffi_requests

# ── 工具调用处理模块 ─────────────────────────────────
from tool_call import (
    build_tool_prompt,
    extract_tool_call,
    get_tool_names,
    convert_messages_for_deepseek,
)

# ── PoW (Proof of Work) Solver — 纯 Python 实现（无 WASM 依赖）────────
from pow_native import DeepSeekPOW

# Initialize PoW solver
pow_solver = DeepSeekPOW()

BASE_DIR = Path(__file__).parent
ACCOUNTS_FILE = BASE_DIR / "accounts.json"
SETTINGS_FILE = BASE_DIR / "settings.json"
STATS_FILE = BASE_DIR / "stats.json"
AUTH_FILE = BASE_DIR / "auth.json"
# 兼容旧版单账号配置
token_json = BASE_DIR / "token.json"
PROXY_PORT = int(os.getenv("PROXY_PORT", "8000"))

# ── 命令行参数 ──
if "--port" in sys.argv:
    try:
        idx = sys.argv.index("--port")
        PROXY_PORT = int(sys.argv[idx + 1])
    except (ValueError, IndexError):
        print("用法: python proxy.py [--port PORT]")
        sys.exit(1)

# ── Web 认证 ─────────────────────────────────────────────
_auth_sessions: dict = {}
_auth_lock = threading.Lock()


def _load_auth() -> dict:
    defaults = {"username": "admin", "password": "admin"}
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text("utf-8"))
            defaults.update(data)
        except Exception:
            pass
    return defaults


def _save_auth(data: dict):
    AUTH_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def _check_session(token: str) -> bool:
    with _auth_lock:
        return token in _auth_sessions


def _create_session() -> str:
    token = secrets.token_hex(32)
    with _auth_lock:
        _auth_sessions[token] = time.time()
    return token


def _clear_session(token: str):
    with _auth_lock:
        _auth_sessions.pop(token, None)

# ── 全局日志队列（线程安全）───────────────────────────
_log_queue: queue.Queue = queue.Queue(maxsize=500)
_log_listeners: List[queue.Queue] = []
_log_lock = threading.Lock()


def log_event(level: str, message: str):
    """推送日志到所有监听器（线程安全）。"""
    ts = time.strftime("%H:%M:%S")
    entry = {"time": ts, "level": level, "message": message}
    # 推送到内存队列（最多保留 500 条）
    try:
        _log_queue.put_nowait(entry)
    except queue.Full:
        try:
            _log_queue.get_nowait()
            _log_queue.put_nowait(entry)
        except queue.Empty:
            pass
    # 推送到 SSE 监听器
    with _log_lock:
        dead = []
        for q in list(_log_listeners):
            try:
                q.put_nowait(entry)
            except queue.Full:
                dead.append(q)
        for q in dead:
            if q in _log_listeners:
                _log_listeners.remove(q)


def log_info(msg: str):
    log_event("info", msg)
    print(f"[INFO] {msg}")


def log_warn(msg: str):
    log_event("warn", msg)
    print(f"[WARN] {msg}")


def log_error(msg: str):
    log_event("error", msg)
    print(f"[ERROR] {msg}")


# ── 数据统计（持久化到 stats.json）─────────────────────
_stats_lock = threading.Lock()


def _load_stats() -> dict:
    defaults = {"total_requests": 0, "total_input_tokens": 0, "total_output_tokens": 0,
                 "total_response_ms": 0, "by_date": {}, "records": []}
    if STATS_FILE.exists():
        try:
            data = json.loads(STATS_FILE.read_text("utf-8"))
            defaults.update(data)
        except Exception:
            pass
    return defaults


def _save_stats(data: dict):
    STATS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def track_api_call(model: str, stream: bool, input_tokens: int, output_tokens: int, elapsed_ms: int):
    today = time.strftime("%Y-%m-%d")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    record = {"time": now, "model": model, "stream": stream,
              "input_tokens": input_tokens, "output_tokens": output_tokens, "elapsed_ms": elapsed_ms}
    with _stats_lock:
        s = _load_stats()
        s["total_requests"] += 1
        s["total_input_tokens"] += input_tokens
        s["total_output_tokens"] += output_tokens
        s["total_response_ms"] += elapsed_ms
        if today not in s["by_date"]:
            s["by_date"][today] = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "response_ms": 0}
        day = s["by_date"][today]
        day["requests"] += 1
        day["input_tokens"] += input_tokens
        day["output_tokens"] += output_tokens
        day["response_ms"] += elapsed_ms
        s["records"].append(record)
        if len(s["records"]) > 1000:
            s["records"] = s["records"][-1000:]
        _save_stats(s)


def get_stats_summary() -> dict:
    today = time.strftime("%Y-%m-%d")
    s = _load_stats()
    total = s.get("total_requests", 0)
    total_ms = s.get("total_response_ms", 0)
    today_data = s.get("by_date", {}).get(today, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "response_ms": 0})
    records = s.get("records", [])[-50:]
    return {
        "today": {
            "requests": today_data["requests"],
            "input_tokens": today_data["input_tokens"],
            "output_tokens": today_data["output_tokens"],
            "avg_response_ms": today_data["response_ms"] // today_data["requests"] if today_data["requests"] else 0,
        },
        "total": {
            "requests": total,
            "input_tokens": s.get("total_input_tokens", 0),
            "output_tokens": s.get("total_output_tokens", 0),
            "avg_response_ms": total_ms // total if total else 0,
        },
        "recent": records[::-1],
    }


# ── cURL 解析 ──────────────────────────────────────────
def parse_curl(curl: str) -> dict:
    try:
        tokens = shlex.split(curl)
    except ValueError:
        tokens = curl.replace("\\\n", " ").split()
    out = {"url": "", "headers": {}, "body": ""}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "curl": i += 1; continue
        if t in ("-H", "--header") and i + 1 < len(tokens):
            line = tokens[i + 1]
            if ":" in line:
                k, _, v = line.partition(":")
                out["headers"][k.strip().lower()] = v.strip()
            i += 2
        elif t in ("--data-raw", "--data", "--data-binary", "-d") and i + 1 < len(tokens):
            out["body"] = tokens[i + 1]; i += 2
        elif t in ("-X", "--request"): i += 2 if i + 1 < len(tokens) else 1
        elif t.startswith("-"): i += 1
        else: out["url"] = t; i += 1
    return out


def build_config(parsed: dict) -> dict:
    h = parsed["headers"]
    token = ""
    ah = h.get("authorization", "")
    if ah.startswith("Bearer "): token = ah[7:]

    session_id = ""
    for src in [parsed.get("url", ""), parsed.get("body", "")]:
        m = re.search(r"[sS]ession[_-]?[iI]d[=:\"]+([a-f0-9-]{36})", src)
        if m: session_id = m.group(1); break
    ref = h.get("referer", "")
    m = re.search(r"/a/chat/s/([a-f0-9-]+)", ref)
    if m: session_id = m.group(1)

    return {
        "token": token,
        "session_id": session_id,
        "headers": h,
        "cookie": h.get("cookie", ""),
        "url": parsed.get("url", ""),
    }


# ── 账号管理 ───────────────────────────────────────────
def _load_accounts() -> List[dict]:
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text("utf-8"))
    # 兼容旧版 token.json
    if token_json.exists():
        old = json.loads(token_json.read_text("utf-8"))
        if old.get("token") and old["token"] != "YOUR_TOKEN_HERE":
            old["active"] = True
            return [old]
    return []


def _save_accounts(accounts: List[dict]):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), "utf-8")


def _get_active_accounts() -> List[dict]:
    return [a for a in _load_accounts() if a.get("active", True)]


def _pick_account(strategy: str = "random") -> Optional[dict]:
    accts = _get_active_accounts()
    if not accts:
        return None
    if len(accts) == 1:
        return accts[0]
    if strategy == "round-robin":
        idx = getattr(_pick_account, "_rr_idx", 0)
        acc = accts[idx % len(accts)]
        _pick_account._rr_idx = idx + 1
        return acc
    return random.choice(accts)


# ── 设置管理 ───────────────────────────────────────────
def _load_settings() -> dict:
    defaults = {
        "system_prompt": "",
        "account_strategy": "random",
        "api_key": "sk-default",
        "default_model": "deepseek-chat",
    }
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text("utf-8"))
            defaults.update(data)
        except Exception:
            pass
    return defaults


def _save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")


# ── FastAPI ────────────────────────────────────────────
app = FastAPI(title="DeepSeek Proxy")


@app.on_event("startup")
async def startup_discover():
    log_info("启动: 探测模型列表...")
    _discover_models()


# ── 管理页面 ─────────────────────────────────────────────
ADMIN = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>DeepSeek Proxy</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;justify-content:center;align-items:flex-start;padding:40px 20px}
.c{background:#1e293b;border-radius:16px;padding:28px;width:800px;max-width:98vw;border:1px solid #334155}
h1{font-size:22px;margin-bottom:16px;display:flex;align-items:center;gap:10px}
.status{display:flex;align-items:center;gap:8px;padding:10px 14px;border-radius:8px;margin-bottom:16px;font-size:13px}
.ok{background:#064e3b;color:#6ee7b7}.no{background:#1e293b;color:#94a3b8}.err{background:#450a0a;color:#fca5a5}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dg{background:#22c55e}.dy{background:#64748b}.dr{background:#ef4444}
.tab-bar{display:flex;gap:0;margin-bottom:16px;border-radius:8px;overflow:hidden;border:1px solid #334155}
.tab{flex:1;padding:10px;text-align:center;font-size:13px;cursor:pointer;background:#0f172a;color:#94a3b8;transition:all .2s;border:none}
.tab.active{background:#2563eb;color:#fff}
.tab:hover:not(.active){background:#1e293b}
.panel{display:none}.panel.active{display:block}
.btn{padding:8px 16px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:500}
.bp{background:#2563eb;color:#fff}.bp:hover{background:#1d4ed8}
.bp:disabled{background:#1e3a5f;color:#64748b;cursor:not-allowed}
.bg{background:#334155;color:#e2e8f0}.bg:hover{background:#475569}
.br{background:#ef4444;color:#fff}.br:hover{background:#dc2626}
input[type=text],input[type=password],input[type=tel],input[type=email],select,textarea{width:100%;padding:10px 12px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;font-size:13px;font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:#3b82f6}
textarea{resize:vertical}
.row{display:flex;gap:10px;margin-bottom:10px}
.row .ac{width:80px;flex-shrink:0}
.row .ph{flex:1}
.card{background:#0f172a;border-radius:10px;padding:14px;margin-bottom:10px;border:1px solid #334155}
.card-title{font-size:13px;font-weight:600;color:#e2e8f0;margin-bottom:8px}
.card-row{display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:12px}
.card-row code{background:#1e293b;padding:2px 6px;border-radius:4px;font-size:11px;color:#7dd3fc;cursor:pointer}
.toast{position:fixed;top:20px;right:20px;padding:10px 18px;border-radius:8px;font-size:13px;z-index:999;display:none}
.ts{display:block;background:#064e3b;color:#6ee7b7}.te{display:block;background:#7f1d1d;color:#fca5a5}
#logBox{height:360px;overflow-y:auto;background:#0b1120;border:1px solid #334155;border-radius:8px;padding:10px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;line-height:1.6}
.log-entry{display:flex;gap:8px;margin-bottom:2px}
.log-time{color:#64748b;flex-shrink:0}.log-info{color:#94a3b8}.log-warn{color:#fbbf24}.log-error{color:#f87171}
.chat-box{height:420px;overflow-y:auto;background:#0b1120;border:1px solid #334155;border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:12px;margin-bottom:10px}
.msg{max-width:85%;padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.msg-user{align-self:flex-end;background:#2563eb;color:#fff;border-bottom-right-radius:4px}
.msg-bot{align-self:flex-start;background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-bottom-left-radius:4px}
.msg-thinking{color:#94a3b8;font-style:italic}
.chat-input{display:flex;gap:8px}
.chat-input textarea{flex:1;height:60px}
.empty{color:#64748b;text-align:center;padding:40px 0;font-size:13px}
hr{border:none;border-top:1px solid #334155;margin:16px 0}
.collapse{cursor:pointer;user-select:none;color:#64748b;font-size:12px;margin-top:8px}
.curl-box{display:none;margin-top:10px}
.api-info{font-size:12px;color:#64748b;margin-top:8px}
.api-info code{background:#1e293b;padding:2px 8px;border-radius:4px;font-size:11px;color:#7dd3fc;cursor:pointer}
.stat-card{background:#0f172a;border:1px solid #334155;border-radius:10px;padding:16px;text-align:center;flex:1;min-width:155px}
.stat-card.total{background:#1e293b}
.stat-num{font-size:26px;font-weight:700;color:#e2e8f0;margin-bottom:4px}
.stat-label{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px}
#statsRecords td{padding:6px 8px;border-bottom:1px solid #1e293b;color:#94a3b8;white-space:nowrap}
#statsRecords tr:hover{background:#1e293b}
</style>
</head>
<body>
<div class="c">
<h1>DeepSeek Proxy <span style="font-size:13px;color:#64748b;font-weight:400">管理后台</span></h1>

<div id="loginBox" style="display:none">
  <div class="card" style="max-width:320px;margin:0 auto">
    <div class="card-title" style="text-align:center;font-size:16px">🔐 登录管理后台</div>
    <div style="margin-top:12px">
      <input type="text" id="loginUser" placeholder="用户名" style="margin-bottom:8px">
      <input type="password" id="loginPass" placeholder="密码" style="margin-bottom:12px">
      <button class="btn bp" onclick="doWebLogin()" id="loginBtn" style="width:100%">登录</button>
      <div id="loginError" style="color:#f87171;font-size:12px;margin-top:8px;text-align:center;display:none"></div>
    </div>
  </div>
</div>

<div id="mainContent" style="display:none">

<div id="statusBar" class="status no"><span class="dot dy"></span><span id="statusText">等待配置</span></div>

<div class="tab-bar">
<button class="tab active" onclick="switchTab('account')" id="tab-account">账号管理</button>
<button class="tab" onclick="switchTab('log')" id="tab-log">日志</button>
<button class="tab" onclick="switchTab('setting')" id="tab-setting">设置</button>
<button class="tab" onclick="switchTab('chat')" id="tab-chat">对话</button>
<button class="tab" onclick="switchTab('stats')" id="tab-stats">统计</button>
</div>

<!-- 账号管理 -->
<div id="panel-account" class="panel active">
  <div id="accountList"></div>
  <button class="btn bg" style="width:100%;margin-top:8px" onclick="toggleAddAccount()">+ 添加新账号</button>

  <div id="addAccountForm" style="display:none;margin-top:14px">
    <div class="tab-bar" style="margin-bottom:10px">
      <button class="tab active" onclick="switchLoginType('phone')" id="lt-phone">手机号</button>
      <button class="tab" onclick="switchLoginType('email')" id="lt-email">邮箱</button>
      <button class="tab" onclick="switchLoginType('curl')" id="lt-curl">cURL</button>
    </div>

    <div id="login-phone">
      <div class="row"><input class="ac" type="tel" id="area_code" value="+86"><input class="ph" type="tel" id="mobile" placeholder="手机号"></div>
      <div class="row"><input type="password" id="pw1" placeholder="密码" style="flex:1"></div>
      <button class="btn bp" id="btn1" onclick="doLogin('phone')" style="width:100%">登录</button>
    </div>

    <div id="login-email" style="display:none">
      <div class="row"><input type="email" id="email" placeholder="邮箱地址" style="flex:1"></div>
      <div class="row"><input type="password" id="pw2" placeholder="密码" style="flex:1"></div>
      <button class="btn bp" id="btn2" onclick="doLogin('email')" style="width:100%">登录</button>
    </div>

    <div id="login-curl" style="display:none">
      <textarea id="curl" placeholder="粘贴 cURL ..." style="width:100%;height:100px;margin-bottom:8px"></textarea>
      <button class="btn bp" id="btn3" onclick="saveCurl()" style="width:100%">保存 cURL</button>
    </div>
  </div>

  <hr>
  <div class="card">
    <div class="card-title">API 配置</div>
    <div class="card-row"><span>API 地址</span><code onclick="cp(this)">http://localhost:""" + str(PROXY_PORT) + """/v1</code></div>
    <div class="card-row"><span>API Key</span><code onclick="cp(this)">任意填写</code></div>
  </div>
  <button class="btn bg" style="width:100%;margin-top:8px" onclick="refreshModels()" id="refreshBtn">刷新模型列表</button>
  <div id="modelsInfo" style="margin-top:8px;font-size:12px;color:#64748b;display:none"></div>
</div>

<!-- 日志 -->
<div id="panel-log" class="panel">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <span style="font-size:13px;color:#94a3b8">实时日志</span>
    <button class="btn bg" onclick="clearLogs()">清空</button>
  </div>
  <div id="logBox"></div>
</div>

<!-- 设置 -->
<div id="panel-setting" class="panel">
  <div class="card">
    <div class="card-title">系统提示词</div>
    <div style="font-size:12px;color:#64748b;margin-bottom:8px">每次调用 API 时自动作为 system 消息带上</div>
    <textarea id="systemPrompt" rows="4" placeholder="输入系统提示词..."></textarea>
  </div>
  <div class="card">
    <div class="card-title">多账号调用策略</div>
    <select id="accountStrategy">
      <option value="random">随机 (random)</option>
      <option value="round-robin">轮询 (round-robin)</option>
    </select>
  </div>
  <div class="card">
    <div class="card-title">默认模型</div>
    <select id="defaultModel"></select>
  </div>
  <div class="card">
    <div class="card-title">API 密钥</div>
    <div style="font-size:12px;color:#64748b;margin-bottom:8px">第三方客户端调用时需要填的 API Key（留空则不校验）</div>
    <input type="text" id="apiKey" placeholder="sk-default">
  </div>
  <div class="card">
    <div class="card-title">修改管理密码</div>
    <input type="text" id="newUsername" placeholder="新用户名（不填则不修改）" style="margin-bottom:8px">
    <input type="password" id="oldPassword" placeholder="旧密码" style="margin-bottom:8px">
    <input type="password" id="newPassword" placeholder="新密码">
    <button class="btn bp" onclick="changePassword()" style="width:100%;margin-top:8px">修改密码</button>
  </div>
  <button class="btn bp" onclick="saveSettings()" style="width:100%">保存设置</button>
</div>

<!-- 对话 -->
<div id="panel-chat" class="panel">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <span style="font-size:13px;color:#94a3b8">与模型对话</span>
    <select id="chatModel" style="width:auto;min-width:200px"></select>
  </div>
  <div id="chatBox" class="chat-box"><div class="empty">开始和 DeepSeek 对话吧</div></div>
  <div class="chat-input">
    <textarea id="chatInput" placeholder="输入消息，Shift+Enter 换行，Enter 发送"></textarea>
    <button class="btn bp" onclick="sendChat()" id="chatSendBtn" style="height:60px;width:80px">发送</button>
  </div>
</div>

<!-- 统计 -->
<div id="panel-stats" class="panel">
  <div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap">
    <div class="stat-card">
      <div class="stat-num" id="stat-today-req">-</div>
      <div class="stat-label">今日请求</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" id="stat-today-in">-</div>
      <div class="stat-label">今日输入 tokens</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" id="stat-today-out">-</div>
      <div class="stat-label">今日输出 tokens</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" id="stat-today-avg">-</div>
      <div class="stat-label">今日平均响应</div>
    </div>
  </div>
  <div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap">
    <div class="stat-card total">
      <div class="stat-num" id="stat-total-req">-</div>
      <div class="stat-label">总请求数</div>
    </div>
    <div class="stat-card total">
      <div class="stat-num" id="stat-total-in">-</div>
      <div class="stat-label">总输入 tokens</div>
    </div>
    <div class="stat-card total">
      <div class="stat-num" id="stat-total-out">-</div>
      <div class="stat-label">总输出 tokens</div>
    </div>
    <div class="stat-card total">
      <div class="stat-num" id="stat-total-avg">-</div>
      <div class="stat-label">总平均响应</div>
    </div>
  </div>
  <div style="margin-bottom:8px;font-size:13px;color:#94a3b8">最近调用记录</div>
  <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead>
        <tr style="border-bottom:1px solid #334155;color:#64748b">
          <th style="padding:6px 8px;text-align:left;white-space:nowrap">时间</th>
          <th style="padding:6px 8px;text-align:left;white-space:nowrap">模型</th>
          <th style="padding:6px 8px;text-align:left;white-space:nowrap">流式</th>
          <th style="padding:6px 8px;text-align:right;white-space:nowrap">输入</th>
          <th style="padding:6px 8px;text-align:right;white-space:nowrap">输出</th>
          <th style="padding:6px 8px;text-align:right;white-space:nowrap">耗时</th>
        </tr>
      </thead>
      <tbody id="statsRecords"></tbody>
    </table>
  </div>
  <div style="text-align:center;margin-top:12px">
    <span style="font-size:11px;color:#64748b">每 5 秒自动刷新</span>
    <span style="margin-left:8px;font-size:11px;color:#64748b">|</span>
    <button class="btn" style="background:none;color:#7dd3fc;font-size:11px;padding:0;margin-left:8px" onclick="clearStats()">清空统计</button>
  </div>
</div>

</div>

</div>
<div id="toast" class="toast"></div>
<script>
const $=id=>document.getElementById(id);
function switchTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  $('tab-'+name).classList.add('active');
  $('panel-'+name).classList.add('active');
  if(name==='log') startLogStream();
  else stopLogStream();
  if(name==='stats'){refreshStats();_statsIv=setInterval(refreshStats,5000);}
  else{if(_statsIv){clearInterval(_statsIv);_statsIv=null;}}
}
let _loginType='phone';
function switchLoginType(type){
  _loginType=type;
  ['phone','email','curl'].forEach(t=>{
    $(`lt-${t}`).classList.toggle('active',t===type);
    $(`login-${t}`).style.display=t===type?'block':'none';
  });
}
function toggleAddAccount(){
  const el=$('addAccountForm');
  el.style.display=el.style.display==='none'?'block':'none';
}
function cp(el){navigator.clipboard.writeText(el.textContent);toast('已复制')}
function toast(m,e){const x=$('toast');x.textContent=m;x.className='toast t'+(e?'e':'s');setTimeout(()=>x.className='toast',2500)}

// 账号状态
async function loadAccounts(){
  try{
    const r=await fetch('/api/accounts');const d=await r.json();
    const list=$('accountList');
    if(!d.accounts||d.accounts.length===0){
      list.innerHTML='<div style="font-size:13px;color:#64748b;padding:10px 0">暂无账号，请添加</div>';
      $('statusBar').className='status no';$('statusText').textContent='等待配置';return;
    }
    const active=d.accounts.filter(a=>a.active).length;
    $('statusBar').className=active>0?'status ok':'status err';
    $('statusText').textContent=`已登录 ${active} 个账号`;
    list.innerHTML=d.accounts.map((a,i)=>`
      <div class="card">
        <div class="card-row"><span style="font-weight:600">${a.account||'未知账号'}</span>
          <div style="display:flex;gap:6px">
            ${a.active?'<span style="color:#6ee7b7;font-size:11px">● 正常</span>':'<span style="color:#fca5a5;font-size:11px">● 失效</span>'}
            <button class="btn br" style="padding:4px 10px;font-size:11px" onclick="logout('${a.account}')">退出</button>
          </div>
        </div>
        <div class="card-row" style="color:#64748b;margin-top:4px"><span>Token</span><code>${a.masked||'***'}</code></div>
        <div class="card-row" style="color:#64748b"><span>Session</span><code>${a.session_id||'N/A'}</code></div>
      </div>
    `).join('');
  }catch(e){toast('加载账号失败: '+e.message,1)}
}
async function logout(account){
  if(!confirm('确定退出该账号?'))return;
  try{
    const r=await fetch('/api/accounts/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account})});
    const d=await r.json();toast(d.ok?'已退出':d.error, d.ok?0:1);loadAccounts();
  }catch(e){toast(e.message,1)}
}
async function doLogin(type){
  let body={},btn;
  if(type==='phone'){
    const m=$('mobile').value.trim(),p=$('pw1').value,a=$('area_code').value.trim();
    if(!m||!p){toast('请输入手机号和密码',1);return}
    body={mobile:m,password:p,area_code:a,login_type:'phone'};btn=$('btn1');
  }else if(type==='email'){
    const e=$('email').value.trim(),p=$('pw2').value;
    if(!e||!p){toast('请输入邮箱和密码',1);return}
    body={email:e,password:p,login_type:'email'};btn=$('btn2');
  }
  btn.disabled=true;btn.textContent='登录中...';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){toast('登录成功');$('addAccountForm').style.display='none';loadAccounts();}
    else{toast(d.error,1)}
  }catch(e){toast(e.message,1)}
  btn.disabled=false;btn.textContent='登录';
}
async function saveCurl(){
  const c=$('curl').value.trim();if(!c){toast('请粘贴 cURL',1);return}
  const b=$('btn3');b.disabled=true;b.textContent='保存中...';
  try{
    const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({curl:c})});
    const d=await r.json();
    if(d.ok){toast('已保存');$('addAccountForm').style.display='none';$('curl').value='';loadAccounts();}
    else{toast(d.error,1)}
  }catch(e){toast(e.message,1)}
  b.disabled=false;b.textContent='保存 cURL';
}
async function refreshModels(){
  const btn=$('refreshBtn'),info=$('modelsInfo');
  btn.disabled=true;btn.textContent='刷新中...';info.style.display='none';
  try{
    const r=await fetch('/v1/models/refresh',{method:'POST'});
    const d=await r.json();
    updateModelSelects(d.data);
    const names=d.data.map(m=>m.id).join(', ');
    info.style.display='block';info.innerHTML='发现 '+d.data.length+' 个模型: '+names;toast('刷新成功');
  }catch(e){info.style.display='block';info.innerHTML='失败: '+e.message;toast('刷新失败',1)}
  btn.disabled=false;btn.textContent='刷新模型列表';
}
async function loadModels(){
  try{
    const r=await fetch('/v1/models');const d=await r.json();
    updateModelSelects(d.data);
  }catch(e){}
}
function updateModelSelects(models){
  if(!models||models.length===0)return;
  const def=$('defaultModel'),chat=$('chatModel');
  const curDef=def.value,curChat=chat.value;
  const opts=models.map(m=>`<option value="${m.id}">${m.id}</option>`).join('');
  def.innerHTML=opts;chat.innerHTML=opts;
  if(models.find(m=>m.id===curDef))def.value=curDef;
  if(models.find(m=>m.id===curChat))chat.value=curChat;
}

// 日志 SSE
let _evtSource=null;
function startLogStream(){
  if(_evtSource)return;
  const box=$('logBox');
  _evtSource=new EventSource('/api/log/stream');
  _evtSource.onmessage=e=>{
    const d=JSON.parse(e.data);
    const div=document.createElement('div');div.className='log-entry';
    div.innerHTML=`<span class="log-time">${d.time}</span><span class="log-${d.level}">[${d.level.toUpperCase()}] ${escapeHtml(d.message)}</span>`;
    box.appendChild(div);box.scrollTop=box.scrollHeight;
  };
  _evtSource.onerror=()=>{};
}
function stopLogStream(){if(_evtSource){_evtSource.close();_evtSource=null;}}
function clearLogs(){$('logBox').innerHTML='';}
function escapeHtml(t){return t.replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}

// 设置
async function loadSettings(){
  try{
    const r=await fetch('/api/settings');const d=await r.json();
    $('systemPrompt').value=d.system_prompt||'';
    $('accountStrategy').value=d.account_strategy||'random';
    $('defaultModel').value=d.default_model||'deepseek-chat';
    $('chatModel').value=d.default_model||'deepseek-chat';
    $('apiKey').value=d.api_key||'sk-default';
  }catch(e){}
}
async function saveSettings(){
  const body={
    system_prompt:$('systemPrompt').value,
    account_strategy:$('accountStrategy').value,
    default_model:$('defaultModel').value,
    api_key:$('apiKey').value,
  };
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();toast(d.ok?'已保存':'保存失败',d.ok?0:1);
  }catch(e){toast(e.message,1)}
}
async function changePassword(){
  const oldPw=$('oldPassword').value,newPw=$('newPassword').value,newUser=$('newUsername').value.trim();
  if(!oldPw||!newPw){toast('请输入旧密码和新密码',1);return}
  try{
    const r=await fetch('/api/auth/change-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_password:oldPw,new_password:newPw,new_username:newUser})});
    const d=await r.json();
    if(d.ok){toast('密码已修改');$('oldPassword').value='';$('newPassword').value='';$('newUsername').value='';}
    else{toast(d.error,1)}
  }catch(e){toast(e.message,1)}
}

// 对话
let _chatHistory=[];
async function sendChat(){
  const input=$('chatInput');const text=input.value.trim();if(!text)return;
  const model=$('chatModel').value;
  const box=$('chatBox');
  if(box.querySelector('.empty'))box.innerHTML='';
  // 用户消息
  const userDiv=document.createElement('div');userDiv.className='msg msg-user';userDiv.textContent=text;box.appendChild(userDiv);box.scrollTop=box.scrollHeight;
  input.value='';
  // 机器人占位
  const botDiv=document.createElement('div');botDiv.className='msg msg-bot';botDiv.innerHTML='<span class="msg-thinking">思考中...</span>';box.appendChild(botDiv);

  const btn=$('chatSendBtn');btn.disabled=true;
  try{
    const messages=_chatHistory.concat([{role:'user',content:text}]);
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages,model,stream:true})});
    const reader=r.body.getReader();const decoder=new TextDecoder();
    let content='',thinking='',done=false;
    botDiv.innerHTML='';
    const thinkDiv=document.createElement('div');thinkDiv.className='msg-thinking';botDiv.appendChild(thinkDiv);
    const contentDiv=document.createElement('div');botDiv.appendChild(contentDiv);

    while(!done){
      const {value,done:d}=await reader.read();if(d){done=true;break;}
      const chunk=decoder.decode(value,{stream:true});
      for(const line of chunk.split('\\n')){
        if(!line.trim().startsWith('data:'))continue;
        const data=line.trim().slice(5).trim();
        if(data==='[DONE]'){done=true;break;}
        try{
          const obj=JSON.parse(data);
          if(obj.error){contentDiv.textContent='错误: '+obj.error.message;done=true;break;}
          const delta=obj.choices?.[0]?.delta||{};
          if(delta.reasoning_content){thinking+=delta.reasoning_content;thinkDiv.textContent='思考: '+thinking;}
          if(delta.content){content+=delta.content;contentDiv.textContent=content;}
          box.scrollTop=box.scrollHeight;
        }catch(e){}
      }
    }
    if(!thinking)thinkDiv.style.display='none';
    _chatHistory.push({role:'user',content:text});
    _chatHistory.push({role:'assistant',content:content||'(无内容)'});
    if(_chatHistory.length>20)_chatHistory=_chatHistory.slice(-20);
  }catch(e){
    botDiv.textContent='错误: '+e.message;
  }
  btn.disabled=false;
}
$('chatInput').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}});

// 统计
function fmtNum(n){if(n>=1e6)return (n/1e6).toFixed(1)+'M';if(n>=1e3)return (n/1e3).toFixed(1)+'K';return String(n);}
function fmtMs(ms){if(ms>=1000)return (ms/1000).toFixed(1)+'s';return ms+'ms';}
async function refreshStats(){
  try{
    const r=await fetch('/api/stats');const d=await r.json();
    $('stat-today-req').textContent=fmtNum(d.today.requests);
    $('stat-today-in').textContent=fmtNum(d.today.input_tokens);
    $('stat-today-out').textContent=fmtNum(d.today.output_tokens);
    $('stat-today-avg').textContent=fmtMs(d.today.avg_response_ms);
    $('stat-total-req').textContent=fmtNum(d.total.requests);
    $('stat-total-in').textContent=fmtNum(d.total.input_tokens);
    $('stat-total-out').textContent=fmtNum(d.total.output_tokens);
    $('stat-total-avg').textContent=fmtMs(d.total.avg_response_ms);
    const tb=$('statsRecords');
    if(!d.recent||d.recent.length===0){tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:#475569;padding:20px">暂无记录</td></tr>';return;}
    tb.innerHTML=d.recent.map(r=>`
      <tr>
        <td>${r.time.slice(5)}</td>
        <td style="color:#7dd3fc">${r.model}</td>
        <td>${r.stream?'流式':'同步'}</td>
        <td style="text-align:right">${fmtNum(r.input_tokens)}</td>
        <td style="text-align:right">${fmtNum(r.output_tokens)}</td>
        <td style="text-align:right;color:#fbbf24">${fmtMs(r.elapsed_ms)}</td>
      </tr>
    `).join('');
  }catch(e){}
}
async function clearStats(){
  if(!confirm('确定清空所有统计数据？'))return;
  try{await fetch('/api/stats/clear',{method:'POST'});refreshStats();toast('已清空');}catch(e){toast(e.message,1);}
}
let _statsIv=null;

// Web 认证
async function doWebLogin(){
  const u=$('loginUser').value.trim(),p=$('loginPass').value;
  if(!u||!p){$('loginError').style.display='block';$('loginError').textContent='请输入用户名和密码';return;}
  $('loginBtn').disabled=true;$('loginBtn').textContent='登录中...';
  try{
    const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
    const d=await r.json();
    if(d.ok){$('loginBox').style.display='none';$('mainContent').style.display='block';initMain();}
    else{$('loginError').style.display='block';$('loginError').textContent=d.error;}
  }catch(e){$('loginError').style.display='block';$('loginError').textContent=e.message;}
  $('loginBtn').disabled=false;$('loginBtn').textContent='登录';
}
async function doWebLogout(){
  await fetch('/api/auth/logout',{method:'POST'});
  $('mainContent').style.display='none';$('loginBox').style.display='block';
}
$('loginPass').addEventListener('keydown',e=>{if(e.key==='Enter')doWebLogin();});

async function initMain(){
  await loadModels();
  loadAccounts();
  loadSettings();
  refreshStats();
}

// 检查登录状态
(async function init(){
  try{
    const r=await fetch('/api/auth/status');const d=await r.json();
    if(d.ok){$('mainContent').style.display='block';initMain();}
    else{$('loginBox').style.display='block';}
  }catch(e){$('loginBox').style.display='block';}
})();
</script>
</body>
</html>"""


from starlette.responses import RedirectResponse


@app.get("/")
async def root():
    return RedirectResponse(url="/admin")


@app.get("/admin", response_class=HTMLResponse)
async def admin():
    from starlette.responses import Response
    return Response(content=ADMIN, media_type="text/html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


# ── 账号 API ─────────────────────────────────────────────
@app.get("/api/accounts")
async def get_accounts():
    accounts = _load_accounts()
    out = []
    for a in accounts:
        t = a.get("token", "")
        out.append({
            "account": a.get("account", "未知账号"),
            "masked": t[:20] + "..." + t[-8:] if len(t) > 30 else "***",
            "session_id": a.get("session_id", "N/A"),
            "active": a.get("active", True),
        })
    return {"accounts": out}


@app.post("/api/accounts/logout")
async def logout_account(data: dict):
    account = data.get("account", "")
    accounts = _load_accounts()
    new_accts = [a for a in accounts if a.get("account") != account]
    _save_accounts(new_accts)
    log_info(f"账号退出: {account}")
    return {"ok": True}


# ── 旧版兼容 & cURL 配置 ────────────────────────────────
@app.post("/api/config")
async def save_config(data: dict):
    curl = data.get("curl", "").strip()
    if not curl:
        raise HTTPException(400, "请提供 cURL")
    parsed = parse_curl(curl)
    cfg = build_config(parsed)
    if not cfg["token"]:
        return {"ok": False, "error": "未从 cURL 提取到 Token，请确认 Authorization header"}
    if not cfg["session_id"]:
        return {"ok": False, "error": "未从 cURL 提取到 Session ID"}

    accounts = _load_accounts()
    # 如果 token 已存在则更新
    found = False
    for a in accounts:
        if a.get("token") == cfg["token"]:
            a["session_id"] = cfg["session_id"]
            a["headers"] = cfg["headers"]
            a["cookie"] = cfg["cookie"]
            a["active"] = True
            found = True
            break
    if not found:
        accounts.append({
            "token": cfg["token"],
            "session_id": cfg["session_id"],
            "headers": cfg["headers"],
            "cookie": cfg["cookie"],
            "account": "cURL账号",
            "login_type": "curl",
            "active": True,
        })
    _save_accounts(accounts)
    t = cfg["token"]
    log_info(f"cURL 配置保存成功: {t[:20]}...{t[-8:]}")
    return {"ok": True, "masked": t[:20] + "..." + t[-8:], "session_id": cfg["session_id"]}


# ── DeepSeek 登录 API ─────────────────────────────────────
@app.post("/api/login")
async def deepseek_login(data: dict):
    login_type = data.get("login_type", "phone")
    password = data.get("password", "").strip()
    if not password:
        raise HTTPException(400, "请提供密码")

    login_payload = {"password": password, "device_id": secrets.token_hex(16), "os": "web"}
    account_label = ""
    email, mobile, area_code = "", "", "+86"

    if login_type == "email":
        email = data.get("email", "").strip()
        if not email:
            raise HTTPException(400, "请提供邮箱")
        login_payload["email"] = email
        login_payload["mobile"] = ""
        login_payload["area_code"] = ""
        account_label = email
    else:
        mobile = data.get("mobile", "").strip()
        area_code = data.get("area_code", "+86").strip()
        if not mobile:
            raise HTTPException(400, "请提供手机号")
        login_payload["mobile"] = mobile
        login_payload["area_code"] = area_code
        login_payload["email"] = ""
        account_label = f"{area_code} {mobile}"

    DS_HEADERS = {
        "content-type": "application/json",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/134.0.0.0 Safari/537.36",
        "x-client-version": "1.0.0-always",
        "x-client-platform": "web",
    }

    try:
        log_info(f"登录开始: {account_label}")
        login_resp = cffi_requests.post(
            "https://chat.deepseek.com/api/v0/users/login",
            json=login_payload,
            headers=DS_HEADERS,
            impersonate="chrome120",
            timeout=30,
        )

        if login_resp is None:
            return {"ok": False, "error": "登录请求失败，无响应"}

        try:
            login_data = login_resp.json()
        except (json.JSONDecodeError, ValueError, AttributeError) as e:
            log_error(f"登录响应解析失败: {e}, body={login_resp.text[:300] if hasattr(login_resp, 'text') else 'N/A'}")
            return {"ok": False, "error": "登录响应解析失败"}

        if not isinstance(login_data, dict):
            log_error(f"登录响应类型异常: {type(login_data)}")
            return {"ok": False, "error": "登录响应格式异常"}

        if login_resp.status_code != 200 or login_data.get("code", 0) != 0:
            err_msg = login_data.get("msg", login_data.get("message", f"HTTP {login_resp.status_code}"))
            log_error(f"登录失败: {err_msg}")
            return {"ok": False, "error": f"登录失败: {err_msg}"}

        # 安全取值，处理 None 值
        biz_data = (login_data.get("data") or {}).get("biz_data") or {}
        user_data = biz_data.get("user") or {}
        token = user_data.get("token", "")
        if not token:
            log_error(f"未获取到 token, biz_data keys: {list(biz_data.keys()) if biz_data else 'None'}")
            return {"ok": False, "error": "登录成功但未获取到 token"}

        log_info(f"Token 获取成功: {token[:20]}...{token[-8:]}")

        auth_headers = {**DS_HEADERS, "authorization": f"Bearer {token}"}
        session_resp = cffi_requests.post(
            "https://chat.deepseek.com/api/v0/chat_session/create",
            json={},
            headers=auth_headers,
            impersonate="chrome120",
            timeout=15,
        )

        session_id = ""
        if session_resp.status_code == 200:
            try:
                session_data = session_resp.json()
                biz = (session_data.get("data") or {}).get("biz_data") or {}
                session_id = biz.get("id", "")
                log_info(f"Session 创建成功: {session_id}")
            except Exception as e:
                log_warn(f"Session 解析失败: {e}")
        else:
            log_warn(f"Session 创建失败: {session_resp.status_code}")

        # 保存到多账号列表
        accounts = _load_accounts()
        # 去重：相同账号更新，不同账号追加
        updated = False
        for a in accounts:
            if a.get("account") == account_label:
                a["token"] = token
                a["session_id"] = session_id
                a["headers"] = {**DS_HEADERS, "authorization": f"Bearer {token}"}
                a["active"] = True
                a["_password"] = password
                a["_email"] = email if login_type == "email" else ""
                a["_mobile"] = mobile if login_type == "phone" else ""
                a["_area_code"] = area_code if login_type == "phone" else "+86"
                updated = True
                break
        if not updated:
            accounts.append({
                "token": token,
                "session_id": session_id,
                "headers": {**DS_HEADERS, "authorization": f"Bearer {token}"},
                "cookie": "",
                "account": account_label,
                "login_type": login_type,
                "active": True,
                "_password": password,
                "_email": email if login_type == "email" else "",
                "_mobile": mobile if login_type == "phone" else "",
                "_area_code": area_code if login_type == "phone" else "+86",
            })
        _save_accounts(accounts)

        masked = token[:20] + "..." + token[-8:]
        return {"ok": True, "masked": masked, "session_id": session_id}

    except Exception as e:
        log_error(f"登录异常: {e}")
        return {"ok": False, "error": str(e)}


# ── Web 认证 API ─────────────────────────────────────────
@app.post("/api/auth/login")
async def auth_login(data: dict, response: Response):
    username = data.get("username", "")
    password = data.get("password", "")
    auth = _load_auth()
    if username == auth["username"] and password == auth["password"]:
        token = _create_session()
        response.set_cookie(key="ds_auth", value=token, httponly=True, max_age=86400)
        log_info(f"Web 登录成功: {username}")
        return {"ok": True}
    return {"ok": False, "error": "用户名或密码错误"}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = request.cookies.get("ds_auth", "")
    _clear_session(token)
    response.delete_cookie("ds_auth")
    return {"ok": True}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    token = request.cookies.get("ds_auth", "")
    return {"ok": _check_session(token)}


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request, data: dict):
    token = request.cookies.get("ds_auth", "")
    if not _check_session(token):
        raise HTTPException(401, "请先登录")
    auth = _load_auth()
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "").strip()
    if not new_pw:
        return {"ok": False, "error": "新密码不能为空"}
    if old_pw != auth["password"]:
        return {"ok": False, "error": "旧密码错误"}
    new_username = data.get("new_username", "").strip()
    if new_username:
        auth["username"] = new_username
    auth["password"] = new_pw
    _save_auth(auth)
    log_info("Web 密码已修改")
    return {"ok": True}


# ── 设置 API ─────────────────────────────────────────────
@app.get("/api/settings")
async def get_settings():
    return _load_settings()


@app.post("/api/settings")
async def save_settings(data: dict):
    s = _load_settings()
    s["system_prompt"] = data.get("system_prompt", s.get("system_prompt", ""))
    s["account_strategy"] = data.get("account_strategy", s.get("account_strategy", "random"))
    s["api_key"] = data.get("api_key", s.get("api_key", "sk-default"))
    s["default_model"] = data.get("default_model", s.get("default_model", "deepseek-chat"))
    _save_settings(s)
    log_info("设置已保存")
    return {"ok": True}


# ── 统计 API ────────────────────────────────────────────
@app.get("/api/stats")
async def api_stats():
    return get_stats_summary()


@app.post("/api/stats/clear")
async def clear_stats():
    with _stats_lock:
        if STATS_FILE.exists():
            STATS_FILE.unlink()
    log_info("统计数据已清空")
    return {"ok": True}


# ── 日志 SSE ─────────────────────────────────────────────
@app.get("/api/log/stream")
async def log_stream(request: Request):
    q: queue.Queue = queue.Queue(maxsize=200)
    with _log_lock:
        _log_listeners.append(q)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.get_event_loop().run_in_executor(None, lambda: q.get(timeout=1))
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _log_lock:
                if q in _log_listeners:
                    _log_listeners.remove(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


# ── 前端聊天 API ─────────────────────────────────────────
@app.post("/api/chat")
async def frontend_chat(request: Request):
    accounts = _get_active_accounts()
    if not accounts:
        raise HTTPException(503, "请先添加并登录账号")

    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", "deepseek-chat")
    stream = body.get("stream", True)

    settings = _load_settings()
    system_prompt = settings.get("system_prompt", "").strip()
    strategy = settings.get("account_strategy", "random")

    # 注入系统提示词
    if system_prompt:
        has_system = any(m.get("role") == "system" for m in messages)
        if has_system:
            # 追加到最后一条 system
            for m in reversed(messages):
                if m.get("role") == "system":
                    m["content"] = m.get("content", "") + "\n\n" + system_prompt
                    break
        else:
            messages = [{"role": "system", "content": system_prompt}] + messages

    # 选账号
    cfg = _pick_account(strategy)
    if not cfg:
        raise HTTPException(503, "没有可用账号")

    # 处理图片/文件上传
    ref_file_ids = []
    try:
        ref_file_ids, cfg = process_vision_images(cfg, messages)
    except Exception:
        pass

    # 模型解析
    model_info = get_models().get(model, get_models().get("deepseek-chat"))
    if not model_info:
        model_info = (False, False, False, 1048576, 393216)
    thinking_enabled, search_enabled, _, _, _ = model_info

    prompt = convert_messages_for_deepseek(messages)
    input_tokens = max(1, len(prompt))
    t0 = time.time()
    result = _do_chat(cfg, prompt, model, thinking_enabled, search_enabled, stream,
                    is_retry=False, has_tools=False, tools=None, ref_file_ids=ref_file_ids)
    return _track_and_return(result, t0, model, stream, input_tokens)


# ── Health ───────────────────────────────────────────────
@app.get("/health")
async def health():
    accts = _get_active_accounts()
    return {"status": "ok" if accts else "waiting", "configured": bool(accts), "accounts": len(accts)}


# ── 模型映射（动态从 DeepSeek 探测）─────────────────
MODEL_CONFIG_URL = "https://chat.deepseek.com/api/v0/client/settings?scope=model"

_models_cache = {}       # model_id → (thinking, search, vision, max_in, max_out)
_models_cache_time = 0
_MODELS_TTL = 3600


def _discover_models() -> dict:
    global _models_cache, _models_cache_time

    cfg = _pick_account()
    if not cfg:
        return None

    token = cfg.get("token", "")
    ua = cfg.get("headers", {}).get("user-agent", "Mozilla/5.0")

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": ua,
        "X-Client-Version": "1.0.0-always",
        "X-Client-Platform": "web",
    }

    try:
        resp = cffi_requests.get(MODEL_CONFIG_URL, headers=headers, timeout=10)
        data = resp.json()
        biz_data = data.get("data", {}).get("biz_data", {})
        settings = biz_data.get("settings", {})
        model_configs = settings.get("model_configs", {}).get("value", [])

        if not model_configs:
            log_warn("model_configs 为空")
            return None

        models = {}
        for mc in model_configs:
            mt = mc.get("model_type")
            if not mt:
                continue

            enabled = mc.get("enabled", False)
            ff = mc.get("file_feature") or {}
            max_in = ff.get("token_limit", 1048576)
            max_out = ff.get("token_limit_with_thinking", 393216)
            has_think = mc.get("think_feature") is not None
            has_search = mc.get("search_feature") is not None
            has_vision = mc.get("image_feature") is not None or mc.get("vision_feature") is not None
            has_file = mc.get("file_feature") is not None and ff.get("file_upload", False)

            speed = "v4-flash" if mt == "default" else "v4-pro"
            base = "chat" if mt == "default" else mt

            name = f"deepseek-{base}"
            models[name] = (False, False, has_vision, max_in, max_out)

            if has_think:
                tname = f"deepseek-{base}-reasoner"
                models[tname] = (True, False, has_vision, max_in, max_out)

            if has_search:
                sname = f"deepseek-{base}-search"
                models[sname] = (False, True, has_vision, max_in, max_out)

            if has_think and has_search:
                cname = f"deepseek-{base}-reasoner-search"
                models[cname] = (True, True, has_vision, max_in, max_out)

            # 视觉专用模型（如果上游有的话）
            if has_vision and mt not in ("default",):
                vname = f"deepseek-{base}-vision"
                models[vname] = (False, False, True, max_in, max_out)
                if has_think:
                    vtname = f"deepseek-{base}-vision-reasoner"
                    models[vtname] = (True, False, True, max_in, max_out)

        if models:
            _models_cache = models
            _models_cache_time = time.time()
            log_info(f"发现 {len(models)} 个模型（含视觉:{sum(1 for v in models.values() if v[2])}）")
            return models

    except Exception as e:
        log_error(f"模型发现失败: {e}")

    return None


def get_models() -> dict:
    global _models_cache, _models_cache_time

    if _models_cache and time.time() - _models_cache_time < _MODELS_TTL:
        return _models_cache

    discovered = _discover_models()
    if discovered:
        return discovered

    log_warn("模型探测失败，模型列表为空")
    return {}


# ── Token 自动刷新 ─────────────────────────────────────────
def relogin(cfg: dict) -> dict | None:
    login_type = cfg.get("login_type", "")
    password = cfg.get("_password", "")
    if not password:
        log_warn(f"无保存密码，无法自动刷新: {cfg.get('account', '')}")
        return None

    login_payload = {"password": password, "device_id": secrets.token_hex(16), "os": "web"}
    account_label = cfg.get("account", "")

    if login_type == "email":
        email = cfg.get("_email", "")
        if not email:
            return None
        login_payload["email"] = email
        login_payload["mobile"] = ""
        login_payload["area_code"] = ""
    elif login_type == "phone":
        mobile = cfg.get("_mobile", "")
        area_code = cfg.get("_area_code", "+86")
        if not mobile:
            return None
        login_payload["mobile"] = mobile
        login_payload["area_code"] = area_code
        login_payload["email"] = ""
    else:
        return None

    DS_HEADERS = {
        "content-type": "application/json",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/134.0.0.0 Safari/537.36",
        "x-client-version": "1.0.0-always",
        "x-client-platform": "web",
    }

    try:
        log_info(f"自动重新登录: {account_label}")
        login_resp = cffi_requests.post(
            "https://chat.deepseek.com/api/v0/users/login",
            json=login_payload,
            headers=DS_HEADERS,
            impersonate="chrome120",
            timeout=30,
        )

        if login_resp is None:
            return None

        try:
            login_data = login_resp.json()
        except (json.JSONDecodeError, ValueError, AttributeError):
            return None

        if not isinstance(login_data, dict):
            return None

        if login_resp.status_code != 200 or login_data.get("code", 0) != 0:
            err_msg = login_data.get("msg", f"HTTP {login_resp.status_code}")
            log_error(f"自动登录失败: {err_msg}")
            return None

        biz_data = (login_data.get("data") or {}).get("biz_data") or {}
        user_data = biz_data.get("user") or {}
        token = user_data.get("token", "")
        if not token:
            log_error("自动登录成功但未获取到 token")
            return None

        log_info(f"新 token: {token[:20]}...{token[-8:]}")

        auth_headers = {**DS_HEADERS, "authorization": f"Bearer {token}"}
        session_resp = cffi_requests.post(
            "https://chat.deepseek.com/api/v0/chat_session/create",
            json={},
            headers=auth_headers,
            impersonate="chrome120",
            timeout=15,
        )
        session_id = ""
        if session_resp.status_code == 200:
            try:
                session_data = session_resp.json()
                biz = (session_data.get("data") or {}).get("biz_data") or {}
                session_id = biz.get("id", "")
                log_info(f"新 session: {session_id}")
            except Exception as e:
                log_warn(f"Session 解析失败: {e}")
        else:
            log_warn(f"Session 创建失败: {session_resp.status_code}")

        new_cfg = {
            "token": token,
            "session_id": session_id,
            "headers": {**DS_HEADERS, "authorization": f"Bearer {token}"},
            "cookie": "",
            "account": account_label,
            "login_type": login_type,
            "active": True,
            "_password": password,
            "_email": cfg.get("_email", ""),
            "_mobile": cfg.get("_mobile", ""),
            "_area_code": cfg.get("_area_code", "+86"),
        }

        # 更新 accounts.json
        accounts = _load_accounts()
        for a in accounts:
            if a.get("account") == account_label:
                a.update(new_cfg)
                break
        _save_accounts(accounts)

        return new_cfg

    except Exception as e:
        log_error(f"自动登录异常: {e}")
        return None


def load_config_with_refresh() -> dict:
    if not ACCOUNTS_FILE.exists() and token_json.exists():
        return json.loads(token_json.read_text("utf-8"))
    accts = _get_active_accounts()
    return accts[0] if accts else {}


# ── OpenAI 兼容 API ──────────────────────────────────────
@app.post("/v1/files")
async def upload_file(request: Request):
    """上传文件到 DeepSeek（兼容 OpenAI Files API）。"""
    if not _get_active_accounts():
        raise HTTPException(503, f"请先登录账号 http://localhost:{PROXY_PORT}/admin")

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(400, "需要 multipart/form-data")

    form = await request.form()
    file_field = form.get("file")
    purpose = form.get("purpose", "assistants")

    if not file_field or not hasattr(file_field, "filename"):
        raise HTTPException(400, "缺少 file 字段")

    file_name = file_field.filename or "file"
    file_data = await file_field.read()

    cfg = _pick_account()
    if not cfg:
        raise HTTPException(503, "没有可用账号")

    fid = upload_file_to_deepseek(file_data, file_name, file_field.content_type or "application/octet-stream")
    if not fid:
        raise HTTPException(502, "文件上传失败")

    return {
        "id": fid, "object": "file", "bytes": len(file_data),
        "created_at": int(time.time()), "filename": file_name, "purpose": purpose,
    }


@app.get("/v1/models")
async def models():
    data = []
    for mid, (think, search, vision, mi, mo) in get_models().items():
        data.append({
            "id": mid, "object": "model", "created": 1704067200,
            "owned_by": "deepseek",
            "max_input_tokens": mi, "max_output_tokens": mo,
            "context_length": mi, "context_window": mi,
            "supported_parameters": ["tools", "tool_choice", "temperature", "max_tokens", "stream"],
        })
    return {"object": "list", "data": data}


@app.get("/v1/models/{model_id}")
async def model_detail(model_id: str):
    info = get_models().get(model_id)
    if not info:
        raise HTTPException(404, f"模型 {model_id} 不存在")
    think, search, vision, mi, mo = info
    return {
        "id": model_id, "object": "model", "created": 1704067200,
        "owned_by": "deepseek",
        "max_input_tokens": mi, "max_output_tokens": mo,
        "context_length": mi, "context_window": mi,
    }


@app.post("/v1/models/refresh")
async def refresh_models():
    global _models_cache_time
    _models_cache_time = 0
    models = get_models()
    data = []
    for mid, (think, search, vision, mi, mo) in models.items():
        data.append({
            "id": mid, "object": "model", "created": 1704067200,
            "owned_by": "deepseek",
            "max_input_tokens": mi, "max_output_tokens": mo,
            "context_length": mi, "context_window": mi,
            "supported_parameters": ["tools", "tool_choice", "temperature", "max_tokens", "stream"],
        })
    return {"object": "list", "data": data}


# ── 端点别名 ───────────────────────────────────────────────
@app.get("/models")
async def models_alias():
    return await models()


@app.get("/models/{model_id}")
async def model_detail_alias(model_id: str):
    return await model_detail(model_id)


@app.post("/models/refresh")
async def refresh_models_alias():
    return await refresh_models()


@app.post("/chat/completions")
async def chat_completions_alias(request: Request):
    return await chat(request)


@app.post("/v1/responses")
async def responses_endpoint(request: Request):
    return await chat(request)


def build_request_headers(cfg: dict, session_id: str) -> dict:
    req_headers = dict(cfg.get("headers", {}))
    req_headers.pop("x-ds-pow-response", None)
    for h in ("host", "content-length", "transfer-encoding", "accept-encoding", "content-type"):
        req_headers.pop(h, None)
    req_headers["content-type"] = "application/json"
    req_headers["origin"] = "https://chat.deepseek.com"
    req_headers["referer"] = f"https://chat.deepseek.com/a/chat/s/{session_id}"
    return req_headers


# ── 文件上传（使用标准 requests，非 curl_cffi）─────────────
import requests as _requests


def upload_file_to_deepseek(file_data: bytes, filename: str, content_type: str = "image/png") -> Optional[str]:
    """上传文件到 DeepSeek（使用标准 requests，获取 Pow 认证）。"""
    cfg = _pick_account()
    if not cfg:
        return None
    session_id = cfg["session_id"]
    pow_response = get_pow_response(target_path="/api/v0/file/upload_file")
    req_headers = build_request_headers(cfg, session_id)
    if pow_response:
        req_headers["x-ds-pow-response"] = pow_response
    req_headers.pop("content-type", None)
    try:
        resp = _requests.post(
            "https://chat.deepseek.com/api/v0/file/upload_file",
            headers=req_headers,
            files={"file": (filename, file_data, content_type)},
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            file_id = (data.get("data", {}).get("biz_data", {}).get("id", "")
                       or data.get("data", {}).get("id", ""))
            if file_id:
                log_info(f"文件上传成功: {filename} -> {file_id}")
                return file_id
        log_warn(f"文件上传失败: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log_error(f"文件上传异常: {e}")
    return None


def fork_file_for_vision(cfg: dict, file_id: str) -> Optional[str]:
    """将已上传文件 fork 到 vision 模型类型。"""
    try:
        headers = build_request_headers(cfg, cfg["session_id"])
        resp = _requests.post(
            "https://chat.deepseek.com/api/v0/file/fork_file_task",
            headers=headers,
            json={"file_id": file_id, "to_model_type": "vision"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            biz = data.get("data", {}).get("biz_data", {})
            forked_id = biz.get("id") or biz.get("file_id", "")
            if forked_id and forked_id != file_id:
                log_info(f"文件 fork 成功: {file_id} -> {forked_id}")
                return forked_id
    except Exception as e:
        log_error(f"文件 fork 异常: {e}")
    return None


def wait_for_file_parsing(cfg: dict, file_ids: list, timeout: int = 30) -> list:
    """等待 DeepSeek 完成文件解析。"""
    if not file_ids:
        return []
    log_info(f"等待文件解析: {file_ids}")
    start = time.time()
    while time.time() - start < timeout:
        try:
            headers = build_request_headers(cfg, cfg["session_id"])
            resp = _requests.get(
                "https://chat.deepseek.com/api/v0/file/fetch_files",
                headers=headers,
                params={"file_ids": file_ids},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                biz = data.get("data", {}).get("biz_data", {})
                files = biz.get("files", [])
                log_info(f"文件状态: {[(f.get('id',''), f.get('status','')) for f in files]}")
                parsed = [f.get("id") or f.get("file_id", "") for f in files
                          if str(f.get("status", "")).upper() in ("SUCCESS", "COMPLETED")]
                pending = [f for f in files
                           if str(f.get("status", "")).upper() in ("PENDING", "PARSING", "UPLOADING", "QUEUED")]
                if not pending:
                    if parsed:
                        log_info(f"文件解析完成: {parsed}")
                        return parsed
                    return []
                if parsed and time.time() - start > 5:
                    log_info(f"部分文件就绪: {parsed}")
                    return parsed
            else:
                log_warn(f"文件状态查询: HTTP {resp.status_code}")
        except Exception as e:
            log_warn(f"文件状态查询异常: {e}")
        time.sleep(2)
    log_warn(f"文件解析超时: {file_ids}")
    return []


def extract_images_from_messages(messages: list) -> list:
    """从 OpenAI 格式消息中提取图片。"""
    images = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        img = _parse_image_url(url)
                        if img:
                            images.append(img)
    return images


def _parse_image_url(url_or_data: str) -> Optional[dict]:
    """解析图片 URL 或 base64 数据，返回 {data, content_type, filename}。"""
    if not url_or_data:
        return None
    s = url_or_data.strip()
    if s.startswith("data:"):
        header, encoded = s.split(",", 1)
        ct = "image/png"
        for part in header.split(";")[0].split(":")[1:]:
            ct = part
        try:
            data = base64.b64decode(encoded)
            ext = ct.split("/")[-1] if "/" in ct else "png"
            return {"data": data, "content_type": ct, "filename": f"image.{ext}"}
        except Exception:
            return None
    if s.startswith("http://") or s.startswith("https://"):
        try:
            resp = cffi_requests.get(s, timeout=30, impersonate="chrome120")
            if resp.status_code == 200:
                ct = resp.headers.get("content-type", "image/png")
                ext = ct.split("/")[-1] if "/" in ct else "png"
                return {"data": resp.content, "content_type": ct, "filename": f"image.{ext}"}
        except Exception:
            pass
    return None


def process_vision_images(cfg: dict, messages: list) -> (list, dict):
    """处理视觉消息：提取图片 → 上传 → fork → 等待解析 → 返回 ref_file_ids 和新 cfg。"""
    images = extract_images_from_messages(messages)
    if not images:
        return [], cfg

    ref_file_ids = []
    log_info(f"视觉处理: 发现 {len(images)} 张图片")
    for img in images:
        orig_fid = upload_file_to_deepseek(img["data"], img["filename"], img["content_type"])
        if orig_fid:
            forked_fid = fork_file_for_vision(cfg, orig_fid)
            if forked_fid:
                ref_file_ids.append(forked_fid)

    if ref_file_ids:
        log_info(f"视觉 ref_file_ids: {ref_file_ids}")
        # 等待解析完成
        ref_file_ids = wait_for_file_parsing(cfg, ref_file_ids)
        # 为视觉请求创建新 session
        token = cfg.get("token", "")
        if token:
            try:
                auth_h = {**cfg.get("headers", {}), "authorization": f"Bearer {token}"}
                sess_resp = cffi_requests.post(
                    "https://chat.deepseek.com/api/v0/chat_session/create",
                    json={}, headers=auth_h, impersonate="chrome120", timeout=15)
                if sess_resp.status_code == 200:
                    biz = sess_resp.json().get("data", {}).get("biz_data", {})
                    new_sid = biz.get("chat_session", {}).get("id", "") or biz.get("id", "")
                    if new_sid:
                        cfg = dict(cfg)
                        cfg["session_id"] = new_sid
                        log_info(f"视觉新 session: {new_sid}")
            except Exception as e:
                log_error(f"视觉 session 创建失败: {e}")
    return ref_file_ids, cfg


def get_pow_response(target_path: str = "/api/v0/chat/completion") -> str | None:
    try:
        cfg = _pick_account()
        if not cfg:
            return None
        headers = build_request_headers(cfg, cfg["session_id"])
        resp = cffi_requests.post(
            "https://chat.deepseek.com/api/v0/chat/create_pow_challenge",
            headers=headers,
            json={"target_path": target_path},
            impersonate="chrome120",
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            challenge = data.get("data", {}).get("biz_data", {}).get("challenge", {})
            if challenge:
                pow_response = pow_solver.solve_challenge(challenge)
                log_info(f"PoW 已解决: {pow_response[:40]}...")
                return pow_response
            else:
                log_warn(f"PoW 无 challenge: {data}")
        else:
            log_warn(f"PoW 请求失败 {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log_error(f"PoW 错误: {e}")
    return None


@app.post("/v1/chat/completions")
async def chat(request: Request):
    if not _get_active_accounts():
        raise HTTPException(503, detail="请先访问 http://localhost:{}/admin 登录账号".format(PROXY_PORT))

    # API Key 校验
    settings = _load_settings()
    api_key = settings.get("api_key", "").strip()
    if api_key:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            provided_key = auth_header[7:].strip()
        else:
            provided_key = auth_header.strip()
        if provided_key != api_key:
            raise HTTPException(401, detail="Invalid API key")

    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", "deepseek-chat")
    stream = body.get("stream", False)
    tools = body.get("tools", None)

    log_info(f"API 请求: model={model}, stream={stream}, tools={'yes' if tools else 'no'}, msgs={len(messages)}")

    strategy = settings.get("account_strategy", "random")
    system_prompt = settings.get("system_prompt", "").strip()

    # 注入系统提示词
    if system_prompt:
        has_system = any(m.get("role") == "system" for m in messages)
        if has_system:
            for m in reversed(messages):
                if m.get("role") == "system":
                    m["content"] = m.get("content", "") + "\n\n" + system_prompt
                    break
        else:
            messages = [{"role": "system", "content": system_prompt}] + messages

    cfg = _pick_account(strategy)
    if not cfg:
        raise HTTPException(503, "没有可用账号")

    # 处理图片/文件上传
    ref_file_ids = []
    try:
        ref_file_ids, cfg = process_vision_images(cfg, messages)
    except Exception:
        pass

    model_info = get_models().get(model, get_models().get("deepseek-chat"))
    if not model_info:
        model_info = (False, False, False, 1048576, 393216)
    thinking_enabled, search_enabled, _, _, _ = model_info

    prompt = convert_messages_for_deepseek(messages, tools)
    tool_prompt = build_tool_prompt(tools) if tools else ""
    if tool_prompt:
        last_user_idx = prompt.rfind("\n[USER]\n")
        if last_user_idx != -1:
            prompt = prompt[:last_user_idx] + "\n\n" + tool_prompt + "\n" + prompt[last_user_idx:]
        else:
            prompt = tool_prompt + "\n\n" + prompt

    input_tokens = max(1, len(prompt))
    t0 = time.time()
    result = _do_chat(cfg, prompt, model, thinking_enabled, search_enabled, stream,
                    is_retry=False, has_tools=bool(tools), tools=tools, ref_file_ids=ref_file_ids)
    return _track_and_return(result, t0, model, stream, input_tokens)


def _track_and_return(result, t0, model, stream, input_tokens):
    """追踪统计并返回响应。对 StreamingResponse 包裹以计数输出 token。"""
    if isinstance(result, StreamingResponse):
        orig_iter = result.body_iterator

        async def _counting_iter():
            buf = ""
            async for chunk in orig_iter:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="ignore")
                buf += chunk
                yield chunk
            parts = []
            for line in buf.split("\n"):
                if line.startswith("data: "):
                    try:
                        obj = json.loads(line[6:])
                        c = obj.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if c:
                            parts.append(c)
                    except Exception:
                        pass
            output_tokens = max(1, len("".join(parts)))
            elapsed_ms = int((time.time() - t0) * 1000)
            track_api_call(model, True, input_tokens, output_tokens, elapsed_ms)

        return StreamingResponse(_counting_iter(), media_type=result.media_type,
                                 headers=dict(result.headers),
                                 background=result.background)
    else:
        elapsed_ms = int((time.time() - t0) * 1000)
        try:
            body = json.loads(result.body) if hasattr(result, 'body') else {}
            content = body.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
            output_tokens = max(1, len(content))
        except Exception:
            output_tokens = input_tokens
        track_api_call(model, stream, input_tokens, output_tokens, elapsed_ms)
        return result


def _do_chat(cfg, prompt, model, thinking_enabled, search_enabled, stream, is_retry=False, has_tools=False, tools=None, ref_file_ids=None):
    session_id = cfg["session_id"]
    req_headers = build_request_headers(cfg, session_id)
    pow_response = get_pow_response()
    if pow_response:
        req_headers["x-ds-pow-response"] = pow_response

    req_body = {
        "chat_session_id": session_id,
        "parent_message_id": None,
        "prompt": prompt,
        "ref_file_ids": ref_file_ids or [],
        "thinking_enabled": thinking_enabled,
        "search_enabled": search_enabled,
    }
    # 设置 model_type
    if ref_file_ids:
        req_body["model_type"] = "vision"
    elif "expert" in model:
        req_body["model_type"] = "expert"
    else:
        req_body["model_type"] = "default"

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def _parse_sse(resp):
        phase = "content"
        _first_content = True
        fragment_type = None  # None=old format, "THINK"/"RESPONSE"=fragments format
        _line_buf = b""

        def _read_lines():
            nonlocal _line_buf
            for chunk in resp.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                _line_buf += chunk
                while b"\n" in _line_buf:
                    raw, _line_buf = _line_buf.split(b"\n", 1)
                    yield raw.decode("utf-8", errors="ignore").strip()
            if _line_buf.strip():
                yield _line_buf.decode("utf-8", errors="ignore").strip()

        for line in _read_lines():
            if not line:
                continue
            # 视觉调试
            if ref_file_ids and (line.startswith("data:") or not line.startswith("{")):
                log_info(f"SSE_RAW: {line[:300]}")

            if line.startswith("event:"):
                continue

            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "code" in obj and obj.get("code", 0) >= 40000:
                        yield ("error", {"message": obj.get("msg", "unknown"), "code": obj.get("code")})
                        return
                except json.JSONDecodeError:
                    pass
                continue

            ds = line[6:] if line.startswith("data: ") else line
            if ds.strip() == "[DONE]":
                yield ("done", "")
                return

            try:
                obj = json.loads(ds)
                if not isinstance(obj, dict):
                    continue

                val = obj.get("v")

                # Toast/metadata (v is dict)
                if isinstance(val, dict):
                    t_type = val.get("type", "")
                    t_content = val.get("content", "")
                    fr = val.get("finish_reason", "")
                    if t_type == "error" and fr:
                        yield ("error", {"message": t_content, "code": fr})
                        return
                    # Extract fragment type from response metadata
                    resp_data = val.get("response", {})
                    if isinstance(resp_data, dict):
                        frags = resp_data.get("fragments", [])
                        if frags and isinstance(frags, list):
                            last = frags[-1]
                            if isinstance(last, dict) and last.get("type"):
                                fragment_type = last["type"]
                    continue

                path = obj.get("p", "")
                v = obj.get("v", "")
                if not isinstance(v, str) or not v:
                    continue

                # ── Fragments format (vision models) ──
                if path and "fragments" in path:
                    if fragment_type == "THINK" and thinking_enabled:
                        yield ("thinking", v)
                    elif fragment_type == "RESPONSE" or fragment_type is None:
                        if _first_content and v and v[0] == '\uff01':
                            v = v[1:]
                        _first_content = False
                        yield ("content", v)
                    continue

                # ── Old format ──
                if path == "response/content" and obj.get("o") == "APPEND":
                    phase = "content"
                    if _first_content and v and v[0] == '\uff01':
                        v = v[1:]
                    _first_content = False
                    yield ("content", v)
                elif path == "response/thinking_content" and thinking_enabled:
                    phase = "thinking"
                    yield ("thinking", v)
                elif path:
                    continue
                elif isinstance(v, str) and v:
                    if _first_content and v and v[0] == '\uff01':
                        v = v[1:]
                    _first_content = False
                    if phase == "thinking" and thinking_enabled:
                        yield ("thinking", v)
                    else:
                        yield ("content", v)
            except json.JSONDecodeError:
                continue

    def do_stream():
        try:
            resp = cffi_requests.post(
                "https://chat.deepseek.com/api/v0/chat/completion",
                headers=req_headers,
                json=req_body,
                impersonate="chrome120",
                stream=True,
                timeout=120,
            )

            if resp.status_code == 401 and not is_retry:
                log_warn("Token 401, 尝试刷新...")
                new_cfg = relogin(cfg)
                if new_cfg:
                    for chunk in _do_chat_stream_only(new_cfg, prompt, model, thinking_enabled, search_enabled, has_tools, tools):
                        yield chunk
                    return
                yield f'data: {json.dumps({"error": {"message": "Token expired", "type": "auth_error", "code": 401}})}\n\n'
                yield "data: [DONE]\n\n"
                return

            if resp.status_code != 200:
                error_msg = f"DeepSeek returned {resp.status_code}: {resp.text[:300]}"
                log_error(error_msg)
                yield f'data: {json.dumps({"error": {"message": error_msg, "type": "server_error", "code": resp.status_code}})}\n\n'
                yield "data: [DONE]\n\n"
                return

            if has_tools:
                buf_content = ""
                for etype, val in _parse_sse(resp):
                    if etype == "content":
                        buf_content += val
                    elif etype == "thinking":
                        r = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                             "choices": [{"index": 0, "delta": {"reasoning_content": val}, "finish_reason": None}]}
                        yield f'data: {json.dumps(r, ensure_ascii=False)}\n\n'
                    elif etype == "error":
                        yield f'data: {json.dumps({"error": {"message": val["message"], "type": "server_error", "code": val.get("code")}})}\n\n'
                        yield "data: [DONE]\n\n"
                        return
                    elif etype == "done":
                        break
                tc_result, final_content = extract_tool_call(buf_content, get_tool_names(tools) if tools else None)
                if tc_result:
                    r = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": None}, "finish_reason": None}]}
                    yield f'data: {json.dumps(r, ensure_ascii=False)}\n\n'
                    for i, tc in enumerate(tc_result):
                        delta = {"role": "assistant", "content": None,
                                 "tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                                 "function": {"name": tc["function"]["name"], "arguments": ""}}]}
                        r = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                        yield f'data: {json.dumps(r, ensure_ascii=False)}\n\n'
                        args = tc["function"]["arguments"]
                        r = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                             "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, "function": {"arguments": args}}]}, "finish_reason": None}]}
                        yield f'data: {json.dumps(r, ensure_ascii=False)}\n\n'
                    r = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
                    yield f'data: {json.dumps(r, ensure_ascii=False)}\n\n'
                else:
                    if buf_content:
                        r = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                             "choices": [{"index": 0, "delta": {"content": buf_content}, "finish_reason": None}]}
                        yield f'data: {json.dumps(r, ensure_ascii=False)}\n\n'
                    r = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    yield f'data: {json.dumps(r, ensure_ascii=False)}\n\n'
                yield "data: [DONE]\n\n"
                return

            for etype, val in _parse_sse(resp):
                if etype == "content":
                    r = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                         "choices": [{"index": 0, "delta": {"content": val}, "finish_reason": None}]}
                    yield f'data: {json.dumps(r, ensure_ascii=False)}\n\n'
                elif etype == "thinking":
                    r = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                         "choices": [{"index": 0, "delta": {"reasoning_content": val}, "finish_reason": None}]}
                    yield f'data: {json.dumps(r, ensure_ascii=False)}\n\n'
                elif etype == "error":
                    yield f'data: {json.dumps({"error": {"message": val["message"], "type": "server_error", "code": val.get("code")}})}\n\n'
                    yield "data: [DONE]\n\n"
                    return
                elif etype == "done":
                    yield "data: [DONE]\n\n"
                    return

        except Exception as e:
            log_error(f"do_stream failed: {e}")
            yield f'data: {json.dumps({"error": {"message": str(e), "type": "server_error"}})}\n\n'
            yield "data: [DONE]\n\n"

    def do_nonstream():
        full_content = ""
        full_thinking = ""

        try:
            resp = cffi_requests.post(
                "https://chat.deepseek.com/api/v0/chat/completion",
                headers=req_headers,
                json=req_body,
                impersonate="chrome120",
                stream=True,
                timeout=120,
            )

            if resp.status_code == 401 and not is_retry:
                log_warn("Token 401 in nonstream, trying refresh...")
                new_cfg = relogin(cfg)
                if new_cfg:
                    return _do_chat(new_cfg, prompt, model, thinking_enabled, search_enabled, False, is_retry=True, has_tools=has_tools, tools=tools)

            if resp.status_code != 200:
                raise HTTPException(502, detail={"error": {"message": f"DeepSeek returned {resp.status_code}", "type": "server_error"}})

            for etype, val in _parse_sse(resp):
                if etype == "content":
                    full_content += val
                elif etype == "thinking":
                    full_thinking += val
                elif etype == "error":
                    log_error(f"DeepSeek SSE error: {val}")
                    raise HTTPException(502, detail={"error": {"message": val["message"], "type": "server_error", "code": val.get("code")}})

        except HTTPException:
            raise
        except Exception as e:
            log_error(f"nonstream error: {e}")
            raise HTTPException(502, detail={"error": {"message": str(e), "type": "server_error"}})

        # 过滤前导 !
        if full_content and full_content[0] == '\uff01':
            full_content = full_content[1:]

        finish_reason = "stop"
        tc_result = None
        final_content = full_content
        if has_tools:
            tc_result, final_content = extract_tool_call(full_content, get_tool_names(tools) if tools else None)
            if tc_result:
                finish_reason = "tool_calls"

        msg = {"role": "assistant", "content": final_content}
        if full_thinking:
            msg["reasoning_content"] = full_thinking
        if tc_result:
            msg["tool_calls"] = tc_result
            if final_content is None:
                msg["content"] = None

        return JSONResponse({
            "id": chat_id, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    if stream:
        return StreamingResponse(do_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    return do_nonstream()


def _do_chat_stream_only(cfg, prompt, model, thinking_enabled, search_enabled, has_tools=False, tools=None):
    result = _do_chat(cfg, prompt, model, thinking_enabled, search_enabled, stream=True, is_retry=True, has_tools=has_tools, tools=tools)
    if isinstance(result, StreamingResponse):
        yield from result.body_iterator
    else:
        yield f"data: {json.dumps({'error': {'message': 'Retry returned non-stream', 'type': 'server_error'}})}\n\n"
        yield "data: [DONE]\n\n"


# ── 启动 ─────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print(f"DeepSeek Proxy\n Admin: http://localhost:{PROXY_PORT}/admin\n API: http://localhost:{PROXY_PORT}/v1")
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT, log_level="info")
