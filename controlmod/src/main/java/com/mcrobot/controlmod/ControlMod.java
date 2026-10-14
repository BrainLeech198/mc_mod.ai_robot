package com.mcrobot.controlmod;

import net.minecraft.client.Minecraft;
import net.minecraftforge.client.event.ClientChatReceivedEvent;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.eventbus.api.SubscribeEvent;
import net.minecraftforge.fml.common.Mod;
import java.nio.file.Path;

@Mod(ControlMod.MODID)
public class ControlMod {
    public static final String MODID = "mcrobot_control";

    private AutoLogin autoLogin;

    public ControlMod() {
        MinecraftForge.EVENT_BUS.register(this);
    }

    @SubscribeEvent
    public void onChat(ClientChatReceivedEvent event) {
        if (autoLogin == null) {
            Path gameDir = Minecraft.getInstance().gameDirectory.toPath();
            autoLogin = ModConfig.load(gameDir)
                    .map(cfg -> new AutoLogin(cfg, cmd -> {
                        Minecraft mc = Minecraft.getInstance();
                        if (mc.player != null) {
                            String command = cmd.startsWith("/") ? cmd.substring(1) : cmd;
                            mc.player.connection.sendCommand(command);
                        }
                    }))
                    .orElse(null);
            if (autoLogin == null) return;
        }
        autoLogin.onChat(event.getMessage().getString(), System.currentTimeMillis());
    }
}
