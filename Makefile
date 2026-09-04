.PHONY: help install lint format check test migrate run docker-up docker-down clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:
	uv sync --all-extras --dev
	uv run pre-commit install

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

check: format lint

test:
	PYTHONPATH=src uv run pytest --ignore=claude_folder tests src -v

migrate:
	PYTHONPATH=src uv run python migrate_add_message_count.py
	PYTHONPATH=src uv run python migrate_add_lead_status.py

run:
	uv run uvicorn src.interface.webhook_app:app --host 0.0.0.0 --port 8000 --reload

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
