# DeepSink — 本机部署的 AI 服务平台

把本机变成一台完整的 AI 服务平台：官网 + 网页版对话 + 开放平台控制台 + 注册登录 + API key + 额度计费，
对外同时说 **OpenAI** 与 **Anthropic** 两套协议。任何只认这两套协议的客户端（MC 模组、SDK、工具）
把 `base_url` 指过来就能获得 AI 能力——而**应答由谁提供**由后端决定。

> 架构上它是一台 **OpenAI 兼容 + Anthropic 兼容的 AI 服务**；`backend` 决定应答来源：
> 本机推理 worker（`bridge`）/ 上游真 API（`openai`）/ 本地回显（`echo`）。
> 接上真 API 后，它就是一台 **AI Gateway（反向代理）**。

> **给 AI / 新窗口**：先读 [`AGENTS.md`](./AGENTS.md)（操作手册：怎么开跑、怎么当推理 worker、有哪些铁律）；
> 待办与路线图见 [`PLAN.md`](./PLAN.md)。

---

## 快速开始

```bash
cd sim
python deepsink_server.py serve                 # 默认 http://127.0.0.1:8788
```

打开 <http://127.0.0.1:8788/>：

| 页面 | 路径 | 说明 |
|------|------|------|
| 官网 | `/` | 产品介绍、架构、定价、接入信息 |
| 状态 | `/status` | 服务状态、模型目录、协议、延迟自检 |
| 登录 / 注册 | `/login` | 邮箱 + 密码，注册赠送 ¥10 |
| 网页版对话 | `/chat` | 会话鉴权（`/api/chat`），**不暴露 key** |
| 开放平台 | `/platform` | 密钥管理、用量、流水、充值、会话管理 |
| 文档 | `/docs` | 双协议快速开始 + 端点表 |

首次启动会把旧的 JSON 账本（`sim/data/account.json`、`keys.json`、`apikey`）**自动迁入一个本机账户**
（`local@deepsink`），余额与密钥都保留。

---

## 后端（谁来应答）

`deepsink.json:backend`：

| 值 | 行为 |
|----|------|
| `bridge`（默认） | 把请求落成文件，等推理 worker 用 `deepsink_brain.py` 收单/回单。**每单最长等 120s** |
| `openai` | 透传到真正的 OpenAI 兼容 API（DeepSeek 等）。key 读环境变量 `MCROBOT_LLM_KEY`，**不写进配置文件** |
| `echo` | 本地回显，自测用 |

`bridge` 模式接单（另开一个终端）：

```bash
python deepsink_brain.py watch          # 阻塞认领一条；有则打印 JSON 退出（已自动认领）
python deepsink_brain.py reply <req_id> "你要回的话"
python deepsink_brain.py status         # 队列快照：待认领/处理中/已回复 + 各 worker + 在线窗口
python deepsink_brain.py say "给别的窗口递句话"   # 黑板留言（跨窗口沟通）
python deepsink_brain.py msgs --tail 20          # 读黑板最近 20 条
```

**可并排开多个 worker 提升吞吐**（多个窗口，可以是不同厂商的 AI）：认领（`req_xxx.lock`）用
`O_CREAT|O_EXCL` 原子创建，同一条请求只会被一个 worker 接走；锁超时
（`bridge.claim_ttl_seconds`，默认 180s）后可被抢占，兜底 worker 崩溃。

- **称呼规范**：`--worker <厂商>-<角色>-<序号>`（如 `codebuddy-main` / `doubao-web-1`），
  每次 watch/reply 都显式带；别用默认的 `host-pid`（重启就变，没法追溯归属）。
- **沟通**：黑板 `data/msgs.jsonl`（`say`/`msgs`）；心跳 `data/workers.json`
  （`watch` 启动/`reply` 成功自动 ping，`status` 显示"在线窗口"）。
- **不擅长就放手**：`release <id>` + 黑板上喊一声，别人捡走。

方案见 [`AGENTS.md`](./AGENTS.md) §3.5、实现见 `deepsink_bridge.py`。

---

## 流式网关行为

流式响应**不等大脑回复**就先到客户端（`deepsink.json:gateway`）：

