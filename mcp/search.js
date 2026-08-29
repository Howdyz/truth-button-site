// Ranking/search logic — ported verbatim from truth-button-mcp/src/search.js
// (the local stdio PoC), converted from ESM to CommonJS since it has no
// ESM-only dependencies of its own and the rest of this backend is CJS.

const WEIGHTS = {
  title: 5,
  description: 3,
  sectionHeading: 2,
  sectionText: 1,
  faqQ: 3,
  faqA: 1.5,
};
const EXACT_PHRASE_BONUS = 10;
const MAX_RESULTS = 5;
const SNIPPET_LENGTH = 200;

function tokenize(query) {
  return [...new Set(
    query
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter((t) => t.length >= 2)
  )];
}

function countOccurrences(text, token) {
  if (!text) return 0;
  return text.toLowerCase().split(token).length - 1;
}

function scorePage(page, tokens, rawQuery) {
  let score = 0;
  for (const tok of tokens) {
    score += countOccurrences(page.title, tok) * WEIGHTS.title;
    score += countOccurrences(page.description, tok) * WEIGHTS.description;
    for (const sec of page.sections) {
      score += countOccurrences(sec.heading, tok) * WEIGHTS.sectionHeading;
      score += countOccurrences(sec.text, tok) * WEIGHTS.sectionText;
    }
    for (const f of page.faqs) {
      score += countOccurrences(f.q, tok) * WEIGHTS.faqQ;
      score += countOccurrences(f.a, tok) * WEIGHTS.faqA;
    }
  }
  const phrase = rawQuery.toLowerCase().trim();
  if (
    phrase &&
    (page.title.toLowerCase().includes(phrase) || page.description.toLowerCase().includes(phrase))
  ) {
    score += EXACT_PHRASE_BONUS;
  }
  return score;
}

function buildSnippet(page, tokens) {
  const truncate = (text) =>
    text.length > SNIPPET_LENGTH ? `${text.slice(0, SNIPPET_LENGTH)}…` : text;
  for (const f of page.faqs) {
    if (tokens.some((tok) => f.a.toLowerCase().includes(tok) || f.q.toLowerCase().includes(tok))) {
      return truncate(f.a || f.q);
    }
  }
  for (const sec of page.sections) {
    if (tokens.some((tok) => sec.text.toLowerCase().includes(tok))) {
      return truncate(sec.text);
    }
  }
  return truncate(page.description);
}

function searchContent(pages, query) {
  const tokens = tokenize(query);
  const scored = pages
    .map((page) => ({ page, score: scorePage(page, tokens, query) }))
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, MAX_RESULTS);

  return scored.map(({ page }) => ({
    url: page.url,
    title: page.title,
    snippet: buildSnippet(page, tokens),
  }));
}

module.exports = { searchContent };
