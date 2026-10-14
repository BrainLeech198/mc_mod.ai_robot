# 控制通道（ControlMod 本地 HTTP 桥）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让外部进程通过 `127.0.0.1:17890` 把指令/聊天下发给运行中的真客户端，并读回玩家状态与聊天回执。

**Architecture:** ControlMod 内嵌 JDK `com.sun.net.httpserver`，HTTP handler 线程经 `GameBridge` 把动作切到客户端主线程执行（`Minecraft.getInstance().execute`），结果用 `CompletableFuture` 回传；聊天经 `ClientChatReceivedEvent` 写入 `ChatLog` 环形缓冲供上行读取。JSON 用 Forge 已捆绑的 Gson。编排侧是最小 Python 客户端 `tools/control_client.py`。

**Tech Stack:** Java 17 / Forge 47.4.16 / 1.20.1；JDK `com.sun.net.httpserver`；Gson 2.10（已随 Minecraft 提供，无新依赖）；Python 3 stdlib（`urllib`、`unittest`）。

**Spec:** `docs/superpowers/specs/2026-10-14-control-channel-design.md`（执行前通读；本计划的每个任务都从该 spec 推导）

## Global Constraints

- 只跑客户端：`FMLEnvironment.dist == Dist.CLIENT` 之外不启动任何东西。模组 id `mcrobot_control`，包 `com.mcrobot.controlmod`。
- 只监听 `127.0.0.1`，端口 `17890`（与 `version/1.20.1-forge/runtime.json` 的 `control_mod.http_port` 一致）。
- token 鉴权：`X-Control-Token` 须等于 `gameDir/control_token`；POST 须 `Content-Type: application/json`；带 `Origin` 头的请求拒绝。
- 密码与敏感信息不得出现在任何 HTTP 响应或日志（沿用设计 §1 硬约束）。
- **本块不做指令拦截**（越界/黑名单/撤回属下一块）；通道只对编排层开放。
- 无新依赖：JDK HttpServer + Gson；Python 只用 stdlib。
- 构建/测试：`cd controlmod && ./gradlew test --console=plain`（**无需** 设 `JAVA_HOME`，工具链路径已在 `gradle.properties`）。部署：`cp build/libs/mcrobot_control-0.1.0.jar ../instance/mods/`。
- 提交只 `git add` 明确文件；`instance/` 与 `.superpowers/` 不入 git。

## Review Focus

1. **本机/浏览器跨站请求能否驱动 bot** —— 由 token 校验 + `Content-Type` + 拒绝 `Origin` 覆盖（Task 3 测试）。
2. **切主线程动作超时/阻塞** —— 由 `GameBridge` 的 `CompletableFuture` + 超时 → 503 覆盖（Task 2/3 测试）。
3. **聊天缓冲的并发读写**（主线程写、HTTP 线程读）—— 由 `ChatLog` 的 `synchronized` 覆盖（Task 1）。
4. **不在游戏中/未连服时查询不应崩** —— `/health`、`/state` 返回 200 `inGame:false`；`/command`、`/chat` 返回 409（Task 3 测试）。
5. **端口被占用时不崩游戏** —— `ControlMod` 捕获 `IOException` 记日志（Task 4）。

---

### Task 1: ChatLog（聊天环形缓冲）

**Files:**
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/control/ChatLog.java`
- Test: `controlmod/src/test/java/com/mcrobot/controlmod/control/ChatLogTest.java`

**Interfaces:**
- Consumes: 无
- Produces: `ChatLog(int capacity)`、`void append(String)`、`long latestSeq()`、`Snapshot since(long seq)`；`record Entry(long seq, String text)`、`record Snapshot(long seq, List<Entry> entries)`

- [ ] **Step 1: 写失败测试**

`controlmod/src/test/java/com/mcrobot/controlmod/control/ChatLogTest.java`：

```java
package com.mcrobot.controlmod.control;

import org.junit.jupiter.api.Test;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;

class ChatLogTest {
    @Test void emptyLog() {
        ChatLog log = new ChatLog(3);
        assertEquals(0, log.latestSeq());
        assertTrue(log.since(0).entries().isEmpty());
    }

    @Test void assignsIncreasingSeqInOrder() {
        ChatLog log = new ChatLog(3);
        log.append("a");
        log.append("b");
        assertEquals(2, log.latestSeq());
        List<ChatLog.Entry> e = log.since(0).entries();
        assertEquals(2, e.size());
        assertEquals(1, e.get(0).seq());
        assertEquals("a", e.get(0).text());
        assertEquals(2, e.get(1).seq());
        assertEquals("b", e.get(1).text());
    }

    @Test void sinceFiltersBySeq() {
        ChatLog log = new ChatLog(3);
        log.append("a");
        log.append("b");
        List<ChatLog.Entry> e = log.since(1).entries();
        assertEquals(1, e.size());
        assertEquals("b", e.get(0).text());
        assertEquals(2, log.since(1).seq());
    }

