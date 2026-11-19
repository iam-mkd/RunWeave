// A small agent to start from, served over the RunWeave HTTP agent contract.
import { defineAgent, serveAgents } from '@runweave/agent';

const wordCount = defineAgent({
  name: 'word-count',
  version: '0.1.0',
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
  async run(input, ctx) {
    const words = await ctx.step('Split words', () => input.text.split(/\s+/).filter(Boolean));
    const longest = await ctx.step('Find the longest word', () =>
      words.reduce((best, word) => (word.length > best.length ? word : best), ''));
    // To call a model, pass ctx.proxy to the OpenAI or Anthropic SDK.
    return { words: words.length, longest };
  },
});

// Port 0 picks a free port; runweave eval reads the address from this line.
const server = await serveAgents([wordCount], { port: Number(process.env.PORT ?? 0) });
console.log(`RUNWEAVE_AGENT_READY ${server.url}`);
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close().then(() => process.exit(0)));
