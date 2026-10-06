# Connect your Google Sheet

This guide connects Job Hunter to a Google Sheet that you own, so you can see everything the agent does in a
normal spreadsheet: jobs it found, what it sent, replies, stops, and anything waiting for you.

You do not need any technical knowledge. You will copy and paste two files, click a few buttons, and paste
two things into the terminal. It takes about 5 minutes.

Good to know before you start:

* The master copy of everything stays on your computer. The Sheet is a readable copy that updates about
  every 20 minutes. If you delete the Sheet, nothing is lost: connect a new one and it is rebuilt.
* The small program you paste into the Sheet (an "Apps Script") can only touch this one spreadsheet. It
  cannot read your email, your Drive or any other file.
* Nobody else can write to the Sheet without the secret code that the Sheet shows you in step 3.

> About the pictures: the screenshots for this guide are described in boxes like this one. The image files
> are listed in [img/sheets/README.md](img/sheets/README.md) and will be added there.

## What you need

* The Job Hunter folder on your computer, installed with `./install.sh` and set up with `./jobhunter init`
  (see the main README).
* Your owner PIN (you chose it during `./jobhunter init`).
* A Google account. A personal Gmail account works best. Some work or school accounts do not allow the
  "Anyone" setting used in step 4; see Troubleshooting if that happens.

## Step 1: Create a new, empty Google Sheet

