from datetime import datetime
import os
import sys
import yaml
import pytz
import subprocess


def get_docker_image_sha(image_name_and_tag):
    try:
        # Run the 'docker image inspect' command as a subprocess
        result = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{index (split (index .RepoDigests 0) "@sha256:") 1}}', image_name_and_tag], universal_newlines=True)

        # 'result' now contains the output of the command, which includes the SHA digest
        sha_digest = result.strip()  # Remove any leading/trailing whitespaces or newlines

        return sha_digest
    except subprocess.CalledProcessError as e:
        # Handle any errors that occur during the subprocess execution
        print("Error:", e)
        return ""


def count_severity(filename):
    filepath = "/tmp/" + GIT_LOCAL_DIR + "/docs/release_artifacts/" + RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + filename
    if os.path.exists(filepath):
        with open(filepath, "r") as file:
            data = file.read()

        lines = data.strip().split('\n')

        severity_list = []

        # Loop through each line (ignoring the first line) and extract the SEVERITY value
        for line in lines[1:]:
            columns = line.split()
            severity = columns[-1]
            severity_list.append(severity)

        # Count occurrences of "Citical", "High", "Medium, "Low", "Unknown"
        critical_count = severity_list.count("Critical")
        high_count = severity_list.count("High")
        medium_count = severity_list.count("Medium")
        low_count = severity_list.count("Low")
        unknown_count = severity_list.count("Unknown")

        result = [
            {
                "C": critical_count,
                "H": high_count,
                "M": medium_count,
                "L": low_count,
                "U": unknown_count
            }
        ]
        # Return the results
        return result

    return []


GIT_LOCAL_DIR = "cicd-status"
RELEASE_TAG = os.environ.get("RELEASE_TAG")

release_filepath = "/tmp/" + GIT_LOCAL_DIR + "/docs/release_artifacts/releases.yaml"

release_name_exists = False

# Get the timezone for Pacific Time
pacific_time = pytz.timezone('US/Pacific')

if not os.path.exists(release_filepath):
    with open(release_filepath, 'w'):
        pass

