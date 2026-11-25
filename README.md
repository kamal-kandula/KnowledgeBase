# KnowledgeBase
# Credit Union Knowledge System

A Python solution for the "knowledge system that knows what's true now" assignment. It reads five snapshots of a credit union website, answers questions with the current value, cites a page, and says it doesn't know when the site is silent.

It uses only the Python standard library. There are no API keys and no extra packages, and it runs offline on the provided snapshots.

## Running it

From the top of the participant kit, with this folder inside it:

    python harness/run_eval.py --submission <this folder> --eval eval/public_eval.jsonl

`solution.py` finds `harness/contract.py` by looking through its parent folders, so keep the folder inside the kit. The snapshots are not included in this repo.

## How it works

**Version history.** Every time a page's text changes between snapshots, I store the new version next to the old one. Current questions use each page's newest version. "As of" questions use the latest version on or before that date, so the 10-month jumbo special comes out as 3.90% for May and not the 4.00% it shows now.

**Chunks and search.** Pages are cut into small pieces. A table row keeps its column headers and the heading above it, so "60 Months | 5.39% APR" still knows it belongs to the 2024-27 model year. Paragraphs and bullets are split up the same way. Search is ordinary BM25 keyword scoring.

**Deciding what's current.** Keyword scoring alone gets fooled by old pages, so I adjust the scores:

- Official rate and product pages count for more than calculators, promo pages, blog posts and press releases. That's why the 60-month auto loan comes out as 5.39% and not the 5.34% still on an old promo page.
- Newer content gets a small bump, but only in proportion to how well it already matched.
- Details in the question matter: loan term, model year, balance tier, jumbo versus standard, APR versus APY, personal versus business. Rows for the wrong tier are pushed down, so different tiers are treated as different facts and not as contradictions.
- If the question asks for a number, chunks that contain one are preferred over label rows.

The answer is built from the one best page and kept short, so stale numbers from other pages don't get in.

**Facts that live in plain text.** Some answers aren't in tables: branch count, who can join, the join link, mobile wallets, Forbes recognition, the Home$tart amount, credit card APRs and fixed mortgage rates. For these I wrote small pattern-based lookups that search the pages. They don't hard-code the answers. They were written after reading this credit union's pages, so they may not carry over to different wording.

**Saying "I don't know".** If nothing matches well, or the site doesn't publish the thing (a 7/1 ARM rate, a construction loan rate, hours for one specific branch), the answer says it isn't listed and suggests contacting the credit union. It gives no number in that case.

**Conflicts.** `conflicts()` works from a short hand-picked list of topics. For each, it gets the official current value, then looks for different values of the same thing on other pages. It reports both and picks the official one. The output is noisy in places, and a few picks are wrong. This is the weakest part.

## Known weak spots

- The branch count is messy on the site itself. The About page says 18, and press releases are split between 22 and 23. My answer says 23, and the practice set expects 22.
- Lobby hours that differ by branch only get partial credit.
- One practice question has an empty expected-answer list, so it's only gradable with the optional LLM judge.
- The `conflicts()` topic list is hand-picked, not discovered.

## Results

On the 40 practice questions it scored about 0.91 accuracy. Answers come back in well under a second, and asking the same question twice gives the same answer. Loading all five snapshots takes about 5 seconds.
