package com.mcrobot.controlmod;

import com.mcrobot.controlmod.control.ChatLog;
import com.mcrobot.controlmod.control.ControlServer;
import com.mcrobot.controlmod.control.ControlToken;
import com.mcrobot.controlmod.control.GameBridge;
import com.mcrobot.controlmod.control.MinecraftGameActions;
import com.mcrobot.controlmod.control.MinecraftScreenCapture;
import net.minecraft.client.Minecraft;
import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.client.event.ClientChatReceivedEvent;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.eventbus.api.IEventBus;
import net.minecraftforge.eventbus.api.SubscribeEvent;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.event.lifecycle.FMLClientSetupEvent;
import net.minecraftforge.fml.javafmlmod.FMLJavaModLoadingContext;
import net.minecraftforge.fml.loading.FMLEnvironment;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Path;

@Mod(ControlMod.MODID)
public class ControlMod {
    public static final String MODID = "mcrobot_control";
    private static final Logger LOGGER = LoggerFactory.getLogger(MODID);
    // 与 version/1.20.1-forge/runtime.json 的 control_mod.http_port 一致
    private static final int CONTROL_PORT = 17890;

    private final ChatLog chatLog = new ChatLog(200);
    private final MinecraftScreenCapture screenCapture = new MinecraftScreenCapture();
    private AutoLogin autoLogin;
    private boolean configAttempted = false;
    private ControlServer controlServer;

    public ControlMod() {
        if (FMLEnvironment.dist != Dist.CLIENT) return;
        IEventBus modEventBus = FMLJavaModLoadingContext.get().getModEventBus();
        modEventBus.addListener(this::onClientSetup);
        MinecraftForge.EVENT_BUS.register(this);
        // 截图必须在渲染阶段（AFTER_LEVEL）抓帧，所以它自己订阅事件
        MinecraftForge.EVENT_BUS.register(screenCapture);
    }

    private void onClientSetup(FMLClientSetupEvent event) {
        Path gameDir = Minecraft.getInstance().gameDirectory.toPath();
        try {
            String token = ControlToken.loadOrCreate(gameDir);
            GameBridge bridge = new GameBridge(new MinecraftGameActions(), Minecraft.getInstance()::execute, 5000);
            controlServer = new ControlServer(CONTROL_PORT, token, bridge, chatLog, screenCapture);
            controlServer.start();
            LOGGER.info("ControlMod control channel on 127.0.0.1:{}", controlServer.port());
        } catch (IOException e) {
            LOGGER.warn("ControlMod control channel failed to start on port {}: {}", CONTROL_PORT, e.toString());
        }
    }

    @SubscribeEvent
    public void onChat(ClientChatReceivedEvent event) {
        String text = event.getMessage().getString();
        chatLog.append(text);
        if (autoLogin == null) {
            if (configAttempted) return;
            configAttempted = true;
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
        autoLogin.onChat(text, System.currentTimeMillis());
    }
}
