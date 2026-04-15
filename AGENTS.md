# Hermes Link AGENTS Guide

本文件只约束 `apps/hermes-link/` 子项目。
当本目录作为 `HermesPilot` monorepo 的子项目存在时，仓库级规则见根 `AGENTS.md`。
当本目录作为独立仓库发布时，本文件本身也承担仓库级规范职责。

涉及以下任一内容前，还必须先读架构文档：

- 在 monorepo 中优先读取根目录 `HERMES_LINK_ARCHITECTURE.md`
- 在独立仓库中若没有该文件，则至少先读 `README.md` 里的 Architecture / Networking / Security 部分

- Relay 协议或 control websocket
- 设备鉴权、scope、配对、token 体系
- 执行面、run 事件流、SSE
- Hermes 适配层边界
- `apps/hermes-link/` 与 `apps/server/` 的跨项目联动

## 0. 产品定义

完整表述固定为：

`Hermes Link is a secure companion service for hermes-agent.`

这不是一句宣传语，而是后续一切实现的边界约束：

- `Hermes Link` 是服务，不是一次性脚本
- `Hermes Link` 是 companion，不是取代 `hermes-agent`
- `Hermes Link` 的核心是 secure access，不是本地 dashboard 的复刻

## 1. 当前状态与边界

- 当前目录已进入第一版可运行控制面阶段
- 当前已覆盖本地服务生命周期、配对鉴权、Hermes 控制面命令与 API、备份与自更新入口
- 当前已具备执行面第一版：结构化 runs、SSE 事件流、继续会话、取消、重试、超时和后台任务状态查询
- 当前已具备 Host allowlist、基础限流，以及面向浏览器接入的 CORS allowlist 配置
- 当前已具备 Relay 第一版基础能力：
  - Cloudflare Worker relay bootstrap / refresh / heartbeat 对接
  - 常驻 control websocket
  - 本地 FastAPI 的 HTTP-over-WebSocket 代理
  - relay connect token 签发
  - relay 内部请求专用 loopback 鉴权
- 当前控制平面已统一收口：
  - Link 本地配置变更
  - Relay 生命周期变更
  - 配对会话查看 / 取消
  - 默认脱敏的 Link / Relay 配置快照
- 当前仍未完成更高阶的审批流、多 relay provider 抽象与公网自动路由协商
- 不要因为“先跑起来”就把它写成粗糙的 HTTP 壳或远程 shell
- 不要把 Hermes 官方 dashboard 当成最终架构，只能把它当成本地能力来源
- 不要修改 `reference/hermes-agent/`，它只用于参考

## 2. 项目目标

`Hermes Link` 的终局目标，是把用户机器上的 `hermes-agent` 变成一个可被外部客户端安全访问的个人 AI 节点。

核心目标只有四个：

1. 提供稳定接入层
2. 提供安全边界
3. 提供多网络路径连接能力
4. 提供可运维、可安装、可更新的本地服务形态

如果一个实现不服务于这四个目标，就要重新审视是否属于本项目职责。

## 3. 产品职责

终局内，`Hermes Link` 应承担以下职责：

- 对外暴露稳定 API 与流式事件能力
- 承接 `HermesPilot App`、网页端或其他客户端接入
- 为 Hermes 管理面提供统一控制层
- 封装 Hermes 内部模块、CLI 和官方 dashboard 可用能力
- 在局域网直连、公网直连、Relay 三种链路间做连接编排
- 实施配对、鉴权、授权、审计与速率限制
- 作为本地常驻服务，支持安装、更新、卸载和服务守护

## 4. 非职责

以下内容默认不属于 `Hermes Link`：

- 重新实现 `hermes-agent` 的核心 agent loop
- 把 Hermes 所有 CLI 命令重新做一套 UI
- 构建重业务 SaaS 平台
- 在云端保存用户会话内容作为默认路径
- 将 Relay 做成“云上主控、本机被动执行”的架构

## 5. 技术方向

### 语言与运行时

