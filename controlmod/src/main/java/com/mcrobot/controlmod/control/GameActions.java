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
}
