# Troubleshooting

## First Run Does Not Show Setup

Check `/api/setup/status`. If it returns `{"initialized": true}`, at least one user exists in SQLite. Use a fresh data volume for a clean setup.

## Settings Save Works But Adapter Still Uses Mock Data

This is expected in phase one. The credential center is real, but external adapters are mocked until the qB, M-Team, TMDB, AI, and WeChat Claw phases are implemented.

## qB 2 Is Locked

Open Downloads, select qB 2, and complete administrator verification. The grant lasts 15 minutes by default and applies only to the current session.

## Diagnostic Export Safety

Use Debug -> Export safe JSON. The export must not contain API keys, cookies, passwords, tokens, complete private IP addresses, or full local paths. If a future adapter adds new sensitive fields, extend `app/utils/redaction.py`.

## Key Rotation

`APP_CONFIG_ENCRYPTION_KEY` encrypts service credentials. Rotating it requires decrypting existing rows with the old key and re-encrypting with the new key. Do not change it casually on a live deployment.

