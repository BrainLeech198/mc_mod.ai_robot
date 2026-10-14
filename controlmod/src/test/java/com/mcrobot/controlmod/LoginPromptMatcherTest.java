package com.mcrobot.controlmod;

import org.junit.jupiter.api.Test;
import static com.mcrobot.controlmod.LoginPromptMatcher.Action;
import static com.mcrobot.controlmod.LoginPromptMatcher.match;
import static org.junit.jupiter.api.Assertions.*;

class LoginPromptMatcherTest {
    @Test void register_prompt() {
        assertEquals(Action.REGISTER, match("需要注册 -【GUI界面】"));
    }
    @Test void login_prompt() {
        assertEquals(Action.LOGIN, match("请先登录"));
    }
    @Test void gate_prompt() {
        assertEquals(Action.GATE, match("请输入全服密码"));
    }
    @Test void locked_stops() {
        assertEquals(Action.STOP_LOCKED, match("账号已锁定，请 30 秒后再试。"));
    }
    @Test void rate_limited_stops() {
        assertEquals(Action.STOP_RATE_LIMITED, match("尝试过于频繁，请稍后再试。"));
    }
    @Test void normal_chat_is_none() {
        assertEquals(Action.NONE, match("<Steve> 我在盖房子"));
    }
    @Test void join_broadcast_is_none() {
        assertEquals(Action.NONE, match("欢迎 DeekSeek 第一次来到服务器！"));
    }
    @Test void login_success_is_none() {
        assertEquals(Action.NONE, match("登录成功，欢迎回来 DeekSeek！"));
    }
}
