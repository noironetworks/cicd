import os
import sys
import yaml
import json

if len(sys.argv) < 3:
    print("Usage: python update_releases_yaml.py <updated_metadata_file> <releases_file>")
    sys.exit(1)

updated_metadata_file = sys.argv[1]
releases_file = sys.argv[2]

def load_json(filepath):
    if not os.path.exists(filepath):
        print(f"Error: {filepath} not found!")
        return []
    
    with open(filepath, "r") as file:
        try:
            return json.load(file)
        except json.JSONDecodeError as exc:
            print(f"Error parsing JSON: {exc}")
            return []

def load_yaml(filepath):
    if not os.path.exists(filepath):
        print(f"Error: {filepath} not found!")
        return {}

    with open(filepath, "r") as file:
        try:
            return yaml.safe_load(file) or {}
        except yaml.YAMLError as exc:
            print(f"Error parsing YAML: {exc}")
            return {}

def save_yaml(filepath, data):
    with open(filepath, "w") as file:
        yaml.dump(data, file, default_flow_style=False)

def update_releases(releases, updated_metadata):
    updated = False
    release_map = {item["release_tag"]: item for item in updated_metadata}

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
            if release_status is False and release_tag in release_map:
                print(f"Updating .z stream metadata for {release_tag}")

                for image in z_stream.get("container_images", []):
                    if image.get("name") == "opflex":
                        opflex_metadata = release_map[release_tag]

                        if "opflex-metadata" not in image:
                            print(f"'opflex-metadata' missing for {release_tag}, initializing...")
                            image["opflex-metadata"] = {
                                "base-image": opflex_metadata["base_image"],
                                "base-image-sha": opflex_metadata["latest_digest"]
                            }
                        else:
                            print(f"Updating existing OpFlex metadata for {release_tag}")
                            image["opflex-metadata"]["base-image"] = opflex_metadata["base_image"]
                            image["opflex-metadata"]["base-image-sha"] = opflex_metadata["latest_digest"]

                        updated = True

    return updated

updated_metadata = load_json(updated_metadata_file)
releases = load_yaml(releases_file)

if not updated_metadata or not releases:
    print("No valid data to update. Exiting.")
    sys.exit(1)

if update_releases(releases, updated_metadata):
    save_yaml(releases_file, releases)
    print(f"Updated releases.yaml with new OpFlex metadata.")
else:
    print("No updates made to releases.yaml.")
