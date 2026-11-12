"""Exercise the real CLI with offline executables and local bare Git remotes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "travis/ci_evidence.py"
FAKE = Path(__file__).parent / "fixtures/fake_evidence_tool.py"


class EvidenceCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test-evidence-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in ("docker", "trivy", "grype"):
            shutil.copyfile(FAKE, self.bin / tool)
            (self.bin / tool).chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        TOOL_LOG=str(self.root / "tools.jsonl"),
                        TRAVIS_TAGGER="STATUS_CREDENTIAL_SENTINEL", GH_TOKEN="STATUS_CREDENTIAL_SENTINEL",
                        GITHUB_TOKEN="STATUS_CREDENTIAL_SENTINEL", PYPI_PASS="STATUS_CREDENTIAL_SENTINEL",
                        TEST_PYPI_PASS="STATUS_CREDENTIAL_SENTINEL")
        self.common = ["--release-tag", "6.1.1.8", "--source-repository", "noironetworks/aci-containers",
                       "--source-sha", "a" * 40, "--run-id", "123", "--run-url", "https://example.test/run/123",
                       "--trigger-tag", "6.1.1.8", "--upstream-id", "42"]

    def command(self, args, *, env=None, check=True, cwd=None):
        result = subprocess.run(args, env=env or self.env, cwd=cwd, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def cli(self, *args, **kw):
        return self.command([sys.executable, str(ENTRY), *map(str, args)], **kw)

    def git(self, *args, cwd=None):
        return self.command(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", *args], cwd=cwd).stdout.strip()

    def images(self, count=1, name="input"):
        directory = self.root / name
        directory.mkdir()
        rows = []
        for index in range(count):
            image = "image-" + str(index)
            reports = {}
            for kind in ("sbom", "cve", "cve-base"):
                filename = image + "-" + kind + ".txt"
                (directory / filename).write_text(f"{image} {kind}\nNAME VERSION SEVERITY\npackage 1 High\n\n")
                reports[kind] = filename
            rows.append({"registry": "quay.io/noiro", "image": image, "tag": "6.1.1.8.42",
                         "other_tags": ["6.1.1.8.42", "6.1.1.8.42.111226.123"],
                         "sha": "sha256:" + "e" * 64, "base_image": "ubuntu:24.04", "reports": reports})
        metadata = directory / "metadata.jsonl"
        metadata.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return metadata

    def prepare(self, metadata=None, bundle_name="bundle"):
        metadata = metadata or self.images()
        bundle = self.root / bundle_name
        self.cli("prepare-images", "--metadata", metadata, "--bundle", bundle, *self.common)
        return bundle

    def remote(self):
        repo, checkout = self.root / "remote.git", self.root / "seed"
        self.git("init", "--bare", "--initial-branch=main", str(repo))
        self.git("clone", str(repo), str(checkout))
        (checkout / "docs/release_artifacts").mkdir(parents=True)
        (checkout / "docs/release_artifacts/releases.yaml").write_text(yaml.safe_dump({
            "custom-root": "keep", "releases": [{"release_tag": "older", "release_streams": []}]}))
        self.git("add", ".", cwd=checkout)
        self.git("commit", "-m", "initial", cwd=checkout)
        self.git("push", "origin", "main", cwd=checkout)
        return repo, checkout

    def publish(self, bundle, repo, **kw):
        return self.cli("publish", "--bundle", bundle, "--repository", repo, "--branch", "main", **kw)

    def test_eight_image_reports_digests_and_status_noop(self):
        bundle = self.prepare(self.images(8))
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(len(manifest["records"]), 8)
        self.assertEqual(len(manifest["observations"]), 40)
        self.assertEqual(len(manifest["files"]), 32)
        for index, record in enumerate(manifest["records"]):
            content = (bundle / "artifacts/docs" / record["cve"]).read_text()
            self.assertIn(f"image-{index} cve", content)
            self.assertIn(f"Trivy evidence for quay.io/noiro/image-{index}", content)
            self.assertEqual(record["severity"][0]["H"], 1)
            self.assertEqual(record["quay"][0]["sha"], hashlib.sha256(
                f"quay.io/noiro/image-{index}:6.1.1.8.42.z".encode()).hexdigest())
        before = (self.root / "tools.jsonl").read_text()
        repo, checkout = self.remote()
        self.publish(bundle, repo)
        head = self.git("--git-dir", str(repo), "rev-parse", "main")
        self.publish(bundle, repo)
        self.assertEqual(head, self.git("--git-dir", str(repo), "rev-parse", "main"))
        self.assertEqual(before, (self.root / "tools.jsonl").read_text(), "publisher executed a scanner or Docker")
        data = yaml.safe_load(self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/releases.yaml"))
        self.assertEqual(data["custom-root"], "keep")
        self.assertEqual(len(data["releases"]), 2)
        self.assertEqual(len(data["releases"][-1]["release_streams"][0]["container_images"]), 8)
        self.assertIn("ImageId: sha256:" + "e" * 64, self.git("--git-dir", str(repo), "log", "-1", "--format=%B"))

    def test_scanner_error_is_fatal_and_not_a_bundle(self):
        env = dict(self.env, FAIL_TRIVY="1")
        result = self.cli("prepare-images", "--metadata", self.images(), "--bundle", self.root / "bad", *self.common, env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "bad").exists())

    def test_unsafe_report_paths_fail_before_scanning(self):
        metadata = self.images()
        row = json.loads(metadata.read_text())
        for bad in ("../outside", "/tmp/absolute", "reports/../../outside"):
            row["reports"]["cve"] = bad
            metadata.write_text(json.dumps(row))
            result = self.cli("prepare-images", "--metadata", metadata, "--bundle", self.root / "bad", *self.common, check=False)
            self.assertNotEqual(result.returncode, 0)
        (metadata.parent / "link.txt").symlink_to(metadata.parent / "image-0-cve.txt")
        row["reports"]["cve"] = "link.txt"
        metadata.write_text(json.dumps(row))
        self.assertNotEqual(self.cli("prepare-images", "--metadata", metadata, "--bundle", self.root / "bad", *self.common, check=False).returncode, 0)
        self.assertFalse((self.root / "tools.jsonl").exists())

    def test_corruption_and_path_escape_cannot_write_git(self):
        bundle = self.prepare()
        repo, checkout = self.remote()
        before = self.git("--git-dir", str(repo), "rev-parse", "main")
        manifest = json.loads((bundle / "manifest.json").read_text())
        artifact = bundle / "artifacts" / manifest["files"][0]["path"]
        artifact.write_text("corrupted")
        self.assertNotEqual(self.publish(bundle, repo, check=False).returncode, 0)
        self.assertEqual(before, self.git("--git-dir", str(repo), "rev-parse", "main"))
        manifest["files"][0]["path"] = "../outside"
        (bundle / "manifest.json").write_text(json.dumps(manifest))
        self.assertNotEqual(self.publish(bundle, repo, check=False).returncode, 0)
        self.assertEqual(before, self.git("--git-dir", str(repo), "rev-parse", "main"))

    def test_package_z_real_artifact_and_run_log(self):
        artifact = self.root / "acc_provision-6.1.1.8.tar.gz"
        artifact.write_bytes(b"prepared-package-bytes")
        bundle = self.root / "package"
        self.cli("prepare-package", "--artifact", artifact, "--bundle", bundle,
                 "--mode", "z", "--version", "6.1.1.8.dev1", "--registry", "https://example.test/pypi", *self.common)
        repo, checkout = self.remote()
        self.publish(bundle, repo)
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(manifest["kind"], "package")
        self.assertEqual(manifest["package"][0]["tag"], "6.1.1.8.dev1")
        self.assertFalse((self.root / "tools.jsonl").exists())

    def test_remote_write_failure_is_visible(self):
        bundle = self.prepare()
        repo, checkout = self.remote()
        hook = repo / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        before = self.git("--git-dir", str(repo), "rev-parse", "main")
        result = self.publish(bundle, repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.git("--git-dir", str(repo), "rev-parse", "main"))

    def test_publisher_rejects_exotic_transports_before_git(self):
        bundle = self.prepare()
        wrapper = self.bin / "git"
        marker = self.root / "git-was-run"
        wrapper.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\nexit 99\n")
        wrapper.chmod(0o755)
        for repository in ("ext::sh -c touch /tmp/never", "ssh://git@github.com/noironetworks/cicd-status.git",
                           "git@github.com:noironetworks/cicd-status.git", "git://github.com/noironetworks/cicd-status.git",
                           "https://token@github.com/noironetworks/cicd-status.git", "https://example.test/repo.git"):
            self.assertNotEqual(self.publish(bundle, repository, check=False).returncode, 0)
        self.assertFalse(marker.exists())

    def test_real_push_entrypoint_snapshots_before_next_image(self):
        artifacts = self.root / "artifacts"
        env = dict(self.env, GITHUB_ACTIONS="true", GHA_DATE_TAG="111226", TRAVIS_TAG="6.1.1.8",
                   UPSTREAM_ID="42", TRAVIS_BUILD_NUMBER="123", CI_ARTIFACT_DIR=str(artifacts),
                   GRYPE_BIN=str(self.bin / "grype"), QUAY_NOIRO_DOCKER_CONFIG=str(self.root / "quay"))
        for index in range(8):
            self.command(["bash", str(ROOT / "travis/push-images.sh"), "quay.io/noirolabs",
                          f"image-{index}", "6.1.1.8.42", "6.1.1.8.42,6.1.1.8.42.111226.123",
                          f"base-{index}:1"], env=env)
        metadata = artifacts / "image-metadata.jsonl"
        rows = [json.loads(line) for line in metadata.read_text().splitlines()]
        self.assertEqual(len(rows), 8)
        for index, row in enumerate(rows):
            for kind, path in row["reports"].items():
                text = (artifacts / path).read_text()
                expected = f"base-{index}:1" if kind == "cve-base" else f"image-{index}"
                self.assertIn(expected, text)
        self.prepare(metadata)

    def test_two_concurrent_writers_retry_fresh_clone_without_rescan(self):
        first = self.prepare(self.images(name="first"), "bundle-first")
        metadata = self.images(name="second")
        row = json.loads(metadata.read_text())
        row["image"] = "second-image"
        metadata.write_text(json.dumps(row))
        second = self.prepare(metadata, "bundle-second")
        repo, checkout = self.remote()
        gate = self.root / "gate"
        gate.mkdir()
        real_git = shutil.which("git")
        wrapper = self.bin / "git"
        wrapper.write_text("#!/usr/bin/env python3\nimport os,pathlib,subprocess,sys,time\n"
                           "gate=pathlib.Path(os.environ['CONFLICT_GATE'])\n"
                           "if 'push' in sys.argv and not (gate/'done').exists():\n"
                           " try:\n  (gate/'leader').mkdir()\n  leader=True\n"
                           " except FileExistsError:\n  leader=False\n  (gate/'follower').touch()\n"
                           " deadline=time.monotonic()+15\n"
                           " while not (gate/('follower' if leader else 'done')).exists():\n"
                           "  if time.monotonic()>deadline: sys.exit(99)\n  time.sleep(.02)\n"
                           f" result=subprocess.run([{real_git!r},*sys.argv[1:]])\n"
                           " if leader: (gate/'done').touch()\n sys.exit(result.returncode)\n"
                           f"os.execv({real_git!r},[{real_git!r},*sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        env = dict(self.env, CONFLICT_GATE=str(gate))
        before = (self.root / "tools.jsonl").read_text()
        processes = [subprocess.Popen([sys.executable, str(ENTRY), "publish", "--bundle", str(bundle),
                                       "--repository", str(repo), "--branch", "main"], env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for bundle in (first, second)]
        results = [process.communicate(timeout=30) for process in processes]
        for process, (out, error) in zip(processes, results):
            self.assertEqual(process.returncode, 0, out + error)
        self.assertTrue(any("reapplying" in error for _, error in results), results)
        self.assertEqual(before, (self.root / "tools.jsonl").read_text())
        # Avoid fixture gate for verification commands.
        wrapper.unlink()
        data = yaml.safe_load(self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/releases.yaml"))
        records = data["releases"][-1]["release_streams"][0]["container_images"]
        self.assertEqual({record["name"] for record in records}, {"image-0", "second-image"})
        self.assertEqual(self.git("--git-dir", str(repo), "rev-list", "--count", "main"), "3")

    def test_release_package_recovers_historical_image_reports(self):
        first = self.prepare()
        repo, checkout = self.remote()
        self.publish(first, repo)
        metadata = self.images(name="replacement")
        row = json.loads(metadata.read_text())
        row["sha"] = "sha256:" + "f" * 64
        metadata.write_text(json.dumps(row))
        (metadata.parent / "image-0-cve.txt").write_text("replacement evidence\n")
        second = self.prepare(metadata, "replacement-bundle")
        self.publish(second, repo)
        self.git("pull", "--ff-only", cwd=checkout)
        artifact = self.root / "acc_provision-6.1.1.8.tar.gz"
        artifact.write_bytes(b"package")
        bundle = self.root / "release"
        self.cli("prepare-package", "--artifact", artifact, "--bundle", bundle, "--mode", "release",
                 "--version", "6.1.1.8", "--registry", "https://example.test/pypi",
                 "--status-checkout", checkout, *self.common)
        manifest = json.loads((bundle / "manifest.json").read_text())
        record = manifest["records"][0]
        self.assertIn("image-0 cve", (bundle / "artifacts/docs" / record["cve"]).read_text())
        self.assertNotIn("replacement evidence", (bundle / "artifacts/docs" / record["cve"]).read_text())
        before = (self.root / "tools.jsonl").read_text()
        self.publish(bundle, repo)
        self.assertEqual(before, (self.root / "tools.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
