"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import {
  FlowBuilder,
  getJson,
  postJson,
  putJson,
  type AgentModelDefinition,
  type CredentialReference,
  type Extension,
  type FlowDefinition,
  type FlowDraftChanges,
  type FlowNodeType,
  type FlowRun,
  type FlowValidationResult,
  type FlowVersion,
  type OperationDefinition,
} from "../page";

type BuilderData = {
  flows: FlowDefinition[];
  nodeTypes: FlowNodeType[];
  operations: OperationDefinition[];
  models: AgentModelDefinition[];
  credentials: CredentialReference[];
  plugins: Extension[];
};

type Feedback = {
  flowId: string;
  kind: "success" | "error";
  message: string;
};

export default function FlowsPage() {
  const [data, setData] = useState<BuilderData | null>(null);
  const [selectedFlowId, setSelectedFlowId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [validation, setValidation] = useState<{ flowId: string; result: FlowValidationResult } | null>(null);
  const [versions, setVersions] = useState<Record<string, FlowVersion[]>>({});
  const [runInputs, setRunInputs] = useState("{}");
  const [runResult, setRunResult] = useState<FlowRun | null>(null);

  const selectFlow = useCallback((flowId: string | null) => {
    setSelectedFlowId(flowId);
    const url = new URL(window.location.href);
    if (flowId) url.searchParams.set("flow", flowId);
    else url.searchParams.delete("flow");
    window.history.replaceState({}, "", url);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [flows, nodeTypes, operations, models, credentials, plugins] = await Promise.all([
        getJson<FlowDefinition[]>("/v1/flows"),
        getJson<FlowNodeType[]>("/v1/node-types"),
        getJson<OperationDefinition[]>("/v1/operations"),
        getJson<AgentModelDefinition[]>("/v1/models"),
        getJson<CredentialReference[]>("/v1/credentials"),
        getJson<Extension[]>("/v1/plugins"),
      ]);
      setData({ flows, nodeTypes, operations, models, credentials, plugins });
      setSelectedFlowId((current) => {
        if (current && flows.some((flow) => flow.id === current)) return current;
        const requested = new URLSearchParams(window.location.search).get("flow");
        return flows.some((flow) => flow.id === requested) ? requested : flows[0]?.id ?? null;
      });
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "API недоступен");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(initial);
  }, [load]);

  useEffect(() => {
    if (!selectedFlowId || versions[selectedFlowId]) return;
    let active = true;
    void getJson<FlowVersion[]>(`/v1/flows/${encodeURIComponent(selectedFlowId)}/versions`)
      .then((items) => {
        if (active) setVersions((current) => ({ ...current, [selectedFlowId]: items }));
      })
      .catch(() => {
        if (active) setVersions((current) => ({ ...current, [selectedFlowId]: [] }));
      });
    return () => { active = false; };
  }, [selectedFlowId, versions]);

  const cloneFlow = useCallback(async (source: FlowDefinition) => {
    setBusy(true);
    setFeedback(null);
    try {
      const created = await postJson<FlowDefinition>("/v1/flows", { source_flow_id: source.id });
      setData((current) => current ? {
        ...current,
        flows: [...current.flows.filter((flow) => flow.id !== created.id), created],
      } : current);
      selectFlow(created.id);
      setFeedback({ flowId: created.id, kind: "success", message: "Черновик создан и готов к редактированию." });
    } catch (reason) {
      setFeedback({ flowId: source.id, kind: "error", message: reason instanceof Error ? reason.message : "Не удалось создать копию" });
    } finally {
      setBusy(false);
    }
  }, [selectFlow]);

  const createBlankFlow = useCallback(async () => {
    setBusy(true);
    setFeedback(null);
    try {
      const created = await postJson<FlowDefinition>("/v1/flows", {
        title: "Новый workflow",
        stage: "operations",
      });
      setData((current) => current ? {
        ...current,
        flows: [created, ...current.flows.filter((flow) => flow.id !== created.id)],
      } : current);
      selectFlow(created.id);
      setFeedback({
        flowId: created.id,
        kind: "success",
        message: "Создан пустой черновик. Перетащите Trigger и остальные узлы из палитры.",
      });
    } catch (reason) {
      setFeedback({
        flowId: selectedFlowId ?? "",
        kind: "error",
        message: reason instanceof Error ? reason.message : "Не удалось создать пустой workflow",
      });
    } finally {
      setBusy(false);
    }
  }, [selectFlow, selectedFlowId]);

  const saveFlow = useCallback(async (flow: FlowDefinition, changes: FlowDraftChanges) => {
    setBusy(true);
    setFeedback(null);
    try {
      const updated = await putJson<FlowDefinition>(`/v1/flows/${encodeURIComponent(flow.id)}/draft`, {
        expected_revision: flow.revision,
        title: changes.title.trim(),
        description: changes.description.trim() || null,
        stage: flow.stage,
        enabled: flow.enabled,
        start_node: changes.startNode,
        nodes: changes.nodes,
        edges: changes.edges,
      });
      setData((current) => current ? {
        ...current,
        flows: current.flows.map((item) => item.id === updated.id ? updated : item),
      } : current);
      setFeedback({ flowId: updated.id, kind: "success", message: `Черновик сохранён: revision ${updated.revision}.` });
    } catch (reason) {
      setFeedback({ flowId: flow.id, kind: "error", message: reason instanceof Error ? reason.message : "Не удалось сохранить черновик" });
    } finally {
      setBusy(false);
    }
  }, []);

  const validateFlow = useCallback(async (flow: FlowDefinition) => {
    setBusy(true);
    setFeedback(null);
    try {
      const result = await postJson<FlowValidationResult>(`/v1/flows/${encodeURIComponent(flow.id)}/validate`, {});
      setValidation({ flowId: flow.id, result });
      setFeedback({
        flowId: flow.id,
        kind: result.valid ? "success" : "error",
        message: result.valid
          ? `Граф корректен${result.warnings.length ? `, предупреждений: ${result.warnings.length}` : ""}.`
          : `Ошибок валидации: ${result.errors.length}. ${result.errors[0]?.message ?? ""}`,
      });
    } catch (reason) {
      setFeedback({ flowId: flow.id, kind: "error", message: reason instanceof Error ? reason.message : "Проверка не выполнена" });
    } finally {
      setBusy(false);
    }
  }, []);

  const publishFlow = useCallback(async (flow: FlowDefinition) => {
    setBusy(true);
    setFeedback(null);
    try {
      const version = await postJson<FlowVersion>(`/v1/flows/${encodeURIComponent(flow.id)}/publish`, {
        expected_revision: flow.revision,
      });
      setVersions((current) => ({
        ...current,
        [flow.id]: [...(current[flow.id] ?? []).filter((item) => item.version !== version.version), version],
      }));
      setFeedback({ flowId: flow.id, kind: "success", message: `Опубликована версия ${version.version} · ${version.sha256.slice(0, 12)}.` });
    } catch (reason) {
      setFeedback({ flowId: flow.id, kind: "error", message: reason instanceof Error ? reason.message : "Публикация не выполнена" });
    } finally {
      setBusy(false);
    }
  }, []);

  const runFlow = useCallback(async (flow: FlowDefinition, version: number) => {
    setBusy(true);
    setRunResult(null);
    try {
      const parsed = JSON.parse(runInputs) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Inputs должны быть JSON-объектом.");
      }
      const run = await postJson<FlowRun>(`/v1/flows/${encodeURIComponent(flow.id)}/runs`, {
        version,
        inputs: parsed,
      });
      setRunResult(run);
      setFeedback({ flowId: flow.id, kind: "success", message: `Запуск ${run.id} создан для версии ${run.flow_version}.` });
    } catch (reason) {
      setFeedback({ flowId: flow.id, kind: "error", message: reason instanceof Error ? reason.message : "Не удалось запустить flow" });
    } finally {
      setBusy(false);
    }
  }, [runInputs]);

  const selectedFlow = data?.flows.find((flow) => flow.id === selectedFlowId) ?? null;
  const selectedVersions = selectedFlow ? versions[selectedFlow.id] ?? [] : [];
  const latestVersion = selectedVersions.reduce((latest, item) => Math.max(latest, item.version), 0);

  return <main className="flow-workspace-shell">
    <header className="topbar">
      <div className="brand-block">
        <span className="brand-mark" aria-hidden="true">F</span>
        <div><p className="eyebrow">VISUAL AUTOMATION</p><h1>Flow Builder</h1></div>
      </div>
      <nav className="flow-workspace-nav" aria-label="Навигация Flow Builder">
        <Link href="/">← Dashboard</Link>
        <button type="button" onClick={() => void createBlankFlow()} disabled={loading || busy}>＋ Пустой workflow</button>
        <button type="button" onClick={() => void load()} disabled={loading || busy}>Обновить</button>
      </nav>
    </header>

    {error && <div className="alert" role="alert"><strong>Оркестратор недоступен.</strong><span>{error}</span></div>}

    <section className="flow-workspace-selector" aria-label="Выбор workflow">
      <label>Workflow
        <select value={selectedFlowId ?? ""} onChange={(event) => selectFlow(event.target.value || null)} disabled={!data?.flows.length}>
          {(data?.flows ?? []).map((flow) => <option value={flow.id} key={flow.id}>{flow.title} · {flow.id} · {flow.builtin ? "builtin" : `draft r${flow.revision}`}</option>)}
        </select>
      </label>
      <small>{loading ? "Загрузка каталогов…" : `${data?.flows.length ?? 0} workflow · узлы перетаскиваются из палитры на холст`}</small>
    </section>

    {selectedFlow && <section className="flow-run-toolbar" aria-label="Ручной запуск workflow">
      <label>Inputs JSON
        <textarea value={runInputs} onChange={(event) => setRunInputs(event.target.value)} spellCheck={false} />
      </label>
      <div>
        <small>{latestVersion ? `Будет запущена опубликованная версия ${latestVersion}` : "Сначала опубликуйте workflow"}</small>
        <button type="button" onClick={() => void runFlow(selectedFlow, latestVersion)} disabled={busy || !latestVersion}>▶ Запустить</button>
      </div>
      {runResult?.flow_id === selectedFlow.id && <p><strong>{runResult.id}</strong> · {runResult.status} · node {runResult.current_node ?? "—"}</p>}
    </section>}

    {selectedFlow && data ? <FlowBuilder
      flow={selectedFlow}
      nodeTypes={data.nodeTypes}
      catalogs={{ operations: data.operations, models: data.models, plugins: data.plugins, credentials: data.credentials }}
      busy={busy}
      feedback={feedback?.flowId === selectedFlow.id ? feedback : null}
      onClone={() => void cloneFlow(selectedFlow)}
      onSave={(changes) => void saveFlow(selectedFlow, changes)}
      onValidate={() => void validateFlow(selectedFlow)}
      onPublish={() => void publishFlow(selectedFlow)}
      onClose={() => window.location.assign("/")}
    /> : <div className="flow-workspace-empty">{loading ? "Загрузка Flow Builder…" : "Нет доступных workflow."}</div>}
    {selectedFlow && validation?.flowId === selectedFlow.id && <section className={`flow-validation-panel is-${validation.result.valid ? "success" : "error"}`}>
      <div><p className="eyebrow">BACKEND VALIDATION</p><h2>{validation.result.valid ? "Граф корректен" : `Ошибок: ${validation.result.errors.length}`}</h2></div>
      {validation.result.errors.length > 0 && <ul>{validation.result.errors.map((issue, index) => <li key={`${issue.code}:${index}`}><strong>{issue.code}</strong><span>{issue.message}</span><small>{issue.node_id ? `node ${issue.node_id}` : issue.edge_id ? `edge ${issue.edge_id}` : "flow"}</small></li>)}</ul>}
      {validation.result.warnings.length > 0 && <ul>{validation.result.warnings.map((issue, index) => <li key={`${issue.code}:${index}`}><strong>{issue.code}</strong><span>{issue.message}</span></li>)}</ul>}
    </section>}
  </main>;
}
