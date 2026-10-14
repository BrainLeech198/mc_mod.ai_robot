package com.mcrobot.controlmod;

import java.util.EnumMap;
import java.util.Map;

public class AutoLogin {
    public interface CommandSender { void send(String cmd); }

    private static final long DEBOUNCE_MS = 3000;

    private final ModConfig config;
    private final CommandSender sender;
    private final Map<LoginPromptMatcher.Action, Long> lastSentMs =
            new EnumMap<>(LoginPromptMatcher.Action.class);
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
            case REGISTER -> maybeSend(action, nowMs, "/yzts register " + config.registerPassword());
            case LOGIN -> maybeSend(action, nowMs, "/yzts login " + config.registerPassword());
            case GATE -> {
                if (!config.gatePassword().isEmpty()) {
                    maybeSend(action, nowMs, "/yzts gate " + config.gatePassword());
                }
            }
            case NONE -> { }
        }
    }

    private void maybeSend(LoginPromptMatcher.Action action, long nowMs, String cmd) {
        if (attempts >= config.maxAttempts()) { stopped = true; return; }
        Long last = lastSentMs.get(action);
        if (last != null && nowMs - last < DEBOUNCE_MS) return;
        lastSentMs.put(action, nowMs);
        attempts++;
        sender.send(cmd);
    }
}
