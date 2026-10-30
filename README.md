# ask-local-files: ask an LLM about your local files

[中文](README.zh-CN.md)

Ask a large language model about your own files on a computer where you cannot install anything. All it needs is Python 3 and a browser. It works with any OpenAI-compatible API, and every answer quotes the files and gets those quotes checked.

- No install: the Python 3 standard library only. Double-click the desktop entry or run one command, then use it in the browser. The page loads nothing from the network.
- You pick the files and they are sent whole: find files by name in the folders you added, pick a few and send them with your question. There is no vector store and no chunk retrieval.
- Quote checking: the model must back each statement with a quote from the files. When the answer ends, each quote is looked up in the file text, and quotes that cannot be found are flagged, so invented support shows.
- Memory: files picked for a question and conclusions whose quotes passed the check are kept on this computer and consulted for later questions, so you can ask about earlier material without picking files again. It can be turned off, inspected and cleared.
- Also useful without a model: find and preview files, then copy or save their extracted text, quoting rules included, to ask an assistant elsewhere.
- Only the endpoint you configure is contacted. That can be an in-house model gateway, a self-hosted vLLM or a public cloud API; llama-server or Ollama running on this computer is found automatically. If you already use OpenCode, its model configuration is read automatically.

The page is in Chinese.

## Files

