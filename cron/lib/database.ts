import { env } from 'cloudflare:workers';
import type { RunRecord, SourceName, TaskInput, TaskRecord, TaskToolName, ToolCallRecord, ToolSourceName } from './types';

const d1=()=>env.DB;

export async function ensureDatabase() {
  const db=d1();
  await db.batch([
    db.prepare(`CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, prompt TEXT NOT NULL, cron_expression TEXT NOT NULL, timezone TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, sources_json TEXT NOT NULL, tools_json TEXT NOT NULL DEFAULT '["bash"]', next_run_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)`),
    db.prepare(`CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, trigger_type TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, finished_at TEXT, result_markdown TEXT, error_message TEXT, warning_message TEXT, retry_of_run_id TEXT, created_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE)`),
    db.prepare(`CREATE TABLE IF NOT EXISTS tool_calls (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source TEXT NOT NULL, tool_name TEXT NOT NULL, status TEXT NOT NULL, input_json TEXT NOT NULL, output_json TEXT, error_message TEXT, started_at TEXT NOT NULL, finished_at TEXT, FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE)`),
    db.prepare('CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(enabled, next_run_at)'),
    db.prepare('CREATE INDEX IF NOT EXISTS idx_runs_task_created ON runs(task_id, created_at DESC)'),
    db.prepare('CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id)'),
  ]);

  const taskColumns=(await db.prepare('PRAGMA table_info(tasks)').all<{name:string}>()).results;
  if(!taskColumns.some(column=>column.name==='tools_json')) {
    await db.prepare(`ALTER TABLE tasks ADD COLUMN tools_json TEXT NOT NULL DEFAULT '["bash"]'`).run();
  }

  const count=await db.prepare('SELECT COUNT(*) AS count FROM tasks').first<{count:number}>();
  if(!count?.count) {
    const now=new Date().toISOString();
    await db.prepare(`INSERT INTO tasks (id,name,description,prompt,cron_expression,timezone,enabled,sources_json,tools_json,next_run_at,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?,?,?,?)`)
      .bind(
        crypto.randomUUID(),
        'Сводка изменений проекта',
        'Собирает изменения в коде и актуальные задачи.',
        'Подготовь краткую сводку проекта. Выдели риски и предложи следующие действия.',
        '*/2 * * * *',
        'Europe/Moscow',
        JSON.stringify(['git','plane']),
        JSON.stringify(['bash']),
        new Date(Date.now()+120000).toISOString(),
        now,
        now,
      ).run();
  }
}

function parseArray<T>(value:unknown,fallback:T[]):T[] {
  try { return JSON.parse(String(value)) as T[]; } catch { return fallback; }
}

function taskFromRow(row:Record<string,unknown>):TaskRecord {
  return {
    id:String(row.id),
    name:String(row.name),
    description:String(row.description),
    prompt:String(row.prompt),
    cronExpression:String(row.cron_expression),
    timezone:String(row.timezone),
    enabled:Boolean(row.enabled),
    sources:parseArray<SourceName>(row.sources_json,[]),
    tools:parseArray<TaskToolName>(row.tools_json,['bash']),
    nextRunAt:row.next_run_at?String(row.next_run_at):null,
    createdAt:String(row.created_at),
    updatedAt:String(row.updated_at),
  };
}

export async function listTasks() {
  await ensureDatabase();
  const rows=(await d1().prepare('SELECT * FROM tasks ORDER BY created_at DESC').all<Record<string,unknown>>()).results;
  return rows.map(taskFromRow);
}

export async function getTask(id:string) {
  await ensureDatabase();
  const row=await d1().prepare('SELECT * FROM tasks WHERE id=?').bind(id).first<Record<string,unknown>>();
  return row?taskFromRow(row):null;
}

export async function createTask(input:TaskInput,nextRunAt:string|null) {
  await ensureDatabase();
  const id=crypto.randomUUID();
  const now=new Date().toISOString();
  await d1().prepare(`INSERT INTO tasks (id,name,description,prompt,cron_expression,timezone,enabled,sources_json,tools_json,next_run_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`)
    .bind(id,input.name,input.description,input.prompt,input.cronExpression,input.timezone,input.enabled?1:0,JSON.stringify(input.sources),JSON.stringify(input.tools),nextRunAt,now,now).run();
  return getTask(id);
}

