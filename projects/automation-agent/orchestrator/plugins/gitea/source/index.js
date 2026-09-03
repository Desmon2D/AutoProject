import { defineTool } from "@deepseek-ai/dsh-tools";

export const name = "automation-gitea";
export const inject = ["tools", "systemPrompt"];

const identifier = {
  type: "string",
  required: true,
};

function text(value, field, maxLength = 255) {
  if (typeof value !== "string" || !value.trim() || value.length > maxLength) {
    throw new Error(`${field} must contain 1..${maxLength} characters`);
  }
  return value.trim();
}

function pullIndex(value) {
  if (!Number.isInteger(value) || value < 1) {
    throw new Error("pull request index must be a positive integer");
  }
  return value;
}

function configuration(config = {}) {
  const baseUrlEnv = config.baseUrlEnv ?? "GITEA_BASE_URL";
  const tokenEnv = config.tokenEnv ?? "GITEA_TOKEN";
  const allowedRepositoriesEnv = config.allowedRepositoriesEnv ?? "GITEA_ALLOWED_REPOSITORIES";
  const baseUrl = process.env[baseUrlEnv]?.trim().replace(/\/+$/, "");
  const token = process.env[tokenEnv]?.trim();
  if (!baseUrl || !/^https?:\/\//u.test(baseUrl)) {
    throw new Error(`Gitea base URL is missing or invalid in ${baseUrlEnv}`);
  }
  if (!token) {
    throw new Error(`Gitea token is missing in ${tokenEnv}`);
  }
  const allowedRepositories = new Set(
    String(process.env[allowedRepositoriesEnv] ?? "")
      .split(",")
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean),
  );
  return { baseUrl, token, allowedRepositories, allowedRepositoriesEnv };
}

function segment(value) {
  return encodeURIComponent(value);
}

