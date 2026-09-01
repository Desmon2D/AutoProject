'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { describeSchedule, resolveSchedule, scheduleFromCron } from '../lib/schedule';
import type { RunRecord, ScheduleInput, SourceName, TaskInput, TaskRecord, TaskToolName, ToolCallRecord, ToolSourceName } from '../lib/types';

const blank: TaskInput = {
  name: '',
  description: '',
  prompt: '',
  cronExpression: '0 9 * * 1-5',
  timezone: 'Europe/Moscow',
  enabled: true,
  sources: ['git', 'plane'],
  tools: ['bash'],
};
const blankSchedule: ScheduleInput = { mode: 'weekdays', time: '09:00' };
const sourceLabel: Record<ToolSourceName, string> = { jira: 'Jira', git: 'Git', wiki: 'Wiki', plane: 'Plane', code: 'Bash' };
const statusLabel: Record<RunRecord['status'], string> = {
  queued: 'Ожидает',
  running: 'Выполняется',
  success: 'Успешно',
  failed: 'Ошибка',
  skipped: 'Пропущен',
};
const triggerLabel: Record<RunRecord['triggerType'], string> = {
  manual: 'Вручную',
  scheduled: 'По расписанию',
  retry: 'Повтор',
};
const formatDate = (value: string | null) => value
  ? new Intl.DateTimeFormat('ru-RU', {
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }).format(new Date(value))
  : '—';

