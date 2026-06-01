# Kalshi credentials — team setup

**Rule #1: secrets never go in git.** No `.pem`, no key IDs, no `.env`. The repo
only contains config *templates* and code that reads your local credentials.

We use **per-developer API keys on a shared account**: same Kalshi account, but
you and your partner each create your *own* API key. Nobody sends a private key
to anyone. If a laptop is lost, revoke just that one key.

## One-time setup (each teammate does this)

1. **Create your own API key** in the Kalshi portal:
   - Demo: https://demo.kalshi.co → Settings → API Keys → *Create*
   - Prod: https://kalshi.com → Settings → API Keys → *Create*
   - Download the private key it gives you. You only see it once.
2. **Drop the key files into `secrets/`** (gitignored — never committed):
   ```
   secrets/kalshi-demo-key.pem    # your demo private key
   secrets/kalshi-key.pem         # your prod private key
   chmod 600 secrets/*.pem
   ```
3. **Create your `.env`** from the template and paste in *your* key IDs:
   ```
   cp .env.example .env
   # edit .env: set KALSHI_DEMO_KEY_ID / KALSHI_PROD_KEY_ID to your own UUIDs
   ```
4. **Test it:**
   ```
   .venv/bin/python kalshi_auth.py          # uses KALSHI_ENV (default: demo)
   KALSHI_ENV=prod .venv/bin/python kalshi_auth.py
   ```
   A working setup prints `✅ Auth OK` and your balance.

## If you *must* share one key instead of per-person

(e.g. the account is limited to one API key.) Do **not** paste it in Slack/email.
Use one of these, then the recipient saves it to `secrets/`:

- **Team password manager / vault** (1Password, Bitwarden, Vault) — share to their account.
- **One-time-secret link** (onetimesecret.com, Bitwarden Send, 1Password share) —
  self-destructs after one view; safe to drop the *link* in Slack.
- **Encrypted file:** `age -r <their-pubkey> -o key.pem.age secrets/kalshi-key.pem`
  then send the ciphertext anywhere.

## What got cleaned up

- The old `documentation/secrets` (a key id + private key) was **committed and pushed**.
  It's now untracked + gitignored, but the leaked key is in git history — **rotate it**
  in the Kalshi portal (delete the old key, create a new one). Once rotated, the
  history copy is worthless. Optional: purge history with `git filter-repo` + force-push.
