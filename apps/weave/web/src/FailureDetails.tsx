import type { Failure } from "./types";

const sandboxGuidance: Record<string, { message: string; action: string }> = {
  "sandbox.runtime.unavailable": {
    message: "沙箱容器引擎不可用。",
    action:
      "请确认 Weave 所在机器上的容器引擎已安装并启动，用 docker info 或 podman info 检查连接，恢复后重新提交请求。",
  },
  "sandbox.cleanup.failed": {
    message: "沙箱容器清理失败。",
    action: "请检查容器引擎连接，并在重试前确认是否有残留容器。",
  },
  "sandbox.execution.failed": {
    message: "沙箱执行失败。",
    action: "请查看诊断信息，检查镜像和执行环境后重试。",
  },
  "sandbox.config.invalid": {
    message: "沙箱配置无效。",
    action: "请检查运行时类型、镜像 digest 和资源限制配置。",
  },
};

export function FailureDetails({ error }: { error: Failure }) {
  const guidance = sandboxGuidance[error.code];
  return (
    <div className="failure-details">
      <p>{guidance?.message ?? error.message}</p>
      {guidance && <p className="failure-action">{guidance.action}</p>}
      <details>
        <summary>诊断信息</summary>
        <code>{error.code}</code>
        {guidance && <pre>{error.message}</pre>}
      </details>
    </div>
  );
}
