# AGENTS.md — DeepSink 操作手册（给 AI / 新窗口的第一份文档）

> **先读我。** 你在 `sim/` 目录里。这是用户在本机部署的 **DeepSink**——一台 **OpenAI + Anthropic 双协议兼容的 AI 服务**。
> 读完这一页，你就知道怎么把它跑起来、怎么接单。更细的说明见 [`README.md`](./README.md)；待办见 [`PLAN.md`](./PLAN.md)。

---

## 0. 一句话

**DeepSink** = 把本机变成一台完整的"AI 服务商"：官网 + 网页版对话 + 开放平台控制台 + 注册登录 + API key + 额度计费，
对外同时说 **OpenAI**（`/v1/chat/completions`）与 **Anthropic**（`/v1/messages`）两套协议。
任何只认这两套协议的客户端（MC 模组、SDK、工具）把 `base_url` 指过来，就能获得 AI 能力——**应答由谁提供，由后端决定**。

- 服务地址：`http://127.0.0.1:8788`
- 数据库：`~/DeepSink/deepsink.db`（SQLite，不在仓库里）
- 页面：`/`(官网) · `/chat`(对话) · `/platform`(控制台) · `/docs`(文档) · `/status`(状态) · `/login`

> ⚠️ **别混淆**：MC 里那个游戏角色叫 **`DeepSeek`**（游戏账号/OP/皮肤绑定的名字）——那是另一回事，**不要改**。
> 本项目内一切标识统一为 **`deepsink`**（文件名、类名、Cookie、模型名都是 `deepsink_*`）。

---

## 1. 3 分钟开跑

```bash
cd sim
python deepsink_server.py serve        # 默认 127.0.0.1:8788，后端取 deepsink.json:backend
```

打开 <http://127.0.0.1:8788/> → `/login` 注册（赠送 ¥10）→ `/platform` 建 API key → 用 curl/SDK 调用。

自检与常用 CLI：

```bash
python deepsink_server.py info                 # 打印 base_url / 账户 / 端点
python deepsink_server.py users                # 列出账户
python deepsink_server.py adduser --email a@b.c --password secret [--name 名] [--admin]
python deepsink_server.py passwd  --email a@b.c --password newsecret
python deepsink_server.py client "你好"         # 用首个账户的 key 打一发，验证链路
python test_deepsink.py                        # 全部单测（纯标准库）
```

---

## 2. 三种后端（谁来应答）

`deepsink.json:backend` 决定"谁来应答"：

| 值 | 谁在应答 | 用途 |
|---|---|---|
| **`bridge`（默认）** | **推理 worker**（AI 窗口读文件、写回复） | 本机 worker 接单。每单最长等 120s |
| `openai` | 真 OpenAI 兼容 API（如 DeepSeek） | 透传；key 读环境变量 `MCROBOT_LLM_KEY`，**不写进配置** |
| `echo` | 本地回显 | 自测/联调，不接任何外部 |

---

## 3. worker 接单标准流程（`bridge` 模式，最重要）

`bridge` 下，每个 chat 请求会被写成 `data/req/req_xxx.json`，**HTTP 连接阻塞等待**回复文件。
你用 `deepsink_brain.py` 收单 / 回单，闭合回路：

```bash
python deepsink_brain.py watch          # 阻塞等新请求；有则打印 JSON 退出(0)；超时退出(2)
python deepsink_brain.py read           # 非阻塞：看当前所有待处理
python deepsink_brain.py status         # 待认领/已认领/已回复统计
python deepsink_brain.py reply req_xxx "你要回的话"
python deepsink_brain.py reply req_xxx --file answer.txt
python deepsink_brain.py reply req_xxx --json '{"message":{"role":"assistant","content":"hi"}}'
```

**工作循环**（背下来）：

1. 起 `watch`（阻塞，后台跑）→ 被唤醒（**已自动认领该请求**）
2. 读它打印的 JSON：看 `messages` / `last_user`
3. `reply <id> "..."` 把答案写回
4. 立刻再起 `watch`，等下一次