| File | Role |
| --- | --- |
| `双击运行.desktop` | Desktop entry, shown as 找文件问 AI ("find files, ask AI"). Double-click it to open the page. |
| `ask_local_files.py` | Local server: file-name index, text extraction, calls to the model endpoint. Python 3.8+, standard library only. |
| `ask_memory.py` | Memory: keeps the paragraphs of files picked for questions and the checked conclusions in a local SQLite database, and looks them up for later questions; see [Memory](#memory). Python 3.8+, standard library only. |
| `ask_local_files.html` | The page, served by `ask_local_files.py`. |
| `doc2kb_core.py` | Reads docx, xlsx, pptx, doc, xls, ppt, eml and pdf. An unchanged copy of a [walkwithedi/doc2kb](https://github.com/walkwithedi/doc2kb) release; `DOC2KB_VERSION` names which one. |
| `skills/` | Bundled skills, see [Skills](#skills). |
| `tests/` | Development only. `test_ask_local_files.py` holds the server tests, `test_ask_memory.py` the memory tests and `test_skill_scripts.py` the tests of the bundled skill scripts (unittest), `ui.test.js` the page tests (needs Playwright and Chromium). `fake_model.py` is a stand-in model endpoint, `fake_opener.sh` and `fake_picker.sh` stand in for the system commands that open and pick folders, and `same_name_cases.json` holds the same-name examples that the server and the page are both tested against. |

## Requirements

- Python 3.8 or later, and a browser with `fetch` and streaming responses (a recent Firefox or Chromium). Double-clicking works on Linux desktops; elsewhere, start it from a terminal.
- Optional: `pdftotext` (poppler-utils). Without it, PDFs cannot be read; other formats still work.
- An OpenAI-compatible endpoint (base URL, model name and, when it needs one, an API key), or llama-server or Ollama running on this computer. No other network access and no packages to install.

## Use

Download `ask-local-files-v<version>.zip` from Releases and unpack it. Double-click `双击运行.desktop`, or run `python3 ask_local_files.py`.

On a wide screen the page fills the browser window: finding files on the left, the conversation on the right. The tool's description under the title folds behind 说明 ▸ ("about"), closed by default. 找文件, 已选 and 问 AI fold to one line when you click their title, with a short summary beside it; the folding is remembered in the browser, and when one column is fully folded the other takes the width. On a narrow screen 找文件 folds itself once a conversation starts. ＋ 新对话 ("new conversation") opens another conversation tab in the same window, up to 8, each with its own files; its × closes it at once, without asking, stopping an answer in progress and discarding unsaved turns.

1. Add folders under 管理文件夹 (manage folders). Give an absolute path, one starting with `~/`, or a local `file:///` address copied from a file manager; or press 选择…… ("choose") to pick one in the system's folder dialog (zenity, kdialog or tkinter, whichever is found). Indexing runs in the background, and you can search while it runs.
2. Find files in one view: the folder path on top (全部 for all added folders), then the search box (× clears it) and the extension box. The page reopens in the folder used last (remembered in the browser; 全部 when it is gone). 排序 ("sort") offers newest first (the default), oldest first (by modification time), name A→Z or name Z→A (ignoring case), remembered in the browser; lists are sorted before the display limit is applied.
   - With both boxes empty the view lists the current folder: subfolders first, by name, with the number of files they hold, then the files of this folder and all its subfolders, the same as an empty search here: in the chosen order, up to 300, each with its folder (click it to go there). Files with the same modification time are ordered by path. Inside a folder, 全部加入 ("add all") adds the readable files of that folder and all its subfolders.
   - Typing in either box searches the current folder and all its subfolders (everything at 全部). Several words must all match the path below that folder, and the extension box filters by type. With 正则表达式 ("regular expression") ticked, the whole box is one Python regular expression, case-insensitive. Results follow the chosen order, up to 300. Moving to another folder through the path or a result's folder keeps the keywords and searches there.
   - Formats the tool cannot read say why. Archives and other unreadable files offer 打开文件夹 ("open folder"); every other file has a folder icon button ("在文件夹中显示", show in folder). Both open the containing folder in the file manager with the file selected: `org.freedesktop.FileManager1.ShowItems` through `gdbus` or `dbus-send` first, then `peony --show-items`, `nautilus --select` or `caja --select`, and finally `xdg-open` or `gio open`, which opens the folder without selecting. When nothing works, the path is shown with a copy button.
   - 从列表移除 ("remove from list") hides one entry from the current list only; it comes back after a new search, another folder or a refresh.
   - 忽略 Markdown 文件 ("ignore Markdown files", on by default, saved in the settings) leaves .md and .markdown files out of search and browse, including file counts, 全部加入 and 加入前 N 个; folders holding only such files are not listed. An explicit md or markdown in the extension box still lists them. Picked files stay; the index is unchanged.
   - 按文件名去重 ("one per file name", on by default) lists only the newest of files with the same name and notes how many others there are; they can be expanded and added one by one. Names match ignoring case, with the same extension, after removing one copy marker such as " (1)", "（2）", " - 副本" or " copy". This changes the list and 全部加入 only, never the index or the files.
3. Pick up to 50 files. When the list (after de-duplication and removals) holds fewer than 50, 全部加入 adds every readable file not yet picked. With more than 10, 30 or 50 files to add, 加入前 10 个, 加入前 30 个 and 加入前 50 个 ("add the first 10 / 30 / 50") take them in the chosen order (in a folder, subfolders included); they are disabled when fewer places are left. You can preview the extracted text and remove files. 复制已选择的文件路径 ("copy the picked file paths") copies their full paths, one per line, to paste into an agent such as OpenCode that works on the files directly; if copying fails, the paths are shown to copy by hand. Files are numbered to match the "文件 N" ("file N") labels sent to the model. Changing the files after the conversation started begins a new conversation.
   复制全部文字 ("copy all text") and 保存全部文字 ("save all text", to `找文件问AI-文字-<date>-<time>.md`) take the same system message the model would get: the quoting rules and each file's full text under its `===== 文件 N：path =====` header, without skill instructions or the character limit. Paste it into any assistant and add your question. Files without text are skipped and counted. Both work with or without a model.
4. Without any usable model (no OpenCode model, no local model and no complete custom endpoint), 问 AI shrinks to one line with 模型设置 ("model settings") and the file finder takes the full width; filling in a custom endpoint brings the conversation back.
   Choose a model and, if you like, a skill (see [Skills](#skills)). Write the task (summarise, rewrite, compare) and send it with Enter or Ctrl+Enter; Shift+Enter starts a new line. The answer streams in and is laid out as common Markdown, and follow-up questions keep the same files and the earlier turns. An answer cut short by 停止 ("stop") or an error offers 继续 ("continue", picks up where it stopped) and 重新生成 ("regenerate").
5. Quote checking: each statement must end with a quote from file N, or say that the files do not contain the answer. Quotes are copied word for word, one sentence or less and at most 50 characters, never across paragraphs or table cells. The rules come before the files and are repeated after them. Answers lead with the conclusion, then points (Markdown allowed); with a skill, its own format applies. When needed, the model may end with a 文件外补充（未核对）："outside the files (unchecked)" section; it is not quote-checked and not kept in memory. When the answer ends, each quote is checked against the file text, ignoring whitespace and treating an ellipsis as a gap. Missing quotes, and answers with no quotes at all, are flagged below the answer. The flags are also written into a saved conversation.
6. Copy an answer or save it as Word, save the whole conversation as Markdown or Word, or clear the conversation to start a new one. Saved conversations start at the first question and list the files at the end under 参考文件 ("reference files"), numbered as in the answers; a single answer saved as Word ends with them too. File names are `找文件问AI-<title>-<date>-<time>`. Conversation tabs show the first 12 characters of the first question, then a title of up to 10 characters that the model writes after the first answer (without files or memory; kept as is if that fails); clearing the conversation or changing the files returns the tab to 对话 N.
7. The page always shows which host and model the request goes to, how many files are included and how many characters. Above the character limit (100,000 by default), nothing is sent until you remove files, raise the limit, or choose 仍然发送 ("send anyway"), which truncates the files proportionally.
8. Endpoint errors say what went wrong and what to do next: 401 or 403 for the key, 404 for the URL or model, a context that is too long, and 429 for rate limits or quota.

Formats: plain text and code files are read as UTF-8 or GB18030, up to 20 MB each. HTML is read with its tags removed. docx, xlsx, xlsm, pptx, eml and pdf are read through `doc2kb_core.py`; pdf needs `pdftotext`, and scanned PDFs have no text. Old binary doc, xls and ppt (Office 97 and later) are read through `doc2kb_core.py` too, and wps, et and dps are tried as doc, xls and ppt; password-protected, older or damaged files ask to be saved as docx, xlsx or pptx first. Archives are not read; unpack them first. The extracted text of each file starts with its path, name and modification time, and its author (and a different last editor) when the document records one; these lines go to the model with the text.

## Index and privacy

- Only the folders you add are indexed. Hidden entries, `node_modules` and `__pycache__` are skipped, and symlinked folders are not followed. Only names and paths are matched, never file contents.
- The index is cached in `~/.cache/ask-local-files/index.json` and settings are kept in `~/.config/ask-local-files/config.json`, both with mode 600.
- With an OpenCode configuration (`~/.config/opencode/opencode.jsonc` or `.json`), every model of every provider that has `options.baseURL` and `options.apiKey` is listed. `{env:VAR}`, `{file:path}` and provider `headers` are supported. A custom endpoint can also be entered in the page, and 测试连接 ("test connection") checks it. The API key may be left empty for endpoints that need none; a remote endpoint without a key is marked 「（未鉴权）」 ("not authenticated") next to its host.
- An endpoint that sends nothing for 120 seconds counts as failed; for endpoints on this computer (127.0.0.1, localhost, ::1) the limit is 900 seconds and no proxy is used.
- Copying or saving all text never contacts the endpoint; the text goes only to the clipboard or the downloaded file.
- Requests go to `POST <base URL>/chat/completions`, streamed. The system message states the quoting rules and carries each file's text under a `===== 文件 N：path =====` header, and the conversation follows. Paths there, and in copied, saved or QR-carried text and saved conversations, start at the added folder's name (such as `资料/方案.docx`) rather than the full path; files outside the added folders show their name only. The page and 复制已选择的文件路径 keep full paths.
- File text and the conversation go to the endpoint you chose, so pick only files you may send there. With memory on (the default), the extracted text of files picked for questions and the checked conclusions are stored on this computer (see [Memory](#memory)); with memory off, extracted text stays in the server's memory and is never written to disk. The API key never reaches the page, and neither the key nor file text is logged.
- When memory is used, the system message adds the earlier conclusions and paragraphs found after the picked files, under `===== 文件 N：path（来自记忆，节选）=====` headers, and says that earlier conclusions are only leads and quotes must be copied from the excerpts.

## Memory

- What it keeps: the extracted text of files picked for a question, by paragraph, and every sentence of an answer whose quote passed the check, stored without its source as a conclusion together with the quoted sentence, the file and the question. Later questions retrieve related conclusions and paragraphs and send them along, so you can ask without picking files. A conclusion reached more often ranks higher.
- Where: `~/.local/share/ask-local-files/memory.sqlite` (`XDG_DATA_HOME` is honoured; file mode 600, folder 700). Only extracted text and original paths are kept, never the files. All conversation tabs share it. A database that cannot be opened is renamed and a new one started.
- Retrieval: the question is split into keywords at punctuation, spaces and common function words; SQLite FTS5 is used when available, a plain scan otherwise. Results rank by matched keywords, conclusions also by how often and how recently they were reached. Content of the picked files is not repeated.
- Kept in step with the files: before use, a remembered file whose modification time or size changed is read again and conclusions and topic points whose quote is gone are set aside, no longer consulted, until the next update rewrites them from the new text; a file that no longer exists is forgotten with its conclusions and points, and topic pages left without points go too.
- Size: without picked files, memory may use what is left of the character limit after the skill and the conversation; with picked files, at most a third of that, and only what the files leave unused.
- 参考记忆 ("use memory"), below the skill menu, is ticked by default; then you may ask without files. An answer that used memory says how many conclusions and paragraphs it drew on, which can be expanded. 不对 ("wrong") next to an answer forgets the conclusions it added and keeps them from being added again.
- Memory settings: a panel of its own, opened and closed by 记忆设置 ▸ ("memory settings") next to 参考记忆, like 模型设置; it starts closed each time. The 已记住 N 条结论 line under an answer (which also opens 查看记忆) and the 记忆设置 button in the first-run notice open it, scroll to it and move focus there. With memory off, 参考记忆 is hidden but 记忆设置 stays, so memory can be turned back on. 查看记忆 is drawn in the accent colour. The page header mentions memory too.
- Management: 记忆设置 has an on/off switch (on by default), the counts of files, conclusions, topic pages and queued items, 查看记忆 ("view memory", searchable; delete a topic page or a single conclusion, or forget a file) and 清空记忆 ("clear memory"). Turned off, nothing is added or consulted, and what is stored stays.
- Updates (after the LLM Wiki pattern): the model keeps the memory as topic pages that grow over time. New or changed conclusions and changed files wait in an update queue, so answers do not slow down. An update sends the queued items with related topic pages and conclusions to the selected model in batches (within the character limit, at most 12,000 characters and 40 conclusions each), which (1) groups conclusions into topic pages, a title and points, each point with its file and quote; (2) merges duplicates (counts add up), replaces statements superseded by newer ones and deletes ones that no longer hold; (3) rewrites conclusions from changed files with a new quote, or deletes them. The reply must be the agreed JSON; every point and rewritten conclusion is checked against the file's current text, and ones that fail or were marked 不对 are not written. A batch whose reply cannot be read is left in the queue. Questions draw on topic points first, written as "topic: point", then conclusions, each quote once.
- 更新记忆 ("update memory") in 记忆设置 works through the queue and then checks every conclusion, showing the stage and batch; 停止 ("stop") ends it without writing the batch in progress, and quitting waits for it.
- 空闲时自动更新 ("update when idle"), on by default and saved in the settings, works through the queue with the selected model when the tool is open, no answer is being written and no question has come for 10 minutes. A question or quitting stops it at once, and the batch in progress is not written. A model error ends the round; the reason shows in 记忆设置 and the next idle period tries again.
- 更新记录 ("update history") lists the last 20 updates, manual or automatic, with time, counts and changes; 撤销 ("undo") on the newest one restores the conclusions and topic pages from before it, one update at a time. Files no longer in memory and conclusions marked 不对 are not restored; the queue is not. 清空记忆 also clears the queue and the history.
- Updates send conclusions, quotes, related topic pages and the new text of changed files to the selected model, the same endpoint as questions; untick 空闲时自动更新 to update only on demand. The database is upgraded in place and keeps its contents.
- Above 200 MB of text the least recently used files are forgotten first. Memory errors are logged and questions go on without it.

## Local models

- When the page opens, when 模型设置 is expanded or when 重新查找 ("look again") is pressed, `/v1/models` is queried on `127.0.0.1:8080` (llama-server) and `127.0.0.1:11434` (Ollama). Each model is listed as 「name（本机 llama-server）」 or 「name（本机 Ollama）」, and requests carry no key.
- The character limit follows the model's context: `n_ctx` from llama-server's `/props`, or the loaded model's `context_length` from Ollama's `/api/ps` (`OLLAMA_CONTEXT_LENGTH`, else 4096, before it is loaded). One token counts as one character and a quarter is left for the answer, so the limit is the smaller of context × 0.75 and the configured limit.
- Skills work as usual; llama-server needs `--jinja` to accept tool calls.
- The tool neither installs nor starts a model server; run, for example, `llama-server -m model.gguf --port 8080 --jinja` or `OLLAMA_CONTEXT_LENGTH=16384 ollama serve` yourself.

## Skills

A skill is a folder holding `SKILL.md`: `name:` and `description:` between two `---` lines at the top, then instructions for the model. The folder may also hold reference files and `.py` or `.sh` scripts (the OpenCode skill format). The skill menu lists the bundled skills in `skills/` and your own in `~/.config/opencode/skills/`; the default is no skill. With a skill chosen you may send without files or without a question, and every answer names the skill it used.

- 管理 skill ("manage skills") next to the skill menu adds skills to `~/.config/opencode/skills/` and selects the new one:
  - Create: a name (lowercase letters, digits and hyphens, up to 64 characters), a description (up to 1,024 characters) and the instructions (up to 50,000 characters) become `SKILL.md`. Missing fields or a bad name are reported and nothing is written.
  - Install: a `.zip` file, clicked, dropped or opened with Enter on the focused drop area (up to 5 MB), or a folder path (选择…… opens a folder picker where available). `SKILL.md` must sit at the top, or in the zip's single top folder, with a valid name and description. Absolute paths, `..` or links in the package, links in the folder, more than 200 files or more than 20 MB unpacked are refused.
  - Replacing a skill of your own with the same name asks first, naming the folder and how many files it holds; so does installing a skill with `.py` or `.sh` scripts, saying how many and what scripts may do. A bundled skill with the same name stays, and yours takes precedence.
  - Files are staged in a temporary folder and checked before they replace anything, so a failure leaves no partial skill and keeps the old one. The installed folder and its scripts are accessible to the current user only.
  - A `.zip` written by a script under an answer that holds a skill gets an 安装 ("install") button with the same rules; the model cannot install anything itself.

- The skill's instructions open the system message and count towards the character limit. With files picked, quotes are still required; statements taken from script output or reference files are marked 「（脚本输出）」 ("script output").
- When the skill has reference files or scripts, the request carries OpenAI-compatible function tools (`tools`) that the model calls as needed:
  - `read_skill_file` reads a text file in the skill folder, up to 20,000 characters.
  - `run_script` runs a `.py` file (with the Python running the tool) or a `.sh` file (with bash) in the skill folder, arguments passed one by one, no shell. The script gets `ASK_FILES` (paths of the picked files, one per line), `ASK_ROOTS` (the added folders), `ASK_OUT` (an empty output folder; files written there can be saved from the page), `ASK_WORK` (this question's work folder) and `ASK_TOOL_DIR` (the tool folder, so it can use `doc2kb_core.py`).
  - `write_work_file`, offered only to skills with scripts, writes long content into `ASK_WORK` for a script to read, up to 500,000 characters per file. The work folder is created for each question and removed when the answer ends.
  - Paths cannot leave the skill folder or the work folder, through links neither.
- Limits: scripts run without asking each time, for at most 60 seconds; at most 5 reads or runs and 10 writes per question; tool results passed to the model are cut at 20,000 characters. 停止 also stops a running script. If the endpoint does not support function calling, the page says so; choose an instructions-only skill or another model.
- Choosing a skill with scripts lets them run as you, with access to whatever you can read and write. Install only skills you trust. Script output is sent to the endpoint.

Bundled skills:

| Name | Shown as | Purpose | Scripts |
| --- | --- | --- | --- |
| `summarize-meeting` | 会议纪要 | Meeting transcript or notes → date, attendees, key points, decisions, action items, open questions | none |
| `doc-compare` | 文档对比 | Line-by-line comparison of two picked files: additions, deletions, changes and a summary of what really changed, with an optional side-by-side HTML report | `scripts/compare.py` |
| `office-gen` | 生成 Word 与 Excel | Turns prepared content into docx or xlsx files that can be saved | `scripts/make_docx.py` (Markdown → docx), `scripts/make_xlsx.py` (JSON → xlsx) |
| `prompt-optimize` | 提示词优化 | Rewrites a draft prompt into a short, clear, numbered one | none |
| `clarify-needs` | 需求澄清 | Asks one question at a time until a vague task is clear, sums up the needs and acts once they are confirmed | none |
| `skill-create` | 创建 skill | Drafts a new skill with you, then writes `SKILL.md` and reference text and packs `<name>.zip` for you to install | `scripts/pack_skill.py` (checks and packs; .md and .txt only, no scripts) |

The bundled scripts use only the Python standard library and also run from a terminal (pass files through the variables above), with `--help` and `--log-level`. `summarize-meeting` is copied unchanged from [phuryn/pm-skills](https://github.com/phuryn/pm-skills) (MIT License); its folder holds that licence and `UPSTREAM.md`.

## Server and exit

Double-clicking opens the page in an app window of a Chromium-family browser (`--app`, no address bar) when one is found, including Chinese Chromium-based browsers such as Qianxin, 360 and UOS, which are also located through their desktop files when not on PATH; otherwise in a normal window of the default browser.

The server listens on a random port on 127.0.0.1 only. Its address carries a one-time token, and Host and Origin are checked. It exits about five seconds after the last page closes, or at once when you press 退出 ("quit"). If an answer is still streaming, a script is running or 更新记忆 is under way, it waits for it to finish; an idle-time memory update does not hold it up. A page left in the background does not count as closed. The log is `ask_local_files.log` next to the program, or in `~/.cache/ask-local-files/` when that folder is not writable.

Command line: `python3 ask_local_files.py [--no-browser] [--no-app] [--log-level DEBUG|INFO|WARNING|ERROR]`. `--no-browser` only prints the address, and `--no-app` opens a normal window of the default browser. The default log level is WARNING, and `LOG_LEVEL` overrides it.

Tests: `python3 -m unittest discover -s tests`. With Playwright installed, also run `node --test tests/ui.test.js`.

## Contributing

The tool's code is exported from the maintainer's master copy. Issues and pull requests are welcome; accepted changes are ported there and come back with the next export. See [CONTRIBUTING.md](CONTRIBUTING.md), and [SECURITY.md](SECURITY.md) for security problems.

## License

MIT, see [LICENSE](LICENSE). `skills/summarize-meeting/` is third-party and used under the MIT licence in its folder.
