"""Exercise the shared ACI wrapper with fake Docker and bounded copy I/O."""
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

import test_ci_evidence as evidence_fixture

ROOT = evidence_fixture.ROOT


class ACIApprovedInput(unittest.TestCase):
    def setUp(self):
        evidence_fixture.EvidenceCLI.setUp(self)
        self.release = re.search(r'^RELEASE_TAG="([^"]+)"', (ROOT / "travis/globals.sh").read_text(), re.M).group(1)
        self.image_tag = self.release + ".42"
        self.dated_tag = self.image_tag + ".111226.123"
        self.common[self.common.index("--release-tag") + 1] = self.release
        self.common[self.common.index("--trigger-tag") + 1] = self.release
    command = evidence_fixture.EvidenceCLI.command
    cli = evidence_fixture.EvidenceCLI.cli
    git = evidence_fixture.EvidenceCLI.git
    prepare = evidence_fixture.EvidenceCLI.prepare

    def project(self, *, prepared=True, expected=True, complete_copy=False, case=""):
        suffix = "-" + case if case else ""
        project = self.root / ("aci" + suffix)
        docker = project / "docker/travis"
        docker.mkdir(parents=True)
        (project / "dist-static").mkdir()
        for name in ("host", "controller", "cnideploy", "operator", "openvswitch", "webhook", "certmanager"):
            image = "ubi-minimal" if name in ("host", "openvswitch") else "ubi"
            (docker / ("Dockerfile-" + name)).write_text(
                f"ARG UBI=registry.access.redhat.com/ubi9/{image}:latest\nFROM ${{UBI}}\n")
        copy_script = project / "docker/copy_iptables.sh"
        copy_script.write_text("#!/usr/bin/env python3\nimport io,json,os,pathlib,sys,tarfile\n"
                              "pathlib.Path(os.environ['COPY_INPUT_LOG']).write_text(json.dumps(sys.argv[1:]))\n" +
                              ("destination=pathlib.Path(sys.argv[2])\n"
                               "for name in ('iptables-bin.tar.gz','iptables-libs.tar.gz'):\n"
                               " with tarfile.open(destination/name,'w:gz') as archive:\n"
                               "  member=tarfile.TarInfo('fixture'); member.size=7\n"
                               "  archive.addfile(member,io.BytesIO(b'fixture'))\n"
                               "(destination/'iptables-wrapper-installer.sh').write_text('#!/bin/sh\\nexit 0\\n')\n"
                               if complete_copy else "sys.exit(77)\n"))
        copy_script.chmod(0o755)
        self.git("init", str(project))
        self.git("add", ".", cwd=project)
        self.git("commit", "-m", "ACI fixture", cwd=project)
        artifacts = self.root / ("aci-artifacts" + suffix)
        env = dict(self.env, GITHUB_ACTIONS="true", GHA_TRIGGER_TAG=self.release, GHA_DATE_TAG="111226",
                   GITHUB_REPOSITORY="noironetworks/aci-containers", GITHUB_REF="refs/tags/" + self.release,
                   TRAVIS_TAG=self.release, UPSTREAM_ID="42", TRAVIS_BUILD_NUMBER="123",
                   CI_ARTIFACT_DIR=str(artifacts), COPY_INPUT_LOG=str(self.root / "copy-input.json"),
                   MAKE_INPUT_LOG=str(self.root / ("make-input" + suffix + ".json")),
                   QUAY_NOIRO_DOCKER_CONFIG=str(self.root / "quay"))
        env.pop("CI_SCAN_POLICY", None)
        env.pop("EXPECTED_OPFLEX_BASE_DIGEST", None)
        env.pop("CI_EVIDENCE_MODE", None)
        if prepared:
            env["CI_EVIDENCE_MODE"] = "prepared-v1"
        if expected:
            env["EXPECTED_OPFLEX_BASE_DIGEST"] = "sha256:" + "a" * 64
        if complete_copy:
            # This path reaches ordinary Docker build-job cleanup. Status/PyPI
            # credentials belong to separate jobs; scanner sentinels are covered
            # by the collector entrypoint tests.
            for key in ("TRAVIS_TAGGER", "GITHUB_TOKEN", "GH_TOKEN", "PYPI_PASS", "TEST_PYPI_PASS"):
                env.pop(key, None)
            docker_tool = self.bin / "docker"
            marker = 'else:\n    sys.exit("unexpected Docker fixture command: " + " ".join(sys.argv))'
            cleanup = 'elif sys.argv[1:3] in (["container", "prune"], ["image", "rm"]):\n    print("fixture cleanup")\n' + marker
            text = docker_tool.read_text()
            self.assertIn(marker, text)
            docker_tool.write_text(text.replace(marker, cleanup))
            make_tool = self.bin / "make"
            make_tool.write_text("#!/usr/bin/env python3\nimport json,os,pathlib,sys\n"
                                 "pathlib.Path(os.environ['MAKE_INPUT_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
                                 "sys.exit(77)\n")
            make_tool.chmod(0o755)
        return project, artifacts, env

    def wrapper(self, project, env):
        return self.command(["bash", str(ROOT / "travis/build-push-aci-containers-images.sh")],
                            cwd=project, env=env, check=False)

    def test_prepared_wrapper_copies_only_the_locked_input_when_z_is_different(self):
        project, artifacts, env = self.project()
        result = self.wrapper(project, env)
        self.assertEqual(result.returncode, 77, result.stderr)
        digest = env["EXPECTED_OPFLEX_BASE_DIGEST"]
        reference = "quay.io/noirolabs/opflex-build-base@" + digest
        self.assertEqual(json.loads((self.root / "copy-input.json").read_text()), [reference, "dist-static"])
        proof = json.loads((artifacts / "base-provenance/aci-iptables-base.json").read_text())
        self.assertEqual(proof["reference"], reference)
        self.assertEqual(proof["digest"], digest)
        self.assertEqual(proof["image_id"], "sha256:" + "e" * 64)
        self.assertEqual(proof["role"], "opflex-build-base")
        self.assertTrue(proof["immutable"])
        self.assertTrue(proof["captured_before_build"])
        mutable = "quay.io/noirolabs/opflex-build-base:" + self.image_tag + ".z"
        self.assertNotEqual("sha256:" + hashlib.sha256(mutable.encode()).hexdigest(), digest)
        calls = [json.loads(line) for line in (self.root / "tools.jsonl").read_text().splitlines()]
        self.assertFalse(any(call["tool"] == "docker" and mutable in call["args"] for call in calls))

    def test_legacy_wrapper_retains_tag_input_and_prepared_requires_digest(self):
        project, artifacts, env = self.project(prepared=False, expected=False)
        result = self.wrapper(project, env)
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assertEqual(json.loads((self.root / "copy-input.json").read_text())[0],
                         "quay.io/noirolabs/opflex-build-base:" + self.image_tag + ".z")
        self.assertFalse((artifacts / "base-provenance/aci-iptables-base.json").exists())
        (self.root / "copy-input.json").unlink()
        env["CI_EVIDENCE_MODE"] = "prepared-v1"
        result = self.wrapper(project, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "copy-input.json").exists())

    def test_actual_aci_producers_emit_and_deduplicate_the_verified_input(self):
        project, artifacts, env = self.project()
        self.assertEqual(self.wrapper(project, env).returncode, 77)
        for image in ("aci-containers-host", "aci-containers-controller"):
            self.command(["bash", str(ROOT / "travis/push-images.sh"), "quay.io/noirolabs", image,
                          self.image_tag, self.image_tag + "," + self.dated_tag,
                          "registry.access.redhat.com/ubi9/ubi:latest"], env=env)
        metadata = artifacts / "image-metadata.jsonl"
        rows = [json.loads(line) for line in metadata.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row["build_inputs"]) == 1 for row in rows))
        self.assertEqual(rows[0]["build_inputs"], rows[1]["build_inputs"])
        bundle = self.prepare(metadata)
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(len(manifest["build_inputs"]), 1)
        self.assertEqual(manifest["build_inputs"][0]["digest"], env["EXPECTED_OPFLEX_BASE_DIGEST"])
        self.assertTrue(any(item["path"].endswith("-build-input-provenance.txt") for item in manifest["files"]))

    def test_successful_extraction_cleans_the_same_prepared_or_legacy_input(self):
        for prepared in (True, False):
            with self.subTest(prepared=prepared):
                case = "prepared" if prepared else "legacy"
                project, artifacts, env = self.project(prepared=prepared, expected=prepared,
                                                       complete_copy=True, case=case)
                result = self.wrapper(project, env)
                self.assertEqual(result.returncode, 77, result.stderr)
                self.assertEqual(json.loads(Path(env["MAKE_INPUT_LOG"]).read_text()), ["-C", ".", "all-static"])
                expected = ("quay.io/noirolabs/opflex-build-base@" + env["EXPECTED_OPFLEX_BASE_DIGEST"]
                            if prepared else "quay.io/noirolabs/opflex-build-base:" + self.image_tag + ".z")
                calls = [json.loads(line) for line in (self.root / "tools.jsonl").read_text().splitlines()]
                removals = [call["args"] for call in calls if call["tool"] == "docker" and call["args"][:2] == ["image", "rm"]]
                self.assertEqual(removals[-1], ["image", "rm", expected])
                self.assertEqual(json.loads((self.root / "copy-input.json").read_text())[0], expected)
