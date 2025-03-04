import os
import sys
import yaml
import json
import subprocess

if len(sys.argv) < 3:
    print("Usage: python extract_opflex_metadata.py <repo_name> <output_file>")
    sys.exit(1)

repo_name = sys.argv[1]
output_filepath = sys.argv[2]
release_filepath = os.path.join(repo_name, "docs/release_artifacts/releases.yaml")

def load_releases(filepath):
    if not os.path.exists(filepath):
        print(f"Error: {filepath} not found!")
        return {}

    with open(filepath, "r") as file:
        try:
            return yaml.safe_load(file) or {}
        except yaml.YAMLError as exc:
            print(f"Error parsing YAML: {exc}")
            return {}

def get_base_image_digest(image_name):
    if image_name == "placeholder-image":
        return None
    try:
        result = subprocess.check_output(
            f"skopeo inspect --no-creds docker://{image_name} | jq -r '.Digest'",
            shell=True,
            text=True
        ).strip()
        return result.replace("sha256:", "")
    except subprocess.CalledProcessError as e:
        print(f"Error fetching digest for {image_name}: {e}")
        return None

def extract_opflex_metadata(releases):
    release_list = []

    for release in releases.get("releases", []):
        release_tag = release.get("release_tag", "")
        release_status = None
        z_stream = None

        for stream in release.get("release_streams", []):
            release_name = stream.get("release_name", "")
            if ".rc" in release_name:
                continue
            if release_name == release_tag:
                release_status = stream.get("released", False)
            elif release_name.endswith(".z"):
                z_stream = stream

        if z_stream:
            if release_status is False:
                print(f"Processing .z stream for {release_tag}")
                for image in z_stream.get("container_images", []):
                    if image.get("name") == "opflex":
                        base_image = image.get("opflex-metadata", {}).get("base-image", "placeholder-image")
                        stored_digest = image.get("opflex-metadata", {}).get("base-image-sha", "unknown")

                        if base_image == "placeholder-image":
                            print(f"Skipping digest lookup for {release_tag} (Base-Image is placeholder).")
                            release_list.append({
                                "release_tag": release_tag,
                                "base_tag": f"{release_tag}-opflex-build-base",
                                "base_image": base_image,
                                "stored_digest": stored_digest,
                                "latest_digest": "unknown",
                                "update_digest": "true"
                            })
                            continue

                        print(f"digest lookup for {release_tag}, {base_image}")
                        latest_digest = get_base_image_digest(base_image)
                        if latest_digest and latest_digest != stored_digest:
                            print(f"Digest changed for {release_tag}: {stored_digest} → {latest_digest}")
                            image["opflex-metadata"]["base-image-sha"] = latest_digest
                            release_list.append({
                                "release_tag": release_tag,
                                "base_tag": f"{release_tag}-opflex-build-base",
                                "base_image": base_image,
                                "stored_digest": stored_digest,
                                "latest_digest": latest_digest,
                                "update_digest": "false"
                            })
                        else:
                            print(f"Digest not changed for {release_tag}: stored:{stored_digest} :: latest:{latest_digest}")

    return release_list

releases = load_releases(release_filepath)
release_list = extract_opflex_metadata(releases)

with open(output_filepath, "w") as json_file:
    json.dump(release_list, json_file, indent=2)

print(f"Extracted OpFlex metadata saved to {output_filepath}")
