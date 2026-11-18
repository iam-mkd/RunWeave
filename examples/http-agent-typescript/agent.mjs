// A TypeScript-SDK agent served over HTTP. RunWeave calls it; nothing is imported into RunWeave.
//   npm --prefix ../../sdk/typescript ci && npm --prefix ../../sdk/typescript run build
//   npm install && npm start            -> RUNWEAVE_AGENT_READY http://127.0.0.1:9200
//   runweave serve --demo --http-agent http://127.0.0.1:9200
import { InputError, defineAgent, serveAgents } from '@runweave/agent';

const wordCount = defineAgent({
  name: 'word-count',
  version: '1.0.0',
  description: 'Counts words and finds the longest one. Offline and deterministic.',
  maxSteps: 5,
  inputSchema: {
    type: 'object',
    title: 'WordCountInput',
    properties: { text: { type: 'string', title: 'Text', minLength: 1, maxLength: 5000 } },
    required: ['text'],
    additionalProperties: false,
  },
  outputSchema: {
    type: 'object',
    properties: { words: { type: 'integer' }, longest: { type: 'string' } },
    required: ['words', 'longest'],
  },
  // RunWeave validates against inputSchema first; this is defense in depth.
  parseInput(raw) {
    if (typeof raw.text !== 'string' || raw.text.trim() === '') {
      throw new InputError([{ loc: ['text'], type: 'missing', msg: 'Text is required' }]);
    }
    return { text: raw.text };
  },
  async run(input, ctx) {
    const words = await ctx.step('Split words', () => input.text.split(/\s+/).filter(Boolean));
    const longest = await ctx.step('Find the longest word', () =>
      words.reduce((best, word) => (word.length > best.length ? word : best), ''));
    ctx.log(`counted ${words.length} words`);
    return { words: words.length, longest };
  },
});

const port = Number(process.env.PORT ?? 9200);
const server = await serveAgents([wordCount], { port });
console.log(`RUNWEAVE_AGENT_READY ${server.url}`);
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close().then(() => process.exit(0)));
