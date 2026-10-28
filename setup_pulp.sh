#!/usr/bin/env bash
set -euo pipefail

for command in curl jq; do
    command -v "$command" >/dev/null || { printf '%s is required\n' "$command" >&2; exit 2; }
done

: "${UPSTREAM_URL:?Set UPSTREAM_URL to the fixture URL reachable from Pulp}"
PULP_BASE_URL=${PULP_BASE_URL:-http://localhost:5001}
PULP_USER=${PULP_USER:-admin}
PULP_PASSWORD=${PULP_PASSWORD:-password}
RUN_ID=${RUN_ID:-$(date +%s)}
SETUP_JSON=${SETUP_JSON:-setup.json}
PULP_BASE_URL=${PULP_BASE_URL%/}
UPSTREAM_URL=${UPSTREAM_URL%/}/

api_url() {
    case "$1" in
        http://*|https://*) printf '%s\n' "$1" ;;
        *) printf '%s%s\n' "$PULP_BASE_URL" "$1" ;;
    esac
}

api() {
    local response
    response=$(curl --fail-with-body --silent --show-error \
        --user "$PULP_USER:$PULP_PASSWORD" "$@") || {
        printf '%s\n' "$response" >&2
        return 1
    }
    printf '%s\n' "$response"
}

create() {
    local endpoint=$1 body=$2 result task state attempt href
    result=$(api -H 'Content-Type: application/json' -X POST -d "$body" "$(api_url "$endpoint")")
    task=$(jq -r '.task // empty' <<<"$result")
    if [[ -n "$task" ]]; then
        for ((attempt=0; attempt<120; attempt++)); do
            result=$(api "$(api_url "$task")")
            state=$(jq -r '.state' <<<"$result")
            case "$state" in
                completed) break ;;
                failed|canceled) printf '%s\n' "$result" >&2; return 1 ;;
            esac
            sleep 1
        done
        [[ "$state" == completed ]] || { printf 'Timed out waiting for %s\n' "$task" >&2; return 1; }
        href=$(jq -er '.created_resources[0]' <<<"$result")
        result=$(api "$(api_url "$href")")
    fi
    jq -er '.pulp_href' <<<"$result"
}

remote_body=$(jq -nc --arg name "loadtest-$RUN_ID" --arg url "$UPSTREAM_URL" \
    '{name:$name,url:$url}')
remote_href=$(create /pulp/api/v3/remotes/maven/maven/ "$remote_body")

for mode in staging normal; do
    if [[ "$mode" == staging ]]; then
        repo_body=$(jq -nc --arg name "loadtest-$RUN_ID-$mode" \
            '{name:$name,staging_enabled:true,staging_autocommit:true}')
    else
        repo_body=$(jq -nc --arg name "loadtest-$RUN_ID-$mode" \
            '{name:$name,staging_enabled:false,staging_autocommit:false}')
    fi
    repo_href=$(create /pulp/api/v3/repositories/maven/maven/ "$repo_body")
    distro_body=$(jq -nc --arg name "loadtest-$RUN_ID-$mode" \
        --arg base_path "loadtest-$RUN_ID-$mode" --arg remote "$remote_href" \
        --arg repository "$repo_href" \
        '{name:$name,base_path:$base_path,remote:$remote,repository:$repository}')
    distro_href=$(create /pulp/api/v3/distributions/maven/maven/ "$distro_body")
    distro=$(api "$(api_url "$distro_href")")
    base_url=$(jq -er '.base_url' <<<"$distro")
    if [[ "$mode" == staging ]]; then
        staging=$(jq -nc --arg repo "$repo_href" --arg distro "$distro_href" \
            --arg base "$base_url" '{repository:$repo,distribution:$distro,base_url:$base}')
    else
        normal=$(jq -nc --arg repo "$repo_href" --arg distro "$distro_href" \
            --arg base "$base_url" '{repository:$repo,distribution:$distro,base_url:$base}')
    fi
done

jq -n --argjson staging "$staging" --argjson normal "$normal" \
    '{staging:$staging,normal:$normal}' > "$SETUP_JSON"
printf 'Wrote %s\n' "$SETUP_JSON"
