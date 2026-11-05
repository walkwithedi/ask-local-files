# Changelog

## 2.30.0

- 管理文件夹 ("manage folders") opens as a large modal dialog (full screen on narrow screens); the rest of the page cannot be used while it is open. 完成 ("done") or Esc closes it, returns focus to the button and refreshes the file list; rebuild progress shows inside it. With no folders, the 文件 tab no longer opens it by itself, and "no folders yet" is said once, in the list, with an 添加文件夹 ("add folders") button.
- Storage location: a location recorded as the tool folder itself, or a folder inside it other than `数据/`, is moved into `数据/` on start, with a notice (nothing is merged when `数据/` already holds data; a failed move is undone and the old place used for this run). Changing the location to the tool folder itself counts as its `数据/`; any other folder inside it is refused, since it would not be carried to a new version.
- 从上一版取回 ("restore from the previous version"): when the knowledge base is empty after an upgrade and the previous version holds one with content, a notice and this button appear. It only copies; the previous version is untouched and files of the same name are kept as `<name>.取回前`. New endpoint `/api/data-restore`; the storage location info gains `previous`.

## 2.29.0

- Automatic inclusion takes only the formats doc2kb reads (Word, Excel, PowerPoint, WPS, PDF, e-mail) plus txt and Markdown; web pages, code, tables and configuration files are listed as 格式不自动收录 ("format not included automatically"). Files of these formats included earlier count as outside the automatic inclusion scope: kept, no longer updated or extracted, and removable with 移出不在自动收录范围内的文件 ("remove files outside the automatic inclusion scope").
- The three knowledge base tabs refresh by themselves: at most every 5 seconds while inclusion or organising runs, after each organising batch, every 15 seconds otherwise; only the visible tab, keeping search, expanded entries and scroll position. The status after 立即收录 no longer sticks at "including".
- Every button has a tooltip; 概览 gains 收录、整理是什么 ("what inclusion and organising are"). The batch size boxes are wide enough for their largest values.
- 保存位置 ("storage location") moved from the 通用 tab of the settings panel to the knowledge base settings, and the 通用 tab is gone. The default location is `数据/` in the tool folder; when the home folder is used instead, the reason is shown with 改存到安装目录…… ("store in the tool folder"). `/api/data-dir` accepts `tool`; the storage location info gains `tool`. Statistics' `outside` includes files in formats no longer included; the left-out list gains the reason `format`.

## 2.28.0

- Knowledge base and question prompts rewritten in numbered layers (input, task, rules, output); JSON fields and fixed phrases are unchanged. Organising: changing a topic must keep all its points; conclusions that fit an existing topic do not get a near-duplicate title; a newer statement replaces an older one only when it is clearly newer; certainty such as "proposed" or "to be decided" is kept. Extraction: numbers, units, amounts and dates are copied as written; extraction rules move to version 4 (no automatic re-extraction; use 按新办法重新整理全部). Questions: citation rules point at the files in `<documents>`; cited sentences must name their subject; no citations inside table cells. Replies are parsed with `<think>` sections removed. Topic picking may take 45 seconds instead of 20, title summaries 60 instead of 30 (local models unchanged).

## 2.27.0

- 关系图 ("graph") in 概览: a full-window, read-only view of topics, conclusions and files; click a node to expand one level, drag, zoom and search; the side panel offers 展开, 在主题与结论里查看 and adding the source files to 已选. A tree list with the same content works with the keyboard and screen readers; narrow screens show only the list. At most 300 nodes at a time. New endpoint `/api/graph`; the knowledge base gains an index, without a database version change.

## 2.26.0

- Conclusions are extracted and organised in batches of files: queued files grouped by folder, oldest first, small ones joined into one batch (by default at most 30,000 characters and 20 files, set in the knowledge base settings) and filed into topics right after each batch; large files go in several batches with the file start and earlier conclusions. A failing batch is retried and split; single files that still fail are skipped, listed and can be retried. Each round ends by checking the topics it touched. 概览 shows the batch plan and the latest batches. Changed files drop conclusions whose quote is gone and are extracted again from the start. Extraction rules version 3; rule changes no longer trigger re-extraction; 按新办法重新整理全部 ("reorganise everything the new way") added. New settings `xbatch_chars`, `xbatch_files`; `/api/memory-update` gains `plan` and `log`; `/api/memory` gains `reorganize` and `retry_xfail`; new `/api/memory-reorg`; statistics gain `xfailed`.

