import { useRef, useState, type KeyboardEvent } from "react";
import {
  ArrowClockwise,
  ArrowLeft,
  CaretDown,
  Check,
  Copy,
  Cpu,
  Cube,
  FolderOpen,
  MagnifyingGlass,
  Wrench,
} from "@phosphor-icons/react";
import { errorMessage } from "./api";
import type { Resources } from "./types";
import "./Settings.css";

type Category = "models" | "tools" | "skills";
const categories: { id: Category; label: string }[] = [
  { id: "models", label: "模型" },
  { id: "tools", label: "Tools" },
  { id: "skills", label: "Skills" },
];

export function ResourceLibrary({
  resources,
  onClose,
  onRetry,
  loading = false,
}: {
  resources: Resources | null;
  onClose: () => void;
  onRetry?: () => void;
  loading?: boolean;
}) {
  const [category, setCategory] = useState<Category>("tools");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [configurationOpen, setConfigurationOpen] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const [error, setError] = useState("");
  const tabsRef = useRef<HTMLDivElement>(null);
  const queryText = query.trim().toLocaleLowerCase();
  const noun =
    category === "models" ? "模型" : category === "tools" ? "工具" : "技能";
  const items = resources
    ? category === "models"
      ? resources.models.map((model) => ({
          name: model.model,
          code: model.role,
          description: "",
        }))
      : resources[category]
    : [];
  const filtered = items.filter((item) =>
    `${item.name} ${item.code} ${item.description}`
      .toLocaleLowerCase()
      .includes(queryText),
  );
  const Icon =
    category === "models" ? Cpu : category === "tools" ? Wrench : Cube;

  function select(next: Category) {
    setCategory(next);
    setQuery("");
    setExpanded(null);
  }
  function moveTab(event: KeyboardEvent<HTMLButtonElement>) {
    const current = categories.findIndex((item) => item.id === category);
    let next: number;
    if (event.key === "ArrowRight") next = (current + 1) % categories.length;
    else if (event.key === "ArrowLeft")
      next = (current + categories.length - 1) % categories.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = categories.length - 1;
    else return;
    event.preventDefault();
    select(categories[next].id);
    tabsRef.current
      ?.querySelectorAll<HTMLButtonElement>("button")
      [next]?.focus();
  }
  async function copyPath(value: string) {
    setCopied(null);
    setError("");
    try {
      await navigator.clipboard.writeText(value);
      setCopied(value);
    } catch (failure) {
      setError(`复制失败，请手动选择路径。${errorMessage(failure)}`);
    }
  }

  return (
    <div className="resource-library-page">
      <nav className="resource-library-breadcrumb" aria-label="资源库导航">
        <button onClick={onClose}>
          <ArrowLeft size={18} />
          返回
        </button>
        <span aria-hidden="true">/</span>
        <span>资源库</span>
      </nav>
      <h1>资源库</h1>
      <div
        className="resource-library-tabs"
        role="tablist"
        aria-label="资源分类"
        ref={tabsRef}
      >
        {categories.map((item) => (
          <button
            key={item.id}
            id={`library-tab-${item.id}`}
            role="tab"
            aria-selected={category === item.id}
            aria-controls="resource-library-content"
            tabIndex={category === item.id ? 0 : -1}
            onClick={() => select(item.id)}
            onKeyDown={moveTab}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div className="resource-library-toolbar">
        <label className="settings-search">
          <MagnifyingGlass size={21} />
          <input
            aria-label={`搜索${noun}`}
            placeholder={`搜索${noun}`}
            disabled={!resources}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        {resources && (
          <button
            className="settings-outline"
            aria-expanded={configurationOpen}
            aria-controls="resource-library-configuration"
            onClick={() => setConfigurationOpen((open) => !open)}
          >
            <FolderOpen size={20} />
            配置位置
            <CaretDown size={14} />
          </button>
        )}
      </div>
      {configurationOpen && resources && (
        <section
          className="resource-library-configuration"
          id="resource-library-configuration"
          aria-label="配置位置"
        >
          <h2>配置位置</h2>
          {[
            { label: "工作区", path: resources.workspace_path },
            { label: "能力清单", path: resources.capability_file },
          ].map((item) => (
            <div className="library-config-path" key={item.label}>
              <span>{item.label}</span>
              <code>{item.path}</code>
              <button
                className="icon-button"
                aria-label={`复制${item.label}路径`}
                onClick={() => void copyPath(item.path)}
              >
                {copied === item.path ? <Check /> : <Copy />}
              </button>
            </div>
          ))}
          <p>
            模型由服务启动配置加载，修改后需重启服务。工具和技能的能力清单在后续请求中重新读取。
          </p>
          <span className="library-copy-status" role="status">
            {copied ? "路径已复制" : ""}
          </span>
          {error && (
            <p className="library-copy-error" role="alert">
              {error}
            </p>
          )}
        </section>
      )}
      <section
        id="resource-library-content"
        role="tabpanel"
        aria-labelledby={`library-tab-${category}`}
        tabIndex={0}
        className="resource-library-content"
      >
        {!resources ? (
          <div className="resource-library-empty" role="status">
            <p>{loading ? "正在加载资源…" : "资源目录暂不可用"}</p>
            {!loading && onRetry && (
              <button className="settings-outline" onClick={onRetry}>
                <ArrowClockwise />
                重新加载
              </button>
            )}
          </div>
        ) : (
          <>
            <div className="resource-library-columns">
              <span>{noun}</span>
              <span>{category === "models" ? "角色" : "标识"}</span>
              <span />
            </div>
            <div className="resource-library-list">
              {filtered.map((item) => (
                <article className="resource-library-item" key={item.code}>
                  <div className="resource-library-row">
                    <Icon size={27} />
                    <strong>{item.name}</strong>
                    <code>{item.code}</code>
                    {category !== "models" && (
                      <button
                        className="text-button"
                        aria-label={`${expanded === item.code ? "收起" : "查看"}${item.name}`}
                        aria-expanded={expanded === item.code}
                        onClick={() =>
                          setExpanded((current) =>
                            current === item.code ? null : item.code,
                          )
                        }
                      >
                        {expanded === item.code ? "收起" : "查看"}
                      </button>
                    )}
                  </div>
                  {category !== "models" && expanded === item.code && (
                    <div className="resource-library-detail">
                      <p>{item.description || "未提供描述"}</p>
                    </div>
                  )}
                </article>
              ))}
            </div>
            {!filtered.length && (
              <p className="resource-library-empty" role="status">
                {queryText ? `没有匹配的${noun}` : `暂无${noun}`}
              </p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
