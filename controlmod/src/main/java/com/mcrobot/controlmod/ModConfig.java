package com.mcrobot.controlmod;

import com.google.gson.Gson;
import java.nio.file.*;
import java.util.Optional;

public record ModConfig(String registerPassword, String gatePassword, int maxAttempts) {
    public static Optional<ModConfig> load(Path gameDir) {
        Path file = gameDir.resolve("mcrobot.json");
        if (!Files.exists(file)) return Optional.empty();
        try {
            Gson gson = new Gson();
            Raw raw = gson.fromJson(Files.readString(file), Raw.class);
            if (raw == null || raw.registerPassword == null) return Optional.empty();
            int attempts = raw.maxAttempts > 0 ? raw.maxAttempts : 3;
            return Optional.of(new ModConfig(raw.registerPassword,
                    raw.gatePassword == null ? "" : raw.gatePassword, attempts));
        } catch (Exception e) {
            return Optional.empty();
        }
    }

    private static class Raw {
        String registerPassword;
        String gatePassword;
        int maxAttempts;
    }
}