## 2.25.0

- The knowledge base, settings and token live in `数据/` in the tool folder by default. The first start moves the old data from the home folder there and writes `data-dir.txt`; when that fails or the tool folder is not writable, the home folder is used for this run with a notice. Upgrade and rollback move `数据/` as a whole (upgrade module 1.2, `move:` entries).
- 清空知识库 stops answers, inclusion and organising first, then deletes and recreates the database file and the file index cache; the dialog lists counts and sizes and warns that an answer in progress will be stopped; automatic inclusion pauses until 立即收录 or the next start; questions are refused while clearing. 概览 gains 移出不在自动收录文件夹里的文件.
- The server exits 5 seconds after the last page closes (instead of 30 minutes); 退出 stops at once and stops all background tasks. `/api/memory` gains `outside`; statistics gain `points`; inclusion status gains `paused`; `/api/quit` always returns `waiting: false`; the storage location info gains `home` and `notice`.

## 2.24.0

- Upgrade and rollback no longer wait forever: they wait only for answers being written (including skill scripts) and manual organising, not for automatic inclusion or idle-time organising, which stop and resume on the new version. The wait says what it is waiting for and can be cancelled with 取消等待, leaving the tool folder unchanged; reloading the page during the wait shows the progress again. `/api/upgrade` gains `wait_for`; new `POST /api/upgrade/cancel` (409 when not waiting).

## 2.23.0

- Unticking automatic inclusion or removing a folder takes its files out of the extraction and review queues; what is stored stays and is still consulted. Folders already removed or unticked are handled on the next inclusion check. The confirmation says how many files leave the queue; cancelling restores the tick. 概览 counts files outside the ticked folders. Re-ticking continues where extraction left off. `/api/folders` gains a read-only `count`; unticking and removing return `parked`; statistics gain `outside`.

## 2.22.0

- Leaner 文件 tab: sorting and the three tick boxes moved into 选项 ▸ ("options"), closed by default and remembered, reading 选项（N 项已改） when changed. The knowledge base settings gear is labelled 设置. Your own skills folder is no longer shown when no skill is chosen. The summary next to 发送 shows the endpoint address on hover only; unauthenticated endpoints are still named.

## 2.21.0

- The page is reorganised around the knowledge base, with the same two columns and file finding: 找文件 becomes the 知识库 card with the tabs 文件 (finding files, with an inclusion mark on each file), 概览 (progress, recent topics, empty states) and 主题与结论 (search, 50 at a time). The knowledge base settings moved to the card's gear, with five items. Citations above an answer jump to their place in the knowledge base, with 返回回答 to return; merged or removed entries say where they went. An empty knowledge base shows a three-step guide in 问 AI. 清空知识库 uses a dialog in the page. Terms: 收录 (inclusion) and 整理 (organising); 更新知识库 is now 现在整理, topic pages are topics, and 删除 next to a conclusion is now 这条不对. A line next to 发送 says what the question sends. A 跳到提问框 link and Ctrl+L focus the question box. Dark mode contrast raised to 4.5:1. New `/api/memory-locate`, paging and filters for `/api/memory`, settings `guide_skip` and `layout_notice`.

## 2.20.0

- The bundled skills were reviewed against Anthropic's Skill authoring best practices. clarify-needs, doc-compare, office-gen, prompt-optimize and skill-create now say when to use them with their trigger words, show how to run their scripts and what the output looks like (made-up examples), and say how to check and correct key steps; text repeating the tool descriptions is gone. skill-create asks the same of the skills it writes. doc-compare's script errors say what to do next. summarize-meeting stays as copied from upstream.
- New evaluation scenarios for every bundled skill (`tests/skill_evals/`, three each, made-up content), a test that checks them and runs their script steps, and `tests/skill_eval_run.py`, which asks each scenario with a configured model and writes a Markdown report for comparing a skill before and after a change.
- The help page's skill section lists the writing principles.

