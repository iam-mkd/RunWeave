import {
  ACTION_PATTERN, DETAIL_LIMIT, NAME_PATTERN, STEP_EVENT_CAP, TITLE_LIMIT, VERSION_PATTERN, clip, isObject,
  type ErrorLocation, type JsonObject, type JsonSchema, type ProxyBinding, type StepRecord,
} from './contract.js';

/** A classified failure whose code RunWeave may show in run snapshots. */
export class AgentError extends Error {
  readonly code: string;

  constructor(code = 'RUN_FAILED') {
    super(code);
    this.name = 'AgentError';
    this.code = code;
  }
}

export class StepLimitError extends AgentError {
  constructor() { super('AGENT_STEP_LIMIT'); }
}

/** No decision before the approval expired. Uncaught, the run ends TIMED_OUT. */
export class ApprovalExpired extends AgentError {
  constructor() { super('APPROVAL_EXPIRED'); }
}

/** Approvals need a durable (PostgreSQL) RunWeave; this one sent no approvals URL. */
export class ApprovalUnavailable extends AgentError {
  constructor() { super('DURABLE_APPROVAL_REQUIRED'); }
}

/** A person's decision on one exact request. A rejection is returned, not thrown. */
export interface Approval {
  readonly id: string;
  readonly status: 'approved' | 'rejected';
  readonly approved: boolean;
  readonly decidedBy: string | null;
  readonly decidedAt: string | null;
}

/** Throw from `parseInput` to reject input with 422. Never include submitted values. */
export class InputError extends Error {
  readonly errors: ErrorLocation[];

  constructor(errors: ErrorLocation[]) {
    super('Invalid input');
    this.name = 'InputError';
    this.errors = errors.slice(0, 20);
  }
}

/** The reason on `ctx.signal` when RunWeave cancels the run or its deadline passes. */
export class RunAborted extends Error {
  readonly reason: 'cancelled' | 'deadline';

  constructor(reason: 'cancelled' | 'deadline') {
    super(reason === 'deadline' ? 'The run deadline passed' : 'The run was cancelled');
    this.name = 'RunAborted';
    this.reason = reason;
  }
}

/** The run's binding to RunWeave's LLM proxy: use `token` as the API key with these URLs. */
export interface ModelProxy {
  readonly token: string;
  readonly openaiBaseUrl: string;
  readonly anthropicBaseUrl: string;
  /** Environment variables the official SDKs read, for subprocesses you start. */
  env(): Record<string, string>;
}

export interface RunContext {
  readonly runId: string;
  readonly agent: string;
  /** 2+ when RunWeave re-runs the run after a lost lease (durable mode). */
  readonly attempt: number;
  /** Aborted when the run is cancelled or its deadline passes. Pass it to your I/O. */
  readonly signal: AbortSignal;
  /** Milliseconds left before the run deadline, which includes queue wait and retries. */
  remainingMs(): number;
  remainingSeconds(): number;
  /** Runs `fn` as one timeline step. Exceeding `maxSteps` fails the run. */
  step<T>(title: string, fn: () => Promise<T> | T, options?: { detail?: string }): Promise<T>;
  /** Logs with protected values redacted, at most 1,000 characters. */
  log(message: string): void;
  /** Point your OpenAI or Anthropic SDK here; null when RunWeave sent no proxy binding. */
  readonly proxy: ModelProxy | null;
  /** Model usage is metered by RunWeave and shown on the run; not readable here yet. */
  usage(): null;
  /**
   * Ask a person to approve `action` with exactly these `args` (JSON) and wait. Returns the
   * decision; throws ApprovalExpired when nobody decides within `expiresIn` seconds (capped
   * by RunWeave). The run deadline is paused while waiting. Repeating the same request after
   * a restart returns the existing request or decision; making the approved effect itself
   * idempotent is up to you. Needs a durable (PostgreSQL) RunWeave.
   */
  requestApproval(action: string, args: unknown, options?: { expiresIn?: number }): Promise<Approval>;
}

