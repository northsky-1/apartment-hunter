# Apartment Hunter — GitHub Actions edition

Runs daily on GitHub's servers (no laptop needed), publishes a live HTML report at your GitHub Pages URL, archives every day's report. Free tier covers it.

## Setup (~10 minutes)

You'll need:
- A GitHub account (you have this)
- Your GitHub username
- Terminal on your Mac with `git` (already installed)

### Step 1 — Create an empty repo on GitHub

1. Go to https://github.com/new
2. Repository name: `apartment-hunter` (or anything you like)
3. **Public** (so GitHub Pages works on the free tier)
4. **Don't** initialize with README, .gitignore, or license — leave all checkboxes off
5. Click **Create repository**

GitHub now shows you a page with setup commands. Ignore those and use mine below.

### Step 2 — Push this folder to GitHub

Open Terminal and paste these, replacing `YOUR-USERNAME` with your actual GitHub username:

```bash
cd ~/Downloads/apartment-hunter-github   # or wherever you saved this folder

git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/apartment-hunter.git
git push -u origin main
```

If git prompts for a password, use a **Personal Access Token** (not your GitHub password — GitHub disabled password auth for git). Create one at https://github.com/settings/tokens/new:
- Note: "apartment hunter"
- Expiration: 90 days (or whatever)
- Scopes: tick **repo** (just that)
- Generate, copy the token, paste it as the password

### Step 3 — Enable GitHub Pages

1. On your repo's GitHub page, click **Settings** (top right)
2. Left sidebar → **Pages**
3. Under "Build and deployment":
   - Source: **GitHub Actions**
4. Save

### Step 4 — Run the workflow once manually to bootstrap

1. On your repo's GitHub page, click **Actions** (top tab)
2. If GitHub asks "Workflows aren't being run on this repo" → click **I understand my workflows, go ahead and enable them**
3. Click **Daily apartment hunt** in the left sidebar
4. Click the **Run workflow** dropdown on the right → **Run workflow** (green button)
5. Wait ~1 minute. Refresh. Click into the run to watch logs.

If it succeeds, you'll see a green checkmark and the deploy step will print your live URL. It'll be:

```
https://YOUR-USERNAME.github.io/apartment-hunter/
```

Bookmark that. From now on, every morning at 08:15 Helsinki time, GitHub will run the script, commit the new report, and the URL will show today's apartments.

## Daily flow

- 06:15 UTC (08:15/09:15 Helsinki) — workflow runs
- Pulls new Helsinki+Espoo listings from Oikotie
- Filters to your criteria, scores each one
- Commits the updated report to `docs/index.html`
- Pages auto-deploys, your bookmarked URL refreshes

## Tuning your criteria

Edit `criteria.json` or `market_baseline.json` locally, then:

```bash
git add criteria.json market_baseline.json
git commit -m "Tune criteria"
git push
```

Next run picks up the changes.

## Troubleshooting

**Workflow failed with "Resource not accessible by integration"** — Settings → Actions → General → Workflow permissions → "Read and write permissions" → Save.

**Pages didn't deploy** — Make sure Settings → Pages source is set to "GitHub Actions" not "Deploy from branch".

**Listings stop coming in** — Oikotie may have changed their bootstrap. Check the Actions log for the error. Tell me and I'll patch the fetch logic.

**You want to stop the daily runs** — Actions tab → Daily apartment hunt → "•••" menu → Disable workflow.

**You want to email yourself the report instead of using the URL** — tell me and I'll add an email step using a free service like Resend or your Gmail app password.

## What's in this repo

| File | Purpose |
|---|---|
| `apartment_hunter.py` | The fetcher + scorer + HTML generator |
| `criteria.json` | Your apartment criteria — edit freely |
| `market_baseline.json` | €/m² baseline per district — refine over time |
| `.github/workflows/daily.yml` | The schedule + automation |
| `docs/index.html` | Auto-generated, the live page |
| `docs/history.html` | Auto-generated, list of all past reports |
| `seen_listings.json` | Auto-generated, tracks which listings you've seen |

## Privacy note

The repo is public, so anyone with the URL can read your `criteria.json` (price max, size, rooms). That's not really sensitive but worth knowing. If you want it private, switch the repo visibility in Settings → General → Danger Zone → "Change visibility" — but Pages on private repos requires GitHub Pro ($4/mo) or you'd need to switch to email delivery.