**注意（时效）**：客户端真正等的是 `gateway.patience_ms`（默认 45s）——超过它服务就删掉请求文件、
改发兜底文案。所以**尽量在 45 秒内 reply**；更老的请求会被拒（`reply` 时提示"已过期"），
回它也没有客户端在读。`bridge.timeout_seconds`（120s）只是底层兜底下限。
`data/req/`、`data/res/` 是运行期目录，已 gitignore；请求闭环后 req/res 文件都会被删掉（队列不留影子）。

> 一旦把某条真实链路（如 MC 模组）接过来，就是完整的"客户端 → 协议栈 → 应答"闭环演示。

---

## 3.5 多 worker 并行（多个 CodeBuddy 窗口一起接单）

`watch` 默认会**认领（claim）**它返回的那条请求：在 `data/req/` 下原子创建一个
`req_xxx.lock`。认领用 `O_CREAT|O_EXCL`，**POSIX/Windows 都保证只有一个 worker 成功**——
所以**同时开 N 个窗口各跑一个 `watch`，它们会自动分工、不会重复回单**，吞吐直接翻倍。

```bash
# 窗口 A
python deepsink_brain.py watch --worker A        # 被唤醒即已认领 req_xxx
python deepsink_brain.py reply req_xxx "回复"     # 回单（自动清锁）
# 窗口 B（并行）
python deepsink_brain.py watch --worker B
```

- `--worker` 只是给人看的标识；不填则用 `host-pid`。
- 认领后若**放弃**：`python deepsink_brain.py release req_xxx`（或直接不 reply，等锁超时）。
- **锁超时**（`deepsink.json:bridge.claim_ttl_seconds`，默认 180s）后，该请求可被其它 worker
  抢占——这是 worker 崩溃/卡死的兜底，避免请求永远卡在"已被认领"。
- **孤儿锁自愈**：worker 在服务端放弃请求的同一瞬间抢单，会产生"请求已没了但锁还在"的孤儿锁；
  无害（没有请求可认领），`claim_next` 每次轮询都会扫掉，队列不留影子。
- 看队列现状：`python deepsink_brain.py status`（待认领 / 已认领 / 已回复 / 各 worker / **在线窗口**）；
  控制台 `/platform` 的「推理队列」卡片也会实时显示。
- 只想等待、不认领（旧行为，排查用）：`python deepsink_brain.py watch --no-claim`。

> 相关实现见 `deepsink_bridge.py`（认领/快照）。服务端公开 `GET /api/bridge`（需登录）给前端读快照。

### 3.5.1 窗口称呼：`<厂商>-<角色>-<序号>`，别用 PID

多个窗口（**可以是不同厂商的 AI**：CodeBuddy / WorkBuddy / 豆包……）并行接单时，
`--worker` 就是它在队列里的名字。约定：

| 窗口 | worker 名 |
|---|---|
| CodeBuddy 主控 | `codebuddy-main` |
| 第二个 CodeBuddy | `codebuddy-2` |
| WorkBuddy | `workbuddy-1` |
| 豆包网页 | `doubao-web-1` |

- **每次 `watch`/`reply` 都显式带 `--worker`**，名字跨重启稳定，`status`/锁文件/回单里都能追溯。
- **不要用默认的 `host-pid`**：watch 每重挂一次 PID 就变，事后没法回答"这单是谁回的"。
- PID 只作辅助：锁文件里记了 pid（`status` 可见），排查"这个窗口还活着吗"时 `ps` 对一下。
- 回单会自动带上 `_worker` 归属字段（服务端忽略它，但留痕有用）。

### 3.5.2 跨窗口沟通：黑板（say/msgs）+ 心跳（ping）

窗口之间没有消息通道，靠两个共享文件（都在 `data/` 下，见 `deepsink.json:bridge`）：

```bash
python deepsink_brain.py say "req_9 我在处理，长对话"   # 留言：自动带 worker 名 + 时间戳
python deepsink_brain.py msgs --tail 20                # 读最近 20 条（旧→新）
python deepsink_brain.py ping --worker 名               # 手动登记在线（一般不用）
```

- **黑板**（`data/msgs.jsonl`，追加式）：用来递话——"这单我接了"、"这单是数学题，豆包来"、
  "我重启了，手里的单谁接一下"。所有窗口随时可读。
