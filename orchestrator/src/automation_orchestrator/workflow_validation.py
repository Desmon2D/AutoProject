from __future__ import annotations

import re
from pathlib import PurePosixPath

from .gitea_client import GiteaClientError
from .models import (
    AgentScenarioStep,
    ArtifactRef,
    ReviewScenarioStep,
    StepResult,
    WorkflowInstance,
)


class AgentResultValidationMixin:
    """Validates agent output against the contract selected by a scenario step."""

    def _validate_agent_result(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        step: AgentScenarioStep,
        result: StepResult,
    ) -> StepResult:
        if result.execution_status != "COMPLETED":
            return result
        if step.result_contract == "test_execution":
            return self._validate_test_execution_result(
                workflow, step_id, iteration, attempt, result
            )
        if result.outcome != "SUCCESS":
            return result
        if step.result_contract == "markdown_document":
            return self._validate_markdown_document_result(
                workflow, step_id, iteration, attempt, result
            )
        if step.result_contract == "bug_report":
            return self._validate_bug_report_result(
                workflow, step_id, iteration, attempt, result
            )
        if step.result_contract == "test_change":
            return self._validate_test_change_result(workflow, step_id, iteration, attempt, result)
        if step.result_contract == "implementation_change":
            return self._validate_implementation_change_result(
                workflow, step_id, iteration, attempt, result
            )
        next_step_id = step.transitions.get("SUCCESS")
        if next_step_id is None:
            return result
        scenario = self._scenario_for(workflow)
        if step.result_contract != "pull_request" and not isinstance(
            scenario.steps[next_step_id], ReviewScenarioStep
        ):
            return result

        pull = result.data.get("pull_request")
        if not isinstance(pull, dict):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_RESULT_PULL_REQUEST_INVALID",
                "successful implementation must return data.pull_request",
            )
        repository = pull.get("repository")
        index = pull.get("index")
        url = pull.get("url") or pull.get("html_url")
        if (
            not isinstance(repository, str)
            or not repository.strip()
            or not isinstance(index, int)
            or index < 1
            or not isinstance(url, str)
            or not url.startswith(("http://", "https://"))
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_RESULT_PULL_REQUEST_INVALID",
                "data.pull_request must contain repository, positive index, and HTTP URL",
            )
        if not any(
            artifact.type == "pull_request" and artifact.uri == url for artifact in result.artifacts
        ):
            result = result.model_copy(
                update={
                    "artifacts": [
                        *result.artifacts,
                        ArtifactRef(
                            type="pull_request",
                            uri=url,
                            summary="Pull request returned by the implementation agent",
                        ),
                    ]
                }
            )

        expected = self._review_reference(workflow)
        expected_repository = expected.get("repository")
        expected_index = expected.get("pull_index")
        if expected_repository is not None and expected_repository != repository:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_PULL_REQUEST_CHANGED",
                "implementation iteration changed the reviewed repository",
            )
        if expected_index is not None and expected_index != index:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_PULL_REQUEST_CHANGED",
                "implementation iteration created a different pull request",
            )
        return result

    def _validate_bug_report_result(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        result: StepResult,
    ) -> StepResult:
        report = result.data.get("bug_report")
        repository_data = workflow.trigger.data.get("repository")
        expected_repository = (
            repository_data.get("full_name") if isinstance(repository_data, dict) else None
        )
        expected_ref = repository_data.get("ref") if isinstance(repository_data, dict) else None
        if not isinstance(report, dict):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_BUG_REPORT_INVALID",
                "successful bug finding must return data.bug_report",
            )

        status = report.get("status")
        findings = report.get("findings")
        report_path = report.get("report_path")
        inspected_commit = report.get("inspected_commit")
        if (
            report.get("repository") != expected_repository
            or report.get("requested_ref") != expected_ref
            or status not in {"FOUND", "NO_FINDINGS"}
            or not isinstance(inspected_commit, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", inspected_commit) is None
            or not isinstance(findings, list)
            or len(findings) > 50
            or (status == "FOUND" and not findings)
            or (status == "NO_FINDINGS" and findings)
            or not isinstance(report_path, str)
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_BUG_REPORT_INVALID",
                "bug report must identify the requested repository/ref, inspected commit, status, findings, and report path",
            )

        path = PurePosixPath(report_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != report_path
            or path.suffix.lower() != ".md"
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_BUG_REPORT_INVALID",
                "bug_report.report_path must be a safe relative Markdown path",
            )

        finding_ids: set[str] = set()
        for finding in findings:
            if not isinstance(finding, dict):
                return self._invalid_bug_finding(
                    workflow, step_id, iteration, attempt, "finding must be an object"
                )
            finding_id = finding.get("id")
            evidence = finding.get("evidence")
            reproduction = finding.get("reproduction")
            reproducer = finding.get("reproducer")
            if (
                not isinstance(finding_id, str)
                or re.fullmatch(r"BUG-[0-9]{3}", finding_id) is None
                or finding_id in finding_ids
                or not isinstance(finding.get("title"), str)
                or not finding["title"].strip()
                or len(finding["title"]) > 300
                or finding.get("severity") not in {"critical", "high", "medium", "low"}
                or finding.get("confidence") not in {"high", "medium", "low"}
                or not isinstance(finding.get("root_cause"), str)
                or not finding["root_cause"].strip()
                or not isinstance(evidence, list)
                or not evidence
                or len(evidence) > 20
                or not isinstance(reproduction, dict)
                or not isinstance(reproducer, dict)
            ):
                return self._invalid_bug_finding(
                    workflow,
                    step_id,
                    iteration,
                    attempt,
                    "finding fields, severity, confidence, evidence, reproduction, or reproducer are invalid",
                )
            finding_ids.add(finding_id)
            if any(
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or not item["path"].strip()
                or not isinstance(item.get("description"), str)
                or not item["description"].strip()
                or (
                    item.get("line") is not None
                    and (type(item.get("line")) is not int or item["line"] < 1)
                )
                for item in evidence
            ):
                return self._invalid_bug_finding(
                    workflow, step_id, iteration, attempt, "finding evidence is invalid"
                )
            steps = reproduction.get("steps")
            if (
                not isinstance(steps, list)
                or not steps
                or len(steps) > 20
                or any(not isinstance(item, str) or not item.strip() for item in steps)
                or not isinstance(reproduction.get("expected"), str)
                or not reproduction["expected"].strip()
                or not isinstance(reproduction.get("actual"), str)
                or not reproduction["actual"].strip()
            ):
                return self._invalid_bug_finding(
                    workflow, step_id, iteration, attempt, "finding reproduction is invalid"
                )
            reproducer_path = reproducer.get("path")
            command = reproducer.get("command")
            content = reproducer.get("content")
            reproducer_parts = (
                PurePosixPath(reproducer_path) if isinstance(reproducer_path, str) else None
            )
            if (
                reproducer_parts is None
                or reproducer_parts.is_absolute()
                or ".." in reproducer_parts.parts
                or reproducer_parts.as_posix() != reproducer_path
                or not reproducer_path.startswith("reproducers/")
                or not isinstance(command, list)
                or not 1 <= len(command) <= 32
                or any(
                    not isinstance(item, str) or not item or len(item) > 1000
                    for item in command
                )
                or not isinstance(content, str)
                or not content.strip()
                or len(content.encode("utf-8")) > 256 * 1024
            ):
                return self._invalid_bug_finding(
                    workflow, step_id, iteration, attempt, "finding reproducer is invalid"
                )

        matching_artifacts = [
            artifact
            for artifact in result.artifacts
            if artifact.type in {"report", "document", "file"}
            and self._artifact_relative_path(artifact, result.execution_id) == report_path
        ]
        if len(matching_artifacts) != 1:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_BUG_REPORT_ARTIFACT_INVALID",
                "successful bug finding must return exactly one matching Markdown report artifact",
            )
        stored = self.agent_service.job_store.artifact(result.execution_id, report_path)
        try:
            if stored is None or stored.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("bug report is missing or exceeds 2 MiB")
            content = stored.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError, ValueError) as exc:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_BUG_REPORT_ARTIFACT_INVALID",
                str(exc),
            )
        if len(content) < 100 or not content.startswith("# "):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_BUG_REPORT_ARTIFACT_INVALID",
                "bug report must start with a level-1 heading and contain substantive text",
            )
        if any(finding_id not in content for finding_id in finding_ids):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_BUG_REPORT_ARTIFACT_INVALID",
                "bug report artifact must mention every structured finding id",
            )

        canonical_uri = f"artifact://{result.execution_id}/{report_path}"
        report_artifact = matching_artifacts[0].model_copy(
            update={"type": "report", "uri": canonical_uri}
        )
        return result.model_copy(
            update={
                "data": {**result.data, "bug_report": report},
                "artifacts": [
                    report_artifact if item is matching_artifacts[0] else item
                    for item in result.artifacts
                ],
            }
        )

    def _invalid_bug_finding(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        message: str,
    ) -> StepResult:
        return self._technical_error(
            workflow,
            step_id,
            iteration,
            attempt,
            "AGENT_BUG_FINDING_INVALID",
            message,
        )

    @staticmethod
    def _artifact_relative_path(artifact: ArtifactRef, execution_id: str) -> str | None:
        if not artifact.uri.startswith("artifact://"):
            return None
        path = artifact.uri.removeprefix("artifact://").lstrip("/")
        return path.removeprefix(f"{execution_id}/")

    def _validate_markdown_document_result(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        result: StepResult,
    ) -> StepResult:
        document = result.data.get("document")
        if not isinstance(document, dict):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_MARKDOWN_DOCUMENT_INVALID",
                "successful analysis must return data.document",
            )
        title = document.get("title")
        relative_path = document.get("path")
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 300
            or document.get("format") != "markdown"
            or not isinstance(relative_path, str)
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_MARKDOWN_DOCUMENT_INVALID",
                "data.document must contain title, format=markdown, and path",
            )
        path = PurePosixPath(relative_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != relative_path
            or path.suffix.lower() != ".md"
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_MARKDOWN_DOCUMENT_INVALID",
                "data.document.path must be a safe relative Markdown path",
            )

        matching_artifacts = []
        for artifact in result.artifacts:
            if artifact.type not in {"document", "file"} or not artifact.uri.startswith(
                "artifact://"
            ):
                continue
            artifact_path = artifact.uri.removeprefix("artifact://").lstrip("/")
            execution_prefix = f"{result.execution_id}/"
            artifact_path = artifact_path.removeprefix(execution_prefix)
            if artifact_path == relative_path:
                matching_artifacts.append(artifact)
        unmatched_document_artifacts = [
            artifact
            for artifact in result.artifacts
            if artifact.type == "document" and artifact not in matching_artifacts
        ]
        if len(matching_artifacts) != 1 or unmatched_document_artifacts:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_MARKDOWN_ARTIFACT_INVALID",
                "successful analysis must return exactly one matching Markdown artifact",
            )

        stored = self.agent_service.job_store.artifact(result.execution_id, relative_path)
        if stored is None:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_MARKDOWN_ARTIFACT_MISSING",
                "the declared Markdown document was not written to sandbox output",
            )
        try:
            if stored.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Markdown document exceeds 2 MiB")
            content = stored.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError, ValueError) as exc:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_MARKDOWN_ARTIFACT_INVALID",
                str(exc),
            )
        if len(content) < 100 or not content.startswith("# "):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_MARKDOWN_ARTIFACT_INVALID",
                "Markdown document must start with a level-1 heading and contain substantive text",
            )
        if not re.search(r"\[[^\]\r\n]+\]\(https?://[^)\s]+\)", content):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_MARKDOWN_CITATION_MISSING",
                "successful analysis must contain at least one Markdown source citation",
            )
        request = self.agent_service.job_store.get_request(result.execution_id)
        if request is not None:
            primary_terms = set(
                request.context.retrieval_summary.get("primary_topic_terms", [])
            )
            primary_source_urls: set[str] = set()
            for source in request.context.swirl_results:
                matched_terms = {
                    term
                    for excerpt in source.get("excerpts", [])
                    for term in excerpt.get("matched_terms", [])
                }
                url = source.get("url")
                if primary_terms & matched_terms and isinstance(url, str):
                    primary_source_urls.add(url)
            if primary_source_urls and not any(url in content for url in primary_source_urls):
                return self._technical_error(
                    workflow,
                    step_id,
                    iteration,
                    attempt,
                    "AGENT_MARKDOWN_PRIMARY_SOURCE_MISSING",
                    "analysis must cite a retrieved source that covers the primary topic",
                )

        canonical_uri = f"artifact://{result.execution_id}/{relative_path}"
        artifact = matching_artifacts[0].model_copy(
            update={"type": "document", "uri": canonical_uri}
        )
        normalized_document = {
            **document,
            "title": title.strip(),
            "path": relative_path,
            "format": "markdown",
        }
        return result.model_copy(
            update={
                "data": {**result.data, "document": normalized_document},
                "artifacts": [
                    artifact if item is matching_artifacts[0] else item for item in result.artifacts
                ],
            }
        )

    def _validate_implementation_change_result(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        result: StepResult,
    ) -> StepResult:
        change = result.data.get("implementation_change")
        repository_data = workflow.trigger.data.get("repository")
        expected_repository = (
            repository_data.get("full_name") if isinstance(repository_data, dict) else None
        )
        if not isinstance(change, dict):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_IMPLEMENTATION_CHANGE_INVALID",
                "successful implementation must return data.implementation_change",
            )
        if (
            change.get("repository") != expected_repository
            or change.get("branch") != f"automation/{workflow.id}"
            or not isinstance(change.get("base_ref"), str)
            or not change["base_ref"].strip()
            or not isinstance(change.get("commit"), str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", change["commit"]) is None
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_IMPLEMENTATION_CHANGE_INVALID",
                "implementation change must identify repository, base ref, stable branch, and commit",
            )
        gitea_client = self.command_executor.gitea_client
        if gitea_client is not None:
            try:
                expected_base = None
                required_ancestor = None
                if isinstance(repository_data, dict):
                    required_ancestor = repository_data.get("implementation_commit")
                    expected_base = repository_data.get("implementation_ref")
                if not isinstance(expected_base, str) or not expected_base:
                    expected_base = gitea_client.default_branch(change["repository"])
                if change["base_ref"] != expected_base:
                    raise GiteaClientError(
                        "implementation change was not based on the required source ref"
                    )
                gitea_client.verify_branch(
                    repository=change["repository"],
                    branch=change["branch"],
                    commit=change["commit"],
                )
                if isinstance(required_ancestor, str):
                    gitea_client.verify_descendant(
                        repository=change["repository"],
                        ancestor=required_ancestor,
                        descendant=change["commit"],
                    )
            except GiteaClientError as exc:
                return self._technical_error(
                    workflow,
                    step_id,
                    iteration,
                    attempt,
                    "AGENT_IMPLEMENTATION_BRANCH_UNAVAILABLE",
                    str(exc),
                )
        return result

    def _validate_test_change_result(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        result: StepResult,
    ) -> StepResult:
        change = result.data.get("test_change")
        if not isinstance(change, dict):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_TEST_CHANGE_INVALID",
                "successful test authoring must return data.test_change",
            )
        repository = change.get("repository")
        base_ref = change.get("base_ref")
        branch = change.get("branch")
        commit = change.get("commit")
        base_commit = change.get("base_commit")
        command = change.get("command")
        changed = change.get("changed")
        repository_data = workflow.trigger.data.get("repository")
        expected_repository = (
            repository_data.get("full_name") if isinstance(repository_data, dict) else None
        )
        expected_base_ref = (
            repository_data.get("implementation_ref") if isinstance(repository_data, dict) else None
        )
        expected_branch = f"automation/{workflow.id}"
        expected_base_commit = (
            repository_data.get("implementation_commit")
            if isinstance(repository_data, dict)
            else None
        )
        if (
            not isinstance(repository, str)
            or repository != expected_repository
            or not isinstance(base_ref, str)
            or base_ref != expected_base_ref
            or branch != expected_branch
            or not isinstance(commit, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None
            or not isinstance(base_commit, str)
            or base_commit != expected_base_commit
            or not isinstance(command, list)
            or not 1 <= len(command) <= 32
            or any(not isinstance(item, str) or not item or len(item) > 1000 for item in command)
            or type(changed) is not bool
            or (
                isinstance(commit, str)
                and isinstance(base_commit, str)
                and changed is False
                and commit.lower() != base_commit.lower()
            )
            or (
                isinstance(commit, str)
                and isinstance(base_commit, str)
                and changed is True
                and commit.lower() == base_commit.lower()
            )
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_TEST_CHANGE_INVALID",
                "data.test_change must identify the mapped repository, implementation ref, stable branch, and commit",
            )
        gitea_client = self.command_executor.gitea_client
        if gitea_client is not None:
            try:
                if changed is False:
                    # With no authored changes, the implementation commit is
                    # already the complete test payload. Materialize the
                    # stable workflow ref if the agent omitted that no-op push.
                    gitea_client.ensure_branch(
                        repository=repository,
                        branch=branch,
                        commit=commit,
                    )
                else:
                    gitea_client.verify_branch(
                        repository=repository,
                        branch=branch,
                        commit=commit,
                    )
                gitea_client.verify_descendant(
                    repository=repository,
                    ancestor=base_commit,
                    descendant=commit,
                )
            except GiteaClientError as exc:
                return self._technical_error(
                    workflow,
                    step_id,
                    iteration,
                    attempt,
                    "AGENT_TEST_BRANCH_UNAVAILABLE",
                    str(exc),
                )
        return result

    def _validate_test_execution_result(
        self,
        workflow: WorkflowInstance,
        step_id: str,
        iteration: int,
        attempt: int,
        result: StepResult,
    ) -> StepResult:
        report = result.data.get("test_report")
        authored = next(
            (
                execution.data.get("test_change")
                for execution in reversed(workflow.executions)
                if execution.step_id == "write-tests"
                and execution.execution_status == "COMPLETED"
                and execution.outcome == "SUCCESS"
                and isinstance(execution.data.get("test_change"), dict)
            ),
            None,
        )
        if not isinstance(report, dict) or not isinstance(authored, dict):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_TEST_REPORT_INVALID",
                "test executor must return data.test_report for the authored test change",
            )
        verdict = report.get("verdict")
        command = report.get("command")
        exit_code = report.get("exit_code")
        total = report.get("total")
        passed = report.get("passed")
        failed = report.get("failed")
        errors = report.get("errors", 0)
        summary = report.get("summary")
        same_revision = all(
            report.get(field) == authored.get(field) for field in ("repository", "branch", "commit")
        )
        if (
            verdict not in {"PASSED", "PRODUCT_FAILURE", "TEST_CODE_ERROR"}
            or not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
            or type(exit_code) is not int
            or type(total) is not int
            or total < 0
            or type(passed) is not int
            or passed < 0
            or type(failed) is not int
            or failed < 0
            or type(errors) is not int
            or errors < 0
            or total < passed + failed + errors
            or not isinstance(summary, str)
            or not summary.strip()
            or not same_revision
        ):
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_TEST_REPORT_INVALID",
                "data.test_report has invalid fields or does not match the authored commit",
            )
        valid_outcome = (
            (
                verdict == "PASSED"
                and result.outcome == "SUCCESS"
                and exit_code == 0
                and passed > 0
                and failed == 0
                and errors == 0
            )
            or (
                verdict == "PRODUCT_FAILURE"
                and result.outcome == "SUCCESS"
                and exit_code != 0
                and failed > 0
                and errors == 0
            )
            or (verdict == "TEST_CODE_ERROR" and result.outcome == "FAILURE")
        )
        if not valid_outcome:
            return self._technical_error(
                workflow,
                step_id,
                iteration,
                attempt,
                "AGENT_TEST_VERDICT_INVALID",
                "test verdict, outcome, exit code, and counters are inconsistent",
            )
        return result
