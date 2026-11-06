---
name: verification-before-completion
description: 在声明任务完成前执行对应验收检查，明确区分已验证结果、失败与尚未验证的部分
license: MIT
metadata:
  upstream: obra/superpowers
  revision: 22f0b463e7a5e579904dd6211a520321035526f7
---

# Verification Before Completion

Adapted from Superpowers for Weave. See SOURCE.md and LICENSE.txt; the original
instructions are in references/upstream.md. No upstream plugin or hook is required.

Evidence must precede a completion claim.

1. Translate each requested outcome into an observable acceptance condition.
   Select a check that would fail if that condition were not met.
2. Use available tools to run the complete relevant check after the final change.
   Inspect its exit status, returned error, output and relevant persisted state.
   A submitted job, successful HTTP request or tool-call acknowledgement alone
   does not prove the requested job finished successfully.
3. For an external write, read back the actual target and verify its identity and
   contents when the service exposes that capability. For browser interaction,
   observe the resulting page. For generated resources, inspect their contents
   and provide the real returned artifact location.
4. Separate successful checks, failed checks, skipped checks, and checks that
   could not run. Distinguish deterministic, transport, real-service and visual
   validation; one category does not establish another.
5. Report the result and supporting evidence concisely. If evidence is missing,
   state that the outcome remains unverified. Do not convert a timeout, empty
   response, missing credentials or unavailable tool into success.

Never rely on a previous run when the relevant inputs or implementation changed.
Avoid additional expensive or mutating checks once sufficient evidence exists,
unless a new failure or change creates a concrete reason to repeat them.
