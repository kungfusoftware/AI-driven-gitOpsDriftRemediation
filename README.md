# 🤖 AI-Driven GitOps Drift Remediation Pipeline

> **Autonomous Infrastructure Drift Detection & Remediation for Azure Staging using GitHub Actions + Azure Resource Graph + Azure OpenAI + Terraform**

---

## Overview

This solution automatically:

1. **Detects** drift instantly via Azure Resource Graph (ARG) KQL queries against the live Azure control plane
2. **Analyses** the drift with Azure OpenAI (risk level, root cause, impact)
3. **Generates** Terraform HCL remediation code via an AI agent
4. **Creates** a GitHub branch and commits the changes
5. **Verifies** the changes with `terraform validate`
6. **Opens** a Pull Request with a full AI-generated summary
7. **Human** reviews and approves the Pull Request

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  GitHub Actions — Daily Cron (06:00 UTC)                            │
│                                                                     │
│  JOB 1: detect-drift                                                │
│  ┌──────────────────────────────────────────────┐                  │
│  │  Azure Login (OIDC)                          │                  │
│  │  → arg_query.py   (ARG KQL — fast, live)     │  ← NEW (fast)   │
│  │  → terraform init (Azure Blob backend)       │                  │
│  │  → terraform plan (targeted confirmation)    │  ← targeted only │
│  │  → terraform show (export JSON)              │                  │
│  │  → summarize_plan.py (one-line summary)      │                  │
│  └──────────┬──────────────────┬────────────────┘                  │
│             │ ARG drift found  │ tf plan diff                       │
│             └────────┬─────────┘                                   │
│                      │ drift_detected=true                          │
│                      ▼                                              │
│  JOB 2: ai-remediation                                              │
│  ┌──────────────────────────────────────────────┐                  │
│  │  plan_parser.py  → merge ARG + tf plan data  │                  │
│  │  openai_client.py→ Azure OpenAI analysis     │                  │
│  │  openai_client.py→ HCL remediation code      │                  │
│  │  github_client.py→ create branch             │                  │
│  │  github_client.py→ commit .tf changes        │                  │
│  │  terraform validate (verify)                 │                  │
│  │  github_client.py→ open Pull Request         │                  │
│  │  👤 Human reviews and approves PR            │                  │
│  └──────────────────────────────────────────────┘                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Why Azure Resource Graph for Drift Detection?

ARG queries the **live Azure control plane directly** — no Terraform state lock needed, and results come back in milliseconds via Kusto (KQL).

| Capability | `terraform plan` | Azure Resource Graph |
|---|---|---|
| Source of truth | Terraform state vs. Azure API | Live Azure control plane |
| Speed | Slow (full API calls per resource) | Fast (KQL query, milliseconds) |
| Detects manual portal changes | Yes | Yes (instantly) |
| Detects tag drift | Yes | Yes (richer metadata) |
| Detects config within a resource | Deep | Surface-level only |
| Cross-subscription queries | No | Yes |
| No state lock required | Needs lock | Stateless |
| Best for | Knowing what Terraform will do | Knowing what Azure actually is |

**Ideal pattern:** ARG detects that drift *happened* → `terraform plan` confirms *what to fix* → AI remediates.

---

## ARG KQL Drift Queries

These queries run in JOB 1 (`arg_query.py`) before `terraform plan` to rapidly surface drift categories.

### 1. Tag compliance drift — resources missing required tags

```kql
Resources
| where resourceGroup =~ "rg-myapp-staging"
| where tags !contains "environment" or tags !contains "managed_by"
| project name, type, resourceGroup, tags, location
```

### 2. SKU/size drift — App Services with unexpected SKUs

```kql
Resources
| where resourceGroup =~ "rg-myapp-staging"
| where type =~ "microsoft.web/serverfarms"
| project name, sku=properties.sku.name, tier=properties.sku.tier
| where sku !in ("B1","B2","S1")
```

### 3. Unmanaged resources — not tracked by Terraform state

```kql
Resources
| where resourceGroup =~ "rg-myapp-staging"
| where tags["managed_by"] != "terraform"
| project name, type, createdTime=properties.createdTime
```

### 4. Network security group rule drift — overly permissive rules

```kql
Resources
| where type =~ "microsoft.network/networksecuritygroups"
| where resourceGroup =~ "rg-myapp-staging"
| mv-expand rule=properties.securityRules
| where rule.properties.access =~ "Allow" and rule.properties.sourceAddressPrefix =~ "*"
| project nsgName=name, ruleName=rule.name, direction=rule.properties.direction
```

