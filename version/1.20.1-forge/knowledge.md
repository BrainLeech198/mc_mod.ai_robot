# 机器人知识库（本版本内查表）

> 这份文档是 **AI 自己用的表**，不是给玩家看的说明书（那个是 `机器人手册.md`）。
> 上半区人工维护，下半区由 `memory/facts.jsonl` 自动汇总：
> `python -m mcrobot.knowledge consolidate`
> 红线：自动区每次整体重写，别在里面手改；要加东西用 `remember` 或写进手写区。

## 手写区（consolidate 不会动这里）

- （暂无）

## 自动汇总

<!-- AUTO:FACTS:BEGIN（由 facts.jsonl 生成，手改会被覆盖） -->

### 坐标
- 机器人常在 multiworld:ai 活动；第二所学校选址在 (-57,-47,-282) 附近 —— 机器人手册 / identity.md · 2026-10-17

### 安全
- 出生点曾在岩浆上导致死亡循环：先 /tp 到安全点，再 /spawnpoint，最后 /data get entity DeepSeek Health 验满血 —— 机器人手册 §8.5.2 · 2026-10-17

### 指令
- 改世界/权限类指令只有服主 oahnuy198 能授权，白名单在 robot.json:auth.dangerous_allowed —— 机器人手册 §8.3 · 2026-10-17
- WorldEdit 需要 OP；选区用 //pos1 x,y,z 和 //pos2 x,y,z，//size 看体积 —— 机器人手册 §6 · 2026-10-17

### 方块
- CFM 确认存在：desk、table、chair、cabinet、crate、ceiling_light、lamp、rubbish_bin、sink、toilet、basin、lattice_fence —— 方块目录.md · 2026-10-17
- CFM 家具没有窗帘、黑板、数字时钟、书柜；垃圾桶真名是 rubbish_bin（不是 trash_can） —— 方块目录.md（2026-10-16 核实）
- 方块目录的显示名是英文，中文查不到就换英文关键词（desk/chair/lamp/rubbish_bin） —— 方块目录.md · 2026-10-17

### 玩家
- oahnuy198 讨厌呆方盒，要求先商讨细节再动砖，喜欢用模组方块做细节 —— 玩家原话（2026-10-15）

### 皮肤
- 换皮肤用 /skin set web slim <url>；/skin clear 会清空皮肤，别手滑 —— 机器人手册 §4 · 2026-10-17

### 移动
- 寻路交给 Baritone：goto/follow/goto_stop（聊天前缀 #，纯客户端，不进服务器） —— 机器人手册 §9.1.1 · 2026-10-17

### 视觉
- look 已接通：MiMo mimo-v2.6-flash 真吃图（响应 image_tokens>0），854×480 实测 6~19 秒 —— 真机回执（2026-10-17）
- 推理型视觉模型 max_tokens 太小时只吐 reasoning_content、content 为空；默认给 600 —— 真机回执（2026-10-17）
- DeepSink 会把 messages.content 拍平成文本（deepsink_server.py:_text_of），图片过不去，视觉 endpoint 必须直连上游 —— mock_api/deepsink_server.py:210 · 2026-10-17

### 验收
- 改世界的目标必须有 block_at/scan_region 回执才算成功；区块没加载返回 loaded=false，不能当空气 —— 机器人手册 §9.2 · 2026-10-17

<!-- AUTO:FACTS:END -->
