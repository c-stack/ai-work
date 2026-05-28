# ai-work

This repo runs an NTG radar service that scans GitHub bounty-style issues, triages them, prepares local workspaces, and can hand new targets to Codex automatically.

## How It Runs

The execution chain is:

1. GitHub Actions runs `.github/workflows/ntg-radar.yml` every hour or on manual dispatch.
2. The workflow starts `python bounty_missions/tools/ntg/service.py --config bounty_missions/tools/ntg/service.github.yaml`.
3. `service.py` calls `pipeline.py`, which:
   - scans recent GitHub issues for bounty-like tasks
   - enriches and scores them in `triager.py`
   - writes `current_queue.json`, `mission_queue.md`, and run summaries
4. `service.py` then prepares per-issue workspaces under `bounty_missions/workspaces/`.
5. When a queue item is newly discovered and matches the configured recommendation level, `service.py` runs the automation command.
6. The default automation command is `bounty_missions/tools/ntg/run_codex_fix.sh`, which opens the prepared repo workspace and invokes `codex exec`.

The queue is effectively the "单子监控" layer. Each run compares the new queue against the previous queue and only triggers automation for newly added items when `automation.trigger_mode` is `new_queue_items`.

## GitHub Mode

Files that make GitHub-hosted execution work:

- `.github/workflows/ntg-radar.yml`: scheduled runner
- `bounty_missions/tools/ntg/service.github.yaml`: GitHub-oriented service config
- `bounty_missions/tools/ntg/sync_github_secrets.py`: syncs local credentials into repo secrets

Current publishing status:

- the workflow file is prepared locally, but the PAT from `auth` does not have GitHub `workflow` scope
- because of that, the remote repo can receive the service code and secrets, but cannot accept `.github/workflows/ntg-radar.yml` until a broader PAT is used

Required secrets:

- `NTG_GITHUB_TOKEN`: used for GitHub API access during scanning
- `OPENAI_API_KEY` or `CODEX_API_KEY`: required only if you want GitHub Actions to actually run Codex auto-fix

Optional secrets:

- `NTG_GITHUB_USERNAME`
- `NTG_GITHUB_PASSWORD`

Without an OpenAI/Codex API key, the scan and triage pipeline still runs on GitHub, but `run_codex_fix.sh` will skip automation cleanly.

## Local Files

- `auth.example` shows the expected local auth file shape
- real `auth` must stay local and must not be committed
- workflow output is written under `bounty_missions/tools/ntg/out/github/`

## Useful Commands

```bash
python3 bounty_missions/tools/ntg/sync_github_secrets.py c-stack/ai-work --auth-file auth
cd bounty_missions/tools/ntg
python3 -m unittest \
  test_triager.py \
  test_scanner.py \
  test_pipeline.py \
  test_service.py \
  test_github_auth.py
cd ../../..
python3 bounty_missions/tools/ntg/service.py --config bounty_missions/tools/ntg/service.github.yaml
```
