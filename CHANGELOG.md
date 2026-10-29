# Changelog

## Unreleased

- Prevent login-time SQLite locks in the `wacli` session store.
- Check authentication read-only to prevent false outage alerts during sync writes.
- Require repeated authentication-check failures before sending an outage email, so one transient `wacli` probe error no longer alerts.
- Ship third-party license notices in the image and label it with the licenses of everything it bundles.
