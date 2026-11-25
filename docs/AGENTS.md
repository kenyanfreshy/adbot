# AdBot agent team

AdBot is growing from one decision engine into a team of small agents, each doing one job, coordinated by
`agents/brain.py` ("the Brain"). The agents run at the end of every scheduled job (`adbot-hourly`, every 4 hours),
read the same MotherDuck data as the phone app, and hand their results to each other. Every run and every agent's
output is kept in `control.agent_runs` / `control.agent_outputs`.

Nothing AdBot already does was removed or changed: imports, the decision engine, proposals and approvals,
DRY_RUN, safeguards, the emergency pause, alerts, the Revenue and Trading tabs all work as before.

## Rules every agent follows

* **Read-only toward the ad platforms.** No agent creates, pauses or changes campaigns, bids or budgets. Changes keep
  going through the existing decision engine, its safeguards and your sign-off. (A test checks the agent modules
  never reference the platform adapters.)
* **Numbers come from code.** Claude only writes short summaries, from facts the agents computed. A summary that
  mentions a number that isn't in the facts is thrown away.
* **Capped AI spend.** Claude API calls are limited per UTC day (`AGENTS_DAILY_USD_CAP`, default $1.00) and logged in
  `control.llm_usage` with token counts and cost. Without `ANTHROPIC_API_KEY` everything still runs, just without
  AI-written summaries.
* **Labeled measurement.** "Real return" is blended (all store proceeds, organic included, ÷ ad spend): AdBot has no
  install attribution feed, so it never claims what a single ad earned.

## Phase 1 (built): the Brain and the morning page

| Agent | Module | What it does |
| --- | --- | --- |
| Department Check | Brain | Every ad account and store: up to date, stale, failed; unmapped campaigns; Play reports kept unparsed |
| Spend Watch | Launch & Optimize | Yesterday's spend per channel, month to date per app/channel, against the monthly budget |
| Real Return | Profit Tracking | Per app, last 7 and 28 days: store proceeds, downloads, paid units, ad spend, blended return |
| Winner Spotter | Profit Tracking | Cheapest installs in the engine's decision window, and items spending with nothing to show |
| Change Review | Launch & Optimize | Changes the engine proposed that need your OK, blocked or unconfirmed changes, mode |
| Morning Page | Brain | One Telegram message a day (first run after 8:00 Los Angeles time) from the facts above |

Settings (on `adbot-hourly`): `ANTHROPIC_API_KEY`, `AGENTS_DAILY_USD_CAP`, `AGENTS_MODEL` (default
`claude-sonnet-5-5`), `AGENTS_TZ`, `AGENTS_MORNING_HOUR`. The morning page can be switched off on the Rules tab.

Try it without sending anything: `python -m agents.brain --preview` (prints the page; Claude calls still count
toward the daily cap).

## Phases 2–4 (built): research, studio, launch drafts, creative briefs

Everything these agents write lands in the **Studio** tab as a draft. You approve or reject each item; approval
only lets the next agent build on it. **Nothing in the studio is sent to an ad platform or a store.**

| Agent | Module | Cadence | What it does |
| --- | --- | --- | --- |
| Review Miner | Research | daily | Your App Store and Google Play reviews → `control.reviews` |
| Search Ad Watch | Research | daily | Apple Ads search terms (last 14 days) → terms to add as keywords, terms to exclude |
| Competitor Ad Spy | Research | weekly, optional | Active ads for `AGENTS_COMPETITOR_TERMS` from the Meta Ad Library |
| Customer Voice | Research | weekly | Claude: likes, complaints, words people use; every point must cite a real review or search term |
| Angle Writer | Research | weekly | Claude: 3 angles per app, each backed by evidence ids that exist |
| Ad Copy | Creative Studio | per approved angle | Meta, Google App campaign and Apple Ads keyword drafts; over-length lines dropped; numbers and claims flagged |
| Listing Writer | Landing | per approved angle | App Store promotional text (≤170) and Google Play short description (≤80) |
| Test Budget | Launch & Optimize | every run | App daily allowance × exploration share, capped by the per-campaign maximum |
| Launch Drafter | Launch & Optimize | per approved copy | Apple Ads keywords/negatives for the app's Search Results campaign, Meta and Google App campaign drafts. Drafts only |
| Resizer | Creative Studio | every run | Sizes each placement needs |
| Creative Briefs | Creative Studio | per approved copy | Claude: 3 image briefs, a 15-second video script and a voiceover script |

Flow: reviews + search terms → customer voice → **angles** (you approve) → **copy** and listing text (you approve
the copy) → **launch draft** and **creative brief**. Winners and spend from phase 1 feed the morning page, which
now also lists research highlights and what's waiting in the Studio.

What still needs you (or a later change):
* **Access.** Reading App Store reviews may need an App Store Connect key whose role can read customer reviews
  (the revenue key may be Finance-only); Google Play reviews may need the "reply to reviews" permission for the
  service account. If access is refused, Review Miner says so in the Studio tab; nothing else is affected.
* **Competitor ads** need `AGENTS_COMPETITOR_TERMS` and a Meta token with Ad Library API access. The Ad Library
  only covers some countries for non-political ads (`AGENTS_COMPETITOR_COUNTRIES`, default GB).
* **Generating images, video and voice** needs an outside tool. `CREATIVE_PROVIDER` is reserved for it; AdBot has no
  integration yet, so briefs are what you hand to a designer or tool.
* **Launching** from AdBot is off. Create approved drafts in each platform's own tool.

## How the reference "32-agent" design maps onto AdBot

| Reference module | Reference agents | AdBot today |
| --- | --- | --- |
| Brain | checks departments, morning page, handoffs | **Phase 1** (Department Check, Morning Page, brain.py) |
| Profit Tracking | Sales Matcher, Customer Cost, Lead to Sale, Budget Proposal, Real Return | Real Return, Winner Spotter (phase 1); budget proposals already in the decision engine. Customer cost per paying user needs an attribution feed (see MEASUREMENT.md); Lead to Sale needs a CRM, which AdBot doesn't use |
| Launch & Optimize | You Sign Off, launchers, Test Budget, Waste Pauser | Sign-off, waste pausing and budget pacing already exist in the engine; Spend Watch, Change Review, Test Budget, Launch Drafter (drafts only) |
| Research | Competitor Ad Spy, Review Miner, Customer Voice, Search Ad Watch, Angle Writer | All five (phase 2) |
| Creative Studio | Image Ads, Video Ads, Voiceover, Ad Copy, Resizer | Ad Copy, Resizer, Creative Briefs (image, video, voiceover scripts); generating the media needs an outside tool |
| Landing | landing pages | Listing Writer (your store listing is the landing page for an app) |

## Not built (needs a decision or an outside account)

* Launching drafts on the platforms (would need write access, DRY_RUN and the existing safeguards).
* Image / video / voice generation (pick a tool; then add a provider for `CREATIVE_PROVIDER`).
* Customer cost per paying user and lead-to-sale (need install attribution; AdBot has no CRM).
