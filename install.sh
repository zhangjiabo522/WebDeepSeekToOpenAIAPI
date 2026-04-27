#!/bin/bash
# WebDeepSeekToOpenAIAPI 一键安装脚本
# 适配 Ubuntu/Debian + Python3
# 用法: bash <(curl -s https://raw.githubusercontent.com/zhangjiabo522/WebDeepSeekToOpenAIAPI/master/install.sh)
# 参数: --port 8080 指定端口，跳过交互
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
err()   { echo -e "${RED}✗${NC} $*"; exit 1; }

INSTALL_DIR="${HOME}/WebDeepSeekToOpenAIAPI"
REPO="https://github.com/zhangjiabo522/WebDeepSeekToOpenAIAPI.git"

echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   WebDeepSeekToOpenAIAPI 一键安装           ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── 解析参数 ──
PORT=""
for arg in "$@"; do
    case "$arg" in
        --port) ;;
        --port=*) PORT="${arg#*=}" ;;
        *)
            if [[ "$prev" == "--port" ]]; then
                PORT="$arg"
            fi
            ;;
    esac
    prev="$arg"
done

# ── 输入端口号 ──
if [ -z "$PORT" ]; then
    if [ -n "${PROXY_PORT:-}" ]; then
        PORT="$PROXY_PORT"
        info "使用环境变量端口: $PORT"
    else
        echo -e "请输入服务端口号（默认 8000）："
        read -p "端口: " PORT
        PORT="${PORT:-8000}"
        # 验证端口号
        if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
            err "无效端口: $PORT，必须是 1-65535 之间的数字"
        fi
        info "端口设置为: $PORT"
    fi
fi

# ── 1. 系统检测 ──
if ! command -v apt &>/dev/null && ! command -v yum &>/dev/null && ! command -v dnf &>/dev/null; then
    warn "非标准 Linux 发行版，将跳过包管理器，请确保已安装 Python3 >= 3.10 和 git"
fi

# ── 2. 安装依赖 ──
if command -v apt &>/dev/null; then
    info "更新软件源..."
    sudo apt update -qq
fi

for pkg in python3 python3-pip git curl; do
    if ! command -v $pkg &>/dev/null; then
        info "安装 $pkg..."
        if command -v apt &>/dev/null; then
            sudo apt install -y $pkg
        elif command -v yum &>/dev/null; then
            sudo yum install -y $pkg
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y $pkg
        else
            err "请手动安装: $pkg"
        fi
    fi
done
info "Python3 $(python3 --version)"
info "pip3 $(pip3 --version 2>&1 | head -1)"

# ── 3. 可选安装 Node.js（PoW 加速）──
if ! command -v node &>/dev/null; then
    warn "Node.js 未安装（PoW 将使用 Python 回退，稍慢但不影响使用）"
    if command -v apt &>/dev/null; then
        read -p "是否安装 Node.js 加速 PoW? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            info "安装 Node.js..."
            curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - 2>/dev/null || true
            sudo apt install -y nodejs 2>/dev/null || true
        fi
    fi
else
    info "Node.js $(node --version)"
fi

# ── 4. 克隆仓库 ──
if [ -d "$INSTALL_DIR/.git" ]; then
    info "仓库已存在，更新..."
    cd "$INSTALL_DIR"
    git pull origin master
else
    info "克隆仓库到 $INSTALL_DIR..."
    git clone "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── 5. 安装 Python 依赖 ──
info "安装 Python 依赖..."
pip3 install --quiet --break-system-packages -r requirements.txt 2>/dev/null || \
pip3 install --quiet -r requirements.txt 2>/dev/null || \
pip3 install -r requirements.txt
info "依赖安装完成"

# ── 6. 验证 ──
for f in proxy.py tool_call.py pow_native.py pow_solver.js; do
    [ -f "$f" ] || err "缺少文件: $f，请检查仓库完整性"
done

# ── 7. 防火墙放行端口 ──
open_firewall() {
    local port="$1"
    local opened=false

    # ufw (Ubuntu/Debian)
    if command -v ufw &>/dev/null && ufw status | grep -q "Status: active" 2>/dev/null; then
        info "检测到 ufw 防火墙，放行端口 $port..."
        sudo ufw allow "$port"/tcp 2>/dev/null && opened=true
        sudo ufw reload 2>/dev/null || true
    fi

    # firewalld (CentOS/RHEL/Fedora)
    if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld 2>/dev/null; then
        info "检测到 firewalld 防火墙，放行端口 $port..."
        sudo firewall-cmd --zone=public --add-port="$port"/tcp --permanent 2>/dev/null && opened=true
        sudo firewall-cmd --reload 2>/dev/null || true
    fi

    # iptables (兜底)
    if ! $opened && command -v iptables &>/dev/null; then
        info "使用 iptables 放行端口 $port..."
        sudo iptables -I INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null && opened=true
        if command -v iptables-save &>/dev/null; then
            sudo iptables-save > /dev/null 2>&1 || true
        fi
    fi

    if $opened; then
        info "端口 $port 已放行"
    else
        warn "未检测到防火墙或自动放行失败，如无法访问请手动放行端口 $port"
    fi
}

open_firewall "$PORT"

# ── 8. 完成 ──
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅ 安装完成！                              ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  端口: $PORT                                ║${NC}"
echo -e "${GREEN}║  目录: ${INSTALL_DIR}                      ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  启动:                                      ║${NC}"
echo -e "${GREEN}║    PROXY_PORT=$PORT python3 proxy.py         ║${NC}"
echo -e "${GREEN}║    或后台: PROXY_PORT=$PORT bash deploy.sh --bg║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  管理: http://localhost:$PORT/admin         ║${NC}"
echo -e "${GREEN}║  API:  http://localhost:$PORT/v1            ║${NC}"
echo -e "${GREEN}║  启动后登录 DeepSeek 账号即可使用           ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""

read -p "立即启动? [Y/n] " -n 1 -r
echo
if [[ $REPLY =~ ^[Nn]$ ]]; then
    echo "手动运行: export PROXY_PORT=$PORT && cd ${INSTALL_DIR} && python3 proxy.py"
    exit 0
fi
cd "$INSTALL_DIR"
export PROXY_PORT="$PORT"
exec python3 proxy.py
