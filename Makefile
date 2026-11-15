.DELETE_ON_ERROR:
.DEFAULT_GOAL := help

# Project identity and container build args: the single source of truth for the
# generated .env. Override on the command line, e.g. `make build SERVER_PORT=9090`.
#
# Deliberately `:=` and not `?=`: an ambient PROJECT_NAME/LANG in the developer's
# shell must never retag the container or change build args.
PROJECT_NAME := ping-tool
SERVER_ADDRESS := 0.0.0.0
SERVER_PORT := 8080
ENCODING := UTF-8
LANG := en_ZA.UTF-8
LANGUAGE := en_ZA
LC_ALL := en_ZA.UTF-8

# Ownership of the container's ./data/ volume. Override per invocation, e.g.
# `make datadir USER_ID=1000 GROUP_ID=1000`.
USER_ID := 999
GROUP_ID := 999

# Name fragment used by `make connect` to find the running container.
DOCKER_APP := app

DOCKER_URL := https://docs.docker.com/engine/install
DOCKER_COMPOSE_URL := https://docs.docker.com/compose/install
DEVCLI_URL := https://code.visualstudio.com/docs/devcontainers/devcontainer-cli
UV_URL := https://docs.astral.sh/uv/getting-started/installation/
CHECK_USER := vscode

# Run tools from .venv without ever re-resolving: uv.lock is the source of truth.
UV_RUN := uv run --frozen --no-sync

# The interpreter request comes from .python-version, and uv discovers the host's
# CPython. `uv python install` is deliberately NOT used: uv publishes no managed
# CPython build for armv6l (`cpython-3.11-linux-arm-gnueabihf` does not exist), so
# on the SBC only discovery can work. Fail early and say what to install instead.
PYTHON_VERSION := $(shell cat .python-version 2>/dev/null)
PYTHON_HINT := No CPython $(PYTHON_VERSION) found on this host. uv has no armv6l build to download, so install the system interpreter: sudo apt install python3-dev

.PHONY: help check dev-build dev-up dev python sync sync-prod setup lock upgrade \
	test verify docker datadir configure build push run rund connect clean \
	unit install-unit uninstall-unit status logs

# ---------- Dev container (host only) ----------

check: ## Check host prerequisites for the dev container (Docker + devcontainer CLI)
	@if [ "${USER}" = "$(CHECK_USER)" ]; then \
	  echo "Running as user ${USER}; dev container targets are host-only."; \
	  echo "Inside the dev container, use: make test|verify|build|run|rund"; \
	  exit 1; \
	fi
	@which docker > /dev/null || (echo "Needs Docker, see $(DOCKER_URL)"; exit 1)
	@which devcontainer > /dev/null || (echo "Needs Dev Container CLI; see $(DEVCLI_URL)"; exit 1)

dev-build: check ## Build the dev container
	devcontainer build --workspace-folder .

dev-up: dev-build ## Start the dev container
	devcontainer up --workspace-folder .

dev: dev-up ## Open a shell in the dev container
	devcontainer exec --workspace-folder . bash

# ---------- Application (host or dev container) ----------

.venv: pyproject.toml uv.lock .python-version ## Create/sync the Python virtual environment
	@uv -V || (echo "Needs uv, see $(UV_URL)"; exit 1)
	@uv python find > /dev/null || (echo "$(PYTHON_HINT)"; exit 1)
	uv sync --frozen
	@touch $@

python: .venv ## Set up the Python virtual environment (alias)

sync: ## Install exactly the locked dependency closure, including the dev group
	@uv -V || (echo "Needs uv, see $(UV_URL)"; exit 1)
	@uv python find > /dev/null || (echo "$(PYTHON_HINT)"; exit 1)
	uv sync --frozen
	@touch .venv

sync-prod: ## Install the locked closure without the dev group (what the service runs)
	@uv -V || (echo "Needs uv, see $(UV_URL)"; exit 1)
	@uv python find > /dev/null || (echo "$(PYTHON_HINT)"; exit 1)
	uv sync --frozen --no-dev
	@touch .venv

setup: sync-prod unit ## Set up this checkout for the native service (deps + rendered unit)
	@echo "Dependencies installed and build/$(UNIT_NAME) rendered."
	@echo "Next: make install-unit (installs, enables and restarts the service)."

lock: ## Re-resolve dependencies within the declared constraints and update uv.lock
	uv lock

upgrade: ## Refresh uv.lock to the newest versions the constraints allow
	uv lock --upgrade

test: ## Run the test suite (offline; ICMP is mocked)
	@uv sync --frozen            # no-op when satisfied; reinstates the dev group if needed
	$(UV_RUN) pytest

verify: ## Fast gate: lockfile agrees, tests pass, sources compile
	uv lock --check
	@uv sync --frozen
	$(UV_RUN) pytest
	python3 -m compileall -q app tests

# ---------- Container ----------

