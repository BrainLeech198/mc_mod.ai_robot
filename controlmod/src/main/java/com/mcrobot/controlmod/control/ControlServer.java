package com.mcrobot.controlmod.control;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
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
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/** 只监听 127.0.0.1 的本地 HTTP 桥：token 鉴权 + JSON。 */
public class ControlServer {
    // serializeNulls：本环境的 Gson 默认会丢弃 JsonNull 成员，须显式开启以输出 "player":null
    private static final Gson GSON = new GsonBuilder().serializeNulls().create();

    private final int requestedPort;
    private final String token;
    private final GameBridge bridge;
    private final ChatLog chatLog;
    private final ScreenCapture capture;
    private HttpServer server;

    public ControlServer(int port, String token, GameBridge bridge, ChatLog chatLog) {
        this(port, token, bridge, chatLog, null);
    }

    public ControlServer(int port, String token, GameBridge bridge, ChatLog chatLog, ScreenCapture capture) {
        this.requestedPort = port;
        this.token = token;
        this.bridge = bridge;
        this.chatLog = chatLog;
        this.capture = capture;
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
                case "/screen/close" -> { requirePost(method); screenClose(ex); }
                case "/blocks" -> { requireGet(method); blocks(ex); }
                case "/looking" -> { requireGet(method); looking(ex); }
                case "/screen.png" -> { requireGet(method); screenPng(ex); }
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

    private void screenClose(HttpExchange ex) throws IOException {
        try {
            bridge.await(bridge.closeScreen());
        } catch (TimeoutException e) {
            throw new HttpError(503, "timeout");
        } catch (ExecutionException e) {
            if (e.getCause() instanceof NotInGameException) throw new HttpError(409, "not in game");
            throw new HttpError(503, "close failed");
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

    private void blocks(HttpExchange ex) throws IOException {
        Integer x = queryInt(ex, "x");
        Integer y = queryInt(ex, "y");
        Integer z = queryInt(ex, "z");
        Integer x1 = queryInt(ex, "x1");
        Integer y1 = queryInt(ex, "y1");
        Integer z1 = queryInt(ex, "z1");
        Integer x2 = queryInt(ex, "x2");
        Integer y2 = queryInt(ex, "y2");
        Integer z2 = queryInt(ex, "z2");

        if (x != null || y != null || z != null) {
            if (x == null || y == null || z == null) {
                throw new HttpError(400, "single block needs x,y,z");
            }
            JsonObject o = blockJson(awaitBridge(bridge.blockAt(x, y, z)));
            o.addProperty("ok", true);
            send(ex, 200, o);
            return;
        }
        if (x1 == null || y1 == null || z1 == null || x2 == null || y2 == null || z2 == null) {
            throw new HttpError(400, "need x,y,z or x1,y1,z1,x2,y2,z2");
        }
        long volume = (long) (Math.abs(x2 - x1) + 1)
                * (Math.abs(y2 - y1) + 1) * (Math.abs(z2 - z1) + 1);
        if (volume > 4096) {
            throw new HttpError(400, "area too large: " + volume + " blocks (max 4096)");
        }
        int limit = Math.min(Math.max(queryIntOr(ex, "limit", 512), 1), 4096);
        WorldView.Area area = awaitBridge(bridge.blocksInBox(x1, y1, z1, x2, y2, z2, limit));
        JsonObject o = new JsonObject();
        o.addProperty("ok", true);
        o.addProperty("total", area.total());
        o.addProperty("returned", area.blocks().size());
        o.addProperty("truncated", area.truncated());
        JsonArray arr = new JsonArray();
        for (WorldView.BlockAt b : area.blocks()) arr.add(blockJson(b));
        o.add("blocks", arr);
        send(ex, 200, o);
    }

    private void looking(HttpExchange ex) throws IOException {
        WorldView.Hit hit = awaitBridge(bridge.lookingAt());
        JsonObject o = new JsonObject();
        o.addProperty("ok", true);
        o.addProperty("type", hit.type());
        if (!hit.type().equals("none")) {
            o.addProperty("id", hit.id());
            o.addProperty("x", hit.x());
            o.addProperty("y", hit.y());
            o.addProperty("z", hit.z());
            o.addProperty("distance", Math.round(hit.distance() * 10.0) / 10.0);
        }
        send(ex, 200, o);
    }

    private void screenPng(HttpExchange ex) throws IOException {
        if (capture == null) throw new HttpError(501, "screenshot not available");
        int max = Math.min(Math.max(queryIntOr(ex, "max", 854), 64), 3840);
        long t0 = System.nanoTime();
        CompletableFuture<WorldView.Shot> future;
        try {
            future = capture.request(max);
        } catch (NotInGameException e) {
            throw new HttpError(409, "not in game");
        } catch (IllegalStateException e) {
            throw new HttpError(503, "capture busy: another screenshot is in flight");
        }
        WorldView.Shot shot;
        try {
            // 比 ScreenCapture 内部的 4s 稍长，让内部超时先给出更准确的原因
            shot = future.get(5, TimeUnit.SECONDS);
        } catch (TimeoutException e) {
            throw new HttpError(504, "capture timeout: window minimized or no frame rendered");
        } catch (ExecutionException e) {
            if (e.getCause() instanceof NotInGameException) throw new HttpError(409, "not in game");
            throw new HttpError(503, "capture failed: " + e.getCause());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new HttpError(503, "interrupted");
        }
        ex.getResponseHeaders().set("X-Image-Width", String.valueOf(shot.width()));
        ex.getResponseHeaders().set("X-Image-Height", String.valueOf(shot.height()));
        ex.getResponseHeaders().set("X-Capture-Ms", String.valueOf((System.nanoTime() - t0) / 1_000_000));
        ex.getResponseHeaders().set("Cache-Control", "no-store");
        sendBytes(ex, 200, "image/png", shot.png());
    }

    private <T> T awaitBridge(CompletableFuture<T> future) {
        try {
            return bridge.await(future);
        } catch (TimeoutException e) {
            throw new HttpError(503, "timeout");
        } catch (ExecutionException e) {
            if (e.getCause() instanceof NotInGameException) throw new HttpError(409, "not in game");
            throw new HttpError(503, "query failed: " + e.getCause());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new HttpError(503, "interrupted");
        }
    }

    private static Integer queryInt(HttpExchange ex, String name) {
        String v = queryParam(ex, name);
        if (v == null || v.isBlank()) return null;
        try {
            return Integer.parseInt(v.trim());
        } catch (NumberFormatException e) {
            throw new HttpError(400, "bad " + name);
        }
    }

    private static int queryIntOr(HttpExchange ex, String name, int fallback) {
        Integer v = queryInt(ex, name);
        return v == null ? fallback : v;
    }

    private static String queryParam(HttpExchange ex, String name) {
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) return null;
        for (String pair : q.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0 && pair.substring(0, eq).equals(name)) {
                return URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    private static JsonObject blockJson(WorldView.BlockAt b) {
        JsonObject o = new JsonObject();
        o.addProperty("x", b.x());
        o.addProperty("y", b.y());
        o.addProperty("z", b.z());
        o.addProperty("id", b.id());
        o.addProperty("loaded", b.loaded());
        return o;
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
        sendBytes(ex, code, "application/json; charset=utf-8", bytes);
    }

    private static void sendBytes(HttpExchange ex, int code, String contentType, byte[] bytes)
            throws IOException {
        ex.getResponseHeaders().set("Content-Type", contentType);
        ex.sendResponseHeaders(code, bytes.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(bytes);
        }
    }
}
