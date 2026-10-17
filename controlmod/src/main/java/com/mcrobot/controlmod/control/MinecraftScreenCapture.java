package com.mcrobot.controlmod.control;

import com.mojang.blaze3d.pipeline.RenderTarget;
import com.mojang.blaze3d.platform.NativeImage;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Screenshot;
import net.minecraftforge.client.event.RenderLevelStageEvent;
import net.minecraftforge.eventbus.api.SubscribeEvent;

import javax.imageio.ImageIO;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 截图实现。
 *
 * 关键点：glReadPixels 必须在某一帧刚画完时读。如果在 HTTP 线程（或随便一个
 * 主线程任务里）直接读，双缓冲已经交换过，读到的是空缓冲。所以这里用「排队 +
 * 在 RenderLevelStageEvent.AFTER_LEVEL 取走」的方式。
 *
 * 也正因为这样，窗口最小化 / 没有渲染帧时不会有结果，调用方必须设超时。
 */
public class MinecraftScreenCapture implements ScreenCapture {
    private static final int TIMEOUT_SEC = 4;

    private final AtomicReference<Pending> pending = new AtomicReference<>();

    private record Pending(int maxWidth, CompletableFuture<WorldView.Shot> future) {}

    @Override
    public CompletableFuture<WorldView.Shot> request(int maxWidth) {
        Minecraft mc = Minecraft.getInstance();
        if (mc == null || mc.level == null || mc.player == null) throw new NotInGameException();
        CompletableFuture<WorldView.Shot> future = new CompletableFuture<>();
        Pending p = new Pending(Math.max(64, maxWidth), future);
        if (!pending.compareAndSet(null, p)) {
            throw new IllegalStateException("capture busy");
        }
        future.orTimeout(TIMEOUT_SEC, TimeUnit.SECONDS);
        // 超时也要把占位清掉，否则之后每次截图都会 busy
        future.whenComplete((r, e) -> pending.compareAndSet(p, null));
        return future;
    }

    @SubscribeEvent
    public void onRenderStage(RenderLevelStageEvent event) {
        if (event.getStage() != RenderLevelStageEvent.Stage.AFTER_LEVEL) return;
        Pending p = pending.getAndSet(null);
        if (p == null) return;
        try {
            p.future().complete(grab(p.maxWidth()));
        } catch (Throwable t) {
            p.future().completeExceptionally(t);
        }
    }

    private static WorldView.Shot grab(int maxWidth) throws IOException {
        Minecraft mc = Minecraft.getInstance();
        RenderTarget target = mc.getMainRenderTarget();
        NativeImage image = Screenshot.takeScreenshot(target);
        try {
            int w = image.getWidth();
            int h = image.getHeight();
            int[] rgba = image.getPixelsRGBA();
            BufferedImage full = new BufferedImage(w, h, BufferedImage.TYPE_INT_ARGB);
            int[] argb = new int[w * h];
            for (int y = 0; y < h; y++) {
                // Screenshot.takeScreenshot 内部已经 flipY()，这里直接用原顺序
                int row = y * w;
                for (int x = 0; x < w; x++) {
                    int p = rgba[row + x];
                    // NativeImage 按小端存 RGBA（0xAABBGGRR），BufferedImage 要 ARGB。
                    // 主帧缓冲的 alpha 不可靠，一律按不透明处理。
                    argb[row + x] = 0xFF000000
                            | ((p & 0xFF) << 16)
                            | (p & 0x00FF00)
                            | ((p >>> 16) & 0xFF);
                }
            }
            full.setRGB(0, 0, w, h, argb, 0, w);

            BufferedImage out = full;
            if (w > maxWidth) {
                int nw = maxWidth;
                int nh = Math.max(1, Math.round(h * (nw / (float) w)));
                BufferedImage small = new BufferedImage(nw, nh, BufferedImage.TYPE_INT_ARGB);
                Graphics2D g = small.createGraphics();
                g.setRenderingHint(RenderingHints.KEY_INTERPOLATION,
                        RenderingHints.VALUE_INTERPOLATION_BILINEAR);
                g.drawImage(full, 0, 0, nw, nh, null);
                g.dispose();
                out = small;
            }

            ByteArrayOutputStream buf = new ByteArrayOutputStream(
                    Math.max(8192, out.getWidth() * out.getHeight() / 4));
            if (!ImageIO.write(out, "png", buf)) throw new IOException("no png writer");
            return new WorldView.Shot(buf.toByteArray(), out.getWidth(), out.getHeight());
        } finally {
            image.close();
        }
    }
}
