"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import Link from "next/link";

const API_BASE = process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ?? "http://127.0.0.1:8080";
const REFRESH_INTERVAL = 10_000;
const SENSITIVE_KEY_PARTS = ["api_key", "apikey", "authorization", "password", "secret", "token"];

type Status = "CREATED" | "RUNNING" | "WAITING" | "COMPLETED" | "FAILED" | "CANCELLED";
type Outcome = "SUCCESS" | "FAILURE" | null;
type WorkflowAction = "approve" | "request_changes" | "cancel" | "retry";
type IndicatorState = "ready" | "idle" | "error";
export type JsonObject = Record<string, unknown>;
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
  pending_review: { provider: "gitea" | "plane"; decision: "review" | "merge" } | null;
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
  result_contract?: string;
  command?: string;
  parameters?: JsonObject;
};
type Scenario = {
  id: string;
  version: string;
  stage: "analysis" | "development" | "testing" | "bug-finding" | "operations" | "system";
  title: string | null;
  description: string | null;
  enabled: boolean;
  trigger: { source: string; event: string };
  start_step: string;
  steps: Record<string, ScenarioStep>;
};
export type FlowNode = {
  id: string;
  type: "trigger" | "agent" | "command" | "review" | "if" | "switch" | "delay" | "merge" | "terminal";
  category: "trigger" | "execution" | "control" | "data" | "terminal";
  title: string;
  subtitle: string | null;
  config: JsonObject;
  input_mapping: Record<string, string>;
  position: { x: number; y: number };
  read_only: boolean;
};
export type FlowEdge = {
  id: string;
  source: string;
  source_port: string;
  target: string;
  label: string;
  kind: "event" | "transition";
  outcome: "SUCCESS" | "FAILURE" | null;
};
export type FlowDefinition = {
  id: string;
  revision: number;
  version: string;
  title: string;
  description: string | null;
  stage: Scenario["stage"];
  enabled: boolean;
  builtin: boolean;
  read_only: boolean;
  status: "builtin" | "draft" | "published";
  source_scenario_id: string | null;
  start_node: string;
  nodes: FlowNode[];
  edges: FlowEdge[];
};
export type FlowValidationResult = {
  valid: boolean;
  errors: { code: string; message: string; node_id: string | null; edge_id: string | null }[];
  warnings: { code: string; message: string; node_id: string | null; edge_id: string | null }[];
  sha256: string | null;
};
export type FlowVersion = {
  flow_id: string;
  version: number;
  sha256: string;
  definition: FlowDefinition;
  published_at: string;
};
export type FlowRun = {
  id: string;
  flow_id: string;
  flow_version: number;
  status: "CREATED" | "RUNNING" | "WAITING" | "COMPLETED" | "FAILED" | "CANCELLED";
  outcome: "SUCCESS" | "FAILURE" | null;
  current_node: string | null;
  inputs: JsonObject;
};
export type FlowDraftChanges = {
  title: string;
  description: string;
  startNode: string;
  nodes: FlowNode[];
  edges: FlowEdge[];
};
type FlowEditorSnapshot = FlowDraftChanges;
type JsonSchema = {
  type?: string | string[];
  title?: string;
  description?: string;
  enum?: unknown[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  maxItems?: number;
  "x-ui-widget"?: "textarea";
  "x-ui-options-by"?: { field: string; values: Record<string, string[]> };
  "x-ui-catalog"?: "operations" | "models" | "plugins" | "credentials";
  "x-ui-filter-by"?: string;
  "x-ui-schema-from"?: { catalog: "operations"; selector: string; field: "input_schema" };
};
export type FlowNodeType = {
  type: FlowNode["type"];
  category: FlowNode["category"];
  title: string;
  description: string;
  config_schema: JsonSchema;
  input_schema: JsonSchema;
  output_schema: JsonSchema;
  outcomes: ("EVENT" | "SUCCESS" | "FAILURE")[];
};
const scenarioStageLabels: Record<Scenario["stage"], string> = {
  analysis: "Аналитика",
  development: "Разработка",
  testing: "Тестирование",
  "bug-finding": "Поиск ошибок",
  operations: "Операции",
  system: "Системный",
};
export type Extension = { name: string; version: string; enabled: boolean; mandatory?: boolean };
export type OperationDefinition = {
  id: string;
  title: string;
  description: string;
  input_schema: JsonSchema;
};
export type AgentModelDefinition = {
  id: string;
  provider: "openai" | "openrouter";
  title: string;
  configured: boolean;
  default: boolean;
};
export type CredentialReference = {
  id: string;
  provider: "openai" | "openrouter" | "gitea" | "plane";
  title: string;
  configured: boolean;
};
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
  flows: FlowDefinition[];
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

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json() as Promise<T>;
}

export async function postJson<T>(path: string, body: JsonObject): Promise<T> {
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

function artifactHref(executionId: string, uri: string): string | null {
  const prefix = `artifact://${executionId}/`;
  if (uri.startsWith(prefix)) {
    const path = uri.slice(prefix.length).split("/").map(encodeURIComponent).join("/");
    return `${API_BASE}/v1/agent-steps/${encodeURIComponent(executionId)}/artifacts/${path}`;
  }
  return uri.startsWith("http://") || uri.startsWith("https://") ? uri : null;
}

function ArtifactLinks({ execution }: { execution: StepExecution }) {
  const links = execution.artifacts
    .map((artifact) => ({ artifact, href: artifactHref(execution.execution_id, artifact.uri) }))
    .filter((item): item is { artifact: Artifact; href: string } => item.href !== null);
  if (links.length === 0) return null;
  return (
    <div className="artifact-links">
      {links.map(({ artifact, href }) => (
        <a key={`${artifact.type}:${artifact.uri}`} href={href} target="_blank" rel="noreferrer">
          <span>{artifact.type === "document" ? "Открыть документ" : "Открыть артефакт"}</span>
          <small>{artifact.summary ?? artifact.uri}</small>
        </a>
      ))}
    </div>
  );
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
  const isDevelopmentReview = canReview
    && workflow.pending_review?.provider === "plane"
    && scenario?.stage === "development";
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
          {isDevelopmentReview && <p className="action-context">Решение синхронизируется с Plane: одобрение запускает этап тестирования, возврат создаёт следующую итерацию разработки.</p>}
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
            {canReview && <button type="button" className="action-button action-approve" disabled={actionBusy} onClick={() => void onAction("approve", actionNote)}>{isDevelopmentReview ? "Передать в тестирование" : "Одобрить"}</button>}
            {canReview && <button type="button" className="action-button" disabled={actionBusy} onClick={() => void onAction("request_changes", actionNote)}>{isDevelopmentReview ? "Вернуть в разработку" : "На доработку"}</button>}
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
              <article><h5><span className="io-direction output">OUT</span>Выход шага</h5><JsonBlock value={{ data: execution.data, artifacts: execution.artifacts, error: execution.error }} /><ArtifactLinks execution={execution} /></article>
            </div>
          </details>
        ))}
        {workflow.executions.length === 0 && <div className="json-empty large">Шаги ещё не выполнялись.</div>}
      </div>
    </section>
  );
}

