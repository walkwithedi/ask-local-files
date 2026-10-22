# ask-local-files: ask an LLM about your local files

[中文](README.zh-CN.md)

Ask a large language model about your own files on a computer where you cannot install anything. All it needs is Python 3 and a browser. It works with any OpenAI-compatible API, and every answer quotes the files and gets those quotes checked.

- No install: the Python 3 standard library only. Double-click the desktop entry or run one command, then use it in the browser. The page loads nothing from the network.
- You pick the files and they are sent whole: find files by name in the folders you added, pick a few and send them with your question. There is no vector store and no chunk retrieval.
- Quote checking: the model must back each statement with a quote from the files. When the answer ends, each quote is looked up in the file text, and quotes that cannot be found are flagged, so invented support shows.
- Only the endpoint you configure is contacted. That can be an in-house model gateway, a self-hosted vLLM or Ollama, or a public cloud API. If you already use OpenCode, its model configuration is read automatically.

The page is in Chinese.

## Files

| File | Role |
| --- | --- |
| `双击运行.desktop` | Desktop entry, shown as 找文件问 AI ("find files, ask AI"). Double-click it to open the page. |
| `ask_local_files.py` | Local server: file-name index, text extraction, calls to the model endpoint. Python 3.8+, standard library only. |
| `ask_local_files.html` | The page, served by `ask_local_files.py`. |
| `doc2kb_core.py` | Reads docx, xlsx, pptx, eml and pdf. An unchanged copy of a [walkwithedi/doc2kb](https://github.com/walkwithedi/doc2kb) release; `DOC2KB_VERSION` names which one. |
| `tests/` | Development only. `test_ask_local_files.py` holds the server tests (unittest), `ui.test.js` the page tests (needs Playwright and Chromium), and `fake_model.py` is a stand-in model endpoint. |

## Requirements

- Python 3.8 or later, and a browser with `fetch` and streaming responses (a recent Firefox or Chromium). Double-clicking works on Linux desktops; elsewhere, start it from a terminal.
- Optional: `pdftotext` (poppler-utils). Without it, PDFs cannot be read; other formats still work.
- An OpenAI-compatible endpoint: base URL, model name and API key. No other network access and no packages to install.

## Use

Download `ask-local-files-v<version>.zip` from Releases and unpack it. Double-click `双击运行.desktop`, or run `python3 ask_local_files.py`.

1. Add folders under 管理文件夹 (manage folders). Give an absolute path or one starting with `~/`. Indexing runs in the background, and you can search while it runs.
2. Find files by typing part of a name or path. Several words must all match, and the extension box filters by type. Results are listed newest first, up to 300. Formats the tool cannot read say why.
3. Pick up to 20 files. You can preview the extracted text and remove files. Files are numbered to match the "文件 N" ("file N") labels sent to the model.
4. Choose a model, write the task (summarise, rewrite, compare) and send it with Ctrl+Enter. The answer streams in, and follow-up questions keep the same files and the earlier turns.
5. Quote checking: each statement must end with a quote from file N, or say that the files do not contain the answer. When the answer ends, each quote is checked against the file text, ignoring whitespace and treating an ellipsis as a gap. Missing quotes, and answers with no quotes at all, are flagged below the answer. The flags are also written into a saved conversation.
6. Copy an answer, save the whole conversation as Markdown, or clear the conversation to start a new one.
7. The page always shows which host and model the request goes to, how many files are included and how many characters. Above the character limit (100,000 by default), nothing is sent until you remove files, raise the limit, or choose 仍然发送 ("send anyway"), which truncates the files proportionally.
8. Endpoint errors say what went wrong and what to do next: 401 or 403 for the key, 404 for the URL or model, a context that is too long, and 429 for rate limits or quota.

Formats: plain text and code files are read as UTF-8 or GB18030, up to 20 MB each. HTML is read with its tags removed. docx, xlsx, xlsm, pptx, eml and pdf are read through `doc2kb_core.py`; pdf needs `pdftotext`, and scanned PDFs have no text. Old binary Office formats are not supported.

## Index and privacy

- Only the folders you add are indexed. Hidden entries, `node_modules` and `__pycache__` are skipped, and symlinked folders are not followed. Only names and paths are matched, never file contents.
- The index is cached in `~/.cache/ask-local-files/index.json` and settings are kept in `~/.config/ask-local-files/config.json`, both with mode 600.
- With an OpenCode configuration (`~/.config/opencode/opencode.jsonc` or `.json`), every model of every provider that has `options.baseURL` and `options.apiKey` is listed. `{env:VAR}`, `{file:path}` and provider `headers` are supported. A custom endpoint can also be entered in the page, and 测试连接 ("test connection") checks it.
- Requests go to `POST <base URL>/chat/completions`, streamed. The system message states the quoting rules and carries each file's text under a `===== 文件 N：path =====` header, and the conversation follows.
- File text and the conversation go to the endpoint you chose, so pick only files you may send there. Extracted text stays in memory and is never written to disk. The API key never reaches the page, and neither the key nor file text is logged.

## Server and exit

The server listens on a random port on 127.0.0.1 only. Its address carries a one-time token, and Host and Origin are checked. It exits about five seconds after the last page closes, or at once when you press 退出 ("quit"). If an answer is still streaming, it waits for the answer to finish. A page left in the background does not count as closed. The log is `ask_local_files.log` next to the program, or in `~/.cache/ask-local-files/` when that folder is not writable.

Command line: `python3 ask_local_files.py [--no-browser] [--log-level DEBUG|INFO|WARNING|ERROR]`. The default log level is WARNING, and `LOG_LEVEL` overrides it.

Tests: `python3 -m unittest discover -s tests`. With Playwright installed, also run `node --test tests/ui.test.js`.

## Contributing

The tool's code is exported from the maintainer's master copy. Issues and pull requests are welcome; accepted changes are ported there and come back with the next export. See [CONTRIBUTING.md](CONTRIBUTING.md), and [SECURITY.md](SECURITY.md) for security problems.

## License

MIT, see [LICENSE](LICENSE).
