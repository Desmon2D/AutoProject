"use client";

import { useCallback, useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ?? "http://127.0.0.1:8080";
const REFRESH_INTERVAL = 10_000;
const SENSITIVE_KEY_PARTS = ["api_key", "apikey", "authorization", "password", "secret", "token"];

type Status = "CREATED" | "RUNNING" | "WAITING" | "COMPLETED" | "FAILED" | "CANCELLED";
type Outcome = "SUCCESS" | "FAILURE" | null;
type WorkflowAction = "approve" | "request_changes" | "cancel" | "retry";
type IndicatorState = "ready" | "idle" | "error";
type JsonObject = Record<string, unknown>;
type Health = {
  status: string;
  docker: boolean;
  providers: {
    openai: { configured: boolean };
    openrouter: { configured: boolean };
    gitea: { configured: boolean; url: string };
    plane: { configured: boolean; url: string };
    swirl: { configured: boolean; url: string };
  };
  default_agent: { provider: string; model: string };
  queue: {
    pending: number;
    running: number;
    completed: number;
    failed: number;
    worker_online: boolean;
    worker_last_heartbeat: number | null;
  };
};
type Artifact = { type: string; uri: string; summary: string | null };
type StepExecution = {
  step_id: string;
  execution_id: string;
  iteration: number;
  attempt: number;
  execution_status: string;
  outcome: Outcome;
  data: JsonObject;
  artifacts: Artifact[];
  error: { code: string; message: string; retryable: boolean } | null;
};
type Workflow = {
  id: string;
  scenario_id: string;
  scenario_version: string;
  status: Status;
  outcome: Outcome;
  current_step: string | null;
  executions: StepExecution[];
  trigger: { source: string; event: string; event_id: string; data: JsonObject };
  review_comments: string[];
  error: { code: string; message: string; retryable: boolean } | null;
  created_at: string;
  updated_at: string;
};
type ScenarioStep = {
  type: string;
  transitions: Record<string, string | null>;
  retry?: { max_attempts: number };
  prompt?: string;
  plugins?: string[];
  context_search?: { query?: string; query_field?: string; providers: string[]; max_results: number };
  provider?: string;
  model?: string;
  timeout_seconds?: number;
  command?: string;
  parameters?: JsonObject;
};
type Scenario = {
  id: string;
  version: string;
  enabled: boolean;
  trigger: { source: string; event: string };
  start_step: string;
  steps: Record<string, ScenarioStep>;
};
type Extension = { name: string; version: string; enabled: boolean; mandatory?: boolean };
type ImageProfile = {
  name: string;
  image: string;
  harness_version: string;
  enabled: boolean;
  capabilities: string[];
};
type Snapshot = {
  health: Health;
  workflows: Workflow[];
  scenarios: Scenario[];
  plugins: Extension[];
  images: ImageProfile[];
};

const statusLabels: Record<Status, string> = {
  CREATED: "создан",
  RUNNING: "в работе",
  WAITING: "ожидает review",
  COMPLETED: "завершён",
  FAILED: "ошибка",
  CANCELLED: "отменён",
};

const outcomeLabels: Record<Exclude<Outcome, null>, string> = {
  SUCCESS: "успешно",
  FAILURE: "неудачно",
};

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json() as Promise<T>;
}

async function postJson<T>(path: string, body: JsonObject): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `${path}: HTTP ${response.status}`;
    try {
      const payload = await response.json() as { detail?: unknown };
      if (typeof payload.detail === "string") message = payload.detail;
    } catch {
      // The HTTP status remains useful when the response has no JSON body.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function redactForDisplay(value: unknown, depth = 0): unknown {
  if (depth > 8) return "[depth limit]";
  if (Array.isArray(value)) return value.slice(0, 100).map((item) => redactForDisplay(item, depth + 1));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => {
        const normalized = key.toLowerCase();
        const sensitive = SENSITIVE_KEY_PARTS.some((part) => normalized.includes(part));
        return [key, sensitive ? "[REDACTED]" : redactForDisplay(item, depth + 1)];
      }),
    );
  }
  return value;
}

function JsonBlock({ value, empty = "Нет данных" }: { value: unknown; empty?: string }) {
  if (value === null || value === undefined) return <div className="json-empty">{empty}</div>;
  if (Array.isArray(value) && value.length === 0) return <div className="json-empty">{empty}</div>;
  if (typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0) {
    return <div className="json-empty">{empty}</div>;
  }
  return <pre className="json-block">{JSON.stringify(redactForDisplay(value), null, 2)}</pre>;
}

