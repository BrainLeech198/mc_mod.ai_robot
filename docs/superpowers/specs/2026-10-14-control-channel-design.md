# 控制通道设计（ControlMod 本地 HTTP 桥）

日期：2026-10-14
状态：待审阅
上游设计：`docs/superpowers/specs/2026-10-13-mc-robot-design.md`（§5 控制模组、§6 编排层、§1 操作安全规范）

## 1. 目标

让**外部进程**（编排层 / 当前阶段的 Claude Code）连上**运行中的真客户端**，程序化地：

- 下行：发送游戏指令（`/yzts ...`、后续的 `//...` WorldEdit 指令）、发送聊天
- 上行：读回游戏状态（是否在游戏中、玩家坐标/维度、最近的聊天栏文本）

这是「WE 建造流程」的**传输层前置**：先把「外面能操控这个玩家」的闭环打通，WE 语义与拦截才有落脚点。

成功标准：

1. 客户端进服后，外部执行一条指令 → 游戏内真实生效（可在聊天栏/世界观察到）
2. 外部能读回玩家的实时坐标与维度
3. 外部能读到客户端收到的服务端聊天（含登录回执这类 `[CHAT]` 行）
4. 仅本机可访问；非本机/浏览器跨站请求被挡

## 2. 非目标（YAGNI，本块不做）

- **不做 WE 语义与拦截层**：越界拒绝、黑名单指令、撤回记账属下一块（需要 `workspace.json` 边界 + WE 语义）。本块的指令通道**不拦截**，唯一客户端是编排层自己 —— 明确记为已知缺口（见 §7）。
- **不做双向实时 WebSocket**：用 HTTP + 轮询。`runtime.json` 预留的 `ws_port` 本块停用（见 §9）。
- 不做多客户端/多账号管理、不做鉴权用户体系、不做持久化历史。

## 3. 架构

```
编排层 / Claude Code（tools/control_client.py）
      ↓ HTTP（仅 127.0.0.1:17890）
ControlMod 内嵌 HttpServer（JDK com.sun.net.httpserver）
      ↓ 切主线程（Minecraft.getInstance().execute）
游戏动作（connection.sendCommand / sendChat / 读 player 状态）
      ↑ 聊天事件（ClientChatReceivedEvent）→ ChatLog 环形缓冲
```

- 服务器由模组在**客户端**启动，随 JVM 退出而消失；不写磁盘状态（除 token 文件）。
- 无新依赖：HTTP 用 JDK 自带 `com.sun.net.httpserver`；JSON 用 Forge 已捆绑的 Gson（`com.google.gson`）。

## 4. 数据契约（HTTP API）

所有请求/响应为 `application/json; charset=utf-8`。

| 方法 | 路径 | 请求 | 响应（200） |
|---|---|---|---|
| GET | `/health` | — | `{"ok":true,"inGame":<bool>,"player":<str|null>}` |
| GET | `/state` | — | `{"ok":true,"inGame":bool,"player":{"name":str,"x":num,"y":num,"z":num,"dim":str}\|null,"seq":int,"chats":[{"seq":int,"text":str}]}` |
| GET | `/chat?since=<seq>` | 查询参数 `since`（整数，缺省 0） | `{"ok":true,"seq":int,"chats":[{"seq":int,"text":str}]}` |
| POST | `/command` | `{"command":"//set stone"}` | `{"ok":true}` |
| POST | `/chat` | `{"text":"hi"}` | `{"ok":true}` |

**注意**：`/health`、`/state` 在「不在游戏中」时仍返回 **200**，只是 `inGame:false`、`player:null` —— 这是正常状态查询，不是错误。

错误响应：始终 `{"ok":false,"error":"<原因>"}`，配 HTTP 状态码：

| 码 | 触发 |
|---|---|
| 400 | 请求体非法/缺字段；`/command` 的 command 不以 `/` 开头 |
| 401 | 缺 `X-Control-Token` 或 token 不匹配 |
| 404 | 未知路径 |
| 405 | 方法不允许 |
| 409 | **仅 `/command` `/chat`**：不在游戏中（`mc.player == null`），无法执行 |
| 503 | 主线程动作超时未完成 |

