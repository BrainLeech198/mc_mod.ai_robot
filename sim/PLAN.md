# PLAN.md — DeepSink 开发计划 / 待办

> 本文件记录**还没做**的事，供后续窗口接着干。已完成的历史见 `README.md` 与 git。
> 动手前先读 [`AGENTS.md`](./AGENTS.md) 了解怎么启动与接单。

---

## A. 协议保真（对齐主流 AI 平台，未做项）

| # | 项 | 现状 | 目标 |
|---|---|---|---|
| A1 | 后端 `openai` 的**流式透传** | 透传是整段返回后再本地切片 | 真透传上游 SSE（`stream:true` 时边收边转） |
| A2 | `n > 1` 多候选 | 只返回 1 个 choice | 支持 `n`，生成 `choices[0..n-1]` |
| A3 | `logprobs` / `top_logprobs` | 忽略 | 可选返回占位 logprobs |
| A4 | `stop` 序列 | 忽略 | 按 `stop` 截断并置 `finish_reason:"stop"` |
| A5 | SSE 心跳 | 无 | 长流期间发 `: ping`/`ping` 事件，防中间层断连 |
| A6 | `usage` 精确计费 | token 用「字符数 ÷ 1.6」粗估 | 可接真分词器（tiktoken 等）或按字/词更细的模型 |
| A7 | OpenAPI / Swagger 规范文件 | 无 | 产出 `openapi.json` + `/docs` 页内可试 |
| A8 | `anthropic-version` 校验 | 不校验 | 缺头时按规范返回错误或取默认版本 |

## B. 账户 / 产品（未做项）

| # | 项 | 现状 | 目标 |
|---|---|---|---|
| B1 | 服务端对话历史持久化 | 对话存浏览器 `localStorage` | 落库（新表 conversations/messages），跨设备可见 |
| B2 | API key 哈希存储 | 明文（为换取"列表可显示掩码"） | 存哈希 + 前缀，仅创建时给全量；列表只回前缀掩码 |
| B3 | `/api/*` 与注册接口限流 | 仅登录有节流 | 控制台接口 + 注册也做令牌桶/IP 限流 |
| B4 | 用量随时间图表 | 只有明细表 | 按天/周聚合的折线/柱状图 |
| B5 | 团队 / 多 key 权限 | 单账户 | 子账户、角色、key 作用域（可选，看是否需要） |
| B6 | 邮箱验证 / 找回密码 | 无 | 当前版本可省；真做时加校验码流程 |
| B7 | 审计日志页 | 无 | 登录/建 key/改密等敏感操作留痕并可视化 |

## C. 工程 / 健壮性（未做项）

| # | 项 | 说明 |
|---|---|---|
| C1 | 独立 git 仓库 | `sim/` 目前在主仓库里（旧 `tools/ai_*.py`、`instance/ai_server/` 是迁移源）；可择机 `git init` 独立成仓 |
| C2 | 配置文件 schema 校验 | 启动时校验 `deepsink.json` 字段，缺项给明确报错而非 KeyError |
| C3 | 结构化日志 | 现在是 `print(..., stderr)`；可换成带级别/请求 id 的日志文件 |
| C4 | 优雅退出 / 信号处理 | `serve_forever` 已接 KeyboardInterrupt；可补 SIGTERM、清理 |
| C5 | 覆盖度补充 | 数据竞争、超大并发的压力测试（本地部署优先级低） |

---

## D. 长期：AI × Minecraft 主线（DeepSink 的终极用途）

DeepSink 的初衷是让 **MC 里的机器人"有脑子"**。相关大目标（跨主项目）：

| # | 项 | 说明 | 状态 |
|---|---|---|---|
| D1 | 机器人手册 | `version/1.20.1-forge/机器人手册.md` | 已完成 |
| D2 | 聊天指挥通道 | 玩家在 MC 打字 → 机器人响应（在线应答回路） | 已完成 |
| D3 | **WE 建造通道工具化** | build 工具 + workspace 圈定 + 撤回记账（防砌墙、可回滚） | **未做** |
| D4 | **具身化** | `look/swing`、走路等客户端动作（已标"后期制作"） | **未做** |
| D5 | MC 模组接 DeepSink | 让支持自定义 base_url 的模组（如 MCAgent 一类）把 base_url 指到 8788，由 DeepSink（bridge 或 openai）供脑 | **未做** |
| D6 | DH 5.3G 停服清理 | 服务器存档清理 | 未做 |

> 备注：**真实 agent 客户端**（带 `tool_use` 多轮循环的）指向本服务会卡——`bridge` 每单需 worker 接，
> 撑不住完整 agent 流程。单轮问答可用；agent 场景请把后端切成 `openai` 接真 API。

## E. 多 worker 并行接单（已做基础）

目标：**开多个 CodeBuddy 窗口并行接单，提升吞吐**。

| # | 项 | 状态 |
|---|---|---|
| E1 | 请求认领原子锁（`deepsink_bridge.py`）+ `watch` 自动认领 | ✅ 已完成 |
| E2 | 锁超时抢占（worker 崩溃兜底，`bridge.claim_ttl_seconds`） | ✅ 已完成 |
| E3 | 队列观测 `deepsink_brain.py status` + `GET /api/bridge` + 控制台卡片 | ✅ 已完成 |
| E4 | 认领心跳（长任务时续锁，避免被误抢占） | 未做 |
| E5 | 按 worker 分工（如按模型/按请求类型路由到不同 worker） | 未做（看需要） |
| E6 | 死信队列 / 失败重试策略 | 未做 |

---

## 本轮（2026-10-16）产品真实化记录

- **去 mock 化**：全站文案（官网/docs/chat/platform/login）+ `deepsink.json` 注释 + 全部文档，
  统一 DeepSink 口吻；应答方统一称"推理 worker"；删除"mock / 模拟 / 假 AI 公司 / 本机大脑"等表述。
- **官网重写**：landing 页含产品定位、双协议架构、三步接入、定价表；新增 `/status` 状态页
  （服务状态 / 模型目录 / 协议 / 延迟自检），导航与 HEAD/路由同步。
- **API 真实化**：`/v1` 成功响应补 `X-RateLimit-Limit-Requests` / `X-RateLimit-Remaining-Requests`，
  并经 CORS `Access-Control-Expose-Headers` 暴露。
- **修复**：`RateLimiter.snapshot` 缺失导致 `/v1/chat/completions` 500（连接被重置）；
  `_send` 未合并限流头。单测 **50/50** 通过。

## 本轮（2026-10-16 早）安全修复记录

见 git 或 `README.md`「安全须知」。要点：Store 读操作加锁、`/v1` 跨域放行（key 鉴权无 CSRF 面）、
登录 `next` 防 open redirect、请求体 1 MiB 上限、`/api/usage` 参数容错、Origin 白名单补 `::1`、
`/api` 响应 `Cache-Control: no-store`。

## 本轮（2026-10-16 早）新增能力记录

- **多 worker 认领**：`deepsink_bridge.py`（O_EXCL 原子锁 + 超时抢占 + 快照）；`deepsink_brain.py` 的 `watch` 默认认领、新增 `release`、`status`、`--worker`、`--no-claim`。
- **队列观测**：`GET /api/bridge`（登录）+ 控制台「推理队列」卡片（待认领/处理中/已回复/活跃 worker）。
- **模型能力字段（A9）**：`/v1/models`、`/v1/models/{id}` 输出 `context_window` / `max_output_tokens` / `capabilities` / `description`。
