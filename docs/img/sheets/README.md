# Screenshots for docs/GOOGLE-SHEETS.md

The setup guide describes each screenshot in a box. The images are not in the repository yet: they must be
taken from a real setup with a throwaway Google account and a sheet that holds only fictional data (no real
names, email addresses, sheet ids or web app addresses may be visible; blur the address bar, the account
avatar and the web app URL). Do not add mock-ups or edited images that pretend to be screenshots.

| File | What it must show |
|---|---|
| `01-new-sheet.png` | An empty Google Sheet named "Job hunt log" |
| `02-apps-script-editor.png` | The Apps Script editor with `Code.gs` and `appsscript.json` in the file list and the pasted script |
| `03-job-hunter-menu.png` | The Job Hunter menu opened, with its four items |
| `04-not-verified.png` | Google's "Google hasn't verified this app" screen with the Advanced link and the "Go to ... (unsafe)" link |
| `05-allow.png` | The permission list (this spreadsheet only, and the menu and dialog) with the Allow button |
| `06-secret-dialog.png` | The "Job Hunter connection" box with a secret (use a fresh throwaway secret, then make a new one) |
| `07-deploy-settings.png` | New deployment: type Web app, Execute as Me, Who has access Anyone |
| `08-web-app-url.png` | "Deployment successfully created" with the Web app URL blurred |

When an image is added, replace its description box in the guide with `![short description](img/sheets/<file>)`
and keep the one line description under it.
