import copy
import json
import unittest

import test_ci_evidence as fixture


class PackageReceipt(unittest.TestCase):
    setUp = fixture.EvidenceCLI.setUp
    command = fixture.EvidenceCLI.command
    cli = fixture.EvidenceCLI.cli
    git = fixture.EvidenceCLI.git
    remote = fixture.EvidenceCLI.remote
    expected_args = fixture.EvidenceCLI.expected_args
    images = fixture.EvidenceCLI.images
    prepare = fixture.EvidenceCLI.prepare
    publish = fixture.EvidenceCLI.publish

    def bundle_and_receipt(self):
        artifact = self.root / "acc_provision-6.1.1.8.dev1.tar.gz"
        artifact.write_bytes(b"one immutable package")
        bundle = self.root / "package"
        self.cli("prepare-package", "--artifact", artifact, "--bundle", bundle, "--mode", "z",
                 "--version", "6.1.1.8.dev1", "--registry", "https://test.pypi.org/project/acc-provision/6.1.1.8.dev1/#files", *self.common)
        manifest = json.loads((bundle / "manifest.json").read_text())
        receipt = {"schema_version": 1, "verified": True, "package": manifest["package_artifact"],
                   "index": "https://test.pypi.org", "source_sha": manifest["source"]["sha"],
                   "cicd_commit": manifest["cicd"]["sha"]}
        return bundle, receipt

    def publish_receipt(self, bundle, repo, receipt, check=False):
        path = self.root / "receipt.json"
        path.write_text(json.dumps(receipt))
        return self.cli("publish", "--bundle", bundle, "--repository", repo, "--branch", "main",
                        "--receipt", path, *self.expected_args(bundle), check=check)

    def test_wrong_receipt_cannot_write_status(self):
        bundle, receipt = self.bundle_and_receipt()
        repo, checkout = self.remote()
        original = self.git("--git-dir", str(repo), "rev-parse", "main")
        for key, value in (("name", "other-package"), ("version", "1.0"), ("index", "pypi"),
                           ("filename", "other.tar.gz"), ("sha256", "0" * 64), ("size", 0)):
            wrong = copy.deepcopy(receipt)
            wrong["package"][key] = value
            self.assertNotEqual(self.publish_receipt(bundle, repo, wrong).returncode, 0, key)
        for key, value in (("verified", False), ("index", "https://pypi.org"),
                           ("source_sha", "b" * 40), ("cicd_commit", "c" * 40)):
            wrong = copy.deepcopy(receipt)
            wrong[key] = value
            self.assertNotEqual(self.publish_receipt(bundle, repo, wrong).returncode, 0, key)
        result = self.cli("publish", "--bundle", bundle, "--repository", repo, "--branch", "main", *self.expected_args(bundle), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(original, self.git("--git-dir", str(repo), "rev-parse", "main"))

    def test_verified_receipt_repairs_failed_status_idempotently_without_upload(self):
        bundle, receipt = self.bundle_and_receipt()
        repo, checkout = self.remote()
        hook = repo / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.assertNotEqual(self.publish_receipt(bundle, repo, receipt).returncode, 0)
        hook.unlink()
        self.publish_receipt(bundle, repo, receipt, check=True)
        head = self.git("--git-dir", str(repo), "rev-parse", "main")
        self.publish_receipt(bundle, repo, receipt, check=True)
        self.assertEqual(head, self.git("--git-dir", str(repo), "rev-parse", "main"))
        self.assertFalse((self.root / "tools.jsonl").exists(), "package status attempted an external publication/scanner command")

    def test_canonical_package_name_version_link_is_checked_at_both_boundaries(self):
        bundle, receipt = self.bundle_and_receipt()
        repo, checkout = self.remote()
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for value in ("https://test.pypi.org/project/unrelated/999/#files",
                      "https://test.pypi.org:443/project/acc-provision/6.1.1.8.dev1/#files",
                      "https://test.pypi.org/project/acc-provision/6.1.1.8.dev1/?other=1#files"):
            manifest["package"][0]["link"] = value
            manifest_path.write_text(json.dumps(manifest))
            self.assertNotEqual(self.publish_receipt(bundle, repo, receipt).returncode, 0)
            artifact = self.root / "acc_provision-6.1.1.8.dev1.tar.gz"
            result = self.cli("prepare-package", "--artifact", artifact, "--bundle", self.root / "bad",
                              "--mode", "z", "--version", "6.1.1.8.dev1", "--registry", value, *self.common, check=False)
            self.assertNotEqual(result.returncode, 0)

    def test_production_package_requires_exact_expected_image_cohort(self):
        images = self.prepare(self.images())
        repo, checkout = self.remote()
        self.publish(images, repo)
        self.git("pull", "--ff-only", cwd=checkout)
        artifact = self.root / "package.tar.gz"
        artifact.write_bytes(b"retained package")
        bundle = self.root / "release"
        self.cli("prepare-package", "--artifact", artifact, "--bundle", bundle, "--mode", "release",
                 "--version", "6.1.1.8", "--registry", "https://pypi.org/project/acc-provision/6.1.1.8/#files",
                 "--status-checkout", checkout, *self.common)
        manifest = json.loads((bundle / "manifest.json").read_text())
        receipt = {"schema_version": 1, "verified": True, "package": manifest["package_artifact"],
                   "index": "https://pypi.org", "source_sha": manifest["source"]["sha"], "cicd_commit": manifest["cicd"]["sha"],
                   "tag": manifest["run"]["trigger_tag"], "tag_object": "d" * 40, "input_lock_sha256": "c" * 64}
        path = self.root / "production-receipt.json"
        path.write_text(json.dumps(receipt))
        expected = self.expected_args(bundle)
        expected[expected.index("--expected-images") + 1] += ",missing-runtime-image"
        result = self.cli("publish", "--bundle", bundle, "--repository", repo, "--branch", "main", "--receipt", path, *expected, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("complete promoted image cohort", result.stderr)
