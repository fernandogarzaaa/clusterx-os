# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Dependabot config for pip and GitHub Actions (weekly).
- requirements.lock with fully pinned transitive dependencies.
- .gitignore for caches, virtualenvs, local .env files and runtime data/.
- SECURITY.md with private reporting contact.
- This changelog.

### Fixed
- Calendar slot test no longer fails when CI runs on a Sunday (clashed with the seeded 'tomorrow 10:00' demo appointment).
