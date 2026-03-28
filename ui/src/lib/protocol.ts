import type { AgentCard, Part, Task } from "./types";

export function isJsonRpc(card: AgentCard): boolean {
  return card.preferredTransport?.toLowerCase() === "jsonrpc";
}

export function isStreaming(card: AgentCard): boolean {
  return !!(card.capabilities && card.capabilities.streaming);
}

export function extractPartsText(parts: Part[]): string {
  let text = "";
  for (const p of parts) {
    if (p.kind === "text" && p.text) text += (text ? "\n" : "") + p.text;
    else if (p.kind === "data") text += (text ? "\n" : "") + JSON.stringify(p.data, null, 2);
  }
  return text;
}

export function extractTextFromTask(task: Task): string {
  if (!task) return "";
  const artifacts = task.artifacts || [];
  let text = "";
  for (const art of artifacts) {
    for (const part of art.parts || []) {
      if (part.kind === "text" && part.text) text += (text ? "\n" : "") + part.text;
      else if (part.kind === "data") text += (text ? "\n" : "") + JSON.stringify(part.data, null, 2);
    }
  }
  if (text) return text;
  if (task.status?.message) {
    return extractPartsText(task.status.message.parts || []);
  }
  return "";
}

export function extractStatusText(evt: Record<string, unknown>): string {
  const status = evt.status as { message?: { parts?: Part[] }; state?: string } | undefined;
  if (status?.message) {
    return extractPartsText(status.message.parts || []);
  }
  if (status?.state) return status.state;
  return "";
}

export function extractArtifactText(evt: Record<string, unknown>): string {
  const artifact = (evt.artifact || evt) as { parts?: Part[] };
  const parts = artifact.parts || [];
  let text = "";
  for (const p of parts) {
    if (p.kind === "text" && p.text) text += p.text;
    else if (p.kind === "data") text += JSON.stringify(p.data);
  }
  return text;
}

interface SendParams {
  agentUrl: string;
  jsonRpc: boolean;
  params: Record<string, unknown>;
}

export async function sendBlocking({ agentUrl, jsonRpc, params }: SendParams): Promise<Task> {
  let url: string, body: string;
  if (jsonRpc) {
    url = agentUrl;
    body = JSON.stringify({ jsonrpc: "2.0", id: crypto.randomUUID(), method: "message/send", params });
  } else {
    url = agentUrl + "/message:send";
    body = JSON.stringify(params);
  }

  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });

  if (!resp.ok) {
    const errText = await resp.text().catch(() => "");
    throw new Error("HTTP " + resp.status + (errText ? ": " + errText : ""));
  }

  let data = await resp.json();
  if (jsonRpc) {
    if (data.error) throw new Error("JSON-RPC error " + data.error.code + ": " + data.error.message);
    data = data.result;
  }
  return data as Task;
}

export interface StreamCallbacks {
  onStatus: (text: string, state: string) => void;
  onArtifactChunk: (text: string, state: string) => void;
  onTask: (task: Task) => void;
  onMessage: (text: string) => void;
  onError: (text: string) => void;
}

export async function sendStreamingRequest(
  { agentUrl, jsonRpc, params }: SendParams,
  cb: StreamCallbacks,
): Promise<void> {
  let url: string, body: string;
  if (jsonRpc) {
    url = agentUrl;
    body = JSON.stringify({ jsonrpc: "2.0", id: crypto.randomUUID(), method: "message/sendStream", params });
  } else {
    url = agentUrl + "/message:stream";
    body = JSON.stringify(params);
  }

  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body,
  });

  if (!resp.ok) {
    const errText = await resp.text().catch(() => "");
    throw new Error("HTTP " + resp.status + (errText ? ": " + errText : ""));
  }

  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventType = "";
  let eventData = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.startsWith("event:")) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        eventData = line.slice(5).trim();
      } else if (line === "" && eventData) {
        try {
          let parsed = JSON.parse(eventData);
          if (jsonRpc && parsed.jsonrpc) {
            if (parsed.error) {
              cb.onError("JSON-RPC error " + parsed.error.code + ": " + parsed.error.message);
              eventData = "";
              eventType = "";
              continue;
            }
            parsed = parsed.result;
          }

          const kind = eventType || parsed.kind || "";
          if (kind === "status-update") {
            const statusText = extractStatusText(parsed);
            const state = parsed.status?.state || "";
            if (statusText) cb.onStatus(statusText, state);
          } else if (kind === "artifact-update") {
            const chunk = extractArtifactText(parsed);
            const state = parsed.status?.state || "";
            if (chunk) cb.onArtifactChunk(chunk, state);
          } else if (kind === "task") {
            cb.onTask(parsed as Task);
          } else if (kind === "message") {
            const text = extractPartsText(parsed.parts || []);
            if (text) cb.onMessage(text);
          }
        } catch {
          // Non-JSON data line
        }
        eventData = "";
        eventType = "";
      }
    }
  }
}

export async function fetchTaskList(card: AgentCard): Promise<Task[]> {
  const agentUrl = card.url || "";
  if (isJsonRpc(card)) {
    const resp = await fetch(agentUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: crypto.randomUUID(), method: "tasks/list", params: {} }),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const rpc = await resp.json();
    if (rpc.error) throw new Error("JSON-RPC " + rpc.error.code + ": " + rpc.error.message);
    return (rpc.result?.tasks || rpc.result || []) as Task[];
  } else {
    const resp = await fetch(agentUrl + "/tasks", {
      headers: { "Content-Type": "application/json" },
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    return (data.tasks || data || []) as Task[];
  }
}

export async function fetchTaskById(card: AgentCard, taskId: string): Promise<Task> {
  const agentUrl = card.url || "";
  if (isJsonRpc(card)) {
    const resp = await fetch(agentUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: crypto.randomUUID(), method: "tasks/get", params: { id: taskId, historyLength: 100 } }),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const rpc = await resp.json();
    if (rpc.error) throw new Error("JSON-RPC " + rpc.error.code + ": " + rpc.error.message);
    return rpc.result as Task;
  } else {
    const resp = await fetch(agentUrl + "/tasks/" + encodeURIComponent(taskId) + "?historyLength=100", {
      headers: { "Content-Type": "application/json" },
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return (await resp.json()) as Task;
  }
}
