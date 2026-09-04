# Production AI Lead Generator Improvements

This document defines the final production direction for Lawyer Bot as an AI lead intake and handoff system for Advokat Jasurbek's team.

## Product Goal

The bot should not behave like a free legal advice assistant. It should behave like a professional intake specialist:

- understand the user's legal situation;
- collect enough context for a human lawyer to act quickly;
- qualify urgency and purchase intent;
- guide users toward paid consultation;
- hand off cleanly to the admin group;
- keep LLM cost predictable.

## Production Behavior

### User Intake

The bot should collect these fields naturally during conversation:

- problem summary;
- legal category;
- city or region;
- when the issue started;
- urgency or deadline;
- documents or evidence;
- phone number, only when handoff is close or the user offers it;
- Telegram username as fallback contact, not as a phone number.

The bot should ask only 1-2 focused questions per reply. Long interrogation-style messages reduce conversion.

### Legal Safety

The bot must avoid:

- legal advice;
- law article numbers;
- fine amounts;
- deadline calculations;
- prepared legal document text;
- guaranteed case outcomes;
- requests for passport, card, or highly sensitive data.

Safe phrasing:

> Men AI yordamchiman, yuridik maslahat bermayman. Lekin vaziyatingizni tushunib, advokat jamoasiga to'g'ri yetkazishga yordam beraman.

### Escalation Rules

Escalate when at least one is true:

- user has described the problem and explicitly asks for an advocate/lawyer;
- the situation is urgent and a legal problem is clear;
- user asks for lawyer/consultation repeatedly after minimal intake;
- AI response intentionally uses the configured escalation signal.

Do not escalate immediately when the first message is only:

- "advokat kerak";
- "maslahat kerak";
- "salom";
- "yordam kerak";
- an informational question about whether a lawyer is needed.

Instead, collect problem, location, documents, and urgency first.

## Telegram UX

### Private Chat

Messages should be:

- short;
- in the user's language;
- Telegram HTML-safe;
- explicit about paid consultation;
- free of markdown tables and long explanations;
- respectful when asking for phone number.

### Admin Group

Admin notifications should include:

- user display name;
- phone number if known;
- city/region;
- category;
- urgency;
- documents;
- problem summary;
- recent transcript;
- clear instruction to reply to the bot message.

Admin commands:

- `/stats` shows operational lead metrics.
- `/open` shows currently open lead cards.
- `/close` closes a user conversation when used as a reply to a bot notification.

Implemented inline lead status buttons:

- contacted;
- booked;
- paid;
- lost;
- closed.

Future admin command ideas:

- `/today` show today's lead summaries;
- `/export` export recent leads to CSV;
- `/tag` add simple labels such as hot, waiting, paid, lost.

## Rate Limit Strategy

Current user-level rate limiting is good for MVP production. Recommended production posture:

- keep per-user hourly limit;
- use friendly reset-time copy;
- avoid blocking admin replies;
- later add global daily LLM budget to protect spend;
- later add stricter limits for repeated empty/spam messages.

Suggested default:

- normal users: 60 messages/hour;
- suspicious repeated short messages: lower dynamic limit;
- admin/group commands: exempt or separately limited.

## LLM Cost Balance

Current approach uses one economical model for intake and profile extraction. This is sensible for production.

Recommended cost controls:

- keep recent history bounded;
- prefer structured heuristics before LLM calls for escalation decisions;
- use low temperature for extraction;
- avoid web search unless legal-information mode is intentionally reintroduced;
- keep responses short with prompt rules;
- log escalation reasons and intake readiness for tuning.

Future upgrade path:

- use a cheaper model for classification/extraction;
- use the current model for user-facing replies;
- cache extracted lead profile after handoff;
- summarize long conversations after 12-16 turns.

## Observability

Production should track:

- total conversations;
- new conversations in last 24 hours;
- total leads;
- leads in last 24 hours;
- active AI conversations;
- escalated conversations waiting for admin;
- closed conversations;
- total messages;
- lead conversion rate.

Implemented baseline:

- admin `/stats` command reads SQLite aggregates.
- admin `/open` command lists non-closed leads.
- admin inline status buttons update the lead pipeline.

Future observability:

- daily admin digest;
- failed LLM request count;
- average messages before escalation;
- leads with missing phone number;
- response time from admin after escalation.

## Production Readiness Checklist

- Environment variables are documented.
- Webhook secret is enforced.
- Health endpoint returns service mode.
- Telegram HTML fallback exists.
- Channel membership gate exists.
- User rate limit exists.
- AI failure escalates safely.
- Human handoff includes profile and transcript.
- Admin reply routing survives restarts through persistent notification registry.
- Tests run with `claude_folder` ignored.
- Existing databases are migrated with `python migrate_add_lead_status.py`.

## Important Follow-Ups

1. Add conversation close command for admins.
2. Add daily summary message to admin group.
3. Store phone number/contact separately in `Lead` if CRM/export becomes necessary.
4. Add production deployment checklist with webhook URL, bot permissions, and admin group setup.
5. Decide whether legal-information mode should stay removed or return as a separate guarded feature.
