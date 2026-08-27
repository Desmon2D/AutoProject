import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render(path = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request(`http://localhost${path}`, { headers: { accept: "text/html" } }),
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
  assert.match(html, /Проверить точную ревизию/i);
  assert.match(html, /Запустить поиск/i);
  assert.doesNotMatch(html, />Skills</);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("server-renders the standalone Flow Builder", async () => {
  const response = await render("/flows");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /Flow Builder/);
  assert.match(html, /VISUAL AUTOMATION/);
  assert.match(html, /Dashboard/);
  assert.match(html, /Загрузка каталогов/);
});

test("workflow controls are wired to orchestrator actions", async () => {
  const source = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  const flowsSource = await readFile(new URL("../app/flows/page.tsx", import.meta.url), "utf8");
  assert.match(source, /\/review/);
  assert.match(source, /\/cancel/);
  assert.match(source, /\/retry/);
  assert.match(source, /Одобрить/);
  assert.match(source, /На доработку/);
  assert.match(source, /Повторить/);
  assert.match(source, /Отменить/);
  assert.match(source, /\/v1\/analysis/);
  assert.match(source, /\/v1\/bug-finding/);
  assert.match(source, /\/artifacts\//);
  assert.match(source, /development: "Разработка"/);
  assert.match(source, /"bug-finding": "Поиск ошибок"/);
  assert.match(source, /Передать в тестирование/);
  assert.match(source, /Вернуть в разработку/);
  assert.match(source, /pending_review/);
  assert.match(source, /\/v1\/flows/);
  assert.match(flowsSource, /\/v1\/node-types/);
  assert.match(source, /FLOW BUILDER/);
  assert.match(source, /flow-canvas/);
  assert.match(source, /FlowBuilder/);
  assert.match(flowsSource, /\/draft/);
  assert.match(flowsSource, /\/validate/);
  assert.match(flowsSource, /\/publish/);
  assert.match(source, /Создать копию/);
  assert.match(source, /Опубликовать/);
  assert.match(source, /onPointerMove/);
  assert.match(source, /addNode/);
  assert.match(source, /deleteNode/);
  assert.match(source, /setTransition/);
  assert.match(source, /SchemaObjectFields/);
  assert.match(source, /resolveNodeConfigurationSchema/);
  assert.match(source, /InputMappingEditor/);
  assert.match(source, /x-ui-options-by/);
  assert.match(source, /suggestedOptions/);
  assert.match(flowsSource, /\/v1\/operations/);
  assert.match(flowsSource, /\/v1\/models/);
  assert.match(flowsSource, /\/v1\/credentials/);
  assert.match(source, /x-ui-catalog/);
  assert.match(source, /schema-choice-list/);
  assert.match(source, /draggable=\{editing && \(item\.type !== "trigger" \|\| !hasTrigger\)\}/);
  assert.match(source, /onDragStart/);
  assert.match(source, /onDrop/);
  assert.match(source, /application\/x-automation-node/);
  assert.match(source, /flow-port-input/);
  assert.match(source, /flow-port-output/);
  assert.match(source, /connectToNode/);
  assert.match(source, /onPointerUp/);
  assert.match(source, /connectionPointer/);
  assert.match(source, /flow-edge-preview/);
  assert.match(source, /FLOW_OUTPUT_PORT_Y\[edge\.source_port\]/);
  assert.doesNotMatch(source, /Соедините \{connectionDrag\.port\} со входом/);
  assert.match(source, /Отпустите, чтобы создать/);
  assert.doesNotMatch(source, /onClick=\{\(\) => addNode\(item\.type\)\}/);
  assert.match(source, /Добавить binding/);
  assert.match(source, /Введите корректный JSON-объект/);
  assert.match(source, /"if" \| "switch" \| "delay" \| "merge"/);
  assert.match(source, /inputs\.enabled/);
  assert.match(source, /merge: \{ mode: "any"/);
  assert.match(source, /historyRef/);
  assert.match(source, /futureRef/);
  assert.match(source, /Ctrl\+Z/);
  assert.match(flowsSource, /\/runs/);
  assert.match(flowsSource, /runInputs/);
  assert.match(flowsSource, /flow-validation-panel/);
  assert.match(flowsSource, /createBlankFlow/);
  assert.match(flowsSource, /Пустой workflow/);
  assert.match(source, /Сначала сохраните изменения/);
});
