# ControlMod 阶段一（自动登录）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 客户端进服后自动完成 `yzt_super` 的注册/登录，不再需要人工在聊天栏敲命令。

**Architecture:** 客户端 Forge 1.20.1 mod，监听 `ClientChatReceivedEvent`，识别 `yzt_super` 的中文提示文案，自动发送 `/yzts register|login|gate` 指令。配置（密码等）由启动器写到 `instance/mcrobot.json`，mod 启动时读取。事件总线注册在 mod 构造器，全部逻辑在客户端侧，不碰服务端。

**Tech Stack:** Java 17、Forge 1.20.1（47.4.x）ForgeGradle 6、Gson（MC 自带）、JUnit 5（纯逻辑测试）

**Spec:** `docs/superpowers/specs/2026-10-13-mc-robot-design.md`

## Global Constraints

- 目标 MC 版本 1.20.1，Forge 47.4.16，Java 17
- mod 只跑客户端（`dist = CLIENT` 或仅客户端事件）
- 密码文件 `instance/mcrobot.json` **不进 git**（已在 `.gitignore` 覆盖 `version/*/config.json`，本文件也要加）
- 安全规范 §1 仍然有效：操作留痕、失败停手不乱重试（登录重试上限 3 次）
- 不修改用户 PCL2 环境；产物 jar 放 `instance/mods/`

## Review Focus

1. **聊天文案匹配过宽** → 误触发注册指令；测试必须覆盖：`需要注册`、`请先登录`、`请输入全服密码` 三个正样本，以及普通玩家聊天/系统广播等负样本
2. **重复发送** → 同一提示在几秒内多次出现（Forge 可能重复投递）；需要去抖，测试覆盖「3 秒内重复提示只发一次」
3. **密码写进聊天回显** → 发送指令时不能把密码记进日志；测试覆盖日志输出不含密码
4. **config.json 缺失/字段缺失** → mod 不能崩游戏；测试覆盖缺失文件与缺字段路径
5. **登录失败后的锁定** → 服务器 `login_locked` 提示后必须停止重试；测试覆盖锁定文案触发停止

---

### Task 1: 项目骨架与构建

**Files:**
- Create: `controlmod/build.gradle`
- Create: `controlmod/settings.gradle`
- Create: `controlmod/gradle.properties`
- Create: `controlmod/src/main/resources/META-INF/mods.toml`
- Create: `controlmod/src/main/resources/pack.mcmeta`
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/ControlMod.java`

**Interfaces:**
- Produces: 可构建的 Forge mod 工程；mod id = `mcrobot_control`，主类 `com.mcrobot.controlmod.ControlMod`

- [ ] **Step 1: 建工程文件**

`controlmod/gradle.properties`：

```properties
org.gradle.jvmargs=-Xmx3G
minecraft_version=1.20.1
forge_version=47.4.16
mod_id=mcrobot_control
mod_version=0.1.0
```

`controlmod/settings.gradle`：

```groovy
pluginManagement {
    repositories {
        gradlePluginPortal()
        maven { url = 'https://maven.minecraftforge.net/' }
    }
}
rootProject.name = 'controlmod'
```

`controlmod/build.gradle`：

```groovy
plugins {
    id 'eclipse'
    id 'idea'
    id 'net.minecraftforge.gradle' version '[6.0,6.2)'
}

version = mod_version
group = 'com.mcrobot'

base { archivesName = mod_id }

java.toolchain.languageVersion = JavaLanguageVersion.of(17)

minecraft {
    mappings channel: 'official', version: minecraft_version
    runs {
        client {
            workingDirectory project.file('run')
            property 'forge.logging.console.level', 'debug'
            mods { mcrobot_control { source sourceSets.main } }
        }
    }
}

sourceSets.main.resources { srcDir 'src/generated/resources' }

