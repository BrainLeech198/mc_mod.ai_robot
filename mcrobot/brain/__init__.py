"""大脑层。

protocol       inbox/outbox 协议（指针、emit）——与身体之间唯一的契约
bridge_brain   L2：人肉 / Claude Code 在线大脑（CLI：watch/read/say/cmd/status）
llm_brain      L1：OpenAI 兼容端点大脑（可切 DeepSink 伪 API 或真 API）
agent_loop     编排：显式状态机 perceive → decide → act → verify（对齐 LangGraph 模式）
"""
