#!/bin/bash

if ! which curl >/dev/null; then
  echo "cURL is not found, please install it and retry."
  exit 1
fi

if ! docker compose >/dev/null; then
  echo "Docker Compose plugin is not working properly, please resolve it and retry."
  echo "Refer to https://docs.docker.com/compose/install/linux/ for more installation instructions."
  exit 2
fi

if ! docker images | grep tigergraph/community >/dev/null; then
  echo "TigerGraph Community version docker image is not found, please download from https://dl.tigergraph.com/ and load it."
  exit 3
fi

tg_version=$(docker images --format "{{.Tag}}" tigergraph/community 2>/dev/null | grep -E '^4\.[23]\.' | sort -Vr | head -1)
if [[ -z "$tg_version" || ! "$tg_version" =~ ^4\.[23]\. ]]; then
  echo "TigerGraph version is not compatible, please use 4.2.0+"
  exit 4
fi

root_dir=${1:-./graphrag}
llm_provider=${2:-openai}
tg_username_raw=${3:-tigergraph}
tg_password_raw=${4:-tigergraph}
tg_username=$(echo "$tg_username_raw" | sed 's/[][\/.^$*+?|(){}]/\\&/g')
tg_password=$(echo "$tg_password_raw" | sed 's/[][\/.^$*+?|(){}]/\\&/g')
export TIGERGRAPH_USERNAME="$tg_username_raw"
export TIGERGRAPH_PASSWORD="$tg_password_raw"
export CHAT_HISTORY_RUNTIME_USERNAME="$tg_username_raw"
export CHAT_HISTORY_RUNTIME_PASSWORD="$tg_password_raw"

if [[ -z $LLM_API_KEY ]]; then
  echo "Warning: LLM_API_KEY is not found in current environment, please set it using 'export LLM_API_KEY=xxx'."
  echo "Or manaully modify ${root_dir}/configs/server_config.json to set the LLM_API_KEY then re-run 'docker compose up -d'."
fi

mkdir -p $root_dir || true
[[ -d $root_dir ]] || { echo "Target dir $root_dir is not found!"; exit 5; }

echo "Entering GraphRAG root dir: $root_dir"
cd $root_dir || { echo "Cannot switch to $root_dir!"; exit 5; }

echo "Downloading GraphRAG service config..."
mkdir -p configs || true
curl -sk https://raw.githubusercontent.com/tigergraph/graphrag/refs/heads/main/docs/tutorials/docker-compose.yml | sed "s/community:4.2.2/community:${tg_version}/g" > docker-compose.yml
curl -sk https://raw.githubusercontent.com/tigergraph/graphrag/refs/heads/main/docs/tutorials/configs/nginx.conf -o configs/nginx.conf
curl -sk "https://raw.githubusercontent.com/tigergraph/graphrag/refs/heads/main/docs/tutorials/configs/server_config.json.${llm_provider}" | sed '/"gsPort": "14240"/a\
    "username": "'${tg_username}'",\
    "password": "'${tg_password}'",
' | sed "s/YOUR_LLM_API_KEY_HERE/${LLM_API_KEY}/g" > configs/server_config.json

echo "Starting GraphRAG services..."
docker compose pull --ignore-pull-failures
docker compose up -d
sleep 5

echo "Checking service status..."
if ! curl -s http://localhost:14240/restpp/version >/dev/null; then
  echo "Starting TigerGraph instance..."
  docker exec tigergraph /home/tigergraph/tigergraph/app/cmd/gadmin start all >/dev/null
  sleep 5
fi

time_out=300
while [[ $time_out -gt 0 ]]; do
  if ! curl -s http://localhost:14240/restpp/version >/dev/null; then
    echo "Waiting for TigerGraph instance to be ready... (${time_out}s remaining)"
    sleep 5
    time_out=$((time_out-5))
  else
    echo "TigerGraph is ready. Starting GraphRAG service..."
    docker compose up -d graphrag >/dev/null
    break
  fi
done

if ! docker ps | grep "tigergraph/graphrag:latest" >/dev/null; then
  echo "Failed to start GraphRAG service."
  echo 'Please double check tigergraph username and password in configs/server_config.json, and re-run `docker compose up -d`'
  echo 'Or check log via `docker logs graphrag` for detailed failure.'
else
  echo "GraphRAG service started successfully."
  echo "Visit http://localhost to access the chatbot."
fi
cd - >/dev/null
