# AI 接入 MC 方案（版本 1.20.1-forge）

> 读者：接管本机器人的 AI。目标：把「聊天指挥 + 建造 + 自我纠错」做到**像真 AI 玩家**。
> 本文件记录**调研结论**与**落地架构/计划**。新摸出的做法请继续补写回本文件与 `机器人手册.md`。
> **最近更新：2026-10-16（补网络调研 + 分阶段计划，见 §1.5 / §7）**

---

## 0. 结论速览

- **建造原理（已印证）**：AI 盖楼**不靠视觉、不接管键鼠**，而是「文本上下文 → 生成下一步操作指令」（mc-bench 的原话：像按棋盘坐标下盲棋）。我们的 **WE 指令模式正是这条路**，方向没错；差的是**指令质量**（提示词 + 技能库 + 素材）。
- **我们架构的独家约束**：野粽乡是**公服**，服务端不加 mod；机器人必须是**真人可见的真玩家**（真客户端 + ControlMod），且要被**服务器里其他玩家**在聊天框指挥。2026-10-16 网络调研确认：现成开源项目**没有一个直接满足**这三条（详见 §1.5），所以主线仍是自建；但它们的**工具设计/提示词工程/协议形态**大量可抄。
- **大脑三层次（用户 2026-10-16 拍板「先用伪 API，以后升级」）**：
  - **L0 反射层**（规则，0 延迟，永不漏）——已实装在 `robot.py`。
  - **L1 API 大脑**（OpenAI 兼容端点）——**先用本机 DeepSink 伪 API 服务（人肉/推理 worker 接单），以后换真 API 只改 `robot.json:llm_api.endpoint` + key**。`tools/brain_llm.py` 已实装（2026-10-16）。
  - **L2 Claude Code 在线大脑**（bridge）——已实装，处理复杂规划/审美/兜底。
- **可抄的项目**（2026-10-16 复核均在活跃更新）：mcagent（同版本 Forge，MIT）、MC-AI-Agent-Player/Bot（MIT）、MineAgent（AGPL，只借思想）、minecraft-mcp-server（工具分类学）。

---

## 1. 调研结论（可借鉴清单）

### 1.1 早期调研（2026-10-15，仍然有效）

| 项目 | 是什么 | 可直接抄的 |
|------|--------|-----------|
| **keepermttl/mcagent** | 1.20.1 Forge AI 助手模组，MIT | 18 工具划分；`execute_commands` 玩家上下文+权限4；`scan_blocks`/`get_looking_at`；Undo 快照；提示词「批量合并命令、失败止损」 |
| **kolbytn/mindcraft** | LLM+Mineflayer 代理框架 | 多模型按职责分工；Profile 驱动；embedding 检索示例；声明式任务+科技树；`newAction` |
| **mc-bench/orchestrator** | 「AI 盖楼 PK」评测框架 | 建造任务的**上下文 → 指令**范式；竞技场对比（可用于自我评测） |
| **MineDojo/Voyager** | NVIDIA 具身智能体 | 技能代码库（可复用组合的建造/移动函数）；迭代执行：环境反馈→修正→自检 |
| **moeru-ai/airi** | Minecraft 游戏 Agent | 提示词工程：错误熔断、行动预算、上下文管理；「零 token 反射层」 |
| **ENA-QWQ/MCAA** | Fabric 多 agent 模组 | Master/Commander 多 agent 协作 |
| **Block Dreamer** | 网页版 AI 建筑生成器 | AI → `.schem`/`.mcfunction` → WE `//schem load` + `//paste` 免开发流水线 |

### 1.2 mcagent 深挖（2026-10-16，v1.4.0，Forge 47.x = 我们同版本，MIT）

- **18 工具 function calling 循环**（工具轮数上限默认 20 防空转）：
  - 状态查看：`get_player_state` `get_nearby_entities` `get_inventory` `get_world_info` `scan_blocks` `get_looking_at`（视线射线）
  - 命令执行：`execute_commands`（**玩家上下文 + 权限等级 4**，`@s/@p` 可用，逐条返回输出）、`command_help`、`list_structures`
  - 查询：`get_recipe`（服务器配方表含模组）、`search_jei`/`lookup_jei_recipe`（JEI 集成）
  - 配置：`list_configs`/`read_mod_config`/`write_mod_config`（自动 .bak 备份、路径安全限定）
  - 数据包：`write_datapack_file`/`list_datapack`（AI 直接创建 advancement+function 机制）
  - 联网：`web_search`、`fetch_web_page`（域名白名单+截断）
  - 聊天：`send_chat_message`