    @Test void evictsOldestWhenFull() {
        ChatLog log = new ChatLog(2);
        log.append("a");
        log.append("b");
        log.append("c");
        assertEquals(3, log.latestSeq());
        List<ChatLog.Entry> e = log.since(0).entries();
        assertEquals(2, e.size());
        assertEquals("b", e.get(0).text());
        assertEquals("c", e.get(1).text());
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd controlmod && ./gradlew test --tests '*ChatLogTest' --console=plain`
Expected: 编译失败 / FAIL —— `ChatLog` 不存在。

- [ ] **Step 3: 实现 ChatLog**

`controlmod/src/main/java/com/mcrobot/controlmod/control/ChatLog.java`：

```java
package com.mcrobot.controlmod.control;

import java.util.ArrayList;
import java.util.List;

/** 定长环形聊天缓冲。主线程 append，任意线程 since（synchronized）。 */
public class ChatLog {
    public record Entry(long seq, String text) {}
    public record Snapshot(long seq, List<Entry> entries) {}

    private final int capacity;
    private final Entry[] ring;
    private long nextSeq = 1;
    private int size = 0;
    private int head = 0;

    public ChatLog(int capacity) {
        if (capacity < 1) throw new IllegalArgumentException("capacity must be >= 1");
        this.capacity = capacity;
        this.ring = new Entry[capacity];
    }

    public synchronized void append(String text) {
        ring[head] = new Entry(nextSeq++, text);
        head = (head + 1) % capacity;
        if (size < capacity) size++;
    }

    public synchronized long latestSeq() {
        return nextSeq - 1;
    }

    public synchronized Snapshot since(long seq) {
        List<Entry> out = new ArrayList<>();
        int start = ((head - size) % capacity + capacity) % capacity;
        for (int i = 0; i < size; i++) {
            Entry e = ring[(start + i) % capacity];
            if (e.seq() > seq) out.add(e);
        }
        return new Snapshot(latestSeq(), out);
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd controlmod && ./gradlew test --tests '*ChatLogTest' --console=plain`
Expected: PASS（4/4）。

- [ ] **Step 5: 提交**

```bash
git add controlmod/src/main/java/com/mcrobot/controlmod/control/ChatLog.java controlmod/src/test/java/com/mcrobot/controlmod/control/ChatLogTest.java
git commit -m "feat: 控制通道聊天环形缓冲 ChatLog"
```

---

### Task 2: PlayerState / GameActions / NotInGameException / GameBridge

**Files:**
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/control/PlayerState.java`
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/control/GameActions.java`
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/control/NotInGameException.java`
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/control/GameBridge.java`
- Test: `controlmod/src/test/java/com/mcrobot/controlmod/control/GameBridgeTest.java`

**Interfaces:**
- Consumes: 无
- Produces:
  - `record PlayerState(String name, double x, double y, double z, String dim)`
  - `interface GameActions { PlayerState playerState(); void sendCommand(String commandWithoutSlash); void sendChat(String text); }`（约定在主线程调用）
  - `class NotInGameException extends RuntimeException`
  - `class GameBridge { interface MainThread { void execute(Runnable r); } GameBridge(GameActions, MainThread, long timeoutMs); CompletableFuture<PlayerState> playerState(); CompletableFuture<Void> sendCommand(String); CompletableFuture<Void> sendChat(String); <T> T await(CompletableFuture<T>) throws InterruptedException, ExecutionException, TimeoutException; }`

- [ ] **Step 1: 写失败测试**

`controlmod/src/test/java/com/mcrobot/controlmod/control/GameBridgeTest.java`：

```java
package com.mcrobot.controlmod.control;

import org.junit.jupiter.api.Test;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicReference;
import static org.junit.jupiter.api.Assertions.*;

class GameBridgeTest {
    static class FakeActions implements GameActions {
        PlayerState state = new PlayerState("DeekSeek", 1.5, 64.0, 2.5, "minecraft:overworld");
        final List<String> commands = new ArrayList<>();
        final List<String> chats = new ArrayList<>();
        boolean inGame = true;
        public PlayerState playerState() { return inGame ? state : null; }
        public void sendCommand(String c) { if (!inGame) throw new NotInGameException(); commands.add(c); }
        public void sendChat(String t) { if (!inGame) throw new NotInGameException(); chats.add(t); }
    }

    private static final GameBridge.MainThread DIRECT = Runnable::run;

    @Test void returnsPlayerState() throws Exception {
        GameBridge b = new GameBridge(new FakeActions(), DIRECT, 1000);
        assertEquals("DeekSeek", b.await(b.playerState()).name());
    }

    @Test void playerStateNullWhenNotInGame() throws Exception {
        FakeActions a = new FakeActions(); a.inGame = false;
        GameBridge b = new GameBridge(a, DIRECT, 1000);
        assertNull(b.await(b.playerState()));
    }

    @Test void sendCommandRunsAction() throws Exception {
        FakeActions a = new FakeActions();
        GameBridge b = new GameBridge(a, DIRECT, 1000);
        b.await(b.sendCommand("/set stone"));
        assertEquals(List.of("/set stone"), a.commands);
    }

    @Test void actionExceptionPropagates() {
        FakeActions a = new FakeActions(); a.inGame = false;
        GameBridge b = new GameBridge(a, DIRECT, 1000);
        ExecutionException ex = assertThrows(ExecutionException.class,
                () -> b.await(b.sendCommand("//set stone")));
        assertInstanceOf(NotInGameException.class, ex.getCause());
    }

    @Test void runsOnProvidedMainThread() throws Exception {
        AtomicReference<String> seen = new AtomicReference<>();
        GameActions a = new GameActions() {
            public PlayerState playerState() { seen.set(Thread.currentThread().getName()); return null; }
            public void sendCommand(String c) {}
            public void sendChat(String t) {}
        };
        ExecutorService ex = Executors.newSingleThreadExecutor(r -> new Thread(r, "fake-main"));
        try {
            GameBridge b = new GameBridge(a, ex::execute, 2000);
            b.await(b.playerState());
        } finally {
            ex.shutdownNow();
        }
        assertEquals("fake-main", seen.get());
    }

    @Test void timesOutWhenMainThreadNeverRuns() {
        GameBridge.MainThread never = r -> { /* drop */ };
        GameBridge b = new GameBridge(new FakeActions(), never, 100);
        assertThrows(TimeoutException.class, () -> b.await(b.playerState()));
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd controlmod && ./gradlew test --tests '*GameBridgeTest' --console=plain`
Expected: 编译失败 / FAIL —— `GameBridge`/`GameActions`/`PlayerState`/`NotInGameException` 不存在。

- [ ] **Step 3: 实现四个类**

`controlmod/src/main/java/com/mcrobot/controlmod/control/PlayerState.java`：

```java
package com.mcrobot.controlmod.control;

public record PlayerState(String name, double x, double y, double z, String dim) {}
```

`controlmod/src/main/java/com/mcrobot/controlmod/control/NotInGameException.java`：

```java
package com.mcrobot.controlmod.control;

/** 客户端不在游戏世界中（player == null）时由 GameActions 抛出。 */
public class NotInGameException extends RuntimeException {
    public NotInGameException() {
        super("not in game");
    }
}
```

`controlmod/src/main/java/com/mcrobot/controlmod/control/GameActions.java`：

```java
package com.mcrobot.controlmod.control;

/** 游戏动作。实现方约定：所有方法都在客户端主线程被调用。 */
public interface GameActions {
    /** 不在游戏中返回 null。 */
    PlayerState playerState();
    /** commandWithoutSlash 形如 "yzts login x" 或 "/set stone"（已去掉一个前导斜杠）。 */
    void sendCommand(String commandWithoutSlash);
    void sendChat(String text);
}
```

`controlmod/src/main/java/com/mcrobot/controlmod/control/GameBridge.java`：

```java
package com.mcrobot.controlmod.control;

import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.function.Supplier;

/** 把游戏动作切到客户端主线程执行，并用 CompletableFuture 回传结果。 */
public class GameBridge {
    public interface MainThread { void execute(Runnable r); }

    private final GameActions actions;
    private final MainThread mainThread;
    private final long timeoutMs;

    public GameBridge(GameActions actions, MainThread mainThread, long timeoutMs) {
        this.actions = actions;
        this.mainThread = mainThread;
        this.timeoutMs = timeoutMs;
    }

    public CompletableFuture<PlayerState> playerState() {
        return submit(actions::playerState);
    }

    public CompletableFuture<Void> sendCommand(String commandWithoutSlash) {
        return submit(() -> { actions.sendCommand(commandWithoutSlash); return null; });
    }

    public CompletableFuture<Void> sendChat(String text) {
        return submit(() -> { actions.sendChat(text); return null; });
    }

    public <T> T await(CompletableFuture<T> future)
            throws InterruptedException, ExecutionException, TimeoutException {
        return future.get(timeoutMs, TimeUnit.MILLISECONDS);
    }

    private <T> CompletableFuture<T> submit(Supplier<T> work) {
        CompletableFuture<T> future = new CompletableFuture<>();
        mainThread.execute(() -> {
            try {
                future.complete(work.get());
            } catch (Throwable t) {
                future.completeExceptionally(t);
            }
        });
        return future;
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd controlmod && ./gradlew test --tests '*GameBridgeTest' --console=plain`
Expected: PASS（6/6）。

- [ ] **Step 5: 提交**

```bash
git add controlmod/src/main/java/com/mcrobot/controlmod/control/PlayerState.java controlmod/src/main/java/com/mcrobot/controlmod/control/GameActions.java controlmod/src/main/java/com/mcrobot/controlmod/control/NotInGameException.java controlmod/src/main/java/com/mcrobot/controlmod/control/GameBridge.java controlmod/src/test/java/com/mcrobot/controlmod/control/GameBridgeTest.java
git commit -m "feat: 控制通道主线程桥 GameBridge + 游戏动作接口"
```

---

### Task 3: ControlServer（HTTP 路由 + 鉴权）

**Files:**
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/control/ControlServer.java`
- Test: `controlmod/src/test/java/com/mcrobot/controlmod/control/ControlServerTest.java`

**Interfaces:**
- Consumes: `GameBridge`、`GameActions`、`PlayerState`、`NotInGameException`（Task 2）；`ChatLog`（Task 1）；Gson
- Produces: `ControlServer(int port, String token, GameBridge bridge, ChatLog chatLog)`、`void start()`、`void stop()`、`int port()`

- [ ] **Step 1: 写失败测试**

`controlmod/src/test/java/com/mcrobot/controlmod/control/ControlServerTest.java`：

```java
package com.mcrobot.controlmod.control;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

class ControlServerTest {
    private static final String TOKEN = "test-token";

    static class FakeActions implements GameActions {
        PlayerState state = new PlayerState("DeekSeek", 1.5, 64.0, 2.5, "minecraft:overworld");
        final List<String> commands = new ArrayList<>();
        final List<String> chats = new ArrayList<>();
        boolean inGame = true;
        public PlayerState playerState() { return inGame ? state : null; }
        public void sendCommand(String c) { if (!inGame) throw new NotInGameException(); commands.add(c); }
        public void sendChat(String t) { if (!inGame) throw new NotInGameException(); chats.add(t); }
    }

    FakeActions actions;
    ChatLog chatLog;
    ControlServer server;
    HttpClient http;
    int port;

    @BeforeEach void setUp() throws Exception {
        actions = new FakeActions();
        chatLog = new ChatLog(10);
        GameBridge bridge = new GameBridge(actions, Runnable::run, 1000);
        server = new ControlServer(0, TOKEN, bridge, chatLog);
        server.start();
        port = server.port();
        http = HttpClient.newHttpClient();
    }

    @AfterEach void tearDown() { server.stop(); }

    private HttpResponse<String> send(HttpRequest req) throws Exception {
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path) throws Exception {
        return send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .header("X-Control-Token", TOKEN).GET().build());
    }

    private HttpResponse<String> post(String path, String json) throws Exception {
        return send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .header("X-Control-Token", TOKEN).header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json)).build());
    }

    private JsonObject json(HttpResponse<String> r) {
        return JsonParser.parseString(r.body()).getAsJsonObject();
    }

    @Test void healthInGame() throws Exception {
        HttpResponse<String> r = get("/health");
        assertEquals(200, r.statusCode());
        JsonObject o = json(r);
        assertTrue(o.get("ok").getAsBoolean());
        assertTrue(o.get("inGame").getAsBoolean());
        assertEquals("DeekSeek", o.get("player").getAsString());
    }

    @Test void healthNotInGameIs200() throws Exception {
        actions.inGame = false;
        HttpResponse<String> r = get("/health");
        assertEquals(200, r.statusCode());
        JsonObject o = json(r);
        assertFalse(o.get("inGame").getAsBoolean());
        assertTrue(o.get("player").isJsonNull());
    }

    @Test void stateReturnsPlayerAndChats() throws Exception {
        chatLog.append("hello");
        HttpResponse<String> r = get("/state");
        assertEquals(200, r.statusCode());
        JsonObject o = json(r);
        assertEquals(1.5, o.getAsJsonObject("player").get("x").getAsDouble(), 1e-9);
        assertEquals("hello", o.getAsJsonArray("chats").get(0).getAsJsonObject().get("text").getAsString());
        assertEquals(1, o.get("seq").getAsLong());
    }

    @Test void commandStripsOneLeadingSlash() throws Exception {
        HttpResponse<String> r = post("/command", "{\"command\":\"//set stone\"}");
        assertEquals(200, r.statusCode());
        assertEquals(List.of("/set stone"), actions.commands);
    }

    @Test void commandWithoutLeadingSlashIs400() throws Exception {
        HttpResponse<String> r = post("/command", "{\"command\":\"set stone\"}");
        assertEquals(400, r.statusCode());
        assertTrue(actions.commands.isEmpty());
    }

    @Test void chatSends() throws Exception {
        HttpResponse<String> r = post("/chat", "{\"text\":\"hi\"}");
        assertEquals(200, r.statusCode());
        assertEquals(List.of("hi"), actions.chats);
    }

    @Test void chatSinceFilters() throws Exception {
        chatLog.append("a");
        chatLog.append("b");
        HttpResponse<String> r = get("/chat?since=1");
        assertEquals(200, r.statusCode());
        JsonObject o = json(r);
        assertEquals(1, o.getAsJsonArray("chats").size());
        assertEquals("b", o.getAsJsonArray("chats").get(0).getAsJsonObject().get("text").getAsString());
    }

    @Test void missingTokenIs401() throws Exception {
        HttpResponse<String> r = send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/health")).GET().build());
        assertEquals(401, r.statusCode());
    }

    @Test void wrongTokenIs401() throws Exception {
        HttpResponse<String> r = send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/health"))
                .header("X-Control-Token", "nope").GET().build());
        assertEquals(401, r.statusCode());
    }

    @Test void originHeaderIs403() throws Exception {
        HttpResponse<String> r = send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/health"))
                .header("X-Control-Token", TOKEN).header("Origin", "http://evil.example").GET().build());
        assertEquals(403, r.statusCode());
    }

    @Test void postWithoutJsonIs415() throws Exception {
        HttpResponse<String> r = send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/command"))
                .header("X-Control-Token", TOKEN).header("Content-Type", "text/plain")
                .POST(HttpRequest.BodyPublishers.ofString("{\"command\":\"//x\"}")).build());
        assertEquals(415, r.statusCode());
    }

    @Test void unknownPathIs404() throws Exception {
        assertEquals(404, get("/nope").statusCode());
    }

    @Test void wrongMethodIs405() throws Exception {
        HttpResponse<String> r = send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/state"))
                .header("X-Control-Token", TOKEN).DELETE().build());
        assertEquals(405, r.statusCode());
    }

    @Test void commandNotInGameIs409() throws Exception {
        actions.inGame = false;
        HttpResponse<String> r = post("/command", "{\"command\":\"//pos1\"}");
        assertEquals(409, r.statusCode());
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd controlmod && ./gradlew test --tests '*ControlServerTest' --console=plain`
Expected: 编译失败 / FAIL —— `ControlServer` 不存在。

- [ ] **Step 3: 实现 ControlServer**

`controlmod/src/main/java/com/mcrobot/controlmod/control/ControlServer.java`：

```java
package com.mcrobot.controlmod.control;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeoutException;

/** 只监听 127.0.0.1 的本地 HTTP 桥：token 鉴权 + JSON。 */
public class ControlServer {
    private static final Gson GSON = new Gson();

    private final int requestedPort;
    private final String token;
    private final GameBridge bridge;
    private final ChatLog chatLog;
    private HttpServer server;

    public ControlServer(int port, String token, GameBridge bridge, ChatLog chatLog) {
        this.requestedPort = port;
        this.token = token;
        this.bridge = bridge;
        this.chatLog = chatLog;
    }

    public void start() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", requestedPort), 0);
        server.createContext("/", this::handle);
        server.setExecutor(Executors.newFixedThreadPool(4));
        server.start();
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) server.stop(0);
    }

    private static final class HttpError extends RuntimeException {
        final int code;
        HttpError(int code, String msg) { super(msg); this.code = code; }
    }

    private void handle(HttpExchange ex) throws IOException {
        try {
            if (ex.getRequestHeaders().getFirst("Origin") != null) {
                throw new HttpError(403, "cross-origin rejected");
            }
            String provided = ex.getRequestHeaders().getFirst("X-Control-Token");
            if (provided == null || !MessageDigest.isEqual(
                    provided.getBytes(StandardCharsets.UTF_8), token.getBytes(StandardCharsets.UTF_8))) {
                throw new HttpError(401, "bad token");
            }
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/health" -> { requireGet(method); health(ex); }
                case "/state" -> { requireGet(method); state(ex); }
                case "/command" -> { requirePost(method); command(ex); }
                case "/chat" -> {
                    if (method.equals("GET")) chatSince(ex);
                    else if (method.equals("POST")) chatSend(ex);
                    else throw new HttpError(405, "method not allowed");
                }
                default -> throw new HttpError(404, "not found");
            }
        } catch (HttpError e) {
            send(ex, e.code, error(e.getMessage()));
        } catch (Exception e) {
            send(ex, 503, error("internal: " + e.getClass().getSimpleName()));
        }
    }

    private static void requireGet(String method) {
        if (!method.equals("GET")) throw new HttpError(405, "method not allowed");
    }

    private static void requirePost(String method) {
        if (!method.equals("POST")) throw new HttpError(405, "method not allowed");
    }

    private void health(HttpExchange ex) throws IOException {
        PlayerState p = playerStateOrNull();
        JsonObject o = new JsonObject();
        o.addProperty("ok", true);
        o.addProperty("inGame", p != null);
        o.add("player", p == null ? JsonNull.INSTANCE : GSON.toJsonTree(p.name()));
        send(ex, 200, o);
    }

    private void state(HttpExchange ex) throws IOException {
        PlayerState p = playerStateOrNull();
        JsonObject o = new JsonObject();
        o.addProperty("ok", true);
        o.addProperty("inGame", p != null);
        o.add("player", p == null ? JsonNull.INSTANCE : playerJson(p));
        o.addProperty("seq", chatLog.latestSeq());
        o.add("chats", chatsJson(chatLog.since(0).entries()));
        send(ex, 200, o);
    }

    private void chatSince(HttpExchange ex) throws IOException {
        long since = 0;
        String q = ex.getRequestURI().getRawQuery();
        if (q != null) {
            for (String pair : q.split("&")) {
                int eq = pair.indexOf('=');
                if (eq > 0 && pair.substring(0, eq).equals("since")) {
                    try { since = Long.parseLong(pair.substring(eq + 1)); }
                    catch (NumberFormatException e) { throw new HttpError(400, "bad since"); }
                }
            }
        }
        JsonObject o = new JsonObject();
        o.addProperty("ok", true);
        o.addProperty("seq", chatLog.latestSeq());
        o.add("chats", chatsJson(chatLog.since(since).entries()));
        send(ex, 200, o);
    }

    private void command(HttpExchange ex) throws IOException {
        requireJson(ex);
        String cmd = requiredString(ex, "command");
        if (!cmd.startsWith("/")) throw new HttpError(400, "command must start with /");
        try {
            bridge.await(bridge.sendCommand(cmd.substring(1)));
        } catch (TimeoutException e) {
            throw new HttpError(503, "timeout");
        } catch (ExecutionException e) {
            if (e.getCause() instanceof NotInGameException) throw new HttpError(409, "not in game");
            throw new HttpError(503, "command failed");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new HttpError(503, "interrupted");
        }
        send(ex, 200, ok());
    }

    private void chatSend(HttpExchange ex) throws IOException {
        requireJson(ex);
        String text = requiredString(ex, "text");
        try {
            bridge.await(bridge.sendChat(text));
        } catch (TimeoutException e) {
            throw new HttpError(503, "timeout");
        } catch (ExecutionException e) {
            if (e.getCause() instanceof NotInGameException) throw new HttpError(409, "not in game");
            throw new HttpError(503, "chat failed");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new HttpError(503, "interrupted");
        }
        send(ex, 200, ok());
    }

    private PlayerState playerStateOrNull() {
        try {
            return bridge.await(bridge.playerState());
        } catch (TimeoutException e) {
            throw new HttpError(503, "timeout");
        } catch (ExecutionException e) {
            throw new HttpError(503, "state failed");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new HttpError(503, "interrupted");
        }
    }

    private static void requireJson(HttpExchange ex) {
        String ct = ex.getRequestHeaders().getFirst("Content-Type");
        if (ct == null || !ct.toLowerCase().startsWith("application/json")) {
            throw new HttpError(415, "content-type must be application/json");
        }
    }

    private static String requiredString(HttpExchange ex, String field) throws IOException {
        JsonObject body;
        try (InputStream in = ex.getRequestBody()) {
            body = JsonParser.parseString(new String(in.readAllBytes(), StandardCharsets.UTF_8)).getAsJsonObject();
        } catch (Exception e) {
            throw new HttpError(400, "invalid json body");
        }
        JsonElement v = body.get(field);
        if (v == null || !v.isJsonPrimitive()) throw new HttpError(400, "missing field: " + field);
        return v.getAsString();
    }

    private static JsonObject playerJson(PlayerState p) {
        JsonObject o = new JsonObject();
        o.addProperty("name", p.name());
        o.addProperty("x", p.x());
        o.addProperty("y", p.y());
        o.addProperty("z", p.z());
        o.addProperty("dim", p.dim());
        return o;
    }

    private static JsonArray chatsJson(List<ChatLog.Entry> entries) {
        JsonArray arr = new JsonArray();
        for (ChatLog.Entry e : entries) {
            JsonObject o = new JsonObject();
            o.addProperty("seq", e.seq());
            o.addProperty("text", e.text());
            arr.add(o);
        }
        return arr;
    }

    private static JsonObject ok() {
        JsonObject o = new JsonObject();
        o.addProperty("ok", true);
        return o;
    }

    private static JsonObject error(String msg) {
        JsonObject o = new JsonObject();
        o.addProperty("ok", false);
        o.addProperty("error", msg);
        return o;
    }

    private static void send(HttpExchange ex, int code, JsonElement body) throws IOException {
        byte[] bytes = GSON.toJson(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, bytes.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(bytes);
        }
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd controlmod && ./gradlew test --tests '*ControlServerTest' --console=plain`
Expected: PASS（14/14）。若 `com.sun.net.httpserver` 无法解析，确认 JDK 17 工具链已生效（`gradlew` 版本应为 17）。

- [ ] **Step 5: 提交**

```bash
git add controlmod/src/main/java/com/mcrobot/controlmod/control/ControlServer.java controlmod/src/test/java/com/mcrobot/controlmod/control/ControlServerTest.java
git commit -m "feat: 控制通道 HTTP 服务器（路由+token+JSON）"
```

---

### Task 4: ControlToken / MinecraftGameActions / ControlMod 接线 / runtime.json

**Files:**
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/control/ControlToken.java`
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/control/MinecraftGameActions.java`
- Test: `controlmod/src/test/java/com/mcrobot/controlmod/control/ControlTokenTest.java`
- Modify: `controlmod/src/main/java/com/mcrobot/controlmod/ControlMod.java`
- Modify: `version/1.20.1-forge/runtime.json`

**Interfaces:**
- Consumes: `GameActions`/`PlayerState`/`NotInGameException`（Task 2）、`ControlServer`（Task 3）、`ChatLog`（Task 1）
- Produces: `ControlToken.loadOrCreate(Path gameDir) -> String`；`MinecraftGameActions implements GameActions`

- [ ] **Step 1: 写 ControlToken 失败测试**

`controlmod/src/test/java/com/mcrobot/controlmod/control/ControlTokenTest.java`：

```java
package com.mcrobot.controlmod.control;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import static org.junit.jupiter.api.Assertions.*;

class ControlTokenTest {
    @Test void generatesAndWritesWhenMissing(@TempDir Path dir) throws Exception {
        String token = ControlToken.loadOrCreate(dir);
        assertTrue(token.matches("[0-9a-f]{32}"), token);
        assertEquals(token, Files.readString(dir.resolve("control_token"), StandardCharsets.UTF_8));
    }

    @Test void reusesExistingToken(@TempDir Path dir) throws Exception {
        Files.writeString(dir.resolve("control_token"), "  existing-token\n", StandardCharsets.UTF_8);
        assertEquals("existing-token", ControlToken.loadOrCreate(dir));
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd controlmod && ./gradlew test --tests '*ControlTokenTest' --console=plain`
Expected: 编译失败 / FAIL —— `ControlToken` 不存在。

- [ ] **Step 3: 实现 ControlToken 与 MinecraftGameActions**

`controlmod/src/main/java/com/mcrobot/controlmod/control/ControlToken.java`：

```java
package com.mcrobot.controlmod.control;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.SecureRandom;

/** 读写 gameDir/control_token：存在则复用（trim 后），缺失则生成 32 位 hex 并落盘。 */
public final class ControlToken {
    private ControlToken() {}

    public static String loadOrCreate(Path gameDir) throws IOException {
        Path file = gameDir.resolve("control_token");
        if (Files.exists(file)) {
            String existing = Files.readString(file, StandardCharsets.UTF_8).trim();
            if (!existing.isEmpty()) return existing;
        }
        String token = generate();
        Files.writeString(file, token, StandardCharsets.UTF_8);
        return token;
    }

    static String generate() {
        byte[] bytes = new byte[16];
        new SecureRandom().nextBytes(bytes);
        StringBuilder sb = new StringBuilder(32);
        for (byte b : bytes) sb.append(String.format("%02x", b));
        return sb.toString();
    }
}
```

`controlmod/src/main/java/com/mcrobot/controlmod/control/MinecraftGameActions.java`：

```java
package com.mcrobot.controlmod.control;

import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;

/** GameActions 的 Forge 客户端实现；必须在主线程调用。 */
public class MinecraftGameActions implements GameActions {
    @Override
    public PlayerState playerState() {
        Minecraft mc = Minecraft.getInstance();
        LocalPlayer p = mc == null ? null : mc.player;
        if (p == null) return null;
        String dim = mc.level == null ? "" : mc.level.dimension().location().toString();
        return new PlayerState(p.getGameProfile().getName(), p.getX(), p.getY(), p.getZ(), dim);
    }

    @Override
    public void sendCommand(String commandWithoutSlash) {
        LocalPlayer p = requirePlayer();
        p.connection.sendCommand(commandWithoutSlash);
    }

    @Override
    public void sendChat(String text) {
        LocalPlayer p = requirePlayer();
        p.connection.sendChat(text);
    }

    private static LocalPlayer requirePlayer() {
        Minecraft mc = Minecraft.getInstance();
        LocalPlayer p = mc == null ? null : mc.player;
        if (p == null) throw new NotInGameException();
        return p;
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd controlmod && ./gradlew test --tests '*ControlTokenTest' --console=plain`
Expected: PASS（2/2）。

- [ ] **Step 5: 改 ControlMod.java（接线）**

把 `controlmod/src/main/java/com/mcrobot/controlmod/ControlMod.java` 全文替换为：

```java
package com.mcrobot.controlmod;

import com.mcrobot.controlmod.control.ChatLog;
import com.mcrobot.controlmod.control.ControlServer;
import com.mcrobot.controlmod.control.ControlToken;
import com.mcrobot.controlmod.control.GameBridge;
import com.mcrobot.controlmod.control.MinecraftGameActions;
import net.minecraft.client.Minecraft;
import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.client.event.ClientChatReceivedEvent;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.eventbus.api.IEventBus;
import net.minecraftforge.eventbus.api.SubscribeEvent;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.event.lifecycle.FMLClientSetupEvent;
import net.minecraftforge.fml.loading.FMLEnvironment;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Path;

@Mod(ControlMod.MODID)
public class ControlMod {
    public static final String MODID = "mcrobot_control";
    private static final Logger LOGGER = LoggerFactory.getLogger(MODID);
    // 与 version/1.20.1-forge/runtime.json 的 control_mod.http_port 一致
    private static final int CONTROL_PORT = 17890;

    private final ChatLog chatLog = new ChatLog(200);
    private AutoLogin autoLogin;
    private boolean configAttempted = false;
    private ControlServer controlServer;

    public ControlMod(IEventBus modEventBus) {
        if (FMLEnvironment.dist != Dist.CLIENT) return;
        modEventBus.addListener(this::onClientSetup);
        MinecraftForge.EVENT_BUS.register(this);
    }

    private void onClientSetup(FMLClientSetupEvent event) {
        Path gameDir = Minecraft.getInstance().gameDirectory.toPath();
        try {
            String token = ControlToken.loadOrCreate(gameDir);
            GameBridge bridge = new GameBridge(new MinecraftGameActions(), Minecraft.getInstance()::execute, 5000);
            controlServer = new ControlServer(CONTROL_PORT, token, bridge, chatLog);
            controlServer.start();
            LOGGER.info("ControlMod control channel on 127.0.0.1:{}", controlServer.port());
        } catch (IOException e) {
            LOGGER.warn("ControlMod control channel failed to start on port {}: {}", CONTROL_PORT, e.toString());
        }
    }

    @SubscribeEvent
    public void onChat(ClientChatReceivedEvent event) {
        String text = event.getMessage().getString();
        chatLog.append(text);
        if (autoLogin == null) {
            if (configAttempted) return;
            configAttempted = true;
            Path gameDir = Minecraft.getInstance().gameDirectory.toPath();
            autoLogin = ModConfig.load(gameDir)
                    .map(cfg -> new AutoLogin(cfg, cmd -> {
                        Minecraft mc = Minecraft.getInstance();
                        if (mc.player != null) {
                            String command = cmd.startsWith("/") ? cmd.substring(1) : cmd;
                            mc.player.connection.sendCommand(command);
                        }
                    }))
                    .orElse(null);
            if (autoLogin == null) return;
        }
        autoLogin.onChat(text, System.currentTimeMillis());
    }
}
```

- [ ] **Step 6: 改 runtime.json（停用 ws_port）**

把 `version/1.20.1-forge/runtime.json` 的 `control_mod` 段替换为：

```json
  "control_mod": {
    "http_port": 17890,
    "_note": "ControlMod 本地 HTTP 桥，只监听 127.0.0.1。token 见 instance/control_token（mod 启动生成）"
  }
```

- [ ] **Step 7: 整模块测试 + 构建**

Run: `cd controlmod && ./gradlew test build --console=plain`
Expected: BUILD SUCCESSFUL；测试总数 = 前 4 个类（ChatLog 4 + GameBridge 6 + ControlServer 14 + ControlToken 2）＝ 26，加既有 AutoLogin 7 + LoginPromptMatcher 8 + ModConfig 3 ＝ 44 全绿。

- [ ] **Step 8: 提交**

```bash
git add controlmod/src/main/java/com/mcrobot/controlmod/control/ControlToken.java controlmod/src/main/java/com/mcrobot/controlmod/control/MinecraftGameActions.java controlmod/src/test/java/com/mcrobot/controlmod/control/ControlTokenTest.java controlmod/src/main/java/com/mcrobot/controlmod/ControlMod.java version/1.20.1-forge/runtime.json
git commit -m "feat: 控制通道接线（token、Forge 动作实现、客户端启动时开服）"
```

---

### Task 5: tools/control_client.py（最小编排客户端 + 测试）

**Files:**
- Create: `tools/control_client.py`
- Test: `tools/test_control_client.py`

**Interfaces:**
- Consumes: 运行中的 ControlMod HTTP 桥（Task 4）；`version/1.20.1-forge/runtime.json`、`instance/control_token`
- Produces: `health()/state()/chat(text)/cmd(command)`；`ControlError`；CLI `python tools/control_client.py health|state|chat <text>|cmd <command>`

- [ ] **Step 1: 写失败测试**

`tools/test_control_client.py`：

```python
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import control_client


def make_handler(state, received):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _handle(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            received.append({
                "method": self.command,
                "path": self.path,
                "token": self.headers.get("X-Control-Token"),
                "content_type": self.headers.get("Content-Type"),
                "body": json.loads(raw.decode("utf-8")) if raw else None,
            })
            payload = json.dumps(state["body"]).encode()
            self.send_response(state["code"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _handle
        do_POST = _handle

    return H


class TestControlClient(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.state = {"code": 200, "body": {"ok": True}}
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.state, self.received))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self._orig_port = control_client.http_port
        self._orig_token = control_client.token
        control_client.http_port = lambda: self.port
        control_client.token = lambda: "tok123"

    def tearDown(self):
        control_client.http_port = self._orig_port
        control_client.token = self._orig_token
        self.server.shutdown()

    def test_health_sends_token(self):
        out = control_client.health()
        self.assertTrue(out["ok"])
        self.assertEqual("tok123", self.received[0]["token"])

    def test_cmd_posts_json(self):
        control_client.cmd("//set stone")
        req = self.received[0]
        self.assertEqual("/command", req["path"])
        self.assertEqual({"command": "//set stone"}, req["body"])
        self.assertTrue(req["content_type"].startswith("application/json"))

    def test_chat_posts_json(self):
        control_client.chat("hi")
        self.assertEqual({"text": "hi"}, self.received[0]["body"])

    def test_http_error_raises_control_error(self):
        self.state["code"] = 409
        self.state["body"] = {"ok": False, "error": "not in game"}
        with self.assertRaises(control_client.ControlError) as cm:
            control_client.cmd("/say hi")
        self.assertIn("409", str(cm.exception))
        self.assertIn("not in game", str(cm.exception))

    def test_connection_refused_raises_control_error(self):
        control_client.http_port = lambda: 1  # 不可连端口
        with self.assertRaises(control_client.ControlError):
            control_client.state()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd tools && python -m unittest test_control_client -v`
Expected: ERROR —— `ModuleNotFoundError: No module named 'control_client'`（文件未建）。

- [ ] **Step 3: 实现 control_client.py**

`tools/control_client.py`：

```python
#!/usr/bin/env python3
"""ControlMod 本地 HTTP 桥的最小客户端（stdlib，无依赖）。"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def http_port():
    cfg = json.loads((REPO / "version" / "1.20.1-forge" / "runtime.json").read_text(encoding="utf-8"))
    return int(cfg["control_mod"]["http_port"])


def token():
    return (REPO / "instance" / "control_token").read_text(encoding="utf-8").strip()


class ControlError(Exception):
    pass


def _request(method, path, body=None):
    url = f"http://127.0.0.1:{http_port()}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Control-Token", token())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(payload).get("error", payload)
        except Exception:
            err = payload
        raise ControlError(f"HTTP {e.code}: {err}") from None
    except urllib.error.URLError as e:
        raise ControlError(f"无法连接 ControlMod（客户端未运行或端口未开？）: {e.reason}") from None


def health():
    return _request("GET", "/health")


def state():
    return _request("GET", "/state")


def chat(text):
    return _request("POST", "/chat", {"text": text})


def cmd(command):
    return _request("POST", "/command", {"command": command})


def main(argv=None):
    p = argparse.ArgumentParser(prog="control_client")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("health")
    sub.add_parser("state")
    sc = sub.add_parser("chat"); sc.add_argument("text")
    sm = sub.add_parser("cmd"); sm.add_argument("command")
    args = p.parse_args(argv)
    try:
        if args.action == "health":
            out = health()
        elif args.action == "state":
            out = state()
        elif args.action == "chat":
            out = chat(args.text)
        else:
            out = cmd(args.command)
    except ControlError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tools && python -m unittest test_control_client -v`
Expected: PASS（5/5）。

- [ ] **Step 5: 提交**

```bash
git add tools/control_client.py tools/test_control_client.py
git commit -m "feat: 最小编排客户端 control_client.py（stdlib）"
```

---

### Task 6: 构建部署 + 真机 e2e

**Files:** 无源码改动（部署 + 验证）

**Interfaces:**
- Consumes: Task 1–5 全部产物；`tools/launch_client.py`；运行中的服务器

- [ ] **Step 1: 构建并部署 jar**

```bash
cd controlmod && ./gradlew test build --console=plain
cp build/libs/mcrobot_control-0.1.0.jar ../instance/mods/
```

Expected: BUILD SUCCESSFUL；jar 已更新到 `instance/mods/`。

- [ ] **Step 2: 确认服务器在线后启动客户端**

```bash
cd .. && python tools/launch_client.py
```

（若端口拒连，先确认简幻欢面板服务器在运行；端口 TCP 通不代表 MC 服务端就绪。）

- [ ] **Step 3: 等进服后核对控制通道**

```bash
python tools/control_client.py health     # 期望 {"ok":true,"inGame":true,"player":"DeekSeek"}
python tools/control_client.py state      # 期望含真实 x/y/z/dim
```

- [ ] **Step 4: 下发一条可读回的指令**

```bash
python tools/control_client.py cmd "/say mcrobot 控制通道测试"
```

然后在 `instance/logs/latest.log`（**GBK 编码**，看 `[CHAT]` 行）或 `state` 的 `chats` 里确认这行出现 → 证明「外部 → 游戏 → 外部」闭环。

- [ ] **Step 5: 核对无明文密码**

```bash
python - <<'EOF'
import json
raw = open('instance/logs/latest.log','rb').read().decode('gbk', errors='replace')
pw = json.load(open('version/1.20.1-forge/config.json', encoding='utf-8'))['account']['password']
print('密码出现在日志:', pw in raw)
EOF
```

Expected: `False`。

- [ ] **Step 6: 收尾**

关闭客户端（`Stop-Process -Name java -Force`）。把 e2e 结果记入 SDD 台账（新建 `.superpowers/sdd/2026-10-14-control-channel/progress.md`）。

---

## 自审记录（Self-Review）

- **Spec 覆盖**：§4 数据契约 → Task 3；§5 组件 → Task 1–5；§6 线程模型 → Task 2/3；§7 安全（token/Origin/Content-Type）→ Task 3；§8 错误处理 → Task 3；§10 测试 → 各任务 + Task 6。无遗漏。
- **占位符**：无 TBD/TODO；每个代码步骤给了完整代码。
- **类型一致性**：`GameActions`/`PlayerState`/`GameBridge` 签名在 Task 2 定义、Task 3/4 消费一致；`ControlServer` 构造签名 Task 3 定义、Task 4 消费一致；`ChatLog.Entry/Snapshot` 一致。
- **Review Focus**：5 条各有对应测试（Task 1/2/3 单测 + Task 4 捕获 IOException）。
- **已知不确定项已消除**：`sendChat`/`sendCommand`/`getGameProfile`/`Minecraft.level/player/gameDirectory` 已用 javap 对 1.20.1 映射 jar 核实；Gson 2.10 已在编译类路径核实。