async function request(client, path, options = {}) {
  const response = await fetch(`${client.baseUrl}/api/v1${path}`, {
    method: options.method ?? "GET",
    headers: {
      Accept: "application/json",
      Authorization: `token ${client.token}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });
  if (options.allowNotFound && response.status === 404) {
    return null;
  }
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 500).replace(/\s+/gu, " ").trim();
    throw new Error(`Gitea API request failed with ${response.status}: ${detail || "no details"}`);
  }
  return response.status === 204 ? {} : response.json();
}

function stableIdentifier(value, field) {
  const normalized = text(value, field, 128);
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/u.test(normalized)) {
    throw new Error(`${field} contains unsupported characters`);
  }
  return normalized;
}

function operationMarkers(workflowId, idempotencyKey) {
  return {
    workflowId: stableIdentifier(workflowId, "workflow_id"),
    idempotencyKey: stableIdentifier(idempotencyKey, "idempotency_key"),
  };
}

function markedBody(body, markers) {
  const content = body === undefined ? "" : text(body, "body", 9000);
  return [
    content,
    `<!-- automation-workflow: ${markers.workflowId} -->`,
    `<!-- automation-idempotency-key: ${markers.idempotencyKey} -->`,
  ].filter(Boolean).join("\n\n");
}

function repositoryPath(owner, repository) {
  return `/repos/${segment(owner)}/${segment(repository)}`;
}

function requireWriteRepository(client, owner, repository) {
  const fullName = `${owner}/${repository}`.toLowerCase();
  if (!client.allowedRepositories.has(fullName)) {
    throw new Error(
      `write access to ${fullName} is not allowed by ${client.allowedRepositoriesEnv}`,
    );
  }
}

function apiOutput(label) {
  return {
    schema: { type: "object", additionalProperties: true },
    render: (_args, value) => [
      {
        type: "text",
        text: `${label}: ${JSON.stringify(value).slice(0, 12000)}`,
      },
    ],
  };
}

export function apply(ctx, config = {}) {
  const client = configuration(config);
  ctx.systemPrompt.section({
    name: "tool:gitea",
    order: 90,
    text:
      "Use the Gitea tools for repository and pull-request API operations. " +
      "Clone and push only with automation_git_url returned by gitea_get_repository; " +
      "Never request, print, or place Gitea credentials in commands, files, results, or logs. " +
      "Before creating a pull request, verify its owner, repository, head, base, and title.",
  });

  ctx.tools.register(
    defineTool({
      name: "gitea_get_repository",
      description: "Get Gitea repository metadata and default branch.",
      parameters: { owner: identifier, repository: identifier },
      output: apiOutput("Gitea repository"),
      async execute(args, exec) {
        const owner = text(args.owner, "owner");
        const repository = text(args.repository, "repository");
        const metadata = await request(client, repositoryPath(owner, repository), {
          signal: exec.signal,
        });
        const automationGitUrl = `${client.baseUrl}/${segment(owner)}/${segment(repository)}.git`;
        return {
          ...metadata,
          clone_url: automationGitUrl,
          automation_git_url: automationGitUrl,
        };
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "gitea_get_pull_request",
      description: "Get a Gitea pull request by numeric index.",
      parameters: {
        owner: identifier,
        repository: identifier,
        index: { type: "integer", required: true },
      },
      output: apiOutput("Gitea pull request"),
      async execute(args, exec) {
        return request(
          client,
          `${repositoryPath(text(args.owner, "owner"), text(args.repository, "repository"))}/pulls/${pullIndex(args.index)}`,
          { signal: exec.signal },
        );
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "gitea_create_pull_request",
      description: "Create a Gitea pull request after a branch has been pushed.",
      parameters: {
        owner: identifier,
        repository: identifier,
        title: identifier,
        head: identifier,
        base: identifier,
        body: { type: "string" },
        workflow_id: identifier,
        idempotency_key: identifier,
      },
      output: apiOutput("Created Gitea pull request"),
      async execute(args, exec) {
        const owner = text(args.owner, "owner");
        const repository = text(args.repository, "repository");
        requireWriteRepository(client, owner, repository);
        const head = text(args.head, "head");
        const base = text(args.base, "base");
        const markers = operationMarkers(args.workflow_id, args.idempotency_key);
        const repositoryRoot = repositoryPath(owner, repository);
        const expectedHead = `automation/${markers.workflowId}`;
        if (head !== expectedHead) {
          throw new Error(`head must be the stable workflow branch ${expectedHead}`);
        }
        const openPulls = await request(
          client,
          `${repositoryRoot}/pulls?state=open&limit=100`,
          { signal: exec.signal },
        );
        const markedPull = Array.isArray(openPulls)
          ? openPulls.find((pull) =>
              String(pull?.body ?? "").includes(
                `automation-idempotency-key: ${markers.idempotencyKey}`,
              ),
            )
          : undefined;
        if (markedPull) {
          return { ...markedPull, automation_reused: true };
        }
        const existing = await request(
          client,
          `${repositoryRoot}/pulls/${segment(base)}/${segment(head)}`,
          { signal: exec.signal, allowNotFound: true },
        );
        if (existing !== null) {
          if (!String(existing.body ?? "").includes(`automation-idempotency-key: ${markers.idempotencyKey}`)) {
            throw new Error("a pull request already exists for head/base with another idempotency key");
          }
          return { ...existing, automation_reused: true };
        }
        return request(client, `${repositoryRoot}/pulls`, {
          method: "POST",
          body: {
            title: text(args.title, "title"),
            head,
            base,
            body: markedBody(args.body, markers),
          },
          signal: exec.signal,
        });
      },
    }),
  );

  ctx.tools.register(
    defineTool({
      name: "gitea_comment_pull_request",
      description: "Post a review comment to a Gitea pull request conversation.",
      parameters: {
        owner: identifier,
        repository: identifier,
        index: { type: "integer", required: true },
        body: { type: "string", required: true },
        workflow_id: identifier,
        idempotency_key: identifier,
      },
      output: apiOutput("Created Gitea comment"),
      async execute(args, exec) {
        const owner = text(args.owner, "owner");
        const repository = text(args.repository, "repository");
        requireWriteRepository(client, owner, repository);
        const index = pullIndex(args.index);
        const markers = operationMarkers(args.workflow_id, args.idempotency_key);
        const repositoryRoot = repositoryPath(owner, repository);
        const comments = await request(
          client,
          `${repositoryRoot}/issues/${index}/comments?limit=100`,
          { signal: exec.signal },
        );
        const existing = Array.isArray(comments)
          ? comments.find((comment) =>
              String(comment?.body ?? "").includes(`automation-idempotency-key: ${markers.idempotencyKey}`),
            )
          : undefined;
        if (existing) {
          return { ...existing, automation_reused: true };
        }
        return request(
          client,
          `${repositoryRoot}/issues/${index}/comments`,
          {
            method: "POST",
            body: { body: markedBody(args.body, markers) },
            signal: exec.signal,
          },
        );
      },
    }),
  );
}
