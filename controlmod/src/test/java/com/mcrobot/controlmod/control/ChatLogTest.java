package com.mcrobot.controlmod.control;

import org.junit.jupiter.api.Test;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;

class ChatLogTest {
    @Test void emptyLog() {
        ChatLog log = new ChatLog(3);
        assertEquals(0, log.latestSeq());
        assertTrue(log.since(0).entries().isEmpty());
    }

    @Test void assignsIncreasingSeqInOrder() {
        ChatLog log = new ChatLog(3);
        log.append("a");
        log.append("b");
        assertEquals(2, log.latestSeq());
        List<ChatLog.Entry> e = log.since(0).entries();
        assertEquals(2, e.size());
        assertEquals(1, e.get(0).seq());
        assertEquals("a", e.get(0).text());
        assertEquals(2, e.get(1).seq());
        assertEquals("b", e.get(1).text());
    }

    @Test void sinceFiltersBySeq() {
        ChatLog log = new ChatLog(3);
        log.append("a");
        log.append("b");
        List<ChatLog.Entry> e = log.since(1).entries();
        assertEquals(1, e.size());
        assertEquals("b", e.get(0).text());
        assertEquals(2, log.since(1).seq());
    }

    @Test void evictsOldestWhenFull() {
        ChatLog log = new ChatLog(2);
        log.append("a");
        log.append("b");
        log.append("c");
        assertEquals(3, log.latestSeq());
        List<ChatLog.Entry> e = log.since(0).entries();
        assertEquals(2, e.size());
        assertEquals("b", e.get(0).text());
        assertEquals("c", e.get(1).text());
    }
}
