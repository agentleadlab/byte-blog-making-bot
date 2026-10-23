# Moving RYTE from the Mac to Railway

Nothing here changes the Mac until step 7. Until then the Mac keeps running
RYTE exactly as it does now, and the Railway copy stays in **standby**: it
checks its connections and waits, and never connects to Discord.

**One RYTE at a time.** Two copies answer everything twice. The Mac is only
stopped in step 7, and the Railway copy only goes live in step 8.

---

## Before you start

- [ ] Railway's status page is green (status.railway.com) - no "builds paused".
- [ ] A **private** GitHub repo on your own account (e.g. `maym84244-dev/ryte`),
      empty. Tell Claude its name; the code goes there. agentleadlab is never
      connected to Railway.
- [ ] Your `.env` from the Mac, open, to copy values from. **Never paste it in
      chat.**

## 1. The service (Gabi's project is left alone)

- [ ] Railway → **New** → **Deploy from GitHub repo** → the private `ryte` repo.
      A **new project** - not inside mindful-vitality.
- [ ] It builds from the repo's `Dockerfile` by itself. Let the first build
      finish (green). It will fail to *run* until step 3 - that's expected.

## 2. The volume - RYTE's memory lives here

- [ ] Service → **Volumes** → **New volume**, mount path **`/data`**.
      Without it RYTE forgets everything on every deploy.

## 3. Standby first

- [ ] Service → **Variables** → add **`RYTE_STANDBY` = `1`**. Add this before
      anything else.

## 4. The keys

- [ ] Variables → **Raw editor** → paste every line of the Mac's `.env`.
      Same names, same values.
- [ ] Add **`TZ` = `America/New_York`** (the build sets it too - belt and braces).
- [ ] Later, if you buy the Webshare Rotating Residential 1 GB plan:
      `WEBSHARE_PROXY_USERNAME`, `WEBSHARE_PROXY_PASSWORD`.

## 5. Check it - still in standby

- [ ] Service → **Deployments** → latest → **View logs**. You should see:
      `RYTE_STANDBY is on - checking connections, then waiting.` and a list:
      `✅ Trello …`, `✅ Payra …`, `✅ Claude …`, `✅ Distro Hub …` and so on.
- [ ] **Every line ✅ or ➖** (➖ = not set up, fine). Any **❌** - send Claude
      a screenshot of that line. Fix the variable; Railway redeploys and checks
      again by itself.
- [ ] `✅ Memory … /data is a volume`. If it says it isn't a volume, step 2.
- [ ] `✅ Clock …` shows Eastern time.

## 6. Pack the Mac's memory (Mac still running)

- [ ] On the Mac, in the RYTE folder: `bash scripts/pack-state.sh`
      → `ryte-state-<date>.tar.gz` on the Desktop. It carries the ledger, payments,
      orders, Faith's texts, the keep list - and posts still to publish.

## 7. The switch - pick a quiet time (after 10 PM)

- [ ] **Stop RYTE on the Mac**: close its terminal window (or Ctrl-C).
- [ ] Railway → Variables → **delete `RYTE_STANDBY`**. Railway redeploys, and
      RYTE comes online in Discord from Railway.

## 8. Bring the memory across

- [ ] In Ryte The Goat: **`@Ryte restore state`** with `ryte-state-<date>.tar.gz`
      attached → **Restore it**. RYTE deletes the message (client data) and
      restarts.
- [ ] If the file is too big for Discord: tell Claude - there's another way in.

## 9. Live checks

- [ ] `@Ryte health` - every line ✅ or ➖, Discord connected.
- [ ] `@Ryte payra status` - the Payra history came across.
- [ ] `@Ryte order audit` - today's payments show their cards.
- [ ] `@Ryte going live today and tomorrow` - the board reads right.
- [ ] 📌 a test message in the Growth server → it lands on the day's card.
- [ ] RYTE's start-up message shows Railway's version and Eastern time.

## 10. The first two days

Each of these says itself in #trello-manager when it runs:

- [ ] 8:30 PM and 10 PM carry-overs, the archive.
- [ ] 6 AM setup card, 9 AM move.
- [ ] An aged payment or a Spark post making its own card.
- [ ] Optional: point `HEALTHCHECK_URL` at a free healthchecks.io check, so
      your phone hears if RYTE goes quiet for 10 minutes.

## If anything's wrong - back to the Mac in a minute

1. Railway → Variables → add **`RYTE_STANDBY` = `1`** (Railway's RYTE goes quiet).
2. Start RYTE on the Mac.

Nothing is lost - whichever copy comes back reads back what it missed.
