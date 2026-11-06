import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  Cube,
  FlowArrow,
  MagnifyingGlass,
  Trash,
  Wrench,
  X,
} from "@phosphor-icons/react";
import { api, errorMessage } from "./api";
import type { GraphReference, Resources, Space } from "./types";
import "./Settings.css";

export function SpaceSettings({
  space,
  resources,
  onSaved,
  onClose,
  onViewGraph,
  onDirtyChange,
  onSavingChange,
  onDelete,
  deletionDisabled = false,
}: {
  space: Space;
  resources: Resources | null;
  onSaved: (space: Space) => void;
  onClose: () => void;
  onViewGraph: (reference: GraphReference) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onSavingChange?: (saving: boolean) => void;
  onDelete?: () => void;
  deletionDisabled?: boolean;
}) {
  const [draft, setDraft] = useState(space);
  const [baseline, setBaseline] = useState(space);
  const [query, setQuery] = useState("");
  const [choosing, setChoosing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const pickerRef = useRef<HTMLDialogElement>(null);
  const pickerTrigger = useRef<HTMLButtonElement>(null);
  const changed =
    draft.name.trim() !== baseline.name ||
    draft.instructions !== baseline.instructions ||
    JSON.stringify(draft.preferred_tool_codes) !==
      JSON.stringify(baseline.preferred_tool_codes) ||
    JSON.stringify(draft.preferred_skill_codes) !==
      JSON.stringify(baseline.preferred_skill_codes) ||
    draft.saved_graph?.graph_id !== baseline.saved_graph?.graph_id ||
    draft.saved_graph?.revision !== baseline.saved_graph?.revision;

  useEffect(() => {
    nameRef.current?.focus({ preventScroll: true });
  }, []);
  useEffect(() => {
    onDirtyChange?.(changed);
  }, [changed, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);
  useEffect(() => {
    onSavingChange?.(saving);
  }, [saving, onSavingChange]);
  useEffect(() => () => onSavingChange?.(false), [onSavingChange]);
  useEffect(() => {
    if (!changed && !saving) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [changed, saving]);
  useEffect(() => {
    if (!error) return;
    errorRef.current?.scrollIntoView({ block: "nearest" });
    errorRef.current?.focus({ preventScroll: true });
  }, [error]);
  useEffect(() => {
    if (!choosing) return;
    const dialog = pickerRef.current;
    dialog?.showModal();
    return () => {
      dialog?.close();
      pickerTrigger.current?.focus();
    };
  }, [choosing]);

  function update(value: Partial<Space>) {
    setSaved(false);
    setDraft((current) => ({ ...current, ...value }));
  }
  function toggle(
    field: "preferred_tool_codes" | "preferred_skill_codes",
    code: string,
  ) {
    update({
      [field]: draft[field].includes(code)
        ? draft[field].filter((item) => item !== code)
        : [...draft[field], code],
    });
  }
  async function save() {
    if (!draft.name.trim() || saving || !changed) return;
    setSaving(true);
    setError("");
    try {
      const result = await api.updateSpace({
        ...draft,
        name: draft.name.trim(),
      });
      setDraft(result);
      setBaseline(result);
      setSaved(true);
      onSaved(result);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-settings-page">
      <button className="settings-back" disabled={saving} onClick={onClose}>
        <ArrowLeft size={21} /> 返回
      </button>
      <header className="space-settings-title">
        <h1>空间设置</h1>
        <span>{baseline.name}</span>
      </header>
      <form
        className="space-settings-form"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <label className="space-settings-field">
          <span>空间名称</span>
          <input
            ref={nameRef}
            value={draft.name}
            maxLength={120}
            disabled={saving}
            aria-invalid={!draft.name.trim()}
            onChange={(event) => update({ name: event.target.value })}
          />
        </label>
        <label className="space-settings-field">
          <span>
            默认指令 <small>可选</small>
          </span>
          <textarea
            aria-label="默认指令"
            rows={5}
            maxLength={20000}
            disabled={saving}
            value={draft.instructions}
            onChange={(event) => update({ instructions: event.target.value })}
            placeholder="例如：默认从杭州出发，优先公共交通。"
          />
        </label>
        <section
          className="space-settings-group"
          aria-labelledby="preferred-resources-title"
        >
          <div className="space-settings-group-heading">
            <h2 id="preferred-resources-title">优先使用的资源</h2>
            <button
              ref={pickerTrigger}
              type="button"
              className="settings-outline"
              disabled={saving || !resources}
              onClick={() => {
                setQuery("");
                setChoosing(true);
              }}
            >
              选择资源
            </button>
          </div>
          <div className="space-resource-chips">
            {(["tools", "skills"] as const).flatMap((kind) => {
              const field =
                kind === "tools"
                  ? "preferred_tool_codes"
                  : "preferred_skill_codes";
              return draft[field].map((code) => {
                const name =
                  resources?.[kind].find((item) => item.code === code)?.name ??
                  code;
                return (
                  <span className="space-resource-chip" key={`${kind}:${code}`}>
                    {kind === "tools" ? (
                      <Wrench size={20} />
                    ) : (
                      <Cube size={20} />
                    )}
                    <span>{name}</span>
                    <button
                      type="button"
                      disabled={saving}
                      aria-label={`移除${name}`}
                      onClick={() => toggle(field, code)}
                    >
                      <X size={16} />
                    </button>
                  </span>
                );
              });
            })}
            {!draft.preferred_tool_codes.length &&
              !draft.preferred_skill_codes.length && (
                <p className="settings-empty">未指定</p>
              )}
          </div>
          {!resources && (
            <p className="settings-empty" role="status">
              资源目录暂不可用
            </p>
          )}
        </section>
        <section
          className="space-settings-group"
          aria-labelledby="saved-graph-title"
        >
          <h2 id="saved-graph-title">已保存的编排</h2>
          {draft.saved_graph ? (
            <div className="space-saved-graph">
              <FlowArrow size={25} />
              <span className="space-saved-graph-name">
                {draft.saved_graph.graph_id}
              </span>
              <span className="space-saved-graph-version">
                v{draft.saved_graph.revision}
              </span>
              <div className="space-saved-graph-actions">
                <button
                  type="button"
                  className="text-button"
                  disabled={saving}
                  onClick={() => {
                    if (draft.saved_graph) onViewGraph(draft.saved_graph);
                  }}
                >
                  查看
                </button>
                <button
                  type="button"
                  className="icon-button"
                  aria-label="取消默认编排"
                  title="取消默认编排"
                  disabled={saving}
                  onClick={() => update({ saved_graph: null })}
                >
                  <X size={18} />
                </button>
              </div>
            </div>
          ) : (
            <p className="settings-empty">尚未保存编排</p>
          )}
        </section>
        {error && (
          <div
            className="error-banner"
            role="alert"
            ref={errorRef}
            tabIndex={-1}
          >
            {error}
          </div>
        )}
        <footer className="space-settings-actions">
          <span role="status">
            {!draft.name.trim()
              ? "请输入空间名称"
              : saved
                ? "已保存，对后续提交生效"
                : changed
                  ? "有未保存的修改"
                  : ""}
          </span>
          <button
            className="primary-button"
            disabled={saving || !draft.name.trim() || !changed}
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </footer>
      </form>
      {onDelete && (
        <div className="settings-delete">
          <button
            className="text-button delete-action"
            disabled={saving || deletionDisabled}
            onClick={onDelete}
          >
            <Trash size={18} /> 删除空间
          </button>
        </div>
      )}
      {choosing && (
        <dialog
          ref={pickerRef}
          className="resource-picker"
          aria-labelledby="resource-picker-title"
          onCancel={() => setChoosing(false)}
          onClick={(event) => {
            if (event.target === event.currentTarget) setChoosing(false);
          }}
        >
          <div className="resource-picker-content">
            <header>
              <h2 id="resource-picker-title">选择资源</h2>
              <button
                className="icon-button"
                aria-label="关闭资源选择"
                onClick={() => setChoosing(false)}
              >
                <X />
              </button>
            </header>
            <label className="settings-search">
              <MagnifyingGlass size={21} />
              <input
                autoFocus
                aria-label="搜索资源"
                placeholder="搜索名称、标识或描述"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <div className="resource-picker-list">
              {(["tools", "skills"] as const).map((kind) => {
                const field =
                  kind === "tools"
                    ? "preferred_tool_codes"
                    : "preferred_skill_codes";
                const items = resources?.[kind].filter((item) =>
                  `${item.code} ${item.name} ${item.description}`
                    .toLocaleLowerCase()
                    .includes(query.trim().toLocaleLowerCase()),
                );
                return (
                  <fieldset key={kind}>
                    <legend>{kind === "tools" ? "Tools" : "Skills"}</legend>
                    {items?.map((item) => (
                      <label className="resource-picker-choice" key={item.code}>
                        <input
                          type="checkbox"
                          checked={draft[field].includes(item.code)}
                          onChange={() => toggle(field, item.code)}
                        />
                        <span>
                          <strong>{item.name}</strong>
                          <code>{item.code}</code>
                          <small>{item.description}</small>
                        </span>
                      </label>
                    ))}
                    {!items?.length && (
                      <p className="settings-empty">
                        {query.trim() ? "没有匹配的资源" : "暂无资源"}
                      </p>
                    )}
                  </fieldset>
                );
              })}
            </div>
            <footer>
              <button
                className="primary-button"
                onClick={() => setChoosing(false)}
              >
                完成
              </button>
            </footer>
          </div>
        </dialog>
      )}
    </div>
  );
}