- 主体语言：Python
- 这是由 Hermes 本体是 Python、且需要直接复用其内部能力决定的
- 不要为了“生态统一”强行改成 Node.js；那会让项目退化为 CLI 包装层

### 目标技术栈

- Python 3.11+
- `FastAPI` 作为本地控制 API 层
- `Uvicorn` 或等价 ASGI server 作为运行容器
- `Pydantic v2` 作为配置与协议模型
- `Typer` 或等价 CLI 框架，用于安装、服务管理、诊断和本地控制命令
- `httpx` / `websockets` / `anyio` 作为网络与流式通信基础
- `sqlite3` 或轻量持久化层，用于本地状态与审计
- `platformdirs` 或等价能力，统一跨平台目录
- `cryptography` 或等价方案，用于设备身份、配对与令牌能力

### 明确约束

- 不要默认引入重量级消息总线、任务编排系统或分布式框架
- 不要把 Cloudflare 能力直接写死进核心领域模型
- Relay 必须有清晰抽象层，Cloudflare 只是首个实现，不是协议定义者

## 6. 架构原则

### 第一性原则

- App 需要的是稳定接入层，而不是 Hermes 内部细节
- 管理与执行需要的是结构化接口，而不是 CLI 文本拼接
- 安全边界必须围绕“外部设备访问本机 agent”来设计，而不是围绕 `localhost`

### 关键架构判断

- 优先做“稳定 API + Adapter”
- 次级做“受控 CLI 执行”
- 禁止把“原样转发 CLI 字符串”当成主架构

### local-first

- 局域网直连优先
- 公网直连次之
- Relay 最后兜底
- 客户端不应该自己管理三套网络策略

### 控制平面与执行平面分离

- 控制平面负责状态、配置、权限、审计、设备、拓扑
- 执行平面负责聊天、任务、命令、流式转发
- 不要把所有逻辑塞进单个路由处理器里
- Link 配置、Relay 状态和配对管理的读写逻辑，应优先集中在共享 control plane 模块，而不是在 CLI / API / service 层各自复制一套
- 默认返回给远端客户端的配置快照必须脱敏；只有本机受信任运维路径才允许显式查看敏感字段
- Relay 代理只应转发显式公开的 API 路径，不应把本地调试页、文档页或未来的私有路由默认暴露出去

## 7. 对 Hermes 的适配规则

`Hermes Link` 必须通过适配层对接 `hermes-agent`，而不能把 Hermes 内部实现直接散落到整个代码库。

### 适配优先级

1. 优先调用 Hermes 稳定的 Python 内部模块
2. 次优调用官方 dashboard 已有的本地管理能力来源
3. 最后才回落到受控 CLI 适配

### 适配约束

- 所有 Hermes 相关调用集中在单独 adapter 层
- adapter 层要做版本探测与能力判定
- 业务层不应直接 import 多处 Hermes 内部模块
- 不能让上游内部结构变化直接击穿外部 API

### 对外协议约束

- 对 App 暴露的是 `Hermes Link API`
- 不是 Hermes CLI 文本
- 也不是 dashboard 的私有 `/api/*` 路由镜像

## 8. 终局功能面

终局内，`Hermes Link` 至少要覆盖这些功能面。

### 连接面

- LAN 直连
- 公网直连
- Relay 兜底
- 节点发现与地址选择
- 连通性检测与切换

### 控制面

- status
- sessions
- config
- env / provider auth
- profiles
- logs
- analytics
- cron
- skills
- toolsets
- backup / doctor / update

### 执行面

- 聊天流式调用
- 会话恢复、中断、重试
- 长任务与后台任务
- 受控 CLI 代执行
- 文件与中间产物传输

### 安全面

- 设备配对
- 设备信任管理
- 短期 token
- scope 权限
- 高风险审批
- 审计日志
- 速率限制
- 吊销与失效

## 9. API 与接口规则

### 对外接口

终局接口至少包含：

- 本地控制 API
- 客户端接入 API
- 流式事件接口
- Relay 接口
- 本地 CLI 管理命令

### API 设计规则