export default function Dashboard() {
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [editing, setEditing] = useState<TaskRecord | null | undefined>(undefined);
  const [form, setForm] = useState<TaskInput>(blank);
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const [schedule, setSchedule] = useState<ScheduleInput>(blankSchedule);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [nextRuns, setNextRuns] = useState<string[]>([]);

  const load = useCallback(async () => {
    const [tasksResponse, runsResponse] = await Promise.all([
      fetch('/api/tasks').then(response => response.json()),
      fetch('/api/runs').then(response => response.json()),
    ]);
    setTasks(tasksResponse.tasks ?? []);
    setRuns(runsResponse.runs ?? []);
  }, []);

  useEffect(() => {
    const start = setTimeout(() => load().catch(() => setError('Не удалось загрузить данные')), 0);
    const refresh = setInterval(() => load().catch(() => {}), 5000);
    const scheduler = setInterval(() => fetch('/api/scheduler', { method: 'POST' }).then(load).catch(() => {}), 15000);
    return () => {
      clearTimeout(start);
      clearInterval(refresh);
      clearInterval(scheduler);
    };
  }, [load]);

  useEffect(() => {
    const timer = setTimeout(() => {
      if (editing === undefined || !form.cronExpression) {
        setNextRuns([]);
        return;
      }
      fetch(`/api/tasks?expression=${encodeURIComponent(form.cronExpression)}&timezone=${encodeURIComponent(form.timezone)}`)
        .then(async response => {
          const data = await response.json();
          setNextRuns(response.ok && Array.isArray(data.nextRuns) ? data.nextRuns : []);
        })
        .catch(() => setNextRuns([]));
    }, 250);
    return () => clearTimeout(timer);
  }, [editing, form.cronExpression, form.timezone]);

  const selectedTask = useMemo(
    () => tasks.find(task => task.id === selectedTaskId) ?? tasks[0] ?? null,
    [tasks, selectedTaskId],
  );
  const activeTaskId = selectedTask?.id ?? null;
  const taskRuns = useMemo(
    () => runs.filter(item => item.taskId === activeTaskId),
    [runs, activeTaskId],
  );

  function openForm(task?: TaskRecord) {
    setEditing(task ?? null);
    setForm(task ? {
      name: task.name,
      description: task.description,
      prompt: task.prompt,
      cronExpression: task.cronExpression,
      timezone: task.timezone,
      enabled: task.enabled,
      sources: task.sources,
      tools: task.tools,
    } : blank);
    setSchedule(task ? scheduleFromCron(task.cronExpression) : blankSchedule);
    setError('');
  }

  function changeSchedule(value: ScheduleInput) {
    const { cronExpression } = resolveSchedule(value);
    setSchedule(value);
    setForm(current => ({ ...current, cronExpression }));
  }

  function toggleSource(source: SourceName) {
    setForm(value => ({
      ...value,
      sources: value.sources.includes(source)
        ? value.sources.filter(item => item !== source)
        : [...value.sources, source],
    }));
  }

  function toggleTool(tool: TaskToolName) {
    setForm(value=>({
      ...value,
      tools:value.tools.includes(tool)?value.tools.filter(item=>item!==tool):[...value.tools,tool],
    }));
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy('save');
    setError('');
    try {
      const response = await fetch(`/api/tasks${editing?.id ? `?id=${editing.id}` : ''}`, {
        method: editing?.id ? 'PATCH' : 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...form, schedule }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.error ?? 'Ошибка сохранения');
        return;
      }
      if (data.task?.id) setSelectedTaskId(data.task.id);
      setEditing(undefined);
      await load();
    } finally {
      setBusy(null);
    }
  }

  async function remove(task: TaskRecord) {
    if (!confirm(`Удалить задачу «${task.name}»?`)) return;
    const response = await fetch(`/api/tasks?id=${task.id}`, { method: 'DELETE' });
    if (!response.ok) {
      setError('Не удалось удалить задачу');
      return;
    }
    if (selectedTaskId === task.id) setSelectedTaskId(null);
    await load();
  }

  async function run(taskId: string, retryOfRunId?: string) {
    setBusy(taskId);
    setError('');
    try {
      const response = await fetch('/api/runs', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ taskId, retryOfRunId }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.error ?? 'Ошибка запуска');
        return;
      }
      await load();
      if (data.runId) {
        const latest = await fetch('/api/runs').then(response => response.json());
        const current = latest.runs.find((item: RunRecord) => item.id === data.runId);
        if (current) setSelectedRun(current);
      }
    } finally {
      setBusy(null);
    }
  }

  async function toggle(task: TaskRecord) {
    const input = {
      name: task.name,
      description: task.description,
      prompt: task.prompt,
      cronExpression: task.cronExpression,
      timezone: task.timezone,
      enabled: !task.enabled,
      sources: task.sources,
      tools: task.tools,
    };
    await fetch(`/api/tasks?id=${task.id}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(input),
    });
    await load();
  }

  return <main className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">A</span><span>AI Cron</span></div>
      <div className="sidebar-heading">
        <div><h2>Задачи</h2><span>{tasks.length}</span></div>
        <button className="add-task-button" onClick={() => openForm()} aria-label="Создать задачу">+</button>
      </div>
      <nav className="sidebar-task-list" aria-label="Список задач">
        {tasks.map(task => <button
          key={task.id}
          className={`sidebar-task ${task.id === activeTaskId ? 'active' : ''}`}
          onClick={() => setSelectedTaskId(task.id)}
          aria-pressed={task.id === activeTaskId}
        >
          <span className={`task-state-dot ${task.enabled ? '' : 'off'}`} />
          <span className="sidebar-task-copy">
            <strong>{task.name}</strong>
            <small>{describeSchedule(task.cronExpression)}</small>
          </span>
        </button>)}
        {!tasks.length && <div className="sidebar-empty">Создайте первую задачу</div>}
      </nav>
      <button className="sidebar-create" onClick={() => openForm()}>+ Новая задача</button>
    </aside>

    <section className="workspace">
      {error && <div className="error-banner">{error}<button onClick={() => setError('')}>×</button></div>}
      {selectedTask ? <>
        <header className="topbar task-detail-header">
          <div>
            <p className="eyebrow">AI-задача</p>
            <h1>{selectedTask.name}</h1>
            <p className="subtitle">{selectedTask.description || 'Описание задачи не заполнено.'}</p>
          </div>
          <div className="header-actions">
            <button className="secondary-button" onClick={() => openForm(selectedTask)}>Настроить</button>
            <button className="primary-button" disabled={busy === selectedTask.id} onClick={() => run(selectedTask.id)}>
              {busy === selectedTask.id ? 'Выполняется…' : 'Запустить сейчас'}
            </button>
            <button className="icon-button danger bordered" aria-label="Удалить задачу" onClick={() => remove(selectedTask)}>×</button>
          </div>
        </header>

        <section className="detail-summary" aria-label="Состояние задачи">
          <article>
            <span>Статус</span>
            <button className={`active-pill ${selectedTask.enabled ? '' : 'off'}`} onClick={() => toggle(selectedTask)}>
              {selectedTask.enabled ? 'Активна' : 'Выключена'}
            </button>
          </article>
          <article>
            <span>Расписание</span>
            <strong>{describeSchedule(selectedTask.cronExpression)}</strong>
          </article>
          <article>
            <span>Следующий запуск</span>
            <strong>{selectedTask.enabled ? formatDate(selectedTask.nextRunAt) : 'Автозапуск выключен'}</strong>
          </article>
        </section>

        <section className="task-information">
          <article className="information-card instruction-card">
            <div className="card-heading"><span className="card-icon">AI</span><h2>Инструкция для AI</h2></div>
            <p>{selectedTask.prompt}</p>
          </article>
          <article className="information-card sources-card">
            <div className="card-heading"><span className="card-icon">↗</span><h2>Источники</h2></div>
            <div className="source-list source-list-large">
              {selectedTask.sources.map(source => <span key={source}>{sourceLabel[source]}</span>)}
            </div>
              <dl><div><dt>Создана</dt><dd>{formatDate(selectedTask.createdAt)}</dd></div></dl>
          </article>
          <article className="information-card tools-card">
            <div className="card-heading"><span className="card-icon">&gt;_</span><h2>Инструменты</h2></div>
            {selectedTask.tools.includes('bash') ? <>
              <div className="tool-module enabled"><strong>Bash-контейнер</strong><span>Доступен для вычислений и коротких команд</span></div>
              <p className="tool-policy">Изолирован от сети и файлов устройства</p>
            </> : <div className="tool-module disabled"><strong>Bash-контейнер</strong><span>Отключён для этой задачи</span></div>}
          </article>
        </section>

        <section className="task-history-section">
          <div className="section-heading">
            <div><p className="eyebrow">Активность</p><h2>История запусков</h2></div>
            <span>{taskRuns.length} запусков</span>
          </div>
          <History
            runs={taskRuns}
            onSelect={setSelectedRun}
            onRetry={item => run(item.taskId, item.id)}
          />
        </section>
      </> : <section className="welcome-empty">
        <span className="empty-icon">+</span>
        <h1>Создайте первую задачу</h1>
        <p>После создания здесь появятся настройки, расписание и история запусков.</p>
        <button className="primary-button" onClick={() => openForm()}>Новая задача</button>
      </section>}
    </section>

    {editing !== undefined && <div className="modal-backdrop" onMouseDown={() => setEditing(undefined)}>
      <section className="modal" onMouseDown={event => event.stopPropagation()}>
        <div className="modal-header">
          <div><p className="eyebrow">Настройка</p><h2>{editing ? 'Редактировать задачу' : 'Новая задача'}</h2></div>
          <button className="close-button" onClick={() => setEditing(undefined)}>×</button>
        </div>
        <form onSubmit={save}>
          <label>Название<input required value={form.name} onChange={event => setForm({ ...form, name: event.target.value })} placeholder="Например, ежедневная сводка" /></label>
          <label>Описание<input value={form.description} onChange={event => setForm({ ...form, description: event.target.value })} placeholder="Что делает эта задача" /></label>
          <label>Инструкция для AI<textarea required rows={4} value={form.prompt} onChange={event => setForm({ ...form, prompt: event.target.value })} /></label>
          <fieldset>
            <legend>Источники данных</legend>
            <div className="choice-row">{(['git', 'plane'] as SourceName[]).map(source => <button type="button" key={source} className={form.sources.includes(source) ? 'selected' : ''} onClick={() => toggleSource(source)}>{sourceLabel[source]}</button>)}</div>
          </fieldset>
          <fieldset className="tool-selector">
            <legend>Инструменты</legend>
            <button type="button" className={`tool-option ${form.tools.includes('bash')?'selected':''}`} onClick={()=>toggleTool('bash')} aria-pressed={form.tools.includes('bash')}>
              <span className="tool-option-icon">&gt;_</span>
              <span><strong>Bash-контейнер</strong><small>Вычисления и короткие команды в изолированной среде</small></span>
              <span className="tool-option-state">{form.tools.includes('bash')?'Включён':'Выключен'}</span>
            </button>
          </fieldset>
          <ScheduleEditor value={schedule} onChange={changeSchedule} />
          {form.cronExpression && <div className="schedule-summary"><span>Расписание</span><strong>{describeSchedule(form.cronExpression)}</strong></div>}
          <div className="next-runs">
            <span>Ближайшие запуски:</span>
            {nextRuns.length
              ? nextRuns.map(item => <small key={item}>{formatDate(item)}</small>)
              : <small className="invalid">{schedule.mode === 'weekly' && !schedule.weekdays?.length ? 'Выберите хотя бы один день недели' : 'Проверьте расписание'}</small>}
          </div>
          <label className="switch-row"><input type="checkbox" checked={form.enabled} onChange={event => setForm({ ...form, enabled: event.target.checked })} /><span>Автоматический запуск включён</span></label>
          <div className="modal-actions">
            <button type="button" className="ghost-button" onClick={() => setEditing(undefined)}>Отмена</button>
            <button className="primary-button" disabled={busy === 'save' || !form.cronExpression}>{busy === 'save' ? 'Сохранение…' : 'Сохранить'}</button>
          </div>
        </form>
      </section>
    </div>}
    {selectedRun && <RunDetails run={selectedRun} onClose={() => setSelectedRun(null)} onRetry={() => run(selectedRun.taskId, selectedRun.id)} />}
  </main>;
}

const weekdays = [
  { value: 1, label: 'Пн' },
  { value: 2, label: 'Вт' },
  { value: 3, label: 'Ср' },
  { value: 4, label: 'Чт' },
  { value: 5, label: 'Пт' },
  { value: 6, label: 'Сб' },
  { value: 0, label: 'Вс' },
];

function ScheduleEditor({ value, onChange }: { value: ScheduleInput; onChange: (value: ScheduleInput) => void }) {
  const setMode = (mode: ScheduleInput['mode']) => {
    if (mode === 'interval') onChange({ mode, intervalMinutes: 15 });
    else if (mode === 'hourly') onChange({ mode });
    else if (mode === 'weekly') onChange({ mode, time: '09:00', weekdays: [1] });
    else if (mode === 'custom') onChange({ mode, cronExpression: '0 9 * * 1-5' });
    else onChange({ mode, time: '09:00' });
  };
  const toggleDay = (day: number) => {
    const current = value.weekdays ?? [];
    onChange({ ...value, weekdays: current.includes(day) ? current.filter(item => item !== day) : [...current, day] });
  };
  return <fieldset className="schedule-editor">
    <legend>Когда запускать</legend>
    <label>Повторение<select value={value.mode} onChange={event => setMode(event.target.value as ScheduleInput['mode'])}><option value="interval">Каждые несколько минут</option><option value="hourly">Каждый час</option><option value="daily">Ежедневно</option><option value="weekdays">По будням</option><option value="weekly">В выбранные дни</option><option value="custom">Расширенный cron</option></select></label>
    {value.mode === 'interval' && <label>Интервал<select value={value.intervalMinutes} onChange={event => onChange({ ...value, intervalMinutes: Number(event.target.value) })}>{[5, 10, 15, 20, 30].map(item => <option value={item} key={item}>Каждые {item} минут</option>)}</select></label>}
    {['daily', 'weekdays', 'weekly'].includes(value.mode) && <label>Время<input type="time" required value={value.time ?? '09:00'} onChange={event => onChange({ ...value, time: event.target.value })} /></label>}
    {value.mode === 'weekly' && <div><span className="field-label">Дни недели</span><div className="weekday-row">{weekdays.map(day => <button type="button" key={day.value} className={value.weekdays?.includes(day.value) ? 'selected' : ''} onClick={() => toggleDay(day.value)}>{day.label}</button>)}</div>{!value.weekdays?.length && <small className="field-error">Выберите хотя бы один день</small>}</div>}
    {value.mode === 'custom' && <label>Cron-выражение<input required value={value.cronExpression ?? ''} onChange={event => onChange({ ...value, cronExpression: event.target.value })} placeholder="0 9 * * 1-5" /><small>Пять полей: минуты, часы, день месяца, месяц, день недели.</small></label>}
  </fieldset>;
}

function History({ runs, onSelect, onRetry }: { runs: RunRecord[]; onSelect: (run: RunRecord) => void; onRetry: (run: RunRecord) => void }) {
  return <div className="history-panel">
    <div className="history-header"><span>Запуск</span><span>Статус</span><span>Время</span><span /></div>
    {runs.map(item => <div className="history-row" key={item.id}>
      <button className="history-open" onClick={() => onSelect(item)} aria-label={`Открыть запуск ${formatDate(item.startedAt ?? item.createdAt)}`}>
        <strong>{triggerLabel[item.triggerType]}</strong>
        <span className={`run-status ${item.status}`}>{statusLabel[item.status]}</span>
        <span>{formatDate(item.startedAt ?? item.createdAt)}</span>
      </button>
      <button className="retry-link" onClick={() => onRetry(item)}>Повторить</button>
    </div>)}
    {!runs.length && <div className="empty-state"><strong>Запусков пока нет</strong><span>Запустите задачу, чтобы увидеть здесь результат.</span></div>}
  </div>;
}

function RunDetails({ run, onClose, onRetry }: { run: RunRecord; onClose: () => void; onRetry: () => void }) {
  return <div className="modal-backdrop" onMouseDown={onClose}>
    <section className="modal run-modal" onMouseDown={event => event.stopPropagation()}>
      <div className="modal-header">
        <div><p className="eyebrow">{run.taskName}</p><h2>Результат запуска</h2></div>
        <button className="close-button" onClick={onClose}>×</button>
      </div>
      <div className="run-summary"><span className={`run-status ${run.status}`}>{statusLabel[run.status]}</span><span>{triggerLabel[run.triggerType]}</span><span>{formatDate(run.startedAt)}</span></div>
      {run.warningMessage && <div className="warning-box">{run.warningMessage}</div>}
      {run.errorMessage && <div className="error-box">{run.errorMessage}</div>}
      <div className="tool-call-list">
        <h3>Источники и инструменты</h3>
        {run.toolCalls.map(call => <details key={call.id}><summary><span>{sourceLabel[call.source]}</span><span className={`run-status ${call.status === 'success' ? 'success' : 'failed'}`}>{call.status === 'success' ? 'Успешно' : 'Ошибка'}</span></summary><ToolCallOutput call={call}/></details>)}
      </div>
      {run.resultMarkdown && <article className="markdown"><ReactMarkdown>{run.resultMarkdown}</ReactMarkdown></article>}
      <div className="modal-actions"><button className="ghost-button" onClick={onClose}>Закрыть</button><button className="primary-button" onClick={onRetry}>Повторить запуск</button></div>
    </section>
  </div>;
}

function ToolCallOutput({call}:{call:ToolCallRecord}) {
  if(call.errorMessage) return <div className="tool-error">{call.errorMessage}</div>;
  let output:Record<string,unknown>={};
  try { output=JSON.parse(call.outputJson??'{}') as Record<string,unknown>; } catch { return <pre>{call.outputJson}</pre>; }
  if(call.source!=='code') return <pre>{JSON.stringify(output,null,2)}</pre>;
  const stdout=typeof output.stdout==='string'?output.stdout.trim():'';
  const stderr=typeof output.stderr==='string'?output.stderr.trim():'';
  return <div className="bash-result">
    <span className="bash-result-label">Результат Bash</span>
    <strong>{stdout||'Команда выполнена без вывода'}</strong>
    {stderr&&<small className="bash-stderr">{stderr}</small>}
    <details className="technical-details"><summary>Технические детали</summary><pre>{JSON.stringify(output,null,2)}</pre></details>
  </div>;
}
