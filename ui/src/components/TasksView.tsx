import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentCard, StateTransition, Task } from "../lib/types";
import { extractPartsText, fetchTaskById, fetchTaskList } from "../lib/protocol";
import { StateBadge } from "./StateBadge";
import { PartsDisplay } from "./PartsDisplay";

const POLL_OPTIONS = [
  { label: "0.5s", value: 500 },
  { label: "1s", value: 1000 },
  { label: "2s", value: 2000 },
  { label: "5s", value: 5000 },
  { label: "10s", value: 10000 },
  { label: "30s", value: 30000 },
];

interface Props {
  card: AgentCard | null;
  active: boolean;
  pollInterval: number;
  onPollIntervalChange: (ms: number) => void;
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

export function TasksView({ card, active, pollInterval, onPollIntervalChange }: Props) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Task | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [pollOk, setPollOk] = useState(true);
  const [empty, setEmpty] = useState(true);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const poll = useCallback(async () => {
    if (!card) return;
    try {
      const list = await fetchTaskList(card);
      list.sort((a, b) => {
        const ta = a.status?.timestamp || "";
        const tb = b.status?.timestamp || "";
        return tb.localeCompare(ta);
      });
      setTasks(list);
      setEmpty(list.length === 0);
      setPollOk(true);
    } catch {
      setPollOk(false);
    }
  }, [card]);

