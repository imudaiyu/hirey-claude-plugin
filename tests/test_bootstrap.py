"""Isolated installer/onboarding regressions; fake curl never contacts a service."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE_CURL = r'''#!/usr/bin/env python3
import base64, json, os, pathlib, sys, time
args = sys.argv[1:]
assert "--connect-timeout" in args and "--max-time" in args, "unbounded HTTP call"
assert any(a.startswith("-") and "f" in a and not a.startswith("--") for a in args), "HTTP errors not rejected"
url = next(a for a in args if a.startswith("https://"))
mode = os.environ["TEST_MODE"]
with open(os.environ["TEST_CALLS"], "a") as f: f.write(url + "\n")
if "raw.githubusercontent.com" in url:
    assert "/adbee73a974b269626c8a8a49f84132707cd4564/" in url, "skills must use the verified release commit"
    assert args[args.index("--retry") + 1] == "2"
    assert args[args.index("--retry-max-time") + 1] == "40"
    if mode == "download_failure" and "hi-events/" in url: sys.exit(28)
    pathlib.Path(args[args.index("-o") + 1]).write_text("new skill\n")
elif url.endswith("/api-keys"):
    body = json.loads(sys.stdin.read() if "--data-binary" in args else args[args.index("--data") + 1])
    assert {k: body[k] for k in ("agent_type", "display_name", "client_version")} == {"agent_type":"claude", "display_name":"Claude Code (Hirey skill)", "client_version":"0.2.6"}
    replayable = "request_id" in body
    if replayable:
        assert len(body["request_id"]) == 32 and body["pending_proof_token"].startswith("hi_ai_")
        assert len(body["client_secret"]) == 64 and body["occurred_at"]
        saved = pathlib.Path(os.environ["CREDS_DIR"]) / ".fixture-registration-body"
        if saved.exists(): assert json.loads(saved.read_text()) == body, "retry changed registration request"
        else: saved.write_text(json.dumps(body))
    else:
        assert "--retry" not in args, "legacy registration must not be retried"
    if mode == "http_failure": sys.exit(22)
    if mode == "timeout": sys.exit(28)
    if mode == "bad_registration":
        print('{"error":"oops","debug":"TEST_SECRET_SENTINEL"}')
    else:
        key = {"v": 2 if mode == "bad_key_version" else 1,"id":"client","secret":body["client_secret"] if replayable else "TEST_SECRET_SENTINEL"}
        if mode == "bad_key_fields": key["secret"] = 12
        encoded = base64.urlsafe_b64encode(json.dumps(key).encode()).decode().rstrip("=")
        if mode == "bad_key_encoding": encoded = "not-json"
        print(json.dumps({"api_key":"hi_ak_"+encoded,"agent_id":"agent","status":"pending"}))
elif url.endswith("/oauth/token"):
    assert "--data-binary" in args and "--data-urlencode" not in args
    token_body = sys.stdin.read()
    assert token_body.startswith("grant_type=client_credentials&client_id=")
    assert "client_secret=" in token_body and "audience=" in token_body
    assert not any("client_secret=" in arg for arg in args), "secret exposed in process arguments"
    assert args[args.index("--retry") + 1] == "2"
    assert args[args.index("--retry-max-time") + 1] == "40"
    assert (pathlib.Path(os.environ["CREDS_DIR"]) / ".register.lock").exists()
    if mode == "slow_token": time.sleep(0.25)
    if mode == "bad_token": print('{"error":"invalid_grant","debug":"TEST_SECRET_SENTINEL"}')
    else: print('{"access_token":"TEST_TOKEN_SENTINEL","expires_in":3600}')
elif url.endswith("/agents/me"):
    print('{"agent":{"agent_id":"agent","status":"pending"},"installation":{"installation_id":"install"}}')
else: sys.exit(99)
'''

class BootstrapTests(unittest.TestCase):
    def run_case(self, source, mode, existing=None, retry=False, channel="", parallel=False, force_refresh=False, base="https://fixture.invalid", stale_lock=False, live_lock=False, pending=False):
        with tempfile.TemporaryDirectory(prefix="hi-claude-test-") as tmp:
            task_dir = Path(tmp)
            bin_dir = task_dir / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "curl"
            fake.write_text(FAKE_CURL)
            fake.chmod(0o755)
            creds_dir = task_dir / "xdg" / "hi"
            creds_dir.mkdir(parents=True)
            creds = creds_dir / "credentials.json"
            if existing is not None:
                creds.write_text(existing)
                creds.chmod(0o600)
            if stale_lock or live_lock:
                lock = creds_dir / ".register.lock"
                lock.mkdir()
                (lock / "owner.pid").write_text(f"{os.getpid() if live_lock else 99999999}\n")
                old = time.time() - 120
                os.utime(lock, (old, old))
            if pending:
                (creds_dir / ".registration-pending.json").write_text(
                    json.dumps(pending) if isinstance(pending, dict) else '{"status":"outcome_unknown"}\n')
            skills = task_dir / "skills"
            for name in ("hi-onboard", "hi-use", "hi-events", "hi-repair"):
                path = skills / name
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text("old skill\n")
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                       XDG_CONFIG_HOME=str(task_dir / "xdg"), CREDS_DIR=str(creds_dir),
                       SKILLS_DIR=str(skills), HI_FORCE_INSTALL="1",
                       HI_BASE="https://fixture.invalid", TEST_MODE=mode,
                       HI_CHANNEL_CODE=channel,
                       HI_FORCE_TOKEN_REFRESH="1" if force_refresh else "0",
                       TEST_CALLS=str(task_dir / "calls"))
            if base is None:
                env.pop("HI_BASE", None)
            else:
                env["HI_BASE"] = base
            if source == "installer":
                command = ["bash", str(ROOT / "install.sh")]
                data = None
            else:
                text = (ROOT / "plugins/hirey-hi/skills/hi-onboard/SKILL.md").read_text()
                data = next(block for block in re.findall(r"```bash\n(.*?)```", text, re.S) if "set -euo pipefail" in block)
                command = ["bash"]
            if parallel:
                first = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                if data is not None:
                    first.stdin.write(data)
                first.stdin.close()
                first.stdin = None
                result = subprocess.run(command, input=data, text=True, env=env, capture_output=True, timeout=15)
                out, err = first.communicate(timeout=15)
                self.assertEqual(first.returncode, 0, out + err)
                self.assertNotIn("TEST_SECRET_SENTINEL", out + err)
                self.assertNotIn("TEST_TOKEN_SENTINEL", out + err)
            else:
                result = subprocess.run(command, input=data, text=True, env=env, capture_output=True, timeout=15)
            if retry:
                env["TEST_MODE"] = "ok"
                second = subprocess.run(command, input=data, text=True, env=env, capture_output=True, timeout=15)
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertNotIn("TEST_SECRET_SENTINEL", result.stdout + result.stderr)
            self.assertNotIn("TEST_TOKEN_SENTINEL", result.stdout + result.stderr)
            calls = (task_dir / "calls").read_text() if (task_dir / "calls").exists() else ""
            stored = creds.read_text() if creds.exists() else None
            permissions = (creds.stat().st_mode & 0o777) if creds.exists() else None
            old_skills = all((skills / n / "SKILL.md").read_text() == "old skill\n" for n in ("hi-onboard", "hi-use", "hi-events", "hi-repair"))
            self.assertFalse(list(creds_dir.glob(".credentials.*")))
            self.assertEqual((creds_dir / ".register.lock").exists(), live_lock)
            if retry:
                self.assertEqual(calls.count("/api-keys"), 2)
                self.assertFalse((creds_dir / ".registration-pending.json").exists())
            if result.returncode == 0:
                self.assertFalse((creds_dir / ".registration-pending.json").exists())
            return result.returncode, stored, permissions, calls, old_skills

    def test_failed_registration_never_persists_credentials(self):
        for source in ("installer", "skill"):
            for mode in ("http_failure", "bad_registration", "timeout", "bad_key_version", "bad_key_fields", "bad_key_encoding"):
                with self.subTest(source=source, mode=mode):
                    code, stored, _, calls, _ = self.run_case(source, mode)
                    self.assertNotEqual(code, 0)
                    self.assertIsNone(stored)
                    self.assertNotIn("/oauth/token", calls)

    def test_ambiguous_creation_is_not_repeated(self):
        for source in ("installer", "skill"):
            for mode in ("timeout", "http_failure", "bad_registration", "bad_key_encoding"):
                with self.subTest(source=source, mode=mode):
                    self.run_case(source, mode, retry=True)

    def test_stale_lock_recovers_without_repeating_ambiguous_registration(self):
        existing = json.dumps({"client_id":"same-client","client_secret":"TEST_SECRET_SENTINEL","agent_id":"same-agent","audience":"hirey-hi","status":"pending"})
        for source in ("installer", "skill"):
            with self.subTest(source=source):
                code, stored, _, calls, _ = self.run_case(source, "ok", existing, stale_lock=True)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stored)["agent_id"], "same-agent")
                self.assertNotIn("/api-keys", calls)
                code, stored, _, calls, _ = self.run_case(source, "ok", stale_lock=True, pending=True)
                self.assertNotEqual(code, 0)
                self.assertIsNone(stored)
                self.assertNotIn("/api-keys", calls)

    def test_completed_credential_clears_only_its_matching_marker(self):
        existing = json.dumps({"client_id":"same-client","client_secret":"TEST_SECRET_SENTINEL",
                               "agent_id":"same-agent","audience":"hirey-hi","status":"pending"})
        marker = {"version": 1, "base": "https://fixture.invalid", "host": "claude",
                  "body": {"client_secret": "TEST_SECRET_SENTINEL"}}
        for source in ("installer", "skill"):
            with self.subTest(source=source):
                code, stored, _, calls, _ = self.run_case(source, "ok", existing, pending=marker)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stored)["client_id"], "same-client")
                self.assertNotIn("/api-keys", calls)

    def test_live_lock_is_never_recovered(self):
        existing = json.dumps({"client_id":"same-client","client_secret":"TEST_SECRET_SENTINEL","agent_id":"same-agent","audience":"hirey-hi","status":"pending"})
        code, stored, _, calls, _ = self.run_case("installer", "ok", existing, live_lock=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(stored, existing)
        self.assertNotIn("/api-keys", calls)
        self.assertNotIn("/oauth/token", calls)

    def test_referral_is_not_silently_discarded(self):
        for source in ("installer", "skill"):
            code, stored, _, calls, _ = self.run_case(source, "ok", channel="user-supplied")
            self.assertNotEqual(code, 0)
            self.assertIsNone(stored)
            self.assertNotIn("/api-keys", calls)

    def test_skill_uses_same_locked_bootstrap_as_installer(self):
        installer = (ROOT / "install.sh").read_text()
        section = installer.split('mkdir -p "$CREDS_DIR" && chmod 700', 1)[1].split('\nAGENT_ID=$(jq', 1)[0]
        skill = (ROOT / "plugins/hirey-hi/skills/hi-onboard/SKILL.md").read_text()
        self.assertIn(section, skill)

    def test_concurrent_refresh_rechecks_under_shared_lock(self):
        existing = json.dumps({"client_id":"same-client","client_secret":"TEST_SECRET_SENTINEL","agent_id":"same-agent","audience":"hirey-hi","status":"pending"})
        for source in ("installer", "skill"):
            with self.subTest(source=source):
                code, stored, _, calls, _ = self.run_case(source, "slow_token", existing, parallel=True)
                self.assertEqual(code, 0)
                self.assertEqual(calls.count("/oauth/token"), 1)
                self.assertNotIn("/api-keys", calls)
                self.assertEqual(json.loads(stored)["client_id"], "same-client")

    def test_verified_binding_can_force_refresh_of_fresh_pending_token(self):
        existing = json.dumps({"client_id":"same-client","client_secret":"TEST_SECRET_SENTINEL","agent_id":"same-agent","audience":"hirey-hi","status":"pending","access_token":"TEST_TOKEN_SENTINEL","access_token_issued_at":int(time.time()),"access_token_expires_in":3600})
        for source in ("installer", "skill"):
            with self.subTest(source=source):
                code, _, _, calls, _ = self.run_case(source, "ok", existing, force_refresh=True)
                self.assertEqual(code, 0)
                self.assertEqual(calls.count("/oauth/token"), 1)
                self.assertNotIn("/api-keys", calls)

    def test_stored_host_controls_refresh_and_override_mismatch_is_rejected(self):
        existing = json.dumps({"client_id":"same-client","client_secret":"TEST_SECRET_SENTINEL","agent_id":"same-agent","audience":"hirey-hi","platform_base_url":"https://early.fixture.invalid"})
        for source in ("installer", "skill"):
            with self.subTest(source=source):
                code, _, _, calls, _ = self.run_case(source, "ok", existing, base=None)
                self.assertEqual(code, 0)
                self.assertIn("https://early.fixture.invalid/oauth/token", calls)
                self.assertNotIn("https://hi.hirey.ai/oauth/token", calls)
                code, stored, _, calls, _ = self.run_case(source, "ok", existing, base="https://different.fixture.invalid")
                self.assertNotEqual(code, 0)
                self.assertEqual(stored, existing)
                self.assertNotIn("/oauth/token", calls)
                self.assertNotIn("/api-keys", calls)

    def test_insecure_non_loopback_hosts_are_rejected_before_credentials(self):
        for source in ("installer", "skill"):
            for base in ("http://early.fixture.invalid", "https://user@fixture.invalid", "https://fixture.invalid/path"):
                with self.subTest(source=source, base=base):
                    code, stored, _, calls, _ = self.run_case(source, "ok", base=base)
                    self.assertNotEqual(code, 0)
                    self.assertIsNone(stored)
                    self.assertNotIn("/api-keys", calls)
                    self.assertNotIn("/oauth/token", calls)

    def test_401_recovery_explicitly_forces_refresh(self):
        skill = (ROOT / "plugins/hirey-hi/skills/hi-onboard/SKILL.md").read_text()
        self.assertRegex(skill, r"401 missing_bearer[^\n]+HI_FORCE_TOKEN_REFRESH=1[^\n]+once")

    def test_refresh_failure_preserves_existing_identity(self):
        existing = json.dumps({"client_id":"same-client","client_secret":"TEST_SECRET_SENTINEL","agent_id":"same-agent","audience":"hi"})
        for source in ("installer", "skill"):
            with self.subTest(source=source):
                code, stored, _, calls, _ = self.run_case(source, "bad_token", existing)
                self.assertNotEqual(code, 0)
                self.assertEqual(stored, existing)
                self.assertNotIn("/register", calls)

    def test_corrupt_existing_identity_is_not_replaced(self):
        for source in ("installer", "skill"):
            with self.subTest(source=source):
                code, stored, _, calls, _ = self.run_case(source, "ok", "{}")
                self.assertNotEqual(code, 0)
                self.assertEqual(stored, "{}")
                self.assertNotIn("/register", calls)

    def test_success_has_private_credentials(self):
        for source in ("installer", "skill"):
            with self.subTest(source=source):
                code, stored, mode, calls, _ = self.run_case(source, "ok")
                self.assertEqual(code, 0)
                self.assertEqual(mode, 0o600)
                self.assertEqual(json.loads(stored)["client_id"], "client")
                self.assertEqual(json.loads(stored)["status"], "pending")
                self.assertNotIn("api_key", json.loads(stored))
                self.assertNotIn("/agents/me", calls)
                self.assertNotIn("/register", calls)

    def test_failed_download_does_not_partially_upgrade(self):
        code, stored, _, calls, old = self.run_case("installer", "download_failure")
        self.assertNotEqual(code, 0)
        self.assertIsNone(stored)
        self.assertTrue(old)
        self.assertNotIn("/register", calls)

    def test_business_calls_surface_upgrade_policy_without_extra_request(self):
        skill = (ROOT / "plugins/hirey-hi/skills/hi-use/SKILL.md").read_text()
        self.assertIn("_meta.hirey_plugin", skill)
        self.assertIn("update_required=true", skill)
        self.assertIn("update_recommended=true", skill)
        self.assertIn("Do not make a separate version-policy request", skill)
        self.assertIn("--connect-timeout 5 --max-time 30", skill)

        reference = (ROOT / "plugins/hirey-hi/reference/api.md").read_text()
        self.assertEqual(
            reference.count("curl -sS"),
            reference.count("curl -sS --connect-timeout 5 --max-time 30"),
        )

if __name__ == "__main__":
    unittest.main()
