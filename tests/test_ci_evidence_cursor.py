import hashlib
import json
import unittest

import yaml
import test_ci_evidence as fixture


class CursorRegressions(unittest.TestCase):
    setUp = fixture.EvidenceCLI.setUp
    command = fixture.EvidenceCLI.command
    cli = fixture.EvidenceCLI.cli
    git = fixture.EvidenceCLI.git
    remote = fixture.EvidenceCLI.remote
    images = fixture.EvidenceCLI.images
    prepare = fixture.EvidenceCLI.prepare
    publish = fixture.EvidenceCLI.publish
    expected_args = fixture.EvidenceCLI.expected_args

    def raw_publish(self, bundle, repo, expected, check=False):
        return self.cli("publish", "--bundle", bundle, "--repository", repo, "--branch", "main", *expected, check=check)

    def test_trusted_scope_rejects_another_release_even_with_valid_hashes(self):
        bundle = self.prepare()
        trusted = self.expected_args(bundle)
        path = bundle / "manifest.json"
        path.write_text(path.read_text().replace("6.1.1.8", "6.1.1.7"))
        artifacts = bundle / "artifacts/docs/release_artifacts"
        (artifacts / "6.1.1.8").rename(artifacts / "6.1.1.7")
        for source in (artifacts / "6.1.1.7").rglob("6.1.1.8-*"):
            source.rename(source.with_name(source.name.replace("6.1.1.8", "6.1.1.7")))
        repo, checkout = self.remote()
        head = self.git("--git-dir", str(repo), "rev-parse", "main")
        result = self.raw_publish(bundle, repo, trusted)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted publisher scope", result.stderr)
        self.assertEqual(head, self.git("--git-dir", str(repo), "rev-parse", "main"))

    def test_run_history_injection_and_another_component_are_rejected(self):
        bundle = self.prepare()
        trusted = self.expected_args(bundle)
        path = bundle / "manifest.json"
        original = path.read_text()
        manifest = json.loads(original)
        manifest["run"]["id"] = "123\nImageId: sha256:" + "f" * 64
        path.write_text(json.dumps(manifest))
        repo, checkout = self.remote()
        self.assertNotEqual(self.raw_publish(bundle, repo, trusted).returncode, 0)
        duplicate = json.loads(original)
        duplicate["images"].append(dict(duplicate["images"][0]))
        path.write_text(json.dumps(duplicate))
        self.assertNotEqual(self.raw_publish(bundle, repo, trusted).returncode, 0)
        path.write_text(original.replace("image-0", "another-component"))
        image = bundle / "artifacts/docs/release_artifacts/6.1.1.8/z/image-0"
        image.rename(image.with_name("another-component"))
        self.assertNotEqual(self.raw_publish(bundle, repo, trusted).returncode, 0)

    def test_same_image_a_b_a_never_replaces_newer_evidence_or_refresh(self):
        first = self.prepare()
        repo, checkout = self.remote()
        self.publish(first, repo)
        self.git("pull", "--ff-only", cwd=checkout)
        path = checkout / "docs/release_artifacts/releases.yaml"
        status = yaml.safe_load(path.read_text())
        record = status["releases"][-1]["release_streams"][0]["container_images"][0]
        record.update(cve_error="Scanning Queued in Quay", severity_link="https://quay.io/old-digest", owner_notes="keep")
        path.write_text(yaml.safe_dump(status))
        self.git("add", ".", cwd=checkout)
        self.git("commit", "-m", "independent refresh", cwd=checkout)
        self.git("push", "origin", "main", cwd=checkout)
        refreshed = self.git("--git-dir", str(repo), "rev-parse", "main")
        self.publish(first, repo)
        self.assertEqual(refreshed, self.git("--git-dir", str(repo), "rev-parse", "main"))
        self.common[self.common.index("--run-id") + 1] = "124"
        self.common[self.common.index("--source-sha") + 1] = "b" * 40
        second = self.prepare(self.images(name="second"), "second-bundle")
        self.publish(second, repo)
        latest = self.git("--git-dir", str(repo), "rev-parse", "main")
        status = yaml.safe_load(self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/releases.yaml"))
        record = status["releases"][-1]["release_streams"][0]["container_images"][0]
        self.assertNotIn("cve_error", record)
        self.assertNotIn("severity_link", record)
        self.assertEqual(record["owner_notes"], "keep")
        self.assertEqual(record["commit"][0]["sha"], "b" * 40)
        self.publish(first, repo)
        self.assertEqual(latest, self.git("--git-dir", str(repo), "rev-parse", "main"))

    def test_quay_refreshed_base_counts_and_auxiliary_files_survive_promotion(self):
        first = self.prepare()
        repo, checkout = self.remote()
        self.publish(first, repo)
        self.git("pull", "--ff-only", cwd=checkout)
        path = checkout / "docs/release_artifacts/releases.yaml"
        status = yaml.safe_load(path.read_text())
        record = status["releases"][-1]["release_streams"][0]["container_images"][0]
        record["quay"][0]["sha"] = hashlib.sha256(b"quay.io/noiro/image-0:6.1.1.8.42").hexdigest()
        record["docker"][0]["sha"] = hashlib.sha256(b"noiro/image-0:6.1.1.8.42").hexdigest()
        record.update(severity=[dict.fromkeys("CHMLU", 0)], severity_type="quay", cve_error="queued", severity_link="old")
        base = record["base-image"][0]
        base.update(severity=[{"C": 2, "H": 7, "M": 0, "L": 0, "U": 0}], severity_type="quay")
        status["releases"][-1]["release_streams"][1]["container_images"] = [{"name": "retired-image"}]
        artifact_root = checkout / "docs/release_artifacts/6.1.1.8/z/image-0"
        (artifact_root / "6.1.1.8-cve-base.txt").write_text('{"quay_json":true}')
        (artifact_root / "6.1.1.8-quay-cve.txt").write_bytes(b"Quay evidence\r\n")
        (artifact_root / "6.1.1.8-base-image-sha.txt").write_text("a" * 64 + "\n")
        path.write_text(yaml.safe_dump(status))
        self.git("add", ".", cwd=checkout)
        self.git("commit", "-m", "refresh known base", cwd=checkout)
        self.git("push", "origin", "main", cwd=checkout)
        artifact = self.root / "package.tar.gz"
        artifact.write_bytes(b"retained package")
        bundle = self.root / "release"
        self.cli("prepare-package", "--artifact", artifact, "--bundle", bundle, "--mode", "release",
                 "--version", "6.1.1.8", "--registry", "https://pypi.org/project/acc-provision/6.1.1.8/#files", "--status-checkout", checkout, *self.common)
        manifest = json.loads((bundle / "manifest.json").read_text())
        promoted = manifest["records"][0]
        self.assertEqual(promoted["base-image"][0]["severity"][0]["H"], 7)
        self.assertEqual(promoted["base-image"][0]["severity_type"], "quay")
        self.assertEqual(promoted["severity"][0]["H"], 1)
        self.assertEqual(promoted["severity_type"], "grype")
        self.assertNotIn("cve_error", promoted)
        self.assertNotIn("severity_link", promoted)
        self.assertNotIn("severity", promoted["base-image-original"][0], "unknown Quay JSON cannot become zero Grype findings")
        self.assertEqual((bundle / "artifacts/docs/release_artifacts/6.1.1.8/r/image-0/6.1.1.8-quay-cve.txt").read_bytes(), b"Quay evidence\r\n")
        self.publish(bundle, repo)
        published = yaml.safe_load(self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/releases.yaml"))
        release_stream = next(item for item in published["releases"][-1]["release_streams"] if item["release_name"] == "6.1.1.8")
        self.assertEqual([item["name"] for item in release_stream["container_images"]], ["image-0"])

    def test_historical_binary_logs_keep_exact_bytes_and_package_stays_out_of_git(self):
        metadata = self.images()
        row = json.loads(metadata.read_text())
        binary = b"start\r\nprogress\r\xffend\n"
        (metadata.parent / "buildlog.txt").write_bytes(binary)
        row["reports"]["buildlog"] = "buildlog.txt"
        metadata.write_text(json.dumps(row))
        first = self.prepare(metadata)
        repo, checkout = self.remote()
        self.publish(first, repo)
        self.git("pull", "--ff-only", cwd=checkout)
        artifact = self.root / "package.tar.gz"
        artifact.write_bytes(b"retained immutable package")
        bundle = self.root / "release"
        self.cli("prepare-package", "--artifact", artifact, "--bundle", bundle, "--mode", "release",
                 "--version", "6.1.1.8", "--registry", "https://pypi.org/project/acc-provision/6.1.1.8/#files", "--status-checkout", checkout, *self.common)
        self.assertEqual((bundle / "artifacts/docs/release_artifacts/6.1.1.8/r/image-0/6.1.1.8-buildlog.txt").read_bytes(), binary)
        self.publish(bundle, repo)
        self.assertNotIn(".tar.gz", self.git("--git-dir", str(repo), "ls-tree", "-r", "--name-only", "main"))
        self.assertEqual((bundle / "retained/package.tar.gz").read_bytes(), artifact.read_bytes())
