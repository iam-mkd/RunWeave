// The RunWeave HTTP agent contract, runweave.agent/v1 (contracts/agent-http-v1.openapi.json).

export const CONTRACT_VERSION = 'runweave.agent/v1';
export const MANIFEST_PATH = '/runweave/v1/agents';
export const NAME_PATTERN = /^[a-z][a-z0-9-]{0,62}$/;
export const VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9.+_-]{0,39}$/;
export const MAX_DEADLINE_MS = 86_400_000;
export const TITLE_LIMIT = 120;
export const DETAIL_LIMIT = 1000;
export const STEP_EVENT_CAP = 200;

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type JsonObject = { [key: string]: unknown };
export type JsonSchema = JsonObject;

export interface AgentDescriptor {
  name: string;
  version: string;
  description: string;
  input_schema: JsonSchema;
  output_schema: JsonSchema;
  max_steps: number;
}

export interface Manifest {
  contract: typeof CONTRACT_VERSION;
  agents: AgentDescriptor[];
}

export interface RunRequest {
  run_id: string;
  attempt: number;
  input: JsonObject;
  deadline_ms: number;
}

export interface StepRecord {
  title: string;
  detail: string | null;
  status: 'succeeded' | 'failed';
  duration_ms: number;
}

export interface RunResponse {
  output: JsonObject;
  steps: StepRecord[];
}

export interface ErrorLocation {
  loc: (string | number)[];
  type: string;
  msg: string;
}

export function isObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isInteger(value: unknown, min: number, max: number): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max;
}

/** Validates the request envelope. Unknown fields are ignored for forward compatibility. */
export function parseRunRequest(value: unknown): RunRequest | null {
  if (!isObject(value)) return null;
  const { run_id: runId, attempt, input, deadline_ms: deadlineMs } = value;
  if (typeof runId !== 'string' || !UUID_PATTERN.test(runId)) return null;
  if (!isInteger(attempt, 1, 1000) || !isInteger(deadlineMs, 1, MAX_DEADLINE_MS) || !isObject(input)) return null;
  return { run_id: runId.toLowerCase(), attempt, input, deadline_ms: deadlineMs };
}

export function isUuid(value: string): boolean {
  return UUID_PATTERN.test(value);
}

export function clip(text: string, limit: number): string {
  const trimmed = text.trim();
  return trimmed.length > limit ? `${trimmed.slice(0, limit - 1)}…` : trimmed;
}
