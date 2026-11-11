# Done shouldn't mean still billing.

![Lights Out banner](docs/banner.png)

```
──────────────────── LIGHTS OUT · session receipt ────────────────────
STILL RUNNING                                                  $/month
  i-063c0ce119c3f2f9d         ec2 t3.micro                10:58   7.59
  i-771cd5f963a2c4054         ec2 t3.micro                10:58   7.59
  my-alb                      elb application             10:58  16.43
UNKNOWN (couldn't read an ID: check by hand)                   0 items
──────────────────────────────────────────────────────────────────────
Billing now: $31.61/month             →  /lights out  to turn them off
prices: AWS us-east-1 on-demand, read 2026-11-10
```

That receipt is from the measured run below (task t04: "put a load balancer in front of two instances"). Hermes had answered "Done"; the receipt printed under its reply.

Lights Out is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for anyone who lets Hermes touch an AWS account. Every billable resource Hermes tries to create through the AWS CLI goes to Hermes's own approval prompt first, with its price. A ledger records what was created and the command that deletes it. After the turn, a receipt shows what is still running and what it costs per month, so a "Done ✅" can't hide a bill.

```
hermes plugins install lights-out          # once the catalog entry is merged
hermes plugins install JannetEkka/lights-out
```

Requires Hermes 0.23.1 or newer. No API keys, no settings needed. Optional: set your time zone so keep dates end at 23:59 your time (`plugins.entries.lights-out.settings.timezone: Asia/Kolkata`).

## What happens

1. Hermes decides to run `aws ec2 run-instances --instance-type t3.large ...`.
2. Before it runs, Lights Out's `pre_tool_call` hook matches the command against a fixed rule pack and a dated price table, and asks Hermes's approval gate:

   ```
   Lights Out: this starts 1 EC2 t3.large: about $60.74/month, billed until
   deleted. To keep it past this session: /lights keep <id> <date>.
   ❯ 1. Allow once   2. Allow for this session   3. Add to permanent allowlist   4. Deny
   ```

   On deny, on a timeout, or when nobody is there to answer (cron, a messaging bot with no reply, a non-interactive `-q` run), Hermes blocks the command. Nothing is created. "Always allow" is per service, so allowing EC2 never allows RDS.
3. After it runs, `post_tool_call` reads the new IDs from the CLI's own output and writes a ledger line with the delete command. If the output has no ID (for example `--query ... --output text` that prints only an IP), the plugin lists that resource type read-only and pairs it only when the counts match exactly. Otherwise the create stays **unknown**, and an unknown blocks the green "All clear".
4. After any turn that changed the ledger, the receipt is posted under Hermes's reply. It is also returned at session end (`/new`, a one-shot run, a gateway reset).
5. `/lights out` turns off everything still running that isn't kept. It lists what it will delete and asks once; each delete then runs through Hermes's normal terminal tool and its guards.
6. At the next session start, leftovers from earlier sessions and keeps whose date has passed are raised before anything else.

## Keep it on purpose, until a date

Some things should stay up: a hackathon demo during two weeks of judging. `/lights keep i-9f8e7d 2026-12-15 judging` marks it kept until 23:59 on that day. The receipt then lists it under "kept on purpose" with what it will have cost by then, instead of flagging it. When the date passes it turns red again.

Only a person sets a keep. If you tell Hermes "keep the demo up until judging ends", it can call `lights_keep`, but that call is a proposal: Lights Out sends it to the approval prompt with the cost by that date, so the model can't hide a resource by marking it kept.

From the measured run (task t09, where the user said the demo must stay up until judging ends): Hermes proposed the keep, the approval prompt showed the cost, and the receipt said:

```
KEPT ON PURPOSE (until 15 Dec 23:59 IST)
  i-ba1100e2f8f795add         ec2 t3.small       hackathon ju ~17.74 by then
──────────────────────────────────────────────────────────────────────
All clear · $0/month unplanned · $15.18/month kept
```

Dates: `2026-12-15`, `eod 2026-12-15`, `10d`, `2w`, `2mo`. Past dates are rejected.

## Results: same model, same tasks, with and without

Hermes 0.23.1, model `nvidia/nemotron-3-ultra-550b-a55b` on build.nvidia.com (chosen 2026-11-10), the 10 tasks in [bench/tasks.yaml](bench/tasks.yaml), a fresh moto for every run. Two tasks say the resource must stay up until 2026-12-15, so leaving those running is correct. Ground truth is moto's own inventory after each task, priced at the dated table, never the plugin's ledger. In arm C the harness answered every approval prompt yes, as a user who allows the work would, and logged each one. Raw screen logs, transcripts, approvals and ledgers: [bench/runs/](bench/runs/).

