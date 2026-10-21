# Third-party notices and license scope

## LazyParse original code

Copyright (C) 2026 m1daz0141. LazyParse original source code and documentation are distributed under the MIT License; see `License` and `COPYRIGHT`.

## Attack-name data

`tools/attack_catalog/data/skills_ja.csv` is retained from Remon-7L/Remon-7L.github.io at the pinned commit in `tools/attack_catalog/data/source.json`. `tools/attack_catalog/skills.csv` supplies initial defaults for the editable `config/skills.csv` beside the executable.

The upstream repository currently reports no detected license, and no license file or data-specific permission notice was found at the pinned commit. The recorded license remains `null`. This project does not assert its MIT License over that third-party data. Its source, hash, and recorded permission status are preserved; permission terms remain an unresolved upstream question.

Source: https://github.com/Remon-7L/Remon-7L.github.io

## MinHook and its included disassembler

Legacy research builds download a pinned MinHook release separately. MinHook and included Hacker Disassembler Engine portions retain their BSD-style terms and copyright notices. The legacy build copies the complete upstream license into `bin/MinHook-LICENSE.txt`; generated binaries and vendor sources are not included here. The portable executable does not link MinHook or its code-patching backend. The project license does not replace those terms.

Source: https://github.com/TsudaKageyu/minhook

## Portable executable runtime

The optional Windows executable bundles CPython, Tcl/Tk and a PyInstaller bootloader. They retain their own licenses; LazyParse's MIT License does not replace them. PyInstaller's license includes its bootloader exception. The build includes the Python license, Tcl/Tk license terms and PyInstaller COPYING text as resources and copies them to `config/licenses/` on first launch. Build dependencies are pinned in `tools/portable/requirements-build.txt`.

Sources: https://docs.python.org/3/license.html, https://www.tcl-lang.org/software/tcltk/license.html, https://pyinstaller.org/en/stable/license.html

## Original OverParse application

The original WPF application, project files, artwork, old CSV prototype, and original JA-exclusion resource are not part of the current distribution. The local research checkout retains the original MIT notice with those source files. Their license has not been changed. Historical commits retain their original contents and notices.

UI inspiration and historical comparison: https://github.com/mysterious64/OverParse
