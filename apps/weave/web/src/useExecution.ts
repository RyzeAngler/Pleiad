import { useCallback, useEffect, useState } from "react";
import { api, errorMessage, isAbort } from "./api";
import { isActive, mergeEvents, updateLiveMessages } from "./projection";
import type { Approval, ExecutionDetails, ExecutionEvent } from "./types";

export function useExecution(id: string | null, onResync: () => void) {
  const [details, setDetails] = useState<ExecutionDetails | null>(null);
  const [events, setEvents] = useState<ExecutionEvent[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [live, setLive] = useState<Record<string, string>>({});
  const [connection, setConnection] = useState("");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    setDetails(null);
    setEvents([]);
    setApprovals([]);
    setLive({});
    setError("");
    if (!id) {
      setConnection("");
      return;
    }
    const abort = new AbortController();
    const seen = new Set<string>();
    let cursor = 0;
    let terminal = false;
    let stream: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    function connect() {
      if (abort.signal.aborted || terminal) return;
      stream = new EventSource(
        `/v1/executions/${encodeURIComponent(id!)}/stream?after_sequence=${cursor}`,
      );
      stream.onopen = () => {
        if (!abort.signal.aborted) setConnection("实时连接");
      };
      stream.addEventListener("execution", (message: MessageEvent<string>) => {
        if (abort.signal.aborted) return;
        let event: ExecutionEvent;
        try {
          event = JSON.parse(message.data) as ExecutionEvent;
          if (
            typeof event.event_id !== "string" ||
            event.execution_id !== id ||
            typeof event.event_type !== "string" ||
            typeof event.sequence !== "number" ||
            !event.data
          )
            throw new Error("事件格式无效");
        } catch {
          setError("服务返回了无法读取的事件，请重新连接。");
          return;
        }
        if (
          seen.has(event.event_id) ||
          (event.sequence > 0 && event.sequence <= cursor)
        )
          return;
        seen.add(event.event_id);
        cursor = Math.max(cursor, event.sequence);
        if (event.sequence > 0)
          setEvents((previous) => mergeEvents(previous, [event]));
        setLive((previous) => updateLiveMessages(previous, event));
        if (
          [
            "execution.completed",
            "execution.failed",
            "execution.cancelled",
          ].includes(event.event_type)
        ) {
          terminal = true;
          setConnection("历史记录");
          stream?.close();
          onResync();
        }
      });
      const resync = () => {
        if (abort.signal.aborted) return;
        stream?.close();
        setConnection("正在重新连接");
        onResync();
        setLive({});
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, 1500);
      };
      stream.addEventListener("resync", resync);
      stream.onerror = () => {
        if (terminal) {
          stream?.close();
          return;
        }
        resync();
      };
    }
    async function poll() {
      try {
        const [snapshot, replay, pending] = await Promise.all([
          api.details(id!, abort.signal),
          api.events(id!, abort.signal),
          api.approvals(id!, abort.signal),
        ]);
        if (abort.signal.aborted) return;
        setDetails(snapshot);
        setEvents((previous) => mergeEvents(previous, replay));
        setApprovals(pending);
        setError("");
        if (!isActive(snapshot.execution)) {
          if (!terminal) onResync();
          terminal = true;
          setLive({});
          stream?.close();
          setConnection("历史记录");
        }
      } catch (failure) {
        if (!isAbort(failure) && !abort.signal.aborted)
          setError(errorMessage(failure));
      } finally {
        if (!abort.signal.aborted)
          timer = setTimeout(() => void poll(), terminal ? 5000 : 1500);
      }
    }
    connect();
    void poll();
    return () => {
      abort.abort();
      stream?.close();
      clearTimeout(timer);
      clearTimeout(reconnectTimer);
    };
  }, [id, revision, onResync]);
  return { details, events, approvals, live, connection, error, refresh };
}
