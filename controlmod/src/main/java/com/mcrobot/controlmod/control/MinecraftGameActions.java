package com.mcrobot.controlmod.control;

import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;

/** GameActions 的 Forge 客户端实现；必须在主线程调用。 */
public class MinecraftGameActions implements GameActions {
    @Override
    public PlayerState playerState() {
        Minecraft mc = Minecraft.getInstance();
        LocalPlayer p = mc == null ? null : mc.player;
        if (p == null) return null;
        String dim = mc.level == null ? "" : mc.level.dimension().location().toString();
        return new PlayerState(p.getGameProfile().getName(), p.getX(), p.getY(), p.getZ(), dim);
    }

    @Override
    public void sendCommand(String commandWithoutSlash) {
        LocalPlayer p = requirePlayer();
        p.connection.sendCommand(commandWithoutSlash);
    }

    @Override
    public void sendChat(String text) {
        LocalPlayer p = requirePlayer();
        p.connection.sendChat(text);
    }

    private static LocalPlayer requirePlayer() {
        Minecraft mc = Minecraft.getInstance();
        LocalPlayer p = mc == null ? null : mc.player;
        if (p == null) throw new NotInGameException();
        return p;
    }
}
