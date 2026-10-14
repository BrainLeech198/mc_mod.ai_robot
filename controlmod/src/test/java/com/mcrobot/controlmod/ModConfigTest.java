package com.mcrobot.controlmod;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.file.*;
import static org.junit.jupiter.api.Assertions.*;

class ModConfigTest {
    @TempDir Path dir;

    @Test
    void loads_valid_config() throws Exception {
        Files.writeString(dir.resolve("mcrobot.json"),
            "{\"registerPassword\":\"pw123\",\"gatePassword\":\"\",\"maxAttempts\":3}");
        var cfg = ModConfig.load(dir).orElseThrow();
        assertEquals("pw123", cfg.registerPassword());
        assertEquals(3, cfg.maxAttempts());
    }

    @Test
    void missing_file_returns_empty() {
        assertTrue(ModConfig.load(dir).isEmpty());
    }

    @Test
    void missing_field_returns_empty() throws Exception {
        Files.writeString(dir.resolve("mcrobot.json"), "{\"gatePassword\":\"\"}");
        assertTrue(ModConfig.load(dir).isEmpty());
    }
}
