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

    @Override
    public void closeScreen() {
        Minecraft mc = Minecraft.getInstance();
        if (mc == null) throw new NotInGameException();
        // 调 onClose 而非直接 setScreen(null)：让 AbstractContainerScreen 顺带关闭容器
        if (mc.screen != null) {
            mc.screen.onClose();
        }
    }

    private static LocalPlayer requirePlayer() {
        Minecraft mc = Minecraft.getInstance();
        LocalPlayer p = mc == null ? null : mc.player;
        if (p == null) throw new NotInGameException();
        return p;
    }
}
