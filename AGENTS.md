# QCopilots Search development

- Application code is C++20 and Qt Widgets. All owned targets, programs, tests, packages and scripts use the `qcs` prefix. C++ namespace is `qcopilots_search`. Configuration variables use `QCOPILOTS_SEARCH_`.
- Never write generated build, dependency, test, model or package files into the source tree. Build roots and SDK roots are configurable. No developer-specific absolute paths in reusable code.
- Tests use unique QTemporaryDir roots, per-invocation working directories and explicit profile paths. Respect QCOPILOTS_SEARCH_TEST_ROOT. Fail explicitly when creation or cleanup fails. Never read or modify a real user profile in tests. Stop child processes and close files before cleanup.
- This checkout is shared by parallel agents. Edit only your assigned module. Subagents must not commit, switch branches, push, or overwrite other agents' files. The coordinating agent is authorized to commit and push codex/qcopilots-search-v1 for this implementation and its CI acceptance. Communicate interface changes before making them.
- Keep the application MIT notice and preserve attribution for adapted DigUp code. Third-party dependencies retain their licenses.
- Implement real behavior and explicit errors. Do not return fake embeddings or silently mark unsupported operations as completed.
- Documentation is primarily Chinese, with minimal semicolons. Reports distinguish executed tests from planned or unavailable tests.

- Keep the display name QCopilots Search, third-party names and stable non-path model/protocol identifiers unchanged. The canonical repository name is QCopilots-Search. The GUI target and executable are qcs-gui, while the product prefix, profile paths and other qcs programs remain stable. Do not add old command aliases.
- A candidate is complete only after local automated tests, all four native GitHub Actions jobs, Windows Computer Use scenarios and required capacity checks pass for the same immutable source and package identities. Triggered or skipped checks are not passes.