| 参数 | 默认 | 作用 |
|---|---|---|
| `ttft_ms` | 400 | 首片前的假 prefill 延迟（真模型也是先愣一下才开口） |
| `ping_ms` | 3000 | 等大脑期间的保活间隔：OpenAI 发 SSE 注释行 `: ka`，Anthropic 发协议自带 `ping` 事件 |
| `patience_ms` | 45000 | 等人肉大脑的上限；超过就删请求文件 + 改发兜底文案。**必须小于 `bridge.timeout_seconds`** |

处理顺序：**先开流发首片 → 后台线程等大脑、主线程周期性保活 → 回复到了按
`stream.chunk_chars/delay_ms` 匀速流出 → finish + usage + [DONE]**。客户端永远先看到
"开始回了"，不会干等；大脑迟到超过耐心上限，客户端拿到的是一句完整人话而不是错误。
非流式请求同样受 `patience_ms` 约束，到点直接返回兜底文案 JSON。

---

## 协议与端点

**OpenAI**
```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"model":"deepsink-chat","messages":[{"role":"user","content":"你好"}]}'
```

**Anthropic**
```bash
curl http://127.0.0.1:8788/v1/messages \
  -H "x-api-key: <key>" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
  -d '{"model":"deepsink-chat","max_tokens":256,"messages":[{"role":"user","content":"你好"}]}'
```

- `/v1/*` 用 **API key** 鉴权（`Authorization: Bearer` 或 `x-api-key`），计费记在 key 的归属账户上。
- `/api/*` 用**登录会话**（`deepsink_session` Cookie），且按账户隔离。
- 误配的 `/v1/v1/...` 会被自动折叠成 `/v1/...`。

**对齐主流平台的协议细节**

- **请求 ID**：每个响应带 `X-Request-Id`，排障可对账。
- **限流余量**：`/v1` 成功响应带 `X-RateLimit-Limit-Requests` / `X-RateLimit-Remaining-Requests`，
  超限返回 `429 rate_limit_error` + `Retry-After`（均通过 CORS 暴露给浏览器）。
- **错误信封**：OpenAI 形如 `{error:{message,type,param,code}}`；Anthropic 形如 `{type:error,error:{type,message}}`，
  并用 `authentication_error` / `rate_limit_error` / `insufficient_quota` 等标准 type。
- **真流式**：`stream:true` 时按片（`deepsink.json:stream`）逐块推送 delta，而非整段落一次。
- **流式网关**（`deepsink.json:gateway`）：请求一到**先开流发首片**（`ttft_ms` 假 prefill 延迟），
  等应答的期间周期性保活——OpenAI 发 SSE 注释行 `: ka`、Anthropic 发协议自带 `ping` 事件；
  应答到达后按片匀速流出，`finish_reason` + `usage` + `[DONE]` 收尾。
  超过 `patience_ms`（默认 45s）仍无应答：删掉该请求（迟到的回单会失败，不产孤儿文件）并改发
  兜底文案——**客户端永远在耐心内得到一句完整回复，而不是干等或报错**。
- **CORS**：仅 `/v1/*` 对浏览器开放（`Access-Control-Allow-Origin: *` + 预检）；控制台 `/api/*` 维持同源锁定。
- **模型目录**：`deepsink.json:models` 定义多模型；`GET /v1/models` 列出、`GET /v1/models/{id}` 取单个，
  并透传可选能力字段 `context_window` / `max_output_tokens` / `capabilities` / `description`。

### 控制台接口（需登录）

`POST /api/auth/register|login|logout`、`GET /api/me`、`POST /api/chat`、
`GET /api/account|keys|usage|ledger|stats|sessions|bridge`、`POST /api/account/password`、
`POST /api/sessions/revoke`、`POST /api/keys`、`POST /api/keys/toggle|delete`、`POST /api/recharge`

`GET /api/bridge` 返回推理队列快照（待认领 / 处理中 / 已回复 / 各 worker / **在线窗口**），
控制台「推理队列」卡片用它。

---

## 账户与计费

- 邮箱 + 密码注册；密码 **PBKDF2-SHA256 加盐**存储，不落明文。
- 网页控制台用 **HttpOnly + SameSite=Strict** Cookie 会话，默认 7 天。
- 每个账户只能看到自己的密钥、余额、用量、流水。
- 支持**网页内改密码**（改密后踢出其它登录设备）与**会话列表/踢出**；登录失败有节流。
- 计费：输入 ¥0.002/1K tok、输出 ¥0.008/1K tok；token 用「字符数 ÷ 1.6」粗估。
  余额 ≤ 0 时接口返回 **402 insufficient_quota**。所有变动记入 `ledger`（注册赠送 / 充值 / 消费）。