dependencies {
    minecraft "net.minecraftforge:forge:${minecraft_version}-${forge_version}"
    testImplementation 'org.junit.jupiter:junit-jupiter:5.10.2'
}

tasks.named('test', Test) { useJUnitPlatform() }

tasks.withType(JavaCompile).configureEach {
    options.encoding = 'UTF-8'
}
```

`controlmod/src/main/resources/META-INF/mods.toml`：

```toml
modLoader="javafml"
loaderVersion="[47,)"
license="MIT"

[[mods]]
modId="mcrobot_control"
version="0.1.0"
displayName="MC Robot Control"
description="AI 进服自动登录与控制"

[[dependencies.mcrobot_control]]
modId="forge"
mandatory=true
versionRange="[47,)"
ordering="NONE"
side="CLIENT"

[[dependencies.mcrobot_control]]
modId="minecraft"
mandatory=true
versionRange="[1.20.1]"
ordering="NONE"
side="CLIENT"
```

`controlmod/src/main/resources/pack.mcmeta`：

```json
{ "pack": { "description": "mcrobot_control", "pack_format": 15 } }
```

`controlmod/src/main/java/com/mcrobot/controlmod/ControlMod.java`：

```java
package com.mcrobot.controlmod;

import net.minecraftforge.fml.common.Mod;

@Mod(ControlMod.MODID)
public class ControlMod {
    public static final String MODID = "mcrobot_control";

    public ControlMod() {
        // 阶段一：事件注册在 Task 3
    }
}
```

- [ ] **Step 2: 构建验证**

```bash
cd controlmod && ./gradlew build
```

首次构建会下载 ForgeGradle，耗时较长。Expected: `BUILD SUCCESSFUL`，产出 `build/libs/mcrobot_control-0.1.0.jar`。

（Windows 下若无 `./gradlew`，先 `gradle wrapper` 或用本机 gradle 8.x 执行。）

- [ ] **Step 3: Commit**

```bash
git add controlmod
git commit -m "feat: ControlMod 工程骨架"
```

---

### Task 2: 配置加载（含密码安全）

**Files:**
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/ModConfig.java`
- Create: `controlmod/src/test/java/com/mcrobot/controlmod/ModConfigTest.java`
- Modify: `tools/launch_client.py`（启动前写 `instance/mcrobot.json`）
- Modify: `.gitignore`

**Interfaces:**
- Produces: `ModConfig.load(Path gameDir)` → `Optional<ModConfig>`；字段 `registerPassword`、`gatePassword`、`maxAttempts`（int，默认 3）
- 后续 Task 3 消费 `ModConfig`

- [ ] **Step 1: 写失败测试**

`controlmod/src/test/java/com/mcrobot/controlmod/ModConfigTest.java`：

```java
package com.mcrobot.controlmod;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.file.*;
import static org.junit.jupiter.api.Assertions.*;

class ModConfigTest {
    @TempDir Path dir;

    @Test
    void loads_valid_config() throws Exception {
        Files.writeString(dir.resolve("mcrobot.json"),
            "{\"registerPassword\":\"pw123\",\"gatePassword\":\"\",\"maxAttempts\":3}");
        var cfg = ModConfig.load(dir).orElseThrow();
        assertEquals("pw123", cfg.registerPassword());
        assertEquals(3, cfg.maxAttempts());
    }

    @Test
    void missing_file_returns_empty() {
        assertTrue(ModConfig.load(dir).isEmpty());
    }

    @Test
    void missing_field_returns_empty() throws Exception {
        Files.writeString(dir.resolve("mcrobot.json"), "{\"gatePassword\":\"\"}");
        assertTrue(ModConfig.load(dir).isEmpty());
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd controlmod && ./gradlew test
```

Expected: FAIL，`ModConfig` 未定义。

- [ ] **Step 3: 实现 ModConfig**

`controlmod/src/main/java/com/mcrobot/controlmod/ModConfig.java`：