const FLOW_NODE_WIDTH = 210;
const FLOW_NODE_HEIGHT = 92;
const FLOW_OUTPUT_PORT_Y: Record<string, number> = {
  EVENT: FLOW_NODE_HEIGHT / 2,
  SUCCESS: 30,
  FAILURE: 66,
};

function schemaLabel(key: string, schema: JsonSchema) {
  return schema.title ?? key.replaceAll("_", " ");
}

function JsonObjectEditor({ value, onChange }: { value: unknown; onChange: (value: JsonObject) => void }) {
  const serialized = JSON.stringify(value && typeof value === "object" && !Array.isArray(value) ? value : {}, null, 2);
  const [text, setText] = useState(serialized);
  const [invalid, setInvalid] = useState(false);
  useEffect(() => {
    const sync = window.setTimeout(() => { setText(serialized); setInvalid(false); }, 0);
    return () => window.clearTimeout(sync);
  }, [serialized]);
  return <>
    <textarea
      className={invalid ? "is-invalid" : ""}
      value={text}
      spellCheck={false}
      onChange={(event) => {
        const next = event.target.value;
        setText(next);
        try {
          const parsed = JSON.parse(next) as unknown;
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("object required");
          setInvalid(false);
          onChange(parsed as JsonObject);
        } catch {
          setInvalid(true);
        }
      }}
    />
    {invalid && <small className="schema-field-error">Введите корректный JSON-объект.</small>}
  </>;
}

function SchemaField({
  name, schema, value, required, context, onChange,
}: {
  name: string;
  schema: JsonSchema;
  value: unknown;
  required: boolean;
  context: JsonObject;
  onChange: (value: unknown) => void;
}) {
  const label = schemaLabel(name, schema);
  const fieldType = Array.isArray(schema.type) ? "scalar" : schema.type;
  if (fieldType === "object" && schema.properties && Object.keys(schema.properties).length > 0) {
    const objectValue = value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
    return <fieldset className="schema-field-group">
      <legend>{label}{required ? " *" : ""}</legend>
      {schema.description && <small className="schema-field-help">{schema.description}</small>}
      <SchemaObjectFields
        schema={schema}
        value={objectValue}
        onChange={onChange}
      />
    </fieldset>;
  }
  if (fieldType === "object") {
    return <label>{label}{required ? " *" : ""}
      {schema.description && <small className="schema-field-help">{schema.description}</small>}
      <JsonObjectEditor value={value} onChange={onChange} />
    </label>;
  }
  if (schema.enum) {
    const selected = value === undefined ? schema.default ?? (required ? schema.enum[0] : null) : value;
    return <label>{label}{required ? " *" : ""}
      {schema.description && <small className="schema-field-help">{schema.description}</small>}
      <select value={JSON.stringify(selected)} onChange={(event) => onChange(JSON.parse(event.target.value))}>
        {!required && <option value="null">Не задано</option>}
        {schema.enum.map((option) => <option key={JSON.stringify(option)} value={JSON.stringify(option)}>{String(option)}</option>)}
      </select>
    </label>;
  }
  const optionSource = schema["x-ui-options-by"];
  const suggestedOptions = optionSource
    ? optionSource.values[String(context[optionSource.field] ?? "")] ?? []
    : [];
  if (suggestedOptions.length > 0) {
    return <label>{label}{required ? " *" : ""}
      {schema.description && <small className="schema-field-help">{schema.description}</small>}
      <select
        value={String(value ?? schema.default ?? suggestedOptions[0])}
        required={required}
        onChange={(event) => onChange(event.target.value)}
      >{suggestedOptions.map((option) => <option key={option} value={option}>{option}</option>)}</select>
    </label>;
  }
  if (fieldType === "array") {
    const entries = Array.isArray(value) ? value.map(String) : [];
    if (schema.items?.enum) {
      return <fieldset className="schema-field-group schema-choice-list">
        <legend>{label}{required ? " *" : ""}</legend>
        {schema.description && <small className="schema-field-help">{schema.description}</small>}
        {schema.items.enum.map((option) => {
          const item = String(option);
          return <label className="schema-checkbox" key={item}>
            <input
              type="checkbox"
              checked={entries.includes(item)}
              onChange={(event) => onChange(event.target.checked
                ? [...entries, item]
                : entries.filter((entry) => entry !== item))}
            />
            {item}
          </label>;
        })}
      </fieldset>;
    }
    return <label>{label}{required ? " *" : ""}
      {schema.description && <small className="schema-field-help">{schema.description}</small>}
      <textarea
        value={entries.join("\n")}
        onChange={(event) => onChange(event.target.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))}
      />
    </label>;
  }
  if (fieldType === "integer" || fieldType === "number") {
    return <label>{label}{required ? " *" : ""}
      {schema.description && <small className="schema-field-help">{schema.description}</small>}
      <input
        type="number"
        step={fieldType === "integer" ? 1 : "any"}
        min={schema.minimum}
        max={schema.maximum}
        value={typeof value === "number" ? value : Number(schema.default ?? 0)}
        onChange={(event) => onChange(fieldType === "integer" ? Number.parseInt(event.target.value, 10) : Number(event.target.value))}
      />
    </label>;
  }
  if (fieldType === "boolean") {
    return <label className="schema-checkbox">
      <input type="checkbox" checked={Boolean(value ?? schema.default)} onChange={(event) => onChange(event.target.checked)} />
      {label}{required ? " *" : ""}
    </label>;
  }
  if (fieldType === "scalar") {
    return <label>{label}{required ? " *" : ""}
      {schema.description && <small className="schema-field-help">{schema.description}</small>}
      <input
        value={value === null ? "null" : String(value ?? "")}
        onChange={(event) => {
          const raw = event.target.value.trim();
          if (raw === "null") onChange(null);
          else if (raw === "true" || raw === "false") onChange(raw === "true");
          else if (raw !== "" && Number.isFinite(Number(raw))) onChange(Number(raw));
          else onChange(event.target.value);
        }}
      />
    </label>;
  }
  const Input = schema["x-ui-widget"] === "textarea" ? "textarea" : "input";
  return <label>{label}{required ? " *" : ""}
    {schema.description && <small className="schema-field-help">{schema.description}</small>}
    <Input
      value={String(value ?? schema.default ?? "")}
      required={required}
      minLength={schema.minLength}
      maxLength={schema.maxLength}
      onChange={(event) => onChange(event.target.value)}
    />
  </label>;
}

