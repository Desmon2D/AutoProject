import { apply } from "/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@automation/dsh-plugin-gitea/index.js";

const tools = [];
const context = {
  tools: { register: (tool) => tools.push(tool) },
  systemPrompt: { section: () => undefined },
};
apply(context, { baseUrlEnv: "GITEA_BASE_URL", tokenEnv: "GITEA_TOKEN" });

const tool = tools.find((candidate) => candidate.name === "gitea_get_repository");
if (!tool) {
  throw new Error("gitea_get_repository was not registered");
}
const repository = await tool.execute(
  {
    owner: process.env.GITEA_USERNAME,
    repository: process.env.GITEA_REPOSITORY,
  },
  { signal: new AbortController().signal },
);
const expected = `${process.env.GITEA_USERNAME}/${process.env.GITEA_REPOSITORY}`;
if (repository.full_name !== expected) {
  throw new Error(`unexpected Gitea repository: ${repository.full_name}`);
}
console.log(`Gitea Harness plugin check passed: ${repository.full_name}`);