- 优先结构化资源与命令模型
- 不要把“执行某个 shell/CLI 字符串”设计成主要接口
- 长任务必须有任务 ID 与状态查询能力
- 流式接口必须支持取消、超时和断线恢复
- 高风险接口必须具备额外权限或审批机制

### 兼容性规则

- 一旦对外暴露给 App 的 API，默认视为兼容性承诺
- 不允许把“上游 Hermes 改了”当作破坏对外协议的理由

## 10. 安全规则

### 基本原则

- 默认拒绝，而不是默认放开
- 默认短期凭证，而不是长期主密钥
- 默认每设备独立身份，而不是所有设备共享凭据
- 默认可审计、可撤销、可限权

### 必须具备的能力

- 配对流程
- 设备注册表
- 令牌签发与过期
- access token 与 refresh token 必须分责：
  - access token 只用于请求授权与短期 access 轮换
  - refresh token 才能续整套设备会话
  - 不允许把 access token 设计成可无限续发 refresh token 的长期凭证
- scope 权限模型
- 审计日志
- 速率限制
- 危险操作二次确认

### 禁止事项

- 不要把 dashboard 当前的 `localhost` 假设当成最终安全模型
- 不要只靠单个固定 Bearer token 保护所有设备
- 不要让 Relay 默认拥有超出路由所需的权限
- 不要把敏感操作做成无日志、无审批、不可追踪

## 10.1 用户文案规则

- 面向用户的文案默认说人话，先说明发生了什么，再说明下一步怎么做
- 除非用户确实需要手动操作配置，否则不要把 `Bearer token`、`scope`、`control websocket` 这类实现细节直接甩给用户
- CLI / API 错误文案要区分：
  - `code` 给程序判断
  - `message` 给人看
- 对人看的 `message` 应优先表达结果、影响和建议动作，而不是底层实现名词
- 引导文案要克制但完整：该简短时不要啰嗦，该需要下一步操作时必须明确告诉用户

## 11. 安装、更新、卸载规范

安装与服务生命周期是产品能力的一部分，不是“后面再补”的脚手架问题。

### 官方分发

- 代码仓库：GitHub
- 版本制品：GitHub Releases
- 官方安装入口：`install.sh` 与 `install.ps1`
- PyPI / `pipx` 作为开发者友好入口，不应是唯一入口

### 安装规则

- 可以假设用户机器已有 Python
- 但必须创建独立 venv
- 不复用 Hermes 自身 venv
- 安装脚本必须使用固定版本制品，而不是直接安装 `main`
- 安装脚本应支持无人值守与交互式两种模式

### 更新规则

- 提供显式 `update` 命令
- 更新要尽量原子化
- 配置与状态默认保留
- 失败时要能回退到上一个已知可用版本

### 卸载规则

- 提供显式 `uninstall` 命令
- 卸载必须先停服务
- 支持“保留数据”和“彻底删除”两种模式
- 永远不能误删 Hermes 本体目录

## 12. 跨平台规范

### 共性原则

- 路径、服务注册、日志目录、启动方式都必须抽象，不要在业务代码里散落平台分支
- 默认 user-scoped 安装，尽量避免一上来就要求管理员权限

### macOS

- 默认 `launchd`
- 优先 `LaunchAgent`

### Linux

- 默认 `systemd --user`
- system-wide 模式只能作为显式选项

### Windows

- 默认 PowerShell 安装脚本
- 不以 `.bat` 作为主安装入口
- 终局支持服务化或等价稳定后台模式

## 13. 目录职责

后续开始写代码后，目录应尽量按下面的边界演进：

```text
apps/hermes-link/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── src/hermes_link/
│   ├── api/              # FastAPI routes / schemas / streaming
│   ├── app/              # app bootstrap / DI / config loading
│   ├── cli/              # init / pair / doctor / install-service / update
│   ├── hermes/           # Hermes adapter layer
│   ├── relay/            # relay provider abstraction + implementations
│   ├── security/         # pairing / tokens / approvals / audit
│   ├── services/         # lifecycle, health, background jobs
│   ├── storage/          # sqlite, file state, migrations
│   └── topology/         # LAN/public/relay routing strategy
├── tests/
└── scripts/
```

