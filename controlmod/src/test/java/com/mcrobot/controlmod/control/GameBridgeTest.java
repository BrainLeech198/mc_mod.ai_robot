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
