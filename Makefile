.PHONY: demo format-check lint migrate seed test test-critical typecheck verify

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

typecheck:
	uv run mypy

test:
	uv run pytest --cov=enterprise_agent

test-critical:
	uv run pytest -m critical

verify:
	$(MAKE) format-check
	$(MAKE) lint
	$(MAKE) typecheck
	$(MAKE) test
	$(MAKE) migrate
	$(MAKE) demo

migrate:
	docker compose up --wait db
	docker compose --profile tools run --build --rm app alembic upgrade head

seed:
	@echo "Seed command is not available until M2."

demo:
	@echo "Demo command is not available until M8."
