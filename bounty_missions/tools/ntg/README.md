# NTG Tools

This folder contains small utilities for the bounty hunting workflow around community bug-fix tasks.

## `scanner.py`

`scanner.py` is now a configurable GitHub discovery tool instead of a hardcoded single-repo script.

What it does:

- searches recent GitHub issues for bounty-like keywords
- supports repo-specific label filters
- supports exclude keywords to cut obvious false positives
- ranks findings so newer and less-contested opportunities float to the top
- exports normalized JSON and a Markdown board for later AI processing

Example usage:

```bash
cd bounty_missions/tools/ntg
python3 scanner.py --auth-file ../../../auth --config targets.example.yaml --days 14 --limit 10
python3 scanner.py --repo projectdiscovery/nuclei --json-output out/opportunities.json
python3 scanner.py --config targets.example.yaml --markdown-output out/opportunities.md
python3 triager.py --auth-file ../../../auth --input out/opportunities.json --markdown-output out/triaged.md
python3 pipeline.py --config targets.example.yaml --algora-org-file algora_orgs.curated.txt --repo-seed-file out/repo_seeds.txt
python3 workon.py --repo openmetaearth/me-hub --issue 108
python3 algora_source.py --org-file algora_orgs.example.txt --json-output out/algora_opportunities.json
python3 source_discovery.py --json-output out/repo_seeds.json --repo-list-output out/repo_seeds.txt
python3 dashboard.py
python3 service.py --config service.example.yaml
```

Authentication:

- set `GITHUB_TOKEN` or `GH_TOKEN` to avoid low anonymous rate limits
- preferred on macOS: store the token in Keychain under service `codex-github-token`
- or point `--auth-file` at a local file that contains a token

Output fields:

- `score`: heuristic priority for triage
- `repo`, `number`, `title`, `url`: GitHub issue identity
- `labels`, `assignees`, `comments`, `age_days`: triage context
- `keyword`, `matched_labels`: why the issue was picked up

Config fields:

- `repo`: optional repo scope in `owner/name` format
- `labels`: optional GitHub labels that must match
- `keywords`: positive search terms or phrases
- `exclude_keywords`: drop noisy text matches after search

## `generator.py`

`generator.py` remains a nuclei-template skeleton generator. It is still specific to the Nuclei branch of the workflow.

## `triager.py`

`triager.py` turns scanner output into a ranked work queue by fetching:

- full issue body
- issue comments
- repo primary language and top languages

It then scores each item for:

- bounty confidence
- actionability
- language fit
- competition risk

It also emits:

- `decision_reason`: short natural-language explanation for `pursue` / `review` / `skip`
- `decision_signals`: compact signal tags that explain the recommendation

## `pipeline.py`

`pipeline.py` runs the whole flow in one command:

- scan GitHub issues
- optionally pull direct bounty issues from public Algora org boards
- optionally extend GitHub scanning with a repo seed file from `source_discovery.py`
- triage the results
- emit a compact `mission_queue.md` for the items worth human or agent follow-up
- emit `current_queue.json` for services or downstream automation
- store timestamped snapshots under `out/runs/` for historical comparison

## `workon.py`

`workon.py` prepares a local workspace for one selected issue:

- fetches repo, issue, comments, and languages from GitHub
- clones or updates the target repo
- writes a local summary plus first-pass `rg` search hits

## `algora_source.py`

`algora_source.py` scrapes public Algora org bounty boards and emits normalized issue candidates:

- one input org can yield multiple guaranteed-bounty GitHub issues
- output is compatible with `triager.py` because it includes `repo`, `number`, `title`, `url`, and `score`
- it now tries multiple public page shapes: `/bounties`, `/bounties?status=open`, `/home`, and bare org pages

## `source_discovery.py`

`source_discovery.py` turns public community bounty lists into seed repos:

- fetches the current `awesome-bounties` README
- extracts `owner/repo` candidates with tech and bounty range
- writes both structured JSON and a plain repo list for later targeting

## `dashboard.py`

`dashboard.py` renders a static HTML view of the latest run:

- source counts
- top triaged candidates
- recommendation reasons and signal tags
- run-to-run changes
- prepared workspaces

## `service.py`

`service.py` turns the toolkit into a repeatable service:

