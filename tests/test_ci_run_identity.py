import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RunIdentity(unittest.TestCase):
    def test_real_cli_keeps_run_id_number_and_attempt_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "package.tar.gz"
            artifact.write_bytes(b"package")
            bundle = root / "bundle"
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "travis/ci_evidence.py"),
                                     "prepare-package", "--artifact", str(artifact), "--bundle", str(bundle),
                                     "--release-tag", "6.1.1.8", "--source-repository", "noironetworks/acc-provision",
                                     "--source-sha", "a" * 40, "--trigger-tag", "6.1.1.8", "--upstream-id", "42",
                                     "--run-id", "12345678901", "--run-number", "567", "--run-attempt", "2",
                                     "--run-url", "https://example.test/run/12345678901", "--mode", "z",
                                     "--version", "6.1.1.8.dev1", "--registry", "https://test.pypi.org/project/acc-provision/6.1.1.8.dev1/#files"],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            run = json.loads((bundle / "manifest.json").read_text())["run"]
            self.assertEqual((run["id"], run["number"], run["attempt"]), ("12345678901", "567", "2"))
