import assert from "node:assert/strict";
import test from "node:test";

import { apply, fetchDocument, normalizeSearch } from "../index.js";

test("normalizes and bounds SWIRL result groups", () => {
  const result = normalizeSearch(
    {
      id: 42,
      results: [
        {
          searchprovider: "Confluence",
          json_results: [
            { title: "<em>Runbook</em>", body: "  Safe strong <em>excerpt</em> strong &amp; guidance  ", url: "https://kb/runbook", score: "0.9" },
            { title: "Ignored", body: "extra", url: "https://kb/extra" },
          ],
        },
      ],
    },
    "deploy",
    1,
  );

  assert.equal(result.search_id, "42");
  assert.deepEqual(result.results, [
    {
      title: "Runbook",
      snippet: "Safe excerpt & guidance",
      url: "https://kb/runbook",
      source: "Confluence",
      document_id: null,
      updated_at: null,
      score: 0.9,
    },
  ]);
});

test("fetches full content through SWIRL and enforces the configured origin", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return new Response(JSON.stringify({ markdown: "# Full document\n\nBody." }), { status: 200 });
  };
  try {
    const result = await fetchDocument(
      {
        baseUrl: "http://swirl:8000",
        username: "agent",
        password: "secret",
        allowedContentOrigins: new Set(["http://bookstack"]),
        routes: new Map([["local bookstack", {
          providerId: 47,
          urlTemplate: "http://bookstack/api/pages/{id}",
          contentPath: "markdown",
          contentFormat: "markdown",
        }]]),
      },
      { source: "Local BookStack", document_id: "17", max_characters: 1000 },
    );
    assert.equal(result.content, "# Full document\n\nBody.");
    assert.equal(result.content_format, "markdown");
    assert.equal(calls.length, 1);
    assert.ok(calls[0].startsWith("http://swirl:8000/api/swirl/fetch-document/"));
    assert.equal(new URL(calls[0]).searchParams.get("url"), "http://bookstack/api/pages/17");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("registers search and full-content tools from the sanitized route contract", () => {
  const previous = {
    base: process.env.SWIRL_BASE_URL,
    username: process.env.SWIRL_USERNAME,
    password: process.env.SWIRL_PASSWORD,
    origins: process.env.SWIRL_CONTENT_ALLOWED_ORIGINS,
    routes: process.env.SWIRL_CONTENT_ROUTES_JSON,
  };
  process.env.SWIRL_BASE_URL = "http://swirl:8000";
  process.env.SWIRL_USERNAME = "agent";
  process.env.SWIRL_PASSWORD = "secret";
  process.env.SWIRL_CONTENT_ALLOWED_ORIGINS = "http://bookstack";
  process.env.SWIRL_CONTENT_ROUTES_JSON = JSON.stringify([{
    source: "Local BookStack",
    provider_id: 47,
    url_template: "http://bookstack/api/pages/{id}",
    content_path: "markdown",
    format: "markdown",
  }]);
  const names = [];
  try {
    apply({
      systemPrompt: { section() {} },
      tools: { register(tool) { names.push(tool.name); } },
    });
    assert.deepEqual(names, ["swirl_search", "swirl_fetch_document"]);
  } finally {
    for (const [name, value] of Object.entries({
      SWIRL_BASE_URL: previous.base,
      SWIRL_USERNAME: previous.username,
      SWIRL_PASSWORD: previous.password,
      SWIRL_CONTENT_ALLOWED_ORIGINS: previous.origins,
      SWIRL_CONTENT_ROUTES_JSON: previous.routes,
    })) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
});
