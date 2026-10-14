package com.mcrobot.controlmod;

import org.junit.jupiter.api.Test;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class AutoLoginTest {
    static class Recorder implements AutoLogin.CommandSender {
        final List<String> sent = new ArrayList<>();
        public void send(String cmd) { sent.add(cmd); }
    }

    private AutoLogin newAl(Recorder r) {
        var cfg = new ModConfig("pw123", "gatepw", 3);
        return new AutoLogin(cfg, r);
    }

    @Test
    void sends_register_on_prompt() {
        Recorder r = new Recorder();
        newAl(r).onChat("需要注册", 1000);
        assertEquals(List.of("/yzts register pw123"), r.sent);
    }

    @Test
    void debounces_repeat_within_3s() {
        Recorder r = new Recorder();
        AutoLogin al = newAl(r);
        al.onChat("需要注册", 1000);
        al.onChat("需要注册", 2000);
        assertEquals(1, r.sent.size());
    }

    @Test
    void resends_after_3s() {
        Recorder r = new Recorder();
        AutoLogin al = newAl(r);
        al.onChat("需要注册", 1000);
        al.onChat("需要注册", 5000);
        assertEquals(2, r.sent.size());
    }

    @Test
    void stops_on_locked() {
        Recorder r = new Recorder();
        AutoLogin al = newAl(r);
        al.onChat("需要注册", 1000);
        al.onChat("账号已锁定，请 30 秒后再试。", 1500);
        al.onChat("请先登录", 2000);
        assertEquals(1, r.sent.size());
    }

    @Test
    void respects_max_attempts() {
        Recorder r = new Recorder();
        var cfg = new ModConfig("pw123", "gatepw", 3);
        AutoLogin al = new AutoLogin(cfg, r);
        al.onChat("请先登录", 1000);
        al.onChat("请先登录", 5000);
        al.onChat("请先登录", 9000);
        al.onChat("请先登录", 13000);
        assertEquals(3, r.sent.size());
    }

    @Test
    void sends_gate_with_gate_password() {
        Recorder r = new Recorder();
        newAl(r).onChat("请输入全服密码", 1000);
        assertEquals(List.of("/yzts gate gatepw"), r.sent);
    }
}
