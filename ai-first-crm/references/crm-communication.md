# CRM communication: e-mail, calendar, campaigns

Read `crm-contract.md` first. The skill has no mailbox connection and never sends anything. It imports e-mail and calendar files that the user exports from their mail program and writes drafts that the user sends with their own tools.

## Contents

- [What the user provides](#what-the-user-provides)
- [Importing e-mail and calendar files](#importing-e-mail-and-calendar-files)
- [E-mail settings](#e-mail-settings)
- [How messages and events are stored](#how-messages-and-events-are-stored)
- [Campaigns](#campaigns)
- [Limits](#limits)

## What the user provides

`.eml` files (one message each), `.mbox` files (a whole folder, as exported by Thunderbird, Apple Mail, or Google Takeout), and `.ics` files (calendar exports or invitations). Outlook `.msg` files are refused with a request to save the messages as `.eml`. Ask for the files; never search the machine for a mailbox.

## Importing e-mail and calendar files

```text
crm_ingest.py plan  --target <wiki> --files <file or folder> ... --actor <actor> --output <plan>
                    [--mailbox-owner <member e-mail>] [--own-addresses a,b] [--visibility METADATA|SUBJECT|SHARE_EVERYTHING]
                    [--include-bulk] [--include-internal] [--include-private] [--create-contacts] [--skip-failed]
crm_ingest.py apply --target <wiki> --plan-file <plan> --expect-plan-sha256 <hash>
```

Show the report before applying: messages and events to create or update, everything skipped with its reason (bulk or newsletter mail, blocklisted sender, internal mail, private event, message already in the trash, older calendar sequence), every redaction, contact and company proposals, and merged invitations. Contact proposals are applied only with `--create-contacts` after the user agreed; they are never created silently. A plan with errors (an unreadable file, a credential that could not be redacted) is applied only after the user agreed to leave those files out with `--skip-failed`.

What the import does:

- Credentials are removed before planning: URL parameters such as `token`, `pwd`, `password`, `code`, `key`, `sig`, `auth`, `access_token`, lines such as `Passwort:` or `Passcode:`, phrases such as "das Passwort lautet …" when the value contains a digit or a symbol, and token shapes (cloud and payment API keys, Slack webhooks, bearer and basic authorization headers, JSON web tokens, private keys) become `[credential removed]`, and the report names each redaction without the value.
- Character sets are decoded strictly, with a recorded fallback; HTML-only messages are converted to text with paragraphs and tables kept; attachments are listed by name and size only, never stored.
- One message is stored once, however many mailboxes it came from (by Message-ID). Threads follow References and In-Reply-To.
- Participants link to people (primary or additional e-mail, case-insensitive) and members (login e-mail). The direction follows the own addresses.
- Calendar events: time zones by IANA name, by Windows name (`assets/crm-windows-zones.json`), or from the file's own VTIMEZONE; all-day events; updates and cancellations by UID and SEQUENCE (an older sequence is ignored and reported); recurring events keep their rule, and exceptions become their own records.
- An invitation e-mail becomes a calendar event with the message text as description.

Every record carries its origin: `message-id:<id>` or `ical-uid:<uid>`.

## E-mail settings

`schema/crm/settings.json` → `email` (change it through `crm_config.py`):

| Key | Meaning |
|---|---|
| `blocklist` | addresses or `@domain` entries; a domain entry covers its subdomains; never imported, never proposed as contacts; not applied retroactively |
| `contact_creation` | `NONE`, `SENT` (participants of messages the owner sent), `SENT_AND_RECEIVED`, or `work-domains` (like SENT, never for free-mail domains; the default the CRM layer is set up with) |
| `exclude_group_emails` | skip group, list, and bulk mail (List-Id, List-Unsubscribe, Precedence, Auto-Submitted) |
| `import_internal` | import mail exchanged only between own addresses |
| `own_addresses`, `own_domains` | the team's addresses and domains, used for direction, internal mail, and contact proposals |
| `excluded_handles` | further addresses never imported |
| `free_email_domains` | domains never turned into companies |
| `default_visibility` | `METADATA` (no subject, no text), `SUBJECT` (subject only), or `SHARE_EVERYTHING` |

Visibility is applied when importing: what is not imported is not in the wiki. A later import never widens the visibility of an existing record. Calendar events know METADATA and SHARE_EVERYTHING only.

## How messages and events are stored

`message` records: subject, text (rich text), receivedAt, headerMessageId (unique), messageThreadId, direction, fromHandle, toHandles, ccHandles, visibility, participants, messageCampaign, isDraft. `calendarEvent` records: title, startsAt, endsAt, isFullDay, location, description, iCalUid (unique), recurrence, conferenceLink, conferenceSolution, isCanceled, organizerHandle, externalCreatedAt, externalUpdatedAt, iCalSequence, visibility, participants. People show their messages and events in the record browser; a company also lists what its people took part in, and an opportunity what the people of its company took part in, as is usual in CRMs.

## Campaigns

The campaign objects follow common CRM conventions: `messageList`, `messageListMember` (with `status` SUBSCRIBED, PENDING, or UNSUBSCRIBED and the consent evidence `consentSource`, `consentAt`), `messageSuppression` (bounces, complaints, unsubscribes), `messageCampaign`.

```text
crm_campaign.py audience        (--list <id> | --filter <json> | --view <id>) [--topic <id>] [--include-auto-created]
crm_campaign.py plan-list       --actor <actor> --output <plan> (--filter <json> | --view <id>) (--name <name> [--description <text>] | --list <id>) [--consent-source <text> --consent-at <instant>] [--include-auto-created]
crm_campaign.py render          --actor <actor> --campaign <id> --output <plan> [--outbox <extra copy outside the wiki>] [--unsubscribe-mailto <address>]
crm_campaign.py import-results  --actor <actor> --file <csv> --output <plan> [--topic <id>] [--skip-failed]
crm_campaign.py apply           --plan-file <plan> --expect-plan-sha256 <hash>
```

An audience counts and excludes: people without e-mail, suppressed addresses, blocklisted addresses, members without consent (PENDING) or unsubscribed, and contacts created automatically from e-mail unless `--include-auto-created`. Members added without consent evidence stay PENDING and receive nothing. `render` checks every variable such as `{{person.name.firstName}}` against the data model and plans one `.eml` draft per recipient plus `manifest.json` in `records/_outbox/campaigns/<campaign>/<time>/`, together with the status RENDERED; applying the plan writes them into the wiki (and the optional copy outside); the user sends the drafts with their own mail or newsletter tool and brings the delivery results back with `import-results` (columns `email,status` with `bounced`, `complained`, or `unsubscribed`). Tell the user plainly that nothing was sent.

## Limits

No mailbox or calendar sync, no shared-inbox forwarding address, no sending, no meeting booking, no tracking or statistics, no sending-domain verification. Quoted earlier messages are not removed from message text. Blocklist changes do not remove earlier imports; linking happens when importing, so a person added later is linked on the next import of the same files. Recurrence rules with BYSETPOS, BYWEEKNO, BYYEARDAY, BYHOUR, or a frequency below daily are kept as text and not expanded.
