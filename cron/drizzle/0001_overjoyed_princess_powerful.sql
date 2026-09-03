CREATE INDEX `idx_runs_task_created` ON `runs` (`task_id`,`created_at`);--> statement-breakpoint
CREATE INDEX `idx_tasks_due` ON `tasks` (`enabled`,`next_run_at`);--> statement-breakpoint
CREATE INDEX `idx_tool_calls_run` ON `tool_calls` (`run_id`);