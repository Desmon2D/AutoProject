from __future__ import annotations

from typing import Any

from .gitea_client import GiteaClient, GiteaClientError
from .models import ArtifactRef, CommandScenarioStep, StepResult, WorkflowInstance
from .operation_catalog import operation_ids, validate_operation_parameters
from .plane_client import PlaneClient, PlaneClientError
from .test_runner import DockerTestRunner, TestRunnerError
from .workflow_errors import WorkflowExecutionError

ALLOWED_WORKFLOW_COMMANDS = operation_ids()


class CommandExecutor:
    """Executes the small allowlisted command vocabulary used by scenarios."""

    def __init__(
        self,
        gitea_client: GiteaClient | None = None,
        plane_client: PlaneClient | None = None,
        test_runner: DockerTestRunner | None = None,
    ):
        self.gitea_client = gitea_client
        self.plane_client = plane_client
        self.test_runner = test_runner

    def execute(
        self,
        *,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        step: CommandScenarioStep,
    ) -> StepResult:
        if step.command not in ALLOWED_WORKFLOW_COMMANDS:
            raise WorkflowExecutionError(f"command is not allowlisted: {step.command}")
        parameter_errors = validate_operation_parameters(step.command, step.parameters)
        if parameter_errors:
            raise WorkflowExecutionError(
                f"command parameters are invalid: {'; '.join(parameter_errors)}"
            )
        artifacts: list[ArtifactRef] = []
        if step.command == "sync_plane_issue":
            if self.plane_client is None:
                plane_sync = {"configured": False}
            else:
                recommendation = step.parameters.get("recommendation")
                if not isinstance(recommendation, str):
                    raise WorkflowExecutionError("sync_plane_issue requires recommendation")
                ticket = workflow.trigger.data.get("ticket")
                project = workflow.trigger.data.get("project")
                issue_id = ticket.get("id") if isinstance(ticket, dict) else None
                project_id = project.get("id") if isinstance(project, dict) else None
                if not isinstance(project_id, str) and isinstance(project, dict):
                    references = project.get("references")
                    project_id = (
                        next((item for item in references if isinstance(item, str)), None)
                        if isinstance(references, list)
                        else None
                    )
                if not isinstance(issue_id, str) or not isinstance(project_id, str):
                    raise WorkflowExecutionError("Plane issue and project ids are required")
                details: dict[str, Any] = {}
                for key in ("implementation_change", "test_report", "pull_request"):
                    value = next(
                        (
                            execution.data.get(key)
                            for execution in reversed(workflow.executions)
                            if isinstance(execution.data.get(key), dict)
                        ),
                        None,
                    )
                    if isinstance(value, dict):
                        details[key] = value
                try:
                    plane_sync = self.plane_client.record_result(
                        project_id=project_id,
                        issue_id=issue_id,
                        workflow_id=workflow.id,
                        recommendation=recommendation,
                        summary=str(step.parameters.get("summary", recommendation)),
                        details=details,
                    )
                except PlaneClientError as exc:
                    raise WorkflowExecutionError(str(exc)) from exc
            outcome = "SUCCESS"
            data = {"plane_sync": plane_sync}
            default_summary = "Plane issue synchronized"
        elif step.command == "verify_bug_report":
            if self.gitea_client is None or self.test_runner is None:
                raise WorkflowExecutionError("Gitea client and deterministic test runner are required")
            author_step = step.parameters.get("author_step", "find-bugs")
            report = next(
                (
                    result.data.get("bug_report")
                    for result in reversed(workflow.executions)
                    if result.step_id == author_step
                    and result.execution_status == "COMPLETED"
                    and result.outcome == "SUCCESS"
                    and isinstance(result.data.get("bug_report"), dict)
                ),
                None,
            )
            if not isinstance(report, dict):
                raise WorkflowExecutionError("validated bug report is required")
            repository = report.get("repository")
            commit = report.get("inspected_commit")
            findings = report.get("findings")
            if (
                not isinstance(repository, str)
                or not isinstance(commit, str)
                or not isinstance(findings, list)
            ):
                raise WorkflowExecutionError("bug report has no valid repository revision")
            try:
                archive = self.gitea_client.download_archive(
                    repository=repository,
                    commit=commit,
                )
                verifications: list[dict[str, Any]] = []
                for finding in findings:
                    reproducer = finding.get("reproducer") if isinstance(finding, dict) else None
                    if not isinstance(reproducer, dict):
                        raise WorkflowExecutionError("bug finding has no reproducer")
                    path = reproducer.get("path")
                    command = reproducer.get("command")
                    content = reproducer.get("content")
                    if (
                        not isinstance(path, str)
                        or not isinstance(command, list)
                        or not isinstance(content, str)
                    ):
                        raise WorkflowExecutionError("bug reproducer fields are invalid")
                    run = self.test_runner.run(
                        archive,
                        command,
                        overlay_files={path: content.encode("utf-8")},
                    )
                    verifications.append(
                        {
                            "finding_id": finding.get("id"),
                            "verdict": run.verdict,
                            "command": run.command,
                            "exit_code": run.exit_code,
                            "framework": run.framework,
                            "total": run.total,
                            "passed": run.passed,
                            "failed": run.failed,
                            "errors": run.errors,
                            "summary": run.summary,
                            "output": run.output,
                            "authoritative": True,
                        }
                    )
            except (GiteaClientError, TestRunnerError) as exc:
                raise WorkflowExecutionError(str(exc)) from exc
            verified = bool(verifications) and all(
                item["verdict"] == "PRODUCT_FAILURE"
                and item["framework"] in {"pytest", "jest", "go", "dotnet"}
                for item in verifications
            )
            no_findings = report.get("status") == "NO_FINDINGS" and not findings
            outcome = "SUCCESS" if verified or no_findings else "FAILURE"
            verification_status = (
                "NO_FINDINGS" if no_findings else "VERIFIED" if verified else "UNVERIFIED"
            )
            data = {
                "bug_verification": {
                    "status": verification_status,
                    "repository": repository,
                    "commit": commit,
                    "findings": verifications,
                    "authoritative": True,
                }
            }
            default_summary = (
                "All reported defects were reproduced independently"
                if verified
                else "Bug analysis completed without findings"
                if no_findings
                else "One or more reported defects could not be reproduced independently"
            )
        elif step.command == "execute_test_change":
            if self.gitea_client is None or self.test_runner is None:
                raise WorkflowExecutionError("Gitea client and deterministic test runner are required")
            authored = next(
                (
                    result.data.get("test_change")
                    for result in reversed(workflow.executions)
                    if result.step_id == step.parameters.get("author_step", "write-tests")
                    and result.execution_status == "COMPLETED"
                    and result.outcome == "SUCCESS"
                    and isinstance(result.data.get("test_change"), dict)
                ),
                None,
            )
            if not isinstance(authored, dict):
                raise WorkflowExecutionError("validated test change is required")
            repository = authored.get("repository")
            branch = authored.get("branch")
            commit = authored.get("commit")
            command = authored.get("command")
            if (
                not isinstance(repository, str)
                or not isinstance(branch, str)
                or not isinstance(commit, str)
                or not isinstance(command, list)
                or any(not isinstance(item, str) for item in command)
            ):
                raise WorkflowExecutionError("test change has no valid repository revision or command")
            try:
                archive = self.gitea_client.download_archive(
                    repository=repository,
                    commit=commit,
                )
                run = self.test_runner.run(archive, command)
            except (GiteaClientError, TestRunnerError) as exc:
                raise WorkflowExecutionError(str(exc)) from exc
            report = {
                "verdict": run.verdict,
                "repository": repository,
                "branch": branch,
                "commit": commit,
                "command": run.command,
                "exit_code": run.exit_code,
                "framework": run.framework,
                "total": run.total,
                "passed": run.passed,
                "failed": run.failed,
                "errors": run.errors,
                "skipped": run.skipped,
                "summary": run.summary,
                "output": run.output,
                "authoritative": True,
            }
            outcome = "FAILURE" if run.verdict == "TEST_CODE_ERROR" else "SUCCESS"
            data = {"test_report": report}
            default_summary = run.summary
        elif step.command == "create_final_pull_request":
            if self.gitea_client is None:
                raise WorkflowExecutionError("Gitea client is not configured")
            authored = next(
                (
                    result.data.get("test_change")
                    for result in reversed(workflow.executions)
                    if result.step_id == step.parameters.get("author_step", "write-tests")
                    and result.execution_status == "COMPLETED"
                    and result.outcome == "SUCCESS"
                    and isinstance(result.data.get("test_change"), dict)
                ),
                None,
            )
            executed = next(
                (
                    result.data.get("test_report")
                    for result in reversed(workflow.executions)
                    if result.step_id == step.parameters.get("executor_step", "execute-tests")
                    and result.execution_status == "COMPLETED"
                    and result.outcome == "SUCCESS"
                    and isinstance(result.data.get("test_report"), dict)
                ),
                None,
            )
            if not isinstance(authored, dict) or not isinstance(executed, dict):
                raise WorkflowExecutionError(
                    "validated test change and execution report are required"
                )
            if executed.get("verdict") != "PASSED" or any(
                executed.get(field) != authored.get(field)
                for field in ("repository", "branch", "commit")
            ):
                raise WorkflowExecutionError("only the exact passing test commit may be proposed")
            ticket = workflow.trigger.data.get("ticket")
            title = ticket.get("summary") if isinstance(ticket, dict) else None
            try:
                pull = self.gitea_client.create_final_pull_request(
                    repository=authored["repository"],
                    head=authored["branch"],
                    commit=authored["commit"],
                    workflow_id=workflow.id,
                    title=str(title or f"Validated changes for {workflow.id}"),
                )
            except (GiteaClientError, KeyError) as exc:
                raise WorkflowExecutionError(str(exc)) from exc
            outcome = "SUCCESS"
            data = {"pull_request": pull, "test_report": executed}
            default_summary = "Final pull request created after successful tests"
            artifacts = [
                ArtifactRef(
                    type="pull_request",
                    uri=pull["url"],
                    summary="Final validated pull request",
                )
            ]
        elif step.command == "store_failure_report":
            failed = next(
                (result for result in reversed(workflow.executions) if result.outcome == "FAILURE"),
                None,
            )
            outcome = "SUCCESS" if failed is not None else "FAILURE"
            data = {
                "failed_step": failed.step_id if failed else None,
                "failure": failed.data if failed else {},
                "artifacts": [artifact.model_dump() for artifact in failed.artifacts]
                if failed
                else [],
            }
            default_summary = (
                "Failure report stored in workflow state"
                if failed
                else "No failed step result is available"
            )
        elif step.command in {"allow_test_rewrite", "allow_bug_rewrite"}:
            author_step = step.parameters.get("author_step")
            max_iterations = step.parameters.get("max_iterations")
            if not isinstance(author_step, str) or not author_step:
                raise WorkflowExecutionError("allow_test_rewrite requires author_step")
            if type(max_iterations) is not int or not 1 <= max_iterations <= 10:
                raise WorkflowExecutionError(
                    "allow_test_rewrite max_iterations must be an integer from 1 to 10"
                )
            authored = sum(
                result.step_id == author_step
                and result.execution_status == "COMPLETED"
                and result.outcome == "SUCCESS"
                for result in workflow.executions
            )
            outcome = "SUCCESS" if authored < max_iterations else "FAILURE"
            data = {
                "author_step": author_step,
                "completed_iterations": authored,
                "max_iterations": max_iterations,
            }
            default_summary = (
                "Agent may repair the unverified result"
                if outcome == "SUCCESS"
                else "Result rewrite limit reached"
            )
        elif step.command == "classify_test_run":
            executor_step = step.parameters.get("executor_step")
            if not isinstance(executor_step, str) or not executor_step:
                raise WorkflowExecutionError("classify_test_run requires executor_step")
            executed = next(
                (
                    result
                    for result in reversed(workflow.executions)
                    if result.step_id == executor_step
                    and result.execution_status == "COMPLETED"
                    and result.outcome == "SUCCESS"
                ),
                None,
            )
            report = executed.data.get("test_report") if executed else None
            if not isinstance(report, dict):
                raise WorkflowExecutionError("no successful test execution report is available")
            verdict = report.get("verdict")
            if verdict not in {"PASSED", "PRODUCT_FAILURE"}:
                raise WorkflowExecutionError("test execution report has no final verdict")
            outcome = "SUCCESS" if verdict == "PASSED" else "FAILURE"
            data = {"test_report": report}
            default_summary = (
                "All authored tests passed"
                if outcome == "SUCCESS"
                else "Authored tests found a product defect"
            )
        else:
            outcome = "SUCCESS" if step.command == "complete" else "FAILURE"
            data = step.parameters.get("data", {})
            default_summary = f"Command {step.command} completed"
        summary = str(step.parameters.get("summary", default_summary))
        if not isinstance(data, dict):
            raise WorkflowExecutionError("command parameters.data must be an object")
        return StepResult(
            step_id=step_id,
            execution_id=self.execution_id(workflow.id, step_id, iteration, attempt),
            iteration=iteration,
            attempt=attempt,
            execution_status="COMPLETED",
            outcome=outcome,
            data={"summary": summary, **data},
            artifacts=artifacts,
        )

    @staticmethod
    def execution_id(workflow_id: str, step_id: str, iteration: int, attempt: int) -> str:
        return f"{workflow_id}-{step_id}-{iteration}-{attempt}"