export interface AgentOptions<I extends JsonObject, O extends JsonObject> {
  name: string;
  version: string;
  description?: string;
  maxSteps?: number;
  /** JSON Schema (2020-12) for input; RunWeave validates it before calling you. */
  inputSchema: JsonSchema;
  outputSchema: JsonSchema;
  /** Optional local validation, for example with zod or ajv. Throw InputError to reject. */
  parseInput?: (raw: JsonObject) => I;
  /** Optional output validation. Throwing fails the run with AGENT_OUTPUT_INVALID. */
  parseOutput?: (raw: unknown) => O;
  run: (input: I, ctx: RunContext) => Promise<O>;
}

export interface AgentDefinition<I extends JsonObject = JsonObject, O extends JsonObject = JsonObject> {
  readonly name: string;
  readonly version: string;
  readonly description: string;
  readonly maxSteps: number;
  readonly inputSchema: JsonSchema;
  readonly outputSchema: JsonSchema;
  readonly parseInput: ((raw: JsonObject) => I) | undefined;
  readonly parseOutput: ((raw: unknown) => O) | undefined;
  readonly run: (input: I, ctx: RunContext) => Promise<O>;
}

export function defineAgent<I extends JsonObject = JsonObject, O extends JsonObject = JsonObject>(
  options: AgentOptions<I, O>,
): AgentDefinition<I, O> {
  const { name, version, description = '', maxSteps = 50 } = options;
  if (!NAME_PATTERN.test(name)) throw new TypeError('Agent name: lowercase letters, digits and hyphens (max 63)');
  if (!VERSION_PATTERN.test(version)) throw new TypeError('Agent version: 1-40 characters of letters, digits, . + _ -');
  if (description.length > 500) throw new TypeError('Agent description: at most 500 characters');
  if (!Number.isInteger(maxSteps) || maxSteps < 1 || maxSteps > 200) throw new TypeError('maxSteps must be between 1 and 200');
  if (!isObject(options.inputSchema) || options.inputSchema['type'] !== 'object') {
    throw new TypeError('inputSchema must be a JSON Schema object with "type": "object"');
  }
  if (!isObject(options.outputSchema)) throw new TypeError('outputSchema must be a JSON Schema object');
  if (typeof options.run !== 'function') throw new TypeError('Provide an async run(input, ctx) function');
  return Object.freeze({
    name, version, description, maxSteps,
    inputSchema: options.inputSchema, outputSchema: options.outputSchema,
    parseInput: options.parseInput, parseOutput: options.parseOutput, run: options.run,
  });
}

export class Redactor {
  readonly #secrets: string[];

  constructor(values: readonly string[]) {
    this.#secrets = [...new Set(values.filter(Boolean).flatMap((value) => [
      value, JSON.stringify(value).slice(1, -1),
    ]))].sort((a, b) => b.length - a.length);
  }

  redact(text: string): string {
    let result = text;
    for (const secret of this.#secrets) result = result.split(secret).join('[REDACTED]');
    return result;
  }
}

/** The run's deadline as the agent sees it; paused while waiting on a person. */
export interface DeadlineClock {
  remainingMs(): number;
  pause(): void;
  resume(): void;
}

export interface ContextParts {
  runId: string;
  agent: string;
  attempt: number;
  maxSteps: number;
  clock: DeadlineClock;
  signal: AbortSignal;
  redactor: Redactor;
  logger: (line: string) => void;
  proxy: ProxyBinding | null;
  approvalsUrl: string | null;
}

const APPROVAL_WAIT_SECONDS = 20;

interface ApprovalAnswer {
  approval?: { id: string; status: string; decided_by: string | null; decided_at: string | null };
  error?: { code?: string };
}

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    const timer = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => { clearTimeout(timer); reject(signal.reason); }, { once: true });
  });
}

// While RunWeave is unreachable (restarting, say) keep trying: the run cannot progress
// without it, and RunWeave ends the run's request (aborting `signal`) if it gives up.
async function approvalCall(url: string, token: string, body: string, signal: AbortSignal): Promise<ApprovalAnswer & { status: number }> {
  for (let failures = 0; ; failures += 1) {
    try {
      const response = await fetch(url, {
        method: 'POST', body, signal,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      });
      const answer = await response.json().catch(() => ({})) as ApprovalAnswer;
      return { ...answer, status: response.status };
    } catch {
      if (signal.aborted) throw signal.reason;
      await pause(Math.min(2000, 200 * 2 ** Math.min(failures, 4)), signal);
    }
  }
}

