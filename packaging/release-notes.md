## Download

Pick the file for the computer you want to control. You don't need the **Source code** files: GitHub adds those to every release.

| Computer | File | First open |
| --- | --- | --- |
| Mac (Apple Silicon or Intel) | `Lazy-Remote-macOS.zip` | Unzip, move to Applications, then Control-click → **Open** |
| Windows 10 or 11 | `Lazy-Remote-Windows.zip` | Unzip, open the **Lazy Remote** folder and run **Lazy Remote.exe**. SmartScreen: **More info → Run anyway** |
| Linux (x86-64; Ubuntu 22.04+, Debian 12+, RHEL 9+) | `Lazy-Remote-Linux-x86_64.tar.gz` | Extract and run `lazy-remote/lazy-remote`. The setup guide shows a one-time input command |

Then open the app, click **Start server**, and follow the setup guide.

## Check the download

`SHA256SUMS.txt` lists each file's SHA-256 checksum. Compare it before opening the app:

- Mac or Linux: `shasum -a 256 -c SHA256SUMS.txt --ignore-missing`
- Windows (PowerShell): `Get-FileHash .\Lazy-Remote-Windows.zip`

To prove a file was built by this project's GitHub workflow from this release's commit, use the [GitHub CLI](https://cli.github.com/):

```
gh attestation verify Lazy-Remote-macOS.zip --repo Reaper-Dhan/Lazy-Remote
```
