import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentCard, ChatMessage, Task } from "../lib/types";
import {
  cancelTask,
  extractPartsText,
  extractTextFromTask,
  fetchTaskById,
  isJsonRpc,
  isStreaming as isStreamingAgent,
  sendBlocking,
  sendStreamingRequest,
} from "../lib/protocol";
import { StateBadge } from "./StateBadge";

interface Props {
  card: AgentCard | null;
}

export function ChatView({ card }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const taskIdRef = useRef<string | null>(null);
  const contextIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "instant" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const addMsg = useCallback(
    (type: ChatMessage["type"], text: string, state?: string) => {
      const msg: ChatMessage = { id: crypto.randomUUID(), type, text, state };
      setMessages((prev) => [...prev, msg]);
      return msg.id;
    },
    [],
  );

  const updateIds = useCallback((task: Task) => {
    if (!task) return;
    const state = task.status?.state;
    if (state === "input-required" || state === "auth-required") {
      if (task.id) taskIdRef.current = task.id;
      if (task.contextId) contextIdRef.current = task.contextId;
    } else if (
      state === "completed" ||
      state === "failed" ||
      state === "canceled" ||
      state === "rejected"
    ) {
      taskIdRef.current = null;
      contextIdRef.current = null;
    } else {
      if (task.id) taskIdRef.current = task.id;
      if (task.contextId) contextIdRef.current = task.contextId;
    }
  }, []);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || sending || !card) return;

    addMsg("user", text);
    setInput("");
    setSending(true);

    const ac = new AbortController();
    abortRef.current = ac;

    const jsonRpc = isJsonRpc(card);
    const streaming = isStreamingAgent(card);
    const agentUrl = card.url || "";

    const msg: Record<string, unknown> = {
      role: "user",
      messageId: crypto.randomUUID(),
      parts: [{ kind: "text", text }],
    };
    if (taskIdRef.current) msg.taskId = taskIdRef.current;
    if (contextIdRef.current) msg.contextId = contextIdRef.current;

    const params: Record<string, unknown> = { message: msg };

    try {
      if (streaming) {
        await handleStreaming(agentUrl, params, jsonRpc, ac.signal);
      } else {
        await handleBlocking(agentUrl, params, jsonRpc, ac.signal);
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        // Cancelled by user — refs already cleared by handleCancel
      } else {
        addMsg("error", "Error: " + (err instanceof Error ? err.message : String(err)));
        // Clear task refs so the next message starts fresh
        taskIdRef.current = null;
        contextIdRef.current = null;
      }
    }

    abortRef.current = null;
    setSending(false);
  }, [input, sending, card, addMsg, updateIds]);

  const handleCancel = useCallback(async () => {
    if (!card) return;
    const tid = taskIdRef.current;
    // Abort the SSE stream / polling
    abortRef.current?.abort();
    // Clear task refs so the next message starts a new task
    taskIdRef.current = null;
    contextIdRef.current = null;
    // Cancel the task on the server
    if (tid) {
      try {
        await cancelTask(card, tid);
        addMsg("status", "Task canceled.");
      } catch {
        addMsg("status", "Cancel request sent.");
      }
    }
  }, [card, addMsg]);

  const handleBlocking = useCallback(
    async (agentUrl: string, params: Record<string, unknown>, jsonRpc: boolean, signal?: AbortSignal) => {
      addMsg("status", "Thinking...");

      // Submit non-blocking so we get the task ID immediately for cancel support
      const initial = await sendBlocking({ agentUrl, jsonRpc, params }, signal);
      if (initial.id) taskIdRef.current = initial.id;
      if (initial.contextId) contextIdRef.current = initial.contextId;
      updateIds(initial);

      const DONE = new Set(["completed", "failed", "canceled", "rejected", "input-required", "auth-required"]);
      const state = initial.status?.state || "";

      if (!initial.id || DONE.has(state) || initial.kind === "message") {
        setMessages((prev) => prev.filter((m) => !(m.type === "status" && m.text === "Thinking...")));
        handleTaskOrMessage(initial);
        return;
      }

      // Poll until terminal or input-required state
      while (!signal?.aborted) {
        await new Promise((r) => setTimeout(r, 500));
        if (signal?.aborted) break;
        const task = await fetchTaskById(card!, initial.id);
        const s = task.status?.state || "";
        if (DONE.has(s)) {
          setMessages((prev) => prev.filter((m) => !(m.type === "status" && m.text === "Thinking...")));
          handleTaskOrMessage(task);
          return;
        }
      }
    },
    [card, addMsg, updateIds],
  );

  const handleTaskOrMessage = useCallback(
    (data: Task) => {
      if (!data) return;

      // Direct message reply
      if (data.kind === "message" || (!data.id && data.parts)) {
        const text = extractPartsText(data.parts || []);
        if (text) addMsg("agent", text, "completed");
        return;
      }

      updateIds(data);
      const state = data.status?.state || "completed";

      if (data.status?.message) {
        const statusText = extractPartsText(data.status.message.parts || []);
        if (state === "input-required" || state === "auth-required") {
          addMsg("agent", statusText || state, state);
          return;
        }
      }

      const text = extractTextFromTask(data);
      if (text) {
        addMsg("agent", text, state);
      } else if (data.status) {
        const stateText = extractPartsText(data.status.message?.parts || []);
        addMsg("agent", stateText || "(Task " + state + ")", state);
      }
    },
    [addMsg, updateIds],
  );

  const handleStreaming = useCallback(
    async (agentUrl: string, params: Record<string, unknown>, jsonRpc: boolean, signal?: AbortSignal) => {
      let agentText = "";
      let agentMsgId: string | null = null;
      let lastState = "";

      await sendStreamingRequest({ agentUrl, jsonRpc, params }, {
        onStatus(text, state) {
          lastState = state || lastState;
          if (agentMsgId) {
            const id = agentMsgId;
            const st = lastState;
            const t = text || agentText;
            setMessages((prev) =>
              prev.map((m) =>
                m.id === id ? { ...m, text: t || m.text, state: st } : m,
              ),
            );
          } else if (text) {
            agentMsgId = addMsg("agent", text, lastState);
          }
        },
        onArtifactChunk(chunk, state) {
          lastState = state || lastState;
          agentText += chunk;
          const currentText = agentText;
          const currentState = lastState;
          if (!agentMsgId) {
            agentMsgId = addMsg("agent", currentText, currentState);
          } else {
            const id = agentMsgId;
            setMessages((prev) =>
              prev.map((m) =>
                m.id === id ? { ...m, text: currentText, state: currentState } : m,
              ),
            );
          }
        },
        onTask(task) {
          updateIds(task);
          lastState = task.status?.state || lastState;
          if (!agentText) {
            const text = extractTextFromTask(task);
            if (text) agentMsgId = addMsg("agent", text, lastState);
          } else if (agentMsgId) {
            const id = agentMsgId;
            const st = lastState;
            setMessages((prev) =>
              prev.map((m) => (m.id === id ? { ...m, state: st } : m)),
            );
          }
        },
        onMessage(text) {
          addMsg("agent", text, "completed");
        },
        onError(text) {
          addMsg("error", text);
        },
      }, signal);

      if (!agentMsgId && !agentText) {
        addMsg("status", "Stream completed (no text content)");
      }
    },
    [addMsg, updateIds],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const newConversation = useCallback(() => {
    taskIdRef.current = null;
    contextIdRef.current = null;
    setMessages([]);
  }, []);

  const autoResize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  }, []);

  return (
    <>
      <div className="chat-header">
        <h2>Chat</h2>
        <button className="btn btn-secondary" onClick={newConversation} title="Clear conversation and start fresh">
          New Conversation
        </button>
      </div>
      <div className="chat-messages">
        {messages.length === 0 && (
          <div className="empty-state">Send a message to start chatting with the agent.</div>
        )}
        {messages.map((m) => {
          if (m.type === "user") {
            return (
              <div key={m.id} className="msg msg-user">
                <div className="msg-label">You</div>
                {m.text}
              </div>
            );
          }
          if (m.type === "agent") {
            return (
              <div key={m.id} className="msg msg-agent">
                <div className="msg-label">
                  Agent {m.state && <StateBadge state={m.state} />}
                </div>
                {m.text}
              </div>
            );
          }
          if (m.type === "status") {
            return (
              <div key={m.id} className="msg msg-status">
                {m.text}
              </div>
            );
          }
          if (m.type === "error") {
            return (
              <div key={m.id} className="msg msg-error">
                {m.text}
              </div>
            );
          }
          return null;
        })}
        {sending && (
          <div className="loading">
            <div className="dot-pulse">
              <span /><span /><span />
            </div>
            <span>Thinking...</span>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>
      <div className="chat-input-area">
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            autoResize();
          }}
          onKeyDown={handleKeyDown}
          placeholder="Type a message..."
          rows={1}
          disabled={sending}
        />
        {sending ? (
          <button className="btn btn-cancel" onClick={handleCancel}>
            Cancel
          </button>
        ) : (
          <button className="btn btn-primary" onClick={handleSend} disabled={!card}>
            Send
          </button>
        )}
      </div>
    </>
  );
}
