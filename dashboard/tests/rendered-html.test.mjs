import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the operations dashboard", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<title>Automation Control/);
  assert.match(html, /Automation Control/);
  assert.match(html, /Agent Operations/i);
  assert.match(html, /Процессы агентов/);
  assert.match(html, /Детали/);
  assert.match(html, /Сценарии и образы/);
  assert.match(html, /Обработчик/);
  assert.match(html, /Plane/);
  assert.match(html, /SWIRL/);
  assert.match(html, /BookStack/);
  assert.match(html, /Технические ошибки/i);
  assert.match(html, /Новый аналитический документ/i);
  assert.match(html, /Запустить анализ/i);
  assert.doesNotMatch(html, />Skills</);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("workflow controls are wired to orchestrator actions", async () => {
  const source = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(source, /\/review/);
  assert.match(source, /\/cancel/);
  assert.match(source, /\/retry/);
  assert.match(source, /Одобрить/);
  assert.match(source, /На доработку/);
  assert.match(source, /Повторить/);
  assert.match(source, /Отменить/);
  assert.match(source, /\/v1\/analysis/);
  assert.match(source, /\/artifacts\//);
  assert.match(source, /development: "Разработка"/);
  assert.match(source, /"bug-finding": "Поиск ошибок"/);
  assert.match(source, /Передать в тестирование/);
  assert.match(source, /Вернуть в разработку/);
  assert.match(source, /pending_review/);
});