**指令语义**：`/command` 收到的是**玩家会原样输入的东西**（含前导斜杠），例如 `//set stone`、`/yzts login x`。ControlMod **去掉恰好一个前导斜杠**后交给 `connection.sendCommand(...)`（与 vanilla `LocalPlayer.chat` 对 `/xx` 的处理一致：`//set` → 包内 `/set`，`/yzts` → 包内 `yzts`）。这是后续 WE 指令共用的通道。

`/chat` 发送普通聊天（非指令）。

## 5. 组件与文件结构

### Java（`controlmod/src/main/java/com/mcrobot/controlmod/control/`）

- `GameActions.java` —— 游戏动作接口（**约定在主线程调用**）：
  ```java
  public interface GameActions {
      PlayerState playerState();            // 不在游戏中返回 null
      void sendCommand(String commandWithoutSlash);
      void sendChat(String text);
  }
  ```
- `PlayerState.java` —— `record PlayerState(String name, double x, double y, double z, String dim)`
- `GameBridge.java` —— 把动作切到主线程并回传结果。可测（注入 `MainThread` 与 `GameActions`）：
  ```java
  public class GameBridge {
      public interface MainThread { void execute(Runnable r); }   // 生产：Minecraft.getInstance()::execute
      public GameBridge(GameActions actions, MainThread mainThread, long timeoutMs);
      public CompletableFuture<PlayerState> playerState();
      public CompletableFuture<Void> sendCommand(String commandWithoutSlash);
      public CompletableFuture<Void> sendChat(String text);
  }
  ```
  实现：建 `CompletableFuture` → `mainThread.execute(...)` 内 try/catch 完成或异常完成；调用方（HTTP 线程）`.get(timeoutMs)`，超时抛 `TimeoutException` → 503。
- `ChatLog.java` —— 定长环形缓冲 + 单调序号。可测：
  ```java
  public class ChatLog {
      public ChatLog(int capacity);                 // 建议 200
      public void append(String text);              // 主线程调用；分配 seq
      public Snapshot since(long seq);              // 任意线程；synchronized
      public long latestSeq();
      public record Entry(long seq, String text) {}
      public record Snapshot(long seq, java.util.List<Entry> entries) {}
  }
  ```
- `ControlServer.java` —— HttpServer + 路由 + 鉴权 + JSON 编解码。可测（端口传 0 取随机端口）：
  ```java
  public class ControlServer {
      public ControlServer(int port, String token, GameBridge bridge, ChatLog chatLog);
      public void start() throws java.io.IOException;
      public void stop();
      public int port();    // 实际绑定端口
  }
  ```
- `ControlToken.java` —— 读或生成 token：
  ```java
  public final class ControlToken {
      public static String loadOrCreate(java.nio.file.Path gameDir); // gameDir/control_token，缺则生成 32 位 hex 写入
  }
  ```
- `MinecraftGameActions.java` —— `GameActions` 的 Forge 实现，使用 `Minecraft.getInstance()`；`player == null` 时 `playerState()` 返回 null，`sendCommand/sendChat` 抛 `IllegalStateException`（由桥转成 409/503）。

### Java 改动（既有文件）

- `ControlMod.java`：
  - 构造器：由 Forge 注入 `IEventBus modEventBus`；`if (FMLEnvironment.dist != Dist.CLIENT) return;`（**新增 dist 守卫**，修掉此前 deferred 的 #7）→ `modEventBus.addListener(this::onClientSetup)` → `MinecraftForge.EVENT_BUS.register(this)`（聊天事件）。
  - `onClientSetup(FMLClientSetupEvent)`：此时 `Minecraft.getInstance()` 与 `gameDirectory` 已可用 → `String token = ControlToken.loadOrCreate(gameDir)` → `new ControlServer(PORT, token, bridge, chatLog).start()`（捕获 `IOException` 记日志、**不崩游戏**）。
  - 端口常量 `PORT = 17890`（与 `runtime.json.control_mod.http_port` 一致；模组侧硬编码，注释指向 runtime.json）。
  - `onChat`：`chatLog.append(event.getMessage().getString())`，**再**转发给 `autoLogin`（既有自动登录行为不变）。
- `runtime.json`：`control_mod` 段更新为 `{"http_port": 17890}`，删 `ws_port`（本块停用）。

### Python（`tools/`）

