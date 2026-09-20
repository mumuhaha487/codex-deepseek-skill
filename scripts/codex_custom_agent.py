#!/usr/bin/env python3
"""配置并验证用户指定模型作为 Codex 原生子 Agent。"""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass


configure_utf8_stdio()

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # macOS / Linux
    msvcrt = None


SKILL_NAME = "deepseek"
LEGACY_RUNTIME_ID = "codex-custom-subagent"
ROLE = "CustomAgent"
AGENT_SANDBOX_MODE = "workspace-write"
AGENT_EXECUTION_MODE = "isolated_git_worktree"
DEFAULT_EFFORT = "high"
REASONING_EFFORTS = {"low", "medium", "high"}
VISION_VALUES = {"yes", "no"}
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
PARENT_MULTI_AGENT_VERSION = "v1"
DESKTOP_MULTI_AGENT_V2 = False
REMOVED_FEATURE_FLAGS = ("thread_tools",)
MAX_STATE_DATABASES = 32
METADATA_WAIT_SECONDS = 5.0
LOCK_WAIT_SECONDS = 5.0
CREDENTIAL_TARGET = f"{LEGACY_RUNTIME_ID}-api-key"
PROVIDER_BEGIN = "# BEGIN CODEX-CUSTOM-SUBAGENT PROVIDER"
PROVIDER_END = "# END CODEX-CUSTOM-SUBAGENT PROVIDER"
ROLE_BEGIN = "# BEGIN CODEX-CUSTOM-SUBAGENT ROLE"
ROLE_END = "# END CODEX-CUSTOM-SUBAGENT ROLE"
DESKTOP_CODEX_CANDIDATES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
)
WINDOWS_CODEX_RELATIVE_CANDIDATES = (
    Path("Programs") / "Codex" / "resources" / "codex.exe",
    Path("Programs") / "OpenAI" / "Codex" / "resources" / "codex.exe",
    Path("Codex") / "resources" / "codex.exe",
)


class ManagerError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class Paths:
    home: Path
    config: Path
    catalog: Path
    agent: Path
    state_dir: Path
    manifest: Path


def resolve_paths(codex_home: str | None) -> Paths:
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    return Paths(
        home=home,
        config=home / "config.toml",
        catalog=home / "models-with-custom-agent.json",
        agent=home / "agents" / f"{ROLE}.toml",
        # Keep the runtime state path stable so renaming the Skill does not lose
        # the existing manifest, backups, or credential association.
        state_dir=home / LEGACY_RUNTIME_ID,
        manifest=home / LEGACY_RUNTIME_ID / "manifest.json",
    )


def result(status: str, **kwargs: Any) -> dict[str, Any]:
    return {"status": status, **kwargs}


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(payload.get("status", "unknown"))
    for key, value in payload.items():
        if key != "status":
            print(f"{key}: {value}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text_file(path: Path) -> str:
    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalized.encode())


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt" or sys.platform == "win32":
        return "windows"
    return "unsupported"