async function requestApproval(parts: ContextParts, action: string, args: unknown, expiresIn: number | undefined): Promise<Approval> {
  if (typeof action !== 'string' || !ACTION_PATTERN.test(action)) {
    throw new TypeError('Action must match ^[a-z][a-z0-9_.:-]{0,99}$, for example issue_refund');
  }
  if (expiresIn !== undefined && !(typeof expiresIn === 'number' && expiresIn > 0)) {
    throw new TypeError('expiresIn must be a positive number of seconds');
  }
  const body = JSON.stringify({ action, arguments: args ?? null, expires_in: expiresIn ?? null, wait_seconds: APPROVAL_WAIT_SECONDS });
  if (body.length > 70_000) throw new TypeError('Approval arguments are limited to 64 KiB');
  if (!parts.approvalsUrl || !parts.proxy) throw new ApprovalUnavailable();
  parts.clock.pause();
  try {
    for (;;) {
      const answer = await approvalCall(parts.approvalsUrl, parts.proxy.token, body, parts.signal);
      if (answer.status !== 200 || !answer.approval) {
        const code = answer.error?.code;
        throw code === 'DURABLE_APPROVAL_REQUIRED' ? new ApprovalUnavailable()
          : new AgentError(typeof code === 'string' ? code : 'APPROVAL_CALLBACK_FAILED');
      }
      const { approval } = answer;
      if (approval.status === 'pending') continue;
      if (approval.status === 'approved' || approval.status === 'rejected') {
        return Object.freeze({
          id: approval.id, status: approval.status, approved: approval.status === 'approved',
          decidedBy: approval.decided_by ?? null, decidedAt: approval.decided_at ?? null,
        });
      }
      throw approval.status === 'expired' ? new ApprovalExpired() : new AgentError('APPROVAL_CANCELLED');
    }
  } finally {
    parts.clock.resume();
  }
}

function modelProxy(binding: ProxyBinding | null): ModelProxy | null {
  if (!binding) return null;
  const proxy = {
    token: binding.token,
    openaiBaseUrl: binding.openai_base_url,
    anthropicBaseUrl: binding.anthropic_base_url,
    env: () => ({
      OPENAI_BASE_URL: binding.openai_base_url, OPENAI_API_KEY: binding.token,
      ANTHROPIC_BASE_URL: binding.anthropic_base_url, ANTHROPIC_API_KEY: binding.token,
    }),
  };
  Object.defineProperty(proxy, 'env', { enumerable: false });
  return Object.freeze(proxy);
}

export function createContext(parts: ContextParts): { context: RunContext; steps: StepRecord[] } {
  const steps: StepRecord[] = [];
  let started = 0;
  const remainingMs = () => parts.clock.remainingMs();
  const context: RunContext = {
    runId: parts.runId,
    agent: parts.agent,
    attempt: parts.attempt,
    signal: parts.signal,
    proxy: modelProxy(parts.proxy),
    remainingMs,
    remainingSeconds: () => remainingMs() / 1000,
    async step(title, fn, options = {}) {
      if (started >= parts.maxSteps) throw new StepLimitError();
      started += 1;
      const record: StepRecord = {
        title: clip(parts.redactor.redact(String(title)), TITLE_LIMIT) || 'Step',
        detail: options.detail === undefined ? null : clip(parts.redactor.redact(String(options.detail)), DETAIL_LIMIT) || null,
        status: 'failed',
        duration_ms: 0,
      };
      if (steps.length < STEP_EVENT_CAP) steps.push(record);
      const begin = performance.now();
      try {
        const result = await fn();
        record.status = 'succeeded';
        return result;
      } finally {
        record.duration_ms = Math.max(0, performance.now() - begin);
      }
    },
    log(message) {
      parts.logger(JSON.stringify({
        level: 'info', logger: 'runweave.agent', run_id: parts.runId, agent: parts.agent,
        message: parts.redactor.redact(String(message)).slice(0, 1000),
      }));
    },
    usage: () => null,
    requestApproval: (action, args, options = {}) => requestApproval(parts, action, args, options.expiresIn),
  };
  return { context, steps };
}
