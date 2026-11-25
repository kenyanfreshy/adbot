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

## Phase 1 (built)

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

## How the reference "32-agent" design maps onto AdBot

| Reference module | Reference agents | AdBot today |
| --- | --- | --- |
| Brain | checks departments, morning page, handoffs | **Phase 1** (Department Check, Morning Page, brain.py) |
| Profit Tracking | Sales Matcher, Customer Cost, Lead to Sale, Budget Proposal, Real Return | Real Return, Winner Spotter (phase 1); budget proposals already in the decision engine. Customer cost per paying user needs an attribution feed (see MEASUREMENT.md); Lead to Sale needs a CRM, which AdBot doesn't use |
| Launch & Optimize | You Sign Off, launchers, Test Budget, Waste Pauser | Sign-off, waste pausing and budget pacing already exist in the engine; Spend Watch and Change Review (phase 1). Launchers: phase 3, drafts only |
| Research | Competitor Ad Spy, Review Miner, Customer Voice, Search Ad Watch, Angle Writer | Phase 2 |
| Creative Studio | Image Ads, Video Ads, Voiceover, Ad Copy, Resizer | Ad Copy in phase 2; image/video/voiceover in phase 4 (needs paid outside tools) |
| Landing | landing pages | Later (App Store / Play listing review instead) |

## Next phases (proposed)

* **Phase 2 · Research + ad copy.** Review Miner (your own App Store / Google Play reviews), Search Ad Watch (Apple
  Ads search terms you already pay for), Angle Writer and Ad Copy (Claude), and Competitor Ad Spy where an official
  source allows it (Meta Ad Library API). Output: angles and copy drafts on a new tab, for your sign-off.
* **Phase 3 · Launch drafts.** Complete campaign / ad drafts per platform built from approved angles, stored in
  AdBot. Nothing is created on a platform until you approve it and a later change turns that on.
* **Phase 4 · Creative studio.** Image / video / voiceover generation and resizing through outside tools you choose.
