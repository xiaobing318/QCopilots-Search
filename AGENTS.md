# QCopilots Search development

- Application code is C++20 and Qt Widgets. All owned targets, programs, tests, packages and scripts use the `qcopilots-search` prefix. C++ namespace is `qcopilots_search`. Configuration variables use `QCOPILOTS_SEARCH_`.
- Never write generated build, dependency, test, model or package files into the source tree. Build roots and SDK roots are configurable. No developer-specific absolute paths in reusable code.
- Tests use unique QTemporaryDir roots and explicit profile paths. Never read or modify a real user profile in tests. Stop child processes and close files before cleanup.
- This checkout is shared by parallel agents. Edit only your assigned module. Do not commit, switch branches, push, or overwrite other agents' files. Communicate interface changes before making them.
- Keep the application MIT notice and preserve attribution for adapted DigUp code. Third-party dependencies retain their licenses.
- Implement real behavior and explicit errors. Do not return fake embeddings or silently mark unsupported operations as completed.
- Documentation is primarily Chinese, with minimal semicolons. Reports distinguish executed tests from planned or unavailable tests.