- **提示词执行策略**（源自 airi，直接抄进我们的系统提示词）：批量合并命令、失败止损、查询高效；错误熔断、行动预算、上下文管理。
- **DeepSeek 上下文缓存友好裁剪**：历史按「用户轮次」整体裁剪，保证 prompt 前缀稳定以命中缓存（0.1x 计费）——**brain_llm.py 已按此实现历史裁剪**。
- **Token 统计**：会话累计/上下文占用/缓存命中率实时显示。
- **语音输入**：在线 STT API 预设（硅基流动 SiliconFlow 推荐 / 火山方舟 / 阿里云百炼 / OpenAI），按 L 说话。放弃本地 Vosk（JNA 与 Connector 冲突崩服）。
- **安全教训**：它的 AI 以控制台权限执行命令、`requireOp` 默认关——**我们不能这么干**，危险操作必须走白名单（我们的 L0 层就是干这个的）。

### 1.3 MC-AI-Agent-Player / Bot 深挖（2026-10-16，ZeroKitLab，MIT）

- **Player 版 = 「帽子架构」**：AI 是戴在真实玩家头上的大脑，所有动作作用于真玩家（和我们的真客户端路线同构；但它是**服务端 mod**，野粽乡不加 mod，只能借设计）。
  - WebSocket 协议：动作下发 `{"type":"chat","reply":...,"actions":[{"type":"drive_goto","params":{...},"action_id":"act_xxx"}]}`；状态周期上报 `state_update`；动作结果 `action_result` 回传（**action_id 关联**——我们的 outbox `cmd` 以后也要有回执关联）。
  - 工具：`look`/`scan_environment`/`drive_goto`/`drive_mine`/`drive_craft`/`drive_farm`/`drive_eat`/`drive_sleep`/`drive_store`/`drive_place`/`drive_hunt`/`drive_base_set`/`drive_build`/`coord_save`/`block_list`…
  - 移动：LocalWalker（BFS 寻路+输入注入，≤12 格）优先，远途回退 Baritone。
  - **算法建房**：7×7×5 房屋、农田（中心水+锄地）、21×21 基地分区——**这正是我们 `build_lib.py` 参数化模板的方向**。
- **Bot 版（已停更，被 Player 取代，但设计仍值得抄）**：
  - **内置模式直连 DeepSeek/OpenAI 兼容 API**：`config.json` 里 `ai_mode/api_key/model/max_history`（保留最近 10~20 轮）——**和我们 brain_llm.py 的形态一致，验证了方向**。
  - **任务链**：`plan_tasks`/`check_chain`/`resume_chain`/`skip_step`/`cancel_chain`——多步任务编排，建造「先打地基再砌墙」这类流程直接抄。
  - 工具：`chop_tree`/`mine_block`/`follow`/`goto`/`farm`/`hunt`/`eat`/`craft_item`/`cook`/`check_inventory`/`give_item`/`check_status`/`scan_environment`/`scan_recipes`/`identify`/`say`/`ask`/`need`。

### 1.4 MineAgent / MCP / AICompanion（2026-10-16）

- **ControlNet/MineAgent**（1.20.1 Fabric+Forge，**AGPL——只借思想不拷码**）：游戏内 AI Terminal（K 键）；**动作审批 UX**（危险动作先 approve/deny 再执行）；session 管理；`config/mineagent/prompts/` **prompt 覆盖文件**；可加载外部 **MCP 工具**。
- **yuniko-software/minecraft-mcp-server**（Apache-2.0，mineflayer）：工具分类学 = 移动（move-to-position/look-at/jump）/背包（list-inventory/find-item/equip-item）/方块（place-block/dig-block/get-block-info/find-block）/实体（find-entity）/聊天（send-chat）。**mineflayer 过不了 Forge 握手不能直接用**，但分类学可抄；远期可把 ControlMod 包成 MCP server（见 §7 阶段五）。
- **guyu0327/AICompanion**（NeoForge 26.1.2/1.21.x，**版本不符用不了**）：JSON action 协议（MOVE/MINE/ATTACK/CHAT/WAIT/EAT/SLEEP/WAKE_UP/DROP_ITEM/USE_ITEM/PLACE_BLOCK 共 11 种）；**HTTP 异步重试**（429/5xx 指数退避 + 尊重 `Retry-After`）；3 秒决策循环；OpenAI 兼容配置形态（`apiUrl`+`apiKey`+`endpoint`+`timeout`+`maxTokens`+`enableJsonMode`）。→ brain_llm.py 的 HTTP 层抄其重试设计。

### 1.5 为什么现成项目都不能「直接装上用」（2026-10-16 调研结论）

| 项目 | 卡点 |
|------|------|
| mcagent | 个人助手 GUI（H 键自己聊），**不听服务器里其他玩家的聊天**；客户端 mod 但命令执行走 C2S，公服场景不匹配 |
| MC-AI-Agent-Player | **服务端 mod**，野粽乡不加；移动/建造能力我们暂时用 WE 指令覆盖 |
| MC-AI-Agent-Bot | 召唤的是 bot 实体不是真玩家；且停更 |
| MineAgent | AGPL + 同样是「自己终端里聊」而非公服聊天指挥 |
| Mindcraft / minecraft-mcp-server | mineflayer 协议 bot 过不了 Forge 握手（老结论，复核仍成立） |
| AICompanion | NeoForge 1.21，版本不符 |

