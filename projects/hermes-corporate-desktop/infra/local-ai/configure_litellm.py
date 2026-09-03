from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil


def _block_end(lines: list[str], start: int, child_indent: int) -> int:
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if line.strip() and not line.lstrip().startswith("#"):
            indent = len(line) - len(line.lstrip(" "))
            if indent < child_indent:
                break
        index += 1
    return index


def _replace_top_level_block(lines: list[str], name: str, replacement: list[str]) -> list[str]:
    pattern = re.compile(rf"^{re.escape(name)}:\s*$")
    for index, line in enumerate(lines):
        if pattern.match(line):
            end = _block_end(lines, index, 2)
            return [*lines[:index], *replacement, *lines[end:]]
    return [*replacement, "", *lines]


def _upsert_provider(lines: list[str], replacement: list[str]) -> list[str]:
    providers_index = next((i for i, line in enumerate(lines) if line == "providers:"), None)
    if providers_index is None:
        model_index = next(i for i, line in enumerate(lines) if line == "model:")
        insert_at = _block_end(lines, model_index, 2)
        block = ["providers:", *replacement, ""]
        return [*lines[:insert_at], *block, *lines[insert_at:]]

    providers_end = _block_end(lines, providers_index, 2)
    for index in range(providers_index + 1, providers_end):
        if lines[index] == "  litellm:":
            end = _block_end(lines, index, 4)
            return [*lines[:index], *replacement, *lines[end:]]
    return [*lines[: providers_index + 1], *replacement, *lines[providers_index + 1 :]]


def configure(config_path: Path, model: str, base_url: str) -> None:
    backup_path = config_path.with_name("config.yaml.before-litellm")
    if not backup_path.exists():
        shutil.copy2(config_path, backup_path)

    lines = config_path.read_text(encoding="utf-8").splitlines()
    model_block = [
        "model:",
        "  provider: custom:litellm",
        f"  default: {model}",
        f"  base_url: {base_url}",
    ]
    provider_block = [
        "  litellm:",
        f"    api: {base_url}",
        "    key_env: LITELLM_MASTER_KEY",
        "    transport: chat_completions",
        f"    default_model: {model}",
        "    discover_models: true",
    ]
    lines = _replace_top_level_block(lines, "model", model_block)
    lines = _upsert_provider(lines, provider_block)
    config_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure DIT Agent to use LiteLLM")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    options = parser.parse_args()
    configure(options.config, options.model, options.base_url)


if __name__ == "__main__":
    main()
