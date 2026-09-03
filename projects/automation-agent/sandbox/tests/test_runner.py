from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess, run
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

import runner
from runner import build_plugin_patch, configure_git_auth, configure_provider, load_agent_result


class LoadAgentResultTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix sockets require Linux")
    def test_configures_git_credentials_without_writing_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth_dir = Path(directory) / ".automation-git-auth"
            auth_socket = auth_dir / "git-auth.sock"
            credential_config = auth_dir / "gitconfig"
            askpass_path = auth_dir / "git-askpass.py"
            with (
                patch.object(runner, "GIT_AUTH_DIR", auth_dir),
                patch.object(runner, "GIT_CREDENTIAL_SOCKET", auth_socket),
                patch.object(runner, "GIT_CREDENTIAL_CONFIG", credential_config),
                patch.object(runner, "GIT_ASKPASS_PATH", askpass_path),
                patch.dict(
                    "os.environ",
                    {"GITEA_USERNAME": "harnes", "GITEA_TOKEN": "secret-token"},
                    clear=True,
                ),
            ):
                server = configure_git_auth()
                self.assertIsNotNone(server)
                try:
                    completed = run(
                        [sys.executable, str(askpass_path), "Password for repository"],
                        capture_output=True,
                        check=False,
                        env={
                            **os.environ,
                            "AUTOMATION_GIT_AUTH_SOCKET": str(auth_socket),
                        },
                        text=True,
                    )
                    self.assertEqual(completed.returncode, 0)
                    self.assertEqual(completed.stdout.strip(), "secret-token")
                    self.assertNotIn("secret-token", askpass_path.read_text(encoding="utf-8"))
                    self.assertNotIn(
                        "secret-token", credential_config.read_text(encoding="utf-8")
                    )
                finally:
                    server.close()

                self.assertEqual(
                    runner.os.environ["GIT_CONFIG_GLOBAL"], str(credential_config)
                )
                self.assertEqual(runner.os.environ["GIT_ASKPASS"], str(askpass_path))
                self.assertEqual(runner.os.environ["GIT_TERMINAL_PROMPT"], "0")
                self.assertFalse(credential_config.exists())
                self.assertFalse(askpass_path.exists())

    def test_configures_native_openrouter_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dsh_home = Path(directory) / ".dsh"
            with patch.object(runner, "DSH_HOME", dsh_home):
                configure_provider("openrouter", "openai/gpt-4.1-nano")

            settings = (dsh_home / "settings.yaml").read_text(encoding="utf-8")
            self.assertIn("provider: openrouter", settings)
            self.assertIn("apiKeyEnv: OPENROUTER_API_KEY", settings)
            self.assertIn('model: "openai/gpt-4.1-nano"', settings)

    def test_builds_patch_with_mandatory_and_requested_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "image-manifest.json"
            dsh_home = root / ".dsh"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "plugins": {
                            "step-result": {
                                "entrypoint": "/plugins/step-result.js",
                                "inject": ["tools"],
                                "config": {},
                                "mandatory": True,
                            },
                            "example": {
                                "entrypoint": "/plugins/example.js",
                                "inject": [],
                                "config": {"mode": "read"},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(runner, "DSH_HOME", dsh_home):
                patch_path = build_plugin_patch(["example"], manifest_path=manifest)

            generated = json.loads(patch_path.read_text(encoding="utf-8"))
            entries = generated[0]["insert"]
            self.assertEqual(
                [item["id"] for item in entries],
                ["automation-step-result", "automation-example"],
            )

    def test_rejects_plugin_missing_from_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "image-manifest.json"
            manifest.write_text(
                json.dumps({"schema_version": 1, "plugins": {}}), encoding="utf-8"
            )
            with (
                patch.object(runner, "DSH_HOME", root / ".dsh"),
                self.assertRaisesRegex(ValueError, "not installed"),
            ):
                build_plugin_patch(["missing"], manifest_path=manifest)

    def test_loads_and_normalizes_native_plugin_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent-result.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "FAILURE",
                        "summary": "  business goal was not achieved  ",
                        "data": {"reason": "missing input"},
                        "artifacts": [
                            {
                                "type": "report",
                                "uri": "artifact://failure.md",
                                "summary": "  details  ",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = load_agent_result(path)

            self.assertEqual(result["outcome"], "FAILURE")
            self.assertEqual(result["summary"], "business goal was not achieved")
            self.assertEqual(result["data"], {"reason": "missing input"})
            self.assertEqual(result["artifacts"][0]["summary"], "details")

    def test_rejects_invalid_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent-result.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "ERROR",
                        "summary": "not a business outcome",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "SUCCESS or FAILURE"):
                load_agent_result(path)

    def test_main_converts_native_failure_to_completed_sandbox_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input" / "task.json"
            output_dir = root / "output"
            workspace_dir = root / "workspace"
            dsh_home = root / "home" / ".dsh"
            agent_result_path = output_dir / "agent-result.json"
            image_manifest_path = root / "image-manifest.json"
            input_path.parent.mkdir(parents=True)
            output_dir.mkdir()
            workspace_dir.mkdir()
            input_path.write_text(
                json.dumps(
                    {
                        "job_id": "exec-1",
                        "prompt": "Do the work",
                        "provider": "openai",
                        "model": "test-model",
                    }
                ),
                encoding="utf-8",
            )
            agent_result_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "FAILURE",
                        "summary": "Business goal was not achieved",
                        "data": {"reason": "missing input"},
                        "artifacts": [],
                    }
                ),
                encoding="utf-8",
            )
            image_manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "plugins": {
                            "step-result": {
                                "entrypoint": "/plugins/step-result.js",
                                "inject": ["tools"],
                                "config": {},
                                "mandatory": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(runner, "INPUT_PATH", input_path),
                patch.object(runner, "OUTPUT_DIR", output_dir),
                patch.object(runner, "WORKSPACE_DIR", workspace_dir),
                patch.object(runner, "DSH_HOME", dsh_home),
                patch.object(runner, "AGENT_RESULT_PATH", agent_result_path),
                patch.object(runner, "IMAGE_MANIFEST_PATH", image_manifest_path),
                patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
                patch.object(
                    runner.subprocess,
                    "run",
                    return_value=CompletedProcess([], 0, stdout="finished", stderr=""),
                ),
            ):
                exit_code = runner.main()

            result = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "failure")
            self.assertEqual(result["data"], {"reason": "missing input"})
            self.assertIsNone(result["error"])


if __name__ == "__main__":
    unittest.main()