- **心跳**（`data/workers.json`）：`watch` 挂上即 ping、**阻塞等待期间每 30s 续期**、`reply` 成功时也 ping；
  超过 `heartbeat_ttl_seconds`（默认 90s）没心跳的窗口视为已关。`status` 与
  `/api/bridge` 的 `online` 字段据此显示"在线窗口"。
- **不擅长就放手**：认领后发现接错了，`release req_xxx` + 黑板上喊一声，别人自然捡走。

---

## 3.6 网关行为（流式为什么"不等大脑"）

`deepsink.json:gateway` 控制流式响应的"手感"——目标是让客户端**像在跟真模型说话**：

| 参数 | 默认 | 作用 |
|---|---|---|
| `ttft_ms` | 400 | 首片前的假 prefill 延迟（真模型也是先愣一下才开口） |
| `ping_ms` | 3000 | 等大脑期间的保活间隔：OpenAI 发 SSE 注释行 `: ka`，Anthropic 发协议自带 `ping` 事件（都是真厂商同款手法） |
| `patience_ms` | 45000 | 等人肉大脑的上限；超过就删请求文件 + 改发兜底文案。**必须小于 `bridge.timeout_seconds`** |

流式请求的处理顺序（`_await_brain`）：**先开流发首片 → 后台线程等大脑、主线程周期性保活 →
大脑回复到了按 `stream.chunk_chars/delay_ms` 匀速流出 → finish + usage + [DONE]**。因此：

- 客户端**永远先看到"开始回了"**，不会干等；
- 大脑迟到超过 `patience_ms`，客户端拿到的是一句完整人话（兜底文案），不是错误；
- 超时的请求文件会被删掉，迟到的回单会失败（不会写成孤儿文件）；`reply` 超龄请求会被拒。

非流式请求没有"提前应声"的余地，但同样受 `patience_ms` 约束——到点直接返回兜底文案 JSON。

---

## 4. 铁律（不要违反）

1. **只绑 `127.0.0.1`**。`deepsink.json:host` 别改成 `0.0.0.0`——那会丢掉 Host 白名单 / Origin 校验的保护。
2. **页面绝不内嵌 API key**。密钥只在创建时返回一次；控制台一律走登录 Cookie（`deepsink_session`，HttpOnly + SameSite=Strict）。改页面时**别再加**把 key 写进 HTML 的占位符。
3. **命名一律 `deepsink_*`**：文件 `deepsink_server.py / deepsink_store.py / deepsink_bridge.py / deepsink_brain.py / deepsink.json / test_deepsink.py`；类 `DeepSinkServer`；前端键 `deepsink_sessions / deepsink_cur / deepsink-bg`；模型 `deepsink-chat / deepsink-reasoner`。
4. **别动 MC 里的游戏角色 `DeepSeek`**（见 §0）。
5. **改代码要跑测试**：`python test_deepsink.py`（改了协议/账户/安全行为就顺手补单测）。
6. **CSRF 面**：`/api/*`（Cookie 鉴权）保留 Origin 同源校验；`/v1/*`（API key 鉴权、无 Cookie）对浏览器开放 CORS。别把这条搞反。
7. **对外口径统一叫 DeepSink**：页面、文档、配置注释里不要出现"mock / 模拟 / 假 AI 公司 / 本机大脑"这类露馅表述；bridge 的应答方统一称"推理 worker"。

---

## 5. 常见任务速查