.env: Makefile docker-compose.template ## Generate the container runtime environment
	@rm -f $@
	@printf '%s\n' \
	  'ENCODING="$(ENCODING)"' \
	  'LANG="$(LANG)"' \
	  'LANGUAGE="$(LANGUAGE)"' \
	  'LC_ALL="$(LC_ALL)"' \
	  'D_LANG="$(LANG)"' \
	  'D_LANGUAGE="$(LANGUAGE)"' \
	  'D_LC_ALL="$(LC_ALL)"' \
	  'DOCKER_IMAGE="$(PROJECT_NAME)"' \
	  'DEVICE_NAME="$(PROJECT_NAME)"' \
	  'SERVER_ADDRESS="$(SERVER_ADDRESS)"' \
	  'SERVER_PORT="$(SERVER_PORT)"' \
	  'APP_NAME="$(PROJECT_NAME)"' \
	  'NO_CRON="true"' \
	  'RUN_JAVA_APP="false"' \
	  'RUN_RUST_APP="false"' > $@
	@if [ -n "$(IPINFO_TOKEN)" ]; then echo 'IPINFO_TOKEN="$(IPINFO_TOKEN)"' >> $@; fi
	@grep -q '^APP_NAME=..*' $@ || (echo "APP_NAME was not written to .env"; rm -f $@; exit 1)
	@grep -q '^SERVER_PORT=..*' $@ || (echo "SERVER_PORT was not written to .env"; rm -f $@; exit 1)

configure: .env ## Generate .env for the container (alias)

docker-compose.yml: docker-compose.template .env ## Generate docker-compose.yml from the template
	@docker compose version > /dev/null || (echo "Needs Docker Compose, see $(DOCKER_COMPOSE_URL)"; exit 1)
	rm -f $@
	cp docker-compose.template $@

docker: docker-compose.yml ## Generate docker-compose.yml (alias)

build: docker-compose.yml ## Build the application container image
	@docker -v
	docker compose build --progress plain

push: build ## Push the built image to Docker Hub
	@docker compose images
	docker compose push

data/: ## Create ./data/ owned by USER_ID:GROUP_ID (runs only if missing)
	mkdir -p ./data/
	sudo chown $(USER_ID):$(GROUP_ID) ./data/
	sudo chmod 755 ./data/
	sudo chmod g+rws ./data/
	sudo rm -f ./data/app-std* ./data/cron-std* ./data/supervisor.sock

datadir: data/ ## Set up ./data/ (alias)

run: data/ build ## Run the app container in the foreground
	@test -f docker-compose.yml
	docker compose up

rund: data/ build ## Run the app container detached
	@test -f docker-compose.yml
	docker compose up -d

connect: ## Open a shell in the running container
	@test -f docker-compose.yml
	./connect_to_app.sh $(DOCKER_APP)

clean: ## Remove generated files and caches
	rm -rf .pytest_cache build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f .env docker-compose.yml

# ---------- systemd (native deployment) ----------

# The unit is rendered for *this* checkout (APP_DIR defaults to $(CURDIR)), so
# `make install-unit` from the deployment directory needs no editing. APP_DIR can
# be overridden to inspect a render for another path, but install from the clone
# the unit points at.
APP_DIR := $(CURDIR)
UNIT_NAME := $(PROJECT_NAME).service
UNIT_TEMPLATE := $(UNIT_NAME).in
UNIT_PATH := /etc/systemd/system/$(UNIT_NAME)

build/:
	mkdir -p $@

build/$(UNIT_NAME): $(UNIT_TEMPLATE) Makefile | build/
	sed -e 's|@APP_DIR@|$(APP_DIR)|g' \
	    -e 's|@APP_NAME@|$(PROJECT_NAME)|g' \
	    -e 's|@SERVER_ADDRESS@|$(SERVER_ADDRESS)|g' \
	    -e 's|@SERVER_PORT@|$(SERVER_PORT)|g' \
	    $< > $@

unit: build/$(UNIT_NAME) ## Render the systemd unit for this checkout into build/

install-unit: build/$(UNIT_NAME) ## Install, enable and (re)start the systemd unit (sudo, offline-safe)
	@test -d /run/systemd/system || (echo "systemd is not running here; run this on the host."; exit 1)
	@test -x $(APP_DIR)/.venv/bin/app || (echo "$(APP_DIR)/.venv/bin/app is missing; run 'make sync' first (needs network)."; exit 1)
	sudo install -m 0644 -o root -g root $< $(UNIT_PATH)
	sudo systemctl daemon-reload
	sudo systemctl enable $(UNIT_NAME)
	sudo systemctl restart $(UNIT_NAME)
	@sudo systemctl --no-pager --lines=0 is-active $(UNIT_NAME)

uninstall-unit: ## Stop, disable and remove the installed unit and its drop-ins (sudo)
	@test -d /run/systemd/system || (echo "systemd is not running here; run this on the host."; exit 1)
	-sudo systemctl disable --now $(UNIT_NAME)
	sudo rm -f $(UNIT_PATH)
	@if [ -d $(UNIT_PATH).d ]; then \
	  echo "Removing drop-ins in $(UNIT_PATH).d/"; \
	  sudo rm -rf $(UNIT_PATH).d; \
	fi
	sudo systemctl daemon-reload
	-sudo systemctl reset-failed $(UNIT_NAME)
	@test ! -e $(UNIT_PATH) || (echo "$(UNIT_PATH) still exists."; exit 1)
	@ss -ltn | grep -q ":$(SERVER_PORT) " \
	  && echo "WARNING: something still listens on port $(SERVER_PORT)." \
	  || echo "$(UNIT_NAME) removed; port $(SERVER_PORT) is free."
	@echo "Reinstall with: make install-unit"

status: ## Show the unit state and whether the probe port is listening
	@systemctl --no-pager --lines=5 status $(UNIT_NAME) || true
	@ss -ltn | grep -q ":$(SERVER_PORT) " \
	  && echo "port $(SERVER_PORT) is listening." \
	  || echo "port $(SERVER_PORT) is NOT listening."

logs: ## Follow the service journal
	sudo journalctl -u $(UNIT_NAME) -f

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_\/.-]+:.*## / {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

