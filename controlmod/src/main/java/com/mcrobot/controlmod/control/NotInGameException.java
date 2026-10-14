package com.mcrobot.controlmod.control;

/** 客户端不在游戏世界中（player == null）时由 GameActions 抛出。 */
public class NotInGameException extends RuntimeException {
    public NotInGameException() {
        super("not in game");
    }
}