### 目录规则

- `hermes/` 之外不要到处散落 Hermes 内部调用
- `security/` 之外不要散落鉴权与审批逻辑
- `relay/` 之外不要把云厂商 SDK 或协议细节扩散到业务层
- `cli/` 不负责业务实现，只负责调度服务能力

## 14. 仓库策略

`Hermes Link` 未来会独立开源，但当前又需要存在于 `HermesPilot` monorepo。

推荐长期策略：

- 独立仓库对外发布与维护
- `apps/hermes-link/` 作为 monorepo 中的同步目录
- 同步优先使用 `git subtree`
- 避免 `git submodule`

### 在 monorepo 内开发时的要求

- 不要引入依赖 monorepo 根工具链才能运行的设计
- 不要把 `Hermes Link` 写成只能在 `HermesPilot` 内部运行的内部模块
- 任何文档、脚本和配置都要能被未来独立仓库原样接收

## 15. 编码与实现规则

### 一般规则

- 优先简单、可读、可测试
- 优先写稳定边界，不要先写功能堆叠
- 遇到 Hermes 上游不稳定点，优先加 adapter，不要直接把 workaround 扩散到外围

### API 与业务规则

- 控制层不能直接拼命令字符串
- 执行层不能绕过权限模型
- 任何高风险操作都不能直接暴露为无保护的 HTTP 路由
- 用户可见文案必须至少支持中文和英文
- CLI 默认跟随宿主系统主语言；HTTP API 应优先尊重 `Accept-Language`
- 对外 JSON 字段和错误 `code` 必须稳定，语言切换只能影响展示文案，不能影响协议字段
- LAN / public 直连共享同一个本地监听器，但拓扑判断必须考虑真实 bind host；如果只绑在 localhost，就不能对外宣称 ready
- 公网直连默认按 HTTPS 公网入口来定义，不要把裸露的公网 HTTP 端口当成“已经完成的 public direct”
- 外部入口至少要有 Bearer 鉴权和 Host allowlist 两层边界；不要只靠其中一层
- 默认配对 scope 必须保持 least privilege；`admin`、设备管理或高风险写权限不能默认下发给所有设备
- 直连接口必须考虑 token 生命周期，至少覆盖：自省、续期、吊销 / 登出
- 公网可达入口必须带基础限流，至少区分匿名请求、已鉴权请求和配对认领请求

### 备份与恢复规则

- 备份恢复默认按安全边界处理，而不是“先解压再说”
- 恢复流程必须拒绝路径穿越、软链接或其他可逃逸目标目录的归档条目
- 如果备份输出路径位于 Hermes home 内部，必须显式跳过输出归档自身，避免自包含污染

### 并发与状态

- 长连接、流式消息、后台任务必须有明确生命周期
- 不要把内存态会话当成唯一事实来源
- 需要持久化的设备、令牌、审计、任务状态要有明确存储边界

### 文档同步

- 只要改了安装、更新、卸载、配对、权限、Relay、公共 API、服务化方式，就必须同步更新 `README.md`
- 只要改了目录职责、编码规范、技术方向、验证命令，就必须同步更新 `AGENTS.md`

## 16. 验证要求

当前阶段至少应覆盖以下最小验证：

- `python3 -m pytest`
- `python3 -m compileall src`
- 如改动涉及 CLI 启动路径，补充：
  - `python3 -m hermes_link help`
  - `python3 -m hermes_link status --json`
  - `python3 -m hermes_link sessions list --json`

后续仍应逐步扩展到：

- 单元测试
- API 协议测试
- Hermes adapter 兼容测试
- 安装脚本测试
- 跨平台服务注册测试
- 配对与权限测试
- Relay 连接与回退测试

### 目标命令方向

后续应继续补齐并固定至少这些命令：

- 格式化 / Lint
- 类型检查
- 单元测试
- 集成测试
- 安装脚本验证
