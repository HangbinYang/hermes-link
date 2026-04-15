# Hermes Link

> Hermes Link is a secure companion service for hermes-agent.  
> Hermes Link 是 hermes-agent 的安全伴随服务。

`Hermes Link` turns a local `hermes-agent` install into a secure personal node that can be reached by mobile apps, web clients, and other trusted devices.

`Hermes Link` 的目标，是把用户机器上的 `hermes-agent` 变成一个安全、稳定、可远程连接的个人 AI 节点。

## What It Does / 它解决什么问题

**English**

- Adds a stable local API and CLI on top of `hermes-agent`
- Handles device pairing, tokens, scopes, audit, and rate limits
- Supports LAN direct access, public direct access, and relay fallback
- Exposes a structured control plane instead of raw shell commands
- Tracks changing LAN IP addresses and can report the latest network snapshot upstream

**中文**

- 在 `hermes-agent` 之上补一层稳定的本地 API 和 CLI
- 负责配对、令牌、权限、审计、限流这些安全边界
- 支持局域网直连、公网直连、Relay 兜底三种链路
- 对外暴露结构化控制面，而不是把 shell 命令原样暴露出去
- 能持续感知宿主机 LAN 地址变化，并把最新网络快照上报给上游服务

## Status / 当前状态

**English**

This repository already contains a working first version of the local companion service:

- FastAPI-based local service
- Typer-based CLI
- Pairing, bearer auth, refresh tokens, device revocation, audit log
- Hermes control APIs for sessions, config, env, providers, cron, logs, skills, profiles, backup, and more
- Structured run execution with SSE events, retry, cancel, and timeout
- Relay control websocket and HTTP-over-WebSocket proxy
- Dynamic LAN IPv4/IPv6 detection and latest LAN endpoint reporting

**中文**

当前仓库已经不是纯骨架，而是一版可运行的本地 companion service：

- 基于 FastAPI 的本地服务
- 基于 Typer 的 CLI
- 已具备配对、Bearer 鉴权、refresh token、设备吊销、审计日志
- 已具备 Hermes 控制面接口：sessions、config、env、providers、cron、logs、skills、profiles、backup 等
- 已具备结构化 run 执行能力，支持 SSE 事件流、重试、取消、超时
- 已具备 Relay control websocket 与 HTTP-over-WebSocket 代理
- 已具备动态 LAN IPv4/IPv6 探测，以及最新 LAN 端点上报

## Quick Start / 快速开始

### macOS / Linux

Install from the current `main` branch:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.sh | bash
```

从当前 `main` 分支直接安装：

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.sh | bash
```

Install a tagged release:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.sh \
| env HERMES_LINK_REF="v0.1.0" HERMES_LINK_REF_TYPE="tag" bash
```

安装指定 tag 版本：

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.sh \
| env HERMES_LINK_REF="v0.1.0" HERMES_LINK_REF_TYPE="tag" bash
```

Install and enable autostart:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.sh \
| env HERMES_LINK_ENABLE_AUTOSTART="1" bash
```

安装并启用开机自启：

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.sh \
| env HERMES_LINK_ENABLE_AUTOSTART="1" bash
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.ps1 | iex
```

## Update / 更新

### macOS / Linux

Update to the latest `main`:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/update.sh | bash
```

更新到最新 `main`：

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/update.sh | bash
```

Update to a specific tag:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/update.sh \
| env HERMES_LINK_REF="v0.1.0" HERMES_LINK_REF_TYPE="tag" bash
```

更新到指定 tag：

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/update.sh \
| env HERMES_LINK_REF="v0.1.0" HERMES_LINK_REF_TYPE="tag" bash
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/update.ps1 | iex
```

## Uninstall / 卸载

### macOS / Linux

Uninstall but keep local data:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/uninstall.sh | bash
```

卸载但保留本地数据：

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/uninstall.sh | bash
```

Uninstall and remove local data:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/uninstall.sh \
| env HERMES_LINK_REMOVE_DATA="1" bash
```

卸载并删除本地数据：

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/uninstall.sh \
| env HERMES_LINK_REMOVE_DATA="1" bash
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/uninstall.ps1 | iex
```

## Installer Environment Variables / 安装脚本环境变量

