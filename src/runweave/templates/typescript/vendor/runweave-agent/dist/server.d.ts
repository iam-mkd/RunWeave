import { type RequestListener } from 'node:http';
import { type AgentDefinition } from './agent.js';
export interface ServerOptions {
    /** Concurrent runs before answering 503 AGENT_BUSY (RunWeave retries). Default 32. */
    maxConcurrency?: number;
    /** Request body limit in bytes. Default 1 MiB. */
    maxBodyBytes?: number;
    /** Values redacted from step text and logs, such as your API keys. */
    protectedValues?: readonly string[];
    /** Completed results kept per Idempotency-Key. Defaults: 256 results for 10 minutes. */
    idempotencyCacheSize?: number;
    idempotencyTtlMs?: number;
    /** Where ctx.log lines and unexpected errors go. Default: stderr. */
    logger?: (line: string) => void;
}
/** A Node request listener serving the agents, to mount in your own server. */
export declare function createAgentHandler(agents: readonly AgentDefinition<any, any>[], options?: ServerOptions): RequestListener;
export interface ServeOptions extends ServerOptions {
    /** Loopback only. Default 127.0.0.1. */
    host?: string;
    /** 0 picks a free port. Default 9100. */
    port?: number;
    /** Serve a custom listener instead (for example one wrapping createAgentHandler). */
    handler?: RequestListener;
}
export interface RunningServer {
    url: string;
    close(): Promise<void>;
}
/** Serve agents on loopback. Resolves once listening. */
export declare function serveAgents(agents: readonly AgentDefinition<any, any>[], options?: ServeOptions): Promise<RunningServer>;
