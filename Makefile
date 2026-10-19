.PHONY: install test lint fmt demo

install:
	pip install -r requirements-dev.txt

test:
	pytest --cov=app --cov-report=term-missing

lint:
	ruff check .
	terraform fmt -check -recursive infra

fmt:
	ruff check . --fix
	terraform fmt -recursive infra

demo:
	./scripts/demo.sh
