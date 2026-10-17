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
            return new WorldView.Area(List.of(blockAt(x1, y1, z1)), 1, false);
        }
        public WorldView.Hit lookingAt() {
            if (!inGame) throw new NotInGameException();
            return WorldView.Hit.none();
        }
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

    @Test void closeScreenRunsAction() throws Exception {
        FakeActions a = new FakeActions();
        GameBridge b = new GameBridge(a, DIRECT, 1000);
        b.await(b.closeScreen());
        assertEquals(1, a.closeCount);
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
            public void closeScreen() {}
            public WorldView.BlockAt blockAt(int x, int y, int z) { return null; }
            public WorldView.Area blocksInBox(int x1, int y1, int z1, int x2, int y2, int z2, int limit) { return null; }
            public WorldView.Hit lookingAt() { return null; }
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