## 2.19.0

- Question assembly: the system message now holds only rules; the picked files and knowledge base excerpts open the latest question, each in a `<document index="N"><source>…</source><document_content>…</document_content></document>` block, followed by the repeated rules and then the question. Earlier questions and answers are sent as text, unchanged. New rules: answer in the language of the question, one made-up citation example, and instructions inside files are not followed. 复制全部文字, 保存全部文字 and the QR code use the same assembly, without the question.
- 提问前先让模型看主题页目录 ("let the model see the topic list before asking") in the knowledge base settings, on by default: each question first asks the selected model to pick up to 5 topic pages from the topic list; their points come before the keyword results. A timeout (20 s, 60 s for a local model), an error or an unreadable reply falls back to keywords only. The help page's privacy section mentions this extra request.
- Conversation titles follow the language of the question.
- The skill introduction and the three tool descriptions say when to use each tool and give an example; a failed tool call tells the model the reason and what to change.
- The help page's 原理与提示词 section explains the assembly, the topic list step and on-demand skill loading, and prints every prompt sent to the model in full; a test keeps it identical to the code.

## 2.18.0

- Stronger extraction prompt for conclusions from files: one fact per conclusion; name the subject but only from the text, the file start or the file name; keep the original's certainty ("proposed", "to be confirmed"); keep relative dates as written and note the reference date; write in the file's language; quotes copied with their punctuation and not across table cells; made-up examples; instructions inside files are not followed. From the second batch on, the start of the file is sent along as `head`.
- Topic page titles and points are written in the language of their conclusions.
- Extraction rules have a version. After upgrading, automatically included files are extracted again under the new rules (subject to 空闲时自动更新, which sends their text to the model again): a conclusion with the same quote is rewritten in place, one not extracted again and never cited by an answer is deleted, conclusions from questions are left alone, and every batch can be undone. The knowledge base settings show how many files remain. The database is upgraded in place.
- The help page gains a 原理与提示词 ("principles and prompts") section with the design behind the knowledge base and both knowledge base prompts in full.

## 2.17.0

- Status messages for steps in progress are shown in the accent colour on a light blue bar with a CSS spinner (still when reduced motion is requested); results get a light bar in their own colour (green, yellow, red), with dark-mode colours.
- While the folder dialog opened by 选择…… is waiting, the status counts the seconds, notes after 10 seconds that the dialog may be behind the browser, and a 取消 ("cancel") button closes it (new endpoint `POST /api/pick-cancel`).

## 2.16.0

- Upgrade module 1.1: files unpacked or copied during an upgrade get their permission bits reduced by the umask, as unzip and cp do; with a umask of 022 nothing changes. No other change.

## 2.15.0

- Upgrade in place: 升级 ("upgrade") at the top right of the page takes a new release zip or unpacked folder (picked or dropped), shows current version → new version, the changelog entries between them, the files that will be kept and any extra files, and upgrades after confirmation. An answer being written, a knowledge base update, inclusion or sync in progress is waited for; afterwards the server restarts on the new version with the same port and token and the page reloads. 退回上一版 ("roll back") swaps back to the previous version. See the Upgrade section of the README.
- Command line: `--upgrade`, `--rollback`, `--yes` and `--version`; upgrading or rolling back from the command line asks to quit the running tool first.
- New files `tool_upgrade.py` (the upgrade module) and `upgrade-keep.txt` (files carried over: `data-dir.txt` and the server log). The help page has an upgrade section.
- Release zips now include `help.html`, which they had left out since 2.10.0, and the two new files.

## 2.14.0

- Starting the tool while this tool from another folder or of another version holds the port (for example after the tool folder was moved or replaced) asks that one to quit and takes over the port, instead of opening a page from the old program. `--new-token` and `--data-dir` treat that case as running too. The error for a missing page file says why and what to do next.

