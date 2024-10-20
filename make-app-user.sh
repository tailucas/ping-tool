#!/usr/bin/env bash
set -eu

USER_NAME=app
GROUP_NAME=app
USER_ID=999
GROUP_ID=999

DOCKER_GROUP_NAME=docker
DOCKER_GROUP_ID=$(getent group ${DOCKER_GROUP_NAME} | cut -f3 -d ':')

if [ "$GROUP_ID" != "${DOCKER_GROUP_ID:-}" ]; then
  if ! getent group ${GROUP_NAME} >/dev/null 2>&1; then
    OP="group '${GROUP_NAME}' (${GROUP_ID})"
    read -p "Create ${OP}? [y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
      echo "Creating ${OP}..."
      sudo groupadd -f -r -g ${GROUP_ID} ${GROUP_NAME}
    else
      getent group ${GROUP_NAME}
    fi
  fi
fi

if ! id ${USER_NAME} >/dev/null 2>&1; then
  OP="user '${USER_NAME}' (${USER_ID}) in group ${GROUP_ID}"
  read -p "Create ${OP}? [y/n] " -n 1 -r
  echo
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Creating ${OP}..."
    sudo useradd -r -u ${USER_ID} -g ${GROUP_ID} ${USER_NAME}
  else
    id ${USER_NAME}
  fi
fi

if [ -n "${DOCKER_GROUP_ID:-}" ]; then
  if ! id -nG "$USER_ID" | grep -qw "${DOCKER_GROUP_NAME}"; then
    OP="user '${USER_NAME}' (${USER_ID}) to docker group ${DOCKER_GROUP_ID}"
    read -p "Add ${OP}? [y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
      echo "Adding ${OP}..."
      sudo usermod -a -G ${DOCKER_GROUP_ID} -u ${USER_ID} ${USER_NAME}
    fi
  fi
fi
