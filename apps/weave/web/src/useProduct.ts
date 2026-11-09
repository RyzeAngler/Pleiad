import { useCallback, useEffect, useRef, useState } from "react";
import { api, configureCsrf, errorMessage, isAbort } from "./api";
import type { Message, Snapshot, ProductEvent, Page } from "./types";

export function mergeMessages(
  current: Message[],
  incoming: Message[],
  readSequence: number,
  observed: Map<string, number>,
): Message[] {
  const entries = new Map(
    current.map((message) => [message.message_id, message]),
  );
  for (const message of incoming) {
    if (readSequence < (observed.get(message.message_id) ?? 0)) continue;
    observed.set(message.message_id, readSequence);
    entries.set(message.message_id, message);
  }
  return [...entries.values()].sort(
    (a, b) => a.chat_sequence - b.chat_sequence,
  );
}

export function useProduct() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [fresh, setFresh] = useState(false);
  const [error, setError] = useState("");
  const [generation, setGeneration] = useState(0);
  const [readVersion, setReadVersion] = useState(0);
  const generationRef = useRef(0);
  const snapshotRef = useRef<Snapshot | null>(null);
  const messageReads = useRef(new Map<string, number>());
  const historyDirty = useRef(0);
  const requests = useRef(new Set<AbortController>());
  const stream = useRef<EventSource | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  const alive = useRef(true);
  const resyncRef = useRef<() => void>(() => {});
  const commit = useCallback(
    (result: Snapshot, current: number, historyPages: Page<Message>[] = []) => {
      if (!alive.current || generationRef.current !== current) return false;
      if (
        snapshotRef.current &&
        result.snapshot_sequence < snapshotRef.current.snapshot_sequence
      )
        return false;
      const previous = snapshotRef.current;
      let messages = previous?.messages.items ?? [];
      for (const page of historyPages)
        messages = mergeMessages(
          messages,
          page.items,
          page.read_sequence,
          messageReads.current,
        );
      messages = mergeMessages(
        messages,
        result.messages.items,
        result.snapshot_sequence,
        messageReads.current,
      );
      const merged = previous
        ? {
            ...result,
            messages: {
              ...result.messages,
              items: messages,
              next_cursor:
                previous.messages.items[0]?.chat_sequence <
                result.messages.items[0]?.chat_sequence
                  ? previous.messages.next_cursor
                  : result.messages.next_cursor,
            },
          }
        : { ...result, messages: { ...result.messages, items: messages } };
      configureCsrf(result.capabilities.csrf_token);
      snapshotRef.current = merged;
      setSnapshot(merged);
      setReadVersion((value) => value + 1);
      return true;
    },
    [],
  );
  const refresh = useCallback(() => {
    const current = generationRef.current;
    if (refreshTimer.current !== undefined) return;
    refreshTimer.current = setTimeout(() => {
      refreshTimer.current = undefined;
      const controller = new AbortController();
      requests.current.add(controller);
      void api
        .snapshot(controller.signal)
        .then(async (result) => {
          if (current !== generationRef.current) return;
          const dirty = historyDirty.current;
          const first = result.messages.items[0]?.chat_sequence;
          const historical =
            dirty > 0 && first !== undefined
              ? (snapshotRef.current?.messages.items ?? []).filter(
                  (message) =>
                    message.chat_sequence < first &&
                    (message.role === "user" ||
                      message.report_ids.length > 0 ||
                      message.artifacts.length > 0 ||
                      message.context_refs.length > 0),
                )
              : [];
          const loaded = new Set(
            historical.map((message) => message.message_id),
          );
          const pages: Page<Message>[] = [];
          let remaining = historical;
          while (remaining.length > 0) {
            const before = remaining.at(-1)!.chat_sequence + 1;
            const page = await api.messages(before, controller.signal, 100);
            if (current !== generationRef.current) return;
            if (page.read_sequence < dirty || page.items.length === 0)
              throw new Error("历史消息的当前归属尚未核验，请重新同步。");
            const oldest = page.items[0].chat_sequence;
            if (oldest >= before)
              throw new Error("历史消息分页没有返回有效的读取边界。");
            pages.push({
              ...page,
              items: page.items.filter((message) =>
                loaded.has(message.message_id),
              ),
            });
            remaining = remaining.filter(
              (message) => message.chat_sequence < oldest,
            );
          }
          if (
            commit(result, current, pages) &&
            result.snapshot_sequence >= dirty &&
            historyDirty.current === dirty
          )
            historyDirty.current = 0;
        })
        .catch((failure) => {
          if (!isAbort(failure) && generationRef.current === current) {
            setFresh(false);
            setError(errorMessage(failure));
            reconnectTimer.current = setTimeout(
              () => resyncRef.current(),
              1500,
            );
          }
        })
        .finally(() => requests.current.delete(controller));
    }, 100);
  }, [commit]);
  const resync = useCallback(() => {
    const current = ++generationRef.current;
    setGeneration(current);
    setFresh(false);
    setError("");
    snapshotRef.current = null;
    messageReads.current.clear();
    historyDirty.current = 0;
    stream.current?.close();
    requests.current.forEach((controller) => controller.abort());
    requests.current.clear();
    clearTimeout(refreshTimer.current);
    refreshTimer.current = undefined;
    clearTimeout(reconnectTimer.current);
    const controller = new AbortController();
    requests.current.add(controller);
    void api
      .snapshot(controller.signal)
      .then((result) => {
        if (!commit(result, current)) return;
        const source = new EventSource(
          `/v1/events?after_sequence=${result.snapshot_sequence}`,
        );
        stream.current = source;
        source.onopen = () => {
          if (generationRef.current === current) {
            setFresh(true);
            setError("");
          }
        };
        let cursor = result.snapshot_sequence;
        source.addEventListener("product", (event) => {
          if (generationRef.current !== current) return;
          try {
            if (
              !(event instanceof MessageEvent) ||
              typeof event.data !== "string"
            )
              throw new Error("产品事件缺少有效内容。");
            const fact: ProductEvent<Message> = JSON.parse(event.data);
            if (
              !Number.isInteger(fact.sequence) ||
              typeof fact.type !== "string"
            )
              throw new Error("产品事件缺少有序身份。");
            if (fact.sequence <= cursor) return;
            if (fact.sequence !== cursor + 1) {
              resyncRef.current();
              return;
            }
            cursor = fact.sequence;
            const previous = snapshotRef.current;
            historyDirty.current = fact.sequence;
            if (fact.type === "chat.message.created" && previous) {
              const message = fact.data;
              if (
                typeof message.message_id !== "string" ||
                !Number.isInteger(message.chat_sequence) ||
                typeof message.body !== "string" ||
                !Array.isArray(message.reports) ||
                !Array.isArray(message.artifacts) ||
                !Array.isArray(message.input_dispositions) ||
                !Array.isArray(message.handbacks) ||
                !Array.isArray(message.handback_resolutions) ||
                !Array.isArray(message.attachment_ids) ||
                !Array.isArray(message.target_execution_ids) ||
                !Array.isArray(message.context_refs)
              )
                throw new Error("正式消息与产品读模型不一致。");
              const exists = previous.messages.items.some(
                (value) => value.message_id === message.message_id,
              );
              const merged = {
                ...previous,
                messages: {
                  ...previous.messages,
                  items: mergeMessages(
                    previous.messages.items,
                    [message],
                    fact.sequence,
                    messageReads.current,
                  ),
                  total: previous.messages.total + (exists ? 0 : 1),
                  read_sequence: fact.sequence,
                },
              };
              snapshotRef.current = merged;
              setSnapshot(merged);
            }
            refresh();
          } catch (failure) {
            source.close();
            setFresh(false);
            setError(errorMessage(failure));
            reconnectTimer.current = setTimeout(
              () => resyncRef.current(),
              1500,
            );
          }
        });
        source.addEventListener("resync", () => {
          if (generationRef.current === current) resyncRef.current();
        });
        source.onerror = () => {
          if (generationRef.current !== current) return;
          source.close();
          setFresh(false);
          setError("连接已中断，正在重新核验工作区状态。");
          reconnectTimer.current = setTimeout(() => resyncRef.current(), 1500);
        };
      })
      .catch((failure) => {
        if (!isAbort(failure) && generationRef.current === current) {
          setError(errorMessage(failure));
          reconnectTimer.current = setTimeout(() => resyncRef.current(), 1500);
        }
      })
      .finally(() => requests.current.delete(controller));
  }, [commit, refresh]);
  resyncRef.current = resync;
  useEffect(() => {
    alive.current = true;
    resync();
    return () => {
      alive.current = false;
      ++generationRef.current;
      stream.current?.close();
      requests.current.forEach((controller) => controller.abort());
      clearTimeout(refreshTimer.current);
      clearTimeout(reconnectTimer.current);
    };
  }, [resync]);
  const loadOlder = useCallback(async () => {
    const before = snapshotRef.current?.messages.next_cursor;
    if (before === null || before === undefined) return;
    const current = generationRef.current,
      controller = new AbortController();
    requests.current.add(controller);
    try {
      const page = await api.messages(before, controller.signal);
      if (current !== generationRef.current || !snapshotRef.current) return;
      const result = {
        ...snapshotRef.current,
        messages: {
          ...page,
          items: mergeMessages(
            snapshotRef.current.messages.items,
            page.items,
            page.read_sequence,
            messageReads.current,
          ),
        },
      };
      snapshotRef.current = result;
      setSnapshot(result);
    } catch (failure) {
      if (!isAbort(failure) && current === generationRef.current)
        setError(errorMessage(failure));
    } finally {
      requests.current.delete(controller);
    }
  }, []);
  return {
    snapshot,
    fresh,
    error,
    generation,
    readVersion,
    refresh,
    resync,
    loadOlder,
  };
}

export function useRecord<T>(
  key: string,
  loader: (signal: AbortSignal) => Promise<T>,
  generation: number,
  version: number,
) {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const loaderRef = useRef(loader);
  const owner = useRef({ key, generation });
  loaderRef.current = loader;
  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setLoading(true);
    setError("");
    if (owner.current.key !== key || owner.current.generation !== generation)
      setValue(null);
    owner.current = { key, generation };
    void loaderRef
      .current(controller.signal)
      .then((result) => {
        if (current) setValue(result);
      })
      .catch((failure) => {
        if (current && !isAbort(failure)) {
          setError(errorMessage(failure));
          setValue(null);
        }
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [key, generation, version]);
  return { value, error, loading };
}
