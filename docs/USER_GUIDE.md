# User guide

Your assistant reads your calendar, inbox and task boards, decides what matters, and handles the rest once you say yes. You can talk to it on your phone, your computer, or Telegram. It never sends anything, declines anything or changes anything without your yes.

## Your day

**08:00, the morning brief.** Your phone shows a notification and the desktop app chimes. Open it (or say "Hey Siri, daily brief") and you hear something like:

> Good morning. You have 4 meetings, 3 tasks and 1 email to act on today. 2 need you: the board deck and the investor call at 11. I suggest delegating the vendor contract review to Rahul and the hiring plan to Arjun. I suggest declining the weekly marketing sync at 4 pm. You can drop the newsletter. Shall I go ahead?

Answer with:

- **"Yes"** or **"Go ahead with all"**: everything it suggested happens.
- **"Skip the second one"**, then "yes": everything except that.
- **"Cancel"**: nothing happens.
- **"Tell me more"**: the rest of the list, if the brief was trimmed.

**During the day.** When something new and important arrives (a P1), you get one alert. Accepted delegations and questions from your team come to you as short notices.

**18:30, the evening wrap-up.** What's still open, who accepted, who hasn't answered yet, and an offer to move unfinished items to tomorrow.

## Things you can say

| You say | What happens |
|---|---|
| "Brief me" / "What's my evening look like?" | The morning brief or evening wrap-up |
| "What's on my calendar tomorrow?" | A list of tomorrow's items |
| "Why is the budget sync high priority?" | The reason behind its score |
| "Delegate the vendor review to Rahul, due Monday" | It reads it back ("Sending Rahul Vendor contract review, due Monday, by email and Telegram. Confirm?"); say yes to send |
| "Give it to Raul" | Speech errors are fine: it works out you meant Rahul, or asks "Rahul Kumar or Rahul Mehta?" |
| "Decline the 4 pm" | Declines with a polite note, after your yes |
| "Push the hiring plan to next week" | Defers it; it comes back that day |
| "Mark the board deck done" | Closes the task in Jira, Asana or ClickUp, after your yes |
| "Drop the newsletter" | Removes it from your list |
| "Make this top priority" | Raises it to P1 and remembers you care about this kind of thing |
| "From now on, anything from the board is P1" | Saved as a standing rule |

It won't pay, buy, refund, approve spending or delete anything. Ask it to delegate that work to the right person instead ("Ask Meera to pay the Acme invoice").

## Priorities

- **P1, do it yourself:** needs you personally.
- **P2, delegate:** someone on your team can own it; it suggests who, based on what they own.
- **P3, decline:** a meeting you don't need to attend.
- **P4, drop:** no action needed.

It scores using your goals, who's asking (board, investors, key customers rank higher) and deadlines from `config/rubric.yaml`, and learns from you: when you overrule it, it remembers. To see or delete what it has learned, open `/docs` and use `GET /preferences` and `DELETE /preferences/{id}`.

## What your team sees

A delegation arrives as an email (and on their Telegram, Slack or Teams if set up) with what to do, why it matters, what done looks like, the deadline and links. They accept by replying "accepted" to the email, pressing **✅ Accept** in Telegram, or reacting ✅ in Slack. If they don't answer, they get one reminder 24 hours before the deadline, and you're told if it goes overdue. If they press **❓ Question** or reply with a question, it comes to you.

## The apps

- **Phone:** tap the orb and speak, or type. Pending actions show **Yes / Cancel** cards; the delegations list shows who accepted.
- **Desktop:** say "hey jarvis" or press Ctrl+Alt+Space. Press the hotkey again to interrupt it.
- **Telegram:** type or send a voice note; it answers with text and a voice note, and shows **Yes / Cancel** buttons when it needs your confirmation.

## Privacy and cost

Your data stays in a SQLite file on your own computer or server. The only outside service it pays for is Claude, which sees item titles and descriptions to score them. The monthly cap (default 20 dollars) is in `config/config.yaml`; past it, it keeps working with rules only and tells you. `GET /usage` shows what you've spent.
