# "Hey Siri, daily brief" and "Hey Google, daily brief"

The phone app starts the brief by itself when it opens at `/?autostart=brief`. Both assistants can open a URL by voice, so the shortcut is just that URL.

Before you start: install the phone app and sign in once (SETUP.md, section 12). Your link is

```
https://<your assistant address>/?autostart=brief
```

for example `https://assistant.example.com/?autostart=brief`.

## iPhone: Siri Shortcut

1. Open the **Shortcuts** app and tap **+** (top right).
2. Tap **Add Action**, search for **Open URLs**, and tap it.
3. Tap the blue **URL** field and paste your link.
4. Tap the name at the top (it says "Open URLs"), choose **Rename**, and type **Daily brief**. This name is what you say to Siri.
5. Tap **Done**.
6. Say **"Hey Siri, daily brief."** The app opens and reads your brief.

Optional, to make it automatic: **Shortcuts → Automation → + → Time of Day → 8:00 AM → Run Immediately**, then add the same **Open URLs** action.

If the shortcut opens Safari instead of the installed app, delete the app icon and add it again from Safari with **Share → Add to Home Screen**, then run the shortcut once and choose the app when asked.

## Android: Google Assistant routine

1. Open the **Google** app (or **Google Assistant** settings), tap your profile picture → **Settings → Google Assistant → Routines**.
2. Tap **New** (or **+**).
3. Under **Starters**, tap **Add starter → When I say to Google Assistant**, type **daily brief**, and tap **Done**.
4. Under **Actions**, tap **Add action → Try adding your own**, type **open https://assistant.example.com/?autostart=brief** (with your address), and tap **Done**.
5. Tap **Save**.
6. Say **"Hey Google, daily brief."** Chrome or the installed app opens and reads your brief.

If your phone has Gemini instead of Google Assistant, routines are under the **Google Home** app → **Automations**; use the same starter phrase and the "open website" action.

## Desktop

The desktop app has its own wake word: say **"hey jarvis, brief me"**, or press Ctrl+Alt+Space and say "brief me".

## Telegram

Send the bot a voice note saying "brief me", or just type it.

## Troubleshooting

- **The page opens but nothing is read out:** iPhones block audio until you have tapped the page once. Tap the orb; it plays. Opening from the home-screen app (rather than a Safari tab) avoids this after the first time.
- **"Connect to your assistant":** the app on that browser hasn't been signed in. Open Settings in the app and enter your secret once.
- **The brief is empty:** your accounts haven't synced yet. The backend syncs every 15 minutes; tap the refresh button on the brief card afterwards. If it stays empty, see "Checking what is connected" in SETUP.md.
