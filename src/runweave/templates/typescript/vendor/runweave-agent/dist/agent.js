import { DETAIL_LIMIT, NAME_PATTERN, STEP_EVENT_CAP, TITLE_LIMIT, VERSION_PATTERN, clip, isObject, } from './contract.js';
/** A classified failure whose code RunWeave may show in run snapshots. */
export class AgentError extends Error {
    code;
    constructor(code = 'RUN_FAILED') {
        super(code);
        this.name = 'AgentError';
        this.code = code;
    }
}
export class StepLimitError extends AgentError {
    constructor() { super('AGENT_STEP_LIMIT'); }
}
/** Throw from `parseInput` to reject input with 422. Never include submitted values. */
export class InputError extends Error {
    errors;
    constructor(errors) {
        super('Invalid input');
        this.name = 'InputError';
        this.errors = errors.slice(0, 20);
    }
}
/** The reason on `ctx.signal` when RunWeave cancels the run or its deadline passes. */
export class RunAborted extends Error {
    reason;
    constructor(reason) {
        super(reason === 'deadline' ? 'The run deadline passed' : 'The run was cancelled');
        this.name = 'RunAborted';
        this.reason = reason;
    }
}
export function defineAgent(options) {
    const { name, version, description = '', maxSteps = 50 } = options;
    if (!NAME_PATTERN.test(name))
        throw new TypeError('Agent name: lowercase letters, digits and hyphens (max 63)');
    if (!VERSION_PATTERN.test(version))
        throw new TypeError('Agent version: 1-40 characters of letters, digits, . + _ -');
    if (description.length > 500)
        throw new TypeError('Agent description: at most 500 characters');
    if (!Number.isInteger(maxSteps) || maxSteps < 1 || maxSteps > 200)
        throw new TypeError('maxSteps must be between 1 and 200');
    if (!isObject(options.inputSchema) || options.inputSchema['type'] !== 'object') {
        throw new TypeError('inputSchema must be a JSON Schema object with "type": "object"');
    }
    if (!isObject(options.outputSchema))
        throw new TypeError('outputSchema must be a JSON Schema object');
    if (typeof options.run !== 'function')
        throw new TypeError('Provide an async run(input, ctx) function');
    return Object.freeze({
        name, version, description, maxSteps,
        inputSchema: options.inputSchema, outputSchema: options.outputSchema,
        parseInput: options.parseInput, parseOutput: options.parseOutput, run: options.run,
    });
}
export class Redactor {
    #secrets;
    constructor(values) {
        this.#secrets = [...new Set(values.filter(Boolean).flatMap((value) => [
                value, JSON.stringify(value).slice(1, -1),
            ]))].sort((a, b) => b.length - a.length);
    }
    redact(text) {
        let result = text;
        for (const secret of this.#secrets)
            result = result.split(secret).join('[REDACTED]');
        return result;
    }
}
function modelProxy(binding) {
    if (!binding)
        return null;
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
export function createContext(parts) {
    const steps = [];
    let started = 0;
    const remainingMs = () => Math.max(0, parts.expiresAt - performance.now());
    const context = {
        runId: parts.runId,
        agent: parts.agent,
        attempt: parts.attempt,
        signal: parts.signal,
        proxy: modelProxy(parts.proxy),
        remainingMs,
        remainingSeconds: () => remainingMs() / 1000,
        async step(title, fn, options = {}) {
            if (started >= parts.maxSteps)
                throw new StepLimitError();
            started += 1;
            const record = {
                title: clip(parts.redactor.redact(String(title)), TITLE_LIMIT) || 'Step',
                detail: options.detail === undefined ? null : clip(parts.redactor.redact(String(options.detail)), DETAIL_LIMIT) || null,
                status: 'failed',
                duration_ms: 0,
            };
            if (steps.length < STEP_EVENT_CAP)
                steps.push(record);
            const begin = performance.now();
            try {
                const result = await fn();
                record.status = 'succeeded';
                return result;
            }
            finally {
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
    };
    return { context, steps };
}
