# Changelog

## 1.5.0

First public release.
- Find files by name in the folders you add, pick up to 20, and ask any OpenAI-compatible endpoint about them; follow-up questions keep the files and the earlier turns.
- Answers must quote the files; each quote is checked against the file text when the answer ends, and missing or absent quotes are flagged and kept in a saved conversation.
- Reads plain text, code and HTML, and docx, xlsx, xlsm, pptx, eml and pdf through doc2kb 2.0.0.
- Models from an OpenCode configuration, or a custom endpoint entered in the page; character limit with "send anyway"; errors that say what to do next.
- Local server on 127.0.0.1 with a one-time token, started by double-click or from the command line; exits when its pages are closed.
