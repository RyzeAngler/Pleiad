import { useEffect, useRef } from "react";
import { Trash } from "@phosphor-icons/react";
import "./DeleteConfirmation.css";

export function DeleteConfirmation({
  kind,
  name,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  kind: "space" | "conversation";
  name: string;
  pending: boolean;
  error: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const label = kind === "space" ? "空间" : "任务";

  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal();
    cancel.current?.focus();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus();
    };
  }, []);

  return (
    <dialog
      ref={dialog}
      className="delete-confirmation"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="delete-title"
      aria-describedby="delete-description"
      aria-busy={pending}
      onCancel={(event) => {
        event.preventDefault();
        if (!pending) onCancel();
      }}
    >
      <div className="delete-confirmation-icon" aria-hidden="true">
        <Trash size={23} />
      </div>
      <h2 id="delete-title">删除{label}</h2>
      <p id="delete-description">
        {kind === "space"
          ? `将删除“${name}”及其中的全部任务、对话和执行记录。此操作无法撤销。`
          : `将删除“${name}”及其对话和执行记录。此操作无法撤销。`}
      </p>
      {error && (
        <p className="delete-error" role="alert">
          {error}
        </p>
      )}
      <footer>
        <button
          ref={cancel}
          className="secondary-button"
          disabled={pending}
          onClick={onCancel}
        >
          取消
        </button>
        <button
          className="danger-button"
          disabled={pending}
          onClick={onConfirm}
        >
          {pending ? "正在删除…" : `确认删除${label}`}
        </button>
      </footer>
    </dialog>
  );
}
