"""身体层：与游戏客户端打交道的一切。

bridge   ControlMod 本地 HTTP 桥客户端（发指令、读状态、读聊天）
reflex   L0 反射层：意图识别、危险判定、规则秒回（零 token，永不漏）
observe  环境感知：坐标/维度/在线/背包快照、附近聆听
loop     主循环：把上面三者串起来，配 inbox/outbox 协议
"""