- `control_client.py` —— 最小编排客户端（stdlib `urllib`，无新依赖）：
  - `_endpoint()`：读 `version/1.20.1-forge/runtime.json` 取 `http_port`；读 `instance/control_token` 取 token。
  - 函数：`health()`, `state()`, `chat(text)`, `cmd(command)`，返回解析后的 dict；HTTP 错误抛带原因的异常。
  - CLI（argparse）：`python tools/control_client.py health|state|chat <text>|cmd <command>`。
- `tools/test_control_client.py` —— 用 stdlib `unittest` + 本地线程内 stub HTTP server 验证：请求头/token 注入、URL 构造、错误响应解析、连接失败提示。

## 6. 线程模型

- HttpServer 的 handler 跑在各自的线程（非主线程）。
- 任何触碰游戏状态的动作（发指令/聊天、读坐标）由 `GameBridge` 通过 `MainThread.execute` 切到客户端主线程。
- 结果用 `CompletableFuture` 从主线程回传；HTTP 线程 `.get(timeout)`（默认 5s）等待。
- `ChatLog` 由主线程写、HTTP 线程读 → 方法 `synchronized`。
- 服务器经 `HttpServer.setExecutor(Executors.newFixedThreadPool(n))` 或默认执行器；不占用主线程。

## 7. 安全

- **只监听 `127.0.0.1`**（`InetSocketAddress("127.0.0.1", port)`），不对外暴露。
- **token 鉴权**：请求须带 `X-Control-Token`，与 `gameDir/control_token` 一致，否则 401。防本机其它程序误控 & 浏览器跨站（CSRF）。
- **要求 `Content-Type: application/json`** 且**拒绝带 `Origin` 头的请求**（浏览器跨站会带 `Origin`）—— 进一步挡网页类 CSRF。
- **密码与敏感信息不得出现在 HTTP 响应或日志**（沿用上游 §1 硬约束）。指令回显只到「已发送」级别，不回显参数内容。
- **已知缺口（下一块补）**：本块指令通道**无越界/黑名单拦截**。上游 §1 的强制拦截依赖后续 WE 块的拦截层；本块期间唯一客户端是编排层（受控）。**必须在 WE 块落地后才允许对外放开**。

## 8. 错误处理

| 情况 | 行为 |
|---|---|
| 端口被占 | 记日志警告，不启动服务器，**不崩游戏** |
| 请求体非法 | 400 + 原因 |
| 不在游戏中 | 409 |
| 主线程动作超时 | 503 |
| 客户端已被关闭 | 外部 HTTP 连接被拒 → 客户端侧无需处理 |

## 9. 与既有部分的衔接

- 与自动登录**不冲突**：`onChat` 先 `chatLog.append`，再走既有 `autoLogin.onChat`。自动登录行为不变。
- `instance/control_token` 落在 gitignore 覆盖的 `instance/` 内，不入库。
- 指令通道的「去一个前导斜杠」逻辑与现有 `AutoLogin` 的 `sendCommand` 适配一致，抽成共用点。

## 10. 测试策略

- **Java 单测**（`gradle test`，无需真实 MC）：
  - `ChatLog`：容量溢出淘汰最旧、`since` 语义、`latestSeq`。
  - `GameBridge`：注入直接执行的 `MainThread` + 假 `GameActions`；正常回传、动作抛异常 → 异常完成、超时路径。
  - `ControlServer`：绑端口 0，用 `java.net.http.HttpClient` 打全部路由：健康、状态、指令、聊天、`/chat?since`、401（错/缺 token）、400（非法体、command 无斜杠）、404、405、409（假 actions 返回 null）。
  - `ControlToken`：缺失时生成并落盘、存在时复用。
- **Python 单测**（`unittest` + stub server）：请求构造、token 头、错误解析、连接失败。
- **真机 e2e**（控制器执行，需服务器在线）：进服后 `control_client.py state` 返回坐标；`control_client.py cmd "/say mcrobot 上线测试"` 或 `//pos1` → 读回聊天确认生效。

## 11. 待定 / 后续（非本块）

- WE 语义与拦截层（越界/黑名单）、撤回记账（上限 10、剩 2 停手）—— 下一块。
- 编排层的「WE 操作序列」生成与下发 —— 再下一块。
- SSE/长轮询替换轮询（如轮询开销成为问题）。
- 双层防护第 1 层 OPAC（服务端，用户加装）。
