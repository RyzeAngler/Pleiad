from rich.text import Text
from textual.widgets import Static


class HeaderBar(Static):
    """Render the centered product identity and nothing from the current run."""

    def __init__(self, id: str | None = None) -> None:
        super().__init__(render_brand(), id=id)


def render_brand() -> Text:
    """Render the one-line centered Weave header."""
    text = Text()
    text.append("\n")
    text.append("WEAVE", style="bold #E0E4E8")
    text.append(" · ", style="#7FB7FF")
    text.append("PLEIAD RUNTIME", style="#68727C")
    text.append("\n")
    text.justify = "center"
    return text
