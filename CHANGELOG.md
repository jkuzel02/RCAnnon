# Changelog

All notable changes to RCAnnon are documented in this file.

Entries are generated automatically from [Conventional Commits](https://www.conventionalcommits.org/) via Release Please when versioned Release PRs are merged into `main`. Version tags (`X.Y.Z`) strictly match `[project].version` in `pyproject.toml`.

## [0.1.4](https://github.com/jkuzel02/RCAnnon/compare/0.1.3...0.1.4) (2026-11-26)


### Features

* **provider:** use OpenAI API directly across provider, compose, evaluation, and docs ([4dd04e8](https://github.com/jkuzel02/RCAnnon/commit/4dd04e82515bb4deff37d77673385574150dec70))
* **provider:** use OpenAI API directly across provider, compose, evaluation, and docs ([9d293c8](https://github.com/jkuzel02/RCAnnon/commit/9d293c86a4b33af3ab3a9751c264907624ab5d96)), closes [#62](https://github.com/jkuzel02/RCAnnon/issues/62)

## [0.1.3](https://github.com/jkuzel02/RCAnnon/compare/0.1.2...0.1.3) (2026-11-26)


### Features

* **ui:** integrate LibreChat v0.8.9 and idempotent admin bootstrap ([415a573](https://github.com/jkuzel02/RCAnnon/commit/415a5737756f71b2c7e9f1f5581814d57567046b))

## [0.1.2](https://github.com/jkuzel02/RCAnnon/compare/0.1.1...0.1.2) (2026-11-26)


### Features

* **workflows:** validate MAS evidence claims and partial-source failures ([2095108](https://github.com/jkuzel02/RCAnnon/commit/2095108f806eb40f6b1ed07e0c9539d69cb88b0d))
* **workflows:** validate MAS evidence claims and partial-source failures ([7d75d17](https://github.com/jkuzel02/RCAnnon/commit/7d75d172f013eed925af19863079a86ce806dc6b)), closes [#11](https://github.com/jkuzel02/RCAnnon/issues/11)

## [0.1.1](https://github.com/jkuzel02/RCAnnon/compare/0.1.0...0.1.1) (2026-11-23)


### Documentation

* add Getting Started guide with architecture visualization ([e6a4a23](https://github.com/jkuzel02/RCAnnon/commit/e6a4a23554735be235fa50280449151511afd59f))
* add Getting Started guide with architecture visualization ([5d04e61](https://github.com/jkuzel02/RCAnnon/commit/5d04e61fca0d07a04d9b5d49bf86748b650a1db5)), closes [#41](https://github.com/jkuzel02/RCAnnon/issues/41)
* add overview, architecture diagram, and doc links to README ([368f049](https://github.com/jkuzel02/RCAnnon/commit/368f04902bb651b492a05d27854a11992b9301af)), closes [#41](https://github.com/jkuzel02/RCAnnon/issues/41)

## [0.1.0] - Initial Baseline

- Initial `0.1.0` development baseline implementing canonical `v1` contracts, e-INFRA CZ provider adapter, `base`/`optimal`/`mas` LangGraph workflows, read-only Kubernetes evidence collector, scoped FastAPI/OpenAI facade, and Docker Compose packaging.
