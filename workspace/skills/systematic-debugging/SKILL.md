---
name: systematic-debugging
description: 对工具失败或任务异常进行根因分析，通过证据和单一假设定位问题后再修复
license: MIT
metadata:
  upstream: obra/superpowers
  revision: 22f0b463e7a5e579904dd6211a520321035526f7
---

# Systematic Debugging

Adapted from Superpowers for Weave. See SOURCE.md and LICENSE.txt; the original
instructions are in references/upstream.md. Use the actual discovered Weave
tools rather than upstream plugin hooks, named tools or subagent commands.

Do not propose a fix before collecting evidence of the root cause.

1. **Investigate:** Read the complete error and its cause, identify the failing
   input and boundary, reproduce with a minimal authorized action, and compare
   with a working case. Trace the data from the caller to the failure. Separate
   configuration, authentication, connectivity, provider, tool and application
   failures. Record missing evidence when the necessary capability is absent.
2. **Compare:** Inspect the actual contract, relevant callers, working examples
   and recent changes. List specific differences rather than assuming that a
   familiar error has a familiar cause.
3. **Test one hypothesis:** State the cause you suspect, the evidence for it,
   and a check that would disprove it. Use the smallest check possible and
   change one variable at a time. Preserve the original error and useful input.
4. **Fix and verify:** Modify the owning boundary only within the user's scope.
   Re-run the original failing case and the relevant regression checks. If a
   check fails, return to investigation rather than stacking speculative fixes.

Repeated failure is evidence to reconsider the hypothesis or design. Never
silently replace a failed tool call with a model-generated result, conceal a
failure, or report success solely because the next step produced plausible text.
