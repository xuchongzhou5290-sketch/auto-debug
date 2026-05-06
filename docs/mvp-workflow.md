# MVP Workflow And CLI Entry

## Goal

The first version should make one thing boring and reliable:

- observe one device
- create a session
- collect baseline evidence
- produce a structured summary

## MVP Flow

```mermaid
flowchart TD
    A["Load profiles"] --> B["Create session"]
    B --> C["Start serial observer"]
    C --> D["Track device state"]
    D --> E["Establish control path"]
    E --> F["Run startup checks"]
    F --> G["Collect evidence"]
    G --> H["Write summary"]
    D --> I{"Needs human help?"}
    I -->|Yes| J["Pause at manual state"]
    J --> D
```

## Current Multi-Iteration Loop

The current loop contract is session-driven. Each iteration writes a new `summary.json`,
then the upper-layer AI decides whether to stop, intervene, or continue based on
`summary.result`.

```mermaid
flowchart TD
    A["Iteration N: upper-layer AI sends action request"] --> B["Include loop(goal, prev_session, iteration)"]
    B --> C["Create fresh session"]
    C --> D["Bootstrap summary.json\nwrite loop\nresult.decision = pending"]
    D --> E["Execute action\nrun / health / observe / exec / fetch-* / deploy-*"]
    E --> F["Write summary.result\n decision\n failure_stage\n next_actions\n carry_forward_request"]

    F --> G{"decision"}
    G -->|success| H["Stop loop"]
    G -->|continue| I["Prepare next iteration"]
    G -->|blocked| J["Wait for prerequisite fix"]
    G -->|manual_required| K["Reserved state\nnot emitted yet"]
    G -->|stalled| L["Reserved state\nnot emitted yet"]
    G -->|pending| M["Abnormal state\niteration did not close cleanly"]

    I --> N["Read next_actions"]
    N --> O["Patch code / config / field conditions"]
    O --> P["Optional: record-intervention"]
    P --> Q["Reuse carry_forward_request"]
    Q --> A

    J --> R["Restore password / serial / network / media prerequisites"]
    R --> P

    K --> P
    L --> P
```

Current stop / pause semantics:

- `success`: stop the multi-iteration loop
- `continue`: soft break; upper-layer AI should inspect `failure_stage`, apply changes, then use `carry_forward_request`
- `blocked`: hard break; fix prerequisites first, then continue
- `manual_required` / `stalled`: reserved by the contract but not emitted by the current code yet
- `pending`: bootstrap default only; if it remains at the end, treat the iteration as abnormal

Common `failure_stage` breakpoints in the current implementation:

- `observe_serial`
- `establish_control`
- `baseline_checks`
- `collect_evidence`
- `validation`
- `prepare_transport`
- `run_bootstrap_commands`
- `validate_connectivity`
- `transfer_artifacts`
- `validate_transfer`
- `execute_command`
- `fetch_file`
- `fetch_path`
- `deploy_artifacts`
- `running_checks`

## First CLI Entry

### `run`

Primary command:

```powershell
cd <repo-root>
$env:AUTO_DBG_SERIAL_PORT = "COM19"
python -m autodbg ports
python -m autodbg run
```

What it does in the scaffold:

1. loads the default profile set from `profiles/defaults.toml`
2. creates a session directory under `artifacts\YYYYMMDD\session_id`
3. writes bootstrap evidence files
4. prints the planned startup-check workflow

### `resume`

```powershell
python -m autodbg resume --session-dir .\artifacts\20260415\...
```

Use it after a manual intervention or when the host tool is restarted.

### `summary`

```powershell
python -m autodbg summary --session-dir .\artifacts\20260415\...
```

Reads `summary.json` and prints the current session snapshot.

### `observe`

```powershell
python -m autodbg observe --seconds 10
```

Captures live serial data into a fresh session and writes:

- `logs/serial.log`
- `events.jsonl`
- `summary.json`

### `exec`

```powershell
python -m autodbg exec --shell-command "ls /mnt/sdcard"
```

Attempts to:

1. reach the login prompt or root shell
2. login if needed
3. run one command with begin/end markers
4. write transcript and output artifacts into the session

## Immediate Next Implementation Targets

1. replace stub serial observer with real serial capture
2. replace stub controller with shell and agent control
3. wire task templates into real step execution
4. add deployer support for the first stable channel