# Check if all required command-line arguments are provided
if "acc-provision" != os.environ.get("TRAVIS_REPO_SLUG").split("/")[1] :

    if len(sys.argv) != 9:
        print("Usage: python update-release.py IMAGE_BUILD_REGISTRY IMAGE IMAGE_BUILD_TAG OTHER_IMAGE_TAGS IMAGE_SHA IMAGE_Z_TAG TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER BASE_IMAGE")
        sys.exit(1)

    # Get the command-line arguments
    IMAGE_BUILD_REGISTRY = sys.argv[1]
    IMAGE = sys.argv[2]
    IMAGE_BUILD_TAG = sys.argv[3]
    OTHER_IMAGE_TAGS = sys.argv[4]
    IMAGE_SHA = sys.argv[5]
    IMAGE_Z_TAG = sys.argv[6]
    TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER = sys.argv[7]
    BASE_IMAGE = sys.argv[8]

    if os.path.exists(release_filepath):
        with open(release_filepath, "r") as file:
            yaml_data = yaml.safe_load(file)
            # Check if yaml_data is not None before accessing its keys
            if yaml_data is None:
                yaml_data = {"releases": []}

            if "releases" not in yaml_data:
                yaml_data["releases"] = []

            for release in yaml_data["releases"]:
                if release.get("release_name") == RELEASE_TAG:
                    release_name_exists = True
                    break
            if not release_name_exists:

                new_release_data = {
                    "release_name": RELEASE_TAG,
                    "last_updated": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "container_images": [
                        {
                            "name": IMAGE,
                            "commit": [{"link": "https://github.com/"+ os.environ.get("TRAVIS_REPO_SLUG") + "/commit/" + os.environ.get("TRAVIS_COMMIT"), "sha":os.environ.get("TRAVIS_COMMIT")}],
                            "quay": [
                                {"tag": IMAGE_Z_TAG, "sha": IMAGE_SHA,
                                 "link": "https://" + IMAGE_BUILD_REGISTRY + "/" + IMAGE + ":" + IMAGE_Z_TAG},
                                {"tag": TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER, "sha": IMAGE_SHA,
                                 "link": "https://" + IMAGE_BUILD_REGISTRY + "/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER},
                            ],
                            "docker": [
                                {"tag": IMAGE_Z_TAG, "sha": IMAGE_SHA,  "link": "https://hub.docker.com/layers/noiro" + "/" + IMAGE + "/" + IMAGE_Z_TAG +
                                          "/images/sha256-" + get_docker_image_sha("noiro/"+IMAGE+":"+IMAGE_Z_TAG) + "?context=explore"},
                                {"tag": TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER, "sha": IMAGE_SHA,
                                 "link": "https://hub.docker.com/layers/noiro" + "/" + IMAGE + "/" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER +
                                          "/images/sha256-" + get_docker_image_sha("noiro/"+IMAGE+":"+TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER) + "?context=explore"},
                            ],
                            "base-image": [
                                {"sha": get_docker_image_sha(BASE_IMAGE),
                                 "cve": "release_artifacts/"+ RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "cve-base.txt",
                                 "severity": count_severity("cve-base.txt")
                                },
                            ],
                            "sbom": "release_artifacts/"+ RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "sbom.txt",
                            "cve": "release_artifacts/"+ RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "cve.txt",
                            "build-logs": "release_artifacts/"+ RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                            "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                            "severity": count_severity("cve.txt")
                        }
                    ]
                }

                yaml_data["releases"].append(new_release_data)

            else:

                for release_idx, release in enumerate(yaml_data["releases"]):
                    if release.get("release_name") == RELEASE_TAG:
                        yaml_data["releases"][release_idx]["last_updated"] = datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z")
                        release_image_exists = False
                        for idx, image in enumerate(yaml_data["releases"][release_idx]["container_images"]):
                            if image.get("name") == IMAGE:
                                release_image_exists = True
                                print("release image exists", release_image_exists)
                                yaml_data["releases"][release_idx]["container_images"][idx] = {
                                        "name": IMAGE,
                                        "commit": [{"link": "https://github.com/" + os.environ.get(
                                            "TRAVIS_REPO_SLUG") + "/commit/" + os.environ.get("TRAVIS_COMMIT"),
                                                    "sha": os.environ.get("TRAVIS_COMMIT")}],
                                        "quay": [
                                            {"tag": IMAGE_Z_TAG, "sha": IMAGE_SHA,
                                             "link": "https://" + IMAGE_BUILD_REGISTRY + "/" + IMAGE + ":" + IMAGE_Z_TAG},
                                            {"tag": TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER, "sha": IMAGE_SHA,
                                             "link": "https://" + IMAGE_BUILD_REGISTRY + "/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER},
                                        ],
                                        "docker": [
                                            {"tag": IMAGE_Z_TAG, "sha": IMAGE_SHA,
                                             "link": "https://hub.docker.com/layers/noiro" + "/" + IMAGE + "/" + IMAGE_Z_TAG +
                                                     "/images/sha256-" + get_docker_image_sha(
                                                 "noiro/" + IMAGE + ":" + IMAGE_Z_TAG) + "?context=explore"},
                                            {"tag": TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER, "sha": IMAGE_SHA,
                                             "link": "https://hub.docker.com/layers/noiro" + "/" + IMAGE + "/" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER +
                                                     "/images/sha256-" + get_docker_image_sha(
                                                 "noiro/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER) + "?context=explore"},
                                        ],
                                        "base-image": [
                                            {"sha": get_docker_image_sha(BASE_IMAGE),
                                             "cve": "release_artifacts/" + RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "cve-base.txt",
                                             "severity": count_severity("cve-base.txt")
                                             },
                                        ],
                                        "sbom": "release_artifacts/" + RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "sbom.txt",
                                        "cve": "release_artifacts/" + RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "cve.txt",
                                        "build-logs": "release_artifacts/" + RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                                        "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                                        "severity": count_severity("cve.txt")
                                }

                                break


                        if not release_image_exists:
                            print("release image not exists", release_image_exists)
                            new_image = {
                                "name": IMAGE,
                                "commit": [{"link": "https://github.com/"+ os.environ.get("TRAVIS_REPO_SLUG") + "/commit/" + os.environ.get("TRAVIS_COMMIT"), "sha":os.environ.get("TRAVIS_COMMIT")}],
                                "quay": [
                                    {"tag": IMAGE_Z_TAG, "sha": IMAGE_SHA,
                                     "link": "https://" + IMAGE_BUILD_REGISTRY + "/" + IMAGE + ":" + IMAGE_Z_TAG},
                                    {"tag": TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER, "sha": IMAGE_SHA,
                                     "link": "https://" + IMAGE_BUILD_REGISTRY + "/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER},
                                ],
                                "docker": [
                                    {"tag": IMAGE_Z_TAG, "sha": IMAGE_SHA,
                                     "link": "https://hub.docker.com/layers/noiro" + "/" + IMAGE + "/" + IMAGE_Z_TAG +
                                             "/images/sha256-" + get_docker_image_sha(
                                         "noiro/" + IMAGE + ":" + IMAGE_Z_TAG) + "?context=explore"},
                                    {"tag": TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER, "sha": IMAGE_SHA,
                                     "link": "https://hub.docker.com/layers/noiro" + "/" + IMAGE + "/" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER +
                                             "/images/sha256-" + get_docker_image_sha(
                                         "noiro/" + IMAGE + ":" + TRAVIS_TAG_WITH_UPSTREAM_ID_DATE_TRAVIS_BUILD_NUMBER) + "?context=explore"},
                                ],
                                "base-image": [
                                    {"sha": get_docker_image_sha(BASE_IMAGE),
                                     "cve": "release_artifacts/" + RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "cve-base.txt",
                                     "severity": count_severity("cve-base.txt")
                                     },
                                ],
                                "sbom": "release_artifacts/" + RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "sbom.txt",
                                "cve": "release_artifacts/" + RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "cve.txt",
                                "build-logs": "release_artifacts/" + RELEASE_TAG + "/" + IMAGE + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                                "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                                "severity": count_severity("cve.txt")
                            }

                            yaml_data["releases"][release_idx]["container_images"].append(new_image)

                        break


else:
    if len(sys.argv) != 3:
        print("Usage: python update-release.py PYPI_REGISTRY TAG_NAME")
        sys.exit(1)

    PYPI_REGISTRY = sys.argv[1]
    TAG_NAME = sys.argv[2]

    if os.path.exists(release_filepath):
        with open(release_filepath, "r") as file:
            yaml_data = yaml.safe_load(file)

            if yaml_data is None:
                yaml_data = {"releases": []}
                
            if "releases" not in yaml_data:
                yaml_data["releases"] = []

            for release in yaml_data["releases"]:
                if release.get("release_name") == RELEASE_TAG:
                    release_name_exists = True
                    break

            if not release_name_exists:
                new_release_data = {
                    "release_name": RELEASE_TAG,
                    "last_updated": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "acc_provision": [
                        {
                            "link": PYPI_REGISTRY,
                            "tag": TAG_NAME,
                            "commit": [{"link": "https://github.com/" + os.environ.get(
                                "TRAVIS_REPO_SLUG") + "/commit/" + os.environ.get("TRAVIS_COMMIT"),
                                        "sha": os.environ.get("TRAVIS_COMMIT")}],
                            "build-logs": "release_artifacts/" + RELEASE_TAG + "/" + "acc-provision" + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                            "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                        }
                    ]
                }

                yaml_data["releases"].append(new_release_data)
            else:
                for release_idx, release in enumerate(yaml_data["releases"]):
                    if release.get("release_name") == RELEASE_TAG:
                        yaml_data["releases"][release_idx]["last_updated"] = datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z")
                        yaml_data["releases"][release_idx]["acc_provision"] = [{
                            "link": PYPI_REGISTRY,
                            "tag": TAG_NAME,
                            "commit": [{"link": "https://github.com/" + os.environ.get(
                                "TRAVIS_REPO_SLUG") + "/commit/" + os.environ.get("TRAVIS_COMMIT"),
                                        "sha": os.environ.get("TRAVIS_COMMIT")}],
                            "build-logs": "release_artifacts/" + RELEASE_TAG + "/" + "acc-provision" + "/" + RELEASE_TAG + "-" + "buildlog.txt",
                            "build-time": datetime.utcnow().astimezone(pacific_time).strftime("%Y-%m-%d %H:%M:%S %Z"),
                        }]

# Write the updated YAML data back to release.yaml
with open(release_filepath, "w") as file:
    yaml.dump(yaml_data, file, default_flow_style=False)