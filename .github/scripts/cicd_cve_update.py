import os
import sys
import yaml
import json
import base64
import requests
import subprocess
from datetime import datetime
import pytz
import re


def count_severity(filepath):
    severity_count = {}
    try:
        if not os.path.exists(filepath):
            print(f"File does not exist: {filepath}")
            return {}

        with open(filepath, "r", encoding="utf-8") as json_file:
            content = json.load(json_file)
            data = content.get("data")
            vulnerabilities = data.get("Layer", {}).get("Features", [])
            accepted_severities = ["Critical", "High", "Medium", "Low", "Unknown"]

            for feature in vulnerabilities:
                vulnerabilities_list = feature.get("Vulnerabilities", [])
                for vulnerability in vulnerabilities_list:
                    severity = vulnerability.get("Severity")
                    if severity not in accepted_severities:
                        print(f"Unexpected severity value found: {severity}")
                        continue
                    severity_count[severity] = severity_count.get(severity, 0) + 1
    except Exception as e:
        print(f"An error occurred while counting severity: {str(e)}")

    result = [{
        "C": severity_count.get("Critical", 0),
        "H": severity_count.get("High", 0),
        "M": severity_count.get("Medium", 0),
        "L": severity_count.get("Low", 0),
        "U": severity_count.get("Unknown", 0)
    }]
    return result

def get_response(api_endpoint, image_name, sha, username, password):
    """
    Fetch the vulnerability report from a Quay API endpoint.
    Constructs the URL using the image name and SHA, encodes credentials,
    and returns the HTTP response.
    """
    api_url = f"{api_endpoint}/{image_name}/manifest/sha256:{sha}/security?vulnerabilities=true"
    credentials = f"{username}:{password}"
    encoded_credentials = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {encoded_credentials}",
        "Content-Type": "application/json",
    }
    response = requests.get(api_url, headers=headers)
    return response

def process_container_image(container_image, release_tag, release_stream, quay_api_endpoint, username, password):
    sha = container_image.get("quay", [{}])[0].get("sha")
    image_name = container_image.get("name")
    
    print(f"Processing image: {image_name} with SHA: {sha}")
    
    if not sha:
        print("No SHA found for image, skipping vulnerability processing.")
        return container_image

    response = get_response(quay_api_endpoint, image_name, sha, username, password)
    print(f"Fetching vulnerabilities for image with SHA: {sha}")
    
    # Build the file path to save the vulnerability report
    save_path = os.path.join("docs", "release_artifacts", release_tag, release_stream, image_name, f"{release_tag}-quay-cve.txt")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "w") as save_file:
        save_file.write(response.text)
    
    quay_error = ""
    if response.status_code == 404:
        quay_error = "Error 404: Page Not Found"
    
    try:
        json_data = response.json()
        if json_data.get("status") == "queued":
            quay_error = "Scanning Queued in Quay"
    except Exception as e:
        print(f"Error parsing JSON response: {e}")
    
    if quay_error == "":
        print("No Quay error")
        result = count_severity(save_path)
        print(f"{image_name} severity result: {result}")
        container_image["severity"] = result
        container_image["cve_error"] = ""
    else:
        print("Quay error present")
        container_image["cve_error"] = quay_error
        container_image["severity"] = []
    
    container_image["severity_type"] = "quay"
    container_image["severity_link"] = f"https://quay.io/repository/noiro/{image_name}/manifest/sha256:{sha}?tab=vulnerabilities"
    container_image["cve"] = os.path.join("release_artifacts", release_tag, release_stream, image_name, f"{release_tag}-quay-cve.txt")
    
    return container_image

