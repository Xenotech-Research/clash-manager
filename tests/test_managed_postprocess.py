import copy
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi import HTTPException

import server


def source_profile() -> dict:
    return {
        "mixed-port": 17890,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "proxies": [
            {"name": "node-a", "type": "socks5", "server": "127.0.0.1", "port": 1080},
        ],
        "proxy-groups": [
            {"name": "Main", "type": "select", "proxies": ["node-a", "DIRECT"]},
            {"name": "Claude", "type": "select", "proxies": ["Main"]},
        ],
        "rules": [
            "DOMAIN,unrelated.example,Main",
            "MATCH,DIRECT",
        ],
    }


def enabled_settings() -> dict:
    return {
        "version": 1,
        "enabled": True,
        "domains": list(server.DEFAULT_CLAUDE_DOMAINS),
        "upstream_group": "Main",
        "residential": {
            "server": "127.0.0.1",
            "port": 45001,
            "username": "test-user",
            "password": "test-password",
        },
    }


class ManagedPostprocessTests(unittest.TestCase):
    def test_exact_rules_are_prepended_and_source_profile_is_unchanged(self) -> None:
        raw = source_profile()
        before = copy.deepcopy(raw)

        deployed, upstream = server._build_deployed_clash_dict(raw, enabled_settings())

        self.assertEqual(upstream, "Main")
        self.assertEqual(raw, before)
        expected_rules = [
            f"DOMAIN,{domain},{server.MANAGED_RELAY_GROUP}"
            for domain in server.DEFAULT_CLAUDE_DOMAINS
        ]
        self.assertEqual(deployed["rules"][: len(expected_rules)], expected_rules)
        self.assertEqual(deployed["rules"][len(expected_rules):], before["rules"])
        self.assertEqual(
            sum(p.get("name") == server.MANAGED_RESIDENTIAL_PROXY for p in deployed["proxies"]),
            1,
        )
        self.assertEqual(
            sum(g.get("name") == server.MANAGED_RELAY_GROUP for g in deployed["proxy-groups"]),
            1,
        )

    def test_overlay_is_idempotent_and_does_not_accumulate_rules(self) -> None:
        first, _ = server._build_deployed_clash_dict(source_profile(), enabled_settings())
        second, _ = server._build_deployed_clash_dict(first, enabled_settings())

        self.assertEqual(first["rules"], second["rules"])
        self.assertEqual(
            sum(p.get("name") == server.MANAGED_RESIDENTIAL_PROXY for p in second["proxies"]),
            1,
        )
        self.assertEqual(
            sum(g.get("name") == server.MANAGED_RELAY_GROUP for g in second["proxy-groups"]),
            1,
        )

    def test_global_passthrough_keeps_non_target_traffic_on_virtual_global(self) -> None:
        raw = source_profile()
        raw["mode"] = "global"
        settings = enabled_settings()
        settings["non_target_behavior"] = "global_passthrough"

        deployed, _ = server._build_deployed_clash_dict(raw, settings)

        self.assertEqual(deployed["mode"], "rule")
        target_count = len(server.DEFAULT_CLAUDE_DOMAINS)
        self.assertEqual(deployed["rules"][target_count], "MATCH,GLOBAL")
        self.assertEqual(deployed["rules"][target_count + 1 :], raw["rules"])

    def test_rejects_wildcards_and_manager_cycles(self) -> None:
        with self.assertRaises(HTTPException):
            server._normalize_exact_domains(["*.claude.ai"])

        raw = source_profile()
        raw["proxy-groups"][0]["proxies"].append(server.MANAGED_RELAY_GROUP)
        with self.assertRaises(HTTPException):
            server._build_deployed_clash_dict(raw, enabled_settings())

    def test_raw_subscription_cache_is_not_overlayed_and_is_private(self) -> None:
        raw = source_profile()
        previous = server.PROFILE_CACHE_DIR
        with tempfile.TemporaryDirectory(prefix="clash-manager-test-cache-") as temp_dir:
            try:
                server.PROFILE_CACHE_DIR = Path(temp_dir) / "cache"
                server._save_subscription_cache_snapshot(9, raw)
                cache_file = server._subscription_cache_file(9)
                self.assertEqual(yaml.safe_load(cache_file.read_text(encoding="utf-8")), raw)
                self.assertFalse(server.MANAGED_PREFIX in cache_file.read_text(encoding="utf-8"))
                self.assertEqual(stat.S_IMODE(cache_file.stat().st_mode), 0o600)
            finally:
                server.PROFILE_CACHE_DIR = previous

    def test_blank_password_update_retains_the_private_stored_value(self) -> None:
        previous = server.MANAGED_POSTPROCESS_FILE
        with tempfile.TemporaryDirectory(prefix="clash-manager-test-state-") as temp_dir:
            try:
                server.MANAGED_POSTPROCESS_FILE = Path(temp_dir) / "managed_postprocess.json"
                server._save_postprocess_settings(enabled_settings())
                update = server.ClaudeResidentialPostprocessUpdate(
                    enabled=True,
                    domains=list(server.DEFAULT_CLAUDE_DOMAINS),
                    upstream_group="Main",
                    non_target_behavior="subscription_rules",
                    residential=server.ResidentialProxyUpdate(
                        server="127.0.0.1",
                        port=45001,
                        username="changed-user",
                    ),
                    apply_now=False,
                )
                result = server._settings_from_postprocess_update(update)
                self.assertEqual(result["residential"]["password"], "test-password")
                self.assertEqual(stat.S_IMODE(server.MANAGED_POSTPROCESS_FILE.stat().st_mode), 0o600)
            finally:
                server.MANAGED_POSTPROCESS_FILE = previous

    def test_hot_reload_supplies_the_absolute_config_path(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

        with patch.object(server, "_clash_request", return_value=Response()) as request, patch.object(
            server, "_restore_persisted_proxy_selections"
        ):
            self.assertTrue(server._hot_reload_clash())
        _, kwargs = request.call_args
        self.assertEqual(kwargs["json"]["path"], str(Path(server.config.CLASH_CONFIG_PATH).resolve()))


if __name__ == "__main__":
    unittest.main()