function SchemaObjectFields({
  schema, value, onChange,
}: {
  schema: JsonSchema;
  value: JsonObject;
  onChange: (value: JsonObject) => void;
}) {
  const required = new Set(schema.required ?? []);
  return <>
    {Object.entries(schema.properties ?? {}).map(([name, field]) => (
      <SchemaField
        key={name}
        name={name}
        schema={field}
        value={value[name]}
        required={required.has(name)}
        context={value}
        onChange={(next) => onChange({ ...value, [name]: next })}
      />
    ))}
  </>;
}

function InputMappingEditor({
  value, onChange,
}: {
  value: Record<string, string>;
  onChange: (value: Record<string, string>) => void;
}) {
  const entries = Object.entries(value);
  const replaceKey = (oldKey: string, newKey: string) => {
    const next: Record<string, string> = {};
    for (const [key, expression] of entries) next[key === oldKey ? newKey : key] = expression;
    onChange(next);
  };
  return <fieldset className="schema-field-group input-mapping-editor">
    <legend>Входные данные</legend>
    <small className="schema-field-help">Ключ назначения и выражение вида {"${{ inputs.value }}"}.</small>
    {entries.map(([key, expression], index) => <div className="input-mapping-row" key={index}>
      <input aria-label="Ключ входного параметра" value={key} onChange={(event) => replaceKey(key, event.target.value)} placeholder="parameter" />
      <input aria-label="Выражение входного параметра" value={expression} onChange={(event) => onChange({ ...value, [key]: event.target.value })} placeholder="${{ inputs.value }}" />
      <button type="button" onClick={() => onChange(Object.fromEntries(entries.filter(([item]) => item !== key)))} aria-label={`Удалить binding ${key}`}>×</button>
    </div>)}
    <button
      type="button"
      className="schema-add-row"
      onClick={() => {
        let index = entries.length + 1;
        while (`input_${index}` in value) index += 1;
        onChange({ ...value, [`input_${index}`]: "${{ inputs.value }}" });
      }}
    >Добавить binding</button>
  </fieldset>;
}

export type FlowCatalogs = {
  operations: OperationDefinition[];
  models: AgentModelDefinition[];
  plugins: Extension[];
  credentials: CredentialReference[];
};

function resolveNodeConfigurationSchema(
  schema: JsonSchema,
  config: JsonObject,
  catalogs: FlowCatalogs,
): JsonSchema {
  const properties = Object.fromEntries(Object.entries(schema.properties ?? {}).map(([name, field]) => {
    let resolved = { ...field };
    if (field["x-ui-catalog"] === "operations") {
      resolved.enum = catalogs.operations.map((item) => item.id);
    } else if (field["x-ui-catalog"] === "models") {
      resolved.enum = catalogs.models
        .filter((item) => item.provider === config.provider)
        .map((item) => item.id);
    } else if (field["x-ui-catalog"] === "plugins") {
      resolved.items = {
        ...(field.items ?? {}),
        enum: catalogs.plugins
          .filter((item) => item.enabled && !item.mandatory)
          .map((item) => item.name),
      };
    } else if (field["x-ui-catalog"] === "credentials") {
      const filterField = field["x-ui-filter-by"] ?? "provider";
      resolved.enum = catalogs.credentials
        .filter((item) => item.provider === config[filterField])
        .map((item) => item.id);
    }
    const source = field["x-ui-schema-from"];
    if (source?.catalog === "operations") {
      const operation = catalogs.operations.find((item) => item.id === config[source.selector]);
      if (operation) resolved = { ...resolved, ...operation[source.field], title: field.title, description: operation.description };
    }
    return [name, resolved];
  }));
  return { ...schema, properties };
}