function ServiceState({ state, label }: { state: IndicatorState; label: string }) {
  return <span className={`service-state is-${state}`}><span className="service-dot" aria-hidden="true" />{label}</span>;
}

function executionInput(workflow: Workflow, scenario: Scenario | undefined, execution: StepExecution, index: number) {
  const step = scenario?.steps[execution.step_id] ?? { type: "unknown" };
  if (step.type !== "agent") return { step };
  return {
    step,
    context: {
      trigger_data: workflow.trigger.data,
      previous_steps: workflow.executions.slice(0, index).map((item) => ({
        step_id: item.step_id,
        execution_status: item.execution_status,
        outcome: item.outcome,
        data: item.data,
        artifacts: item.artifacts,
      })),
      review_comments: workflow.review_comments,
    },
  };
}

function WorkflowDetails({
  workflow,
  scenario,
  actionBusy,
  actionFeedback,
  onAction,
  onClose,
}: {
  workflow: Workflow;
  scenario: Scenario | undefined;
  actionBusy: boolean;
  actionFeedback: { kind: "success" | "error"; message: string } | null;
  onAction: (action: WorkflowAction, note: string) => Promise<void>;
  onClose: () => void;
}) {
  const [actionNote, setActionNote] = useState("");
  const canReview = workflow.status === "WAITING";
  const canCancel = ["CREATED", "RUNNING", "WAITING"].includes(workflow.status);
  const canRetry = workflow.status === "FAILED";
  const hasActions = canReview || canCancel || canRetry;

  return (
    <section className="inspection-panel" aria-label={`Детали процесса ${workflow.id}`}>
      <div className="inspection-header">
        <div><p className="eyebrow">WORKFLOW INSPECTION</p><h3 className="mono">{workflow.id}</h3></div>
        <button type="button" className="close-button" onClick={onClose}>Закрыть ×</button>
      </div>
      <div className="inspection-meta">
        <span><small>Сценарий</small>{workflow.scenario_id} · v{workflow.scenario_version}</span>
        <span><small>Статус</small>{statusLabels[workflow.status]}</span>
        <span><small>Итог</small>{workflow.outcome ? outcomeLabels[workflow.outcome] : "—"}</span>
        <span><small>Создан</small>{formatTime(workflow.created_at)}</span>
        <span><small>Обновлён</small>{formatTime(workflow.updated_at)}</span>
      </div>
      {hasActions && (
        <section className="workflow-actions" aria-label="Действия с процессом">
          <label htmlFor={`action-note-${workflow.id}`}>Комментарий или причина</label>
          <textarea
            id={`action-note-${workflow.id}`}
            value={actionNote}
            maxLength={2000}
            placeholder={canReview ? "Комментарий к проверке" : "Причина действия"}
            onChange={(event) => setActionNote(event.target.value)}
            disabled={actionBusy}
          />
          <div className="workflow-action-buttons">
            {canReview && <button type="button" className="action-button action-approve" disabled={actionBusy} onClick={() => void onAction("approve", actionNote)}>Одобрить</button>}
            {canReview && <button type="button" className="action-button" disabled={actionBusy} onClick={() => void onAction("request_changes", actionNote)}>На доработку</button>}
            {canRetry && <button type="button" className="action-button action-approve" disabled={actionBusy} onClick={() => void onAction("retry", actionNote)}>Повторить</button>}
            {canCancel && <button type="button" className="action-button action-danger" disabled={actionBusy} onClick={() => void onAction("cancel", actionNote)}>Отменить</button>}
          </div>
          {actionFeedback && <p className={`action-feedback is-${actionFeedback.kind}`} role={actionFeedback.kind === "error" ? "alert" : "status"}>{actionFeedback.message}</p>}
        </section>
      )}
      <div className="workflow-io-grid">
        <article className="io-card"><div className="io-title"><span className="io-direction">IN</span><h4>Вход workflow</h4></div><JsonBlock value={workflow.trigger} /></article>
        <article className="io-card"><div className="io-title"><span className="io-direction output">OUT</span><h4>Итог workflow</h4></div><JsonBlock value={{ status: workflow.status, outcome: workflow.outcome, current_step: workflow.current_step, error: workflow.error }} /></article>
      </div>
      <div className="execution-list">
        <div className="section-label">Выполненные шаги · {workflow.executions.length}</div>
        {workflow.executions.map((execution, index) => (
          <details className="execution-detail" key={execution.execution_id} open={index === workflow.executions.length - 1}>
            <summary>
              <span className="execution-index">{String(index + 1).padStart(2, "0")}</span>
              <span><strong>{execution.step_id}</strong><small className="mono">{execution.execution_id}</small></span>
              <span className={`outcome-chip outcome-${(execution.outcome ?? execution.execution_status).toLowerCase()}`}>{execution.outcome ?? execution.execution_status}</span>
              <span className="disclosure">⌄</span>
            </summary>
            <div className="execution-meta"><span>Итерация {execution.iteration}</span><span>Попытка {execution.attempt}</span><span>{execution.execution_status}</span></div>
            <div className="step-io-grid">
              <article><h5><span className="io-direction">IN</span>Вход шага</h5><JsonBlock value={executionInput(workflow, scenario, execution, index)} /></article>
              <article><h5><span className="io-direction output">OUT</span>Выход шага</h5><JsonBlock value={{ data: execution.data, artifacts: execution.artifacts, error: execution.error }} /></article>
            </div>
          </details>
        ))}
        {workflow.executions.length === 0 && <div className="json-empty large">Шаги ещё не выполнялись.</div>}
      </div>
    </section>
  );
}