| 想做什么 | 怎么做 |
|---|---|
| 重启服务 | 停旧进程 → `python deepsink_server.py serve` |
| 换后端 | 改 `deepsink.json:backend`，或 `serve --backend echo` |
| 换端口 | `serve --port 9000`（同步改客户端 base_url） |
| 加账户 / 改密 | `adduser` / `passwd` 子命令，或网页 `/login`、`/platform` |
| 迁移旧账本 | 首次启动自动读 `deepsink.json:legacy_import_dir`（`data/`）→ 建 `local@deepsink` |
| 模型目录 | 改 `deepsink.json:models`（`/v1/models` 会跟着变） |
| 限流 / 登录节流 | `deepsink.json:rate_limit` / `login_throttle`（设 0 关闭） |
| 流式分片手感 | `deepsink.json:stream`（`chunk_chars` / `delay_ms`） |
| 流式网关手感 | `deepsink.json:gateway`（`ttft_ms` / `ping_ms` / `patience_ms`） |
| 并行接单（多 worker） | 多窗口各跑 `python deepsink_brain.py watch --worker 名`；称呼用 `厂商-角色-序号`（如 `codebuddy-main`/`doubao-web-1`），别用默认 host-pid；看队列 `deepsink_brain.py status` |
| 认领锁超时 | `deepsink.json:bridge.claim_ttl_seconds`（默认 180s，超时可被抢占） |
| 跨窗口递话 | `deepsink_brain.py say "..."` / `msgs --tail 20`（黑板 `data/msgs.jsonl`） |
| 窗口在线登记 | 自动：watch 启动/reply 成功即 ping；手动 `deepsink_brain.py ping`。保鲜期 `bridge.heartbeat_ttl_seconds`（默认 90s） |
| 跑测试 | `python test_deepsink.py` |

---

## 6. 排障

| 现象 | 排查 |
|---|---|
| 401 invalid api key | key 对不对/是否被禁用；`Authorization: Bearer` 或 `x-api-key` 二选一 |
| 402 insufficient_quota | 余额 ≤ 0，去 `/platform` 充值 |
| 429 | 触发限流（`rate_limit`）或登录节流（`login_throttle`）；看 `Retry-After` |
| 403 forbidden host / cross-origin | Host 不在 `allowed_hosts`；或对 `/api/*` 发了跨域写请求 |
| bridge 请求卡满 120s | 没人接单：另开终端跑 `deepsink_brain.py watch`；或队列见 `/api/bridge` |
| reply 提示"已过期"被拒 | 客户端等待上限是 `min(gateway.patience_ms, bridge.timeout_seconds)`，超龄请求无人认领，属正常保护 |
| 客户端看到 `: ka` / `ping` 但没有正文 | 那是网关在等大脑时的保活字节（SSE 注释/Anthropic ping），不是错——大脑回了就出正文 |
| 请求一直"处理中"没人回 | 认领它的 worker 挂了：等锁超时（`claim_ttl_seconds`）自动可抢，或 `deepsink_brain.py release req_xxx` |
| 窗口不在"在线窗口"列表 | 超过 `heartbeat_ttl_seconds`（90s）没 ping：watch 没挂或窗口关了；重挂 watch 会自动登记 |
| 页面 404 | 路径拼错；`/v1/v1/...` 会被自动折叠为 `/v1/...` |
| curl 传中文报 400 bad json | Git Bash 把中文按 GBK 塞进 body 了：用 `python` 写 UTF-8 的 body 文件，再 `curl --data-binary @file` 传 |
| 服务没起来 | 端口被占：换 `--port`；或旧进程还在（8788） |

---

## 7. 文档地图

- `README.md` — 完整说明（定位/启动/端点/账户/安全/测试/已知边界）
- `PLAN.md` — 待办与路线图
- `COORD.md` — **多窗口协作约定**（本机可能有别的 AI 窗口在弄同一项目：谁改源码、接单统一走带锁 watch、服务重启先打招呼）
- `/docs` 页面 — 面向"接入方"的协议与端点表
- `deepsink.json` 内每个字段都有 `_说明` 注释
- 记忆：本项目在 AI 记忆里的条目名为「DeepSink 本地 AI 服务」

---

## 8. 给下一个 AI 的提示

- 想验证链路：`serve`（另开一个终端）+ `client "你好"`（`echo` 后端秒回，`bridge` 需另起 `watch`）。
- 想真正"提供应答"：把 `backend` 设成 `bridge`，然后跑 §3 的循环。
- 改动前先 `git status` 看现状；**破坏性操作（删文件/改权限/停进程）先只读侦查 → 出清单 → 问用户**。
- 同一文件的多处编辑要**逐个串行**改完再验证落盘（并行编辑同文件曾互相覆盖）。
- 新增的必做动作 / 命令 / 坑，**回写这份 AGENTS.md 或 README**，让下一个窗口不用重学。
