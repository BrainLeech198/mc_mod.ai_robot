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
