package com.mcrobot.controlmod.control;

import java.util.concurrent.CompletableFuture;

/** 截图。request 可在任意线程调用，结果在渲染线程完成。 */
public interface ScreenCapture {
    /**
     * 请求一张当前画面。maxWidth 是期望的最大宽度（会等比缩小），
     * 返回值在下一帧画完后完成；窗口最小化/不在游戏中时会失败或超时。
     */
    CompletableFuture<WorldView.Shot> request(int maxWidth);
}
