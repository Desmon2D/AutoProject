export type SourceName = 'jira' | 'git' | 'wiki';
export type RunStatus = 'queued' | 'running' | 'success' | 'failed' | 'skipped';
export type ScheduleMode = 'interval' | 'hourly' | 'daily' | 'weekdays' | 'weekly' | 'custom';
export type ScheduleInput = { mode:ScheduleMode; intervalMinutes?:number; time?:string; weekdays?:number[]; cronExpression?:string };
export type TaskRecord = { id:string; name:string; description:string; prompt:string; cronExpression:string; timezone:string; enabled:boolean; sources:SourceName[]; nextRunAt:string|null; createdAt:string; updatedAt:string };
export type ToolCallRecord = { id:string; runId:string; source:SourceName; toolName:string; status:'success'|'failed'; inputJson:string; outputJson:string|null; errorMessage:string|null; startedAt:string; finishedAt:string|null };
export type RunRecord = { id:string; taskId:string; taskName:string; triggerType:'manual'|'scheduled'|'retry'; status:RunStatus; startedAt:string|null; finishedAt:string|null; resultMarkdown:string|null; errorMessage:string|null; warningMessage:string|null; retryOfRunId:string|null; createdAt:string; toolCalls:ToolCallRecord[] };
export type TaskInput = Omit<TaskRecord, 'id'|'nextRunAt'|'createdAt'|'updatedAt'>;
