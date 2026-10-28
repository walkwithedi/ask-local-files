# Changelog

## 2.4.0

- Prompt: the rules come before the files and their key points are repeated after them, so long files do not crowd them out. Quotes are copied word for word after finding them, one sentence or less, at most 50 characters, never across paragraphs or table cells. Answers lead with the conclusion, then points. When the files are not enough, or a term needs explaining, the model may add a closing 文件外补充（未核对）："outside the files (unchecked)" section, which is not quote-checked and not kept in memory. With a skill, the skill's own format applies.
- Find files: 忽略 Markdown 文件 ("ignore Markdown files"), on by default and saved in the settings, leaves .md and .markdown out of search and browse, including counts and the add buttons. An explicit md or markdown in the extension box still lists them.
- Memory settings are now their own panel, opened and closed by 记忆设置 ▸ next to 参考记忆, like 模型设置; it starts closed and stays reachable with memory off. 查看记忆 is drawn in the accent colour.

## 2.3.0

- Memory updates, after the LLM Wiki pattern: the selected model turns conclusions into topic pages (a title and points, each point with its file and quote), merges duplicates, replaces statements superseded by newer ones, deletes ones that no longer hold, and rewrites conclusions from files that changed. Every point and rewritten conclusion is checked against the file's current text before it is written; ones marked 不对 ("wrong") are never written back.
- New conclusions and changed files wait in an update queue, so answers do not slow down. 更新记忆 ("update memory") works through the queue and then checks every conclusion, in batches within the character limit; it can be stopped, and quitting waits for it. 空闲时自动更新 ("update when idle", on by default) works through the queue after 10 minutes without a question, and gives way at once to a question or to quitting, discarding the batch in progress.
- 更新记录 ("update history") lists the last 20 updates with their changes; they can be undone one at a time, newest first. 查看记忆 lists topic pages too, and questions draw on topic points first.
- A conclusion whose quote is gone from a changed file now waits for the update instead of being deleted at once; it is not consulted meanwhile. The memory database is upgraded in place and keeps what it holds.

## 2.2.0

- Memory is easier to reach: 记忆设置 ("memory settings") next to 参考记忆, the 已记住 N 条结论 ("N conclusions kept") line under an answer, and a 记忆设置 button in the first-run notice open 模型设置, scroll to its 记忆 section and move focus there; the line under an answer also opens 查看记忆. The entry next to 参考记忆 hides with it when memory is off.
- The page header now mentions memory and where to manage it.

## 2.1.0

- Find files: 搜索 now comes before 浏览文件夹, and the page opens on the tab used last. A new 排序 ("sort") menu offers newest first (default), oldest first, name A→Z and name Z→A for both tabs; search and browse sort on the server before the 300-entry limit, and the choice is remembered in the browser.
- 加入前 10 个 and 加入前 30 个 ("add the first 10 / 30") next to 全部加入 take files in the chosen order, subfolders included on the browse tab, and are disabled when fewer places are left.
- 复制已选择的文件路径 under 已选 copies the full paths of the picked files, one per line, for an agent such as OpenCode to work on; failures show the paths for copying by hand.
- Closing a conversation tab no longer asks; quitting still does.
- Memory (`ask_memory.py`): files picked for questions and conclusions whose quotes passed the check are kept in `~/.local/share/ask-local-files/memory.sqlite` and consulted for later questions, re-checked against the files' current content; questions may be asked without picking files. 参考记忆, 不对 and 查看记忆 / 清空记忆 under 模型设置 control it. On by default, so extracted text of picked files is now written to disk unless memory is turned off.
- Local models: llama-server on port 8080 and Ollama on port 11434 are found automatically and listed with their models; their context sets the character limit, and 重新查找 looks again. Endpoints on this computer wait up to 900 seconds and bypass proxies. The custom endpoint's API key may be left empty.
- 找文件, 已选 and 问 AI fold to one line by clicking their title, with a summary, remembered in the browser; on narrow screens 找文件 folds itself once a conversation starts.
- Double-clicking also opens Qianxin, 360 and UOS Chromium-based browsers as app windows, found through their desktop files when not on PATH.

## 1.16.0

- Works without a model endpoint: when no model is configured, 问 AI shrinks to one line with 模型设置 and the file finder takes the full width; the model settings no longer open by themselves.
- 复制全部文字 ("copy all text") and 保存全部文字 ("save all text") under 已选 carry the quoting rules and the full text of the picked files, the same system message the model would get (without skill instructions), to paste into an assistant elsewhere. Files without text are skipped and counted. Available with or without a model.
- Every file in the lists and under 已选 has a folder icon button that opens its folder in the file manager with the file selected: `org.freedesktop.FileManager1.ShowItems` through `gdbus` or `dbus-send` first, then `peony --show-items`, `nautilus --select` or `caja --select`, and plain `xdg-open` or `gio open` as the last resort. 打开文件夹 on unreadable files selects the file the same way.
- Paths sent by the page are replaced by the index's own copy before use; reading, asking, exporting and showing in the file manager accept only indexed files, so a file just added to a folder needs a refresh first.

## 1.15.0

- Skills: choose a bundled skill or one of your own in the OpenCode format. The model can read the skill's reference files, run its `.py` or `.sh` scripts through function calls (60 seconds each, at most 5 reads or runs per question) and write long content into a per-question work folder for those scripts (at most 10 writes); files the scripts produce can be saved. With a skill, files and the question are optional.
- Bundled skills: 会议纪要 (`summarize-meeting`, MIT, from phuryn/pm-skills), 文档对比 (`doc-compare`), 生成 Word 与 Excel (`office-gen`), 提示词优化 (`prompt-optimize`) and 需求澄清 (`clarify-needs`).
- New 浏览文件夹 tab, shown first, to walk the added folders level by level and add all files of a folder and its subfolders.
- Up to 50 files at once (was 20); 全部加入 appears whenever the list holds fewer than 50.
- Search: optional regular expressions; 从列表移除 hides one entry from the current list; 按文件名去重 (on by default) lists only the newest of files with the same name.
- Folder boxes accept local `file:///` addresses, and 选择…… opens the system's folder dialog.
- Unreadable files such as archives offer 打开文件夹 to open the containing folder, or show the path to copy.
- Reads old doc, xls and ppt (wps, et and dps tried as the same kinds) through doc2kb 2.1.0; the extracted text starts with the file's path, name, modification time and author.
- Answers: Enter sends and Shift+Enter starts a new line; answers are laid out as Markdown; an incomplete answer offers 继续 and 重新生成; each answer can be copied or saved as Word.
- ＋ 新对话 opens up to 8 conversation tabs in one window; the page fills the window on wide screens.
- Double-clicking opens a Chromium-family app window when one is found; `--no-app` keeps a normal browser window.
- Changing the picked files during a conversation starts a new one without asking.

## 1.5.0

First public release.
- Find files by name in the folders you add, pick up to 20, and ask any OpenAI-compatible endpoint about them; follow-up questions keep the files and the earlier turns.
- Answers must quote the files; each quote is checked against the file text when the answer ends, and missing or absent quotes are flagged and kept in a saved conversation.
- Reads plain text, code and HTML, and docx, xlsx, xlsm, pptx, eml and pdf through doc2kb 2.0.0.
- Models from an OpenCode configuration, or a custom endpoint entered in the page; character limit with "send anyway"; errors that say what to do next.
- Local server on 127.0.0.1 with a one-time token, started by double-click or from the command line; exits when its pages are closed.