function ScenarioDetails({ scenario, onClose }: { scenario: Scenario; onClose: () => void }) {
  return (
    <section className="inspection-panel scenario-inspection" aria-label={`Детали сценария ${scenario.id}`}>
      <div className="inspection-header">
        <div><p className="eyebrow">SCENARIO DEFINITION</p><h3>{scenario.id} <span className="mono">v{scenario.version}</span></h3></div>
        <button type="button" className="close-button" onClick={onClose}>Закрыть ×</button>
      </div>
      <div className="scenario-overview">
        <div><small>Trigger</small><strong>{scenario.trigger.source} / {scenario.trigger.event}</strong></div>
        <div><small>Стартовый шаг</small><strong>{scenario.start_step}</strong></div>
        <div><small>Состояние</small><strong>{scenario.enabled ? "включён" : "выключен"}</strong></div>
      </div>
      <div className="execution-list">
        <div className="section-label">Определения шагов · {Object.keys(scenario.steps).length}</div>
        {Object.entries(scenario.steps).map(([stepId, step], index) => {
          const { transitions, ...input } = step;
          return (
            <details className="execution-detail" key={stepId} open={index === 0}>
              <summary>
                <span className="execution-index">{String(index + 1).padStart(2, "0")}</span>
                <span><strong>{stepId}</strong><small>{step.type}</small></span>
                <span className="step-type">{step.provider ?? step.command ?? step.type}</span>
                <span className="disclosure">⌄</span>
              </summary>
              <div className="step-io-grid">
                <article><h5><span className="io-direction">IN</span>Настройки шага</h5><JsonBlock value={input} /></article>
                <article><h5><span className="io-direction output">NEXT</span>Переходы</h5><JsonBlock value={transitions} /></article>
              </div>
            </details>
          );
        })}
      </div>
    </section>
  );
}

