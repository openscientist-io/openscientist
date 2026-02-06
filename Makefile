.PHONY: start stop restart build build-no-cache rebuild rebuild-no-cache logs shell clean clean-jobs test help deploy dev-start dev-stop dev-restart dev-rebuild lint format typecheck

# Deployment configuration
DEPLOY_HOST ?= gassh
DEPLOY_DIR ?= ~/shandy
COMPOSE_FILE ?= docker-compose.yml

# Default target
help:
	@echo "SHANDY - Makefile commands"
	@echo ""
	@echo "Production:"
	@echo "  make start            - Start the Docker container"
	@echo "  make stop             - Stop the Docker container"
	@echo "  make restart          - Restart the Docker container (without rebuilding)"
	@echo "  make build            - Build the Docker image (with cache)"
	@echo "  make build-no-cache   - Build the Docker image (without cache, for dependency updates)"
	@echo "  make rebuild          - Rebuild image and restart (use after code changes, with cache)"
	@echo "  make rebuild-no-cache - Rebuild image and restart (without cache)"
	@echo "  make logs             - Tail container logs"
	@echo "  make shell            - Open a shell in the running container"
	@echo "  make clean            - Remove container and volumes"
	@echo "  make clean-jobs       - Clean up old job directories"
	@echo "  make test             - Run tests in container"
	@echo "  make deploy           - Deploy to production server (default: gassh, with cache)"
	@echo ""
	@echo "Development (with live code reload):"
	@echo "  make dev-start        - Start in development mode (source mounted, auto-reload)"
	@echo "  make dev-stop         - Stop development container"
	@echo "  make dev-restart      - Restart development container"
	@echo "  make dev-rebuild      - Rebuild and restart in development mode"
	@echo ""
	@echo "Local development:"
	@echo "  make dev-test         - Run tests locally with coverage"
	@echo "  make lint             - Lint src/ and tests/ with ruff"
	@echo "  make format           - Format src/ and tests/ with ruff"
	@echo "  make typecheck        - Type check with mypy"
	@echo ""
	@echo "Jobs are stored in: ./jobs/"
	@echo "Web interface at: http://localhost:8080"

start:
	@echo "Starting SHANDY..."
	docker compose -f $(COMPOSE_FILE) up -d
	@echo "SHANDY started at http://localhost:8080"

stop:
	@echo "Stopping SHANDY..."
	docker compose -f $(COMPOSE_FILE) down
	@echo "SHANDY stopped"

restart: stop start

build:
	@echo "Building SHANDY Docker image (with cache)..."
	DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose -f $(COMPOSE_FILE) build \
		--build-arg SHANDY_COMMIT=$$(git rev-parse --short HEAD 2>/dev/null || echo "unknown") \
		--build-arg BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ)

build-no-cache:
	@echo "Building SHANDY Docker image (without cache)..."
	DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose -f $(COMPOSE_FILE) build --no-cache \
		--build-arg SHANDY_COMMIT=$$(git rev-parse --short HEAD 2>/dev/null || echo "unknown") \
		--build-arg BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ)

rebuild: build
	@echo "Restarting with new build..."
	docker compose -f $(COMPOSE_FILE) down
	docker compose -f $(COMPOSE_FILE) up -d
	@echo "SHANDY rebuilt and started at http://localhost:8080"

rebuild-no-cache: build-no-cache
	@echo "Restarting with new build (no cache)..."
	docker compose -f $(COMPOSE_FILE) down
	docker compose -f $(COMPOSE_FILE) up -d
	@echo "SHANDY rebuilt and started at http://localhost:8080"

logs:
	@echo "Tailing SHANDY logs (Ctrl+C to exit)..."
	docker compose -f $(COMPOSE_FILE) logs -f

shell:
	@echo "Opening shell in SHANDY container..."
	docker compose -f $(COMPOSE_FILE) exec shandy /bin/bash

clean:
	@echo "Removing containers and volumes..."
	docker compose -f $(COMPOSE_FILE) down -v
	@echo "Cleaned up"