**数据库**：`~/DeepSink/deepsink.db`（SQLite，单文件）。

```bash
python deepsink_server.py users                                   # 列出账户
python deepsink_server.py adduser --email a@b.c --password secret --name 小明 [--admin]
python deepsink_server.py passwd  --email a@b.c --password newsecret
python deepsink_server.py info                                    # 打印 base_url / 账户 / 端点
python deepsink_server.py client "你好" [--stream] [--key sk-...]
```

---

## 安全须知 ⚠

本服务本地部署，默认只绑 `127.0.0.1`，并做了多层加固：

| 措施 | 作用 |
|------|------|
| Host 白名单（`deepsink.json:allowed_hosts`） | 挡 **DNS rebinding**——恶意网页把域名解析到 127.0.0.1 也无法冒充同源 |
| Origin 同源校验 | 跨域写请求一律 **403**，挡 **CSRF** |
| `OPTIONS` 返回 204 且不下发 CORS 许可 | 浏览器跨域预检直接失败（仅 `/v1/*` 例外，那是给客户端用的） |
| 页面不内嵌 key | 密钥只在创建时返回一次；控制台接口全部要求登录 |
| 页面安全响应头 | `CSP` / `X-Frame-Options: DENY` / `Permissions-Policy` |
| 登录节流 | 同一邮箱连续失败多次后临时锁定，返回 **429** |
| 请求体上限 1 MiB | 挡超大 body 撑爆内存 |

**不要把 `host` 改成 `0.0.0.0`** 暴露到局域网/公网——那会让上面这些保护的意义大打折扣。
若要对外共享，请自行加反向代理 + TLS + 真鉴权。

---

## 目录结构

```
sim/
├── AGENTS.md            # 给 AI/新窗口的操作手册（先读它）
├── PLAN.md              # 开发计划 / 待办
├── COORD.md             # 多窗口协作约定
├── deepsink_server.py   # 服务本体（双协议 + 账户 + 计费 + 安全守卫）
├── deepsink_store.py    # 账本层（SQLite：users/sessions/api_keys/usage/ledger）
├── deepsink_bridge.py   # 推理队列：多 worker 认领锁 + 快照 + 黑板/心跳
├── deepsink_brain.py    # bridge 后端：worker 侧收单/回单/沟通（watch/reply/status/say/msgs/ping）
├── deepsink.json        # 配置
├── test_deepsink.py     # 单测
├── web/                 # 前端（index/status/login/chat/platform/docs + assets）
└── data/                # 运行期文件（bridge 队列、日志），已 gitignore
```

数据库不在仓库内，位于 `~/DeepSink/deepsink.db`。

---

## 测试

```bash
cd sim && python test_deepsink.py
```

覆盖：双协议（含 SSE 流式分片）、`/v1/v1` 容错、HEAD、`X-Request-Id`、限流响应头、`/v1` CORS、
错误信封、限流 429、`/v1/models/{id}`、模型能力字段、`max_tokens` 截断、注册/登录/登出、改密踢会话、
会话列表、登录节流、管理接口鉴权、多用户隔离、计费与 402、Host 白名单、跨域拒答、OPTIONS、
页面不内嵌 key、页面安全头、bridge 收/回单与超时兜底、
**多 worker 认领（原子互斥 / 锁超时抢占 / 回单清锁 / 孤儿锁自愈 / `/api/bridge`）**、
**跨窗口协作（黑板 say/msgs、心跳 ping/在线判定、锁带 pid、回单归属）**、
**队列卫生（complete 超时 / 客户端断开均清理请求文件与认领锁）**。

---

## 已知边界

- **不是给 agent 用的**：真实 agent 客户端会带工具定义、要求多轮 `tool_use` 循环，
  `bridge` 每单要 worker 接，撑不住完整流程。单轮问答可用；agent 场景请接真 API。
- **token 是粗估**：`字符数 ÷ 1.6`，不是真分词器。
- **`bridge` 模式会阻塞**最长 120 秒（`bridge.timeout_seconds`）后才回兜底文案。
