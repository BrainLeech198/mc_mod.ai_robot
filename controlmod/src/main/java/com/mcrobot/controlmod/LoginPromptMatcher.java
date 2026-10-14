package com.mcrobot.controlmod;

public final class LoginPromptMatcher {
    public enum Action { NONE, REGISTER, LOGIN, GATE, STOP_LOCKED, STOP_RATE_LIMITED }

    private LoginPromptMatcher() {}

    public static Action match(String chatText) {
        if (chatText == null) return Action.NONE;
        String t = chatText;
        if (t.contains("账号已锁定")) return Action.STOP_LOCKED;
        if (t.contains("尝试过于频繁")) return Action.STOP_RATE_LIMITED;
        if (t.contains("需要注册")) return Action.REGISTER;
        if (t.contains("需要登录") || t.contains("请先登录")) return Action.LOGIN;
        if (t.contains("全服密码") && t.contains("输入")) return Action.GATE;
        return Action.NONE;
    }
}