- runs the pipeline with a YAML config
- optionally refreshes repo seeds before scanning
- auto-prepares workspaces for `pursue` / `review` items
- can optionally trigger an external fixer command such as `codex` for newly queued items
- renders a static dashboard and writes service state
- suppresses the known macOS Python 3.9 `urllib3` LibreSSL warning during service runs
- generates alert artifacts and can optionally post queue changes to a webhook

## `notifier.py`

`notifier.py` turns queue state into service-friendly alerts:

- compares the latest `current_queue.json` with the previous queue
- writes `out/alerts/latest_alert.json` and `out/alerts/latest_alert.md`
- keeps a timestamped history under `out/alerts/history/`
- can `POST` the alert payload to a configured webhook

## Service operation

Single run:

```bash
cd /Users/mac/Desktop/alauda_project/zonghangwang
bounty_missions/tools/ntg/run_once.sh
```

Continuous watch mode:

```bash
cd /Users/mac/Desktop/alauda_project/zonghangwang
python3 bounty_missions/tools/ntg/service.py --config bounty_missions/tools/ntg/service.example.yaml --watch
```

GitHub Actions mode:

```bash
cd /Users/mac/Desktop/alauda_project/zonghangwang
python3 bounty_missions/tools/ntg/sync_github_secrets.py OWNER/REPO --auth-file auth
git add .github/workflows/ntg-radar.yml bounty_missions/tools/ntg/service.github.yaml
git commit -m "Add NTG GitHub Actions radar"
git push
```

Notes:

- the workflow file is `.github/workflows/ntg-radar.yml`
- the GitHub-oriented service config is `bounty_missions/tools/ntg/service.github.yaml`
- `sync_github_secrets.py` pushes `NTG_GITHUB_TOKEN` and, when present, `NTG_GITHUB_USERNAME` / `NTG_GITHUB_PASSWORD`
- Codex auto-fix on GitHub is wired through `bounty_missions/tools/ntg/run_codex_fix.sh`
- on GitHub-hosted runners, if `codex` is missing from `PATH`, the wrapper exits cleanly and the scan still succeeds

macOS `launchd` install:

```bash
cd /Users/mac/Desktop/alauda_project/zonghangwang
chmod +x bounty_missions/tools/ntg/run_once.sh bounty_missions/tools/ntg/install_launchd.sh
bounty_missions/tools/ntg/install_launchd.sh
launchctl list | rg com.ntg.radar
```

Operational notes:

- default logs go to `bounty_missions/tools/ntg/out/logs/`
- default launch agent path is `~/Library/LaunchAgents/com.ntg.radar.plist`
- override config path with `NTG_SERVICE_CONFIG=/abs/path/to/service.yaml`
- the template file is `bounty_missions/tools/ntg/launchd/com.ntg.radar.plist.template`
- machine-readable queue is written to `bounty_missions/tools/ntg/out/current_queue.json`
- alert artifacts are written to `bounty_missions/tools/ntg/out/alerts/`

Triage config:

- `triage.profile`: `strict`, `balanced`, or `aggressive`
- the `triage.*` numeric fields can override the chosen profile when you need custom thresholds

Notification config:

- `notifications.send_when`: `changes_only`, `nonempty_queue`, or `always`
- `notifications.webhook_url`: optional destination for JSON alert posts
- `notifications.headers`: optional extra HTTP headers for the webhook request

Automation config:

- `automation.command`: optional argv-style command template; empty means disabled
- placeholders available in command/env values include `{repo}`, `{number}`, `{title}`, `{url}`, `{workspace_dir}`, `{recommendation}`, `{repo_root}`, `{output_dir}`
- `automation.trigger_on`: which recommendations should trigger automation, e.g. `pursue`
- `automation.trigger_mode`: `new_queue_items` or `all_queue_items`
- `automation.max_items`: max queued items to trigger per service run
- `automation.timeout_seconds`: timeout per spawned fixer command
- `automation.extra_env`: optional environment variables passed to the fixer command

## Token hygiene

- current code prefers `GITHUB_TOKEN` / `GH_TOKEN`, then macOS Keychain service `codex-github-token`, then local auth files
- the checked-in `auth` file should stay a placeholder only
- because the old token was previously exposed, GitHub-side token rotation is still required
