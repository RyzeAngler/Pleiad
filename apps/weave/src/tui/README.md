# TUI

Weave TUI consumes the Application Service and its ordered Execution Events.
Runtime, Graph, and Trace are separate projections; Trace is the Agent Loop
event timeline, not a logging or OpenTelemetry span browser. Program Log is a
bounded, redacted UI feed and is never mixed into the conversation or Trace.
The shared service, event contracts, and TUI projection boundaries are
documented in the [Weave architecture](../../../../docs/weave-architecture.md#4-组件视图c3).
The current layout and interactions are implemented in `app.py`,
`screens/`, `widgets/`, and `styles/` in this directory.

Runtime explains common SDK log messages in Chinese while preserving unknown
diagnostics, credential redaction, and correlation fields. The file sink retains
the original diagnostic messages. Following new records changes only the vertical
position; horizontal reading positions remain stable.

Trace uses a table with labelled columns measured in terminal cells. Event names
and outcomes have Chinese labels; tool names and error codes remain available in
the details column. Failed tool results, timeouts, and exhausted limits retain
their distinct outcomes. Appending events preserves the selected row and paused
scroll positions. Focus a log or the table before using arrow keys; Shift+wheel
scrolls horizontally when the terminal forwards mouse events.