```java
package com.mcrobot.controlmod;

import com.google.gson.Gson;
import java.nio.file.*;
import java.util.Optional;

public record ModConfig(String registerPassword, String gatePassword, int maxAttempts) {
    public static Optional<ModConfig> load(Path gameDir) {
        Path file = gameDir.resolve("mcrobot.json");
        if (!Files.exists(file)) return Optional.empty();
        try {
            Gson gson = new Gson();
            Raw raw = gson.fromJson(Files.readString(file), Raw.class);
            if (raw == null || raw.registerPassword == null) return Optional.empty();
            int attempts = raw.maxAttempts > 0 ? raw.maxAttempts : 3;
            return Optional.of(new ModConfig(raw.registerPassword,
                    raw.gatePassword == null ? "" : raw.gatePassword, attempts));
        } catch (Exception e) {
            return Optional.empty();
        }
    }

    private static class Raw {
        String registerPassword;
        String gatePassword;
        int maxAttempts;
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd controlmod && ./gradlew test
```

Expected: PASS（3 个用例）。

- [ ] **Step 5: 启动器写配置 + gitignore**

`tools/launch_client.py` 的 `main()` 里，`Popen` 之前加：

```python
import json as _json
cfg_src = os.path.join(WORKSPACE, "version", "1.20.1-forge", "config.json")
with open(cfg_src, encoding="utf-8") as f:
    vcfg = _json.load(f)
mcrobot_cfg = {
    "registerPassword": vcfg["account"]["password"],
    "gatePassword": vcfg["account"].get("gate_password", ""),
    "maxAttempts": 3,
}
with open(os.path.join(INSTANCE_DIR, "mcrobot.json"), "w", encoding="utf-8") as f:
    _json.dump(mcrobot_cfg, f, ensure_ascii=False)
```

`.gitignore` 追加一行：

```
instance/mcrobot.json
instance/logs/
```

- [ ] **Step 6: Commit**

```bash
git add controlmod/src tools/launch_client.py .gitignore
git commit -m "feat: ControlMod 配置加载，启动器同步密码到 instance/mcrobot.json"
```

---

### Task 3: 提示文案匹配器

**Files:**
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/LoginPromptMatcher.java`
- Create: `controlmod/src/test/java/com/mcrobot/controlmod/LoginPromptMatcherTest.java`

**Interfaces:**
- Produces: `LoginPromptMatcher.Action match(String chatText)` → 枚举 `NONE / REGISTER / LOGIN / GATE / STOP_LOCKED / STOP_RATE_LIMITED`
- Task 4 消费此枚举

- [ ] **Step 1: 写失败测试**

`controlmod/src/test/java/com/mcrobot/controlmod/LoginPromptMatcherTest.java`：

```java
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
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd controlmod && ./gradlew test
```

Expected: FAIL，`LoginPromptMatcher` 未定义。

- [ ] **Step 3: 实现匹配器**

`controlmod/src/main/java/com/mcrobot/controlmod/LoginPromptMatcher.java`：

```java
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
        if (t.contains("请先登录")) return Action.LOGIN;
        if (t.contains("全服密码") && t.contains("输入")) return Action.GATE;
        return Action.NONE;
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd controlmod && ./gradlew test
```

Expected: PASS（8 个用例）。

- [ ] **Step 5: Commit**

```bash
git add controlmod/src
git commit -m "feat: yzt_super 提示文案匹配器"
```

---

### Task 4: 聊天监听与自动指令（含去抖与重试上限）

**Files:**
- Create: `controlmod/src/main/java/com/mcrobot/controlmod/AutoLogin.java`
- Create: `controlmod/src/test/java/com/mcrobot/controlmod/AutoLoginTest.java`
- Modify: `controlmod/src/main/java/com/mcrobot/controlmod/ControlMod.java`

**Interfaces:**
- Consumes: `ModConfig.load`、`LoginPromptMatcher.match`
- Produces: `AutoLogin.onChat(String text, long nowMs, CommandSender sender)`（纯逻辑，可测）；`interface CommandSender { void send(String cmd); }`
- `ControlMod` 构造器注册 `@SubscribeEvent` 的客户端聊天监听，把真实事件转给 `AutoLogin`

- [ ] **Step 1: 写失败测试**

`controlmod/src/test/java/com/mcrobot/controlmod/AutoLoginTest.java`：

```java
package com.mcrobot.controlmod;

