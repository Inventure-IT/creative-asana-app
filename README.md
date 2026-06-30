# Asana Hours Dashboard

## Getting your Asana Personal Access Token (PAT)

The app authenticates with a **Personal Access Token** — a private key tied to *your* Asana
account. It can see exactly what you can see in Asana, so treat it like a password.

1. Sign in to Asana in your browser.
2. Open the developer console: **https://app.asana.com/0/my-apps**
   *(or: click your profile photo, top‑right → **My Settings** → **Apps** tab → **Manage developer apps**).*
3. Under **Personal access tokens**, click **➕ Create new token**.
4. Give it a name you'll recognise (e.g. `Hours Dashboard`) and accept the API terms.
5. **Copy the token immediately** — Asana shows it **only once**. It looks like
   `1/1234567890:abcdef0123456789abcdef0123456789`.
6. Paste it into the app (login screen for the static site, or the `ASANA_PAT` environment
   variable for the local app).

If you lose it, just create a new one and delete the old token from that same page.

**Access note:** the token can only read projects/portfolios your Asana account has access to.
If a project shows no data, confirm you can open it in Asana with the same account.
