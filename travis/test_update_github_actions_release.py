#!/usr/bin/env python3
"""Unit tests for the GitHub Actions legacy-stream status updater."""

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("update-github-actions-release.py")
SPEC = importlib.util.spec_from_file_location("github_actions_release_updater", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
UPDATER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = UPDATER
SPEC.loader.exec_module(UPDATER)

CICD_COMMIT = "b" * 40
RUN_ID = "12345"
RUN_ATTEMPT = "2"
RUN_NUMBER = "789"
TIMESTAMP = "2026-08-14T10:00:00Z"
Z_TAG = "6.1.1.7.81c2369.z"
DATED_TAG = f"6.1.1.7.81c2369.081426.{RUN_NUMBER}"


def digests(images, offset=1):
    return {
        name: f"sha256:{index:064x}"
        for index, name in enumerate(images, start=offset)
    }


def manifest_text(prefix, images, offset=1, tags=(Z_TAG, DATED_TAG)):
    rows = ["image\tdigest"]
    for index, name in enumerate(images, start=offset):
        digest = f"sha256:{index:064x}"
        for tag in tags:
            rows.append(f"{prefix}/{name}:{tag}\t{digest}")
    return "\n".join(rows) + "\n"


def old_entry(name, marker):
    return (
        "    - base-image: []\n"
        f"      build-logs: old-{marker}\n"
        f"      name: {name}\n"
        f"      stale-field: stale-{marker}\n"
    )


class GithubActionsReleaseUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.aci = UPDATER.COMPONENTS["aci-containers"]
        self.opflex = UPDATER.COMPONENTS["opflex"]
        self.aci_commit = "a" * 40
        self.opflex_commit = "c" * 40

    def make_entries(self, component_name, commit, offset=1, timestamp=TIMESTAMP):
        component = UPDATER.COMPONENTS[component_name]
        return UPDATER.render_image_entries(
            component,
            digests(component.images, offset),
            digests(component.images, offset + 20),
            Z_TAG,
            DATED_TAG,
            commit,
            RUN_ID,
            RUN_ATTEMPT,
            timestamp,
        )

    def new_releases_file(self, directory):
        releases = Path(directory) / "releases.yaml"
        entries = [old_entry("opflex", "opflex")]
        entries.extend(old_entry(name, name) for name in UPDATER.ACI_IMAGES)
        entries.append(old_entry("acc-provision-operator", "unrelated"))
        releases.write_text(
            "releases:\n"
            "- release_streams:\n"
            "  - acc_provision:\n"
            "    - tag: keep-acc-provision\n"
            "      custom: keep-this-too\n"
            "    container_images:\n"
            + "".join(entries)
            + "    last_updated: old-time\n"
            "    release_name: 6.1.1.7.z\n"
            "  - acc_provision: []\n"
            "    container_images: []\n"
            "    last_updated: old-release-time\n"
            "    release_name: 6.1.1.7\n"
            "    released: false\n"
            "  release_tag: 6.1.1.7\n"
            "- release_streams:\n"
            "  - release_name: unrelated.z\n"
            "  release_tag: unrelated\n",
            encoding="utf-8",
        )
        return releases

    def test_manifest_requires_z_and_dated_tags_for_every_image(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "images.tsv"
            manifest.write_text(
                manifest_text(UPDATER.QUAY_REGISTRY_PREFIX, self.opflex.images),
                encoding="utf-8",
            )
            result = UPDATER.read_publication_manifest(
                manifest,
                UPDATER.QUAY_REGISTRY_PREFIX,
                self.opflex.images,
                Z_TAG,
                DATED_TAG,
            )
            self.assertEqual(result, {"opflex": "sha256:" + "0" * 63 + "1"})

            manifest.write_text(
                manifest_text(
                    UPDATER.QUAY_REGISTRY_PREFIX,
                    self.opflex.images,
                    tags=(Z_TAG, "6.1.1.7.81c2369.gha.bad"),
                ),
                encoding="utf-8",
            )
            with self.assertRaises(UPDATER.UpdateError):
                UPDATER.read_publication_manifest(
                    manifest,
                    UPDATER.QUAY_REGISTRY_PREFIX,
                    self.opflex.images,
                    Z_TAG,
                    DATED_TAG,
                )

    def test_manifest_rejects_different_digests_for_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "images.tsv"
            manifest.write_text(
                "image\tdigest\n"
                f"quay.io/noiro/opflex:{Z_TAG}\tsha256:{1:064x}\n"
                f"quay.io/noiro/opflex:{DATED_TAG}\tsha256:{2:064x}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(UPDATER.UpdateError, "different digests"):
                UPDATER.read_publication_manifest(
                    manifest,
                    UPDATER.QUAY_REGISTRY_PREFIX,
                    self.opflex.images,
                    Z_TAG,
                    DATED_TAG,
                )

    def test_aci_replaces_only_aci_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            releases = self.new_releases_file(directory)
            before = releases.read_text(encoding="utf-8")
            self.assertTrue(
                UPDATER.merge_release(
                    releases,
                    self.aci,
                    self.make_entries("aci-containers", self.aci_commit),
                    TIMESTAMP,
                )
            )
            output = releases.read_text(encoding="utf-8")
            self.assertIn("tag: keep-acc-provision", output)
            self.assertIn("custom: keep-this-too", output)
            self.assertIn("stale-field: stale-opflex", output)
            self.assertIn("stale-field: stale-unrelated", output)
            self.assertNotIn("stale-field: stale-aci-containers-host", output)
            self.assertNotIn(".gha.", output)
            self.assertIn(f'tag: "{Z_TAG}"', output)
            self.assertIn(f'tag: "{DATED_TAG}"', output)
            self.assertIn("https://quay.io/noiro/aci-containers-host:", output)
            self.assertNotIn("quay.io/noirolabs", output)
            self.assertNotEqual(output, before)

    def test_opflex_replaces_only_opflex_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            releases = self.new_releases_file(directory)
            self.assertTrue(
                UPDATER.merge_release(
                    releases,
                    self.opflex,
                    self.make_entries("opflex", self.opflex_commit, offset=100),
                    TIMESTAMP,
                )
            )
            output = releases.read_text(encoding="utf-8")
            self.assertNotIn("stale-field: stale-opflex", output)
            self.assertIn("stale-field: stale-aci-containers-host", output)
            self.assertIn("stale-field: stale-unrelated", output)
            self.assertIn("tag: keep-acc-provision", output)

    def test_missing_component_image_rejects_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            releases = self.new_releases_file(directory)
            malformed = releases.read_text(encoding="utf-8").replace(
                "      name: opflex\n", "      name: some-other-image\n", 1
            )
            releases.write_text(malformed, encoding="utf-8")
            with self.assertRaisesRegex(UPDATER.UpdateError, "missing expected images"):
                UPDATER.merge_release(
                    releases,
                    self.opflex,
                    self.make_entries("opflex", self.opflex_commit),
                    TIMESTAMP,
                )
            self.assertEqual(releases.read_text(encoding="utf-8"), malformed)

    def test_build_identity_uses_legacy_tags_and_run_number(self):
        UPDATER.validate_build_identity(
            Z_TAG,
            DATED_TAG,
            self.aci_commit,
            CICD_COMMIT,
            RUN_ID,
            RUN_ATTEMPT,
            RUN_NUMBER,
        )
        with self.assertRaises(UPDATER.UpdateError):
            UPDATER.validate_build_identity(
                Z_TAG,
                "6.1.1.7.81c2369.gha.bad",
                self.aci_commit,
                CICD_COMMIT,
                RUN_ID,
                RUN_ATTEMPT,
                RUN_NUMBER,
            )
        with self.assertRaises(UPDATER.UpdateError):
            UPDATER.validate_build_identity(
                Z_TAG,
                DATED_TAG,
                self.aci_commit,
                CICD_COMMIT,
                RUN_ID,
                RUN_ATTEMPT,
                "999",
            )


if __name__ == "__main__":
    unittest.main()
