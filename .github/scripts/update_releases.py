import os
import sys
import yaml
import json

if len(sys.argv) < 3:
    print("Usage: python update_releases_yaml.py <metadata_file> <releases_file>")
    sys.exit(1)

metadata_file = sys.argv[1]
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

def update_releases(releases, metadata):
    updated = False
    release_map = {item["release_tag"]: item for item in metadata}

    for release in releases.get("releases", []):
        release_tag = release.get("release_tag")
        if release_tag in release_map:
            opflex_metadata = release_map[release_tag]
            for stream in release.get("release_streams", []):
                if stream.get("release_name", "").endswith(".z"):
                    for image in stream.get("container_images", []):
                        if image.get("name") == "opflex":
                            print(f"Updating opflex-metadata for {release_tag}")
                            image["opflex-metadata"]["base-image"] = opflex_metadata["base_image"]
                            image["opflex-metadata"]["base-image-sha"] = opflex_metadata["latest_digest"]
                            updated = True
                            break
    return updated

# Load metadata and releases.yaml
metadata = load_json(metadata_file)
releases = load_yaml(releases_file)

if not metadata or not releases:
    print("No valid data to update. Exiting.")
    sys.exit(1)

# Update releases.yaml with new metadata
if update_releases(releases, metadata):
    save_yaml(releases_file, releases)
    print(f"Updated releases.yaml with new OpFlex metadata.")
else:
    print("No updates made to releases.yaml.")
