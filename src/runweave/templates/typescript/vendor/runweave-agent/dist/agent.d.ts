import { type ErrorLocation, type JsonObject, type JsonSchema, type ProxyBinding, type StepRecord } from './contract.js';
/** A classified failure whose code RunWeave may show in run snapshots. */
export declare class AgentError extends Error {
    readonly code: string;
    constructor(code?: string);
}
export declare class StepLimitError extends AgentError {
    constructor();
}
/** Throw from `parseInput` to reject input with 422. Never include submitted values. */
export declare class InputError extends Error {
    readonly errors: ErrorLocation[];
    constructor(errors: ErrorLocation[]);
}
/** The reason on `ctx.signal` when RunWeave cancels the run or its deadline passes. */
export declare class RunAborted extends Error {
    readonly reason: 'cancelled' | 'deadline';
    constructor(reason: 'cancelled' | 'deadline');
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
    step<T>(title: string, fn: () => Promise<T> | T, options?: {
        detail?: string;
    }): Promise<T>;
    /** Logs with protected values redacted, at most 1,000 characters. */
    log(message: string): void;
    /** Point your OpenAI or Anthropic SDK here; null when RunWeave sent no proxy binding. */
    readonly proxy: ModelProxy | null;
    /** Model usage is metered by RunWeave and shown on the run; not readable here yet. */
    usage(): null;
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
export declare function defineAgent<I extends JsonObject = JsonObject, O extends JsonObject = JsonObject>(options: AgentOptions<I, O>): AgentDefinition<I, O>;
export declare class Redactor {
    #private;
    constructor(values: readonly string[]);
    redact(text: string): string;
}
export interface ContextParts {
    runId: string;
    agent: string;
    attempt: number;
    maxSteps: number;
    expiresAt: number;
    signal: AbortSignal;
    redactor: Redactor;
    logger: (line: string) => void;
    proxy: ProxyBinding | null;
}
export declare function createContext(parts: ContextParts): {
    context: RunContext;
    steps: StepRecord[];
};
