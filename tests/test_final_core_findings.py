"""Real-entrypoint regressions for the final independent core review."""
import copy
import hashlib
import json
import os
import shutil
import sys
import unittest

import test_ci_evidence as evidence_fixture
import test_package_receipt as package_fixture
import test_scan_result as scan_fixture

ROOT = evidence_fixture.ROOT


class FinalCoreFindings(unittest.TestCase):
    setUp = evidence_fixture.EvidenceCLI.setUp
    command = evidence_fixture.EvidenceCLI.command
    cli = evidence_fixture.EvidenceCLI.cli
    git = evidence_fixture.EvidenceCLI.git
    remote = evidence_fixture.EvidenceCLI.remote
    prepare = evidence_fixture.EvidenceCLI.prepare
    publish = evidence_fixture.EvidenceCLI.publish
    expected_args = evidence_fixture.EvidenceCLI.expected_args
    push = scan_fixture.ScanCLI.push
    bundle_and_receipt = package_fixture.PackageReceipt.bundle_and_receipt
    publish_receipt = package_fixture.PackageReceipt.publish_receipt

    def decoded_counts(self, text):
        result = self.command([sys.executable, "-B", "-c",
                               "import json,sys; sys.path.insert(0,sys.argv[1]); import ci_evidence; "
                               "print(json.dumps(ci_evidence.report_counts(sys.argv[2])))",
                               str(ROOT / "travis"), text])
        return json.loads(result.stdout)

    def test_json_failure_messages_never_become_legacy_clean_results(self):
        values = (json.dumps({"state": "error", "reason": "No vulnerabilities found"}),
                  '{"state":"error","reason":"No vulnerabilities found"',
                  '[{"state":"error","reason":"No vulnerabilities found"}',
                  json.dumps("No vulnerabilities found"))
        for text in values:
            with self.subTest(text=text):
                self.assertIsNone(self.decoded_counts(text))

    def test_supported_legacy_zero_and_valid_json_counts_are_preserved(self):
        self.assertEqual(self.decoded_counts("No vulnerabilities found\n"), dict.fromkeys("CHMLU", 0))
        counts = dict.fromkeys("CHMLU", 0)
        counts["H"] = 3
        self.assertEqual(self.decoded_counts(json.dumps({"state": "ok", "counts": counts})), counts)

    def test_fatal_producer_json_counts_survive_z_to_release_and_rc(self):
        artifacts = self.root / "push-ok-image-0"
        dockerfile = self.root / "Dockerfile"
        dockerfile.write_text("FROM registry.access.redhat.com/ubi9/ubi-minimal:latest\n")
        self.command([sys.executable, str(ROOT / "travis/scan_result.py"), "capture-base",
                      "--dockerfile", str(dockerfile), "--pull", "--output",
                      str(artifacts / "base-provenance/image-0.json")])
        result, artifacts = self.push(policy=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads((artifacts / "image-metadata.jsonl").read_text())
        self.assertEqual(metadata["scan_policy"], "fatal")
        self.assertEqual(metadata["scan"]["image"]["counts"]["H"], 1)
        source_bundle = self.prepare(artifacts / "image-metadata.jsonl")
        source = json.loads((source_bundle / "manifest.json").read_text())
        self.assertNotIn("scan", source["records"][0], "exercise the default fatal public schema")
        expected = source["records"][0]["severity"]
        self.assertEqual(expected[0]["H"], 1)
        repo, checkout = self.remote()
        self.publish(source_bundle, repo)
        self.git("pull", "--ff-only", cwd=checkout)
        artifact = self.root / "acc_provision.tar.gz"
        artifact.write_bytes(b"retained package fixture")
        for mode, version, host in (("release", "6.1.1.8", "pypi.org"),
                                    ("rc", "6.1.1.8rc1", "test.pypi.org")):
            with self.subTest(mode=mode):
                common = list(self.common)
                common[common.index("--source-repository") + 1] = "noironetworks/acc-provision"
                common[common.index("--source-sha") + 1] = "f" * 40
                common[common.index("--trigger-tag") + 1] = version
                bundle = self.root / ("promoted-" + mode)
                extra = ["--rc-number", "1"] if mode == "rc" else []
                self.cli("prepare-package", "--artifact", artifact, "--bundle", bundle,
                         "--mode", mode, "--version", version, "--registry",
                         f"https://{host}/project/acc-provision/{version}/#files",
                         "--status-checkout", checkout, *extra, *common)
                promoted = json.loads((bundle / "manifest.json").read_text())["records"][0]
                self.assertNotIn("scan", promoted)
                self.assertEqual(promoted["severity"], expected)
                self.assertEqual(promoted["base-image"][0]["severity"][0]["H"], 1)
                self.assertEqual(promoted["base-image-original"][0]["severity"][0]["H"], 1)

    def test_package_record_commit_cannot_escape_trusted_bundle_source(self):
        bundle, receipt = self.bundle_and_receipt()
        repo, checkout = self.remote()
        real_git = shutil.which("git", path=os.environ["PATH"])
        initial = self.git("--git-dir", str(repo), "rev-parse", "main")
        manifest_path = bundle / "manifest.json"
        original = json.loads(manifest_path.read_text())
        marker = self.root / "git-was-invoked"
        spy = self.bin / "git"
        spy.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\nexit 99\n")
        spy.chmod(0o755)
        wrong_sha = "b" * 40
        variants = ([{"sha": wrong_sha, "link": f"https://github.com/noironetworks/acc-provision/commit/{wrong_sha}"}],
                    [{"sha": original["source"]["sha"], "link": "https://github.com/unrelated/project/commit/" + original["source"]["sha"]}],
                    [{"sha": wrong_sha, "link": original["package"][0]["commit"][0]["link"]}], [])
        for commit in variants:
            with self.subTest(commit=commit):
                manifest = copy.deepcopy(original)
                manifest["package"][0]["commit"] = commit
                manifest_path.write_text(json.dumps(manifest))
                result = self.publish_receipt(bundle, repo, receipt)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("package record source differs", result.stderr)
                self.assertFalse(marker.exists(), "publisher reached Git with a contradictory package commit")
        current = self.command([real_git, "--git-dir", str(repo), "rev-parse", "main"]).stdout.strip()
        self.assertEqual(initial, current)

    def test_malformed_nested_scanner_objects_remain_advisory(self):
        image_id = "sha256:" + "e" * 64
        grype = {"matches": [], "source": {"target": {"imageID": image_id}}}
        trivy = {"SchemaVersion": 2, "Metadata": {"ImageID": image_id}, "Results": []}
        pairs = (({"matches": [], "source": None}, {**trivy, "Metadata": None}),
                 ({"matches": [], "source": []}, {**trivy, "Results": [None]}),
                 ({"matches": [], "source": {"target": None}}, {**trivy, "Results": [{"Vulnerabilities": [None]}]}),
                 ({**grype, "matches": [{"vulnerability": None}]}, {**trivy, "Results": [{"Vulnerabilities": [{"Severity": None}]}]}),
                 ({**grype, "matches": [None]}, {**trivy, "Results": ["not-an-object"]}))
        for index, (bad_grype, bad_trivy) in enumerate(pairs):
            with self.subTest(case=index):
                lock_path = self.root / "tools.lock.json"
                lock = json.loads(lock_path.read_text())
                for name, payload in (("grype", bad_grype), ("trivy", bad_trivy)):
                    tool = self.bin / name
                    tool.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdout.write(" + repr(json.dumps(payload)) + ")\n")
                    tool.chmod(0o755)
                    lock["tools"][name]["sha256"] = hashlib.sha256(tool.read_bytes()).hexdigest()
                lock_path.write_text(json.dumps(lock))
                result, artifacts = self.push(mode="nested-" + str(index))
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = json.loads((artifacts / "publication/image-0.json").read_text())
                self.assertEqual(receipt["state"], "complete")
                self.assertTrue(any(item["phase"] == "push" for item in receipt["attempts"]))
                row = json.loads((artifacts / "image-metadata.jsonl").read_text())
                for target in ("image", "trivy"):
                    self.assertEqual(row["scan"][target]["state"], "error")
                    self.assertNotIn("counts", row["scan"][target])
                    self.assertNotIn("last_success", row["scan"][target])
                if index == 0:
                    bundle = self.prepare(artifacts / "image-metadata.jsonl", "nested-bundle")
                    repo, checkout = self.remote()
                    self.publish(bundle, repo)
