#!/bin/bash

# Ensure an argument is provided
if [ -z "$1" ]; then
  echo "Usage: $0 <metadata_json_file>"
  exit 1
fi

METADATA_FILE=$1
CLONE_DIR="opflex"
UPDATED_METADATA="updated_metadata.json"

# Check if the metadata file exists
if [ ! -f "$METADATA_FILE" ]; then
  echo "Error: File $METADATA_FILE not found!"
  exit 1
fi

# Ensure the OpFlex repository is available
if [ ! -d "$CLONE_DIR" ]; then
  echo "Error: OpFlex repository not found! Ensure it is cloned in the workspace."
  exit 1
fi

# Clear old metadata file
jq -n '[]' > "$UPDATED_METADATA"

# Process metadata
jq -c '.[]' "$METADATA_FILE" | while read -r item; do
  RELEASE_TAG=$(echo "$item" | jq -r '.release_tag')
  BASE_TAG=$(echo "$item" | jq -r '.base_tag')
  BASE_IMAGE=$(echo "$item" | jq -r '.base_image')
  UPDATE_DIGEST=$(echo "$item" | jq -r '.update_digest')

  if [ -z "$BASE_TAG" ]; then
    echo "Base tag is empty for $RELEASE_TAG. Skipping..."
    continue
  fi

  # Move to OpFlex repo
  cd "$CLONE_DIR" || exit 1
  git checkout master
  git pull origin master

  # Skip updating if the branch doesn't exist
  if git show-ref --verify --quiet "refs/heads/$BASE_TAG"; then
    git checkout "$BASE_TAG"
  else
    echo "Branch $BASE_TAG does not exist. Skipping update for $RELEASE_TAG."
    cd .. || exit 1
    continue
  fi

  if [ "$UPDATE_DIGEST" == "true" ]; then
    echo "Finding actual base image for $RELEASE_TAG..."
    BASE_IMAGE=$(grep -E '^FROM' "docker/travis/Dockerfile-opflex" | awk '{print $2}')
    
    if [ -z "$BASE_IMAGE" ]; then
      echo "Failed to determine base image for $RELEASE_TAG. Skipping..."
      cd .. || exit 1
      continue
    fi

    echo "Base image for $RELEASE_TAG resolved to: $BASE_IMAGE"

    # Fetch digest of resolved base image
    LATEST_DIGEST=$(skopeo inspect --no-creds "docker://$BASE_IMAGE" | jq -r '.Digest' | sed 's/sha256://')

    if [ -z "$LATEST_DIGEST" ]; then
      echo "Failed to fetch digest for base image $BASE_IMAGE. Skipping..."
      cd .. || exit 1
      continue
    fi

    echo "Resolved digest for $RELEASE_TAG: $LATEST_DIGEST"

    # Append updated metadata safely
    tmp_file=$(mktemp)
    jq --arg release_tag "$RELEASE_TAG" \
       --arg base_tag "$BASE_TAG" \
       --arg base_image "$BASE_IMAGE" \
       --arg latest_digest "$LATEST_DIGEST" \
       '. + [{"release_tag": $release_tag, "base_tag": $base_tag, "base_image": $base_image, "latest_digest": $latest_digest, "update_digest": "true"}]' \
       "$UPDATED_METADATA" > "$tmp_file" && mv "$tmp_file" "$UPDATED_METADATA"
  fi

  # Set up git user & ensure remote exists
  git config --local user.name "noiro-generic"
  git config --local user.email "noiro-generic@github.com"

  if ! git remote | grep -q noiro-generic; then
    git remote add noiro-generic https://noiro-generic:${{ secrets.NOIRO101_GENERIC_PAT }}@github.com/noironetworks/opflex.git
  fi

  TAG_MESSAGE="ACI Release $BASE_TAG Created by Github workflow ${{ github.workflow }} ${{ github.server_url }}/noironetworks/opflex/actions/runs/${{ github.run_id }}"

  git tag -f -a "$BASE_TAG" -m "$TAG_MESSAGE"
  git push noiro-generic -f "$BASE_TAG"

  cd .. || exit 1
done