import org.junit.jupiter.api.Test;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class AutoLoginTest {
    static class Recorder implements CommandSender {
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
        newAl(r).onChat("需要注册", 1000, r);
        assertEquals(List.of("/yzts register pw123"), r.sent);
    }

    @Test
    void debounces_repeat_within_3s() {
        Recorder r = new Recorder();
        AutoLogin al = newAl(r);
        al.onChat("需要注册", 1000, r);
        al.onChat("需要注册", 2000, r);
        assertEquals(1, r.sent.size());
    }

    @Test
    void resends_after_3s() {
        Recorder r = new Recorder();
        AutoLogin al = newAl(r);
        al.onChat("需要注册", 1000, r);
        al.onChat("需要注册", 5000, r);
        assertEquals(2, r.sent.size());
    }

    @Test
    void stops_on_locked() {
        Recorder r = new Recorder();
        AutoLogin al = newAl(r);
        al.onChat("需要注册", 1000, r);
        al.onChat("账号已锁定，请 30 秒后再试。", 1500, r);
        al.onChat("请先登录", 2000, r);
        assertEquals(1, r.sent.size());
    }

    @Test
    void respects_max_attempts() {
        Recorder r = new Recorder();
        var cfg = new ModConfig("pw123", "gatepw", 3);
        AutoLogin al = new AutoLogin(cfg, r);
        al.onChat("请先登录", 1000, r);
        al.onChat("请先登录", 5000, r);
        al.onChat("请先登录", 9000, r);
        al.onChat("请先登录", 13000, r);
        assertEquals(3, r.sent.size());
    }

    @Test
    void sends_gate_with_gate_password() {
        Recorder r = new Recorder();
        newAl(r).onChat("请输入全服密码", 1000, r);
        assertEquals(List.of("/yzts gate gatepw"), r.sent);
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd controlmod && ./gradlew test
```

Expected: FAIL，`AutoLogin`/`CommandSender` 未定义。

- [ ] **Step 3: 实现 AutoLogin**

`controlmod/src/main/java/com/mcrobot/controlmod/AutoLogin.java`：

```java
package com.mcrobot.controlmod;

public class AutoLogin {
    public interface CommandSender { void send(String cmd); }

    private static final long DEBOUNCE_MS = 3000;

    private final ModConfig config;
    private long lastSentMs = Long.MIN_VALUE;
    private int attempts = 0;
    private boolean stopped = false;

    public AutoLogin(ModConfig config, CommandSender sender) {
        this.config = config;
    }

    public void onChat(String text, long nowMs, CommandSender sender) {
        if (stopped) return;
        LoginPromptMatcher.Action action = LoginPromptMatcher.match(text);
        switch (action) {
            case STOP_LOCKED, STOP_RATE_LIMITED -> stopped = true;
            case REGISTER -> maybeSend(nowMs, sender, "/yzts register " + config.registerPassword());
            case LOGIN -> maybeSend(nowMs, sender, "/yzts login " + config.registerPassword());
            case GATE -> {
                if (!config.gatePassword().isEmpty()) {
                    maybeSend(nowMs, sender, "/yzts gate " + config.gatePassword());
                }
            }
            case NONE -> { }
        }
    }

    private void maybeSend(long nowMs, CommandSender sender, String cmd) {
        if (attempts >= config.maxAttempts()) { stopped = true; return; }
        if (nowMs - lastSentMs < DEBOUNCE_MS) return;
        lastSentMs = nowMs;
        attempts++;
        sender.send(cmd);
    }
}
```

注意：`onChat` 的 `sender` 参数与构造器注入二选一即可，实现时**只保留构造器注入**，测试里把 `onChat` 签名改为 `onChat(String text, long nowMs)`，与实现一致（避免双来源不一致）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd controlmod && ./gradlew test
```

Expected: PASS（6 个用例）。

- [ ] **Step 5: 接入 Forge 聊天事件**

`ControlMod.java` 改为：

```java
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
                    .map(cfg -> new AutoLogin(cfg, cmd ->
                            Minecraft.getInstance().player != null
                                    && Minecraft.getInstance().player.chat(cmd)
                                    || false))
                    .orElse(null);
            if (autoLogin == null) return;
        }
        autoLogin.onChat(event.getMessage().getString(), System.currentTimeMillis());
    }
}
```

实现时注意 `player.chat` 返回 void，`CommandSender` 适配器写成 lambda 语句块即可。密码不得出现在任何日志/`event` 回显里。

- [ ] **Step 6: 构建并部署到 instance**

```bash
cd controlmod && ./gradlew build
cp build/libs/mcrobot_control-0.1.0.jar ../instance/mods/
```

Expected: jar 出现在 `instance/mods/`。

- [ ] **Step 7: Commit**

```bash
git add controlmod
git commit -m "feat: 聊天监听自动登录，含去抖与重试上限"
```

---

### Task 5: 端到端验证（真实进服）

**Files:**
- 无新文件；验收步骤

**Interfaces:**
- Consumes: Task 2 的 `mcrobot.json`、Task 4 的 jar

- [ ] **Step 1: 确认配置已生成**

```bash
python3 tools/launch_client.py --dry-run && ls instance/mcrobot.json
```

（`--dry-run` 不该写配置——若实现时把写配置放 Popen 前，则单独跑一次启动再杀进程；确认 `instance/mcrobot.json` 内容正确。）

- [ ] **Step 2: 启动客户端并观察自动注册**

```bash
python3 tools/launch_client.py
```

在 `instance/logs/latest.log` 里验证序列：
1. `Connecting to play.simpfun.cn, 15685`
2. 出现「需要注册」提示
3. **紧接着出现 `/yzts register` 的发送记录或服务器回「注册成功，已自动登录」**

Expected: DeekSeek 自动完成注册并进入世界，无需人工敲命令。

- [ ] **Step 3: 二次启动验证自动登录**

关闭客户端，再次 `python3 tools/launch_client.py`。
Expected: 服务器提示「请先登录」→ 自动发 `/yzts login` → 「登录成功」。

- [ ] **Step 4: 验证失败停手**

把 `instance/mcrobot.json` 的密码改成错的，重启进服。
Expected: 连续失败后出现「账号已锁定」或达到 maxAttempts，**不再发送**，日志无密码明文。

- [ ] **Step 5: 恢复正确密码并 Commit**

```bash
git add -A && git commit -m "test: 端到端验证自动注册/登录"
```

---

## 自审结果

1. **Spec 覆盖**：本计划只覆盖 spec §5（ControlMod）中「登录自动化」与 §8；HTTP/WS 接口、WE 集成、越界拦截留待后续计划——符合「阶段一先跑通登录」的范围约定。
2. **占位符**：无 TBD/TODO；Task 4 Step 3 的签名不一致已标注「实现时只保留构造器注入」并给出确定写法。
3. **类型一致性**：`ModConfig` record 字段名、`LoginPromptMatcher.Action` 枚举值、`AutoLogin.onChat` 签名在各任务间一致。
4. **Review Focus**：5 条风险各自绑定了测试用例（文案匹配 8 例、去抖/重试 6 例、配置 3 例、日志不含密码在 Task 5 Step 4 人工验证）。
