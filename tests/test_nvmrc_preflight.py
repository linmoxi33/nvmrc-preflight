"""Tests for nvmrc-preflight.

Two layers:
  * grammar tests  -- import the parser and compare against tests/corpus.json,
    whose expectations were observed from the real nvm 0.41.0.
  * behaviour tests -- run the CLI in a subprocess and assert exit codes,
    JSON shape and --fail-on semantics.
  * oracle test (optional) -- run the real `nvm_process_nvmrc` when an nvm
    checkout is available (env NVM_SH or /tmp/nvm/nvm.sh) and assert that our
    token matches byte for byte.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin", "nvmrc-preflight")


def _load_module():
    spec = importlib.util.spec_from_loader(
        "nvmrc_preflight_mod", loader=importlib.machinery.SourceFileLoader("nvmrc_preflight_mod", BIN)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def run_cli(*args, cwd=None):
    proc = subprocess.run(
        [sys.executable, BIN, *args],
        cwd=cwd or ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc


class GrammarCorpusTests(unittest.TestCase):
    def test_corpus_is_large_enough(self):
        self.assertGreaterEqual(len(mod.__dict__) and len(CORPUS), 20)

    def test_every_corpus_case(self):
        for case in CORPUS:
            with self.subTest(case=case["name"]):
                token, findings = mod.parse_nvmrc(case["content"].encode("utf-8"))
                self.assertEqual(token, case["token"])
                self.assertEqual([f.code for f in findings], case["codes"])

    def test_bom_witness_is_byte_level(self):
        token, findings = mod.parse_nvmrc(b"\xef\xbb\xbf" + b"20.11.0\n")
        self.assertEqual(token, "\ufeff20.11.0")
        wit = findings[0].witness
        self.assertEqual(wit["first_bytes"], [0xEF, 0xBB, 0xBF])
        self.assertEqual(wit["would_be"], "20.11.0")

    def test_bom_free_token_has_no_finding(self):
        _, findings = mod.parse_nvmrc(b"20.11.0\n")
        self.assertEqual(findings, [])


class DiscoveryTests(unittest.TestCase):
    def test_nearest_nvmrc_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, ".nvmrc"), "w").write("18\n")
            sub = os.path.join(tmp, "a", "b")
            os.makedirs(sub)
            self.assertEqual(mod.find_nvmrc(sub), os.path.join(tmp, ".nvmrc"))
            open(os.path.join(tmp, "a", ".nvmrc"), "w").write("20\n")
            self.assertEqual(mod.find_nvmrc(sub), os.path.join(tmp, "a", ".nvmrc"))

    def test_no_nvmrc_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(mod.find_nvmrc(tmp))


class CliBehaviourTests(unittest.TestCase):
    def _fixture(self, content, extra=None):
        tmp = tempfile.mkdtemp(prefix="nvmrcpf-")
        with open(os.path.join(tmp, ".nvmrc"), "wb") as fh:
            fh.write(content)
        if extra:
            for name, data in extra.items():
                with open(os.path.join(tmp, name), "wb") as fh:
                    fh.write(data)
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return tmp

    def test_exit_0_on_clean_file(self):
        d = self._fixture(b"v18.12.1\n")
        proc = run_cli("check", d, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["token"], "v18.12.1")
        self.assertEqual(payload["summary"]["errors"], 0)

    def test_exit_1_on_bom_silent_corruption(self):
        d = self._fixture(b"\xef\xbb\xbf20.11.0\n")
        proc = run_cli("check", d, "--json")
        self.assertEqual(proc.returncode, 1)
        codes = [f["code"] for f in json.loads(proc.stdout)["findings"]]
        self.assertIn("NRC008", codes)

    def test_exit_2_on_missing_path(self):
        proc = run_cli("check", "/definitely/not/here")
        self.assertEqual(proc.returncode, 2)

    def test_fail_on_error_ignores_warning_only_file(self):
        d = self._fixture(b"18.12.1\n", extra={".node-version": b"20.11.0\n"})
        self.assertEqual(run_cli("check", d).returncode, 1)
        self.assertEqual(run_cli("check", d, "--fail-on", "error").returncode, 0)
        self.assertEqual(run_cli("check", d, "--fail-on", "never").returncode, 0)

    def test_json_output_is_parseable(self):
        d = self._fixture(b"18.12.1\n")
        proc = run_cli("check", d, "--json")
        json.loads(proc.stdout)

    def test_resolve_prints_token(self):
        d = self._fixture(b"lts/*\n")
        proc = run_cli("resolve", d)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "lts/*")

    def test_resolve_invalid_exit_1(self):
        d = self._fixture(b"18\n20\n")
        self.assertEqual(run_cli("resolve", d).returncode, 1)

    def test_resolve_no_file_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_cli("resolve", tmp).returncode, 2)

    def test_usage_error_exit_2(self):
        self.assertEqual(run_cli().returncode, 2)

    def test_crlf_flagged_as_info(self):
        d = self._fixture(b"18.12.1\r\n")
        proc = run_cli("check", d, "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0)  # info-only never fails
        self.assertEqual([f["code"] for f in payload["findings"]], ["NRC009"])

    def test_node_version_sibling_flagged(self):
        d = self._fixture(b"18.12.1\n", extra={".node-version": b"20.11.0\n"})
        proc = run_cli("check", d, "--json")
        codes = [f["code"] for f in json.loads(proc.stdout)["findings"]]
        self.assertIn("NRC010", codes)


class OracleTests(unittest.TestCase):
    """Differential check against the real nvm parser, when available."""

    NVM_SH = os.environ.get("NVM_SH", "/tmp/nvm/nvm.sh")

    def real_nvm_token(self, path):
        script = '. "%s" --no-use >/dev/null 2>&1; nvm_process_nvmrc "%s"' % (self.NVM_SH, path)
        proc = subprocess.run(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return proc.returncode, proc.stdout.rstrip("\n")

    @unittest.skipUnless(os.path.exists(os.environ.get("NVM_SH", "/tmp/nvm/nvm.sh")),
                         "no nvm checkout available (set NVM_SH)")
    def test_real_nvm_agrees_on_every_corpus_case(self):
        agreed = 0
        for case in CORPUS:
            with self.subTest(case=case["name"]):
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, ".nvmrc")
                    with open(path, "wb") as fh:
                        fh.write(case["content"].encode("utf-8"))
                    rc, token = self.real_nvm_token(path)
                    ours, _ = mod.parse_nvmrc(case["content"].encode("utf-8"))
                    if case["token"] is None:
                        self.assertNotEqual(rc, 0)
                    else:
                        self.assertEqual(rc, 0)
                        self.assertEqual(token, ours)
                    agreed += 1
        self.assertGreaterEqual(agreed, 20)


with open(os.path.join(HERE, "corpus.json"), encoding="utf-8") as _fh:
    CORPUS = json.load(_fh)["cases"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
