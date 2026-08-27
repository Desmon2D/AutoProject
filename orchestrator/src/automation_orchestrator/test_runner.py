from __future__ import annotations

import io
import json
import re
import tarfile
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import PurePosixPath

import docker
from docker.errors import DockerException, NotFound


class TestRunnerError(RuntimeError):
    __test__ = False


@dataclass(frozen=True)
class TestRun:
    __test__ = False

    command: list[str]
    exit_code: int
    passed: int
    failed: int
    verdict: str
    summary: str
    output: str
    framework: str = "unknown"
    total: int = 0
    errors: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class TestCounts:
    __test__ = False

    framework: str
    total: int
    passed: int
    failed: int
    errors: int = 0
    skipped: int = 0


class DockerTestRunner:
    """Runs an exact repository archive without model access, network, or credentials."""

    def __init__(
        self,
        image: str,
        *,
        timeout_seconds: int = 900,
        max_archive_bytes: int = 100 * 1024 * 1024,
        max_output_bytes: int = 256 * 1024,
    ):
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.max_archive_bytes = max_archive_bytes
        self.max_output_bytes = max_output_bytes

    def run(
        self,
        archive: bytes,
        command: list[str],
        *,
        overlay_files: dict[str, bytes] | None = None,
    ) -> TestRun:
        if not command or len(command) > 32 or any(
            not isinstance(item, str) or not item or len(item) > 1000 for item in command
        ):
            raise TestRunnerError("test command must be a non-empty argument array")
        normalized = self._normalize_archive(archive)
        framework, execution_command, report_path = self._prepare_command(command)
        container = None
        seeder = None
        volume = None
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            client = docker.from_env()
            volume = client.volumes.create(
                name=f"automation-test-{uuid.uuid4().hex[:12]}",
                labels={"automation.role": "test-workspace"},
            )
            workspace_mount = {volume.name: {"bind": "/workspace", "mode": "rw"}}
            seeder = client.containers.create(
                self.image,
                name=f"automation-test-seed-{uuid.uuid4().hex[:12]}",
                command=["sh", "-lc", "while :; do sleep 3600; done"],
                detach=True,
                user="10001:10001",
                network_mode="none",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                volumes=workspace_mount,
                labels={"automation.role": "test-seeder"},
            )
            seeder.start()
            if not seeder.put_archive("/workspace", normalized):
                raise TestRunnerError("cannot copy repository archive into test workspace")
            if overlay_files:
                overlay = self._build_overlay_archive(overlay_files)
                if not seeder.put_archive("/workspace", overlay):
                    raise TestRunnerError("cannot copy test overlay into test workspace")
            seeder.remove(force=True)
            seeder = None
            container = client.containers.create(
                self.image,
                name=f"automation-test-{uuid.uuid4().hex[:12]}",
                command=["sh", "-lc", "while :; do sleep 3600; done"],
                detach=True,
                user="10001:10001",
                network_mode="none",
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                mem_limit="2g",
                nano_cpus=2_000_000_000,
                pids_limit=256,
                volumes=workspace_mount,
                tmpfs={
                    "/tmp": "rw,nosuid,nodev,size=128m,uid=10001,gid=10001",
                    "/home/sandbox": "rw,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
                },
                labels={"automation.role": "test-runner"},
            )
            container.start()
            future = pool.submit(
                container.exec_run,
                execution_command,
                workdir="/workspace",
                demux=True,
            )
            try:
                execution = future.result(timeout=self.timeout_seconds)
            except FutureTimeoutError as exc:
                container.kill()
                raise TestRunnerError(
                    f"test command exceeded {self.timeout_seconds} seconds"
                ) from exc
            stdout, stderr = execution.output or (b"", b"")
            combined = self._decode_output(stdout or b"", stderr or b"")
            counts = None
            if report_path is not None:
                report = self._read_report(container, report_path)
                if report is not None:
                    counts = self._parse_structured_report(framework, report)
            elif framework == "go":
                counts = self._parse_go_json(combined)
            return self._classify(
                command,
                execution.exit_code,
                combined,
                counts=counts,
                framework=framework,
                require_structured=framework in {"pytest", "jest", "go", "dotnet"},
            )
        except DockerException as exc:
            raise TestRunnerError(f"Docker test execution failed: {exc}") from exc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            if seeder is not None:
                try:
                    seeder.remove(force=True)
                except DockerException:
                    pass
            if container is not None:
                try:
                    container.remove(force=True)
                except DockerException:
                    pass
            if volume is not None:
                try:
                    volume.remove(force=True)
                except DockerException:
                    pass

    @staticmethod
    def _build_overlay_archive(files: dict[str, bytes]) -> bytes:
        if not files or len(files) > 50:
            raise TestRunnerError("test overlay must contain between 1 and 50 files")
        output = io.BytesIO()
        total_size = 0
        with tarfile.open(fileobj=output, mode="w") as archive:
            for relative_path, content in sorted(files.items()):
                path = PurePosixPath(relative_path)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or path.as_posix() != relative_path
                    or not isinstance(content, bytes)
                ):
                    raise TestRunnerError("test overlay contains an invalid path or payload")
                total_size += len(content)
                if total_size > 1024 * 1024:
                    raise TestRunnerError("test overlay exceeds 1 MiB")
                member = tarfile.TarInfo(relative_path)
                member.size = len(content)
                member.uid = 10001
                member.gid = 10001
                member.uname = "sandbox"
                member.gname = "sandbox"
                member.mode = 0o600
                archive.addfile(member, io.BytesIO(content))
        return output.getvalue()

    def _normalize_archive(self, archive: bytes) -> bytes:
        if not archive or len(archive) > self.max_archive_bytes:
            raise TestRunnerError("repository archive is empty or exceeds the size limit")
        output = io.BytesIO()
        total_size = 0
        file_count = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as source:
                members = source.getmembers()
                roots = {
                    PurePosixPath(member.name).parts[0]
                    for member in members
                    if PurePosixPath(member.name).parts
                }
                strip_root = len(roots) == 1
                with tarfile.open(fileobj=output, mode="w") as target:
                    for member in members:
                        path = PurePosixPath(member.name)
                        parts = path.parts[1:] if strip_root else path.parts
                        if not parts:
                            continue
                        if path.is_absolute() or ".." in parts:
                            raise TestRunnerError("repository archive contains an invalid path")
                        if member.issym() or member.islnk() or member.isdev():
                            raise TestRunnerError("repository archive contains an unsafe entry")
                        if not (member.isdir() or member.isfile()):
                            continue
                        relative = PurePosixPath(*parts).as_posix()
                        info = tarfile.TarInfo(relative)
                        info.uid = 10001
                        info.gid = 10001
                        info.uname = "sandbox"
                        info.gname = "sandbox"
                        info.mtime = 0
                        if member.isdir():
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            target.addfile(info)
                            continue
                        total_size += member.size
                        file_count += 1
                        if total_size > self.max_archive_bytes or file_count > 20_000:
                            raise TestRunnerError("repository archive expands beyond safety limits")
                        info.size = member.size
                        info.mode = 0o755 if member.mode & 0o111 else 0o644
                        payload = source.extractfile(member)
                        if payload is None:
                            raise TestRunnerError("repository archive contains an unreadable file")
                        target.addfile(info, payload)
        except (tarfile.TarError, OSError) as exc:
            raise TestRunnerError(f"cannot read repository archive: {exc}") from exc
        return output.getvalue()

    def _decode_output(self, stdout: bytes, stderr: bytes) -> str:
        payload = stdout + (b"\n" if stdout and stderr else b"") + stderr
        if len(payload) > self.max_output_bytes:
            payload = payload[-self.max_output_bytes :]
        return payload.decode("utf-8", errors="replace")

    @classmethod
    def _prepare_command(cls, command: list[str]) -> tuple[str, list[str], str | None]:
        executable = PurePosixPath(command[0]).name.casefold()
        is_python_module = lambda module: any(
            item == "-m" and index + 1 < len(command) and command[index + 1] == module
            for index, item in enumerate(command)
        )
        if executable in {"pytest", "py.test"} or is_python_module("pytest"):
            report_path = "/tmp/automation-pytest.xml"
            prepared = cls._without_options(command, {"--junitxml", "--junit-xml"})
            return "pytest", [*prepared, f"--junitxml={report_path}"], report_path
        if executable == "jest" or (
            executable in {"npx", "pnpx", "yarn"}
            and len(command) > 1
            and PurePosixPath(command[1]).name.casefold() == "jest"
        ):
            report_path = "/tmp/automation-jest.json"
            prepared = cls._without_options(command, {"--outputFile", "--output-file"})
            prepared = [item for item in prepared if item != "--json"]
            return "jest", [*prepared, "--json", f"--outputFile={report_path}"], report_path
        if executable == "go" and len(command) > 1 and command[1] == "test":
            prepared = list(command)
            if "-json" not in prepared[2:]:
                prepared.insert(2, "-json")
            return "go", prepared, None
        if executable == "dotnet" and len(command) > 1 and command[1] == "test":
            report_path = "/tmp/automation-dotnet"
            prepared = cls._without_options(command, {"--logger", "--results-directory"})
            return (
                "dotnet",
                [
                    *prepared,
                    "--logger",
                    "trx;LogFileName=automation-tests.trx",
                    "--results-directory",
                    report_path,
                ],
                report_path,
            )
        if is_python_module("unittest"):
            return "unittest", list(command), None
        if executable == "node" and "--test" in command[1:]:
            return "tap", list(command), None
        return "unknown", list(command), None

    @staticmethod
    def _without_options(command: list[str], names: set[str]) -> list[str]:
        prepared: list[str] = []
        skip_next = False
        for item in command:
            if skip_next:
                skip_next = False
                continue
            if item in names:
                skip_next = True
                continue
            if any(item.startswith(f"{name}=") for name in names):
                continue
            prepared.append(item)
        return prepared

    @staticmethod
    def _read_report(container, path: str) -> bytes | None:
        try:
            chunks, _metadata = container.get_archive(path)
            payload = io.BytesIO(b"".join(chunks))
            with tarfile.open(fileobj=payload, mode="r:") as archive:
                files = [member for member in archive.getmembers() if member.isfile()]
                if not files:
                    return None
                selected = next(
                    (
                        member
                        for member in files
                        if member.name.endswith((".xml", ".json", ".trx"))
                    ),
                    files[0],
                )
                extracted = archive.extractfile(selected)
                return extracted.read() if extracted is not None else None
        except NotFound:
            return DockerTestRunner._read_report_via_exec(container, path)
        except (DockerException, OSError, tarfile.TarError) as exc:
            raise TestRunnerError(f"cannot read structured test report: {exc}") from exc

    @staticmethod
    def _read_report_via_exec(container, path: str) -> bytes | None:
        try:
            target = path
            probe = container.exec_run(["test", "-f", target])
            if probe.exit_code != 0:
                listing = container.exec_run(["find", path, "-type", "f"])
                if listing.exit_code != 0:
                    return None
                candidates = [
                    line
                    for line in listing.output.decode("utf-8", errors="replace").splitlines()
                    if line.endswith((".xml", ".json", ".trx"))
                ]
                if not candidates:
                    return None
                target = candidates[0]
            report = container.exec_run(["cat", target])
            if report.exit_code != 0 or not isinstance(report.output, bytes):
                return None
            if len(report.output) > 16 * 1024 * 1024:
                raise TestRunnerError("structured test report exceeds 16 MiB")
            return report.output
        except DockerException as exc:
            raise TestRunnerError(f"cannot read structured test report: {exc}") from exc

    @staticmethod
    def _parse_structured_report(framework: str, payload: bytes) -> TestCounts | None:
        try:
            if framework == "pytest":
                root = ET.fromstring(payload)
                cases = root.findall(".//testcase")
                if root.tag.endswith("testcase"):
                    cases = [root]
                failed = sum(case.find("failure") is not None for case in cases)
                errors = sum(case.find("error") is not None for case in cases)
                skipped = sum(case.find("skipped") is not None for case in cases)
                total = len(cases)
                return TestCounts(
                    framework=framework,
                    total=total,
                    passed=max(total - failed - errors - skipped, 0),
                    failed=failed,
                    errors=errors,
                    skipped=skipped,
                )
            if framework == "jest":
                data = json.loads(payload)
                if not isinstance(data, dict):
                    return None
                return TestCounts(
                    framework=framework,
                    total=int(data.get("numTotalTests", 0)),
                    passed=int(data.get("numPassedTests", 0)),
                    failed=int(data.get("numFailedTests", 0)),
                    errors=int(data.get("numRuntimeErrorTestSuites", 0)),
                    skipped=int(data.get("numPendingTests", 0)),
                )
            if framework == "dotnet":
                root = ET.fromstring(payload)
                counters = root.find(".//{*}Counters")
                if counters is None:
                    return None
                return TestCounts(
                    framework=framework,
                    total=int(counters.get("total", "0")),
                    passed=int(counters.get("passed", "0")),
                    failed=int(counters.get("failed", "0")),
                    errors=sum(
                        int(counters.get(name, "0"))
                        for name in ("error", "timeout", "aborted")
                    ),
                    skipped=int(counters.get("notExecuted", "0")),
                )
        except (ET.ParseError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None
        return None

    @staticmethod
    def _parse_go_json(output: str) -> TestCounts | None:
        outcomes: dict[tuple[str, str], str] = {}
        parse_error = False
        for line in output.splitlines():
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                parse_error = True
                continue
            test = event.get("Test")
            action = event.get("Action")
            package = event.get("Package", "")
            if isinstance(test, str) and action in {"pass", "fail", "skip"}:
                outcomes[(str(package), test)] = action
        if not outcomes and not parse_error:
            return None
        passed = sum(value == "pass" for value in outcomes.values())
        failed = sum(value == "fail" for value in outcomes.values())
        skipped = sum(value == "skip" for value in outcomes.values())
        return TestCounts(
            framework="go",
            total=len(outcomes),
            passed=passed,
            failed=failed,
            errors=int(parse_error),
            skipped=skipped,
        )

    @classmethod
    def _classify(
        cls,
        command: list[str],
        exit_code: int,
        output: str,
        *,
        counts: TestCounts | None = None,
        framework: str | None = None,
        require_structured: bool = False,
    ) -> TestRun:
        detected_framework = framework or cls._prepare_command(command)[0]
        if counts is None and not require_structured:
            counts = cls._parse_text_counts(detected_framework, output)
        passed = counts.passed if counts is not None else 0
        failed = counts.failed if counts is not None else 0
        errors = counts.errors if counts is not None else 0
        skipped = counts.skipped if counts is not None else 0
        total = counts.total if counts is not None else 0
        framework_name = counts.framework if counts is not None else detected_framework
        invalid_patterns = (
            r"ERROR collecting",
            r"IndentationError",
            r"SyntaxError",
            r"no tests ran",
            r"no tests found",
            r"unrecognized arguments",
            r"command not found",
        )
        invalid_output = any(
            re.search(pattern, output, re.IGNORECASE) for pattern in invalid_patterns
        )
        executed = passed + failed + errors
        if exit_code == 0 and counts is not None and executed > 0 and failed == 0 and errors == 0:
            verdict = "PASSED"
            summary = f"Authoritative {framework_name} run passed ({passed} passed)"
        elif exit_code != 0 and failed > 0 and errors == 0 and not invalid_output:
            verdict = "PRODUCT_FAILURE"
            summary = f"Authoritative {framework_name} run found {failed} failing test(s)"
        else:
            verdict = "TEST_CODE_ERROR"
            if counts is None:
                reason = (
                    "the required structured test report is missing"
                    if require_structured
                    else "no supported structured or textual test report"
                )
            elif executed == 0:
                reason = "zero tests were executed"
            elif errors > 0:
                reason = f"the test framework reported {errors} error(s)"
            else:
                reason = f"test process exited with code {exit_code}"
            summary = f"Authoritative test run could not validate the product: {reason}"
        return TestRun(
            command=command,
            exit_code=exit_code,
            passed=passed,
            failed=failed,
            verdict=verdict,
            summary=summary,
            output=output[-12_000:],
            framework=framework_name,
            total=total,
            errors=errors,
            skipped=skipped,
        )

    @staticmethod
    def _parse_text_counts(framework: str, output: str) -> TestCounts | None:
        def last(pattern: str) -> int:
            values = re.findall(pattern, output, re.IGNORECASE)
            return int(values[-1]) if values else 0

        if framework == "unittest":
            total = last(r"Ran\s+(\d+)\s+tests?")
            failed = last(r"failures=(\d+)")
            errors = last(r"errors=(\d+)")
            skipped = last(r"skipped=(\d+)")
            if total:
                return TestCounts(
                    framework=framework,
                    total=total,
                    passed=max(total - failed - errors - skipped, 0),
                    failed=failed,
                    errors=errors,
                    skipped=skipped,
                )
        if framework == "tap":
            total = last(r"(?m)^1\.\.(\d+)\s*$")
            failed = len(re.findall(r"(?m)^not ok\b", output, re.IGNORECASE))
            passed = len(re.findall(r"(?m)^ok\b", output, re.IGNORECASE))
            if total:
                return TestCounts(framework=framework, total=total, passed=passed, failed=failed)
        dotnet = re.findall(
            r"Failed:\s*(\d+).*?Passed:\s*(\d+).*?Skipped:\s*(\d+).*?Total:\s*(\d+)",
            output,
            re.IGNORECASE,
        )
        if dotnet:
            failed, passed, skipped, total = map(int, dotnet[-1])
            return TestCounts("dotnet", total, passed, failed, skipped=skipped)
        jest_lines = [line for line in output.splitlines() if line.lstrip().startswith("Tests:")]
        if jest_lines:
            total = last(r"Tests:.*?(\d+)\s+total")
            passed = last(r"Tests:.*?(\d+)\s+passed")
            failed = last(r"Tests:.*?(\d+)\s+failed")
            skipped = last(r"Tests:.*?(\d+)\s+(?:skipped|pending)")
            if total:
                return TestCounts("jest", total, passed, failed, skipped=skipped)
        passed = last(r"(\d+)\s+passed\b")
        failed = last(r"(\d+)\s+failed\b")
        errors = last(r"(\d+)\s+errors?\b")
        skipped = last(r"(\d+)\s+skipped\b")
        total = passed + failed + errors + skipped
        if total:
            return TestCounts(framework or "unknown", total, passed, failed, errors, skipped)
        return None
