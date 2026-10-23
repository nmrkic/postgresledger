SHELL := /bin/bash

COMPOSE = docker compose -f docker/docker-compose.yml
PSQL = PGPASSWORD=postgres psql -X -q -v ON_ERROR_STOP=1 -h localhost -p 55432 -U postgres

# pgl works on one database: DATABASE_* from .env (dev). ENV=stage|prod
# points one run at another database on the same local server instead.
ENVS = dev stage prod
PGL = $(if $(ENV),DATABASE_NAME=$(ENV) )uv run --quiet pgl

.PHONY: help up down reset seed setup test baseline new apply apply-all status status-all drift drift-all capture snapshot snapshot-all compare

help:
	@echo "  make setup                   - uv sync, download pgschema + scratch Postgres (~/.cache/postgresledger), check psql"
	@echo "  make test                    - pytest (integration tests need make up)"
	@echo
	@echo "Local Postgres (docker/):"
	@echo "  make up                      - start postgres:17 on localhost:55432 (dev, stage, prod)"
	@echo "  make down                    - stop it and delete the volume"
	@echo "  make seed                    - load the demo schema + data into dev"
	@echo "  make reset                   - down + up + seed, delete schema.sql and migrations"
	@echo
	@echo "Migrations (pgl, one database: .env = dev, or ENV=stage|prod):"
	@echo "  make baseline [ENV=<env>]    - snapshot the database -> schema.sql + 0001_baseline.sql"
	@echo "  make new NAME=<name>         - migration from the changes in database/schema/schema.sql"
	@echo "  make apply [ENV=<env>]       - apply pending migrations (DRY=1 for dry run)"
	@echo "  make status [ENV=<env>]      - applied / pending"
	@echo "  make drift [ENV=<env>]       - detect manual changes"
	@echo "  make capture NAME=<name> [ENV=<env>] - manual changes -> migration, recorded as captured there"
	@echo "  make snapshot [ENV=<env>]    - database/snapshots/<env>.sql"
	@echo "  make compare A=<a> [B=<b>]   - differences between two snapshots"
	@echo "  make apply-all / status-all / drift-all / snapshot-all - the same on dev, stage and prod"

up:
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down -v

reset: down up seed
	rm -rf database/migrations database/schema database/snapshots

seed:
	$(PSQL) -d dev -f database/seed/01_schema.sql -f database/seed/02_data.sql

setup:
	uv sync
	uv run --quiet pgl setup

test:
	uv run --quiet pytest -q

baseline:
	$(PGL) baseline

new:
	@test -n "$(NAME)" || (echo "NAME=<name> is required" && exit 1)
	$(PGL) new $(NAME)

apply:
	$(PGL) apply $(if $(DRY),--dry-run)

status:
	$(PGL) status

drift:
	$(PGL) drift

capture:
	@test -n "$(NAME)" || (echo "NAME=<name> is required" && exit 1)
	$(PGL) capture $(NAME)

snapshot:
	$(PGL) snapshot $(or $(ENV),dev)

compare:
	@test -n "$(A)" || (echo "A=<snapshot> is required" && exit 1)
	$(PGL) compare $(A) $(B)

# the tool knows one database; looping over the local ones is this repository's convenience
apply-all status-all drift-all snapshot-all:
	@rc=0; for e in $(ENVS); do echo "== $$e"; $(MAKE) --no-print-directory $(@:-all=) ENV=$$e || rc=1; done; exit $$rc

# personal targets that stay out of git (Makefile.local is git-ignored)
-include Makefile.local
