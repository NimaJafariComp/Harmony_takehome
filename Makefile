.PHONY: demo format-check lint llm-smoke migrate seed test test-critical typecheck verify

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

seed: migrate
	docker compose --profile tools run --build --rm app enterprise-agent reset
	docker compose --profile tools run --build --rm app enterprise-agent seed

demo: migrate
	docker compose --profile tools run --build --rm app enterprise-agent demo --unattended

llm-smoke:
	LLM_PROFILE="$(LLM_PROFILE)" uv run enterprise-agent llm-smoke
