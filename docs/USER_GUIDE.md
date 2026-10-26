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

The brief also warns you about meetings that overlap, a day with no free block for deep work, delegated work that is late, promises you made that are due, and people who have written several times without an answer.

**During the day.** When something new and important arrives (a P1), you get one alert. Accepted delegations and questions from your team come to you as short notices. About 10 minutes before each meeting you get a short prep: who is in it, what is open between you, recent emails, and what to push for (`workday.prep_minutes_before` in `config/config.yaml`).

**Your team, chased for you.** Work nobody acknowledged is chased (twice at most); accepted work gets a "how is it going?" the day before it's due, and a status request if it slips. You only hear about it when something is late or someone has a question.

**Friday at 5 pm.** The team pulse (who finished what, what's open, what slipped, who is overloaded) and a report of everything Atlas did for you that week, which you can undo.

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

### Chief-of-staff requests

| You say | What happens |
|---|---|
| "Prep me for my next meeting" / "Prep me for my 3 pm" | Who is in it, what is open between you, and what to push for |
| "Triage my inbox" | Emails sorted into needs-you, delegate and FYI, with drafted replies read back; say yes to send |
| "Notes from the pricing review: Rahul will send margins by Monday..." | A summary, the decisions, and each action item offered as a delegation |
| "Block focus time this afternoon" | Holds the first free gap, after your yes |
| "Move my 4 pm to tomorrow at 10" | Moves it and updates the attendees, after your yes |
| "Remind me to call the auditor at 3" | A reminder on your phone at 3 |
| "What did I promise?" / "What am I forgetting?" | Promises found in your sent mail, your reminders, and people waiting on you |
| "What am I waiting on?" | Delegated work, late first |
| "How's the team doing?" | The team pulse |
| "Rahul finished the vendor review" | Closes the follow-up |
| "Remember that Acme's renewal is in March" | Remembered and used in prep, decisions and drafts; "forget that..." removes it |
| "Should I hire a second sales lead?" | A recommendation, pros and cons, the main risk, and what to find out first, from your own context |
| "Draft a LinkedIn post about our Pune office" | A draft in your writing style (learned from your sent mail) |
| "What's the latest on Tata Motors?" / "What's the weather in Mumbai?" | A web search, answered in a few sentences with the source named. Turn it off with `models.web_search: false` in `config/config.yaml` |
| "Make me a 6-slide deck for the Sattva pitch: our offer, pricing and the ask" | A real PowerPoint file with speaker notes; tap **Open** under the reply to open or share it. Missing numbers are left as [placeholders] |
| "What did you do this week?" / "Undo that" | Everything it did; undo reopens items, reverts team changes, or moves a meeting back after a yes. Sent messages can't be unsent. |
| "Send emails to my team without asking" | Emails to people on your team go straight away; anyone else still needs your yes. "Always ask before emailing" switches it back. Works for emails, replies, Teams messages, meetings and delegations. |

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
- **Desktop:** say "Atlas" (or "Atlas, brief me") or press Ctrl+Alt+Space. Press the hotkey again to interrupt it.
- **Telegram:** type or send a voice note; it answers with text and a voice note, and shows **Yes / Cancel** buttons when it needs your confirmation.

## Privacy and cost

Your data stays in a SQLite file on your own computer or server. The only outside service it pays for is Claude, which sees item titles and descriptions to score them. The monthly cap (default 20 dollars) is in `config/config.yaml`; past it, it keeps working with rules only and tells you. `GET /usage` shows what you've spent.
