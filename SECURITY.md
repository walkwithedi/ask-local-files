# Security policy

## Reporting a problem

Please report security problems privately through GitHub: the Security tab of this repository, then "Report a vulnerability". Do not open a public issue for them.

Useful to include: what you did, what happened, and the version, browser and model endpoint type involved. Never attach real documents or personal data; describe them, or build a fictional sample that shows the problem.

## What counts

- The local server answering anything but the page it opened: requests without the one-time token, from another host or origin, or on an address other than 127.0.0.1.
- Reading a file outside the folders the user added, or writing, moving or deleting any file the user picked (the tool only reads them).
- The API key reaching the page, the log or any host other than the configured model endpoint; file text written to disk.
- A document whose content makes the tool run code or commands, or use unbounded memory or disk.
- Personal data committed to this repository.

## Supported versions

Only the latest release receives fixes.

## Keeping personal data out

Everything here is public: code, history, commit e-mails, issues, pull requests, discussions and the Actions logs. scripts/sensitive_scan.py looks for personal data (e-mail, phone, ID and card numbers, home paths), work details (organisation names, internal domains, private and public IP addresses, Windows, UNC and mount paths, confidentiality markings; scripts/sensitive_scan.py lists them) and credentials (tokens, keys, private key blocks, passwords), and for metadata in images and documents. It runs:

- before each commit, commit message and push (.githooks, after `git config core.hooksPath .githooks`; Claude Code sessions through .claude/settings.json);
- on every pull request and push to main, over the tree and the new commits, with gitleaks over all of history;
- weekly over all of history, every branch and tag (sensitive-history workflow);
- on every issue, pull request, review, discussion and comment written or edited (sensitive-text workflow).

Under GitHub Actions the scan prints only the file, line and rule, never the text, so a finding is not repeated in a public log.

Names no rule can recognise (an employer, its systems, colleagues) go on a private list that never enters the repository, one term per line, matched without regard to case:

- locally in `~/.config/ask-local-files/sensitive_terms.txt` (or the file named by SENSITIVE_TERMS_FILE);
- for CI in the repository secret SENSITIVE_TERMS (Settings > Secrets and variables > Actions). Pull requests from forks do not receive it.

Maintainers also keep GitHub secret scanning and push protection on (Settings > Advanced Security), which stop known credential formats at `git push`.

## Something sensitive reached the repository

A new commit that deletes it is not enough: the old commit, its diff and any copy stay public.

1. Revoke or rotate any credential at once; assume it was copied.
2. Remove it from history (for example `git filter-repo --replace-text`), force-push every affected branch and tag, and ask contributors to re-clone.
3. Delete the Actions runs whose logs or annotations show it, and edit any issue or comment that quotes it.
4. Ask GitHub Support to remove cached views and pull request references that still point to the old commits.
