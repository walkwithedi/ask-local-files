# Contributing

Thanks for helping.

## How to start

1. Open an issue first and describe the problem before writing code. Reading bugs are most useful with a small, fictional sample file that shows them.
2. Fork the repository, make a branch, and open a pull request against main. The template lists the checks; CI runs them again.
3. The tool's code (ask_local_files.py, ask_memory.py, ask_local_files.html, the desktop entry and tests/) is exported from the maintainer's master copy. An accepted change is ported there and comes back with the next export, which then replaces these files; the pull request is closed with a note once that is done. Repository files (README, workflows, scripts/) are edited here directly.

## Rules

0. Run `git config core.hooksPath .githooks` once after cloning. Then pre-commit runs scripts/run_checks.py (unit tests, repository checks, personal-data scan), commit-msg scans the commit message, and pre-push scans every commit about to be pushed; any of them stops a commit or push that fails.
   Claude Code sessions get the same through .claude/settings.json: the checks run once when a cloud session starts and before every git commit Claude makes, the commit scan before every git push.
   On GitHub, every pull request runs the checks, the personal-data scan over its commits (lines, messages, author e-mails, image and document metadata), gitleaks over all of history and CodeQL; Scorecard reports supply-chain practices weekly, and Dependabot keeps the actions current. Report security problems as SECURITY.md describes.
1. Python 3 standard library only, and a page that loads nothing from the network. No third-party packages; at run time the tool talks only to the model endpoint the user configured.
2. A change in behaviour updates both READMEs and comes with a test.
3. Scripts log to stderr in the format used by the other scripts and accept --log-level.
4. A release is a change to VERSION with its CHANGELOG.md section; merging it publishes the release.
5. No personal data, work details or credentials, ever: run python3 scripts/sensitive_scan.py. Examples and test documents use obviously fictional names (Riverside Clinic, Project Atlas), example.com addresses and the documentation IP ranges (192.0.2.0/24). Test documents carry no author or other metadata. Commit with your GitHub noreply address (Settings > Emails > Keep my email addresses private). SECURITY.md lists everything the scan covers and how to keep a private list of names it should also catch.
6. No case stories, no background. Documentation, comments, the changelog, commit messages and pull requests state rules and behaviour, not where they came from: no anecdotes, no description of the environment, organisation, people or projects something was built for, and no figures taken from real documents. Explain a rule by its reason in general terms. Where an example is truly needed, keep it minimal, fictional and de-identified (rule 5).
