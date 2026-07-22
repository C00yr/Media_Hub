# Troubleshooting

## First Run Does Not Show Setup

Check `/api/setup/status`. If it returns `{"initialized": true}`, at least one user exists in SQLite. Use a fresh data volume for a clean setup.

## A Module Is Unavailable After Saving Settings

Saving only stores a draft. Use “保存并测试”, confirm the connection test succeeds, and then enable the module. The diagnostics page performs a fresh check and shows the current failure reason. Verify the service address from inside the Media Hub container; a NAS browser being able to open an address does not prove the container can reach it.

For TMDB through Mihomo, verify the HTTP proxy port and use a hostname reachable from the container, such as `mihomo` on the same Docker network or `host.docker.internal` for a host-published port.

## qB 2 Is Locked

Open Downloads, select qB 2, and complete administrator verification. The grant lasts 15 minutes by default and applies only to the current session.

## Diagnostic Export Safety

Use Debug -> Export safe JSON. The export must not contain API keys, cookies, passwords, tokens, complete private IP addresses, or full local paths. If a future adapter adds new sensitive fields, extend `app/utils/redaction.py`.

## Key Rotation

`APP_CONFIG_ENCRYPTION_KEY` encrypts service credentials. Rotating it requires decrypting existing rows with the old key and re-encrypting with the new key. Do not change it casually on a live deployment.

