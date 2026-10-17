package com.mcrobot.controlmod.control;

/** 游戏动作。实现方约定：所有方法都在客户端主线程被调用。 */
public interface GameActions {
    /** 不在游戏中返回 null。 */
    PlayerState playerState();
    /** commandWithoutSlash 形如 "yzts login x" 或 "/set stone"（已去掉一个前导斜杠）。 */
    void sendCommand(String commandWithoutSlash);
    void sendChat(String text);
    /** 关闭当前打开的界面（等价按 ESC）；无界面时为无害空操作。 */
    void closeScreen();

    /** 读单个方块。区块未加载时 loaded=false，不可当成空气。 */
    WorldView.BlockAt blockAt(int x, int y, int z);

    /** 读长方体区域（含端点）。limit 限制返回条数，防止一次拉爆。 */
    WorldView.Area blocksInBox(int x1, int y1, int z1, int x2, int y2, int z2, int limit);

    /** 准星指向的方块/实体；没指向任何东西时 type=none。 */
    WorldView.Hit lookingAt();
}