def find_desktop_codex() -> str:
    configured = os.environ.get("CODEX_DESKTOP_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise ManagerError(
            "desktop_codex_missing",
            f"CODEX_DESKTOP_BIN 指向的文件不存在：{candidate}",
        )

    candidates: list[Path] = []
    if platform_name() == "macos":
        candidates.extend(DESKTOP_CODEX_CANDIDATES)
    elif platform_name() == "windows":
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            root = os.environ.get(variable)
            if root:
                candidates.extend(Path(root) / relative for relative in WINDOWS_CODEX_RELATIVE_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    if platform_name() == "windows":
        discovered = shutil.which("codex.exe") or shutil.which("codex")
        if discovered:
            return discovered

    raise ManagerError(
        "desktop_codex_missing",
        "没有找到 Codex 桌面应用内置运行时。请先安装或启动桌面应用；Windows 自动发现失败时设置 CODEX_DESKTOP_BIN。",
    )


def codex_version_text(codex_bin: str) -> str:
    proc = subprocess.run(
        [codex_bin, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    text = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0 or not text:
        raise ManagerError("codex_version_unknown", "无法读取 Codex 桌面应用内置运行时版本。")
    return text


def credential_account() -> str:
    return getpass.getuser()


def credential_backend() -> str | None:
    current = platform_name()
    if current == "macos" and Path("/usr/bin/security").is_file():
        return "macos-keychain"
    if current == "windows":
        return "windows-credential-manager"
    return None


def _macos_read_credential() -> str | None:
    proc = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            credential_account(),
            "-s",
            CREDENTIAL_TARGET,
            "-w",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\r\n") or None


def _windows_credential_api():
    import ctypes
    from ctypes import wintypes

    class CredentialW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CredentialW)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return ctypes, CredentialW, advapi32


def _windows_read_credential() -> str | None:
    ctypes, credential_type, advapi32 = _windows_credential_api()
    credential = ctypes.POINTER(credential_type)()
    if not advapi32.CredReadW(CREDENTIAL_TARGET, 1, 0, ctypes.byref(credential)):
        error = ctypes.get_last_error()
        if error == 1168:
            return None
        raise ManagerError(
            "credential_read_failed",
            f"无法读取 Windows Credential Manager（错误 {error}）。",
        )
    try:
        raw = ctypes.string_at(
            credential.contents.CredentialBlob,
            credential.contents.CredentialBlobSize,
        )
        return raw.decode("utf-8")
    finally:
        advapi32.CredFree(credential)


def read_credential_key() -> str | None:
    backend = credential_backend()
    if backend == "macos-keychain":
        return _macos_read_credential()
    if backend == "windows-credential-manager":
        return _windows_read_credential()
    raise ManagerError("unsupported_platform", "当前只支持 macOS 和 Windows 系统凭据库。")


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def parse_toml_text(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManagerError("invalid_config", f"config.toml 无法解析：{exc}") from exc


def remove_marked_block(text: str, begin: str, end: str) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(begin)}.*?{re.escape(end)}\n?",
        flags=re.DOTALL,
    )
    return pattern.sub("\n", text).rstrip() + ("\n" if text else "")


def remove_managed_blocks(text: str) -> str:
    text = remove_marked_block(text, PROVIDER_BEGIN, PROVIDER_END)
    return remove_marked_block(text, ROLE_BEGIN, ROLE_END)


def toml_table_header(table: str) -> re.Pattern[str]:
    tokens = [
        rf"(?:{re.escape(part)}|\"{re.escape(part)}\"|'{re.escape(part)}')"
        for part in table.split(".")
    ]
    return re.compile(r"^\[\s*" + r"\s*\.\s*".join(tokens) + r"\s*\]\s*(?:#.*)?$")


def remove_toml_table(text: str, table: str) -> str:
    lines = text.splitlines()
    header = toml_table_header(table)
    start = next((index for index, line in enumerate(lines) if header.match(line.strip())), None)
    if start is None:
        return text
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[")),
        len(lines),
    )
    kept = lines[:start] + lines[end:]
    return "\n".join(kept).rstrip() + "\n"


def top_level_key(text: str, key: str) -> str | None:
    value = parse_toml_text(text).get(key)
    return value if isinstance(value, str) else None


def set_top_level_key(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    assignment = f"{key} = {toml_string(value)}"
    first_table = next((i for i, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(first_table):
        if key_pattern.match(lines[index]):
            lines[index] = assignment
            return "\n".join(lines).rstrip() + "\n"
    lines.insert(first_table, assignment)
    if first_table and lines[first_table - 1].strip():
        lines.insert(first_table + 1, "")
    return "\n".join(lines).rstrip() + "\n"


def remove_top_level_key_if_value(text: str, key: str, expected: str) -> str:
    lines = text.splitlines()
    first_table = next((i for i, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
    kept: list[str] = []
    for index, line in enumerate(lines):
        matches = False
        if index < first_table:
            try:
                matches = tomllib.loads(line).get(key) == expected
            except tomllib.TOMLDecodeError:
                matches = False
        if not matches:
            kept.append(line)
    return "\n".join(kept).rstrip() + "\n"


def set_table_bool(text: str, table: str, key: str, value: bool) -> str:
    lines = text.splitlines()
    assignment = f"{key} = {'true' if value else 'false'}"
    header = toml_table_header(table)
    start = next((index for index, line in enumerate(lines) if header.match(line.strip())), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend((f"[{table}]", assignment))
        return "\n".join(lines).rstrip() + "\n"
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[")),
        len(lines),
    )
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(start + 1, end):
        if key_pattern.match(lines[index]):
            lines[index] = assignment
            return "\n".join(lines).rstrip() + "\n"
    lines.insert(end, assignment)
    return "\n".join(lines).rstrip() + "\n"


def remove_table_bool_if_value(text: str, table: str, key: str, expected: bool) -> str:
    lines = text.splitlines()
    header = toml_table_header(table)
    start = next((index for index, line in enumerate(lines) if header.match(line.strip())), None)
    if start is None:
        return text
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[")),
        len(lines),
    )
    expected_text = "true" if expected else "false"
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*{expected_text}\s*(?:#.*)?$")
    kept = [
        line
        for index, line in enumerate(lines)
        if not (start < index < end and pattern.match(line))
    ]
    return "\n".join(kept).rstrip() + "\n"


def remove_table_key(text: str, table: str, key: str) -> str:
    lines = text.splitlines()
    header = toml_table_header(table)
    start = next((index for index, line in enumerate(lines) if header.match(line.strip())), None)
    if start is None:
        return text
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[")),
        len(lines),
    )
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    kept = [
        line
        for index, line in enumerate(lines)
        if not (start < index < end and pattern.match(line))
    ]
    return "\n".join(kept).rstrip() + "\n"


def removed_feature_flags(parsed: dict[str, Any]) -> list[str]:
    features = parsed.get("features")
    if not isinstance(features, dict):
        return []
    return [name for name in REMOVED_FEATURE_FLAGS if name in features]


def expected_agent_text(model: str, provider: str, effort: str, supports_vision: bool) -> str:
    vision_instruction = (
        "When image inputs are included in your task context, inspect them directly and use the visual evidence in your implementation; do not ask the parent agent to pre-analyze them."
        if supports_vision
        else "You are configured for text-only input. Do not claim to inspect images; use visual observations supplied by the parent agent."
    )
    return f'''name = {toml_string(ROLE)}
description = "Writable implementation subagent restricted to a parent-managed isolated Git worktree."
model = {toml_string(model)}
model_provider = {toml_string(provider)}
model_reasoning_effort = {toml_string(effort)}
sandbox_mode = {toml_string(AGENT_SANDBOX_MODE)}
developer_instructions = """
You are the writable implementation subagent running inside Codex.

Work only on the single bounded plan item assigned by the parent agent and edit code directly in the exact isolated Git worktree path it provides. Before editing, verify that your working directory is that worktree and inspect git status. Never edit the parent's active checkout or any path outside the assigned worktree.
{vision_instruction}
Implement the task in the worktree, run the relevant tests, and return changed file paths, test results, and explicit assumptions. Do not return a replacement patch unless the parent explicitly asks for one.
The parent owns checkpoints, integration, rollback, and cleanup. Do not run git reset, git clean, git checkout, git restore, git worktree remove, branch deletion, merge, rebase, cherry-pick, or revert. Do not commit unless the parent explicitly asks you to do so.
When the parent reports an acceptance failure, use its exact file locations, commands, evidence, expected behavior, and direction to revise the files in the same assigned worktree.
Do not spawn or delegate to any additional subagent.
"""
'''


def validate_model_id(model: str) -> str:
    if not MODEL_ID_PATTERN.fullmatch(model):
        raise ManagerError("invalid_model", "模型 ID 包含不支持的字符或长度超过 128。")
    return model


def validate_reasoning_effort(effort: str) -> str:
    if effort not in REASONING_EFFORTS:
        raise ManagerError("invalid_reasoning_effort", "思考强度必须是 low、medium 或 high。")
    return effort


def validate_vision(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in VISION_VALUES:
        raise ManagerError("invalid_vision", "识图能力必须是 yes 或 no。")
    return normalized == "yes"


def managed_role_block(paths: Paths) -> str:
    return f'''
{ROLE_BEGIN}
[agents.{ROLE}]
description = "Writable implementation subagent restricted to a parent-managed isolated Git worktree."
config_file = {toml_string(str(paths.agent))}
{ROLE_END}
'''


def compatible_existing(parsed: dict[str, Any], paths: Paths) -> tuple[bool, list[str]]:
    issues: list[str] = []
    agent = (parsed.get("agents") or {}).get(ROLE)
    if agent:
        if set(agent) - {"description", "config_file"}:
            issues.append(f"agents.{ROLE}")
        if Path(agent.get("config_file", "")).expanduser() != paths.agent:
            issues.append(f"agents.{ROLE}.config_file")
    return not issues, issues


def model_for_endpoint(
    base: dict[str, Any],
    parent_model: str,
    selected_model: str,
    effort: str,
    supports_vision: bool,
) -> dict[str, dict[str, Any]]:
    parent_entry = next(
        (item for item in base.get("models", []) if item.get("slug") == parent_model),
        None,
    )
    if not isinstance(parent_entry, dict):
        raise ManagerError("parent_model_missing", f"模型目录中没有父模型 {parent_model}。")
    template = copy.deepcopy(parent_entry)
    template.update(
        {
            "slug": selected_model,
            "display_name": selected_model,
            "description": (
                "Custom agentic model with text and image input inherited from the parent provider."
                if supports_vision
                else "Text-only custom agentic model inherited from the parent provider."
            ),
            "input_modalities": ["text", "image"] if supports_vision else ["text"],
            "supports_image_detail_original": supports_vision,
            "default_reasoning_level": effort,
            "multi_agent_version": PARENT_MULTI_AGENT_VERSION,
        }
    )
    return {selected_model: template}


def configured_custom_model(paths: Paths) -> str | None:
    manifest = read_manifest(paths)
    selected = manifest.get("selected_model")
    if isinstance(selected, str):
        try:
            return validate_model_id(selected)
        except ManagerError:
            return None
    return None


def configured_reasoning_effort(paths: Paths) -> str:
    value = read_manifest(paths).get("reasoning_effort", DEFAULT_EFFORT)
    if not isinstance(value, str):
        return DEFAULT_EFFORT
    try:
        return validate_reasoning_effort(value)
    except ManagerError:
        return DEFAULT_EFFORT


def configured_supports_vision(paths: Paths) -> bool:
    value = read_manifest(paths).get("supports_vision", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return validate_vision(value)
        except ManagerError:
            pass
    return False


def resolve_selected_model(paths: Paths, requested: str | None) -> str | None:
    if requested is not None:
        return validate_model_id(requested)
    return configured_custom_model(paths)


def resolve_reasoning_effort(paths: Paths, requested: str | None) -> str:
    return validate_reasoning_effort(requested) if requested is not None else configured_reasoning_effort(paths)


def resolve_supports_vision(paths: Paths, requested: str | None) -> bool:
    return validate_vision(requested) if requested is not None else configured_supports_vision(paths)


def run_codex_models(codex_bin: str, paths: Paths) -> dict[str, Any]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(paths.home)
    proc = subprocess.run(
        [codex_bin, "debug", "models"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=45,
    )
    if proc.returncode != 0:
        raise ManagerError("codex_catalog_failed", "Codex 无法读取当前模型目录。", {"stderr": proc.stderr[-800:]})
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ManagerError("codex_catalog_invalid", "Codex 返回的模型目录不是有效 JSON。") from exc


def load_base_catalog(codex_bin: str, paths: Paths, config: dict[str, Any]) -> dict[str, Any]:
    configured_path = config.get("model_catalog_json")
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data.get("models"), list):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
    return run_codex_models(codex_bin, paths)


def merged_catalog(
    base: dict[str, Any],
    custom_models: dict[str, dict[str, Any]],
    parent_model: str,
    replace_slugs: set[str],
) -> dict[str, Any]:
    models = [
        model for model in base.get("models", [])
        if model.get("slug") not in replace_slugs
    ]
    models.extend(custom_models.values())
    parent_found = False
    for model in models:
        if model.get("slug") == parent_model:
            model["multi_agent_version"] = PARENT_MULTI_AGENT_VERSION
            parent_found = True
            break
    if not parent_found:
        raise ManagerError("parent_model_missing", f"模型目录中没有父模型 {parent_model}。")
    models.sort(key=lambda item: item.get("slug", ""))
    return {"models": models}


def configured_parent_model(config: dict[str, Any]) -> str | None:
    model = config.get("model")
    if isinstance(model, str) and model:
        return model
    return None


def configured_parent_provider(config: dict[str, Any]) -> str | None:
    provider = config.get("model_provider")
    return provider if isinstance(provider, str) and provider else None


def make_backup(paths: Paths) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = paths.state_dir / "backups" / stamp
    backup.mkdir(parents=True, exist_ok=False)
    for source in (paths.config, paths.catalog, paths.agent, paths.manifest):
        if source.is_file():
            shutil.copy2(source, backup / source.name)
    return backup


def write_manifest(paths: Paths, payload: dict[str, Any]) -> None:
    atomic_write(paths.manifest, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def read_manifest(paths: Paths) -> dict[str, Any]:
    if not paths.manifest.is_file():
        return {}
    try:
        payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def install(
    paths: Paths,
    codex_bin: str,
    selected_model: str,
    effort: str,
    supports_vision: bool,
    replace_agent: bool = False,
) -> dict[str, Any]:
    paths.home.mkdir(parents=True, exist_ok=True)
    config_text = paths.config.read_text(encoding="utf-8") if paths.config.is_file() else ""
    parsed = parse_toml_text(config_text) if config_text.strip() else {}
    removed_flags = removed_feature_flags(parsed)
    previous_manifest = read_manifest(paths)
    provider_marker_present = PROVIDER_BEGIN in config_text and PROVIDER_END in config_text
    role_marker_present = ROLE_BEGIN in config_text and ROLE_END in config_text
    unmanaged_config = remove_managed_blocks(config_text)
    unmanaged_parsed = parse_toml_text(unmanaged_config) if unmanaged_config.strip() else {}
    compatible, conflicts = compatible_existing(unmanaged_parsed, paths)
    if not compatible:
        raise ManagerError("conflict", "发现不兼容的现有自定义子 Agent 配置。", {"fields": conflicts})
    parent_provider = configured_parent_provider(unmanaged_parsed)
    if not parent_provider:
        raise ManagerError("parent_provider_unconfigured", "桌面配置中没有明确的父 model_provider。")
    target_agent_text = expected_agent_text(selected_model, parent_provider, effort, supports_vision)
    if paths.agent.is_file() and paths.agent.read_text(encoding="utf-8") != target_agent_text:
        managed_agent_unchanged = bool(previous_manifest.get("managed_agent_file")) and (
            sha256_text_file(paths.agent) == previous_manifest.get("agent_sha256")
        )
        if not managed_agent_unchanged and not replace_agent:
            raise ManagerError(
                "conflict",
                "现有 CustomAgent 文件与目标配置不同。",
                {
                    "path": str(paths.agent),
                    "resolution": "确认完整覆盖范围后，以 --confirmed --replace-agent 重试。",
                },
            )
    registered_role_present = bool((unmanaged_parsed.get("agents") or {}).get(ROLE))

    catalog_preexisted_now = paths.catalog.is_file()
    agent_preexisted_now = paths.agent.is_file()
    selected_before = parsed.get("model_catalog_json") == str(paths.catalog)
    previous_schema = previous_manifest.get("schema_version", 1) if previous_manifest else 4
    previous_selection_managed = bool(previous_manifest.get("managed_catalog_selection"))
    if previous_manifest and previous_schema < 2 and selected_before:
        previous_selection_managed = True
    managed_catalog_selection = previous_selection_managed or not selected_before
    previous_catalog_value = (
        previous_manifest.get("previous_model_catalog_json")
        if previous_selection_managed and selected_before
        else parsed.get("model_catalog_json")
    )
    if previous_manifest and previous_schema < 2:
        catalog_preexisted = bool(previous_manifest.get("catalog_preexisted", False))
        agent_preexisted = bool(
            previous_manifest.get(
                "agent_preexisted",
                not previous_manifest.get("managed_agent_file", False),
            )
        )
    else:
        catalog_preexisted = bool(previous_manifest.get("catalog_preexisted", catalog_preexisted_now))
        agent_preexisted = bool(previous_manifest.get("agent_preexisted", agent_preexisted_now))
    current_multi_agent_v2 = (parsed.get("features") or {}).get("multi_agent_v2")
    previous_multi_agent_v2 = (
        previous_manifest.get("previous_multi_agent_v2")
        if previous_manifest.get("managed_multi_agent_v2")
        else current_multi_agent_v2
    )
    managed_multi_agent_v2 = bool(
        previous_manifest.get("managed_multi_agent_v2")
        or current_multi_agent_v2 is not DESKTOP_MULTI_AGENT_V2
    )

    backup = make_backup(paths)
    try:
        base = load_base_catalog(codex_bin, paths, parsed)
        parent_model = configured_parent_model(parsed)
        if not parent_model:
            raise ManagerError("parent_model_unconfigured", "桌面配置中没有明确的父模型。")
        custom_models = model_for_endpoint(base, parent_model, selected_model, effort, supports_vision)
        previous_parent = previous_manifest.get("parent_model")
        if previous_parent and previous_parent != parent_model:
            previous_entry = next(
                (item for item in base.get("models", []) if item.get("slug") == previous_parent),
                None,
            )
            if previous_entry is not None:
                original_version = previous_manifest.get("parent_original_multi_agent_version")
                if original_version is None:
                    previous_entry.pop("multi_agent_version", None)
                else:
                    previous_entry["multi_agent_version"] = original_version
        current_parent_entry = next(
            (item for item in base.get("models", []) if item.get("slug") == parent_model),
            None,
        )
        if not current_parent_entry:
            raise ManagerError("parent_model_missing", f"模型目录中没有父模型 {parent_model}。")
        parent_original_version = (
            previous_manifest.get("parent_original_multi_agent_version")
            if previous_parent == parent_model
            else current_parent_entry.get("multi_agent_version")
        )
        previous_managed_models = previous_manifest.get("managed_models") or previous_manifest.get("supported_models") or []
        replace_slugs = set(previous_managed_models) | set(custom_models)
        catalog = merged_catalog(base, custom_models, parent_model, replace_slugs)
        catalog_bytes = (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode()

        new_config = unmanaged_config
        for flag in removed_flags:
            new_config = remove_table_key(new_config, "features", flag)
        if not registered_role_present:
            new_config = new_config.rstrip() + "\n" + managed_role_block(paths)
        new_config = set_top_level_key(new_config, "model_catalog_json", str(paths.catalog))
        new_config = set_table_bool(
            new_config,
            "features",
            "multi_agent_v2",
            DESKTOP_MULTI_AGENT_V2,
        )
        parse_toml_text(new_config)
        json.loads(catalog_bytes)

        atomic_write(paths.catalog, catalog_bytes)
        atomic_write(paths.agent, target_agent_text.encode(), mode=0o644)
        atomic_write(paths.config, new_config.encode())

        previous_agent_managed = bool(previous_manifest.get("managed_agent_file"))
        managed_agent_file = previous_agent_managed or not agent_preexisted_now
        catalog_original_backup = previous_manifest.get("catalog_original_backup")
        if not catalog_original_backup and catalog_preexisted_now:
            candidate = backup / paths.catalog.name
            if candidate.is_file():
                catalog_original_backup = str(candidate)
        adopted_existing = registered_role_present or agent_preexisted or catalog_preexisted
        manifest = {
            "schema_version": 9,
            "installed_at": datetime.now().isoformat(timespec="seconds"),
            "backup": str(backup),
            "previous_model_catalog_json": previous_catalog_value,
            "managed_catalog_selection": managed_catalog_selection,
            "managed_provider_block": False,
            "legacy_provider_block_removed": provider_marker_present,
            "managed_agent_file": managed_agent_file,
            "catalog_preexisted": catalog_preexisted,
            "catalog_original_backup": catalog_original_backup,
            "agent_preexisted": agent_preexisted,
            "managed_role_block": role_marker_present or not registered_role_present,
            "adopted_existing": adopted_existing,
            "parent_model": parent_model,
            "parent_provider": parent_provider,
            "parent_multi_agent_version": PARENT_MULTI_AGENT_VERSION,
            "parent_original_multi_agent_version": parent_original_version,
            "managed_multi_agent_v2": managed_multi_agent_v2,
            "previous_multi_agent_v2": previous_multi_agent_v2,
            "desktop_multi_agent_v2": DESKTOP_MULTI_AGENT_V2,
            "removed_feature_flags": removed_flags,
            "selected_model": selected_model,
            "reasoning_effort": effort,
            "supports_vision": supports_vision,
            "sandbox_mode": AGENT_SANDBOX_MODE,
            "execution_mode": AGENT_EXECUTION_MODE,
            "managed_models": list(custom_models),
            "config_sha256": sha256_bytes(new_config.encode()),
            "catalog_sha256": sha256_bytes(catalog_bytes),
            "agent_sha256": sha256_bytes(target_agent_text.encode()),
        }
        write_manifest(paths, manifest)
        route = native_route_details(paths, parse_toml_text(new_config))
        return {
            "skill_name": SKILL_NAME,
            "backup": str(backup),
            "adopted_existing": adopted_existing,
            "selected_model": selected_model,
            "reasoning_effort": effort,
            "supports_vision": supports_vision,
            "sandbox_mode": AGENT_SANDBOX_MODE,
            "execution_mode": AGENT_EXECUTION_MODE,
            "parent_credentials_untouched": True,
            "replaced_conflicting_agent": replace_agent,
            "legacy_provider_block_removed": provider_marker_present,
            "removed_feature_flags": removed_flags,
            **route,
        }
    except Exception:
        restore_backup(paths, backup)
        raise


def static_status(paths: Paths, codex_bin: str | None = None) -> dict[str, Any]:
    selected_model = configured_custom_model(paths)
    effort = configured_reasoning_effort(paths)
    supports_vision = configured_supports_vision(paths)
    manifest = read_manifest(paths)
    managed_models = manifest.get("managed_models") or []
    checks: dict[str, Any] = {
        "config_exists": paths.config.is_file(),
        "catalog_exists": paths.catalog.is_file(),
        "agent_exists": paths.agent.is_file(),
        "manifest_exists": paths.manifest.is_file(),
        "selected_model": selected_model,
        "reasoning_effort": effort,
        "supports_vision": supports_vision,
        "sandbox_mode": manifest.get("sandbox_mode", AGENT_SANDBOX_MODE),
        "execution_mode": manifest.get("execution_mode", AGENT_EXECUTION_MODE),
        "model_selected": bool(selected_model),
        "parent_credentials_untouched": True,
    }
    errors: list[str] = []
    parsed: dict[str, Any] = {}
    if paths.config.is_file():
        try:
            parsed = parse_toml_text(paths.config.read_text(encoding="utf-8"))
            checks["config_valid"] = True
        except ManagerError as exc:
            checks["config_valid"] = False
            errors.append(str(exc))
    legacy_flags = removed_feature_flags(parsed)
    checks["unrecognized_feature_flags"] = legacy_flags
    checks["unrecognized_feature_flags_absent"] = not legacy_flags
    role = (parsed.get("agents") or {}).get(ROLE)
    checks["agent_discovery"] = "user_config_registration"
    checks["role_registration_present"] = bool(role)
    checks["role_registration_valid"] = (
        bool(role)
        and not compatible_existing(parsed, paths)[1]
    )
    checks["catalog_selected"] = Path(parsed.get("model_catalog_json", "")).expanduser() == paths.catalog
    checks["desktop_multi_agent_v2"] = (parsed.get("features") or {}).get("multi_agent_v2")
    checks["desktop_multi_agent_v2_disabled"] = (
        checks["desktop_multi_agent_v2"] is DESKTOP_MULTI_AGENT_V2
    )
    parent_model = configured_parent_model(parsed)
    parent_provider = configured_parent_provider(parsed)
    checks["parent_model"] = parent_model
    checks["parent_model_configured"] = bool(parent_model)
    checks["parent_provider"] = parent_provider
    checks["parent_provider_configured"] = bool(parent_provider)
    route = native_route_details(paths, parsed)
    checks["native_route_mode"] = route["route_mode"]
    checks["native_expected_provider"] = route["expected_provider"]
    checks["native_credential_source"] = route["credential_source"]
    checks["native_uses_dedicated_credential"] = route["uses_dedicated_credential"]
    checks["parent_provider"] = route["parent_provider"]
    if paths.catalog.is_file():
        try:
            data = json.loads(paths.catalog.read_text(encoding="utf-8"))
            registered = {item.get("slug") for item in data.get("models", [])}
            checks["supported_models_registered"] = all(
                model in registered for model in managed_models
            )
            checks["model_registered"] = selected_model in registered
            custom_entry = next(
                (item for item in data.get("models", []) if selected_model and item.get("slug") == selected_model),
                None,
            )
            expected_modalities = ["text", "image"] if supports_vision else ["text"]
            checks["model_modalities_valid"] = bool(custom_entry) and (
                custom_entry.get("input_modalities") == expected_modalities
            )
            checks["model_reasoning_default_valid"] = bool(custom_entry) and (
                custom_entry.get("default_reasoning_level") == effort
            )
            parent_entry = next(
                (item for item in data.get("models", []) if parent_model and item.get("slug") == parent_model),
                None,
            )
            checks["parent_multi_agent_version"] = (
                parent_entry.get("multi_agent_version") if parent_entry else None
            )
            checks["parent_uses_plaintext_v1"] = (
                checks["parent_multi_agent_version"] == PARENT_MULTI_AGENT_VERSION
            )
        except (OSError, json.JSONDecodeError):
            checks["model_registered"] = False
            checks["model_modalities_valid"] = False
            checks["model_reasoning_default_valid"] = False
            checks["parent_uses_plaintext_v1"] = False
            errors.append("模型目录无法解析。")
    else:
        checks["supported_models_registered"] = False
        checks["model_registered"] = False
        checks["model_modalities_valid"] = False
        checks["model_reasoning_default_valid"] = False
        checks["parent_uses_plaintext_v1"] = False
    checks["agent_content_valid"] = bool(selected_model and parent_provider) and paths.agent.is_file() and (
        paths.agent.read_text(encoding="utf-8") == expected_agent_text(
            selected_model,
            parent_provider,
            effort,
            supports_vision,
        )
    )

    version: tuple[int, int, int] | None = None
    if codex_bin:
        try:
            version_text = codex_version_text(codex_bin)
            checks["desktop_codex_path"] = codex_bin
            checks["desktop_codex_version"] = version_text
            checks["desktop_codex_detected"] = True
        except ManagerError as exc:
            checks["desktop_codex_detected"] = False
            errors.append(str(exc))
    required = (
        "config_valid",
        "catalog_selected",
        "model_selected",
        "supported_models_registered",
        "model_registered",
        "model_modalities_valid",
        "model_reasoning_default_valid",
        "parent_model_configured",
        "parent_provider_configured",
        "parent_uses_plaintext_v1",
        "desktop_multi_agent_v2_disabled",
        "agent_content_valid",
        "manifest_exists",
        "role_registration_valid",
        "desktop_codex_detected",
        "unrecognized_feature_flags_absent",
    )
    ready = all(checks.get(key) is True for key in required)
    return result(
        "configured" if ready else "partial",
        skill_name=SKILL_NAME,
        selected_model=selected_model,
        reasoning_effort=effort,
        supports_vision=supports_vision,
        sandbox_mode=AGENT_SANDBOX_MODE,
        execution_mode=AGENT_EXECUTION_MODE,
        parent_credentials_untouched=True,
        **route,
        checks=checks,
        errors=errors,
    )


def direct_test(paths: Paths, codex_bin: str, selected_model: str, effort: str) -> dict[str, Any]:
    parsed = parse_toml_text(paths.config.read_text(encoding="utf-8"))
    parent_provider = configured_parent_provider(parsed)
    if not parent_provider:
        raise ManagerError("parent_provider_unconfigured", "桌面配置中没有明确的父 model_provider。")
    env = dict(os.environ)
    env["CODEX_HOME"] = str(paths.home)
    prompt = "Reply exactly CUSTOM_AGENT_DIRECT_OK and nothing else."
    proc = subprocess.run(
        [
            codex_bin,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "-s",
            "read-only",
            "-C",
            str(paths.home),
            "-m",
            selected_model,
            "-c",
            f"model_provider={toml_string(parent_provider)}",
            "-c",
            f"model_reasoning_effort={toml_string(effort)}",
            prompt,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
    )
    if proc.returncode != 0 or "CUSTOM_AGENT_DIRECT_OK" not in proc.stdout:
        raise ManagerError(
            "direct_test_failed",
            "自定义子 Agent 直连测试失败。",
            {"stderr": proc.stderr[-1000:]},
        )
    return {
        "direct": True,
        "selected_model": selected_model,
        "model_provider": parent_provider,
        "reasoning_effort": effort,
        "credential_source": "parent_provider",
    }


def choose_parent_model(paths: Paths) -> str:
    parsed = parse_toml_text(paths.config.read_text(encoding="utf-8"))
    parent_model = configured_parent_model(parsed)
    if not parent_model:
        raise ManagerError("parent_model_unconfigured", "桌面配置中没有明确的父模型。")
    return parent_model


def query_child_metadata(
    paths: Paths,
    child_id: str,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    candidates: list[tuple[float, Path]] = []
    for state_db in paths.home.glob("state_*.sqlite"):
        try:
            candidates.append((state_db.stat().st_mtime, state_db))
        except OSError:
            continue
    for _, state_db in sorted(candidates, reverse=True)[:MAX_STATE_DATABASES]:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        try:
            with sqlite3.connect(
                f"{state_db.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=0.05,
            ) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()
                }
                required = {"id", "model_provider", "model", "reasoning_effort", "agent_role"}
                if not required.issubset(columns):
                    continue
                row = connection.execute(
                    "SELECT model_provider, model, reasoning_effort, agent_role FROM threads WHERE id = ?",
                    (child_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            continue
        if row:
            return {
                "model_provider": row[0],
                "model": row[1],
                "reasoning_effort": row[2],
                "agent_role": row[3],
            }
    return None


def wait_for_child_metadata(
    paths: Paths,
    child_id: str,
    timeout_seconds: float = METADATA_WAIT_SECONDS,
    poll_interval: float = 0.2,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        metadata = query_child_metadata(paths, child_id, deadline)
        if metadata is not None:
            return metadata
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval, remaining))


def native_route_details(paths: Paths, parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    if parsed is None:
        parsed = parse_toml_text(paths.config.read_text(encoding="utf-8"))
    parent_provider = configured_parent_provider(parsed)
    return {
        "route_mode": "inherited_parent_provider",
        "expected_provider": parent_provider,
        "parent_provider": parent_provider,
        "credential_source": "parent_provider",
        "uses_dedicated_credential": False,
    }


def parse_native_events(stdout: str) -> tuple[list[str], dict[str, dict[str, Any]]]:
    child_ids: list[str] = []
    child_states: dict[str, dict[str, Any]] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if (
            event.get("type") == "item.completed"
            and item.get("type") == "collab_tool_call"
            and item.get("tool") == "spawn_agent"
        ):
            child_ids.extend(item.get("receiver_thread_ids") or [])
        if (
            event.get("type") == "item.completed"
            and item.get("type") == "collab_tool_call"
            and item.get("tool") == "wait"
        ):
            for receiver_id, state in (item.get("agents_states") or {}).items():
                if isinstance(state, dict):
                    child_states[receiver_id] = {
                        "status": state.get("status"),
                        "message": state.get("message"),
                    }
    return child_ids, child_states


def native_test(paths: Paths, codex_bin: str, selected_model: str, effort: str) -> dict[str, Any]:
    parent_model = choose_parent_model(paths)
    route = native_route_details(paths)
    env = dict(os.environ)
    env["CODEX_HOME"] = str(paths.home)
    with tempfile.TemporaryDirectory(prefix="codex-custom-agent-write-test-") as directory:
        repository = Path(directory) / "repo"
        repository.mkdir()
        subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.name", "Codex Test"], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.email", "codex-test@localhost"], check=True)
        (repository / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "baseline"], check=True)
        prompt = (
            'Use the native spawn_agent tool exactly once. Set agent_type to CustomAgent and fork_turns to none. '
            'Give it this task: In the current isolated Git worktree, create a file named '
            'native-custom-agent-write.txt containing exactly NATIVE_CUSTOM_AGENT_WRITE_OK followed by a newline. '
            'Do not modify any other file and do not commit. When finished, reply exactly NATIVE_CUSTOM_AGENT_OK. '
            "Then wait for that subagent and return only its final response."
        )
        proc = subprocess.run(
            [
                codex_bin,
                "exec",
                "--json",
                "-s",
                AGENT_SANDBOX_MODE,
                "-C",
                str(repository),
                "-m",
                parent_model,
                prompt,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=300,
        )
        marker = repository / "native-custom-agent-write.txt"
        status_after_write = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        content_verified = False
        try:
            content_verified = marker.read_bytes() in {
                b"NATIVE_CUSTOM_AGENT_WRITE_OK\n",
                b"NATIVE_CUSTOM_AGENT_WRITE_OK\r\n",
            }
        except PermissionError:
            pass
        try:
            marker_size = marker.stat().st_size
        except (FileNotFoundError, PermissionError, OSError):
            marker_size = -1
        write_verified = (
            marker.is_file()
            and marker_size in {29, 30}
            and status_after_write.returncode == 0
            and status_after_write.stdout.strip() == "?? native-custom-agent-write.txt"
        )
    if proc.returncode != 0:
        raise ManagerError(
            "native_test_failed",
            "新 Codex 任务中的原生 spawn_agent 测试失败。",
            {**route, "stderr": proc.stderr[-1200:]},
        )
    child_ids, child_states = parse_native_events(proc.stdout)
    child_id = child_ids[0] if len(child_ids) == 1 else None
    child_state = child_states.get(child_id) if child_id else None
    raw_message = child_state.get("message") if child_state else None
    child_message = raw_message.strip() if isinstance(raw_message, str) else None
    metadata = wait_for_child_metadata(paths, child_id) if child_id else None
    expected = {
        "model_provider": route["expected_provider"],
        "model": selected_model,
        "reasoning_effort": effort,
        "agent_role": ROLE,
    }
    if child_state and child_state.get("status") not in {None, "completed"}:
        raise ManagerError(
            "native_child_failed",
            "原生 CustomAgent 子线程启动或执行失败。",
            {
                **route,
                "child_id": child_id,
                "child_state": child_state,
                "metadata": metadata,
                "expected": expected,
            },
        )
    if len(child_ids) != 1 or child_message != "NATIVE_CUSTOM_AGENT_OK" or metadata != expected or not write_verified:
        raise ManagerError(
            "native_route_mismatch",
            "原生子 Agent 路由验收证据不完整或不符合自定义配置。",
            {
                **route,
                "child_ids": child_ids,
                "child_state": child_state,
                "child_message": child_message,
                "metadata": metadata,
                "expected": expected,
                "write_verified": write_verified,
                "content_verified": content_verified,
            },
        )
    return {
        "desktop_fresh_session_native": True,
        "child_id": child_id,
        "configured_provider": route["expected_provider"],
        "writable_child_verified": write_verified,
        **route,
        **expected,
    }


def run_tests(paths: Paths, codex_bin: str) -> dict[str, Any]:
    selected_model = configured_custom_model(paths)
    effort = configured_reasoning_effort(paths)
    supports_vision = configured_supports_vision(paths)
    if not selected_model:
        raise ManagerError(
            "model_selection_required",
            "尚未配置子 Agent 模型 ID。",
        )
    status = static_status(paths, codex_bin)
    if status["status"] != "configured":
        raise ManagerError("not_configured", "静态配置尚未完整，不能运行实时测试。", status)
    direct = direct_test(paths, codex_bin, selected_model, effort)
    native = native_test(paths, codex_bin, selected_model, effort)
    evidence = {**direct, **native}
    return result(
        "ready",
        **evidence,
        supports_vision=supports_vision,
        parent_credentials_untouched=True,
        new_task_required=True,
        restart_required=True,
    )


def restore_backup(paths: Paths, backup: Path) -> None:
    for target in (paths.config, paths.catalog, paths.agent, paths.manifest):
        source = backup / target.name
        if source.is_file():
            atomic_write(target, source.read_bytes(), mode=0o644 if target == paths.agent else 0o600)
        elif target.is_file():
            target.unlink()


def setup(
    paths: Paths,
    codex_bin: str,
    skip_live_test: bool,
    requested_model: str | None,
    requested_effort: str | None,
    requested_vision: str | None,
    model_env: bool = False,
    effort_env: bool = False,
    vision_env: bool = False,
    replace_agent: bool = False,
) -> dict[str, Any]:
    if model_env:
        requested_model = os.environ.get("CUSTOM_AGENT_MODEL", "").strip()
        if not requested_model:
            raise ManagerError("configuration_missing", "设置页面没有注入模型 ID。")
    if effort_env:
        requested_effort = os.environ.get("CUSTOM_AGENT_REASONING_EFFORT", "").strip()
        if not requested_effort:
            raise ManagerError("configuration_missing", "设置页面没有注入思考强度。")
    if vision_env:
        requested_vision = os.environ.get("CUSTOM_AGENT_VISION", "").strip()
        if not requested_vision:
            raise ManagerError("configuration_missing", "设置页面没有注入识图能力。")
    selected_model = resolve_selected_model(paths, requested_model)
    if not selected_model:
        return result(
            "model_selection_required",
            message="请填写要配置的精确模型 ID。",
        )
    effort = resolve_reasoning_effort(paths, requested_effort)
    supports_vision = resolve_supports_vision(paths, requested_vision)

    install_result: dict[str, Any] | None = None
    try:
        install_result = install(
            paths,
            codex_bin,
            selected_model,
            effort,
            supports_vision,
            replace_agent=replace_agent,
        )
        if skip_live_test:
            return result(
                "configured",
                **install_result,
                new_task_required=True,
                restart_required=True,
            )
        tested = run_tests(paths, codex_bin)
        return {**tested, **install_result}
    except Exception as original_error:
        rollback_failures: list[str] = []
        if install_result and install_result.get("backup"):
            try:
                restore_backup(paths, Path(install_result["backup"]))
            except Exception as exc:
                rollback_failures.append(f"files:{type(exc).__name__}")
        if rollback_failures:
            raise ManagerError(
                "rollback_failed",
                "配置失败，且事务回滚未能完整完成。",
                {"steps": rollback_failures},
            ) from original_error
        raise


def disable(paths: Paths) -> dict[str, Any]:
    if not paths.manifest.is_file():
        raise ManagerError("not_managed", "没有找到本 Skill 的管理记录，拒绝修改现有配置。")
    manifest = read_manifest(paths)
    if manifest.get("managed_agent_file") and paths.agent.is_file():
        if sha256_text_file(paths.agent) != manifest.get("agent_sha256"):
            raise ManagerError(
                "conflict",
                "CustomAgent 文件已被修改，拒绝停用。",
                {"path": str(paths.agent)},
            )
    changed = False
    if paths.config.is_file():
        text = paths.config.read_text(encoding="utf-8")
        updated = remove_marked_block(text, ROLE_BEGIN, ROLE_END)
        if manifest.get("managed_multi_agent_v2"):
            previous = manifest.get("previous_multi_agent_v2")
            if isinstance(previous, bool):
                updated = set_table_bool(updated, "features", "multi_agent_v2", previous)
            else:
                updated = remove_table_bool_if_value(
                    updated,
                    "features",
                    "multi_agent_v2",
                    DESKTOP_MULTI_AGENT_V2,
                )
        if updated != text:
            parse_toml_text(updated)
            atomic_write(paths.config, updated.encode())
            changed = True
    if manifest.get("managed_agent_file") and paths.agent.is_file():
        paths.agent.unlink()
        changed = True
    return result(
        "disabled",
        changed=changed,
        agent_preserved=not bool(manifest.get("managed_agent_file")),
        parent_credentials_untouched=True,
    )


def uninstall(paths: Paths) -> dict[str, Any]:
    manifest = read_manifest(paths)
    if not manifest:
        raise ManagerError("not_managed", "没有找到本 Skill 的管理记录，拒绝修改现有配置。")
    if paths.catalog.is_file() and sha256_bytes(paths.catalog.read_bytes()) != manifest.get("catalog_sha256"):
        raise ManagerError(
            "conflict",
            "模型目录已被修改，拒绝卸载。",
            {"path": str(paths.catalog)},
        )
    backup = make_backup(paths)
    try:
        disabled = disable(paths)
        if paths.config.is_file():
            text = paths.config.read_text(encoding="utf-8")
            if manifest.get("managed_provider_block"):
                text = remove_marked_block(text, PROVIDER_BEGIN, PROVIDER_END)
            if manifest.get("managed_catalog_selection"):
                previous_catalog = manifest.get("previous_model_catalog_json")
                if previous_catalog is None:
                    text = remove_top_level_key_if_value(text, "model_catalog_json", str(paths.catalog))
                elif top_level_key(text, "model_catalog_json") == str(paths.catalog):
                    text = set_top_level_key(text, "model_catalog_json", previous_catalog)
            parse_toml_text(text)
            atomic_write(paths.config, text.encode())
        catalog_removed = False
        catalog_restored = False
        if paths.catalog.is_file():
            original_backup = manifest.get("catalog_original_backup")
            if manifest.get("catalog_preexisted") and original_backup and Path(original_backup).is_file():
                atomic_write(paths.catalog, Path(original_backup).read_bytes())
                catalog_restored = True
            elif not manifest.get("catalog_preexisted"):
                paths.catalog.unlink()
                catalog_removed = True
        paths.manifest.unlink(missing_ok=True)
    except Exception:
        restore_backup(paths, backup)
        raise
    return result(
        "uninstalled",
        disabled=disabled,
        catalog_removed=catalog_removed,
        catalog_restored=catalog_restored,
        parent_credentials_untouched=True,
        legacy_credential_preserved=True,
    )


def try_acquire_file_lock(lock_file) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    if msvcrt is not None:
        lock_file.seek(0)
        if lock_file.read(1) == "":
            lock_file.seek(0)
            lock_file.write("\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    raise ManagerError("unsupported_platform", "当前平台没有可用的文件锁实现。")


def release_file_lock(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def operation_lock(paths: Paths, timeout_seconds: float = LOCK_WAIT_SECONDS):
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    with (paths.state_dir / "manager.lock").open("a+") as lock_file:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if try_acquire_file_lock(lock_file):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagerError(
                    "operation_in_progress",
                    "另一个自定义子 Agent 配置操作仍在进行，请稍后重试。",
                )
            time.sleep(min(0.1, remaining))
        try:
            yield
        finally:
            release_file_lock(lock_file)


def main() -> int:
    if sys.argv[1:] == ["_credential-get"]:
        try:
            secret = read_credential_key()
            if not secret:
                print("旧版子代理凭据不存在。", file=sys.stderr)
                return 2
            sys.stdout.write(secret)
            return 0
        except ManagerError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "setup", "test", "repair", "disable", "uninstall"))
    parser.add_argument("--codex-home")
    model_input = parser.add_mutually_exclusive_group()
    model_input.add_argument("--model")
    model_input.add_argument("--model-env", action="store_true", help="仅从可信包装器注入的 CUSTOM_AGENT_MODEL 读取模型 ID")
    effort_input = parser.add_mutually_exclusive_group()
    effort_input.add_argument("--effort", choices=sorted(REASONING_EFFORTS))
    effort_input.add_argument("--effort-env", action="store_true", help="仅从可信包装器注入的 CUSTOM_AGENT_REASONING_EFFORT 读取思考强度")
    vision_input = parser.add_mutually_exclusive_group()
    vision_input.add_argument("--vision", choices=sorted(VISION_VALUES))
    vision_input.add_argument("--vision-env", action="store_true", help="仅从可信包装器注入的 CUSTOM_AGENT_VISION 读取识图能力")
    parser.add_argument("--skip-live-test", action="store_true")
    parser.add_argument(
        "--replace-agent",
        action="store_true",
        help="仅在二次确认已明确覆盖现有 CustomAgent.toml 时允许替换冲突角色文件",
    )
    parser.add_argument(
        "--confirmed",
        action="store_true",
        help="仅在已展示持久变更影响并于后续独立消息收到精确回复“已确认”后使用",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    paths = resolve_paths(args.codex_home)
    try:
        if args.replace_agent and args.command not in {"setup", "repair"}:
            raise ManagerError("invalid_option", "--replace-agent 只能与 setup 或 repair 一起使用。")
        if args.command in {"setup", "repair", "disable", "uninstall"} and not args.confirmed:
            raise ManagerError(
                "confirmation_required",
                "拒绝执行持久化子智能体配置变更。必须先展示当前配置、目标配置、影响和文件范围，"
                "并在后续独立用户消息中收到精确回复“已确认”；首次请求中附带的确认无效。"
                "确认后重试并传入 --confirmed。",
            )
        codex_bin = find_desktop_codex() if args.command in {"status", "setup", "repair", "test"} else None
        if args.command == "status":
            payload = static_status(paths, codex_bin)
        else:
            with operation_lock(paths):
                if args.command in {"setup", "repair"}:
                    payload = setup(
                        paths,
                        codex_bin or "",
                        args.skip_live_test,
                        args.model,
                        args.effort,
                        args.vision,
                        model_env=args.model_env,
                        effort_env=args.effort_env,
                        vision_env=args.vision_env,
                        replace_agent=args.replace_agent,
                    )
                elif args.command == "test":
                    payload = run_tests(paths, codex_bin or "")
                elif args.command == "disable":
                    payload = disable(paths)
                else:
                    payload = uninstall(paths)
        emit(payload, args.json)
        return 0 if payload["status"] not in {
            "partial",
            "configuration_missing",
            "model_selection_required",
        } else 2
    except ManagerError as exc:
        emit(result(exc.code, message=str(exc), **exc.details), args.json)
        return 2
    except subprocess.TimeoutExpired:
        emit(result("timeout", message="操作超时，未输出任何凭据。"), args.json)
        return 3
    except Exception as exc:
        emit(result("failed", message=f"{type(exc).__name__}: {exc}"), args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
