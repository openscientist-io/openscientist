.PHONY: start stop restart build rebuild logs shell clean clean-jobs test help

# Default target
help:
	@echo "SHANDY - Makefile commands"
	@echo ""
	@echo "Available targets:"
	@echo "  make start       - Start the Docker container"
	@echo "  make stop        - Stop the Docker container"
	@echo "  make restart     - Restart the Docker container (without rebuilding)"
	@echo "  make build       - Build the Docker image"
	@echo "  make rebuild     - Rebuild image and restart (use after code changes)"
	@echo "  make logs        - Tail container logs"
	@echo "  make shell       - Open a shell in the running container"
	@echo "  make clean       - Remove container and volumes"
	@echo "  make clean-jobs  - Clean up old job directories"
	@echo "  make test        - Run tests in container"
	@echo ""
	@echo "Jobs are stored in: ./jobs/"
	@echo "Web interface at: http://localhost:8080"

start:
	@echo "Starting SHANDY..."
	docker-compose up -d
	@echo "SHANDY started at http://localhost:8080"

stop:
	@echo "Stopping SHANDY..."
	docker-compose down
	@echo "SHANDY stopped"

restart: stop start

build:
	@echo "Building SHANDY Docker image..."
	docker-compose build

rebuild: build
	@echo "Restarting with new build..."
	docker-compose down
	docker-compose up -d
	@echo "SHANDY rebuilt and started at http://localhost:8080"

logs:
	@echo "Tailing SHANDY logs (Ctrl+C to exit)..."
	docker-compose logs -f

shell:
	@echo "Opening shell in SHANDY container..."
	docker-compose exec shandy /bin/bash

clean:
	@echo "Removing containers and volumes..."
	docker-compose down -v
	@echo "Cleaned up"

clean-jobs:
	@echo "Cleaning up old job directories..."
	@read -p "Delete jobs older than how many days? [7]: " days; \
	days=$${days:-7}; \
	docker-compose exec shandy python -m shandy.job_manager cleanup --days $$days
	@echo "Job cleanup complete"

test:
	@echo "Running tests in container..."
	docker-compose exec shandy pytest

# Development helpers
dev-install:
	@echo "Installing development dependencies locally..."
	uv sync

dev-test:
	@echo "Running tests locally..."
	uv run pytest

lint:
	@echo "Linting code..."
	uv run ruff check src/

format:
	@echo "Formatting code..."
	uv run ruff format src/

# Show job status
status:
	@echo "Job status:"
	docker-compose exec shandy python -m shandy.job_manager summary