| Variable | Meaning |
| --- | --- |
| `HERMES_LINK_GITHUB_REPOSITORY` | GitHub repository, default: `HangbinYang/hermes-link` |
| `HERMES_LINK_REF` | Branch, tag, or commit to install, default: `main` |
| `HERMES_LINK_REF_TYPE` | `branch`, `tag`, or `commit`, default: `branch` |
| `HERMES_LINK_PACKAGE_SPEC` | Full pip package spec override |
| `HERMES_LINK_INSTALL_ROOT` | Install root, default: `~/.local/share/hermes-link` on macOS/Linux |
| `HERMES_LINK_VENV_DIR` | Virtualenv path override |
| `HERMES_LINK_ENABLE_AUTOSTART` | `1` to enable autostart during install |
| `HERMES_LINK_START_AFTER_INSTALL` | `0` to install without starting the service |
| `HERMES_LINK_RESTART_AFTER_UPDATE` | `0` to skip restart after update when the service was running |
| `HERMES_LINK_REMOVE_DATA` | `1` to remove runtime data during uninstall |
| `PYTHON` | Python executable override |

| 变量 | 含义 |
| --- | --- |
| `HERMES_LINK_GITHUB_REPOSITORY` | GitHub 仓库名，默认 `HangbinYang/hermes-link` |
| `HERMES_LINK_REF` | 要安装的分支、tag 或 commit，默认 `main` |
| `HERMES_LINK_REF_TYPE` | `branch`、`tag` 或 `commit`，默认 `branch` |
| `HERMES_LINK_PACKAGE_SPEC` | 直接覆盖 pip 安装源 |
| `HERMES_LINK_INSTALL_ROOT` | 安装根目录，macOS/Linux 默认 `~/.local/share/hermes-link` |
| `HERMES_LINK_VENV_DIR` | 自定义虚拟环境目录 |
| `HERMES_LINK_ENABLE_AUTOSTART` | 安装时设为 `1` 可启用开机自启 |
| `HERMES_LINK_START_AFTER_INSTALL` | 设为 `0` 可安装后不立即启动服务 |
| `HERMES_LINK_RESTART_AFTER_UPDATE` | 设为 `0` 可在更新后跳过自动重启 |
| `HERMES_LINK_REMOVE_DATA` | 卸载时设为 `1` 会连同本地运行数据一起删除 |
| `PYTHON` | 指定 Python 可执行文件 |

## Common CLI Commands / 常用 CLI 命令

```bash
hermes-link status
hermes-link doctor
hermes-link pair
hermes-link devices list
hermes-link relay status
hermes-link sessions list
hermes-link config show
```

## Runtime Layout / 运行目录

**English**

By default, the installer creates a virtualenv under:

```text
~/.local/share/hermes-link/venv
```

The service runtime data is managed by `platformdirs`, or by `HERMES_LINK_HOME` when explicitly set.

**中文**

默认情况下，安装脚本会把虚拟环境放到：

```text
~/.local/share/hermes-link/venv
```

服务运行数据默认由 `platformdirs` 管理；如果显式设置了 `HERMES_LINK_HOME`，则会收敛到你指定的目录。

## Networking / 网络能力

**English**

- LAN direct, public direct, and relay are three different connection paths on top of the same local FastAPI listener
- LAN URLs are generated from the current bind family and current LAN IPv4/IPv6 observations
- IPv6 direct URLs are always rendered as `http://[addr]:port`
- The latest LAN endpoints can be reported upstream so clients can discover fresh local addresses after network changes

**中文**

- 局域网直连、公网直连、Relay 是建立在同一个本地 FastAPI 监听器上的三种链路
- LAN URL 会基于当前监听族以及当前探测到的 LAN IPv4/IPv6 地址动态生成
- IPv6 直连地址始终按 `http://[addr]:port` 的形式输出
- 最新 LAN 端点可以上报到上游，方便客户端在网络切换后获取最新地址

## Security / 安全边界

**English**

- Device pairing is the entry point
- Access tokens and refresh tokens are separated by responsibility
- Host allowlist is dynamic and derived from local hostnames, direct addresses, and explicit extra hosts
- Relay only forwards explicitly exposed API paths

**中文**

- 设备配对是接入入口
- access token 和 refresh token 明确分责
- Host allowlist 是动态生成的，来源于本机 host、直连地址和显式配置的额外 host
- Relay 只转发显式公开的 API 路径

## Development / 开发

Clone the repo and install it in editable mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

本地开发方式：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Repository Layout / 仓库结构

```text
scripts/        Install, update, and uninstall helpers
src/hermes_link Local service source code
tests/          Automated tests
```

```text
scripts/        安装、更新、卸载脚本
src/hermes_link 本地服务源码
tests/          自动化测试
```

## Relationship to HermesPilot / 与 HermesPilot 的关系

**English**

This repository is the standalone home of `Hermes Link`. It can also be mirrored or embedded into the larger `HermesPilot` monorepo.

**中文**

这个仓库是 `Hermes Link` 的独立仓库形态；同时它也可以被镜像或嵌入到更大的 `HermesPilot` monorepo 中。

## License / 许可证

MIT
