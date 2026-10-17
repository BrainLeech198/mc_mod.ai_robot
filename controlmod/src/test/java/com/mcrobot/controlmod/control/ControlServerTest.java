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
        int closeCount = 0;
        public PlayerState playerState() { return inGame ? state : null; }
        public void sendCommand(String c) { if (!inGame) throw new NotInGameException(); commands.add(c); }
        public void sendChat(String t) { if (!inGame) throw new NotInGameException(); chats.add(t); }
        public void closeScreen() { if (!inGame) throw new NotInGameException(); closeCount++; }

        public WorldView.BlockAt blockAt(int x, int y, int z) {
            if (!inGame) throw new NotInGameException();
            return new WorldView.BlockAt(x, y, z, "minecraft:stone", true);
        }

        public WorldView.Area blocksInBox(int x1, int y1, int z1, int x2, int y2, int z2, int limit) {
            if (!inGame) throw new NotInGameException();
            List<WorldView.BlockAt> out = new ArrayList<>();
            out.add(blockAt(x1, y1, z1));
            return new WorldView.Area(out, 1, false);
        }

        public WorldView.Hit lookingAt() {
            if (!inGame) throw new NotInGameException();
            return new WorldView.Hit("block", "minecraft:stone", 10, 64, -3, 2.5);
        }
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

    @Test void screenCloseCloses() throws Exception {
        HttpResponse<String> r = post("/screen/close", "{}");
        assertEquals(200, r.statusCode());
        assertEquals(1, actions.closeCount);
    }

    @Test void screenCloseNotInGameIs409() throws Exception {
        actions.inGame = false;
        HttpResponse<String> r = post("/screen/close", "{}");
        assertEquals(409, r.statusCode());
    }

    @Test void blocksSingle() throws Exception {
        HttpResponse<String> r = get("/blocks?x=10&y=64&z=-3");
        assertEquals(200, r.statusCode());
        JsonObject o = json(r);
        assertTrue(o.get("ok").getAsBoolean());
        assertEquals("minecraft:stone", o.get("id").getAsString());
        assertTrue(o.get("loaded").getAsBoolean());
    }

    @Test void blocksAreaAndTooLarge() throws Exception {
        HttpResponse<String> ok = get("/blocks?x1=0&y1=0&z1=0&x2=1&y2=1&z2=1");
        assertEquals(200, ok.statusCode());
        assertEquals(1, json(ok).get("total").getAsInt());

        HttpResponse<String> big = get("/blocks?x1=0&y1=0&z1=0&x2=99&y2=99&z2=99");
        assertEquals(400, big.statusCode());
    }

    @Test void lookingAtBlock() throws Exception {
        JsonObject o = json(get("/looking"));
        assertEquals("block", o.get("type").getAsString());
        assertEquals("minecraft:stone", o.get("id").getAsString());
    }
}
