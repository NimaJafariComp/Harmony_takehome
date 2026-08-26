.PHONY: demo demo-list format-check guide lint llm-smoke migrate seed status test test-critical tui typecheck usage verify

guide:
	uv run enterprise-agent guide

demo-list:
	uv run enterprise-agent demo --list

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

tui: migrate
	mkdir -p .enterprise-agent
	chmod 700 .enterprise-agent
	docker compose --profile tools run --rm app enterprise-agent

status: migrate
	docker compose --profile tools run --rm app enterprise-agent status

usage: migrate
	docker compose --profile tools run --rm app enterprise-agent llm-usage

llm-smoke:
	LLM_PROFILE="$(LLM_PROFILE)" uv run enterprise-agent llm-smoke
