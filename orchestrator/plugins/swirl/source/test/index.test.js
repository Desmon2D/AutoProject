import assert from "node:assert/strict";
import test from "node:test";

import { normalizeSearch } from "../index.js";

test("normalizes and bounds SWIRL result groups", () => {
  const result = normalizeSearch(
    {
      id: 42,
      results: [
        {
          searchprovider: "Confluence",
          json_results: [
            { title: "Runbook", body: "  Safe   excerpt  ", url: "https://kb/runbook", score: "0.9" },
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
      snippet: "Safe excerpt",
      url: "https://kb/runbook",
      source: "Confluence",
      updated_at: null,
      score: 0.9,
    },
  ]);
});