**结论：主线自建不变；上面所有项目的「工具集 / 提示词 / 协议 / 重试」按 §1.2~1.4 抄。**

---

## 2. 现状与问题

| 问题 | 现状（2026-10-16） | 影响 |
|------|------|------|
| ~~反应漏~~ | 已有 L0 反射层 + 45s 大脑超时兜底 | 基本解决 |
| ~~对话不像 API~~ | L1 `brain_llm.py` 已实装（接 DeepSink 伪 API，可切真 API） | 待真机联调 |
| **建造呆** | 手工 `//set` 拼方盒，无调色/细节 | 不好看 |
| **不会用模组方块** | 只用原版方块 | 少了服务器一半素材（1426 个模组方块已编目） |
| 多步任务无编排 | 单条指令单点执行 | 复杂建造要人盯 |

---

## 3. 目标架构（三层大脑）

```
游戏内聊天（任何玩家可指挥）
   │
   ▼
robot.py（常驻循环，已做）
   ├─ L0 反射层（规则，0 延迟，永不漏）   ← 问候/报位置/查在线/危险拦截+白名单/即时回执
   ├─ L1 LLM 大脑（brain_llm.py → llm_api 端点）  ← 聊天+工具调用（建造/感知）；先用 DeepSink 伪 API，后切真 API
   └─ L2 Claude Code（bridge，在线时）    ← 复杂规划/审美/兜底
```

- **L0 反射层**独立于 LLM，保证「永不漏话」（airi 思路）。
- **L1 = API 级对话的关键**。端点配置在 `robot.json:llm_api`（`endpoint`/`model`/`timeout_seconds`/`history_turns`；key 走环境变量 `MCROBOT_LLM_KEY` 不落文件）。**换真 API = 改 endpoint + 换 key，代码不动**（用户 2026-10-16 拍板「先用伪 API 服务，以后升级」）。
- **L2 保留**：需要「人类级审美/长规划」的活仍走 bridge（`tools/brain.py`）。
- **切换方式**：`brain=bridge`（AI 在线接单）与 `brain=api`（`python tools/brain_llm.py watch` 常驻）二选一；两者共用 inbox/outbox 协议，robot.py 无感。

### 工具集（抄 MCAgent 18 工具 + Agent-Player drive_*，按需裁剪）

| 类别 | 工具（我们的落地形态） |
|------|------|
| 感知 | `get_state`（位置/维度/朝向/手持——control_client 已有）、`scan_blocks`（WE `//size` 等）、`get_looking_at`（需 ControlMod 新增射线） |
| 动作 | `say(text)`、`run_command(cmd)`（**权限门控：只读放行，危险走白名单**）、`move_to/look_at`（后期 ControlMod 原生动作） |
| 建造 | `we_select(p1,p2)`、`we_set/walls/hollow/replace(block)`、`build_lib` 参数化模板（墙/屋顶/窗带/门廊/楼梯/围栏/花圃）、`schem_load(name)`+`paste`、`undo` |
| 编排 | 任务链：`plan_tasks`/`check_chain`/`resume_chain`/`skip_step`/`cancel_chain`（抄 Agent-Bot） |
| 记忆 | `remember/recall`（会话上下文 + 世界事实：建过什么、在哪） |

---

## 4. 建造流水线（三条腿，按质量排序）

1. **Schematic 落地**（质量最高）：LLM/我 生成 `.schem` 或 `.mcfunction` → 传到服务器 `config/worldedit/schematics/` → `//schem load <名>` + `//paste`。
   - 素材来源：Block Dreamer 生成的图、公开 schematic 站、或我用参数化模板离线生成。
2. **参数化模板库（技能库）**：把「墙/坡屋顶/窗带/门廊/楼梯/围栏/花圃」写成可组合函数（抄 Agent-Player 的算法建房：7×7×5 房屋、农田、基地分区），**保证下限**、稳定好看。
3. **LLM 逐指令**（灵活但有上限）：适合小改动/装饰，不适合整栋。

### 素材：模组方块
- 服务器有 **1426 个模组方块**，已编目录：`version/1.20.1-forge/方块目录.md`（`云北城建 yunbeiuc` 977 + `MrCrayfish家具 refurbished_furniture` 449）。
- 用法：`//set yunbeiuc:<名>` / `//set refurbished_furniture:<名>`。选材要先查目录。

---

## 5. 可靠性（抄 airi/mcagent/AICompanion 的提示词与重试工程）

