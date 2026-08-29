// Fetch-based content index builder — the deployed counterpart to
// truth-button-mcp/src/build-index.js (which reads local HTML files from disk
// for the local stdio PoC). This backend has no filesystem access to the site
// repo, so it fetches live pages from projectsilverbeam.com instead.
//
// Page discovery: llms.txt is already the curated, complete list of the
// site's tool pages (unlike sitemap.xml, which is missing at least one), so
// it doubles as the manifest of which URLs to fetch and parse.

const SITE_ORIGIN = 'https://projectsilverbeam.com';
const LLMS_TXT_URL = `${SITE_ORIGIN}/llms.txt`;

const SKIP_SECTION_HEADINGS = new Set([
  'Frequently asked questions',
  'Also on The Truth Button',
]);

function extractPageUrls(llmsTxt) {
  const urls = [];
  const linkPattern = /^-\s*\[.+?\]\((https?:\/\/[^\s)]+)\)/gm;
  let match;
  while ((match = linkPattern.exec(llmsTxt)) !== null) {
    urls.push(match[1]);
  }
  return urls;
}

function extractFaqs($) {
  let faqs = [];
  $('script[type="application/ld+json"]').each((_, el) => {
    let data;
    try {
      data = JSON.parse($(el).html());
    } catch {
      return;
    }
    if (data['@type'] === 'FAQPage' && Array.isArray(data.mainEntity)) {
      faqs = data.mainEntity.map((item) => ({
        q: (item.name ?? '').trim(),
        a: (item.acceptedAnswer?.text ?? '').trim(),
      }));
    }
  });
  if (faqs.length === 0) {
    $('.faq-item').each((_, el) => {
      faqs.push({
        q: $(el).find('.faq-q').text().trim(),
        a: $(el).find('.faq-a').text().trim(),
      });
    });
  }
  return faqs;
}

function extractSections($) {
  const sections = [];
  $('section').each((_, el) => {
    const h2 = $(el).children('h2').first();
    if (h2.length === 0) return; // e.g. disclaimer block — no heading
    const heading = h2.text().trim();
    if (SKIP_SECTION_HEADINGS.has(heading)) return;
    const text = $(el)
      .clone()
      .find('.faq-item, h2')
      .remove()
      .end()
      .text()
      .replace(/\s+/g, ' ')
      .trim();
    sections.push({ heading, text });
  });
  return sections;
}

async function extractPage(url, load) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Fetch failed for ${url}: ${res.status}`);
  const html = await res.text();
  const $ = load(html);
  const filename = url.replace(SITE_ORIGIN + '/', '');
  return {
    slug: filename.replace(/\.html$/, ''),
    title: $('head > title').text().trim(),
    description: $('meta[name="description"]').attr('content')?.trim() ?? '',
    url,
    sections: extractSections($),
    faqs: extractFaqs($),
  };
}

async function buildIndex() {
  const { load } = await import('cheerio');
  const llmsRes = await fetch(LLMS_TXT_URL);
  if (!llmsRes.ok) throw new Error(`Fetch failed for ${LLMS_TXT_URL}: ${llmsRes.status}`);
  const llmsTxt = await llmsRes.text();
  const urls = extractPageUrls(llmsTxt);
  return Promise.all(urls.map((url) => extractPage(url, load)));
}

module.exports = { buildIndex };
