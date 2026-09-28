# Screenshots for the setup guides

The setup guides (`README.md`, `docs/SETUP-macos.md`) describe each step in words. Screenshots are not in the
repository yet: they must be taken on a real setup with a throwaway macOS user and fictional data only (no real
names, email addresses, phone numbers, sheet ids or web app addresses may be visible; blur the terminal prompt
if it shows a user name). Do not add mock-ups or edited images that pretend to be screenshots.

| File | What it must show |
|---|---|
| `01-terminal-xcode.png` | The `xcode-select --install` dialog |
| `02-claude-login.png` | `claude auth status --text` reporting a logged-in account (account name blurred) |
| `03-install-done.png` | The end of `./install.sh`: step 16 with DONE and the next steps |
| `04-keychain-prompt.png` | The macOS Keychain prompt shown by `./jobhunter browser import` |
| `05-jobhunter-profile.png` | The `jobhunter` browser window opened by `./jobhunter browser login linkedin` |
| `06-init-interview.png` | One question of the `./jobhunter init` interview with its suggested default |
| `07-doctor-green.png` | `./jobhunter doctor` with every line ok |
| `08-status.png` | `./jobhunter status` after the first day |

When an image is added, reference it from the guide as `![short description](img/setup/<file>)` next to the
step it shows.
