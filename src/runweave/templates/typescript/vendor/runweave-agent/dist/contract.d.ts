import { type EffectFaults } from './effects.js';
export declare const CONTRACT_VERSION = "runweave.agent/v1";
export declare const MANIFEST_PATH = "/runweave/v1/agents";
export declare const NAME_PATTERN: RegExp;
export declare const VERSION_PATTERN: RegExp;
export declare const MAX_DEADLINE_MS = 86400000;
export declare const TITLE_LIMIT = 120;
export declare const DETAIL_LIMIT = 1000;
export declare const STEP_EVENT_CAP = 200;
export declare const ACTION_PATTERN: RegExp;
export type JsonObject = {
    [key: string]: unknown;
};
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
export interface ProxyBinding {
    token: string;
    openai_base_url: string;
    anthropic_base_url: string;
}
export interface RunRequest {
    run_id: string;
    attempt: number;
    input: JsonObject;
    deadline_ms: number;
    proxy: ProxyBinding | null;
    /** Where to request approvals (durable RunWeave only). */
    approvals_url: string | null;
    /** The run's effect faults, when its fault profile has any (see ctx.effect). */
    effect_faults: EffectFaults | null;
}
export interface StepRecord {
    title: string;
    detail: string | null;
    status: 'succeeded' | 'failed';
    duration_ms: number;
    /** Effects (ctx.effect) only: the name, and digests of the operation and the resource. */
    kind?: 'step' | 'effect';
    effect?: string;
    operation?: string;
    resource?: string | null;
    fault?: 'response_lost' | null;
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
export declare function isObject(value: unknown): value is JsonObject;
/** Validates the request envelope. Unknown fields are ignored for forward compatibility. */
export declare function parseRunRequest(value: unknown): RunRequest | null;
export declare function isUuid(value: string): boolean;
export declare function clip(text: string, limit: number): string;