  useEffect(() => {
    if (!active) {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      return;
    }
    poll();
    timerRef.current = setInterval(poll, pollInterval);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [active, poll, pollInterval]);

  const toggleExpand = useCallback(
    async (taskId: string) => {
      if (expandedId === taskId) {
        setExpandedId(null);
        setDetail(null);
        setDetailError(null);
        return;
      }
      setExpandedId(taskId);
      setDetail(null);
      setDetailError(null);
      if (!card) return;
      try {
        const t = await fetchTaskById(card, taskId);
        setDetail(t);
      } catch (err) {
        setDetailError(err instanceof Error ? err.message : String(err));
      }
    },
    [card, expandedId],
  );

  return (
    <>
      <div className="tasks-header">
        <h2>Tasks</h2>
        <div className="tasks-status">
          <span className={`poll-dot${pollOk ? "" : " stale"}`} />
          polling:
          <select
            className="poll-select"
            value={pollInterval}
            onChange={(e) => onPollIntervalChange(Number(e.target.value))}
          >
            {POLL_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
      </div>
      <div className="tasks-content">
        {empty ? (
          <div className="empty-state">No tasks yet.</div>
        ) : (
          <table className="task-table">
            <thead>
              <tr>
                <th>Task ID</th>
                <th>State</th>
                <th>Context</th>
                <th>Timestamp</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((t) => {
                const state = t.status?.state || "unknown";
                const ts = t.status?.timestamp || "";
                const isExpanded = expandedId === t.id;
                return (
                  <TaskRow
                    key={t.id}
                    task={t}
                    state={state}
                    ts={ts}
                    isExpanded={isExpanded}
                    detail={isExpanded ? detail : null}
                    detailError={isExpanded ? detailError : null}
                    onClick={() => toggleExpand(t.id!)}
                  />
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function TaskRow({
  task,
  state,
  ts,
  isExpanded,
  detail,
  detailError,
  onClick,
}: {
  task: Task;
  state: string;
  ts: string;
  isExpanded: boolean;
  detail: Task | null;
  detailError: string | null;
  onClick: () => void;
}) {
  const shortId = (task.id || "").substring(0, 8);
  const shortCtx = (task.contextId || "").substring(0, 12);
  const shortTs = ts ? formatTimestamp(ts) : "\u2014";

  return (
    <>
      <tr className="task-row" onClick={onClick}>
        <td><span className="task-id">{shortId}</span></td>
        <td><StateBadge state={state} /></td>
        <td><span className="task-ctx">{shortCtx || "\u2014"}</span></td>
        <td><span className="task-time">{shortTs}</span></td>
      </tr>
      {isExpanded && (
        <tr>
          <td colSpan={4} className="task-detail">
            {detailError ? (
              <div className="task-error">Failed to load task: {detailError}</div>
            ) : detail ? (
              <TaskDetail task={detail} />
            ) : (
              <div style={{ color: "var(--text-dim)", fontSize: 13 }}>
                Loading task details...
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function TaskDetail({ task }: { task: Task }) {
  const state = task.status?.state || "unknown";
  const ts = task.status?.timestamp || "";
  const history = task.history || [];
  const artifacts = task.artifacts || [];
  const transitions: StateTransition[] =
    (task.metadata?.stateTransitions as StateTransition[] | undefined) || [];

  const filteredMeta: Record<string, unknown> = {};
  let hasMeta = false;
  if (task.metadata) {
    for (const [key, val] of Object.entries(task.metadata)) {
      if (!key.startsWith("_") && key !== "stateTransitions") {
        filteredMeta[key] = val;
        hasMeta = true;
      }
    }
  }

  const statusText =
    task.status?.message ? extractPartsText(task.status.message.parts || []) : "";

  return (
    <>
      <div className="task-detail-section">
        <div className="task-detail-row">
          <span className="task-detail-label">Task</span>
          <span className="task-detail-value">{task.id || ""}</span>
        </div>
        <div className="task-detail-row">
          <span className="task-detail-label">Context</span>
          <span className="task-detail-value">{task.contextId || "\u2014"}</span>
        </div>
        <div className="task-detail-row">
          <span className="task-detail-label">State</span>
          <span className="task-detail-value">
            <StateBadge state={state} /> {ts}
          </span>
        </div>
        {statusText && (
          <div className="task-detail-row">
            <span className="task-detail-label">Status</span>
            <span className="task-detail-value" style={{ fontFamily: "var(--font)" }}>
              {statusText}
            </span>
          </div>
        )}
      </div>

      {transitions.length > 0 && (
        <div className="task-detail-section">
          <h3>State Transitions</h3>
          <div className="transitions-timeline">
            {transitions.map((tr, i) => (
              <div key={i} className="transition-item">
                <div className="transition-dot-col">
                  <span className="transition-dot" />
                  {i < transitions.length - 1 && <span className="transition-line" />}
                </div>
                <div className="transition-content">
                  <StateBadge state={tr.state} />
                  <span className="transition-ts">{formatTimestamp(tr.timestamp)}</span>
                  {tr.messageText && (
                    <span className="transition-msg">{tr.messageText}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {history.length > 0 && (
        <div className="task-detail-section">
          <h3>History</h3>
          {history.map((entry, i) => {
            const role = entry.role || "unknown";
            const cls = role === "user" ? "history-user" : "history-agent";
            return (
              <div key={i} className={`history-entry ${cls}`}>
                <div className="history-role">
                  {role}
                  {entry.messageId && (
                    <span style={{ color: "var(--text-dim)", fontFamily: "var(--mono)", fontSize: 10, marginLeft: 6 }}>
                      {entry.messageId.substring(0, 8)}
                    </span>
                  )}
                </div>
                <PartsDisplay parts={entry.parts || []} />
              </div>
            );
          })}
        </div>
      )}

      {artifacts.length > 0 && (
        <div className="task-detail-section">
          <h3>Artifacts</h3>
          {artifacts.map((art, i) => (
            <div key={i} className="artifact-item">
              {art.artifactId && <div className="artifact-id">{art.artifactId}</div>}
              <PartsDisplay parts={art.parts || []} />
            </div>
          ))}
        </div>
      )}

      {hasMeta && (
        <div className="task-detail-section">
          <h3>Metadata</h3>
          <div className="metadata-block">{JSON.stringify(filteredMeta, null, 2)}</div>
        </div>
      )}
    </>
  );
}
