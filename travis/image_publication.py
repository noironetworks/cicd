#!/usr/bin/env python3
"""Publish GHA images with durable per-attempt receipts and aliases last."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from scan_result import collect, environment, execute, stamp, DIGEST


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def publish(args):
    directory = Path(args.artifacts)
    policy = os.environ.get("CI_SCAN_POLICY", "fatal")
    if policy not in ("fatal", "advisory"):
        write(directory / "publication" / (args.image + ".json"), {
            "schema_version": 1, "image": args.image, "state": "failed", "attempts": [],
            "critical_failure": "unknown-scan-policy", "scan_policy": policy, "finished_at": stamp()})
        return 1
    report_dir = directory / "reports" / args.image
    base = directory / "base-provenance" / (args.image + ".json")
    scan_data = collect(args.registry + "/" + args.image + ":" + args.tag,
                        base if base.exists() else None, report_dir,
                        lock=os.environ.get("SCAN_TOOL_LOCK"), timeout=float(os.environ.get("SCAN_TIMEOUT_SECONDS", "600")))
    build_inputs, build_input_error = [], None
    if args.image == "opflex":
        try:
            captured = json.loads((directory / "base-provenance/opflex-build.json").read_text())
            values = dict(line.split("=", 1) for line in (directory / "opflex-build-base-input.txt").read_text().splitlines() if line)
            reference, expected = values["opflex_build_base_ref"], values["opflex_build_base_digest"]
            if (captured.get("state") != "ok" or captured.get("captured_before_build") is not True
                    or captured.get("reference") != reference or captured.get("digest") != expected
                    or not DIGEST.fullmatch(expected)):
                raise ValueError("child input and prebuild capture disagree")
            build_inputs.append({"role": "opflex-build-base", "reference": reference, "digest": expected,
                                 "image_id": captured["image_id"], "captured_before_build": True,
                                 "immutable": reference.endswith("@" + expected)})
        except (OSError, ValueError, KeyError, TypeError):
            build_input_error = "actual-build-base-input-not-bound"
    receipt = {"schema_version": 1, "image": args.image, "image_id": scan_data.get("image_id"),
               "state": "in-progress", "attempts": [], "references": [], "alias_policy": "aliases-last",
               "started_at": stamp(), "critical_failure": None, "scan_policy": policy}
    receipt_path = directory / "publication" / (args.image + ".json")
    write(receipt_path, receipt)
    built = f"{args.registry}/{args.image}:{args.tag}"
    expected_id = scan_data.get("image_id")
    other_tags = list(dict.fromkeys(tag for tag in re.split(r"[, ]+", args.other_tags) if tag and tag != args.z_tag))

    def command(argv, phase, reference, config=None):
        item = {"phase": phase, "reference": reference, "command": argv[:2], "state": "started", "started_at": stamp()}
        receipt["attempts"].append(item)
        write(receipt_path, receipt)
        env = environment()
        if config:
            env["DOCKER_CONFIG"] = config
        try:
            output = subprocess.run(argv, env=env, check=True, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=args.timeout).stdout
            item.update(state="ok", finished_at=stamp())
            return output
        except (OSError, subprocess.SubprocessError) as exc:
            item.update(state="error", finished_at=stamp(), reason="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "command-failed")
            raise RuntimeError("critical Docker publication operation failed") from exc
        finally:
            write(receipt_path, receipt)

    def push(reference, registry, config=None, tag_from=True, verify=True):
        if tag_from:
            command(["docker", "tag", built, reference], "tag", reference, config)
        command(["docker", "push", reference], "push", reference, config)
        if verify:
            command(["docker", "pull", reference], "verify-pull", reference, config)
            raw = command(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", reference], "verify-digest", reference, config)
            entries = json.loads(raw)
            repository = reference.rsplit(":", 1)[0].removeprefix("docker.io/")
            digest = next(value.split("@", 1)[1] for value in entries
                          if value.split("@", 1)[0].removeprefix("docker.io/") == repository)
            observed_id = command(["docker", "image", "inspect", "--format", "{{.Id}}", reference], "verify-image-id", reference, config).strip()
            if not DIGEST.fullmatch(digest) or observed_id != expected_id:
                receipt["attempts"][-1].update(state="error", reason="published-image-identity-mismatch",
                                                expected_image_id=expected_id, observed_image_id=observed_id,
                                                observed_digest=digest)
                write(receipt_path, receipt)
                raise RuntimeError("published image identity verification failed")
            receipt["references"].append({"registry": registry, "reference": reference,
                                           "tag": reference.rsplit(":", 1)[1], "digest": digest,
                                           "image_id": observed_id, "verified_at": stamp()})
            write(receipt_path, receipt)

    try:
        if not expected_id or not DIGEST.fullmatch(expected_id):
            raise RuntimeError("unable to resolve built image ID")
        if policy == "fatal" and any(scan_data.get(target, {}).get("state") != "ok" for target in ("image", "base", "sbom", "trivy")):
            receipt["critical_failure"] = "fatal-scan-policy-failed"
            raise RuntimeError("fatal scan policy rejected unavailable/failed evidence")
        push(built, "noirolabs", tag_from=False)
        for tag in other_tags:
            push(f"quay.io/noirolabs/{args.image}:{tag}", "noirolabs")
        if args.image == "opflex-build":
            # Preserve the runtime build-image visibility boundary.
            push(f"quay.io/noirolabs/{args.image}:{args.z_tag}", "noirolabs")
            receipt["state"] = "complete"
            return 0
        for tag in (args.dated_tag,):
            push(f"quay.io/noiro/{args.image}:{tag}", "quay", args.quay_config)
            push(f"docker.io/noiro/{args.image}:{tag}", "docker")
        # No mutable alias is pushed until all immutable destinations verify.
        push(f"quay.io/noirolabs/{args.image}:{args.z_tag}", "noirolabs")
        push(f"quay.io/noiro/{args.image}:{args.z_tag}", "quay", args.quay_config)
        push(f"docker.io/noiro/{args.image}:{args.z_tag}", "docker")
        receipt["state"] = "complete"
        return 0
    except (RuntimeError, ValueError, KeyError, TypeError, StopIteration):
        receipt["state"] = "partial" if receipt["references"] else "failed"
        receipt["critical_failure"] = receipt.get("critical_failure") or "image-publication-failed"
        return 1
    finally:
        receipt["finished_at"] = stamp()
        write(receipt_path, receipt)
        # A failed image still has publishable scan and publication evidence.
        if args.image not in ("opflex-build", "opflex-build-base"):
            row = {"registry": "quay.io/noiro", "image": args.image, "tag": args.tag,
                   "other_tags": other_tags, "sha": expected_id,
                   "base_image": scan_data.get("base", {}).get("provenance", {}).get("reference", args.base_image),
                   "image_z_tag": args.z_tag, "dated_tag": args.dated_tag,
                   "scan": scan_data, "scan_policy": policy, "publication": receipt,
                   "build_inputs": build_inputs, "build_input_error": build_input_error,
                   "reports": {kind: f"reports/{args.image}/{kind}.txt" for kind in ("sbom", "cve", "cve-base")}}
            for kind in ("scan", "trivy"):
                if (report_dir / (kind + ".json")).exists():
                    row["reports"][kind] = f"reports/{args.image}/{kind}.json"
            for target, filename in (("image", "cve"), ("base", "cve-base")):
                if (report_dir / (filename + ".json")).exists():
                    row["reports"]["grype-" + target] = f"reports/{args.image}/{filename}.json"
            if build_inputs:
                row["reports"]["build-input"] = "opflex-build-base-input.txt"
                row["reports"]["build-input-provenance"] = "base-provenance/opflex-build.json"
            metadata = directory / "image-metadata.jsonl"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            with metadata.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "registry", "image", "tag", "other-tags", "base-image", "z-tag", "dated-tag", "quay-config"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    sys.exit(publish(args))


if __name__ == "__main__":
    main()
