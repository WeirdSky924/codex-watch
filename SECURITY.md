# Security

## Full-access default

This project preserves its original operating mode: unless `--safe` is passed,
it starts Codex with `--dangerously-bypass-approvals-and-sandbox`. This grants
Codex unrestricted local execution and is only appropriate inside an external
sandbox or on a machine where the user accepts that risk.

Use the safer mode for ordinary installations:

```bash
codex-watch --safe
```

## Logs

Terminal output can contain source code, prompts, local paths, request IDs, and
other sensitive data. Logs are stored in a private XDG state directory by
default. Do not attach logs to public issues without reviewing and redacting
them first.

## Reporting issues

Do not open a public issue containing credentials, access tokens, private
rollout files, or unredacted watchdog logs. Contact the repository maintainer
privately for vulnerabilities until a dedicated security contact is published.
