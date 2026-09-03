import assert from "node:assert/strict";
import test from "node:test";

import { apply } from "../index.js";

function context() {
  const tools = [];
  const sections = [];
  return {
    tools,
    sections,
    value: {
      tools: { register: (tool) => tools.push(tool) },
      systemPrompt: { section: (section) => sections.push(section) },
    },
  };
}

test("rewrites repository clone URL for the agent network", async () => {
  process.env.TEST_GITEA_URL = "http://gitea.internal:3000/";
  process.env.TEST_GITEA_TOKEN = "secret-token";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(
    JSON.stringify({ clone_url: "http://localhost:3000/team/service.git", default_branch: "main" }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
  try {
    const ctx = context();
    apply(ctx.value, { baseUrlEnv: "TEST_GITEA_URL", tokenEnv: "TEST_GITEA_TOKEN" });
    const getRepository = ctx.tools.find((tool) => tool.name === "gitea_get_repository");
    const result = await getRepository.execute(
      { owner: "team", repository: "service" },
      { signal: undefined },
    );
    assert.equal(result.clone_url, "http://gitea.internal:3000/team/service.git");
    assert.equal(result.automation_git_url, "http://gitea.internal:3000/team/service.git");
  } finally {
    globalThis.fetch = originalFetch;
    delete process.env.TEST_GITEA_URL;
    delete process.env.TEST_GITEA_TOKEN;
  }
});

test("registers Gitea tools and performs authenticated API requests", async () => {
  process.env.TEST_GITEA_URL = "https://gitea.example.test/";
  process.env.TEST_GITEA_TOKEN = "secret-token";
  process.env.GITEA_ALLOWED_REPOSITORIES = "team/service";
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/pulls?state=open&limit=100")) {
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (options.method === "GET") {
      return new Response(JSON.stringify({ message: "not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ id: 7, title: "Agent changes" }), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const ctx = context();
    apply(ctx.value, { baseUrlEnv: "TEST_GITEA_URL", tokenEnv: "TEST_GITEA_TOKEN" });
    assert.deepEqual(
      ctx.tools.map((tool) => tool.name),
      [
        "gitea_get_repository",
        "gitea_get_pull_request",
        "gitea_create_pull_request",
        "gitea_comment_pull_request",
      ],
    );
    const create = ctx.tools.find((tool) => tool.name === "gitea_create_pull_request");
    const result = await create.execute(
      {
        owner: "team",
        repository: "service",
        title: "Agent changes",
        head: "automation/wf-123",
        base: "main",
        body: "Ready for review",
        workflow_id: "wf-123",
        idempotency_key: "wf-123-create-pr-1",
      },
      { signal: undefined },
    );
    assert.equal(result.id, 7);
    assert.equal(
      calls[0].url,
      "https://gitea.example.test/api/v1/repos/team/service/pulls?state=open&limit=100",
    );
    assert.equal(
      calls[1].url,
      "https://gitea.example.test/api/v1/repos/team/service/pulls/main/automation%2Fwf-123",
    );
    assert.equal(calls[2].url, "https://gitea.example.test/api/v1/repos/team/service/pulls");
    assert.equal(calls[2].options.method, "POST");
    assert.equal(calls[2].options.headers.Authorization, "token secret-token");
    const body = JSON.parse(calls[2].options.body);
    assert.deepEqual({ ...body, body: undefined }, {
      title: "Agent changes",
      head: "automation/wf-123",
      base: "main",
      body: undefined,
    });
    assert.match(body.body, /automation-workflow: wf-123/u);
    assert.match(body.body, /automation-idempotency-key: wf-123-create-pr-1/u);
  } finally {
    globalThis.fetch = originalFetch;
    delete process.env.TEST_GITEA_URL;
    delete process.env.TEST_GITEA_TOKEN;
    delete process.env.GITEA_ALLOWED_REPOSITORIES;
  }
});

test("reuses a pull request with the same idempotency key", async () => {
  process.env.TEST_GITEA_URL = "https://gitea.example.test";
  process.env.TEST_GITEA_TOKEN = "secret-token";
  process.env.GITEA_ALLOWED_REPOSITORIES = "team/service";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(
    JSON.stringify([{
      id: 9,
      body: "<!-- automation-idempotency-key: wf-123-create-pr-1 -->",
    }]),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
  try {
    const ctx = context();
    apply(ctx.value, { baseUrlEnv: "TEST_GITEA_URL", tokenEnv: "TEST_GITEA_TOKEN" });
    const create = ctx.tools.find((tool) => tool.name === "gitea_create_pull_request");
    const result = await create.execute(
      {
        owner: "team",
        repository: "service",
        title: "Agent changes",
        head: "automation/wf-123",
        base: "main",
        workflow_id: "wf-123",
        idempotency_key: "wf-123-create-pr-1",
      },
      { signal: undefined },
    );
    assert.equal(result.id, 9);
    assert.equal(result.automation_reused, true);
  } finally {
    globalThis.fetch = originalFetch;
    delete process.env.TEST_GITEA_URL;
    delete process.env.TEST_GITEA_TOKEN;
    delete process.env.GITEA_ALLOWED_REPOSITORIES;
  }
});

test("rejects a pull request from a non-workflow branch", async () => {
  process.env.TEST_GITEA_URL = "https://gitea.example.test";
  process.env.TEST_GITEA_TOKEN = "secret-token";
  process.env.GITEA_ALLOWED_REPOSITORIES = "team/service";
  const ctx = context();
  try {
    apply(ctx.value, { baseUrlEnv: "TEST_GITEA_URL", tokenEnv: "TEST_GITEA_TOKEN" });
    const create = ctx.tools.find((tool) => tool.name === "gitea_create_pull_request");
    await assert.rejects(
      create.execute(
        {
          owner: "team",
          repository: "service",
          title: "Agent changes",
          head: "automation/wf-123-fix",
          base: "main",
          workflow_id: "wf-123",
          idempotency_key: "wf-123-create-pr-1",
        },
        { signal: undefined },
      ),
      /head must be the stable workflow branch automation\/wf-123/u,
    );
  } finally {
    delete process.env.TEST_GITEA_URL;
    delete process.env.TEST_GITEA_TOKEN;
    delete process.env.GITEA_ALLOWED_REPOSITORIES;
  }
});

test("reuses a pull request comment with the same idempotency key", async () => {
  process.env.TEST_GITEA_URL = "https://gitea.example.test";
  process.env.TEST_GITEA_TOKEN = "secret-token";
  process.env.GITEA_ALLOWED_REPOSITORIES = "team/service";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(
    JSON.stringify([
      {
        id: 11,
        body: "Already posted\n<!-- automation-idempotency-key: wf-123-comment-1 -->",
      },
    ]),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
  try {
    const ctx = context();
    apply(ctx.value, { baseUrlEnv: "TEST_GITEA_URL", tokenEnv: "TEST_GITEA_TOKEN" });
    const comment = ctx.tools.find((tool) => tool.name === "gitea_comment_pull_request");
    const result = await comment.execute(
      {
        owner: "team",
        repository: "service",
        index: 7,
        body: "Already posted",
        workflow_id: "wf-123",
        idempotency_key: "wf-123-comment-1",
      },
      { signal: undefined },
    );
    assert.equal(result.id, 11);
    assert.equal(result.automation_reused, true);
  } finally {
    globalThis.fetch = originalFetch;
    delete process.env.TEST_GITEA_URL;
    delete process.env.TEST_GITEA_TOKEN;
    delete process.env.GITEA_ALLOWED_REPOSITORIES;
  }
});

test("rejects missing credentials without registering tools", () => {
  delete process.env.TEST_GITEA_URL;
  delete process.env.TEST_GITEA_TOKEN;
  assert.throws(
    () => apply(context().value, { baseUrlEnv: "TEST_GITEA_URL", tokenEnv: "TEST_GITEA_TOKEN" }),
    /base URL is missing/u,
  );
});
