from __future__ import annotations

import io
import os
import tarfile

import pytest

from automation_orchestrator.test_runner import (
    DockerTestRunner,
    TestCounts,
    TestRunnerError,
)


def test_classifies_authoritative_pytest_results():
    passed = DockerTestRunner._classify(
        ["python3", "-m", "pytest"], 0, "======= 7 passed in 0.10s ======="
    )
    product = DockerTestRunner._classify(
        ["python3", "-m", "pytest"], 1, "======= 1 failed, 6 passed in 0.10s ======="
    )
    invalid = DockerTestRunner._classify(
        ["python3", "-m", "pytest"], 2, "IndentationError: expected an indented block"
    )

    assert (passed.verdict, passed.passed, passed.failed) == ("PASSED", 7, 0)
    assert (product.verdict, product.passed, product.failed) == (
        "PRODUCT_FAILURE",
        6,
        1,
    )
    assert invalid.verdict == "TEST_CODE_ERROR"


def test_rejects_zero_tests_and_unknown_success_output():
    zero = DockerTestRunner._classify(
        ["pytest"], 0, "================ no tests ran in 0.01s ================"
    )
    unknown = DockerTestRunner._classify(["./project-check"], 0, "everything looks fine")

    assert zero.verdict == "TEST_CODE_ERROR"
    assert "zero tests" in zero.summary or "no supported" in zero.summary
    assert unknown.verdict == "TEST_CODE_ERROR"
    assert "no supported" in unknown.summary


def test_structured_counts_distinguish_product_failures_from_framework_errors():
    product = DockerTestRunner._classify(
        ["pytest"],
        1,
        "one assertion failed",
        counts=TestCounts("pytest", total=3, passed=2, failed=1),
        framework="pytest",
    )
    broken = DockerTestRunner._classify(
        ["pytest"],
        1,
        "collection error",
        counts=TestCounts("pytest", total=1, passed=0, failed=0, errors=1),
        framework="pytest",
    )

    assert product.verdict == "PRODUCT_FAILURE"
    assert broken.verdict == "TEST_CODE_ERROR"


def test_supported_framework_cannot_fall_back_when_structured_report_is_missing():
    result = DockerTestRunner._classify(
        ["pytest"],
        0,
        "1 passed in 0.01s",
        framework="pytest",
        require_structured=True,
    )

    assert result.verdict == "TEST_CODE_ERROR"
    assert "structured test report is missing" in result.summary


def test_prepares_structured_report_commands_for_supported_frameworks():
    pytest_framework, pytest_command, pytest_report = DockerTestRunner._prepare_command(
        ["python3", "-m", "pytest", "-q"]
    )
    jest_framework, jest_command, jest_report = DockerTestRunner._prepare_command(
        ["npx", "jest", "--runInBand"]
    )
    go_framework, go_command, go_report = DockerTestRunner._prepare_command(
        ["go", "test", "./..."]
    )
    dotnet_framework, dotnet_command, dotnet_report = DockerTestRunner._prepare_command(
        ["dotnet", "test"]
    )

    assert (pytest_framework, pytest_report) == ("pytest", "/tmp/automation-pytest.xml")
    assert any(item.startswith("--junitxml=") for item in pytest_command)
    assert (jest_framework, jest_report) == ("jest", "/tmp/automation-jest.json")
    assert "--json" in jest_command
    assert (go_framework, go_command, go_report) == (
        "go",
        ["go", "test", "-json", "./..."],
        None,
    )
    assert dotnet_framework == "dotnet"
    assert "--logger" in dotnet_command
    assert dotnet_report == "/tmp/automation-dotnet"


def test_parses_pytest_junit_and_jest_json_reports():
    pytest_counts = DockerTestRunner._parse_structured_report(
        "pytest",
        b"""<testsuite tests="3" failures="1" errors="0" skipped="1">
        <testcase name="passes" />
        <testcase name="fails"><failure /></testcase>
        <testcase name="skips"><skipped /></testcase>
        </testsuite>""",
    )
    jest_counts = DockerTestRunner._parse_structured_report(
        "jest",
        b'{"numTotalTests":4,"numPassedTests":3,"numFailedTests":1,'
        b'"numPendingTests":0,"numRuntimeErrorTestSuites":0}',
    )

    assert pytest_counts == TestCounts("pytest", 3, 1, 1, skipped=1)
    assert jest_counts == TestCounts("jest", 4, 3, 1)


