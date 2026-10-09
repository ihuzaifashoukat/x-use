# Roadmap

The 3.0.0 release candidate centers on reviewed X outreach, structured inbox
management and resilient local sessions. Release readiness depends on the CI
results for the exact commit and maintainer review. This roadmap records scope,
not a promise of platform compatibility or scheduled delivery.

## Implemented in the 3.0.0 candidate

- Async Patchright account contexts with ownership locks, bounded capacity,
  configured proxy routes and process cleanup.
- Seventy MCP tools, seven prompts, four read-only resources and six skills.
- Structured profile, post, thread, media, inbox and notification reads, with
  partial coverage and unknown evidence represented explicitly.
- Reviewed messaging, follows and multi-post threads, durable action journals,
  recovery identities, local leads, campaigns and opt-out handling.
- Stable private data roots, one-command uv setup, dependency locking,
  diagnostics and a Claude Code plugin.
- Cross-platform Python CI, installed-wheel browser fixtures, container checks
  and release publishing gates.

See [the changelog](CHANGELOG.md) for 2.x history and 3.0.0 migration notes.
Working Selenium batch entry points remain for compatibility and are labeled
separately from the current MCP runtime.

## Next priorities

- Expand supported X layouts using observed DOM evidence and reproducible
  synthetic tests, including analytics dashboards available to eligible accounts.
- Improve bounded history traversal while preserving stable message identity,
  partial-result reporting and exact conversation context.
- Add optional video/audio understanding without treating thumbnails as full
  content or requiring media services for ordinary text workflows.
- Build a local dashboard for account health, drafts, queues and recovery.
- Evaluate distributed account ownership for multi-host deployments; current
  locks coordinate only one host.
- Add operational metrics/export and scheduler integration where they support
  explicit operator workflows.
- Continue installation and dependency maintenance across supported platforms.

Public write operations, account credentials and uncertain outcomes require
clear boundaries in every addition. Features should not promise undetectability
or automatic bypass of login challenges or platform restrictions.

## Contributions

Useful contributions include current browser selector regressions, MCP contract
improvements, configuration examples, platform fixes, and clearer recovery
documentation. Follow [CONTRIBUTING.md](CONTRIBUTING.md); discuss substantial
behavior changes before implementation.

## Versioning and release preparation

The version source is `src/xuse/__init__.py`. Update registry/plugin product
versions, `uv.lock` and release notes alongside it; the distribution derives
its version from that source. Verify the exact branch commit through CI, review
the changes, merge when authorized, and only then create the matching release
tag and publish the GitHub release. The publishing workflow reruns verification
before uploading to PyPI and the MCP Registry.