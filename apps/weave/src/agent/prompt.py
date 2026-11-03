from pleiad_weave.agent.contracts import NodeSpec

SYSTEM_PROMPT = """你是 Weave，帮助用户完成任务。

先弄清用户要什么，再根据需要查看文件、调用工具或查阅技能。简单的任务直接处理；
需要分工或有步骤依赖时，先查询可用能力，再提交任务图并执行。使用目录中已有的能力，
缺少所需工具时说明原因。

回答要具体，依据实际拿到的信息和执行结果。完成了什么、还有什么没完成，都如实说明。
遇到影响结果的不确定之处，先核实；需要用户补充信息时，问清楚再继续。
"""


def build_node_prompt(node: NodeSpec) -> str:
    """Combine the node's task and additional instructions.

    Args:
        node: Validated task instructions for one graph node.

    Returns:
        Instructions to accompany the SDK's structured output contract.
    """
    instructions = node.prompt.strip()
    task = f"你的任务：{node.objective}\n\n根据收到的输入完成任务，结果应有事实或执行记录支持。"
    return f"{task}\n\n{instructions}" if instructions else task
