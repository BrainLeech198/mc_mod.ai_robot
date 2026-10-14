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

    public CompletableFuture<Void> closeScreen() {
        return submit(() -> { actions.closeScreen(); return null; });
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
