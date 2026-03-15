export interface AgentCard {
  name: string;
  description?: string;
  version?: string;
  preferredTransport: "jsonrpc" | "http+json";
  protocolVersion?: string;
  url?: string;
  capabilities?: { streaming?: boolean; stateTransitionHistory?: boolean };
  defaultInputModes?: string[];
  defaultOutputModes?: string[];
  skills?: Array<{ id?: string; name?: string; description?: string }>;
}

export interface Part {
  kind: "text" | "data" | "file";
  text?: string;
  data?: unknown;
  filename?: string;
  mediaType?: string;
}

export interface Message {
  role: "user" | "agent";
  messageId?: string;
  parts: Part[];
}

export interface TaskStatus {
  state: string;
  timestamp?: string;
  message?: Message;
}

export interface Artifact {
  artifactId?: string;
  parts: Part[];
}

export interface Task {
  kind?: string;
  id?: string;
  contextId?: string;
  status?: TaskStatus;
  artifacts?: Artifact[];
  history?: Message[];
  metadata?: Record<string, unknown>;
  // direct message reply fields
  parts?: Part[];
}

export interface StateTransition {
  state: string;
  timestamp: string;
  messageText?: string;
}

export interface ChatMessage {
  id: string;
  type: "user" | "agent" | "status" | "error";
  text: string;
  state?: string;
}
