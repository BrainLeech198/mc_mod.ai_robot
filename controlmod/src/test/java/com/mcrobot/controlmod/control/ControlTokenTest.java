package com.mcrobot.controlmod.control;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import static org.junit.jupiter.api.Assertions.*;

class ControlTokenTest {
    @Test void generatesAndWritesWhenMissing(@TempDir Path dir) throws Exception {
        String token = ControlToken.loadOrCreate(dir);
        assertTrue(token.matches("[0-9a-f]{32}"), token);
        assertEquals(token, Files.readString(dir.resolve("control_token"), StandardCharsets.UTF_8));
    }

    @Test void reusesExistingToken(@TempDir Path dir) throws Exception {
        Files.writeString(dir.resolve("control_token"), "  existing-token\n", StandardCharsets.UTF_8);
        assertEquals("existing-token", ControlToken.loadOrCreate(dir));
    }
}
