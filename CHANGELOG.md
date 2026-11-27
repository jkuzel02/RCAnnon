# Changelog

All notable changes to RCAnnon are documented in this file.

Entries are generated automatically from [Conventional Commits](https://www.conventionalcommits.org/) via Release Please when versioned Release PRs are merged into `main`. Version tags (`X.Y.Z`) strictly match `[project].version` in `pyproject.toml`.

## [0.1.8](https://github.com/jkuzel02/RCAnnon/compare/0.1.7...0.1.8) (2026-11-27)


### Features

* simplify first use and make LibreChat optional ([966cdf0](https://github.com/jkuzel02/RCAnnon/commit/966cdf037872779d00df133124e0d5b6851c44be))

## [0.1.7](https://github.com/jkuzel02/RCAnnon/compare/0.1.6...0.1.7) (2026-11-27)


### Bug Fixes

* automate prompt checks under the test suite ([4b9894e](https://github.com/jkuzel02/RCAnnon/commit/4b9894e00b019de5ed6808efbd9cd76a58887e7c))
* automate prompt checks under the test suite ([610dfa0](https://github.com/jkuzel02/RCAnnon/commit/610dfa08e1a3b65d747a1a49b25cca6ae3c40780)), closes [#77](https://github.com/jkuzel02/RCAnnon/issues/77)
* show raw JSON in metrics prompt examples ([a6b4713](https://github.com/jkuzel02/RCAnnon/commit/a6b47134ab8ab647fb9b86f3f0444de746dc14fb)), closes [#77](https://github.com/jkuzel02/RCAnnon/issues/77)

## [0.1.6](https://github.com/jkuzel02/RCAnnon/compare/0.1.5...0.1.6) (2026-11-26)


### Features

* **k8s:** auto-wire read-only Kind kubeconfig and Compose override in bootstrap.sh ([73c9741](https://github.com/jkuzel02/RCAnnon/commit/73c9741c700b21ea851ae00c165bb9597ea3bcab))
* **k8s:** auto-wire read-only Kind kubeconfig and Compose override in bootstrap.sh ([05f4009](https://github.com/jkuzel02/RCAnnon/commit/05f4009ce3aba2e5373ca78dce06ca23f5ab7391)), closes [#68](https://github.com/jkuzel02/RCAnnon/issues/68)

## [0.1.5](https://github.com/jkuzel02/RCAnnon/compare/0.1.4...0.1.5) (2026-11-26)


### Features

* **compose:** simplify quick start with zero-touch docker compose up and Kind guide ([79dc1f6](https://github.com/jkuzel02/RCAnnon/commit/79dc1f612854f4e65d7c85a1ca2c95dc15dc0a09))
* **compose:** simplify quick start with zero-touch docker compose up and Kind guide ([c5f5e39](https://github.com/jkuzel02/RCAnnon/commit/c5f5e39011f3fcfe3b544ddadc7a602ac32eb452)), closes [#65](https://github.com/jkuzel02/RCAnnon/issues/65)

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