def test_archive_normalization_strips_repository_root_and_rejects_links():
    runner = DockerTestRunner("unused")
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w:gz") as archive:
        payload = b"print('ok')\n"
        item = tarfile.TarInfo("service-sha/test_sample.py")
        item.size = len(payload)
        archive.addfile(item, io.BytesIO(payload))

    normalized = runner._normalize_archive(source.getvalue())
    with tarfile.open(fileobj=io.BytesIO(normalized), mode="r:") as archive:
        assert archive.getnames() == ["test_sample.py"]

    unsafe = io.BytesIO()
    with tarfile.open(fileobj=unsafe, mode="w:gz") as archive:
        item = tarfile.TarInfo("service-sha/link")
        item.type = tarfile.SYMTYPE
        item.linkname = "/etc/passwd"
        archive.addfile(item)
    with pytest.raises(TestRunnerError, match="unsafe"):
        runner._normalize_archive(unsafe.getvalue())


def test_reproducer_overlay_is_bounded_and_rejects_unsafe_paths():
    overlay = DockerTestRunner._build_overlay_archive(
        {"reproducers/test_bug.py": b"def test_bug():\n    assert False\n"}
    )

    with tarfile.open(fileobj=io.BytesIO(overlay), mode="r:") as archive:
        assert archive.getnames() == ["reproducers/test_bug.py"]
        assert archive.extractfile("reproducers/test_bug.py").read().startswith(b"def test_bug")

    with pytest.raises(TestRunnerError, match="invalid path"):
        DockerTestRunner._build_overlay_archive({"../escape.py": b"bad"})

    with pytest.raises(TestRunnerError, match="exceeds 1 MiB"):
        DockerTestRunner._build_overlay_archive(
            {"reproducers/large.py": b"x" * (1024 * 1024 + 1)}
        )


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("HARNES_DOCKER_E2E") != "1",
    reason="set HARNES_DOCKER_E2E=1 to run the isolated test runner",
)
def test_docker_runner_reads_structured_pytest_report_and_rejects_empty_suite():
    runner = DockerTestRunner(
        os.environ.get(
            "TEST_RUNNER_IMAGE", "automation-dsh-sandbox-delivery:0.1.0-rc.7"
        )
    )

    passing_archive = io.BytesIO()
    with tarfile.open(fileobj=passing_archive, mode="w:gz") as archive:
        payload = b"def test_real_execution():\n    assert 2 + 2 == 4\n"
        item = tarfile.TarInfo("service/test_sample.py")
        item.size = len(payload)
        archive.addfile(item, io.BytesIO(payload))
    passed = runner.run(passing_archive.getvalue(), ["pytest", "-q"])

    empty_archive = io.BytesIO()
    with tarfile.open(fileobj=empty_archive, mode="w:gz") as archive:
        payload = b"No tests here\n"
        item = tarfile.TarInfo("service/README.md")
        item.size = len(payload)
        archive.addfile(item, io.BytesIO(payload))
    empty = runner.run(empty_archive.getvalue(), ["pytest", "-q"])
    reproduced = runner.run(
        empty_archive.getvalue(),
        ["pytest", "-q", "reproducers/test_bug.py"],
        overlay_files={
            "reproducers/test_bug.py": b"def test_bug():\n    assert 2 == 1\n"
        },
    )

    assert (passed.verdict, passed.framework, passed.total, passed.passed) == (
        "PASSED",
        "pytest",
        1,
        1,
    )
    assert empty.verdict == "TEST_CODE_ERROR"
    assert empty.total == 0
    assert (reproduced.verdict, reproduced.framework, reproduced.failed) == (
        "PRODUCT_FAILURE",
        "pytest",
        1,
    ), reproduced.output
