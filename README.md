# mc_robot —— MC 接入 AI 的封装 agent

让一个**真 Minecraft 客户端**里的账号变成能被服务器里其他玩家用聊天指挥的 AI 玩家。

和其他方案的关键差别：野粽乡是**公服**，服务端不能加 mod，所以机器人必须是**真人可见的真玩家**
（真客户端 + 客户端 mod ControlMod），并且要**听服务器聊天框里其他玩家的指挥**。
现成开源项目没有一个同时满足这三条，所以主线自建——但工具设计、提示词工程、协议形态大量抄了主流做法。

## 目录

```
version/           身份与环境（一个版本文件夹 = 一份完整 AI 身份）
  active.json      ← 改一行就换身份
  1.20.1-forge/    identity.md（人格/手写常识/后端）· config.json · runtime.json
                   mods.md · 方块目录.md · workspace.json · undo-policy.json
                   robot.json（聊天行为）· memory/ · ready.json · 手册
mcrobot/           代码（分层）
  body/            身体：bridge（ControlMod 桥）· reflex（L0 反射）· loop（主循环）
  brain/           大脑：protocol（inbox/outbox 契约）· llm_brain（L1 API）·
                   bridge_brain（L2 人肉）· agent_loop（编排）· llm_policy（决策/自检）·
                   checkpoint（断点续跑）
  tools/           工具层：registry（MCP 风格注册表）· builtin（28 个工具）· mcp_server
  skills/          技能库：library（存取/检索/学习）· build（预置建造模板）
  knowledge.py     知识层：facts.jsonl + knowledge.md（按话题检索的事实表）
  vision.py        视觉：截图 → 多模态模型 → 文字（look 工具）
  textmatch.py     中英混合检索打分（技能库/知识库共用）
  ops/             运维：supervisor · launch_client · sftp · cleanup · reply
  console/         本地网页控制台（:8901）
tests/             单测（271 项）
controlmod/        Forge 客户端 mod 源码
instance/          游戏运行时目录（不入库）
```

## 架构

```
服务器聊天框 ─▶ body.loop ─┬─ L0 反射（规则，0 延迟，永不漏）
                           ├─ L1 API 大脑（OpenAI 兼容端点 / 伪 API）
                           └─ L2 人肉 / Claude Code 大脑（bridge）

大脑侧：perceive → decide → act → verify ─┬─ ok ──▶ 结束
                    ▲                     └─ critique（未达上限则重试）
```

- **工具层走 MCP**：任何 MCP 客户端（Claude Desktop、CodeBuddy…）可直接指挥机器人
- **闭环抄 Voyager**：执行后拿回执 → 独立 critic 自检 → 失败带批评重试（默认 4 轮）
- **技能库抄 Voyager**：做成过的事自动入库，下次同类目标直接调模板（`version/<版本>/skills.jsonl`）
- **知识层（自己的做法）**：摸出来的事实带出处记进 `facts.jsonl`，按当前话题检索后注入，查不到不塞
- **断点续跑抄 LangGraph**：任务状态每拍落盘，同一个 task_id 可从断掉的那一拍接着跑
- **工程纪律抄 mcagent/airi**：行动预算、失败止损、错误熔断
- **改文档即改 AI 的认知**：`mods.md` / `方块目录.md` 一改，下次注入的世界常识就跟着变

## 常用命令

```bash
python -m mcrobot.ops.supervisor start --with-console   # 守护身体 + 控制台
python -m mcrobot.ops.supervisor status                 # 看进程
python -m mcrobot.ops.launch_client                     # 起游戏客户端进服
python -m mcrobot.console.server --port 8901            # 网页控制台（浏览器打开）
python -m mcrobot.brain.bridge_brain watch --timeout 1800  # L2 人肉接单（有消息即退出）
python -m mcrobot.brain.llm_brain status                # 看 L1 端点与连通性
python -m mcrobot.identity versions | use <版本> | prompt   # 身份与提示词调试
python -m mcrobot.identity blocks desk                  # 查方块目录
python -m mcrobot.tools.mcp_server                      # 作为 MCP server 运行（stdio）
python -m mcrobot.skills.library install | list | search 砌墙   # 技能库
python -m mcrobot.knowledge list | search 家具 | consolidate    # 知识库（版本内查表）
python -m mcrobot.vision check | shot "这面墙齐不齐" | review        # 视觉（截图→多模态，review=五项评分表验收）
python -m mcrobot.brain.checkpoint list | show <task_id>        # 断点续跑存档
```

跑全部单测：

```bash
python -m unittest discover -s tests -t .
```

## 怎么接真 API

改 `version/1.20.1-forge/robot.json:llm_api`（或在控制台里改后端模板）：
`endpoint` + `model` + 环境变量 `MCROBOT_LLM_KEY`。**代码一行不动。**

## 兄弟项目

- `../mock_api`（原 `mc_robot/sim`）：DeepSink 伪 API 平台，OpenAI + Anthropic 双协议，
  独立 git 仓库。MC 只是它的一个客户端。

## 规矩

- 动手前先读 `version/<版本>/` 里的文档，它们才是权威；**别另建平行配置层手抄已有信息**
- 破坏性操作（删文件、改权限、杀服务）必须先侦查、出清单、人确认
- 新摸出的必做动作/命令/坑，补写回 `version/<版本>/机器人手册.md`