export function FlowBuilder({
  flow, nodeTypes, catalogs, busy, feedback, onClone, onSave, onValidate, onPublish, onClose,
}: {
  flow: FlowDefinition;
  nodeTypes: FlowNodeType[];
  catalogs: FlowCatalogs;
  busy: boolean;
  feedback: { kind: "success" | "error"; message: string } | null;
  onClone: () => void;
  onSave: (changes: FlowDraftChanges) => void;
  onValidate: () => void;
  onPublish: () => void;
  onClose: () => void;
}) {
  const [selectedNodeId, setSelectedNodeId] = useState(flow.start_node);
  const [draftTitle, setDraftTitle] = useState(flow.title);
  const [draftDescription, setDraftDescription] = useState(flow.description ?? "");
  const [draftStartNode, setDraftStartNode] = useState(flow.start_node);
  const [draftNodes, setDraftNodes] = useState(flow.nodes);
  const [draftEdges, setDraftEdges] = useState(flow.edges);
  const [dirty, setDirty] = useState(false);
  const [drag, setDrag] = useState<{
    nodeId: string; clientX: number; clientY: number; originX: number; originY: number;
  } | null>(null);
  const [paletteDragType, setPaletteDragType] = useState<FlowNode["type"] | null>(null);
  const [connectionDrag, setConnectionDrag] = useState<{ nodeId: string; port: string } | null>(null);
  const [connectionPointer, setConnectionPointer] = useState<{ x: number; y: number } | null>(null);
  const historyRef = useRef<FlowEditorSnapshot[]>([]);
  const futureRef = useRef<FlowEditorSnapshot[]>([]);
  const [historyState, setHistoryState] = useState({ canUndo: false, canRedo: false });
  useEffect(() => {
    const sync = window.setTimeout(() => {
      setSelectedNodeId(flow.start_node);
      setDraftTitle(flow.title);
      setDraftDescription(flow.description ?? "");
      setDraftStartNode(flow.start_node);
      setDraftNodes(flow.nodes);
      setDraftEdges(flow.edges);
      setDirty(false);
      setConnectionDrag(null);
      setConnectionPointer(null);
      historyRef.current = [];
      futureRef.current = [];
      setHistoryState({ canUndo: false, canRedo: false });
    }, 0);
    return () => window.clearTimeout(sync);
  }, [flow.id, flow.revision, flow.title, flow.description, flow.start_node, flow.nodes, flow.edges]);
  const selectedNode = draftNodes.find((node) => node.id === selectedNodeId) ?? draftNodes[0];
  const selectedNodeType = nodeTypes.find((item) => item.type === selectedNode?.type);
  const selectedConfigurationSchema = selectedNodeType && selectedNode
    ? resolveNodeConfigurationSchema(selectedNodeType.config_schema, selectedNode.config, catalogs)
    : null;
  const nodeById = new Map(draftNodes.map((node) => [node.id, node]));
  const hasTrigger = draftNodes.some((node) => node.type === "trigger");
  const canvasWidth = Math.max(900, Math.max(...draftNodes.map((node) => node.position.x), 0) + FLOW_NODE_WIDTH + 80);
  const canvasHeight = Math.max(480, Math.max(...draftNodes.map((node) => node.position.y), 0) + FLOW_NODE_HEIGHT + 80);
  const editing = !flow.read_only;
  const transitionPorts = selectedNode?.type === "trigger"
    ? ["EVENT"]
    : selectedNode?.type === "terminal" ? [] : ["SUCCESS", "FAILURE"];
  const { canUndo, canRedo } = historyState;

  const currentSnapshot = (): FlowEditorSnapshot => ({
    title: draftTitle,
    description: draftDescription,
    startNode: draftStartNode,
    nodes: draftNodes,
    edges: draftEdges,
  });
  const restoreSnapshot = (snapshot: FlowEditorSnapshot) => {
    setDraftTitle(snapshot.title);
    setDraftDescription(snapshot.description);
    setDraftStartNode(snapshot.startNode);
    setDraftNodes(snapshot.nodes);
    setDraftEdges(snapshot.edges);
    setDirty(true);
  };
  const rememberSnapshot = () => {
    const snapshot = currentSnapshot();
    const previous = historyRef.current.at(-1);
    if (!previous || JSON.stringify(previous) !== JSON.stringify(snapshot)) {
      historyRef.current = [...historyRef.current.slice(-49), snapshot];
    }
    futureRef.current = [];
    setHistoryState({ canUndo: historyRef.current.length > 0, canRedo: false });
  };
  const undo = () => {
    const previous = historyRef.current.pop();
    if (!previous) return;
    futureRef.current.push(currentSnapshot());
    restoreSnapshot(previous);
    setHistoryState({ canUndo: historyRef.current.length > 0, canRedo: true });
  };
  const redo = () => {
    const next = futureRef.current.pop();
    if (!next) return;
    historyRef.current.push(currentSnapshot());
    restoreSnapshot(next);
    setHistoryState({ canUndo: true, canRedo: futureRef.current.length > 0 });
  };

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!editing || !(event.ctrlKey || event.metaKey)) return;
      if (event.key.toLowerCase() === "z" && !event.shiftKey) {
        event.preventDefault();
        undo();
      } else if (event.key.toLowerCase() === "y" || (event.key.toLowerCase() === "z" && event.shiftKey)) {
        event.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  });

  const markMetadataDirty = (setter: (value: string) => void, value: string) => {
    rememberSnapshot();
    setter(value);
    setDirty(true);
  };
  const updateNode = (nodeId: string, update: Partial<FlowNode>, remember = true) => {
    if (remember) rememberSnapshot();
    setDraftNodes((nodes) => nodes.map((node) => node.id === nodeId ? { ...node, ...update } : node));
    setDirty(true);
  };
  const updateNodeConfiguration = (node: FlowNode, config: JsonObject) => {
    if (node.type === "trigger" && config.source !== node.config.source) {
      const eventSchema = selectedNodeType?.config_schema.properties?.event;
      const options = eventSchema?.["x-ui-options-by"]?.values[String(config.source)] ?? [];
      config = { ...config, event: options[0] ?? "" };
    }
    if (node.type === "command" && config.command !== node.config.command) {
      config = { ...config, parameters: {} };
    }
    if (node.type === "agent" && config.provider !== node.config.provider) {
      const model = catalogs.models.find((item) => item.provider === config.provider);
      config = { ...config, model: model?.id ?? "", credential_id: null };
    }
    if (node.type === "review" && config.provider !== node.config.provider) {
      config = { ...config, credential_id: null };
    }
    let subtitle = node.subtitle;
    if (node.type === "trigger") subtitle = `${String(config.source ?? "")} · ${String(config.event ?? "")}`;
    if (node.type === "agent") subtitle = `${String(config.provider)} · ${String(config.model)}`;
    if (node.type === "command") subtitle = String(config.command);
    if (node.type === "review") subtitle = `${String(config.provider)} · ${String(config.decision)}`;
    if (node.type === "if") subtitle = String(config.condition);
    if (node.type === "switch") subtitle = `match · ${String(config.equals)}`;
    if (node.type === "delay") subtitle = `${String(config.seconds)}s`;
    if (node.type === "merge") subtitle = String(config.mode);
    if (node.type === "terminal") subtitle = String(config.outcome);
    updateNode(node.id, { config, subtitle });
  };
  const transitionTarget = (nodeId: string, port: string) =>
    draftEdges.find((edge) => edge.source === nodeId && edge.source_port === port)?.target ?? "";
  const setTransition = (node: FlowNode, port: string, target: string) => {
    rememberSnapshot();
    setDraftEdges((edges) => {
      const remaining = edges.filter((edge) => !(edge.source === node.id && edge.source_port === port));
      if (!target) return remaining;
      const outcome = port === "SUCCESS" || port === "FAILURE" ? port : null;
      return [...remaining, {
        id: `${node.id}:${port}:${target}`,
        source: node.id,
        source_port: port,
        target,
        label: port,
        kind: port === "EVENT" ? "event" : "transition",
        outcome,
      }];
    });
    if (node.type === "trigger" && target) setDraftStartNode(target);
    setDirty(true);
  };
  const addNode = (type: FlowNode["type"], position?: FlowNode["position"]) => {
    if (!editing || (type === "trigger" && hasTrigger)) return;
    rememberSnapshot();
    let sequence = 1;
    while (draftNodes.some((node) => node.id === `${type}-${sequence}`)) sequence += 1;
    const id = type === "trigger" ? "__trigger__" : `${type}-${sequence}`;
    const initialModel = catalogs.models.find((item) => item.default) ?? catalogs.models[0];
    const initialOperation = catalogs.operations[0]?.id ?? "complete";
    const configs: Record<FlowNode["type"], JsonObject> = {
      trigger: { source: "manual", event: "flow.run" },
      agent: { prompt: "Опишите задачу агента", plugins: [], provider: initialModel?.provider ?? "openrouter", model: initialModel?.id ?? "", timeout_seconds: 600, retry: { max_attempts: 1, delay_seconds: 5, backoff: "exponential", max_delay_seconds: 300 }, result_contract: "none" },
      command: { command: initialOperation, parameters: {}, retry: { max_attempts: 1, delay_seconds: 5, backoff: "exponential", max_delay_seconds: 300 } },
      review: { provider: "gitea", decision: "review", retry: { max_attempts: 1, delay_seconds: 5, backoff: "exponential", max_delay_seconds: 300 } },
      if: { condition: "${{ inputs.enabled }}", retry: { max_attempts: 1, delay_seconds: 5, backoff: "exponential", max_delay_seconds: 300 } },
      switch: { value: "${{ inputs.value }}", equals: "expected", retry: { max_attempts: 1, delay_seconds: 5, backoff: "exponential", max_delay_seconds: 300 } },
      delay: { seconds: 5, retry: { max_attempts: 1, delay_seconds: 5, backoff: "exponential", max_delay_seconds: 300 } },
      merge: { mode: "any", retry: { max_attempts: 1, delay_seconds: 5, backoff: "exponential", max_delay_seconds: 300 } },
      terminal: { outcome: "SUCCESS" },
    };
    const subtitles: Record<FlowNode["type"], string> = { trigger: "manual · flow.run", agent: `${initialModel?.provider ?? "openrouter"} · ${initialModel?.id ?? ""}`, command: initialOperation, review: "gitea · review", if: "inputs.enabled", switch: "match · expected", delay: "5s", merge: "any", terminal: "Сценарий завершён" };
    const node: FlowNode = {
      id,
      type,
      category: type === "trigger" ? "trigger" : type === "merge" ? "data" : ["review", "if", "switch", "delay"].includes(type) ? "control" : type === "terminal" ? "terminal" : "execution",
      title: type === "trigger" ? "Trigger" : id,
      subtitle: subtitles[type],
      config: configs[type],
      input_mapping: {},
      position: position ?? { x: 320 + (draftNodes.length % 3) * 280, y: 40 + (draftNodes.length % 4) * 120 },
      read_only: false,
    };
    setDraftNodes((nodes) => [...nodes, node]);
    setSelectedNodeId(id);
    setDirty(true);
  };
  const deleteNode = (nodeId: string) => {
    if (!editing || nodeId === "__trigger__") return;
    rememberSnapshot();
    const remaining = draftNodes.filter((node) => node.id !== nodeId);
    setDraftNodes(remaining);
    setDraftEdges((edges) => edges.filter((edge) => edge.source !== nodeId && edge.target !== nodeId));
    if (draftStartNode === nodeId) {
      setDraftStartNode(remaining.find((node) => node.type !== "trigger" && node.type !== "terminal")?.id ?? "");
    }
    setSelectedNodeId("__trigger__");
    setDirty(true);
  };
  const connectToNode = (targetNode: FlowNode) => {
    if (!editing || !connectionDrag) return;
    const sourceNode = draftNodes.find((item) => item.id === connectionDrag.nodeId);
    if (sourceNode && sourceNode.id !== targetNode.id) {
      setTransition(sourceNode, connectionDrag.port, targetNode.id);
    }
    setConnectionDrag(null);
    setConnectionPointer(null);
  };

  return (
    <section className="inspection-panel flow-builder" aria-label={`Граф сценария ${flow.id}`}>
      <div className="inspection-header">
        <div><p className="eyebrow">FLOW BUILDER · {flow.builtin ? "BUILTIN" : "DRAFT"}</p><h3>{flow.title} <span className="mono">{flow.builtin ? `v${flow.version}` : `rev ${flow.revision}`}</span></h3><small>{flow.id} · {flow.builtin ? "встроенный сценарий" : "пользовательский черновик"}</small></div>
        <button type="button" className="close-button" onClick={onClose}>Закрыть ×</button>
      </div>
      <div className="flow-draft-toolbar">
        {flow.builtin ? <>
          <p>Встроенный граф защищён от изменений. Создайте независимую копию для редактирования.</p>
          <button type="button" onClick={onClone} disabled={busy}>Создать копию</button>
        </> : <>
          <label>Название<input value={draftTitle} onChange={(event) => markMetadataDirty(setDraftTitle, event.target.value)} maxLength={200} /></label>
          <label>Описание<input value={draftDescription} onChange={(event) => markMetadataDirty(setDraftDescription, event.target.value)} maxLength={1000} /></label>
          <div className="flow-draft-actions">
            <button type="button" onClick={undo} disabled={busy || !canUndo} title="Отменить изменение (Ctrl+Z)">↶ Undo</button>
            <button type="button" onClick={redo} disabled={busy || !canRedo} title="Вернуть изменение (Ctrl+Y)">↷ Redo</button>
            <button type="button" onClick={() => onSave({ title: draftTitle, description: draftDescription, startNode: draftStartNode, nodes: draftNodes, edges: draftEdges })} disabled={busy || !dirty || !draftTitle.trim()}>Сохранить{dirty ? " *" : ""}</button>
            <button type="button" onClick={onValidate} disabled={busy || dirty} title={dirty ? "Сначала сохраните изменения" : ""}>Проверить</button>
            <button type="button" className="flow-publish" onClick={onPublish} disabled={busy || dirty} title={dirty ? "Сначала сохраните изменения" : ""}>Опубликовать</button>
          </div>
        </>}
        {feedback && <p className={`flow-feedback is-${feedback.kind}`}>{feedback.message}</p>}
      </div>
      <div className="flow-builder-layout">
        <aside className="flow-palette" aria-label="Типы узлов">
          <div className="section-label">Компоненты · {nodeTypes.length}</div>
          {nodeTypes.map((item) => (
            <article
              key={item.type}
              className={editing && (item.type !== "trigger" || !hasTrigger) ? "is-draggable" : ""}
              draggable={editing && (item.type !== "trigger" || !hasTrigger)}
              onDragStart={(event) => {
                if (!editing || (item.type === "trigger" && hasTrigger)) {
                  event.preventDefault();
                  return;
                }
                event.dataTransfer.effectAllowed = "copy";
                event.dataTransfer.setData("application/x-automation-node", item.type);
                event.dataTransfer.setData("text/plain", item.type);
                setPaletteDragType(item.type);
              }}
              onDragEnd={() => setPaletteDragType(null)}
            >
              <span className={`flow-type-icon type-${item.type}`}>{item.title.slice(0, 2).toUpperCase()}</span>
              <div><strong>{item.title}</strong><small>{item.description}</small></div>
              <span className="flow-palette-action">{item.type === "trigger" && hasTrigger ? "в графе" : editing ? "перетащите" : "read-only"}</span>
            </article>
          ))}
        </aside>
        <div className="flow-canvas-scroll" aria-label="Полотно графа">
          <div
            className={`flow-canvas ${paletteDragType ? "is-drop-target" : ""} ${connectionDrag ? "is-connecting" : ""}`}
            style={{ width: canvasWidth, height: canvasHeight }}
            onPointerMove={(event) => {
              if (!connectionDrag) return;
              const bounds = event.currentTarget.getBoundingClientRect();
              setConnectionPointer({
                x: event.clientX - bounds.left,
                y: event.clientY - bounds.top,
              });
            }}
            onPointerUp={() => {
              setConnectionDrag(null);
              setConnectionPointer(null);
            }}
            onPointerCancel={() => {
              setConnectionDrag(null);
              setConnectionPointer(null);
            }}
            onDragOver={(event) => {
              if (!editing) return;
              event.preventDefault();
              event.dataTransfer.dropEffect = "copy";
            }}
            onDrop={(event) => {
              event.preventDefault();
              if (!editing) return;
              const draggedType = event.dataTransfer.getData("application/x-automation-node")
                || event.dataTransfer.getData("text/plain");
              const nodeType = nodeTypes.find((item) => item.type === draggedType && (item.type !== "trigger" || !hasTrigger));
              if (!nodeType) return;
              const bounds = event.currentTarget.getBoundingClientRect();
              const position = {
                x: Math.max(0, Math.round((event.clientX - bounds.left - FLOW_NODE_WIDTH / 2) / 10) * 10),
                y: Math.max(0, Math.round((event.clientY - bounds.top - FLOW_NODE_HEIGHT / 2) / 10) * 10),
              };
              addNode(nodeType.type, position);
              setPaletteDragType(null);
            }}
          >
            {paletteDragType && <div className="flow-drop-hint">Отпустите, чтобы создать {paletteDragType}</div>}
            <svg aria-hidden="true" width={canvasWidth} height={canvasHeight}>
              <defs>
                <marker id="arrow-event" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z" /></marker>
                <marker id="arrow-success" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z" /></marker>
                <marker id="arrow-failure" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z" /></marker>
              </defs>
              {draftEdges.map((edge) => {
                const source = nodeById.get(edge.source);
                const target = nodeById.get(edge.target);
                if (!source || !target) return null;
                const x1 = source.position.x + FLOW_NODE_WIDTH;
                const y1 = source.position.y + (FLOW_OUTPUT_PORT_Y[edge.source_port] ?? FLOW_NODE_HEIGHT / 2);
                const x2 = target.position.x;
                const y2 = target.position.y + FLOW_NODE_HEIGHT / 2;
                const bend = Math.max(70, Math.abs(x2 - x1) / 2);
                const path = `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
                const tone = edge.outcome?.toLowerCase() ?? "event";
                return <g className={`flow-edge edge-${tone}`} key={edge.id}>
                  <path d={path} markerEnd={`url(#arrow-${tone})`} />
                  <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 7}>{edge.label}</text>
                </g>;
              })}
              {connectionDrag && connectionPointer && (() => {
                const source = nodeById.get(connectionDrag.nodeId);
                if (!source) return null;
                const x1 = source.position.x + FLOW_NODE_WIDTH;
                const y1 = source.position.y + (FLOW_OUTPUT_PORT_Y[connectionDrag.port] ?? FLOW_NODE_HEIGHT / 2);
                const x2 = connectionPointer.x;
                const y2 = connectionPointer.y;
                const bend = Math.max(70, Math.abs(x2 - x1) / 2);
                const tone = connectionDrag.port.toLowerCase();
                return <g className={`flow-edge flow-edge-preview edge-${tone}`}>
                  <path d={`M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`} />
                </g>;
              })()}
            </svg>
            {draftNodes.map((node) => (
              <article
                key={node.id}
                className={`flow-node type-${node.type} ${selectedNode?.id === node.id ? "is-selected" : ""}`}
                style={{ left: node.position.x, top: node.position.y }}
              >
                {node.type !== "trigger" && <button
                  type="button"
                  className="flow-port flow-port-input"
                  aria-label={`Вход узла ${node.title}`}
                  title="Перетащите сюда связь"
                  onPointerDown={(event) => {
                    if (!connectionDrag) return;
                    event.preventDefault();
                    event.stopPropagation();
                  }}
                  onPointerUp={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    connectToNode(node);
                  }}
                />}
                {(node.type === "trigger" ? ["EVENT"] : node.type === "terminal" ? [] : ["SUCCESS", "FAILURE"]).map((port) => <button
                  type="button"
                  className={`flow-port flow-port-output port-${port.toLowerCase()} ${connectionDrag?.nodeId === node.id && connectionDrag.port === port ? "is-active" : ""}`}
                  key={port}
                  aria-label={`${port} узла ${node.title}`}
                  title={`Перетащите ${port} на вход другого узла`}
                  onPointerDown={(event) => {
                    if (!editing) {
                      event.preventDefault();
                      return;
                    }
                    event.preventDefault();
                    event.stopPropagation();
                    const payload = { nodeId: node.id, port };
                    setConnectionDrag(payload);
                    setConnectionPointer({
                      x: node.position.x + FLOW_NODE_WIDTH,
                      y: node.position.y + (FLOW_OUTPUT_PORT_Y[port] ?? FLOW_NODE_HEIGHT / 2),
                    });
                  }}
                />)}
                <button
                  type="button"
                  className="flow-node-body"
                  onClick={() => setSelectedNodeId(node.id)}
                  onPointerDown={(event) => {
                    if (!editing || event.button !== 0) return;
                    rememberSnapshot();
                    event.currentTarget.setPointerCapture(event.pointerId);
                    setDrag({ nodeId: node.id, clientX: event.clientX, clientY: event.clientY, originX: node.position.x, originY: node.position.y });
                  }}
                  onPointerMove={(event) => {
                    if (!drag || drag.nodeId !== node.id) return;
                    const x = Math.max(0, Math.round((drag.originX + event.clientX - drag.clientX) / 10) * 10);
                    const y = Math.max(0, Math.round((drag.originY + event.clientY - drag.clientY) / 10) * 10);
                    updateNode(node.id, { position: { x, y } }, false);
                  }}
                  onPointerUp={(event) => {
                    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
                    setDrag(null);
                  }}
                  aria-pressed={selectedNode?.id === node.id}
                >
                  <span className="flow-node-type">{node.type}</span>
                  <strong>{node.title}</strong>
                  <small>{node.subtitle}</small>
                </button>
              </article>
            ))}
          </div>
        </div>
        <aside className="flow-inspector" aria-label="Настройки выбранного узла">
          <div className="section-label">Инспектор узла</div>
          {selectedNode && <>
            <div className="flow-inspector-heading"><span className={`flow-type-icon type-${selectedNode.type}`}>{selectedNode.type.slice(0, 2).toUpperCase()}</span><div><strong>{selectedNode.title}</strong><small>{selectedNode.id}</small></div></div>
            <dl><div><dt>Тип</dt><dd>{selectedNode.type}</dd></div><div><dt>Категория</dt><dd>{selectedNode.category}</dd></div><div><dt>Режим</dt><dd>{flow.builtin ? "только чтение" : "черновик"}</dd></div></dl>
            {editing && <div className="flow-node-form">
              <label>Название узла<input value={selectedNode.title} onChange={(event) => updateNode(selectedNode.id, { title: event.target.value })} maxLength={120} /></label>
              {selectedConfigurationSchema
                ? <SchemaObjectFields schema={selectedConfigurationSchema} value={selectedNode.config} onChange={(config) => updateNodeConfiguration(selectedNode, config)} />
                : <p className="schema-field-error">Схема конфигурации этого узла недоступна.</p>}
              {!(["trigger", "terminal"] as FlowNode["type"][]).includes(selectedNode.type) && <InputMappingEditor value={selectedNode.input_mapping} onChange={(input_mapping) => updateNode(selectedNode.id, { input_mapping })} />}
              {transitionPorts.length > 0 && <fieldset><legend>Переходы</legend>{transitionPorts.map((port) => <label key={port}>{port}<select value={transitionTarget(selectedNode.id, port)} onChange={(event) => setTransition(selectedNode, port, event.target.value)}><option value="">Не задан</option>{draftNodes.filter((node) => node.type !== "trigger").map((node) => <option value={node.id} key={node.id}>{node.title} · {node.id}</option>)}</select></label>)}</fieldset>}
              {selectedNode.type !== "trigger" && <button type="button" className="flow-delete-node" onClick={() => deleteNode(selectedNode.id)}>Удалить узел</button>}
            </div>}
            <details className="flow-json-preview"><summary>JSON конфигурации</summary><JsonBlock value={selectedNode.config} /></details>
          </>}
        </aside>
      </div>
    </section>
  );
}

