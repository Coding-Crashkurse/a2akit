import type { AgentCard } from "../lib/types";

export function AgentInfo({
  card,
  error,
}: {
  card: AgentCard | null;
  error: string | null;
}) {
  if (error) {
    return (
      <div className="panel-left">
        <h2>Agent Information</h2>
        <div className="error-banner">
          <p>Failed to load agent card</p>
          <p style={{ fontSize: 12, color: "var(--text-dim)" }}>{error}</p>
          <button className="btn btn-secondary" onClick={() => location.reload()}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!card) {
    return (
      <div className="panel-left">
        <h2>Agent Information</h2>
        <p style={{ color: "var(--text-dim)" }}>Loading agent card...</p>
      </div>
    );
  }

  const proto = card.preferredTransport === "jsonrpc" ? "JSON-RPC" : "HTTP+JSON";
  const streaming = card.capabilities?.streaming;
  const stateHistory = card.capabilities?.stateTransitionHistory;
  const inputModes = card.defaultInputModes || [];
  const outputModes = card.defaultOutputModes || [];
  const skills = card.skills || [];

  return (
    <div className="panel-left">
      <h2>Agent Information</h2>
      <div className="info-group">
        <InfoRow label="Name" value={card.name} />
        <InfoRow label="Description" value={card.description || "\u2014"} />
        <InfoRow label="Version" value={card.version || "\u2014"} />
        <InfoRow label="Protocol" value={proto} />
        <InfoRow label="A2A Version" value={card.protocolVersion || "\u2014"} />
        <InfoRow label="URL" value={card.url || "\u2014"} mono />
        <div className="info-row">
          <span className="info-label">Streaming</span>
          <span className="info-value">
            {streaming ? (
              <span className="check">{"\u2713"}</span>
            ) : (
              <span className="cross">{"\u2717"}</span>
            )}
          </span>
        </div>
        <div className="info-row">
          <span className="info-label">State History</span>
          <span className="info-value">
            {stateHistory ? (
              <span className="check">{"\u2713"}</span>
            ) : (
              <span className="cross">{"\u2717"}</span>
            )}
          </span>
        </div>
      </div>

      {(inputModes.length > 0 || outputModes.length > 0) && (
        <>
          <h2 style={{ marginTop: 8 }}>Modes</h2>
          <div className="info-group">
            {inputModes.length > 0 && (
              <div className="info-row" style={{ flexDirection: "column" }}>
                <span className="info-label">Input</span>
                <div style={{ marginTop: 4 }}>
                  {inputModes.map((m) => (
                    <span key={m} className="mode-tag">{m}</span>
                  ))}
                </div>
              </div>
            )}
            {outputModes.length > 0 && (
              <div className="info-row" style={{ flexDirection: "column" }}>
                <span className="info-label">Output</span>
                <div style={{ marginTop: 4 }}>
                  {outputModes.map((m) => (
                    <span key={m} className="mode-tag">{m}</span>
                  ))}
                </div>
              </div>
            )}
          </div>
        </>
      )}

      <h2 style={{ marginTop: 8 }}>Skills</h2>
      {skills.length === 0 ? (
        <p style={{ color: "var(--text-dim)", fontSize: 13 }}>(none)</p>
      ) : (
        skills.map((s, i) => (
          <div key={i} className="skill-item">
            <div className="skill-name">{s.name || s.id || "Unnamed"}</div>
            {s.description && <div className="skill-desc">{s.description}</div>}
          </div>
        ))
      )}
    </div>
  );
}

function InfoRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="info-row">
      <span className="info-label">{label}</span>
      <span className={`info-value${mono ? " mono" : ""}`}>{value}</span>
    </div>
  );
}