def process_base_image(container_image, release_tag, release_stream, base_quay_api_endpoint, username, password, slave_tag):
    image_name = container_image.get("name")
    base_image_name, base_image_sha = get_base_image(image_name, slave_tag)
    print(f"Fetching vulnerabilities for base image {base_image_name} with SHA: {base_image_sha}")
    
    response = get_response(base_quay_api_endpoint, base_image_name, base_image_sha, username, password)
    save_path = os.path.join("docs", "release_artifacts", release_tag, release_stream, image_name, f"{release_tag}-cve-base.txt")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "w") as save_file:
        save_file.write(response.text)
    
    base_quay_error = ""
    if response.status_code == 404:
        base_quay_error = "Error 404: Page Not Found"
        print(f"Response status code: {response.status_code}")
    
    try:
        json_data = response.json()
        if json_data.get("status") == "queued":
            base_quay_error = "Scanning Queued in Quay"
    except Exception as e:
        print(f"Error parsing base JSON response: {e}")
    
    # Ensure a base-image field exists
    if "base-image" not in container_image:
        container_image["base-image"] = [{}]
    
    if base_quay_error == "":
        print("No base_quay_error")
        result = count_severity(save_path)
        print(f"{base_image_name} severity result: {result}")
        container_image["base-image"][0]["severity"] = result
        container_image["base-image"][0]["base_cve_error"] = ""
    else:
        print("Base image error present")
        container_image["base-image"][0]["base_cve_error"] = base_quay_error
        container_image["base-image"][0]["severity"] = []
    
    container_image["base-image"][0]["sha"] = base_image_sha
    container_image["base-image"][0]["cve"] = os.path.join("release_artifacts", release_tag, release_stream, image_name, f"{release_tag}-cve-base.txt")
    container_image["base-image"][0]["severity_link"] = f"https://quay.io/repository/noirolabs/{base_image_name}/manifest/sha256:{base_image_sha}?tab=vulnerabilities"
    container_image["base-image"][0]["severity_type"] = "quay"
    
    return container_image

def get_base_image(image_name, slave_tag):
    if image_name == "acc-provision-operator":
        base_image = "ansible-operator"
    elif image_name in ["openvswitch", "opflex", "aci-containers-host", "aci-containers-host-ovscni"]:
        base_image = "ubi9-minimal"
    else:
        base_image = "ubi9"

    if image_name == "acc-provision-operator":
        base_tag = "main." + slave_tag
    elif image_name == "opflex":
        base_tag = "9.3." + slave_tag
    else:
        base_tag = "latest." + slave_tag

    image_with_tag = f"quay.io/noirolabs/{base_image}:{base_tag}"
    sha = pull_image_and_get_sha(image_with_tag)
    return base_image, sha

def pull_image_and_get_sha(image_name_and_tag):
    try:
        subprocess.check_output(["docker", "pull", image_name_and_tag], universal_newlines=True)
    except subprocess.CalledProcessError as e:
        print("Error pulling image:", e)
        return "error"
    return get_repo_digest_sha(image_name_and_tag)

def get_repo_digest_sha(image_name_and_tag):
    try:
        try:
            result = subprocess.check_output(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    '{{if .RepoDigests}}{{index (split (index .RepoDigests 0) "@sha256:") 1}}{{else}}missing{{end}}',
                    image_name_and_tag,
                ],
                universal_newlines=True,
            )
            if result.strip() == "missing":
                print(f"Error: RepoDigests field is missing for image {image_name_and_tag}")
                return "error"
        except subprocess.CalledProcessError as e:
            print("Error inspecting image:", e)
            return "error"
    except subprocess.CalledProcessError as e:
        print("Error inspecting image:", e)
        return "error"
    return result.strip()


def select_container_images(release_streams, release_tag):
    container_images = []
    selected_stream_idx = None
    largest_rc_idx = None
    largest_rc_number = -1
    
    for idx, stream in enumerate(release_streams):
        release_name = stream.get("release_name")

        if release_name.endswith(".z"):
            continue
        
        if len(release_streams) == 2 and release_name == release_tag and stream.get("released") == True:
            return stream.get("container_images", []), idx
        
        rc_match = re.search(r"\.rc(\d+)$", release_name)
        if rc_match:
            rc_number = int(rc_match.group(1))
            if rc_number > largest_rc_number:
                largest_rc_number = rc_number
                largest_rc_idx = idx
    
    if largest_rc_idx is not None:
        return release_streams[largest_rc_idx].get("container_images", []), largest_rc_idx
    
    return [], None  # If no valid stream is found

