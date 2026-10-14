package com.mcrobot.controlmod.control;

import java.util.ArrayList;
import java.util.List;

/** 定长环形聊天缓冲。主线程 append，任意线程 since（synchronized）。 */
public class ChatLog {
    public record Entry(long seq, String text) {}
    public record Snapshot(long seq, List<Entry> entries) {}

    private final int capacity;
    private final Entry[] ring;
    private long nextSeq = 1;
    private int size = 0;
    private int head = 0;

    public ChatLog(int capacity) {
        if (capacity < 1) throw new IllegalArgumentException("capacity must be >= 1");
        this.capacity = capacity;
        this.ring = new Entry[capacity];
    }

    public synchronized void append(String text) {
        ring[head] = new Entry(nextSeq++, text);
        head = (head + 1) % capacity;
        if (size < capacity) size++;
    }

    public synchronized long latestSeq() {
        return nextSeq - 1;
    }

    public synchronized Snapshot since(long seq) {
        List<Entry> out = new ArrayList<>();
        int start = ((head - size) % capacity + capacity) % capacity;
        for (int i = 0; i < size; i++) {
            Entry e = ring[(start + i) % capacity];
            if (e.seq() > seq) out.add(e);
        }
        return new Snapshot(latestSeq(), out);
    }
}
