from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "codex_custom_agent.py"
SPEC = importlib.util.spec_from_file_location("codex_custom_agent_under_test", SCRIPT)
assert SPEC and SPEC.loader
MANAGER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MANAGER
SPEC.loader.exec_module(MANAGER)


class SetupConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = MANAGER.resolve_paths(self.temporary.name)
        self.install_result = {
            "backup": str(Path(self.temporary.name) / "backup"),
            "selected_model": "vendor-model",
            "reasoning_effort": "medium",
            "supports_vision": True,
        }

    def test_environment_values_are_forwarded_without_touching_credentials(self) -> None:
        with (
            patch.dict(os.environ, {
                "CUSTOM_AGENT_MODEL": "vendor-model",
                "CUSTOM_AGENT_REASONING_EFFORT": "medium",
                "CUSTOM_AGENT_VISION": "yes",
            }),
            patch.object(MANAGER, "install", return_value=self.install_result) as install,
        ):
            result = MANAGER.setup(
                self.paths,
                "unused-codex",
                True,
                None,
                None,
                None,
                model_env=True,
                effort_env=True,
                vision_env=True,
            )

        self.assertEqual(result["status"], "configured")
        install.assert_called_once_with(self.paths, "unused-codex", "vendor-model", "medium", True)

    def test_old_manifest_defaults_to_high_and_text_only(self) -> None:
        self.paths.state_dir.mkdir(parents=True)
        self.paths.manifest.write_text(json.dumps({"selected_model": "vendor-model"}), encoding="utf-8")
        with (
            patch.object(MANAGER, "install", return_value=self.install_result) as install,
        ):
            MANAGER.setup(self.paths, "unused-codex", True, None, None, None)

        install.assert_called_once_with(self.paths, "unused-codex", "vendor-model", "high", False)

    def test_empty_vision_environment_is_rejected(self) -> None:
        with patch.dict(os.environ, {"CUSTOM_AGENT_VISION": ""}):
            with self.assertRaises(MANAGER.ManagerError) as caught:
                MANAGER.setup(self.paths, "unused-codex", True, "vendor-model", "high", None, vision_env=True)

        self.assertEqual(caught.exception.code, "configuration_missing")


class ModelCatalogTests(unittest.TestCase):
    def base(self) -> dict[str, object]:
        return {"models": [{"slug": "parent", "display_name": "Parent", "input_modalities": ["text"]}]}

    def test_vision_enabled_adds_image_modality(self) -> None:
        model = MANAGER.model_for_endpoint(self.base(), "parent", "child", "medium", True)["child"]
        self.assertEqual(model["input_modalities"], ["text", "image"])
        self.assertTrue(model["supports_image_detail_original"])
        self.assertEqual(model["default_reasoning_level"], "medium")

    def test_vision_disabled_remains_text_only(self) -> None:
        model = MANAGER.model_for_endpoint(self.base(), "parent", "child", "low", False)["child"]
        self.assertEqual(model["input_modalities"], ["text"])
        self.assertFalse(model["supports_image_detail_original"])

    def test_agent_uses_parent_provider_and_selected_effort(self) -> None:
        text = MANAGER.expected_agent_text("child", "parent-provider", "low", True)
        self.assertIn('model_provider = "parent-provider"', text)
        self.assertIn('model_reasoning_effort = "low"', text)
        self.assertIn("inspect them directly", text)

    def test_text_only_agent_does_not_claim_image_access(self) -> None:
        text = MANAGER.expected_agent_text("child", "parent-provider", "high", False)
        self.assertIn("configured for text-only input", text)


class InstallIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = MANAGER.resolve_paths(self.temporary.name)
        self.paths.home.mkdir(parents=True, exist_ok=True)

    def test_install_inherits_parent_provider_and_preserves_parent_auth(self) -> None:
        original = (
            'model = "parent-model"\n'
            'model_provider = "parent-provider"\n'
            '[model_providers.parent-provider]\n'
            'base_url = "https://gateway.example/v1"\n'
            'wire_api = "responses"\n'
            '[model_providers.parent-provider.auth]\n'
            'env_key = "PARENT_KEY"\n'
            f'{MANAGER.PROVIDER_BEGIN}\n'
            '[model_providers.custom_agent]\n'
            'base_url = "https://legacy.example/v1"\n'
            f'{MANAGER.PROVIDER_END}\n'
        )
        self.paths.config.write_text(original, encoding="utf-8")
        catalog = {"models": [{"slug": "parent-model", "display_name": "Parent"}]}

        with patch.object(MANAGER, "load_base_catalog", return_value=catalog):
            outcome = MANAGER.install(self.paths, "unused-codex", "child-model", "medium", True)

        updated = self.paths.config.read_text(encoding="utf-8")
        parsed = MANAGER.parse_toml_text(updated)
        self.assertEqual(parsed["model_provider"], "parent-provider")
        self.assertEqual(parsed["model_providers"]["parent-provider"]["auth"]["env_key"], "PARENT_KEY")
        self.assertNotIn("custom_agent", parsed["model_providers"])
        self.assertTrue(outcome["parent_credentials_untouched"])
        self.assertTrue(outcome["legacy_provider_block_removed"])
        agent = self.paths.agent.read_text(encoding="utf-8")
        self.assertIn('model_provider = "parent-provider"', agent)
        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["reasoning_effort"], "medium")
        self.assertTrue(manifest["supports_vision"])


class StdioEncodingTests(unittest.TestCase):
    def test_help_is_utf8_without_python_encoding_environment(self) -> None:
        environment = dict(os.environ)
        environment.pop("PYTHONUTF8", None)
        environment.pop("PYTHONIOENCODING", None)

        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=True,
        )

        output = completed.stdout.decode("utf-8")
        error = completed.stderr.decode("utf-8")
        self.assertIn("配置并验证用户指定模型作为 Codex 原生子 Agent", output)
        self.assertEqual(error, "")


class ConfirmationGuardTests(unittest.TestCase):
    def test_mutating_cli_command_requires_explicit_confirmed_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "disable", "--codex-home", directory, "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )

        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(payload["status"], "confirmation_required")
        self.assertIn("已确认", payload["message"])
        self.assertEqual(completed.stderr, "")


class ConfigCompatibilityTests(unittest.TestCase):
    def test_removed_feature_flag_is_deleted_without_touching_valid_flags(self) -> None:
        text = (
            "[features]\n"
            "thread_tools = true\n"
            "multi_agent_v2 = false\n"
            "\n"
            "[desktop]\n"
            'localeOverride = "zh-CN"\n'
        )

        updated = MANAGER.remove_table_key(text, "features", "thread_tools")

        parsed = MANAGER.parse_toml_text(updated)
        self.assertNotIn("thread_tools", parsed["features"])
        self.assertIs(parsed["features"]["multi_agent_v2"], False)
        self.assertEqual(parsed["desktop"]["localeOverride"], "zh-CN")

    def test_removed_feature_flag_is_reported(self) -> None:
        parsed = {"features": {"thread_tools": True, "multi_agent_v2": False}}

        self.assertEqual(MANAGER.removed_feature_flags(parsed), ["thread_tools"])


class NativeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = MANAGER.resolve_paths(self.temporary.name)
        self.paths.home.mkdir(parents=True, exist_ok=True)
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        self.paths.config.write_text(
            'model_provider = "parent"\n'
            '[model_providers.parent]\n'
            'base_url = "https://gateway.example/v1"\n',
            encoding="utf-8",
        )

    def test_route_always_inherits_parent_provider(self) -> None:
        route = MANAGER.native_route_details(self.paths)

        self.assertEqual(route["route_mode"], "inherited_parent_provider")
        self.assertEqual(route["expected_provider"], "parent")
        self.assertEqual(route["credential_source"], "parent_provider")
        self.assertFalse(route["uses_dedicated_credential"])

    def test_native_event_parser_preserves_failed_child_state(self) -> None:
        output = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "collab_tool_call",
                            "tool": "spawn_agent",
                            "receiver_thread_ids": ["child-1"],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "collab_tool_call",
                            "tool": "wait",
                            "agents_states": {
                                "child-1": {
                                    "status": "failed",
                                    "message": "model is not supported by the parent account",
                                }
                            },
                        },
                    }
                ),
            ]
        )

        child_ids, states = MANAGER.parse_native_events(output)

        self.assertEqual(child_ids, ["child-1"])
        self.assertEqual(states["child-1"]["status"], "failed")
        self.assertIn("parent account", states["child-1"]["message"])


if __name__ == "__main__":
    unittest.main()
