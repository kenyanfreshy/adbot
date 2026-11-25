"""
AdBot's agent team ("the Brain").

Each agent is a small, single-purpose step that reads AdBot's own data (the same MotherDuck tables the phone app
reads), works out one thing, and hands its result to the agents after it. The Brain (agents/brain.py) runs them in
order once per scheduled job, records every result in control.agent_outputs, and never lets one agent's failure
stop the others or the ad bot.

Phase 1 (this package):
  Department Check   checks every data source: ad accounts, App Store, Google Play, unmapped campaigns, trading
  Spend Watch        what you spent: yesterday and month to date, per channel and app, against the monthly budget
  Real Return        what sold vs what you spent, per app (blended: all store proceeds, organic included)
  Winner Spotter     keywords / ad sets with the cheapest installs, and the ones spending with nothing to show
  Change Review      changes AdBot has proposed and that need your OK (from the existing decision engine)
  Morning Page       one page every morning on Telegram, written from the agents' facts

Hard limits, enforced by construction:
  * no agent writes to an ad platform, and none changes spend, bids, budgets or campaigns: they read, and write only
    to their own control.agent_* / morning_pages / llm_usage tables;
  * numbers on the morning page come from code, never from the language model. Claude only writes the short summary
    at the top, from the facts, and a summary that mentions a number not in the facts is thrown away;
  * Claude API spend is capped per day (AGENTS_DAILY_USD_CAP) and every call is logged with its cost.
"""
