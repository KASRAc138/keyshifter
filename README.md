# KeyShift

Fixes text typed in the wrong keyboard layout: `sghl` → `سلام`, `اثممخ` → `hello`.

## Run
Double-click `KeyShift.pyw`, or run `pythonw KeyShift.pyw`. You only need Python 3.8 or later. It has no dependencies.
On first run it adds itself to Windows startup. You can turn that off in the window.

## Use
| You do | It does |
|---|---|
| Select text → **Ctrl + Shift + Space** | Converts the selection to the other layout |
| Nothing selected → shortcut | Finds the wrong-layout end of the current line and fixes only that part |
| Press again right after | Undo (with 2 layouts) or cycle to the next layout (with 3 or more) |

After a fix, it switches your keyboard to the correct layout.

## How it works
- It reads every layout installed in Windows live, using `ToUnicodeEx`. There are no built-in tables, so any language pair works.
- It maps by physical key (scancode): the same key on layout A turns into the same key on layout B.
- It picks the direction automatically from the characters only one layout can type.
- It saves and restores your clipboard, and its temporary clipboard entries stay out of Win+V history.
- It skips terminal windows, because Ctrl+C there would interrupt the running program.
- The only thing written to disk is settings, in `%APPDATA%\KeyShift\config.json`.

## Build a single .exe (optional)
Run `build_exe.bat`. It needs `pip install pyinstaller`. The result is `dist\KeyShift.exe`.
