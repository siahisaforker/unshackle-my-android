# unshackle my android
android's runtime compiler (ART) normally decides how much app code to compile ahead of time. i wanted to see what happens if we just make it compile everything it can.

this uses adb to ask android to compile installed apps using the `speed` filter. it can make apps feel faster because there's less code left to compile while you're using them. it's not magic though, some apps won't benefit and it'll take up more storage.

no root needed on devices that support the package compile command
## usage

install python 3.11+ and adb, turn on usb debugging, and plug in your phone.

```bash
python3 unshackle.py scan
python3 unshackle.py status
python3 unshackle.py run --dry-run
python3 unshackle.py run --mode speed
```

`scan` gets basic device info, `status` shows what ART has compiled, and `--dry-run` lists the apps without changing anything.

it compiles all installed packages by default. use `--scope user` if you only want downloaded apps. `--scope system` does the opposite. you can also switch to `speed-profile` or `verify` with `--mode`.

if it gets interrupted:

```bash
python3 unshackle.py run --resume
```

it saves results after every app, so you don't have to restart from zero. reports are in `~/.local/state/unshackle-my-android/` on linux or `%LOCALAPPDATA%\UnshackleMyAndroid\` on windows.

it checks battery, storage and thermals before and during compilation. by default it wants at least 30% battery and 4 gib free. it'll stop if android reports serious overheating.

some apps load extra dex files that ART can't fully compile. a successful command doesn't necessarily mean every method in the app was compiled. the script checks the actual compiler filters when it's done.

app and system updates can replace compiled code, so you might need to run it again. `speed-profile` is sometimes better than `speed`, especially if you're low on storage or ram.