1. Open [sheets.new](https://sheets.new) in your browser. This creates a new, empty spreadsheet.
   (Or go to Google Sheets and click Blank.)
2. Click "Untitled spreadsheet" at the top left and give it a name, for example `Job hunt log`.

> Screenshot 1 (`01-new-sheet.png`): an empty Google Sheet with the name "Job hunt log" at the top left.

## Step 2: Paste the Job Hunter script

1. In the Sheet's menu bar click **Extensions**, then **Apps Script**. A new browser tab opens with a code
   editor. It shows a file called `Code.gs` with a few lines like `function myFunction() {}`.
2. Click inside that code, select all of it (Cmd+A on a Mac, Ctrl+A on Windows or Linux) and delete it.
3. Copy the Job Hunter script to your clipboard. In the terminal, inside the Job Hunter folder, run:

   * on a Mac: `pbcopy < sheets/Code.gs`
   * on Linux: `xclip -selection clipboard < sheets/Code.gs` (or open `sheets/Code.gs` in a text editor,
     select all and copy)

4. Click in the empty editor and paste (Cmd+V or Ctrl+V). The editor now shows a long script that starts
   with `openclaw-job-hunter: Google Sheets mirror`.
5. Now the settings file. Click the **gear icon** (Project Settings) in the left bar, and tick the box
   **Show "appsscript.json" manifest file in editor**.
6. Click the **< >** icon (Editor) in the left bar. A second file, `appsscript.json`, is now listed. Click it,
   select everything in it and delete it.
7. Copy the settings file: on a Mac `pbcopy < sheets/appsscript.json` (on Linux use xclip or a text editor
   as above), then paste it into the empty `appsscript.json`.
8. Click the **Save** icon (the small disk above the code) or press Cmd+S or Ctrl+S.

> Screenshot 2 (`02-apps-script-editor.png`): the Apps Script editor with `Code.gs` and `appsscript.json` in
> the file list on the left and the pasted script on the right.

Why the settings file matters: it limits the script to "this spreadsheet only" and to showing its own menu.
Without it Google would ask for wider access than needed.

## Step 3: Set up the tabs and get your secret

1. Go back to the browser tab with your Sheet and **reload the page** (Cmd+R or Ctrl+R, or the reload button).
2. Wait a few seconds. A new menu called **Job Hunter** appears next to Help.

   > Screenshot 3 (`03-job-hunter-menu.png`): the Job Hunter menu opened, showing "Set up or repair this
   > sheet", "Show connection secret", "Make a new secret" and "Re-apply formatting".

3. Click **Job Hunter > Set up or repair this sheet**.
4. Google asks for permission the first time. This is normal for any script you add yourself:

   1. "Authorization required": click **OK** or **Continue**.
   2. Choose your Google account.
   3. You may see **"Google hasn't verified this app"**. The "app" is the script you just pasted; it is not
      published anywhere, so Google has not reviewed it. Click **Advanced** (small link at the bottom left),
      then **Go to Untitled project (unsafe)** (the name may be different).

      > Screenshot 4 (`04-not-verified.png`): the "Google hasn't verified this app" screen with the Advanced
      > link, and below it the "Go to ... (unsafe)" link.

   4. The next screen lists what the script may do: **"View and manage spreadsheets that this application
      has been installed in"** and **"Display and run third-party web content in prompts and sidebars
      inside Google applications"**. That is only this one Sheet and the Job Hunter menu and dialog. Click
      **Allow**.

      > Screenshot 5 (`05-allow.png`): the permission list with the two lines above and the Allow button.

5. If nothing seems to happen after you click Allow, click **Job Hunter > Set up or repair this sheet**
   again. The tabs appear (Start here, Dashboard, Approvals, Jobs and so on) and a box opens that shows your
   **connection secret**: a line of 64 letters and digits.

   > Screenshot 6 (`06-secret-dialog.png`): the "Job Hunter connection" box with the secret in a grey field.

6. Leave this box open or come back to it later: **Job Hunter > Show connection secret** shows the same
   secret again at any time. Treat it like a password and do not share it.

## Step 4: Publish the script as a web app

This gives the script a private web address so the agent on your computer can send it updates.

1. Go to the Apps Script browser tab (from step 2).
2. Click the blue **Deploy** button at the top right, then **New deployment**.
3. Next to "Select type", click the **gear icon** and choose **Web app**.
4. Fill in:

   * Description: `job hunter`
   * Execute as: **Me** (your email address)
   * Who has access: **Anyone**

   > Screenshot 7 (`07-deploy-settings.png`): the New deployment box with type Web app, Execute as "Me" and
   > Who has access "Anyone".

5. Click **Deploy**. If Google asks for permission again, allow it the same way as in step 3.
6. You now see **Web app URL**: a long address that starts with `https://script.google.com/macros/s/` and
   ends with `/exec`. Click **Copy**.

   > Screenshot 8 (`08-web-app-url.png`): the "Deployment successfully created" box with the Web app URL and
   > its Copy button.

Why "Anyone": the agent on your computer is not logged in to Google, so Google must accept the request
without a login. That does not make your Sheet public. Every request must also carry your secret; a request
without the right secret changes nothing. Nobody can read the Sheet through this address either: without the secret it
only answers with an error.

## Step 5: Connect from your computer

1. In the terminal, inside the Job Hunter folder, run:

   ```
   ./jobhunter sheet connect
   ```

2. Type your owner PIN (nothing shows while you type; that is on purpose) and press Enter.
3. When asked for the web app URL, paste the address from step 4 and press Enter.
4. When asked for the connection secret, paste the secret from step 3 and press Enter. It stays hidden.
5. The command checks the connection, sets the Sheet's time zone to yours, and fills every tab for the first
   time. When it says **Connected**, go back to your Sheet: the tabs now show your data.

From now on the Sheet updates by itself about every 20 minutes. To update it right now, run
`./jobhunter sheet sync`.

## What you will see

| Tab | What it shows |
|---|---|
| Start here | A short explanation, the agent's state and the time of the last update |
| Dashboard | Today, the last 7 and 30 days and all time; the funnel from jobs found to interviews; limits used today with bars; anything stopped and what to do; a 14 day trend. A red banner appears if important messages could not reach your chat |
| Approvals | Messages and applications waiting for you, with the full text |
| Jobs | Every job that passed your filters, with the fit score, the reason and any deal breakers |
| Skipped by filters | Jobs your filters removed in the last 30 days, with the reason in plain words |
| Applications | What was submitted, where, with which resume, and the proof |
| Outreach | Every email and LinkedIn message: to whom, why this person, the text, and whether they replied |
| Follow-ups | Each conversation: when the one follow-up is due, what they said, and the next step |
| QC log | How each draft did in the quality check |
| Daily summary | One line per day with the counts |
| Alerts | Everything that made the agent stop, and what you need to do; also site accounts created, email codes used and CAPTCHAs waiting for you (see below) |
| Limits and settings | How the agent works, the limits it works under and how much is used today, which sites it may use with your Chrome logins, and the optional email finder's free credits (see below) |

### The Limits and settings tab

This tab is rewritten at every update. It has five blocks, each under a blue heading row:

- **How the agent works**: approval mode, email route and whether it is connected, LinkedIn on or off, the
  limit tiers, the warm-up week, whether the safety plugin is running, and your time zone.
- **Daily limits (last 24 hours)**: every limit with how much of it is used. A limit that is used up turns
  amber.
- **Browser sites (your Chrome logins)**: the Chrome profile you picked, then one row per site (Gmail,
  LinkedIn, Naukri, Indeed, Glassdoor, Foundit, Instahyre, Wellfound, and any other site you allowed). The
  Value says **Allowed** (green), **Not allowed** (grey, the default for every site until you say yes),
  **Revoked** (grey, you took it back), **Needs you** (amber, the login expired or shows another account) or
  **Unknown** (amber, the answer could not be read, so the agent does not use that site). The Sheet only
  shows your answers; you give or take back consent on your computer with `./jobhunter browser consent` and
  `./jobhunter browser forget <site>`, never in the Sheet.
- **Email finder (optional)**: Off unless you turned it on. When it is on or was used in the last 31 days,
  it shows the addresses found, and for each service you connected the free credits left in the last 31
  days, the credits used today against its daily cap, and whether it stopped itself (red) or the service
  said its free credits are used up (amber). See [EMAIL-FINDER.md](EMAIL-FINDER.md).
- **Email codes and site accounts**: one row per job site and permission (email codes, site accounts) with
  **Granted**, **Declined**, **Taken back**, **Not asked** or **Unavailable: Gmail not allowed**, then
  "Email codes used (24 h)", "New site accounts (24 h)", "CAPTCHA hand-offs (24 h)" and "Open CAPTCHA tasks",
  each as used of the limit (for example "New site accounts (24 h): 0 of 3").

### Email code, site account and CAPTCHA rows in the Alerts tab

Next to the stops, the Alerts tab lists the steps the agent took on company career sites. Rows starting with
`S` are code steps: "Created an account on kestrel.wd5.myworkdayjobs.com for Kestrel Commerce, Senior
Analyst (with your address)", "Signed in", "Used an email code from myworkday.com ..." (Info), or a failed
sign-in or rejected code (Warning). Rows starting with `K` are CAPTCHA tasks: **Needs you** with "Solve it in
the agent's browser window, then /jh continue K7QA" and the deadline, then **Resolved** or **Skipped**. The
area reads like "Job forms: Workday". The Sheet never shows a code, a link or a password. Jobs skipped because
a CAPTCHA was not solved in time appear in the Skipped tab as "CAPTCHA not solved in time". The digest in your
chat adds a "Job sites:" line with these counts.

`./jobhunter status` and the digest in your chat carry the same two lines in short form, for example
`Browser sites allowed: Gmail, LinkedIn (Chrome profile "Personal"); every other site is off` and
`Email finder: on; 4 addresses found in 31 days; 120 of 180 free credits left (31 days)`.

Colors: **green** means done, **amber** means waiting, **red** means stopped or failed, **grey** means
skipped, **blue** means a reply or an interview. Fit and QC scores go from red (low) to green (high). A
follow-up date that has passed turns amber.

Dates and times are real dates in your time zone, so you can sort and filter by them. Links show a short
label such as "Open posting" or "Profile"; click them to open the page.

## What you can change in the Sheet

The **light yellow columns** are yours. The agent reads your choices at the next update (within about 20
minutes) and never overwrites them:

| Tab | Column | Choices | What happens |
|---|---|---|---|
| Approvals | Your decision | Approve, Skip | Approve sends the message or application as shown. Skip drops it |
| Jobs | Your call | Apply anyway, Never apply | Apply anyway puts the job back in the queue; every safety check still applies. Never apply closes it |
| Skipped by filters | Your call | Apply anyway | Sends the job to the evaluator even though a filter removed it |
| Applications | Outcome | No response yet, Rejected, Screening call, Interview, Offer, Withdrawn, Role closed | Stops follow-ups and records the result |
| Applications, Outreach | Your notes | anything | Kept for you |
| Follow-ups | Outcome | No response yet, Rejected, Screening call, Interview, Offer, Referred, Role closed (no Withdrawn: a conversation cannot be withdrawn) | Records the result of the conversation |

Everything else is filled by the agent. If you type in those cells, Google shows a warning, and your change
is replaced at the next update.

You can safely sort, filter, rename tabs, move columns, change column widths and add your own columns. Please
do not delete rows or the ID column; the agent uses the ID to find each row.

If a choice you made cannot be applied (for example you approved something that had already expired), you
get a short message in your chat explaining why.

## Updating the script later

When a new version of Job Hunter changes `sheets/Code.gs` (the update command tells you), do this once:

1. Copy the new script (`pbcopy < sheets/Code.gs` on a Mac) and paste it over the old one in the Apps Script
   editor, then Save.
2. Click **Deploy > Manage deployments**, click the **pencil icon** (Edit) on your deployment, set
   **Version** to **New version**, and click **Deploy**.

This keeps the same web address, so you do not need to run `./jobhunter sheet connect` again. Do not use
"New deployment" for updates: it creates a new address, and you would have to connect again.

## Troubleshooting

Run `./jobhunter sheet ping` to test the connection. `./jobhunter status` shows the time of the last update
and the last error.

| What you see | What it means | What to do |
|---|---|---|
| "The sheet answered with a Google sign-in page" | Who has access is not "Anyone" | Deploy > Manage deployments > pencil > Who has access: Anyone > Deploy |
| "The sheet refused the connection secret" | The secret was mistyped, or a new one was made | Job Hunter > Show connection secret, then run `./jobhunter sheet connect` again |
| "The script in your sheet is older than this version" | Job Hunter was updated but the Sheet still has the old script | Follow "Updating the script later" above |
| "the web app address must ... end in /exec" | The wrong address was copied (for example the editor address) | Deploy > Manage deployments, copy the Web app URL |
| "could not reach the sheet web app" | No internet, or Google is slow | Nothing; the next update retries by itself |
| The Job Hunter menu does not appear | The page was not reloaded, or the script was not saved | Save in Apps Script, reload the Sheet, wait 10 seconds |
| "Exception: You do not have permission" in the Sheet | Permission was not given yet | Job Hunter > Set up or repair this sheet, and allow |
| Your work or school account does not offer "Anyone" | Your administrator blocks it | Use a personal Google account for the Sheet (see below) |
| A tab is missing or looks broken | A tab or header was deleted or changed | Job Hunter > Set up or repair this sheet, then `./jobhunter sheet sync --full` |
| Colors or dropdowns disappeared | Formatting was changed | Job Hunter > Re-apply formatting |
| Dates are in the wrong time zone | The Sheet's time zone differs from yours | Run `./jobhunter sheet connect` again, or File > Settings > Time zone |
| You think the secret leaked | Someone else might write to the Sheet | Job Hunter > Make a new secret, then `./jobhunter sheet connect` |
| You want to start over | | Create a new Sheet, repeat this guide; the new Sheet is filled from your computer |

## Privacy settings

These live in `private/config.json` under `sheets`:

* `store_message_text`: set it to `false` to keep the text of messages off Google. The Sheet then shows
  "(kept on your computer)" instead, and you read the text with `./jobhunter inbox`.
* `person_name_style`: `first_last_initial` (default, for example "Alex R."), `full` or `first`.
* `skipped_tab_days`: how many days of skipped jobs the Skipped by filters tab keeps (30).
* `enabled`: set it to `false` to stop updating the Sheet.

`./jobhunter export` writes the same tables as CSV files into the `exports/` folder, if you prefer files.

## For technical users: a service account instead

If your Google Workspace administrator does not allow "Anyone" web apps, the alternative is a Google Cloud
service account with the Sheets API. It needs a Google Cloud project, the Sheets API turned on, a JSON key
stored on your computer, and the Sheet shared with the service account's email address. This route is not
built into this version of Job Hunter; the web app above is the supported way. A personal Google account for
the Sheet is the simplest workaround.
