"""Run advisory collectors and publishers as real entrypoints, entirely offline."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
import yaml

import test_ci_evidence as evidence_fixture
ROOT = evidence_fixture.ROOT


class ScanCLI(unittest.TestCase):
    setUp = evidence_fixture.EvidenceCLI.setUp
    command = evidence_fixture.EvidenceCLI.command
    git = evidence_fixture.EvidenceCLI.git
    remote = evidence_fixture.EvidenceCLI.remote
    prepare = evidence_fixture.EvidenceCLI.prepare
    images = evidence_fixture.EvidenceCLI.images
    publish = evidence_fixture.EvidenceCLI.publish
    cli = evidence_fixture.EvidenceCLI.cli
    expected_args = evidence_fixture.EvidenceCLI.expected_args

    def collect(self, mode="ok", timeout="5", lock=None):
        output = self.root / ("scan-" + mode)
        env = dict(self.env, SCAN_FIXTURE_MODE=mode)
        self.command([sys.executable, str(ROOT / "travis/scan_result.py"), "collect",
                      "--image-ref", "quay.io/noiro/image-0:build", "--output", str(output),
                      "--lock", str(lock or self.env["SCAN_TOOL_LOCK"]), "--timeout", timeout], env=env)
        return json.loads((output / "scan.json").read_text()), output

    def push(self, *, mode="ok", failure=None, name="image-0", policy="advisory"):
        artifacts = self.root / ("push-" + mode + ("-failure" if failure else "") + "-" + name)
        env = dict(self.env, GITHUB_ACTIONS="true", CI_EVIDENCE_MODE="prepared-v1", GHA_DATE_TAG="111226", TRAVIS_TAG="6.1.1.8",
                   UPSTREAM_ID="42", TRAVIS_BUILD_NUMBER="123", CI_ARTIFACT_DIR=str(artifacts),
                   SCAN_FIXTURE_MODE=mode, QUAY_NOIRO_DOCKER_CONFIG=str(self.root / "quay"))
        if policy is not None:
            env["CI_SCAN_POLICY"] = policy
        else:
            env.pop("CI_SCAN_POLICY", None)
        if failure:
            env["FAIL_PUSH_MATCH"] = failure
        result = self.command(["bash", str(ROOT / "travis/push-images.sh"), "quay.io/noirolabs", name,
                               "6.1.1.8.42", "6.1.1.8.42,6.1.1.8.42.111226.123", "incorrect-postbuild-base:1"],
                              env=env, check=False)
        return result, artifacts

    def test_valid_zero_is_success_and_missing_base_is_unknown(self):
        data, output = self.collect("zero")
        self.assertEqual(data["image"]["state"], "ok")
        self.assertEqual(data["image"]["counts"], dict.fromkeys("CHMLU", 0))
        self.assertEqual(data["image"]["last_success"]["counts"], dict.fromkeys("CHMLU", 0))
        self.assertEqual(data["base"]["state"], "unavailable")
        self.assertNotIn("counts", data["base"])
        self.assertNotIn("last_success", data["base"])

    def test_empty_malformed_and_mismatched_outputs_never_become_zero(self):
        for mode in ("empty", "malformed", "digest-mismatch"):
            data, output = self.collect(mode)
            self.assertEqual(data["image"]["state"], "error", mode)
            self.assertNotIn("counts", data["image"])
            self.assertNotIn("last_success", data["image"])
            self.assertTrue((output / "cve.txt").exists())

    def test_timeout_is_advisory_with_no_current_counts(self):
        data, output = self.collect("timeout", ".1")
        self.assertEqual(data["image"]["reason"], "scanner-timeout")
        self.assertEqual(data["image"]["state"], "error")
        self.assertNotIn("counts", data["image"])

    def test_install_failure_and_unsupported_platform_are_explicit(self):
        original = json.loads(Path(self.env["SCAN_TOOL_LOCK"]).read_text())
        for mode, expected in (("missing", "unavailable"), ("platform", "unsupported")):
            lock = json.loads(json.dumps(original))
            if mode == "missing":
                for item in lock["tools"].values():
                    item["path"] = str(self.root / "uninstalled")
            else:
                lock["platform"] = "unsupported-test-platform"
            path = self.root / (mode + ".json")
            path.write_text(json.dumps(lock))
            data, output = self.collect(mode, lock=path)
            self.assertEqual(data["image"]["state"], expected)
            self.assertNotIn("counts", data["image"])
            self.assertEqual(data["sbom"]["state"], "unavailable")

    def test_hash_mismatched_local_tool_is_never_executed(self):
        lock = json.loads(Path(self.env["SCAN_TOOL_LOCK"]).read_text())
        for item in lock["tools"].values():
            item["sha256"] = "0" * 64
        path = self.root / "wrong-hash.json"
        path.write_text(json.dumps(lock))
        data, output = self.collect("bad-hash", lock=path)
        self.assertEqual(data["image"]["state"], "unavailable")
        tools = [json.loads(line)["tool"] for line in (self.root / "tools.jsonl").read_text().splitlines()]
        self.assertEqual(tools, ["docker"])

    def test_advisory_scan_error_cannot_block_image_publication_or_status(self):
        result, artifacts = self.push(mode="malformed")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads((artifacts / "image-metadata.jsonl").read_text())
        self.assertEqual(row["publication"]["state"], "complete")
        self.assertEqual(row["scan"]["image"]["state"], "error")
        bundle = self.prepare(artifacts / "image-metadata.jsonl")
        manifest = json.loads((bundle / "manifest.json").read_text())
        record = manifest["records"][0]
        self.assertNotIn("severity", record)
        self.assertNotIn("sha", record["base-image"][0], "postbuild base argument must never invent provenance")
        repo, checkout = self.remote()
        self.publish(bundle, repo)
        status = self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/releases.yaml")
        self.assertIn("scanner-install-or-report-invalid", status)

    def test_unmigrated_gha_path_retains_fatal_scanner_policy(self):
        artifacts = self.root / "legacy-artifacts"
        env = dict(self.env, GITHUB_ACTIONS="true", GHA_DATE_TAG="111226", TRAVIS_TAG="6.1.1.8",
                   UPSTREAM_ID="42", TRAVIS_BUILD_NUMBER="123", CI_ARTIFACT_DIR=str(artifacts),
                   GRYPE_BIN=str(self.bin / "grype"), SCAN_FIXTURE_MODE="exec-failure",
                   QUAY_NOIRO_DOCKER_CONFIG=str(self.root / "quay"))
        result = self.command(["bash", str(ROOT / "travis/push-images.sh"), "quay.io/noirolabs", "image-0",
                               "6.1.1.8.42", "6.1.1.8.42,6.1.1.8.42.111226.123", "base:1"], env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((artifacts / "image-metadata.jsonl").exists())
        log = [json.loads(line) for line in (self.root / "tools.jsonl").read_text().splitlines()]
        self.assertFalse(any(item["tool"] == "docker" and item["args"][0] == "push" for item in log))

    def test_prepared_default_fatal_keeps_errors_but_stops_before_push(self):
        result, artifacts = self.push(mode="malformed", policy=None)
        self.assertNotEqual(result.returncode, 0)
        receipt = json.loads((artifacts / "publication/image-0.json").read_text())
        self.assertEqual(receipt["critical_failure"], "fatal-scan-policy-failed")
        self.assertEqual(receipt["scan_policy"], "fatal")
        self.assertFalse(receipt["attempts"])
        bundle = self.prepare(artifacts / "image-metadata.jsonl")
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertFalse(manifest["records"])
        record = manifest["failed_records"][0]
        self.assertNotIn("scan", record)
        self.assertNotIn("scan-tools", record)
        self.assertFalse(record["quay"])

    def test_fatal_failed_run_retains_good_public_record_and_reports(self):
        metadata = self.images()
        first = self.prepare(metadata)
        repo, checkout = self.remote()
        self.publish(first, repo)
        prior = self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/releases.yaml")
        original_report = self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/6.1.1.8/z/image-0/6.1.1.8-cve.txt")
        result, artifacts = self.push(mode="malformed", policy=None)
        self.assertNotEqual(result.returncode, 0)
        self.common[self.common.index("--run-id") + 1] = "124"
        failed = self.prepare(artifacts / "image-metadata.jsonl", "failed-bundle")
        self.publish(failed, repo)
        self.assertEqual(prior, self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/releases.yaml"))
        self.assertEqual(original_report, self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/6.1.1.8/z/image-0/6.1.1.8-cve.txt"))
        paths = self.git("--git-dir", str(repo), "ls-tree", "-r", "--name-only", "main")
        self.assertIn("/failures/124-1/6.1.1.8-publication.json", paths)

    def test_unknown_prepared_scan_policy_is_rejected_before_tools(self):
        result, artifacts = self.push(policy="unexpected")
        self.assertNotEqual(result.returncode, 0)
        receipt = json.loads((artifacts / "publication/image-0.json").read_text())
        self.assertEqual(receipt["critical_failure"], "unknown-scan-policy")
        self.assertFalse((self.root / "tools.jsonl").exists())

    def test_critical_push_failure_keeps_partial_receipt_and_never_moves_alias(self):
        failed = "docker.io/noiro/image-0:6.1.1.8.42.111226.123"
        result, artifacts = self.push(failure=failed)
        self.assertNotEqual(result.returncode, 0)
        receipt = json.loads((artifacts / "publication/image-0.json").read_text())
        self.assertEqual(receipt["state"], "partial")
        self.assertEqual(receipt["critical_failure"], "image-publication-failed")
        self.assertTrue(any(item["state"] == "error" for item in receipt["attempts"]))
        log = [json.loads(line) for line in (self.root / "tools.jsonl").read_text().splitlines()]
        pushes = [item["args"][-1] for item in log if item["args"][0] == "push"]
        self.assertFalse(any(reference.endswith(".z") for reference in pushes))
        bundle = self.prepare(artifacts / "image-metadata.jsonl")
        manifest = json.loads((bundle / "manifest.json").read_text())
        record = manifest["records"][0]
        self.assertEqual(record["publication"]["state"], "partial")
        self.assertFalse(record["docker"])
        self.assertTrue(record["quay"])
        repo, checkout = self.remote()
        self.publish(bundle, repo)

    def test_all_immutable_destinations_verify_before_alias_pushes(self):
        result, artifacts = self.push()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((artifacts / "publication/image-0.json").read_text())
        attempts = receipt["attempts"]
        aliases = [index for index, item in enumerate(attempts) if item["phase"] == "push" and item["reference"].endswith(".z")]
        immutable = [index for index, item in enumerate(attempts) if item["phase"] == "verify-image-id" and not item["reference"].endswith(".z")]
        self.assertLess(max(immutable), min(aliases))
        self.assertTrue(all(item["image_id"] == receipt["image_id"] for item in receipt["references"]))

    def test_opflex_build_preserves_noirolabs_alias_without_other_registries(self):
        result, artifacts = self.push(name="opflex-build")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((artifacts / "publication/opflex-build.json").read_text())
        pushes = [item["reference"] for item in receipt["attempts"] if item["phase"] == "push"]
        self.assertTrue(pushes[-1].endswith(".z"))
        self.assertTrue(all(reference.startswith("quay.io/noirolabs/") for reference in pushes))
        self.assertEqual(receipt["state"], "complete")

    def test_prebuild_capture_records_actual_host_minimal_base(self):
        dockerfile = self.root / "Dockerfile-host"
        dockerfile.write_text("FROM registry.access.redhat.com/ubi9/ubi-minimal:latest as base\nFROM base as with-ovscni\n")
        output = self.root / "base.json"
        self.command([sys.executable, str(ROOT / "travis/scan_result.py"), "capture-base",
                      "--dockerfile", str(dockerfile), "--output", str(output)])
        captured = json.loads(output.read_text())
        self.assertEqual(captured["state"], "ok")
        self.assertTrue(captured["captured_before_build"])
        self.assertEqual(captured["reference"], "registry.access.redhat.com/ubi9/ubi-minimal:latest")

    def test_fresh_arg_base_is_pulled_once_and_default_fatal_can_publish(self):
        artifacts = self.root / "push-ok-image-0"
        dockerfile = self.root / "Dockerfile-host"
        reference = "registry.access.redhat.com/ubi9/ubi-minimal:latest"
        dockerfile.write_text("ARG UBI=" + reference + "\nFROM ${UBI} as base\n")
        pulled = self.root / "pulled.json"
        env = dict(self.env, REQUIRE_PULL_REF=reference, PULLED_REFS_FILE=str(pulled))
        for image in ("image-0", "image-1"):
            self.command([sys.executable, str(ROOT / "travis/scan_result.py"), "capture-base",
                          "--dockerfile", str(dockerfile), "--output", str(artifacts / "base-provenance" / (image + ".json")),
                          "--pull-once-file", str(artifacts / "base-provenance/.pulled.json")], env=env)
        self.assertEqual(json.loads(pulled.read_text()), [reference])
        result, artifacts = self.push(policy=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads((artifacts / "image-metadata.jsonl").read_text())
        self.assertEqual(metadata["scan"]["base"]["state"], "ok")
        self.assertEqual(metadata["publication"]["state"], "complete")

    def test_explicit_immutable_opflex_build_arg_and_expected_digest_match(self):
        digest = "sha256:" + "a" * 64
        reference = "quay.io/noirolabs/opflex-build-base@" + digest
        dockerfile = self.root / "Dockerfile-opflex-build"
        dockerfile.write_text("ARG DOCKER_TAG\nARG DOCKER_HUB_ID=quay.io/noirolabs\nARG OPFLEX_BUILD_BASE_IMAGE\nFROM ${OPFLEX_BUILD_BASE_IMAGE}\n")
        output = self.root / "opflex-build.json"
        self.command([sys.executable, str(ROOT / "travis/scan_result.py"), "capture-base", "--dockerfile", str(dockerfile),
                      "--build-arg", "DOCKER_TAG=6.1.1.8.42", "--build-arg", "OPFLEX_BUILD_BASE_IMAGE=" + reference,
                      "--pull", "--expected-digest", digest, "--output", str(output)])
        captured = json.loads(output.read_text())
        self.assertEqual(captured["reference"], reference)
        self.assertEqual(captured["digest"], digest)
        self.assertTrue(captured["captured_before_build"])

    def test_actual_opflex_producer_emits_bound_child_build_input(self):
        artifacts = self.root / "push-ok-opflex"
        digest = "sha256:" + "a" * 64
        reference = "quay.io/noirolabs/opflex-build-base@" + digest
        dockerfile = self.root / "Dockerfile-opflex-build"
        dockerfile.write_text("ARG OPFLEX_BUILD_BASE_IMAGE\nFROM ${OPFLEX_BUILD_BASE_IMAGE}\n")
        self.command([sys.executable, str(ROOT / "travis/scan_result.py"), "capture-base", "--dockerfile", str(dockerfile),
                      "--build-arg", "OPFLEX_BUILD_BASE_IMAGE=" + reference, "--pull", "--expected-digest", digest,
                      "--output", str(artifacts / "base-provenance/opflex-build.json")])
        # Fixture of the child builder's already-verified pre-build output.
        (artifacts / "opflex-build-base-input.txt").write_text(
            "opflex_build_base_ref=" + reference + "\nopflex_build_base_digest=" + digest + "\n")
        result, artifacts = self.push(name="opflex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.common[self.common.index("--source-repository") + 1] = "noironetworks/opflex"
        bundle = self.prepare(artifacts / "image-metadata.jsonl")
        manifest = json.loads((bundle / "manifest.json").read_text())
        proof = manifest["build_inputs"][0]
        self.assertEqual((proof["reference"], proof["digest"]), (reference, digest))
        self.assertTrue(proof["immutable"])
        self.assertTrue(proof["captured_before_build"])
        self.assertTrue(any(item["path"].endswith("-build-input.txt") for item in manifest["files"]))

    def test_failed_current_scan_retains_success_and_refresh_in_distinct_fields(self):
        result, first = self.push()
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = self.prepare(first / "image-metadata.jsonl")
        repo, checkout = self.remote()
        self.publish(bundle, repo)
        self.git("pull", "--ff-only", cwd=checkout)
        status_path = checkout / "docs/release_artifacts/releases.yaml"
        status = yaml.safe_load(status_path.read_text())
        record = status["releases"][-1]["release_streams"][0]["container_images"][0]
        record["custom-maintainer-field"] = "retain"
        record["scan"]["refresh"] = {"image": {"state": "queued", "reason": "scheduled-refresh"}}
        status_path.write_text(yaml.safe_dump(status))
        self.git("add", ".", cwd=checkout)
        self.git("commit", "-m", "independent refresh writer", cwd=checkout)
        self.git("push", "origin", "main", cwd=checkout)
        result, failed = self.push(mode="malformed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.common[self.common.index("--run-id") + 1] = "124"
        second = self.prepare(failed / "image-metadata.jsonl", "second-bundle")
        self.publish(second, repo)
        data = yaml.safe_load(self.git("--git-dir", str(repo), "show", "main:docs/release_artifacts/releases.yaml"))
        current = data["releases"][-1]["release_streams"][0]["container_images"][0]
        self.assertEqual(current["custom-maintainer-field"], "retain")
        self.assertEqual(current["scan"]["refresh"]["image"]["state"], "queued")
        scan = current["scan"]["build"]["image"]
        self.assertEqual(scan["state"], "error")
        self.assertNotIn("counts", scan)
        self.assertNotIn("severity", current)
        self.assertEqual(scan["last_success"]["counts"]["H"], 1)
        self.assertIn("/scans/123-1/", scan["last_success"]["report"])
        self.assertIn("/scans/124-1/", scan["report"])
        original = self.git("--git-dir", str(repo), "show", "main:docs/" + scan["last_success"]["report"])
        self.assertIn('"state": "ok"', original)


if __name__ == "__main__":
    unittest.main()
