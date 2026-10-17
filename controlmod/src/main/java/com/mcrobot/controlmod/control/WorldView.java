package com.mcrobot.controlmod.control;

import java.util.List;

/** 只读世界视图。字段名就是 HTTP JSON 的字段名。 */
public final class WorldView {
    private WorldView() {}

    /** loaded=false 表示该区块还没加载，此时 id 为空，不能当成空气。 */
    public record BlockAt(int x, int y, int z, String id, boolean loaded) {}

    /** total 是请求区域的实际格数；blocks 最多 limit 个，truncated 表示被截断。 */
    public record Area(List<BlockAt> blocks, int total, boolean truncated) {}

    /** type: block / entity / none。 */
    public record Hit(String type, String id, int x, int y, int z, double distance) {
        public static Hit none() {
            return new Hit("none", "", 0, 0, 0, 0);
        }
    }
}
