package com.mcrobot.controlmod;

public class AutoLogin {
    public interface CommandSender { void send(String cmd); }

    private static final long DEBOUNCE_MS = 3000;

    private final ModConfig config;
    private final CommandSender sender;
    private boolean everSent = false;
    private long lastSentMs = 0L;
    private int attempts = 0;
    private boolean stopped = false;

    public AutoLogin(ModConfig config, CommandSender sender) {
        this.config = config;
        this.sender = sender;
    }

    public void onChat(String text, long nowMs) {
        if (stopped) return;
        LoginPromptMatcher.Action action = LoginPromptMatcher.match(text);
        switch (action) {
            case STOP_LOCKED, STOP_RATE_LIMITED -> stopped = true;
            case REGISTER -> maybeSend(nowMs, "/yzts register " + config.registerPassword());
            case LOGIN -> maybeSend(nowMs, "/yzts login " + config.registerPassword());
            case GATE -> {
                if (!config.gatePassword().isEmpty()) {
                    maybeSend(nowMs, "/yzts gate " + config.gatePassword());
                }
            }
            case NONE -> { }
        }
    }

    private void maybeSend(long nowMs, String cmd) {
        if (attempts >= config.maxAttempts()) { stopped = true; return; }
        if (everSent && nowMs - lastSentMs < DEBOUNCE_MS) return;
        everSent = true;
        lastSentMs = nowMs;
        attempts++;
        sender.send(cmd);
    }
}