---

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       └── drift-detection.yml       # Main GitHub Actions workflow
├── scripts/
│   ├── ai_remediate.py               # Main agent orchestrator
│   ├── arg_query.py                  # Azure Resource Graph KQL queries
│   ├── openai_client.py              # Azure OpenAI integration
│   ├── github_client.py              # GitHub API (branch/commit/PR)
│   ├── plan_parser.py                # Merges ARG + terraform plan JSON
│   ├── pr_template.py                # PR body builder
│   ├── summarize_plan.py             # Shell-callable plan summary
│   └── requirements.txt              # Python dependencies
└── terraform/
    └── staging/
        ├── backend.tf                # Azure Blob backend config
        ├── main.tf                   # Your Terraform resources
        ├── variables.tf              # Variable declarations
        └── staging.tfvars            # Staging environment values
```

---

## Prerequisites

### 1. Azure Service Principal with OIDC (Federated Credentials)

```bash
# Create a service principal
az ad sp create-for-rbac --name "sp-drift-remediation" \
  --role Contributor \
  --scopes /subscriptions/<SUBSCRIPTION_ID>

# Add federated credential for GitHub Actions OIDC
az ad app federated-credential create \
  --id <APP_ID> \
  --parameters '{
    "name": "github-actions-oidc",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:<ORG>/<REPO>:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

### 2. Azure OpenAI Deployment

Deploy a GPT-4 or GPT-4o model in your Azure OpenAI resource:

```bash
az cognitiveservices account deployment create \
  --name <AOAI_RESOURCE_NAME> \
  --resource-group <RG> \
  --deployment-name gpt-4o-drift \
  --model-name gpt-4o \
  --model-version "2024-05-13" \
  --model-format OpenAI \
  --sku-capacity 10 \
  --sku-name Standard
```

---

## GitHub Secrets Required

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|--------|-------------|
| `ARM_CLIENT_ID` | Azure Service Principal Client ID |
| `ARM_TENANT_ID` | Azure AD Tenant ID |
| `ARM_SUBSCRIPTION_ID` | Azure Subscription ID |
| `TF_BACKEND_RG` | Resource group of the Terraform state storage account |
| `TF_BACKEND_SA` | Storage account name for Terraform state |
| `TF_BACKEND_CONTAINER` | Blob container name (`tfstate`) |
| `AZURE_OPENAI_ENDPOINT` | `https://<resource>.openai.azure.com/` |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | Model deployment name (e.g. `gpt-4o-drift`) |

> **Note:** `GITHUB_TOKEN` is automatically provided by GitHub Actions — no manual setup needed.

---

## Risk Levels & Auto-Remediation Policy

| Risk Level | Auto-Remediation | PR Created | Example |
|-----------|-----------------|------------|---------|
| 🟢 LOW | ✅ Yes | ✅ Yes | Tag changes, metadata |
| 🟡 MEDIUM | ✅ Yes | ✅ Yes | SKU changes, scaling |
| 🟠 HIGH | ⚠️ Code only | ✅ Yes (for review) | Security groups, roles |
| 🔴 CRITICAL | ❌ No code | ✅ Yes (alert only) | Deletions, IAM, VNets |

---

## Manual Trigger (Dry Run)

```bash
# Via GitHub CLI — analyse only, no branch/PR created
gh workflow run drift-detection.yml -f dry_run=true

# Full run
gh workflow run drift-detection.yml
```

---

## Local Development & Testing

```bash
# Install dependencies
pip install -r scripts/requirements.txt

# Set environment variables
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
export AZURE_OPENAI_API_KEY="your-key"
export AZURE_OPENAI_DEPLOYMENT="gpt-4o-drift"
export GITHUB_TOKEN="ghp_..."
export GITHUB_REPOSITORY="your-org/your-repo"

# Step 1: Run ARG queries (fast, live — no state lock needed)
python scripts/arg_query.py \
  --subscription-id <SUBSCRIPTION_ID> \
  --resource-group rg-myapp-staging \
  --output /tmp/arg_drift.json

# Step 2: Run targeted terraform plan for confirmation (requires Azure auth)
cd terraform/staging
terraform init
terraform plan -out=tfplan.binary
terraform show -json tfplan.binary > /tmp/tfplan.json
terraform plan 2>&1 > /tmp/plan_output.txt

# Step 3: Run the agent in dry-run mode (merges ARG + tf plan results)
python scripts/ai_remediate.py \
  --plan-json /tmp/tfplan.json \
  --plan-text /tmp/plan_output.txt \
  --arg-drift /tmp/arg_drift.json \
  --working-dir terraform/staging \
  --dry-run
```

---

## Customisation

### Change the schedule
Edit `.github/workflows/drift-detection.yml`:
```yaml
schedule:
  - cron: "0 6 * * *"   # Change to your preferred time (UTC)
```

### Adjust AI risk thresholds
Edit `scripts/openai_client.py` — the `ANALYSIS_SYSTEM_PROMPT` defines
what qualifies as LOW/MEDIUM/HIGH/CRITICAL.

### Change the base branch for PRs
Edit `scripts/ai_remediate.py` — the `create_pull_request` call defaults to `main`.

---

## Solution Name

> **"AI-Driven GitOps Drift Remediation Pipeline"**
>
> Also known as: *Autonomous IaC Self-Healing Pipeline* or *Agentic Drift Detector*