- **常驻**：supervisor 保证 `robot.py` 与大脑不断线（阶段二）。
- **错误熔断**：连续失败 N 次 → 停手 + 上报，不空转（提示词层 + 代码层双保险）。
- **行动预算**：单次任务指令数上限（防 LLM 狂刷；mcagent 工具轮次默认 20）。
- **上下文管理**：只喂最近 K 轮 + 关键世界事实；工具结果截断；**历史按用户轮次整体裁剪保前缀稳定**（命中 DeepSeek 缓存）。
- **HTTP 重试**（brain_llm.py 已实装思路）：超时/5xx 指数退避，尊重 `Retry-After`；仍失败则不写 outbox，交给 robot.py 45s 兜底——**玩家永不落空**。
- **撤回**：`//undo` + 快照（抄 MCAgent/gemini-minecraft），上限与剩量停手规则见 `undo-policy.json`。

---

## 6. 已落地清单（2026-10-16）

| 组件 | 状态 | 位置 |
|------|------|------|
| L0 反射层 + 聊天指挥通道 | ✅ | `tools/robot.py` `tools/robot_chat.py` `version/1.20.1-forge/robot.json` |
| 即时回执 + 45s 大脑超时兜底 | ✅ | `robot.py:auto_reply/check_brain_timeouts` |
| L2 bridge 大脑（AI 在线接单） | ✅ | `tools/brain.py`（watch/read/say/cmd/status） |
| L1 API 大脑 | ✅ 代码完成，待真机联调 | `tools/brain_llm.py`（watch/once/ask/status；`[[cmd]]` 协议经 `rc.analyze` 安全校验） |
| L1 单测 | ✅ | `tools/test_brain_llm.py`（假 transport + 本地 stub HTTP e2e） |
| 伪 API 服务（DeepSink） | ✅ 另一窗口维护中 | `sim/`（`:8788`，OpenAI+Anthropic 双协议，流式时序仿真） |

---

## 7. 分阶段计划（2026-10-16 重排）

**阶段一（已完成）**：自动登录 + 控制通道（ControlMod HTTP 桥）。

**阶段二（进行中）——「API 级对话」**：
1. ✅ L1 `brain_llm.py` + 单测（2026-10-16）
2. ⬜ **真机联调**：DeepSink（`:8788`，另一窗口维护）+ 推理 worker 在线时，玩家聊天 → L1 回复全链路；顺带验证伪 API 的「骗客户端」效果
3. ⬜ **常驻 supervisor**（`tools/supervisor.py`）：保证 `robot.py` + `brain_llm.py watch` 不断线，漏话重挂（抄 AICompanion 重试策略）
4. ⬜ **提示词打磨**：把 mcagent/airi 的执行策略（批量合并、失败止损、行动预算、错误熔断）写进 `brain_llm.py:SYSTEM_PROMPT` 并按真机表现迭代

**阶段三——「建得好看」**：
1. ⬜ `tools/build_lib.py` 参数化建造技能库（墙/坡屋顶/窗带/门廊/楼梯/围栏/花圃 + 调色板含模组方块；抄 Agent-Player 算法建房）
2. ⬜ 任务链编排（chat 级 `plan_tasks` 等，抄 Agent-Bot；先做「一句话→多步→逐步回执」）
3. ⬜ Schematic 流水线（schem 上传 + `//schem load` + `//paste` 封装）

**阶段四——「活人感」**：
1. ⬜ ControlMod 原生动作：`look(yaw,pitch)` 平滑转头 / `swing()` / 走路 / 粒子（替代 `/tp facing`，用户定为后期）
2. ⬜ `state` 增加朝向/手持，便于校验
3. ⬜ 语音输入（可选，抄 mcagent 的 STT 预设：硅基流动等）

**阶段五——「可扩展」**：
1. ⬜ 把 ControlMod 能力包成 **MCP server**（工具分类学抄 minecraft-mcp-server），让任意 MCP 客户端（Claude Desktop 等）都能指挥机器人
2. ⬜ 多 agent 协作（Master/Commander，抄 MCAA）——远期
3. ⬜ 自我评测：抄 mc-bench，「盖同一栋楼」两个版本给人投票对比

---

## 8. 待用户决策

- ~~LLM 大脑用哪家~~ → **已拍板（2026-10-16）**：先用 DeepSink 伪 API 服务（sim/），`llm_api.endpoint` 已预填；以后换真 API 只改配置。
- **建造优先**：先做**参数化模板库**（稳），还是先做 **schematic 载入**（快且好，但要素材）？（推荐：模板库先行，schematic 作为质量上限补充）
- **ControlMod 原生动作**（活人感从指令层升级到客户端动作层）：用户此前定为「后期制作」，是否提前到阶段四？
- **语音输入**：要不要做（需要额外 STT key，硅基流动有免费额度）？
