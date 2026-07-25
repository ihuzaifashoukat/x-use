# Contributing to x-use

First off, thank you for considering contributing to x-use (formerly twitter-automation-ai)! Your help is appreciated.

## How Can I Contribute?

There are many ways to contribute, from writing tutorials or blog posts, improving the documentation, submitting bug reports and feature requests or writing code which can be incorporated into the main project.

### The most valuable contributions right now

*   **Selector fixes.** X changes its DOM regularly. PRs that update or harden selectors in `src/xuse/features/scraper/` and `src/xuse/features/publisher/` are the most valuable contributions this project gets, include a DOM snippet showing what changed.
*   **Skills and personas.** The bundled agent skills live in `src/xuse/skills_pack/`, edit them there, then run `python scripts/sync_skills.py` to sync the marketplace copy. New persona presets go under `presets/personas/`.
*   **Presets.** New account presets (`presets/accounts/`) and settings presets (`presets/settings/`) for real-world use cases.
*   **Docs.** Clarify setup steps, add troubleshooting entries, improve `docs/CONFIG_REFERENCE.md` and `docs/MCP_GUIDE.md`.
*   **MCP tool ideas.** Open an issue describing the tool call you wish existed (name, inputs, expected behavior). Note that the tool surface is pinned by a contract test (`tests/mcp/test_contract.py`, `EXPECTED_TOOLS`), any new tool or parameter must update it.
*   **Tests.** pytest coverage for pure logic; nothing in the suite may need a network or a browser.

### Reporting Bugs

*   **Ensure the bug was not already reported** by searching on GitHub under [Issues](https://github.com/ihuzaifashoukat/x-use/issues).
*   If you're unable to find an open issue addressing the problem, [open a new one](https://github.com/ihuzaifashoukat/x-use/issues/new). Be sure to include a **title and clear description**, as much relevant information as possible, and a **code sample or an executable test case** demonstrating the expected behavior that is not occurring.
*   Describe the **environment** in which you encountered the bug (e.g., Python version, OS, browser version if applicable).

### Suggesting Enhancements

*   Open a new issue to discuss your suggested enhancement. Clearly describe the proposed enhancement and its potential benefits.
*   Provide a clear and concise description of what you want to happen.
*   Explain why this enhancement would be useful.
*   If possible, provide a code snippet or an example of how the enhancement might look or work.

### Pull Requests

1.  Fork the repository.
2.  Create a new branch (`git checkout -b feature/your-feature-name` or `bugfix/issue-number`).
3.  Make your changes. The package lives under `src/xuse/` (`xuse/core`, `xuse/features`, `xuse/mcp`, `xuse/utils`, `xuse/models`).
4.  Ensure your code lints and follows the project's coding style (if one is established).
5.  Add tests for your changes if applicable. Run the suite with `python -m pytest`, it must pass with no network or browser.
6.  Commit your changes (`git commit -m 'feat: Add some amazing feature'`). Follow [Conventional Commits](https://www.conventionalcommits.org/) if possible.
7.  Push to the branch (`git push origin feature/your-feature-name`).
8.  Open a pull request to the `main` branch of the original repository.
9.  Clearly describe your pull request, including the problem it solves or the feature it adds. Link to any relevant issues.

## Coding Conventions

*   Follow PEP 8 for Python code.
*   Write clear and concise comments where necessary.
*   Ensure your code is well-tested.
*   Never commit `config/accounts.json`, cookie files, `.env`, or API keys.

## Code of Conduct

This project and everyone participating in it is governed by the [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code. Please report unacceptable behavior.

## Questions?

If you have any questions, feel free to open an issue and tag it as a `question`.

We look forward to your contributions!
