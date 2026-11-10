# unshackle my android
android's runtime compiler (ART) normally decides how much app code to compile ahead of time. i wanted to see what happens if we just make it compile everything it can.

this uses adb to ask android to compile installed apps using the `speed` filter. it can make apps feel faster because there's less code left to compile while you're using them. it's not magic though, some apps won't benefit and it'll take up more storage.

no root needed on devices that support the package compile command. if adb is missing, the script asks if you want to install it locally or system-wide. local downloads google platform-tools, system-wide uses your package manager. neither happens without asking.

## usage

install python 3.11+, turn on usb debugging, and plug in your phone. if adb isn't installed, the script can get it for you.

```bash
python3 unshackle.py
```

that's it. it'll check for adb, ask to install it if it's missing, find the phone, then compile all installed packages using `speed`. it'll check the battery, storage and temperature first, show progress, and check the compiled code when it finishes. if everything compiles successfully, it'll reboot the phone afterward. if any packages fail or the run gets interrupted, it won't reboot.

if you just want to check things without compiling anything:

```bash
python3 unshackle.py scan
python3 unshackle.py status
python3 unshackle.py --dry-run
```

`scan` gets basic device info, `status` shows what ART has compiled, and `--dry-run` lists the apps without changing anything. add `--no-reboot` if you don't want the phone to restart after compiling.

it compiles all installed packages by default. use `--scope user` if you only want downloaded apps. `--scope system` does the opposite. you can also switch to `speed-profile` or `verify` with `--mode`.

if it gets interrupted:

```bash
python3 unshackle.py --resume
```

it saves results after every app, so you don't have to restart from zero. reports are in `~/.local/state/unshackle-my-android/` on linux or `%LOCALAPPDATA%\UnshackleMyAndroid\` on windows.

it checks battery, storage and thermals before and during compilation. by default it wants at least 30% battery and 4 gib free. it'll stop if android reports serious overheating.

some apps load extra dex files that ART can't fully compile. a successful command doesn't necessarily mean every method in the app was compiled. the script checks the actual compiler filters when it's done.

app and system updates can replace compiled code, so you might need to run it again. `speed-profile` is sometimes better than `speed`, especially if you're low on storage or ram.