| | A: Hermes alone | B: Hermes + clean-up prompt | C: Hermes + Lights Out |
|---|---|---|---|
| Resources left running unintentionally (10 tasks) | 14 | 1 | 14 |
| … their cost, $/month at the dated price table | $262.22 | $3.65 | $262.22 |
| Billable creates run | 13 | 14 | 13 |
| Billable creates no person approved | 13 | 14 | 0 |
| Leftovers named by ID (reply for A/B, receipt for C) | 100% | 100% | 100% |
| Wrong "all clean" claims | 0 | 0 | 0 |
| "Stay up until 2026-12-15" resources still up | 2 of 2 | 2 of 2 | 2 of 2 |
| After `/lights out`: still running, not kept | – | – | 0 |
| Keeps recorded for the two "stay up until 2026-12-15" tasks | – | – | 2 of 2 |

What this shows:

- **The clean-up prompt works with this model.** With it, Hermes left 1 resource running (an Elastic IP, $3.65/month) where Hermes alone left 14 ($262.22/month). Lights Out deletes nothing on its own, so before `/lights out` arm C left the same 14 as Hermes alone.
- **What Lights Out changed is who decides.** 0 billable creates ran without a person's yes, against 13 and 14 without it, and every prompt showed the price before the command ran. Then `/lights out` turned off all 14 leftovers in one command and left the two stay-up resources running, recorded as kept until 15 Dec 23:59 IST.
- **The model reported honestly here.** In all three arms it named every leftover by ID and never claimed "all clean" while something was running. So in this run the receipt didn't tell the user anything the reply hadn't. The receipt doesn't depend on the model, but this run had no case where that made a difference.
- **The first arm-C pass found two bugs**, kept in [bench/runs-first-C/](bench/runs-first-C/) and [bench/results-first-run.md](bench/results-first-run.md): a multi-line `aws rds create-db-instance \` command lost its flags, so the price showed "unknown"; and a database created with deletion protection couldn't be turned off, so 1 resource was still running after `/lights out` (the receipt kept it in red and said FAIL). Both are fixed and tested, and arm C was re-run on the fixed code. Arms A and B were re-scored with the fixed matcher from their saved transcripts; their numbers didn't change.

One run per task per arm. With a different model, or prompts that make Hermes use Python instead of the CLI, the numbers will differ.

Changed after the measured run, and not re-measured: the `lights_keep` approval now shows the cost by the keep date (it showed "price ?" because the date wasn't set yet), and the receipt is coloured on terminal platforms by default. Neither changes what is asked, recorded or deleted.

## Demo

[demo/lights-out-demo.mp4](demo/lights-out-demo.mp4) (74 s, with narration) is one real session: Hermes 0.23.1, the same model, a fresh moto. The terminal is replayed from the recording ([demo/demo.cast](demo/demo.cast), asciicast v2) by [demo/render.py](demo/render.py), which zooms in on the receipt and the approval prompts; waiting for the model is compressed and the screen says "sped up ×N" where it is. The voice is AI-generated, locally, with the open-weights [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) model (script in [demo/narration.json](demo/narration.json), made by [demo/voice.py](demo/voice.py)). The results card is drawn from [bench/results.json](bench/results.json). Earlier takes are kept as `demo/take1.cast` (the model launched t3.micro instead of the t3.large asked for) and `demo/take2.cast` (the keep prompt showed "price ?", fixed since).

## Commands and tools

| | |
|---|---|
| `/lights` | the receipt |
| `/lights out [yes]` | turn off everything still running that isn't kept (asks first; typing `yes` is your answer) |
| `/lights keep <id> <when> [why]` | keep one on purpose until a date |
| `/lights unkeep <id>` | remove a keep |
| `/lights check` | compare the ledger with the cloud using read-only `describe` calls; marks a resource gone only on proof (a not-found error or a terminated/deleted state) |
| `/lights status` | the ledger as JSON |
| tool `lights_status` | read-only ledger for Hermes, so it can check before saying "nothing is left running" |
| tool `lights_keep` | Hermes proposes a keep; you approve it or not |

## What's covered

AWS CLI (v1 and v2), any command line Hermes runs through its `terminal` tool, including `&&`, `;`, `|` chains and `bash -c '...'`.

| Resource | Create | Turn off | Price |
|---|---|---|---|
| EC2 instances | `ec2 run-instances` | `ec2 terminate-instances` | per instance type, × `--count` |
| EBS volumes | `ec2 create-volume` | `ec2 delete-volume` | GiB × $/GiB-month by volume type |
| Elastic IPs | `ec2 allocate-address` | `ec2 release-address` | public IPv4 hourly |
| NAT gateways | `ec2 create-nat-gateway` | `ec2 delete-nat-gateway` | hourly, + $0.045/GB processed (not counted) |
| Load balancers (ELBv2) | `elbv2 create-load-balancer` | `elbv2 delete-load-balancer` | hourly, + LCU usage (not counted) |
| RDS instances | `rds create-db-instance` | `rds delete-db-instance --skip-final-snapshot` | Single-AZ class + gp2/gp3 storage |
| S3 buckets | `s3api create-bucket`, `s3 mb` | `s3 rb --force` | $0 + storage (says so, never guessed) |

Prices are AWS us-east-1 on-demand, read 2026-11-10 from AWS's public price list files (sources and dates are in [prices/aws-us-east-1.yaml](prices/aws-us-east-1.yaml)). A type or region not in the table shows "price unknown". It is never guessed.

## Approvals, YOLO and unattended runs

Lights Out never answers an approval and never routes around one. It returns `{"action": "approve"}` from `pre_tool_call`, and Hermes's own gate (`tools/approval.py`, `request_tool_approval`) decides:

- Interactive CLI or gateway: the normal prompt.
- `--yolo` or `approvals.mode: off`: Hermes approves at once without asking. That is your setting and Lights Out doesn't override it. The ledger and the receipt still work.
- Cron (`approvals.cron_mode`, default deny), non-TTY one-shot runs (`approvals.single_query_mode`, default deny) and any other context with no one to ask: blocked.
- If the plugin's own hook raises or times out, Hermes blocks the tool (fails closed). Lights Out also catches its own matcher errors and asks anyway for any command containing `aws`.

## Disclosure

- **Shell commands:** `/lights check` runs read-only `aws ... describe-*` / `list-*` calls through Hermes's terminal tool, with your own AWS CLI and credentials. After a create whose output has no ID, the plugin runs one read-only list call for that resource type. `/lights out` runs the delete commands shown in the ledger, through the same terminal tool, after you say yes. Nothing else is executed.
- **Stored data:** the ledger is a local JSON-lines file in your profile's plugin-data directory (`<HERMES_HOME>/plugin-data/lights-out/ledger.jsonl`). It holds resource IDs, sizes, prices, times and the command lines that created them. Nothing leaves your machine.
- **Model calls:** one optional `ctx.llm` call per end-of-session receipt, through your own provider, for a one-line summary. It is dropped if it contains a number that isn't on the receipt. Turn it off with `summary_sentence: false`.
- **Network:** none of its own. No telemetry, no update checks, no self-updater.
- **Secrets:** none read. Lights Out never touches AWS credentials; your CLI does.

## Limits

- AWS CLI only. Resources created by Python and boto3 (`execute_code`), Terraform, CloudFormation, the console or another CLI are not seen in v1. `/lights check` only checks what is in the ledger.
- The demo and the measured run use [moto](https://github.com/getmoto/moto), a local AWS emulator, so the resources are emulated. The rules match the real AWS CLI, and the prices come from AWS's public price lists, dated.
- Prices are us-east-1 on-demand list prices. Data transfer, LCU, IOPS above baseline and storage inside S3 aren't counted, and the receipt says so where it applies.
- In Hermes 0.23.1's CLI, `/exit` only detaches from the local gateway, and the gateway doesn't deliver end-of-session text from there. That's why the receipt is also posted after each turn that changed the ledger.

## Related projects

Similar tools exist for other agents. None is a Hermes plugin.

- [agent-leftovers](https://github.com/ApahSaroj/agent-leftovers) reads Claude Code, Codex, Gemini and Copilot session logs after the fact and reports leftover cloud resources and local processes, with a rough cost so far and teardown commands. It is read-only and covers many clouds. It doesn't ask before a create.
- [cloud-costaware](https://github.com/ozkanpoyrazoglu/cloud-costaware) is a pre-execution hook plus a skill for Claude Code, Codex and Antigravity that holds costly AWS calls for approval and shows a price from the AWS Price List API. It doesn't keep a record of what was created or print a teardown.
- [agent-cost-guard](https://github.com/ashishkaloge/agent-cost-guard) is a deterministic before-tool hook for Claude Code, Codex, Cursor and Copilot that allows, asks or blocks cost actions (AWS, Cloudflare, Terraform). It shows "Price: unknown" rather than querying prices, and keeps a decision log.
- [rentctl](https://github.com/Michael-Drake/rentctl) leases local dev servers so they stop when the agent session ends. It's about local processes, not cloud resources.

What Lights Out does differently: it is native to Hermes and asks through Hermes's own approval gate from `pre_tool_call`, so Hermes's once/session/always/deny rules, unattended fail-closed and YOLO behaviour all apply. It keeps a ledger across sessions and ends turns with a receipt built from that ledger, not from the model's account. Keeps until a date are approved only by a person. And it is measured against a prompt-only baseline on the same model and tasks (above).

## Development

```
bash scripts/test.sh              # 140 unit tests, no network (pytest, PyYAML outside Hermes)
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
python bench/integration.py       # real Hermes + moto + a model; needs moto[server] and pexpect
python bench/run_bench.py         # the measured comparison; raw logs in bench/runs/
```

The prices file is generated by `scripts/build_prices.py` from AWS's public price list downloads. The banner is `scripts/make_banner.py`.

The build chat log is in [docs/build-log.md](docs/build-log.md).

MIT licence.
