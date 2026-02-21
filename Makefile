.PHONY: start stop restart build rebuild logs shell clean clean-jobs reset-db help deploy status

# Deployment configuration
DEPLOY_HOST ?= gassh
DEPLOY_DIR ?= ~/shandy
COMPOSE_FILE ?= docker-compose.yml

# Default target
help:
	@echo "SHANDY - Makefile commands"
	@echo ""
	@echo "Docker:"
	@echo "  make build      - Build all Docker images (base, main, agent, executor)"
	@echo "  make start      - Start containers"
	@echo "  make stop       - Stop containers"
	@echo "  make restart    - Restart containers (no rebuild)"
	@echo "  make rebuild    - Rebuild images and restart"
	@echo "  make logs       - Tail container logs"
	@echo "  make shell      - Open shell in main container"
	@echo "  make clean      - Remove containers and volumes"
	@echo "  make reset-db   - Flush database and run migrations"
	@echo ""
	@echo "Deployment:"
	@echo "  make deploy     - Deploy to production server"

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
	@echo "Building base image (Python, uv)..."
	DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build -f Dockerfile.base -t shandy-base:latest .
	@echo "Building SHANDY main image..."
	DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose -f $(COMPOSE_FILE) build \
		--build-arg SHANDY_COMMIT=$$(git rev-parse --short HEAD 2>/dev/null || echo "unknown") \
		--build-arg BUILD_TIME=$$(date -u +%Y-%m-%dT%H:%M:%SZ)
	@echo "Building executor image..."
	DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build -f Dockerfile.executor -t shandy-executor:latest .
	@echo "Building agent image..."
	DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build -f Dockerfile.agent -t shandy-agent:latest .
	@echo "All images built: shandy-base, shandy, shandy-executor, shandy-agent"

rebuild: build
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

reset-db:
	@echo "WARNING: This will delete all database data!"
	@read -p "Are you sure? [y/N]: " confirm; \
	if [ "$$confirm" = "y" ] || [ "$$confirm" = "Y" ]; then \
		echo "Stopping containers and removing volumes..."; \
		docker compose -f $(COMPOSE_FILE) down -v; \
		echo "Starting containers..."; \
		docker compose -f $(COMPOSE_FILE) up -d; \
		echo "Waiting for postgres to be ready..."; \
		until docker compose -f $(COMPOSE_FILE) exec postgres pg_isready -U shandy -d shandy 2>/dev/null; do \
			sleep 1; \
		done; \
		echo "Running migrations..."; \
		docker compose -f $(COMPOSE_FILE) exec shandy alembic upgrade head; \
		echo "Database reset complete!"; \
	else \
		echo "Aborted."; \
	fi

# Show job status
status:
	@echo "Job status:"
	docker compose -f $(COMPOSE_FILE) exec shandy python -m shandy.job_manager summary

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
	@ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && make rebuild"
	@echo ""
	@echo "Step 4: Running database migrations on $(DEPLOY_HOST)..."
	@ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose exec shandy alembic upgrade head"
	@echo ""
	@echo "========================================="
	@echo "Deployment complete!"
	@echo "Application should be running at https://chat.alzassistant.org"
	@echo "========================================="
