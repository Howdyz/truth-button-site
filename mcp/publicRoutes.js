// Public MCP (Model Context Protocol) HTTP endpoint — lets an AI agent query
// The Truth Button's content directly (search_content / get_page) instead of
// scraping the DOM. Mount directly in server.js: app.use(require('./mcp/publicRoutes'))
// — and mount it BEFORE the global express.json() (same reason as the Stripe
// webhook / /api/photos routes above it): the underlying MCP SDK adapter
// (toNodeHandler) needs to read the raw, unparsed request body itself to
// construct a Web-standard Request. No body-parsing middleware runs on this
// route at all, for the same reason.
//
// The MCP server SDK (@modelcontextprotocol/server, @modelcontextprotocol/node)
// and its schema library (zod) are ESM-only; this file stays CommonJS (like
// the rest of the backend) and bridges via a single cached dynamic import().
//
// Requires: npm install cheerio zod @modelcontextprotocol/server @modelcontextprotocol/node

const express = require('express');
const rateLimit = require('express-rate-limit');
const { buildIndex } = require('./build-index');
const { searchContent } = require('./search');

const router = express.Router();

const mcpLimiter = rateLimit({
  windowMs: 60000,
  max: 60,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many requests. Slow down a bit.' },
});

// In-memory only (no Redis) — rebuilding means 18 small fetches, cheap enough
// that a shared cross-restart cache isn't worth the extra complexity here.
const CACHE_TTL_MS = 60 * 60 * 1000; // 1 hour
let cachedIndex = null;
let cachedAt = 0;

async function getIndex() {
  const now = Date.now();
  if (cachedIndex && now - cachedAt < CACHE_TTL_MS) return cachedIndex;
  cachedIndex = await buildIndex();
  cachedAt = now;
  return cachedIndex;
}

function createServerFactory(McpServer, z) {
  return function createServer() {
    const server = new McpServer({ name: 'truth-button-content', version: '1.0.0' });

    server.registerTool(
      'search_content',
      {
        title: 'Search Truth Button content',
        description:
          'Keyword search across all The Truth Button tool pages (titles, descriptions, sections, FAQs). Returns ranked results with a url, title, and snippet.',
        inputSchema: z.object({ query: z.string().min(1) }),
      },
      async ({ query }) => {
        const pages = await getIndex();
        const results = searchContent(pages, query);
        if (results.length === 0) {
          const available = pages.map((p) => `${p.slug} — ${p.title}`).join('\n');
          return {
            content: [{ type: 'text', text: `No matches for "${query}". Available pages:\n${available}` }],
          };
        }
        return { content: [{ type: 'text', text: JSON.stringify(results, null, 2) }] };
      }
    );

    server.registerTool(
      'get_page',
      {
        title: 'Get Truth Button page content',
        description: 'Full structured content (title, description, sections, FAQs) for one page, by slug.',
        inputSchema: z.object({ slug: z.string().min(1) }),
      },
      async ({ slug }) => {
        const pages = await getIndex();
        const page = pages.find((p) => p.slug === slug);
        if (!page) {
          const validSlugs = pages.map((p) => p.slug).join(', ');
          return {
            content: [{ type: 'text', text: `Unknown slug "${slug}". Valid slugs: ${validSlugs}` }],
            isError: true,
          };
        }
        return { content: [{ type: 'text', text: JSON.stringify(page, null, 2) }] };
      }
    );

    return server;
  };
}

let nodeHandlerPromise = null;
function getNodeHandler() {
  if (!nodeHandlerPromise) {
    nodeHandlerPromise = (async () => {
      const { McpServer, createMcpHandler } = await import('@modelcontextprotocol/server');
      const { toNodeHandler } = await import('@modelcontextprotocol/node');
      const z = await import('zod/v4');

      const handler = createMcpHandler(createServerFactory(McpServer, z), {
        legacy: 'stateless',
        responseMode: 'json',
      });
      return toNodeHandler(handler);
    })();
  }
  return nodeHandlerPromise;
}

router.post('/mcp', mcpLimiter, async (req, res, next) => {
  try {
    const nodeHandler = await getNodeHandler();
    await nodeHandler(req, res);
  } catch (err) {
    next(err);
  }
});

module.exports = router;
