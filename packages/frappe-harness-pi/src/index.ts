/**
 * Optional Pi boundary: typed JSON in, typed JSON out.
 *
 * This package deliberately has no Pi runtime dependency and no authority to
 * execute commands, access files, call providers, or record approvals. A host
 * may transport these values over the existing headless harness protocol.
 */

export const OPERATIONS = [
  "get_supported_vocabulary",
  "submit_requirement_facts",
  "submit_clarifications",
  "propose_spec_patch",
  "get_validation_report",
  "harness_status",
] as const;

export const OPERATOR_COMMANDS = [
  "/harness", "/spec", "/validate", "/plan", "/status", "/approval",
] as const;

/** Built-in Pi capabilities that must remain disabled for this adapter. */
export const DENIED_BUILTIN_TOOLS = [
  "bash", "read", "write", "edit", "pi.exec", "filesystem", "shell",
] as const;

const AUTHORITY_TOKENS = [
  "path", "command", "query", "credential", "provider", "approval", "shell", "filesystem",
] as const;

export type Operation = (typeof OPERATIONS)[number];
export type OperatorCommand = (typeof OPERATOR_COMMANDS)[number];

/** Minimal host surface used by the inert, authority-free Pi extension. */
export type PiExtensionHost = Readonly<{
  on(event: "session_start", handler: () => void): void;
}>;

export type PiExtensionFactory = (host: PiExtensionHost) => void;

/**
 * Create the distributable Pi extension boundary.
 *
 * It registers only a no-op lifecycle observer. It deliberately registers no
 * tools, commands, providers, filesystem access, shell access, or approvals.
 * Protocol transport remains the responsibility of the existing headless
 * harness boundary.
 */
export function createPiExtension(): PiExtensionFactory {
  return (host: PiExtensionHost): void => {
    if (!host || typeof host.on !== "function") {
      throw new TypeError("Pi extension host must expose an event registrar");
    }
    host.on("session_start", () => undefined);
  };
}

export default createPiExtension();

export type ToolPolicy = Readonly<{
  allow: readonly [];
  deny: typeof DENIED_BUILTIN_TOOLS;
}>;

export type ProposalStatusDescriptor = Readonly<{
  operations: typeof OPERATIONS;
  commands: typeof OPERATOR_COMMANDS;
  tool_policy: ToolPolicy;
}>;

export const PROPOSAL_STATUS_DESCRIPTOR: ProposalStatusDescriptor = Object.freeze({
  operations: OPERATIONS,
  commands: OPERATOR_COMMANDS,
  tool_policy: Object.freeze({ allow: [] as const, deny: DENIED_BUILTIN_TOOLS }),
});

export type PiRequest = Readonly<{
  request_id: string;
  operation: Operation;
  payload: Readonly<Record<string, unknown>>;
}>;

export type PiResponse = Readonly<{
  request_id: string;
  ok: boolean;
  data?: Readonly<Record<string, unknown>>;
  error_code?: string;
}>;

/** Versioned headless protocol envelope; session metadata is display-only. */
export type SessionMetadata = Readonly<{
  session_id: string;
  session_hash?: string;
  authoritative: false;
}>;

export type HeadlessRequest = Readonly<{
  protocol_version: 1;
  session: SessionMetadata;
  request: PiRequest;
}>;

export type HeadlessResponse = Readonly<{
  protocol_version: 1;
  session: SessionMetadata;
  response: PiResponse;
}>;

export function bindHeadlessRequest(request: PiRequest, session: SessionMetadata): HeadlessRequest {
  if (session.authoritative !== false || !session.session_id.trim()) {
    throw new TypeError("Pi session metadata must remain non-authoritative");
  }
  return Object.freeze({ protocol_version: 1, session, request });
}

export function validateRequest(value: unknown): PiRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("Pi request must be an object");
  }
  const request = value as Record<string, unknown>;
  if (Object.keys(request).some((key) => !["request_id", "operation", "payload"].includes(key))) {
    throw new TypeError("Pi request contains unsupported top-level keys");
  }
  if (typeof request.request_id !== "string" || !request.request_id.trim()) {
    throw new TypeError("request_id must be a non-empty string");
  }
  if (!OPERATIONS.includes(request.operation as Operation)) {
    throw new TypeError("operation is not allowlisted");
  }
  if (!request.payload || typeof request.payload !== "object" || Array.isArray(request.payload)) {
    throw new TypeError("payload must be an object");
  }
  assertDataOnly(request.payload);
  return request as PiRequest;
}

/** Reject authority-shaped data recursively without interpreting proposal text. */
export function assertDataOnly(value: unknown): void {
  if (Array.isArray(value)) {
    value.forEach(assertDataOnly);
    return;
  }
  if (!value || typeof value !== "object") {
    if (typeof value === "string" && /^(?:\/|[A-Za-z]:[\\/])/.test(value)) {
      throw new TypeError("absolute paths are not allowed in Pi payloads");
    }
    if (typeof value === "string" && /(?:^|[\\/])\.\.(?:[\\/]|$)/.test(value)) {
      throw new TypeError("path traversal values are not allowed in Pi payloads");
    }
    if (typeof value === "string" && /(?:^|\s)(?:rm|sudo|chmod|curl|wget)\s/i.test(value)) {
      throw new TypeError("shell-shaped values are not allowed in Pi payloads");
    }
    return;
  }
  for (const [key, child] of Object.entries(value)) {
    const normalized = key.toLowerCase().replace(/[^a-z]/g, "");
    if (AUTHORITY_TOKENS.some((token) => normalized.includes(token))) {
      throw new TypeError(`authority-shaped key is not allowed: ${key}`);
    }
    assertDataOnly(child);
  }
}
