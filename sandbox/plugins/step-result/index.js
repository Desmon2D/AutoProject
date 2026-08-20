import { randomUUID } from "node:crypto";
import { mkdir, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";

import { defineTool } from "@deepseek-ai/dsh-tools";

export const name = "automation-step-result";
export const inject = ["tools", "systemPrompt"];

const OUTCOMES = ["SUCCESS", "FAILURE"];
const DEFAULT_OUTPUT_PATH = "/output/agent-result.json";
const DEFAULT_MAX_SUMMARY_LENGTH = 4000;

function normalizeConfig(config = {}) {
  const outputPath = config.outputPath ?? DEFAULT_OUTPUT_PATH;
  const maxSummaryLength = config.maxSummaryLength ?? DEFAULT_MAX_SUMMARY_LENGTH;
  if (typeof outputPath !== "string" || !path.isAbsolute(outputPath)) {
    throw new Error("step-result outputPath must be an absolute path");
  }
  if (!Number.isInteger(maxSummaryLength) || maxSummaryLength < 1) {
    throw new Error("step-result maxSummaryLength must be a positive integer");
  }
  return { outputPath, maxSummaryLength };
}

function normalizeArtifacts(artifacts = []) {
  return artifacts.map((artifact, index) => {
    const type = artifact.type.trim();
    const uri = artifact.uri.trim();
    if (!type || !uri) {
      throw new Error(`artifact ${index} must contain non-empty type and uri`);
    }
    const result = { type, uri };
    if (artifact.summary?.trim()) {
      result.summary = artifact.summary.trim();
    }
    return result;
  });
}

async function writeAtomic(outputPath, value, signal) {
  await mkdir(path.dirname(outputPath), { recursive: true });
  const temporary = `${outputPath}.${process.pid}.${randomUUID()}.tmp`;
  try {
    await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: "utf8",
      signal,
    });
    await rename(temporary, outputPath);
  } catch (error) {
    await rm(temporary, { force: true }).catch(() => undefined);
    throw error;
  }
}

export function apply(ctx, config = {}) {
  const { outputPath, maxSummaryLength } = normalizeConfig(config);
  let submitted = false;

  ctx.systemPrompt.section({
    name: "tool:submit-step-result",
    order: 119,
    text:
      "Before finishing this Agent Step, you MUST call submit_step_result exactly once. " +
      "Use SUCCESS only when the requested business result was achieved. Use FAILURE when " +
      "the work completed normally but the requested result could not be achieved, and explain " +
      "why. Do not use FAILURE to hide transient tool, provider, or runtime errors. Include only " +
      "small structured data and references to artifacts; do not place full logs in the result.",
  });

  ctx.tools.register(
    defineTool({
      name: "submit_step_result",
      description:
        "Submit the mandatory final business result of this Agent Step. Call exactly once after " +
        "all work and verification are complete. SUCCESS means the requested result was achieved; " +
        "FAILURE means execution completed but the business goal was not achieved.",
      parameters: {
        outcome: {
          type: "string",
          required: true,
          enum: [...OUTCOMES],
          description: "Business outcome: SUCCESS or FAILURE.",
        },
        summary: {
          type: "string",
          required: true,
          description: "Concise factual result summary.",
        },
        data: {
          type: "object",
          additionalProperties: true,
          description: "Small structured facts needed by the orchestrator or next step.",
        },
        artifacts: {
          type: "array",
          description: "References to files or external entities produced by this step.",
          items: {
            type: "object",
            additionalProperties: false,
            properties: {
              type: { type: "string", required: true },
              uri: { type: "string", required: true },
              summary: { type: "string" },
            },
          },
        },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: false,
          properties: {
            accepted: { type: "boolean", required: true },
            outcome: { type: "string", required: true, enum: [...OUTCOMES] },
          },
        },
        render: (_args, value) => [
          {
            type: "text",
            text: `Agent Step result accepted with outcome ${value.outcome}.`,
          },
        ],
      },
      async execute(args, exec) {
        if (submitted) {
          throw new Error("Agent Step result has already been submitted");
        }
        const summary = args.summary.trim();
        if (!summary) {
          throw new Error("step result summary must not be empty");
        }
        if (summary.length > maxSummaryLength) {
          throw new Error(`step result summary exceeds ${maxSummaryLength} characters`);
        }
        const result = {
          schema_version: 1,
          outcome: args.outcome,
          summary,
          data: args.data ?? {},
          artifacts: normalizeArtifacts(args.artifacts),
        };
        await writeAtomic(outputPath, result, exec.signal);
        submitted = true;
        return { accepted: true, outcome: result.outcome };
      },
      presentCall: (args) => ({
        card: "generic",
        title: `Submit Agent Step result: ${args.outcome}`,
        kind: "other",
      }),
    }),
  );
}