def update_releases_yaml(releases_data, username, password, quay_api_endpoint, base_quay_api_endpoint, slave_tag):
    release_stream = "r"
    for release in releases_data.get("releases", []):
        release_tag = release.get("release_tag")
        release_streams = release.get("release_streams", [])
        
        container_images, selected_stream_idx = select_container_images(release_streams, release_tag)
        if not container_images:
            print(f"Warning: No valid release stream found for release_tag {release_tag}. Skipping processing.")
            continue
        else:
            print(f"Processing release_tag {release_tag} with selected stream index {selected_stream_idx}.")
        
        update_until_str = container_images[0].get("update-release-cves-until")
        if not update_until_str:
            print(f"Skipping stream as \"update-release-cves-until\" tag is missing for {release_tag}")
            continue

        
        try:
            # Split the last part (timezone) and remove it
            update_until_str_parts = update_until_str.rsplit(" ", 1)
            if len(update_until_str_parts) == 2:
                update_until_str_clean = update_until_str_parts[0]  # Remove the timezone
            else:
                update_until_str_clean = update_until_str  # Fallback in case of unexpected format
            
            # Parse without the timezone
            update_until = datetime.strptime(update_until_str_clean, "%Y-%m-%d %H:%M:%S")

            # Convert to Los Angeles timezone
            la_tz = pytz.timezone("America/Los_Angeles")
            update_until = la_tz.localize(update_until)
        except Exception as e:
            print(f"Error parsing update-release-cves-until date: {e}")
            continue
        
        now = datetime.now(pytz.timezone("America/Los_Angeles"))
        if update_until and now > update_until:
            print(f"Skipping processing for {release_tag} as update-release-cves-until has passed.")
            continue
        
        for idx, container_image in enumerate(container_images):
            try:
                container_image = process_container_image(container_image, release_tag, release_stream, quay_api_endpoint, username, password)
                container_image = process_base_image(container_image, release_tag, release_stream, base_quay_api_endpoint, username, password, slave_tag)
                container_images[idx] = container_image
            except requests.exceptions.RequestException as e:
                print(f"Error fetching vulnerabilities for {container_image.get('name')}: {e}")
                continue
        
        if selected_stream_idx is not None:
            release_streams[selected_stream_idx]["container_images"] = container_images
    
    return releases_data

quay_username = os.getenv("INPUT_QUAY_USERNAME")
quay_password = os.getenv("INPUT_QUAY_PASSWORD")
slave_tag = os.getenv("INPUT_SLAVE_TAG", "172.28.184.245")
quay_api_endpoint = os.getenv("INPUT_QUAY_API_ENDPOINT", "https://quay.io/api/v1/repository/noiro")
base_quay_api_endpoint = os.getenv("INPUT_BASE_QUAY_API_ENDPOINT", "https://quay.io/api/v1/repository/noirolabs")

if not all([quay_username, quay_password, slave_tag]):
    print("Missing one or more required environment variables: INPUT_QUAY_USERNAME, INPUT_QUAY_PASSWORD, INPUT_SLAVE_TAG")
    sys.exit(1)

# Get the path to the releases.yaml file (can be overridden by INPUT_RELEASES_YAML)
releases_yaml_path = os.getenv("INPUT_RELEASES_YAML", os.path.join("docs", "release_artifacts", "releases.yaml"))

try:
    with open(releases_yaml_path, "r") as file:
        releases_data = yaml.safe_load(file)
except Exception as e:
    print(f"Error loading YAML file: {e}")
    sys.exit(1)

updated_data = update_releases_yaml(
    releases_data,
    quay_username,
    quay_password,
    quay_api_endpoint,
    base_quay_api_endpoint,
    slave_tag
)

try:
    with open(releases_yaml_path, "w") as file:
        yaml.dump(updated_data, file, default_flow_style=False)
    print("Updated releases.yaml successfully.")
except Exception as e:
    print(f"Error writing YAML file: {e}")