export async function putJson<T>(path: string, body: JsonObject): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `${path}: HTTP ${response.status}`;
    try {
      const payload = await response.json() as { detail?: unknown };
      if (typeof payload.detail === "string") message = payload.detail;
    } catch {
      // Keep the HTTP status when the response has no JSON body.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export default function Dashboard() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [filter, setFilter] = useState<Status | "ALL">("ALL");
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [analysisTitle, setAnalysisTitle] = useState("");
  const [analysisRequest, setAnalysisRequest] = useState("");
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const [analysisFeedback, setAnalysisFeedback] = useState<{
    kind: "success" | "error";
    message: string;
  } | null>(null);
  const [bugRepository, setBugRepository] = useState("");
  const [bugRef, setBugRef] = useState("main");
  const [bugScope, setBugScope] = useState("");
  const [bugSymptoms, setBugSymptoms] = useState("");
  const [bugFindingBusy, setBugFindingBusy] = useState(false);
  const [bugFindingFeedback, setBugFindingFeedback] = useState<{
    kind: "success" | "error";
    message: string;
  } | null>(null);
  const [actionFeedback, setActionFeedback] = useState<{
    workflowId: string;
    kind: "success" | "error";
    message: string;
  } | null>(null);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const [health, workflows, scenarios, flows, plugins, images] = await Promise.all([
        getJson<Health>("/health"), getJson<Workflow[]>("/v1/workflows"), getJson<Scenario[]>("/v1/scenarios"),
        getJson<FlowDefinition[]>("/v1/flows"),
        getJson<Extension[]>("/v1/plugins"), getJson<ImageProfile[]>("/v1/images"),
      ]);
      setSnapshot({ health, workflows, scenarios, flows, plugins, images });
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

  const submitAnalysis = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const request = analysisRequest.trim();
    if (request.length < 3) {
      setAnalysisFeedback({ kind: "error", message: "Опишите аналитическую задачу." });
      return;
    }
    setAnalysisBusy(true);
    setAnalysisFeedback(null);
    try {
      const workflow = await postJson<Workflow>("/v1/analysis", {
        request,
        ...(analysisTitle.trim() ? { title: analysisTitle.trim() } : {}),
      });
      setSnapshot((current) => current ? {
        ...current,
        workflows: [workflow, ...current.workflows.filter((item) => item.id !== workflow.id)],
      } : current);
      setSelectedWorkflowId(workflow.id);
      setAnalysisRequest("");
      setAnalysisTitle("");
      setAnalysisFeedback({
        kind: "success",
        message: "Аналитическая задача поставлена в очередь.",
      });
      await load();
    } catch (reason) {
      setAnalysisFeedback({
        kind: "error",
        message: reason instanceof Error ? reason.message : "Не удалось создать задачу",
      });
    } finally {
      setAnalysisBusy(false);
    }
  }, [analysisRequest, analysisTitle, load]);

  const submitBugFinding = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const repository = bugRepository.trim();
    const ref = bugRef.trim();
    const scope = bugScope.trim();
    if (!repository.includes("/") || !ref || scope.length < 3) {
      setBugFindingFeedback({
        kind: "error",
        message: "Укажите репозиторий owner/name, Git ref и область поиска.",
      });
      return;
    }
    setBugFindingBusy(true);
    setBugFindingFeedback(null);
    try {
      const workflow = await postJson<Workflow>("/v1/bug-finding", {
        repository,
        ref,
        scope,
        ...(bugSymptoms.trim() ? { symptoms: bugSymptoms.trim() } : {}),
      });
      setSnapshot((current) => current ? {
        ...current,
        workflows: [workflow, ...current.workflows.filter((item) => item.id !== workflow.id)],
      } : current);
      setSelectedWorkflowId(workflow.id);
      setBugScope("");
      setBugSymptoms("");
      setBugFindingFeedback({
        kind: "success",
        message: "Поиск дефектов поставлен в очередь.",
      });
      await load();
    } catch (reason) {
      setBugFindingFeedback({
        kind: "error",
        message: reason instanceof Error ? reason.message : "Не удалось запустить поиск дефектов",
      });
    } finally {
      setBugFindingBusy(false);
    }
  }, [bugRef, bugRepository, bugScope, bugSymptoms, load]);

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

      <section className="panel analysis-panel" aria-labelledby="analysis-heading">
        <div className="analysis-copy">
          <p className="eyebrow">АНАЛИТИКА</p>
          <h2 id="analysis-heading">Новый аналитический документ</h2>
          <p>Опишите задачу. Агент изучит доступную документацию и подготовит Markdown-файл.</p>
        </div>
        <form className="analysis-form" onSubmit={(event) => void submitAnalysis(event)}>
          <label htmlFor="analysis-title">Название документа <span>необязательно</span></label>
          <input id="analysis-title" value={analysisTitle} onChange={(event) => setAnalysisTitle(event.target.value)} maxLength={300} placeholder="Например: Требования к модулю аналитики" />
          <label htmlFor="analysis-request">Что нужно изучить и подготовить</label>
          <textarea id="analysis-request" value={analysisRequest} onChange={(event) => setAnalysisRequest(event.target.value)} minLength={3} maxLength={20_000} required placeholder="Изучи документацию по процессу обработки заявок и составь функциональные и нефункциональные требования…" />
          <div className="analysis-submit-row">
            <small>Результат появится в деталях workflow как документ .md</small>
            <button type="submit" disabled={analysisBusy || analysisRequest.trim().length < 3}>{analysisBusy ? "Создаём…" : "Запустить анализ"}</button>
          </div>
          {analysisFeedback && <p className={`analysis-feedback is-${analysisFeedback.kind}`} role="status">{analysisFeedback.message}</p>}
        </form>
      </section>

      <section className="panel analysis-panel" aria-labelledby="bug-finding-heading">
        <div className="analysis-copy">
          <p className="eyebrow">ПОИСК ОШИБОК</p>
          <h2 id="bug-finding-heading">Проверить точную ревизию</h2>
          <p>Агент не меняет продукт, а формирует отчёт и независимо проверяемые reproducer-тесты.</p>
        </div>
        <form className="analysis-form" onSubmit={(event) => void submitBugFinding(event)}>
          <label htmlFor="bug-repository">Репозиторий Gitea</label>
          <input id="bug-repository" value={bugRepository} onChange={(event) => setBugRepository(event.target.value)} maxLength={300} required placeholder="team/service" />
          <label htmlFor="bug-ref">Git ref</label>
          <input id="bug-ref" value={bugRef} onChange={(event) => setBugRef(event.target.value)} maxLength={300} required placeholder="main или commit" />
          <label htmlFor="bug-scope">Область поиска</label>
          <textarea id="bug-scope" value={bugScope} onChange={(event) => setBugScope(event.target.value)} minLength={3} maxLength={20_000} required placeholder="Проверь обработку повторных платежей и идемпотентность…" />
          <label htmlFor="bug-symptoms">Наблюдаемые симптомы <span>необязательно</span></label>
          <textarea id="bug-symptoms" value={bugSymptoms} onChange={(event) => setBugSymptoms(event.target.value)} maxLength={20_000} placeholder="После таймаута платёж иногда создаётся дважды" />
          <div className="analysis-submit-row">
            <small>Каждый найденный дефект должен подтверждаться изолированным reproducer-тестом</small>
            <button type="submit" disabled={bugFindingBusy || bugScope.trim().length < 3}>{bugFindingBusy ? "Запускаем…" : "Запустить поиск"}</button>
          </div>
          {bugFindingFeedback && <p className={`analysis-feedback is-${bugFindingFeedback.kind}`} role="status">{bugFindingFeedback.message}</p>}
        </form>
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
            {snapshot?.flows.map((flowItem, index) => (
              <Link href={`/flows?flow=${encodeURIComponent(flowItem.id)}`} key={flowItem.id}>
                <span className="catalog-index">{String(index + 1).padStart(2, "0")}</span><span><strong>{flowItem.title}</strong><small>{scenarioStageLabels[flowItem.stage]} · {flowItem.id}</small></span><span>{flowItem.builtin ? "builtin" : `draft r${flowItem.revision}`}</span><span className="catalog-action">открыть ↗</span>
              </Link>
            ))}
            {snapshot?.flows.length === 0 && <div className="empty-inline">Нет сценариев</div>}
          </div>
          <div className="image-list">{snapshot?.images.map((image) => <article key={image.name}><div className="image-title"><span className="cube" aria-hidden="true" /><div><strong>{image.name}</strong><small>{image.image}</small></div></div><div className="capability-row">{image.capabilities.map((capability) => <span key={capability}>{capability}</span>)}</div><small>Harness {image.harness_version}</small></article>)}</div>
        </div>
      </section>

      <footer><span>LOCAL CONTROL PLANE</span><span>Данные остаются внутри Docker-контура</span></footer>
    </main>
  );
}