export async function updateTask(id:string,input:TaskInput,nextRunAt:string|null) {
  await ensureDatabase();
  await d1().prepare(`UPDATE tasks SET name=?,description=?,prompt=?,cron_expression=?,timezone=?,enabled=?,sources_json=?,tools_json=?,next_run_at=?,updated_at=? WHERE id=?`)
    .bind(input.name,input.description,input.prompt,input.cronExpression,input.timezone,input.enabled?1:0,JSON.stringify(input.sources),JSON.stringify(input.tools),nextRunAt,new Date().toISOString(),id).run();
  return getTask(id);
}

export async function deleteTask(id:string) {
  await ensureDatabase();
  await d1().prepare('DELETE FROM tasks WHERE id=?').bind(id).run();
}

export async function createRun(taskId:string,triggerType:RunRecord['triggerType'],retryOfRunId:string|null=null) {
  const id=crypto.randomUUID();
  await d1().prepare(`INSERT INTO runs (id,task_id,trigger_type,status,retry_of_run_id,created_at) VALUES (?,?,?,'queued',?,?)`)
    .bind(id,taskId,triggerType,retryOfRunId,new Date().toISOString()).run();
  return id;
}

export async function updateRun(id:string,fields:{status:string;startedAt?:string;finishedAt?:string;result?:string;error?:string;warning?:string}) {
  await d1().prepare(`UPDATE runs SET status=?,started_at=COALESCE(?,started_at),finished_at=COALESCE(?,finished_at),result_markdown=?,error_message=?,warning_message=? WHERE id=?`)
    .bind(fields.status,fields.startedAt??null,fields.finishedAt??null,fields.result??null,fields.error??null,fields.warning??null,id).run();
}

export async function addToolCall(call:Omit<ToolCallRecord,'id'>) {
  await d1().prepare(`INSERT INTO tool_calls (id,run_id,source,tool_name,status,input_json,output_json,error_message,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)`)
    .bind(crypto.randomUUID(),call.runId,call.source,call.toolName,call.status,call.inputJson,call.outputJson,call.errorMessage,call.startedAt,call.finishedAt).run();
}

export async function listRuns(taskId?:string) {
  await ensureDatabase();
  const query=taskId
    ?d1().prepare(`SELECT runs.*,tasks.name AS task_name FROM runs JOIN tasks ON tasks.id=runs.task_id WHERE task_id=? ORDER BY created_at DESC LIMIT 50`).bind(taskId)
    :d1().prepare(`SELECT runs.*,tasks.name AS task_name FROM runs JOIN tasks ON tasks.id=runs.task_id ORDER BY created_at DESC LIMIT 50`);
  const rows=(await query.all<Record<string,unknown>>()).results;
  const result:RunRecord[]=[];
  for(const row of rows) {
    const calls=(await d1().prepare('SELECT * FROM tool_calls WHERE run_id=? ORDER BY started_at').bind(row.id).all<Record<string,unknown>>()).results;
    result.push({
      id:String(row.id),
      taskId:String(row.task_id),
      taskName:String(row.task_name),
      triggerType:row.trigger_type as RunRecord['triggerType'],
      status:row.status as RunRecord['status'],
      startedAt:row.started_at?String(row.started_at):null,
      finishedAt:row.finished_at?String(row.finished_at):null,
      resultMarkdown:row.result_markdown?String(row.result_markdown):null,
      errorMessage:row.error_message?String(row.error_message):null,
      warningMessage:row.warning_message?String(row.warning_message):null,
      retryOfRunId:row.retry_of_run_id?String(row.retry_of_run_id):null,
      createdAt:String(row.created_at),
      toolCalls:calls.map(call=>({
        id:String(call.id),runId:String(call.run_id),source:call.source as ToolSourceName,toolName:String(call.tool_name),
        status:call.status as 'success'|'failed',inputJson:String(call.input_json),outputJson:call.output_json?String(call.output_json):null,
        errorMessage:call.error_message?String(call.error_message):null,startedAt:String(call.started_at),finishedAt:call.finished_at?String(call.finished_at):null,
      })),
    });
  }
  return result;
}

export async function listDueTasks(now:string) {
  await ensureDatabase();
  const rows=(await d1().prepare('SELECT * FROM tasks WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=?').bind(now).all<Record<string,unknown>>()).results;
  return rows.map(taskFromRow);
}

export async function hasActiveRun(taskId:string) {
  return Boolean(await d1().prepare(`SELECT id FROM runs WHERE task_id=? AND status IN ('queued','running') LIMIT 1`).bind(taskId).first());
}

export async function setTaskNextRun(id:string,nextRunAt:string|null) {
  await d1().prepare('UPDATE tasks SET next_run_at=?,updated_at=? WHERE id=?').bind(nextRunAt,new Date().toISOString(),id).run();
}