export default function Dashboard() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [filter, setFilter] = useState<Status | "ALL">("ALL");
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null);
  const [selectedScenarioId, setSelectedScenarioId] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionFeedback, setActionFeedback] = useState<{
    workflowId: string;
    kind: "success" | "error";
    message: string;
  } | null>(null);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const [health, workflows, scenarios, plugins, images] = await Promise.all([
        getJson<Health>("/health"), getJson<Workflow[]>("/v1/workflows"), getJson<Scenario[]>("/v1/scenarios"),
        getJson<Extension[]>("/v1/plugins"), getJson<ImageProfile[]>("/v1/images"),
      ]);
      setSnapshot({ health, workflows, scenarios, plugins, images });
      setUpdatedAt(new Date());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "API недоступен");
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(), REFRESH_INTERVAL);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [load]);

  const runWorkflowAction = useCallback(async (workflow: Workflow, action: WorkflowAction, note: string) => {
    const reason = note.trim();
    const basePath = `/v1/workflows/${encodeURIComponent(workflow.id)}`;
    const requests: Record<WorkflowAction, { path: string; body: JsonObject; success: string }> = {
      approve: {
        path: `${basePath}/review`,
        body: { outcome: "SUCCESS", comments: reason ? [reason] : [] },
        success: "Одобрение принято.",
      },
      request_changes: {
        path: `${basePath}/review`,
        body: { outcome: "FAILURE", comments: reason ? [reason] : [] },
        success: "Процесс возвращён на доработку.",
      },
      cancel: {
        path: `${basePath}/cancel`,
        body: { reason: reason || "Отменено пользователем через панель" },
        success: "Процесс отменён.",
      },
      retry: {
        path: `${basePath}/retry`,
        body: { reason: reason || "Повтор запрошен пользователем через панель" },
        success: "Повтор поставлен в очередь.",
      },
    };
    const request = requests[action];
    setActionBusy(true);
    setActionFeedback(null);
    try {
      const updated = await postJson<Workflow>(request.path, request.body);
      setSnapshot((current) => current ? {
        ...current,
        workflows: current.workflows.map((item) => item.id === updated.id ? updated : item),
      } : current);
      setActionFeedback({ workflowId: workflow.id, kind: "success", message: request.success });
      await load();
    } catch (reasonValue) {
      setActionFeedback({
        workflowId: workflow.id,
        kind: "error",
        message: reasonValue instanceof Error ? reasonValue.message : "Действие не выполнено",
      });
    } finally {
      setActionBusy(false);
    }
  }, [load]);

  const workflows = snapshot?.workflows ?? [];
  const visibleWorkflows = workflows.filter((item) => filter === "ALL" || item.status === filter);
  const active = workflows.filter((item) => ["CREATED", "RUNNING", "WAITING"].includes(item.status)).length;
  const succeeded = workflows.filter((item) => item.status === "COMPLETED" && item.outcome === "SUCCESS").length;
  const unsuccessful = workflows.filter((item) =>
    item.status === "FAILED" || (item.status === "COMPLETED" && item.outcome === "FAILURE"),
  ).length;
  const successRate = succeeded + unsuccessful === 0
    ? "—"
    : `${Math.round((succeeded / (succeeded + unsuccessful)) * 100)}%`;
  const selectedWorkflow = workflows.find((item) => item.id === selectedWorkflowId);
  const selectedWorkflowScenario = snapshot?.scenarios.find((item) => item.id === selectedWorkflow?.scenario_id);
  const selectedScenario = snapshot?.scenarios.find((item) => item.id === selectedScenarioId);
  const bookstackRoutes = snapshot?.scenarios.filter((scenario) =>
    Object.values(scenario.steps).some((step) => step.context_search?.providers.includes("bookstack")),
  ).length ?? 0;
  const technicalErrors = workflows.flatMap((workflow) =>
    workflow.executions
      .filter((execution) => execution.execution_status === "ERROR" && execution.error)
      .map((execution) => ({ workflow, execution, error: execution.error! })),
  ).slice(-5).reverse();

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <div className="brand-block"><span className="brand-mark" aria-hidden="true">A</span><div><p className="eyebrow">УПРАВЛЕНИЕ АГЕНТАМИ</p><h1>Automation Control</h1></div></div>
        <div className="topbar-actions">
          <div className="sync-copy"><span>{error ? "Связь потеряна" : "Автообновление · 10 сек"}</span><time>{updatedAt ? updatedAt.toLocaleTimeString("ru-RU") : "—"}</time></div>
          <button className="refresh-button" onClick={() => void load()} disabled={refreshing}><span className={refreshing ? "spin" : ""} aria-hidden="true">↻</span>Обновить</button>
        </div>
      </header>

      {error && <div className="alert" role="alert"><strong>Оркестратор недоступен.</strong><span>{error}</span></div>}

      <section className="status-rail" aria-label="Состояние сервисов">
        <div className="rail-label">СОСТОЯНИЕ СИСТЕМЫ</div>
        <ServiceState state={snapshot?.health.status === "ok" ? "ready" : "error"} label="Оркестратор" />
        <ServiceState state={snapshot?.health.docker ? "ready" : "error"} label="Песочница Docker" />
        <ServiceState state={snapshot?.health.providers.openrouter.configured ? "ready" : "idle"} label="OpenRouter" />
        <ServiceState state={snapshot?.health.providers.gitea.configured ? "ready" : "idle"} label="Gitea" />
        <ServiceState state={snapshot?.health.queue.worker_online ? "ready" : "error"} label="Обработчик" />
        <ServiceState state={snapshot?.health.providers.plane.configured ? "ready" : "idle"} label="Plane" />
        <ServiceState state={snapshot?.health.providers.swirl.configured ? "ready" : "idle"} label="SWIRL" />
        <a href="http://127.0.0.1:3000" target="_blank" rel="noreferrer">открыть Gitea ↗</a>
      </section>

      <section className="metrics" aria-label="Сводка">
        <article className="metric-card primary-metric"><p>Активные процессы</p><strong>{active.toString().padStart(2, "0")}</strong><span>{active ? "агенты выполняют задачи" : "очередь свободна"}</span></article>
        <article className="metric-card"><p>Успешность</p><strong>{successRate}</strong><span>{succeeded} успешно · {unsuccessful} неудачно</span></article>
        <article className="metric-card"><p>Сценарии</p><strong>{snapshot?.scenarios.filter((item) => item.enabled).length ?? "—"}</strong><span>валидных workflow-графов</span></article>
        <article className="metric-card"><p>Очередь workflow</p><strong>{snapshot?.health.queue.pending ?? "—"}</strong><span>{snapshot ? `${snapshot.health.queue.running} выполняется · ${snapshot.health.queue.failed} сбоев worker` : "ожидаем worker"}</span></article>
      </section>

      <div className="content-grid">
        <section className="panel workflows-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">EXECUTION STREAM</p><h2>Процессы агентов</h2><p className="panel-help">Нажмите «Детали», чтобы увидеть вход и выход шагов.</p></div>
            <div className="filters" aria-label="Фильтр процессов">{(["ALL", "RUNNING", "WAITING", "FAILED"] as const).map((status) => <button key={status} className={filter === status ? "active" : ""} onClick={() => setFilter(status)}>{status === "ALL" ? "Все" : statusLabels[status]}</button>)}</div>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Процесс</th><th>Сценарий / шаг</th><th>Статус</th><th>Обновлён</th><th><span className="sr-only">Действия</span></th></tr></thead>
              <tbody>{visibleWorkflows.map((workflow) => (
                <tr key={workflow.id} className={selectedWorkflowId === workflow.id ? "is-selected" : ""}>
                  <td><span className="mono workflow-id">{workflow.id}</span><small>{workflow.trigger.source} · {workflow.trigger.event}</small></td>
                  <td><span>{workflow.scenario_id}</span><small>{workflow.current_step ?? `${workflow.executions.length} шагов`}</small></td>
                  <td><span className={`status-pill status-${workflow.status.toLowerCase()}`}>{statusLabels[workflow.status]}</span>{workflow.outcome && <small>{outcomeLabels[workflow.outcome]}</small>}</td>
                  <td><time>{formatTime(workflow.updated_at)}</time></td>
                  <td><button type="button" className="inspect-button" aria-expanded={selectedWorkflowId === workflow.id} onClick={() => setSelectedWorkflowId(selectedWorkflowId === workflow.id ? null : workflow.id)}>{selectedWorkflowId === workflow.id ? "Скрыть" : "Детали"}</button></td>
                </tr>
              ))}</tbody>
            </table>
            {!snapshot && <div className="empty-state">Получаем данные оркестратора…</div>}
            {snapshot && visibleWorkflows.length === 0 && <div className="empty-state"><span>Очередь пуста</span>Процессы появятся после первого trigger-события.</div>}
          </div>
          {selectedWorkflow && <WorkflowDetails
            workflow={selectedWorkflow}
            scenario={selectedWorkflowScenario}
            actionBusy={actionBusy}
            actionFeedback={actionFeedback?.workflowId === selectedWorkflow.id ? actionFeedback : null}
            onAction={(action, note) => runWorkflowAction(selectedWorkflow, action, note)}
            onClose={() => setSelectedWorkflowId(null)}
          />}
        </section>

        <aside className="side-stack">
          <section className="panel integration-panel">
            <div className="panel-heading compact"><div><p className="eyebrow">ИНТЕГРАЦИИ</p><h2>Готовность источников</h2></div></div>
            <div className="integration-list">
              <article><span className="integration-code">PL</span><div><strong>Plane</strong><small>Источник задач</small></div><span className={`readiness ${snapshot?.health.providers.plane.configured ? "ready" : "idle"}`}>{snapshot?.health.providers.plane.configured ? "настроен" : "выключен"}</span></article>
              <article><span className="integration-code">SW</span><div><strong>SWIRL</strong><small>Корпоративный поиск</small></div><span className={`readiness ${snapshot?.health.providers.swirl.configured ? "ready" : "idle"}`}>{snapshot?.health.providers.swirl.configured ? "настроен" : "выключен"}</span></article>
              <article><span className="integration-code">BK</span><div><strong>BookStack</strong><small>Источник знаний через SWIRL</small></div><span className={`readiness ${bookstackRoutes ? "ready" : "idle"}`}>{bookstackRoutes ? `в ${bookstackRoutes} сцен.` : "не назначен"}</span></article>
            </div>
          </section>
          <section className="panel errors-panel">
            <div className="panel-heading compact"><div><p className="eyebrow">ТЕХНИЧЕСКИЕ ОШИБКИ</p><h2>Последние причины сбоев</h2></div><span className="inventory-total error-total">{technicalErrors.length}</span></div>
            <div className="error-list">
              {technicalErrors.map(({ workflow, execution, error: stepError }) => (
                <button type="button" key={execution.execution_id} onClick={() => setSelectedWorkflowId(workflow.id)}>
                  <span className="error-code">{stepError.code}</span>
                  <strong>{execution.step_id} · попытка {execution.attempt}</strong>
                  <small>{stepError.message}</small>
                  <i>{stepError.retryable ? "разрешён повтор" : "повтор запрещён"}</i>
                </button>
              ))}
              {technicalErrors.length === 0 && <div className="empty-inline">Технических ошибок нет</div>}
            </div>
          </section>
          <section className="panel inventory-panel">
            <div className="panel-heading compact"><div><p className="eyebrow">РАСШИРЕНИЯ</p><h2>Доступный инструментарий</h2></div><span className="inventory-total">{snapshot?.plugins.length ?? 0}</span></div>
            <div className="inventory-groups">
              <div><h3>Подключаемые средства</h3><div className="tag-list">{snapshot?.plugins.map((item) => <span key={item.name} className={item.enabled ? "" : "disabled"}>{item.name}<i>{item.version}</i></span>)}</div></div>
            </div>
          </section>
        </aside>
      </div>

      <section className="panel scenario-panel">
        <div className="panel-heading"><div><p className="eyebrow">AUTOMATION CATALOG</p><h2>Сценарии и образы</h2></div><span className="mono api-address">API {API_BASE}</span></div>
        <div className="catalog-grid">
          <div className="catalog-list">
            {snapshot?.scenarios.map((scenario, index) => (
              <button type="button" className={selectedScenarioId === scenario.id ? "is-selected" : ""} key={scenario.id} aria-expanded={selectedScenarioId === scenario.id} onClick={() => setSelectedScenarioId(selectedScenarioId === scenario.id ? null : scenario.id)}>
                <span className="catalog-index">{String(index + 1).padStart(2, "0")}</span><span><strong>{scenario.id}</strong><small>{scenario.trigger.source} / {scenario.trigger.event}</small></span><span>{Object.keys(scenario.steps).length} шаг.</span><span className="catalog-action">{selectedScenarioId === scenario.id ? "скрыть" : "детали"}</span>
              </button>
            ))}
            {snapshot?.scenarios.length === 0 && <div className="empty-inline">Нет сценариев</div>}
          </div>
          <div className="image-list">{snapshot?.images.map((image) => <article key={image.name}><div className="image-title"><span className="cube" aria-hidden="true" /><div><strong>{image.name}</strong><small>{image.image}</small></div></div><div className="capability-row">{image.capabilities.map((capability) => <span key={capability}>{capability}</span>)}</div><small>Harness {image.harness_version}</small></article>)}</div>
        </div>
        {selectedScenario && <ScenarioDetails scenario={selectedScenario} onClose={() => setSelectedScenarioId(null)} />}
      </section>

      <footer><span>LOCAL CONTROL PLANE</span><span>Данные остаются внутри Docker-контура</span></footer>
    </main>
  );
}
