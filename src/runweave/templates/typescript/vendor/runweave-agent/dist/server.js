import { createServer } from 'node:http';
import { AgentError, InputError, Redactor, RunAborted, createContext, } from './agent.js';
import { CONTRACT_VERSION, MANIFEST_PATH, isObject, isUuid, parseRunRequest, } from './contract.js';
const RUN_ROUTE = /^\/runweave\/v1\/agents\/([^/]+)\/runs$/;
const CANCEL_ROUTE = /^\/runweave\/v1\/agents\/([^/]+)\/runs\/([^/]+)\/cancel$/;
function failure(status, code, extra = {}) {
    return { status, body: { error: { code, ...extra } } };
}
function send(response, { status, body }) {
    if (response.headersSent || response.destroyed)
        return;
    const text = JSON.stringify(body);
    const headers = {
        'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(text), 'Cache-Control': 'no-store',
    };
    if (status === 503)
        headers['Retry-After'] = '1';
    response.writeHead(status, headers).end(text);
}
function readBody(request, limit) {
    const declared = Number(request.headers['content-length']);
    if (Number.isFinite(declared) && declared > limit) {
        request.resume();
        return Promise.resolve(null);
    }
    return new Promise((resolve, reject) => {
        const chunks = [];
        let size = 0;
        let done = false;
        request.on('data', (chunk) => {
            if (done)
                return;
            size += chunk.length;
            if (size > limit) {
                done = true;
                resolve(null);
                return;
            }
            chunks.push(chunk);
        });
        request.on('end', () => { if (!done) {
            done = true;
            resolve(Buffer.concat(chunks));
        } });
        request.on('error', (error) => { if (!done) {
            done = true;
            reject(error);
        } });
    });
}
class AgentServer {
    #agents = new Map();
    #running = new Map();
    #done = new Map();
    #maxConcurrency;
    #maxBody;
    #cacheSize;
    #cacheTtl;
    #redactor;
    #logger;
    #anonymous = 0;
    constructor(agents, options) {
        for (const agent of agents) {
            if (this.#agents.has(agent.name))
                throw new TypeError(`Agent name "${agent.name}" is served more than once`);
            this.#agents.set(agent.name, agent);
        }
        const { maxConcurrency = 32, maxBodyBytes = 1_048_576 } = options;
        if (!Number.isInteger(maxConcurrency) || maxConcurrency < 1 || maxConcurrency > 1024) {
            throw new RangeError('maxConcurrency must be between 1 and 1024');
        }
        this.#maxConcurrency = maxConcurrency;
        this.#maxBody = maxBodyBytes;
        this.#cacheSize = options.idempotencyCacheSize ?? 256;
        this.#cacheTtl = options.idempotencyTtlMs ?? 600_000;
        this.#redactor = new Redactor(options.protectedValues ?? []);
        this.#logger = options.logger ?? ((line) => process.stderr.write(`${line}\n`));
    }
    handle = (request, response) => {
        this.#route(request, response).catch((error) => {
            this.#logger(JSON.stringify({ level: 'error', logger: 'runweave.agent', message: 'Request failed', error: String(error) }));
            send(response, failure(500, 'RUN_FAILED'));
        });
    };
    async #route(request, response) {
        const path = new URL(request.url ?? '/', 'http://agent').pathname;
        if (request.method === 'GET' && path === MANIFEST_PATH) {
            request.resume();
            send(response, { status: 200, body: this.#manifest() });
            return;
        }
        const cancel = request.method === 'POST' ? CANCEL_ROUTE.exec(path) : null;
        if (cancel) {
            request.resume();
            send(response, this.#cancel(decodeURIComponent(cancel[1] ?? ''), cancel[2] ?? ''));
            return;
        }
        const run = request.method === 'POST' ? RUN_ROUTE.exec(path) : null;
        if (!run) {
            request.resume();
            send(response, failure(404, 'NOT_FOUND'));
            return;
        }
        const agent = this.#agents.get(decodeURIComponent(run[1] ?? ''));
        if (!agent) {
            request.resume();
            send(response, failure(404, 'UNKNOWN_AGENT'));
            return;
        }
        const body = await readBody(request, this.#maxBody);
        if (body === null) {
            send(response, failure(413, 'INPUT_TOO_LARGE'));
            return;
        }
        let envelope = null;
        try {
            envelope = parseRunRequest(JSON.parse(body.toString('utf8')));
        }
        catch { /* Malformed JSON is an invalid request. */ }
        if (!envelope) {
            send(response, failure(400, 'INVALID_REQUEST'));
            return;
        }
        let input = envelope.input;
        if (agent.parseInput) {
            try {
                input = agent.parseInput(envelope.input);
            }
            catch (error) {
                send(response, error instanceof InputError
                    ? failure(422, 'INVALID_INPUT', { errors: error.errors })
                    : failure(422, 'INVALID_INPUT'));
                return;
            }
        }
        const header = request.headers['idempotency-key'];
        const key = typeof header === 'string' ? header : undefined;
        if (key !== undefined && (key.length === 0 || key.length > 200 || /[^\x20-\x7e]/.test(key))) {
            send(response, failure(400, 'INVALID_REQUEST'));
            return;
        }
        this.#prune();
        if (key !== undefined) {
            const done = this.#done.get(key);
            if (done) {
                send(response, done.name === agent.name && done.runId === envelope.run_id
                    ? done.outcome : failure(409, 'IDEMPOTENCY_CONFLICT'));
                return;
            }
        }
        let execution = key === undefined ? undefined : this.#running.get(key);
        if (execution && (execution.name !== agent.name || execution.runId !== envelope.run_id)) {
            send(response, failure(409, 'IDEMPOTENCY_CONFLICT'));
            return;
        }
        if (!execution) {
            if (this.#running.size >= this.#maxConcurrency) {
                send(response, failure(503, 'AGENT_BUSY'));
                return;
            }
            execution = this.#start(key ?? `anonymous:${++this.#anonymous}`, key !== undefined, agent, envelope, input);
        }
        await this.#wait(response, execution);
    }
    #manifest() {
        return {
            contract: CONTRACT_VERSION,
            agents: [...this.#agents.values()].map((agent) => ({
                name: agent.name, version: agent.version, description: agent.description,
                input_schema: agent.inputSchema, output_schema: agent.outputSchema, max_steps: agent.maxSteps,
            })),
        };
    }
    #cancel(name, runId) {
        if (!isUuid(runId))
            return failure(404, 'UNKNOWN_RUN');
        let found = false;
        for (const execution of this.#running.values()) {
            if (execution.name === name && execution.runId === runId.toLowerCase()) {
                execution.controller.abort(new RunAborted('cancelled'));
                found = true;
            }
        }
        return found ? { status: 202, body: {} } : failure(404, 'UNKNOWN_RUN');
    }
    #start(key, cacheable, agent, envelope, input) {
        const controller = new AbortController();
        const execution = {
            name: agent.name, runId: envelope.run_id, controller, waiters: 0, settled: false,
            result: Promise.resolve(failure(500, 'RUN_FAILED')),
        };
        execution.result = this.#execute(agent, envelope, input, controller).then((outcome) => {
            execution.settled = true;
            this.#running.delete(key);
            if (cacheable && !controller.signal.aborted) {
                this.#done.set(key, { expires: Date.now() + this.#cacheTtl, name: agent.name, runId: envelope.run_id, outcome });
                while (this.#done.size > this.#cacheSize)
                    this.#done.delete(this.#done.keys().next().value);
            }
            return outcome;
        });
        this.#running.set(key, execution);
        return execution;
    }
    async #execute(agent, envelope, input, controller) {
        const clock = deadlineClock(envelope.deadline_ms, () => controller.abort(new RunAborted('deadline')));
        const { context, steps } = createContext({
            runId: envelope.run_id, agent: agent.name, attempt: envelope.attempt, maxSteps: agent.maxSteps,
            clock, signal: controller.signal, redactor: this.#redactor, logger: this.#logger,
            proxy: envelope.proxy, approvalsUrl: envelope.approvals_url, effectFaults: envelope.effect_faults,
        });
        // The run settles when the agent returns or the signal aborts, whichever is first. An agent
        // that ignores ctx.signal keeps running in the background, but its result is discarded.
        const aborted = new Promise((_, reject) => {
            controller.signal.addEventListener('abort', () => reject(controller.signal.reason), { once: true });
        });
        aborted.catch(() => { });
        try {
            const raw = await Promise.race([agent.run(input, context), aborted]);
            let output = raw;
            if (agent.parseOutput) {
                try {
                    output = agent.parseOutput(raw);
                }
                catch {
                    return failure(500, 'AGENT_OUTPUT_INVALID');
                }
            }
            if (!isObject(output))
                return failure(500, 'AGENT_OUTPUT_INVALID');
            return { status: 200, body: { output, steps: steps } };
        }
        catch (error) {
            const reason = controller.signal.aborted ? controller.signal.reason : error;
            if (reason instanceof RunAborted) {
                return reason.reason === 'deadline' ? failure(500, 'AGENT_DEADLINE_EXCEEDED') : failure(409, 'RUN_CANCELLED');
            }
            if (error instanceof AgentError)
                return failure(500, error.code);
            // The stack stays in this process's logs; RunWeave only sees the code.
            this.#logger(JSON.stringify({
                level: 'error', logger: 'runweave.agent', agent: agent.name, run_id: envelope.run_id,
                message: this.#redactor.redact(error instanceof Error ? error.stack ?? error.message : String(error)),
            }));
            return failure(500, 'RUN_FAILED');
        }
        finally {
            clock.stop();
        }
    }
    async #wait(response, execution) {
        execution.waiters += 1;
        const onClose = () => {
            // RunWeave closed the request (cancellation or deadline): stop unless others wait.
            if (!execution.settled && !response.writableEnded && execution.waiters === 1) {
                execution.controller.abort(new RunAborted('cancelled'));
            }
        };
        response.on('close', onClose);
        try {
            send(response, await execution.result);
        }
        finally {
            execution.waiters -= 1;
            response.off('close', onClose);
        }
    }
    #prune() {
        const now = Date.now();
        for (const [key, entry] of this.#done)
            if (entry.expires <= now)
                this.#done.delete(key);
    }
}
/** A Node request listener serving the agents, to mount in your own server. */
export function createAgentHandler(agents, options = {}) {
    if (agents.length === 0)
        throw new TypeError('Serve at least one agent');
    return new AgentServer(agents, options).handle;
}
const LOOPBACK = new Set(['127.0.0.1', '::1', 'localhost']);
/** Serve agents on loopback. Resolves once listening. */
export async function serveAgents(agents, options = {}) {
    const { host = '127.0.0.1', port = 9100, handler, ...rest } = options;
    if (!LOOPBACK.has(host))
        throw new TypeError('serveAgents binds only to loopback (127.0.0.1, ::1 or localhost)');
    if (!handler && agents.length === 0)
        throw new TypeError('Serve at least one agent');
    const listener = handler ?? createAgentHandler(agents, rest);
    const server = createServer({ requestTimeout: 0, headersTimeout: 30_000 }, listener);
    await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(port, host, () => { server.off('error', reject); resolve(); });
    });
    const address = server.address();
    const shown = address.family === 'IPv6' ? `[${address.address}]` : address.address;
    return {
        url: `http://${shown}:${address.port}`,
        close: () => new Promise((resolve) => {
            server.closeAllConnections();
            server.close(() => resolve());
        }),
    };
}
/** The run deadline RunWeave sent; it stops while the agent waits on an approval. */
function deadlineClock(deadlineMs, expire) {
    let left = deadlineMs;
    let started = performance.now();
    let timer = setTimeout(expire, left);
    let paused = 0;
    return {
        remainingMs: () => (paused ? left : Math.max(0, left - (performance.now() - started))),
        pause() {
            paused += 1;
            if (paused > 1)
                return;
            left = Math.max(0, left - (performance.now() - started));
            clearTimeout(timer);
            timer = undefined;
        },
        resume() {
            if (paused === 0 || --paused > 0)
                return;
            started = performance.now();
            timer = setTimeout(expire, left);
        },
        stop() { clearTimeout(timer); timer = undefined; },
    };
}
