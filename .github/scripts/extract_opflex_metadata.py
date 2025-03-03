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

def save_releases(filepath, data):
    with open(filepath, "w") as file:
        yaml.dump(data, file, default_flow_style=False)

def get_base_image_digest(image_name):
    if image_name == "placeholder-image":
        return None
    try:
        result = subprocess.check_output(
            f"skopeo inspect --no-creds docker://{image_name} | jq -r '.Digest'",
            shell=True,
            text=True
        ).strip()
        return result.replace("sha256:", "") if result else None
    except subprocess.CalledProcessError as e:
        print(f"Error fetching digest for {image_name}: {e}")
        return None

def process_opflex_metadata(releases):
    release_list = []
    updated = False

    for release in releases.get("releases", []):
        if not release.get("released", False):
            for stream in release.get("release_streams", []):
                if stream.get("release_name", "").endswith(".z"):
                    for image in stream.get("container_images", []):
                        if image.get("name") == "opflex":
                            # Ensure opflex-metadata exists
                            if "opflex-metadata" not in image:
                                print(f"Adding missing opflex-metadata for {release['release_tag']}")
                                image["opflex-metadata"] = {
                                    "base-image": "placeholder-image",
                                    "base-image-sha": "unknown",
                                    "update_digest": "true"
                                }
                                updated = True

                            metadata = image["opflex-metadata"]
                            base_image = metadata["base-image"]
                            stored_digest = metadata["base-image-sha"]

                            if base_image == "placeholder-image":
                                print(f"Skipping digest lookup for {release['release_tag']} (Base-Image is placeholder).")
                                release_list.append({
                                    "release_tag": release["release_tag"],
                                    "base_tag": f"{release['release_tag']}-opflex-build-base",
                                    "base_image": base_image,
                                    "stored_digest": stored_digest,
                                    "latest_digest": "unknown",
                                    "update_digest": "true"
                                })
                                continue

                            latest_digest = get_base_image_digest(base_image)

                            if latest_digest and latest_digest != stored_digest:
                                print(f"Digest changed for {release['release_tag']}: {stored_digest} → {latest_digest}")
                                metadata["base-image-sha"] = latest_digest
                                updated = True
                                release_list.append({
                                    "release_tag": release["release_tag"],
                                    "base_tag": f"{release['release_tag']}-opflex-build-base",
                                    "base_image": base_image,
                                    "stored_digest": stored_digest,
                                    "latest_digest": latest_digest,
                                    "update_digest": "false"
                                })

    return release_list, updated

releases = load_releases(release_filepath)
release_list, metadata_updated = process_opflex_metadata(releases)

# Save updated releases.yaml if changes were made
if metadata_updated:
    save_releases(release_filepath, releases)
    print(f"Updated releases.yaml with new opflex metadata and digest information.")

# Save extracted metadata to JSON file
with open(output_filepath, "w") as json_file:
    json.dump(release_list, json_file, indent=2)

print(f"Extracted opflex metadata saved to {output_filepath}")
