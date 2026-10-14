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
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeoutException;

/** 只监听 127.0.0.1 的本地 HTTP 桥：token 鉴权 + JSON。 */
public class ControlServer {
    // serializeNulls：本环境的 Gson 默认会丢弃 JsonNull 成员，须显式开启以输出 "player":null
    private static final Gson GSON = new GsonBuilder().serializeNulls().create();

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
