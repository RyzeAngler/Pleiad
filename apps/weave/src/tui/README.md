# Terminal interface

`pleiad tui --server http://127.0.0.1:8000` connects to the running workspace owner.
Start that owner separately with `pleiad serve`. The terminal interface displays
committed permanent-chat messages and current product details through `/v1`.

The left pane contains one composer. Work can be selected as an explicit message
target. Selecting an Attention exposes its current question or bound approval.
Answer uses the same composer, while Approve and Reject submit explicit decisions
with the inspected revision and action digest. Pause, Resume, and Cancel use the
selected Execution and server-provided action availability.

Ctrl+R refreshes current facts and the product journal subscription. Esc returns
an answer draft to main chat. Ctrl+Q disconnects the interface. Closing it leaves
work with the service. A disconnected interface retains its draft and requires a
fresh read before another mutation.
