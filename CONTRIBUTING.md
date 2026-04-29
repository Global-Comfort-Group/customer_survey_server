# Contributing

Thanks for your interest in improving `customer_survey_server`. This guide
covers the local workflow we use for branches, commits, tests, and pull
requests.

## Branch naming

Create a topic branch off `main` using one of the following prefixes:

- `feature/<slug>` — new functionality
- `fix/<slug>` — bug fixes
- `chore/<slug>` — tooling, infra, dependency, or housekeeping changes
- `test/<slug>` — adding or refactoring tests

Use lowercase, dash-separated slugs. Examples:

```
feature/department-aggregates
fix/resend-invite-headers
chore/upgrade-fastapi
test/distribution-dedup
```

## Commit messages

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional-scope>): <subject>

<optional body explaining the why>
```

Common types: `feat`, `fix`, `chore`, `refactor`, `test`, `docs`, `perf`.

Examples:

```
feat(distribution): per-recipient invite tokens
fix(email): drop List-Unsubscribe header to stay out of Promotions tab
chore(infra): tighten .gitignore and untrack business documents
```

Keep the subject under ~72 characters and use the imperative mood.

## Running tests

Install the dev dependencies and run pytest from the repo root:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

The tests use an in-memory SQLite fixture (see `app/tests/conftest.py`), so no
local PostgreSQL instance is required to run the suite.

Please make sure `pytest` is green before opening a pull request, and add or
update tests for any behaviour change.

## Pull requests

1. Push your branch to GitHub.
2. Open a pull request against `main`.
3. Fill out the PR template — describe the change, link any related issue,
   and complete the test-plan checklist.
4. Keep the PR focused; prefer several small PRs over one large one.
5. Address review comments by pushing follow-up commits (we squash on merge).
