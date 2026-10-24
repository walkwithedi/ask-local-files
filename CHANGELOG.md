# Changelog

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