## 2.13.0

- The desktop entry is renamed from `双击运行.desktop` to `资料管家.desktop`, so it no longer has the same file name as other tools' entries and can be sent to the desktop alongside them. A desktop link made to the old name stops working and has to be made again from the tool folder. The entry's content and the way it finds the program are unchanged; its not-found message, the page and the help page name the new file.

## 2.12.1

- The READMEs end with a Feedback section giving the maintainer's feedback address. The sensitive-data scanner allows that one full address (PUBLIC_ADDRESSES) and still reports every other e-mail address. No change to the tool. walkwithedi/edi-lab#206.

## 2.12.0

- The Chinese name is now 资料管家 ("file keeper"): page title and header, footer, help page, desktop entry, messages and saved file names (`资料管家-`). The English name, folder, program, repository and release names are unchanged.
- The page header shows the tagline 用 AI 查找、整理、问答本机资料 ("find, organise and ask AI about your local files") next to the name, in the secondary text colour; on narrow screens it moves below the name and leaves the header buttons their room. The page title and desktop entry show only the name.

## 2.11.0

- "Memory" is now called the knowledge base (知识库) throughout the page, messages, help page and READMEs: 参考知识库 ("use knowledge base"), 知识库设置, 更新知识库, 查看知识库, 清空知识库; recording is 收录 ("include") and forgetting is 移出 ("remove"). Saved Markdown packages are named `知识库-<date>-<time>.zip`. Endpoints, `ask_memory.py`, `memory.sqlite` and the settings fields keep their names, so existing data and settings work unchanged.
- Folders added from now on have 自动收录到知识库 ("include in knowledge base") ticked: their files are included in the background, and the message after adding a folder says so and how to untick it. A folder added while the knowledge base is off is ticked too and included once it is turned on. Folders added before keep their state.
- The Chinese name is now 本机资料问答 ("local file Q&A"); saved files start with `本机资料问答-`. The English name, folder, repository and release names are unchanged.
- Model, skill and knowledge base settings are one 设置 ("settings") panel below the model and skill row, with four tabs: 模型, skill, 知识库 and 通用 ("general", holding the storage location). 模型设置, skill 设置 (formerly 管理 skill) and 知识库设置, each with a gear icon, open their tab and close the panel when it is already showing it. Arrow keys switch tabs, Esc or 收起 closes the panel and returns focus to the entry, the last tab is remembered in the browser, and a tab with unsaved input shows a dot that screen readers announce. Each section keeps one line of explanation with the rest under 详细说明 ("details"); 查看知识库 comes last in its tab, folded. Without a usable model the skill tab is hidden.

## 2.10.0

- Folders that record themselves: each added folder has 自动记入记忆 ("record into memory"), off by default. Ticked folders are scanned after every index scan and at most every 10 minutes; new and changed supported files are read into memory by paragraph in the background (files are only read, no model is called), and files that give no text are listed with the reason. Updates then send each recorded file paragraph by paragraph in batches (within the character limit and at most 12,000 characters) to the selected model to extract conclusions with word-for-word quotes; only quotes found in the current file text are stored, and the same update files them into topic pages. Deleted files are forgotten; unticking keeps what was recorded. Recording never holds the tool open and resumes on the next start.
- Memory capacity is a setting in 记忆设置 (50 to 102,400 MB), default raised from 200 MB to 1 GB. Recorded files count as never picked and are forgotten first when memory is full; a file left out for lack of room is not recorded again until it changes or the limit is raised.
- Topic pages carry related topics (up to 8, existing titles only, undone with the update). 保存为 Markdown ("save as Markdown") in 查看记忆 saves the topic pages and conclusions as a zip that opens as an Obsidian vault, with links between topics and source files.
- 配置 OpenCode ("configure OpenCode") in 模型设置 replaces the key of one provider in an existing OpenCode configuration: it tests the new key first, writes only the `apiKey` value (comments and formatting kept), backs up the original and can roll back. Without an OpenCode configuration nothing is created.
- The Chinese name is now 找资料问 AI ("find material, ask AI"); saved files start with `找资料问AI-`. Folder, program and data locations are unchanged.
- New help page `help.html` (quick start, how each feature works, privacy, common questions), opened by 帮助 in the page header.
- The memory database is upgraded in place to version 3 and keeps its contents.

