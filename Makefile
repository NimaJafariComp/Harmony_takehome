.PHONY: migrate

migrate:
	docker compose up --wait db
	docker compose --profile tools run --rm app alembic upgrade head
