import { index, integer, sqliteTable, text } from 'drizzle-orm/sqlite-core';

export const tasks = sqliteTable('tasks', {
  id: text('id').primaryKey(), name: text('name').notNull(), description: text('description').notNull(),
  prompt: text('prompt').notNull(), cronExpression: text('cron_expression').notNull(), timezone: text('timezone').notNull(),
  enabled: integer('enabled', { mode: 'boolean' }).notNull().default(true), sourcesJson: text('sources_json').notNull(),
  toolsJson: text('tools_json').notNull().default('["bash"]'),
  nextRunAt: text('next_run_at'), createdAt: text('created_at').notNull(), updatedAt: text('updated_at').notNull(),
}, (table) => [index('idx_tasks_due').on(table.enabled, table.nextRunAt)]);

export const runs = sqliteTable('runs', {
  id: text('id').primaryKey(), taskId: text('task_id').notNull().references(() => tasks.id, { onDelete: 'cascade' }),
  triggerType: text('trigger_type').notNull(), status: text('status').notNull(), startedAt: text('started_at'),
  finishedAt: text('finished_at'), resultMarkdown: text('result_markdown'), errorMessage: text('error_message'),
  warningMessage: text('warning_message'), retryOfRunId: text('retry_of_run_id'), createdAt: text('created_at').notNull(),
}, (table) => [index('idx_runs_task_created').on(table.taskId, table.createdAt)]);

export const toolCalls = sqliteTable('tool_calls', {
  id: text('id').primaryKey(), runId: text('run_id').notNull().references(() => runs.id, { onDelete: 'cascade' }),
  source: text('source').notNull(), toolName: text('tool_name').notNull(), status: text('status').notNull(),
  inputJson: text('input_json').notNull(), outputJson: text('output_json'), errorMessage: text('error_message'),
  startedAt: text('started_at').notNull(), finishedAt: text('finished_at'),
}, (table) => [index('idx_tool_calls_run').on(table.runId)]);