## 2.9.0

- Storage location (保存位置): memory, settings and the address token can be kept together in a folder of your choice, such as one on a data disk that is not cleaned up with the home folder. Change it at the bottom of 记忆设置 or with `--data-dir DIR`. An empty folder receives the current files (copied first, originals removed only after every copy succeeded); a folder that already holds them is used as it is, without merging. The location is recorded in `data-dir.txt` in the tool folder; copy it along when upgrading into a new folder. If the recorded folder is unusable, the tool does not fall back to the home folder: it says so below the header and, for that run, keeps no memory and saves no settings. Until a location is set, the first start mentions once that it can be changed.
- `doc2kb_core.py` updated to doc2kb 2.2.0. Only its single-file conversion is used, and the extracted text is the same as with 2.1.0.

## 2.7.0

- Fixed address: the server listens on port 47821 by default (`--port`, or `"port"` in `~/.config/ask-local-files/config.json`), and its token is kept in `~/.config/ask-local-files/token` (mode 600), so the address stays the same across restarts and can be bookmarked or pasted into any browser. 复制网址 ("copy address") in the footer copies it. `--new-token` replaces the token, invalidating the old address; `--print-url` prints the address without starting the server.
- Starting the tool while it is already running opens a page on the running server instead of starting a second one. When the port is taken by another program, it says so and suggests `--port`.
- Closing the page no longer stops the server. It exits after 30 minutes with no page open and nothing running (`--idle-timeout` in seconds, 0 to never exit on its own), or at once on 退出 ("quit"), still waiting for answers in progress.

## 2.6.0

- Browsing a folder with both boxes empty now works like an empty search of that folder: files from all its subfolders are listed together, sorted before the 300-file limit, each with its folder (click it to go there). Subfolders still come first. De-duplication spans the levels. Files with the same modification time are ordered by path, so the order stays fixed.
- 管理 skill ("manage skills") next to the skill menu creates a skill from a name, a description and instructions, or installs one from a .zip file (click, drop or keyboard) or a folder into `~/.config/opencode/skills/`. Packages with absolute paths, `..`, links, more than 200 files or over 20 MB unpacked are refused. Replacing a skill of your own with the same name, and installing one with scripts, ask first and say what will be replaced or run. Files are staged and checked first, so a failure leaves nothing behind, and the installed folder and its scripts are accessible to the current user only. The new skill is selected afterwards.
- New bundled skill `skill-create` (创建 skill): the model drafts a skill, writes it once you agree, and `scripts/pack_skill.py` checks it and packs `<name>.zip` (instructions and .md/.txt references only, no scripts). A skill package listed under an answer gets an 安装 ("install") button; nothing is installed until you click it.

## 2.5.0

- Find files is one view instead of two tabs: a folder path on top, then the search and extension boxes. With both boxes empty it lists the current folder; with either filled it searches the current folder and all its subfolders (everything when at 全部), matching keywords and regular expressions against the path below that folder. Moving to another folder keeps the keywords, and the page reopens in the folder used last. 按文件名去重, 正则表达式 and 忽略 Markdown 文件 sit together above the list. 加入前 50 个 ("add the first 50") joins 10 and 30.
- Saved conversations and whole-conversation Word files start at the first question and list the files at the end under 参考文件 ("reference files"); a single answer saved as Word ends with them too.
- Paths sent to the model, copied or saved as text, carried by QR code, and written in saved conversations start at the added folder's name instead of the full path. Files outside the added folders show their name only. 复制已选择的文件路径 still copies full paths.
- The page header's description folds behind 说明 ▸ ("about"), closed by default.
- Conversation tabs show the first 12 characters of the first question, then a title of up to 10 characters written by the model after the first answer; saved files carry the title in their names.

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
