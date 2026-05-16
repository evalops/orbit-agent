PY?=python3
VENV?=venv

.PHONY: bootstrap bootstrap-prod test lint fmt ci chat config hooks bazel-format bazel-mod-tidy bazel-check bazel-test bazel-test-remote bazel-rbe-smoke clean

bootstrap:
	@bash scripts/bootstrap_venv.sh dev $(PY)

bootstrap-prod:
	@bash scripts/bootstrap_venv.sh prod $(PY)

test:
	@$(VENV)/bin/python -m pytest -q

lint:
	@$(VENV)/bin/ruff check .

fmt:
	@$(VENV)/bin/black .

ci: lint test

chat:
	@$(VENV)/bin/python -m orbit_agent.cli chat

config:
	@$(VENV)/bin/python -m orbit_agent.cli config-info

hooks:
	@$(VENV)/bin/pre-commit install && echo "pre-commit hooks installed"

bazel-format:
	buildifier BUILD.bazel bazel/platforms/BUILD.bazel

bazel-mod-tidy:
	bazelisk mod tidy

bazel-test:
	bazelisk test //:pytest

bazel-test-remote:
	bazelisk test //:pytest --config=remote-gcp-dev

bazel-rbe-smoke:
	scripts/run-bazel-rbe.sh test //:pytest

bazel-check: bazel-format bazel-mod-tidy bazel-test

clean:
	rm -rf $(VENV) __pycache__ .pytest_cache
	find . -name "*.pyc" -delete
eval-personas:
	@$(VENV)/bin/python -m orbit_agent.cli eval run --dataset evals/scenarios_personas.yaml --out .orbit/evals/personas.jsonl
	@$(VENV)/bin/python -m orbit_agent.cli eval report .orbit/evals/personas.jsonl
	@$(VENV)/bin/python -m orbit_agent.cli eval summary --input-path .orbit/evals/personas.jsonl --csv-out reports/personas.csv --md-out reports/personas.md
