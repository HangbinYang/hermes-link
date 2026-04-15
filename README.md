# Hermes Link

> Hermes Link is a secure companion service for hermes-agent.

[中文说明 / Chinese documentation](./README.zh-CN.md)

`Hermes Link` turns a local `hermes-agent` install into a secure personal node that can be reached by mobile apps, web clients, and other trusted devices.

## What It Does

- Adds a stable local API and CLI on top of `hermes-agent`
- Handles device pairing, tokens, scopes, audit, and rate limits
- Supports LAN direct access, public direct access, and relay fallback
- Exposes a structured control plane instead of raw shell commands
- Tracks changing LAN IP addresses and can report the latest network snapshot upstream

## Status

This repository already contains a working first version of the local companion service:

- FastAPI-based local service
- Typer-based CLI
- Pairing, bearer auth, refresh tokens, device revocation, and audit log
- Hermes control APIs for sessions, config, env, providers, cron, logs, skills, profiles, backup, and more
- Structured run execution with SSE events, retry, cancel, and timeout
- Relay control websocket and HTTP-over-WebSocket proxy
- Dynamic LAN IPv4/IPv6 detection and latest LAN endpoint reporting

## Quick Start

### macOS / Linux

Install from the current `main` branch:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.sh | bash
```

Install a tagged release:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.sh \
| env HERMES_LINK_REF="v0.1.0" HERMES_LINK_REF_TYPE="tag" bash
```

Install and enable autostart:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.sh \
| env HERMES_LINK_ENABLE_AUTOSTART="1" bash
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/install.ps1 | iex
```

## Update

### macOS / Linux

Update to the latest `main`:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/update.sh | bash
```

Update to a specific tag:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/update.sh \
| env HERMES_LINK_REF="v0.1.0" HERMES_LINK_REF_TYPE="tag" bash
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/update.ps1 | iex
```

## Uninstall

### macOS / Linux

Uninstall but keep local data:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/uninstall.sh | bash
```

Uninstall and remove local data:

```bash
curl -fsSL https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/uninstall.sh \
| env HERMES_LINK_REMOVE_DATA="1" bash
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/HangbinYang/hermes-link/main/scripts/uninstall.ps1 | iex
```

## Installer Environment Variables

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

## Common CLI Commands

```bash
hermes-link status
hermes-link doctor
hermes-link pair
hermes-link devices list
hermes-link relay status
hermes-link sessions list
hermes-link config show
```

## Runtime Layout

By default, the installer creates a virtualenv under:

```text
~/.local/share/hermes-link/venv
```

The service runtime data is managed by `platformdirs`, or by `HERMES_LINK_HOME` when explicitly set.

## Networking

- LAN direct, public direct, and relay are three connection paths on top of the same local FastAPI listener
- LAN URLs are generated from the current bind family and current LAN IPv4/IPv6 observations
- IPv6 direct URLs are always rendered as `http://[addr]:port`
- The latest LAN endpoints can be reported upstream so clients can discover fresh local addresses after network changes

## Security

- Device pairing is the entry point
- Access tokens and refresh tokens are separated by responsibility
- Host allowlist is dynamic and derived from local hostnames, direct addresses, and explicit extra hosts
- Relay only forwards explicitly exposed API paths

## Development

Clone the repo and install it in editable mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Repository Layout

```text
scripts/        Install, update, and uninstall helpers
src/hermes_link Local service source code
tests/          Automated tests
```

## Relationship to HermesPilot

This repository is the standalone home of `Hermes Link`. It can also be mirrored or embedded into the larger `HermesPilot` monorepo.

## License

MIT
