import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { mkdtemp, rm } from "node:fs/promises";

import { apply, inject, name } from "../index.js";

test("registers a native tool and writes one structured result", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "step-result-"));
  const outputPath = path.join(directory, "agent-result.json");
  let tool;
  let section;
  const ctx = {
    systemPrompt: {
      section(value) {
        section = value;
        return () => undefined;
      },
    },
    tools: {
      register(value) {
        tool = value;
        return () => undefined;
      },
    },
  };

  try {
    apply(ctx, { outputPath });
    assert.equal(name, "automation-step-result");
    assert.deepEqual(inject, ["tools", "systemPrompt"]);
    assert.match(section.text, /MUST call submit_step_result/);
    assert.equal(tool.name, "submit_step_result");

    const response = await tool.execute(
      {
        outcome: "FAILURE",
        summary: "  Required repository was unavailable.  ",
        data: { repository: "backend" },
        artifacts: [{ type: "report", uri: "artifact://failure.md" }],
      },
      { signal: new AbortController().signal },
    );
    assert.deepEqual(response, { accepted: true, outcome: "FAILURE" });

    const result = JSON.parse(await readFile(outputPath, "utf8"));
    assert.deepEqual(result, {
      schema_version: 1,
      outcome: "FAILURE",
      summary: "Required repository was unavailable.",
      data: { repository: "backend" },
      artifacts: [{ type: "report", uri: "artifact://failure.md" }],
    });

    await assert.rejects(
      tool.execute(
        { outcome: "SUCCESS", summary: "done" },
        { signal: new AbortController().signal },
      ),
      /already been submitted/,
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

