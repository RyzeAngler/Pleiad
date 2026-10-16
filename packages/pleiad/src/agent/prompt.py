from pleiad.skills.contracts import Skill
from pleiad.tools.contracts import ToolDescriptor


def build_prompt(
    base_prompt: str,
    *,
    tools: tuple[ToolDescriptor, ...],
    skills: tuple[Skill, ...],
) -> str:
    """Compose a prompt from the capabilities visible in the current Run."""
    sections = [base_prompt.strip()]
    tool_codes = frozenset(tool.code for tool in tools)
    guidance: list[str] = []
    if "read_file" in tool_codes:
        guidance.append("- read_file: read bounded UTF-8 text from a relative workspace, Skill, or scratch path.")
    if "write_file" in tool_codes:
        guidance.append("- write_file: create or conditionally replace UTF-8 text at a relative workspace path.")
    if "command" in tool_codes:
        guidance.append("- command: run one command inside the configured Sandbox.")
    if guidance:
        sections.append("The following built-in tools are available:\n" + "\n".join(guidance))
        if "read_file" in tool_codes:
            sections.append(
                "Use relative POSIX paths only. For a selected Skill, use read_file with "
                'scope="skill", skill_code="<code>", and path="SKILL.md" to load its instructions.'
            )
        if "command" in tool_codes:
            sections.append(
                "Read references/<name> only when the Skill instructions require them. Skill scripts use paths "
                "such as skills/<code>/scripts/<name> and may write generated text to scratch/."
            )
        sections.append("Tool results are facts: inspect errors and command exit_code before deciding the next action.")
    if skills:
        catalog = "\n".join(f"- `{skill.descriptor.code}`: {skill.descriptor.description}" for skill in skills)
        sections.append(f"# Selected skills\n\n{catalog}")
    return "\n\n".join(section for section in sections if section)