clean-jobs:
	@echo "Cleaning up old job directories..."
	@read -p "Delete jobs older than how many days? [7]: " days; \
	days=$${days:-7}; \
	docker compose -f $(COMPOSE_FILE) exec shandy python -m shandy.job_manager cleanup --days $$days
	@echo "Job cleanup complete"

test:
	@echo "Running tests in container..."
	docker compose -f $(COMPOSE_FILE) exec shandy pytest

# Development helpers
dev-install:
	@echo "Installing development dependencies locally..."
	uv sync

dev-test:
	@echo "Running tests locally..."
	uv run pytest --cov=src/shandy --cov-report=term-missing

lint:
	@echo "Linting code..."
	uv run ruff check src/ tests/

format:
	@echo "Formatting code..."
	uv run ruff format src/ tests/

typecheck:
	@echo "Type checking..."
	uv run mypy src/shandy/ tests/

# Show job status
status:
	@echo "Job status:"
	docker compose -f $(COMPOSE_FILE) exec shandy python -m shandy.job_manager summary

# Development mode commands
dev-start:
	@echo "Starting SHANDY in development mode (with live reload)..."
	docker compose -f $(COMPOSE_FILE) -f docker-compose.dev.yml up -d
	@echo "SHANDY development mode started at http://localhost:8080"
	@echo "Source code is mounted - changes will auto-reload!"

dev-stop:
	@echo "Stopping SHANDY development container..."
	docker compose -f $(COMPOSE_FILE) -f docker-compose.dev.yml down
	@echo "Development container stopped"

dev-restart: dev-stop dev-start

dev-rebuild:
	@echo "Rebuilding SHANDY for development mode..."
	DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose -f $(COMPOSE_FILE) -f docker-compose.dev.yml build \
		--build-arg SHANDY_COMMIT=$$(git rev-parse --short HEAD 2>/dev/null || echo "unknown") \
		--build-arg BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ)
	@echo "Restarting with new build..."
	docker compose -f $(COMPOSE_FILE) -f docker-compose.dev.yml down
	docker compose -f $(COMPOSE_FILE) -f docker-compose.dev.yml up -d
	@echo "SHANDY rebuilt and started in development mode at http://localhost:8080"

dev-logs:
	@echo "Tailing SHANDY development logs (Ctrl+C to exit)..."
	docker compose -f $(COMPOSE_FILE) -f docker-compose.dev.yml logs -f

# Deploy to production server
deploy:
	@echo "========================================="
	@echo "Deploying SHANDY to $(DEPLOY_HOST)"
	@echo "========================================="
	@echo ""
	@echo "Step 1: Ensuring repository exists on $(DEPLOY_HOST)..."
	@ssh $(DEPLOY_HOST) "if [ ! -d $(DEPLOY_DIR)/.git ]; then \
		echo 'ERROR: Repository not found at $(DEPLOY_DIR)'; \
		echo 'Please clone the repository first:'; \
		echo '  ssh $(DEPLOY_HOST)'; \
		echo '  git clone <your-repo-url> $(DEPLOY_DIR)'; \
		exit 1; \
	else \
		echo 'Repository exists, pulling latest changes...'; \
		cd $(DEPLOY_DIR) && git pull; \
	fi"
	@echo ""
	@echo "Step 2: Checking .env configuration on $(DEPLOY_HOST)..."
	@ssh $(DEPLOY_HOST) "if [ ! -f $(DEPLOY_DIR)/.env ]; then \
		echo 'WARNING: .env does not exist on server!'; \
		echo 'You need to create it manually:'; \
		echo '  1. ssh $(DEPLOY_HOST)'; \
		echo '  2. cd $(DEPLOY_DIR) && cp .env.example .env'; \
		echo '  3. Edit .env with production values (ANTHROPIC_AUTH_TOKEN, etc.)'; \
		echo 'Deployment will continue, but app will not start without .env'; \
	else \
		echo '.env already exists - preserving existing configuration'; \
	fi"
	@echo ""
	@echo "Step 3: Building and restarting application on $(DEPLOY_HOST)..."
	@ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && COMPOSE_FILE=docker-compose.gassh.yml make rebuild"
	@echo ""
	@echo "========================================="
	@echo "Deployment complete!"
	@echo "Application should be running at https://chat.alzassistant.org"
	@echo "========================================="
