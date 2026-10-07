# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please don't open a public issue. Report it privately through GitHub's [private vulnerability reporting](https://github.com/Helpmeeters/assist-learner/security/advisories/new) instead.

Include what you found, how to reproduce it, and what an attacker could do with it. You should get a reply within a week.

## What's in scope

Assist Learner runs inside Home Assistant with its full permissions. Issues that are especially relevant:

- Writing outside `config/custom_sentences/<language>/assist_learner.yaml`, or a sentences file that breaks other local commands.
- A learned or replayed command affecting an entity in a denied domain (locks, alarm panels, valves, garage and gate covers, or a user-added domain).
- Stored utterances in `config/.storage/assist_learner`, or entity IDs, leaking through diagnostics, which are supposed to be redacted.
- A replayed multi-step command acting outside the scope that was approved.
