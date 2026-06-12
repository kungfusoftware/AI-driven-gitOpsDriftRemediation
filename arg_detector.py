#!/usr/bin/env python3
"""
Azure Resource Graph Drift Detector
-------------------------------------
Runs a suite of KQL queries against the Azure Resource Graph API to detect
live infrastructure drift in the Staging environment — fast, stateless,
no Terraform state lock required.

Returns a structured DriftReport dict consumed by the AI agent.
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions

log = logging.getLogger("drift-agent.arg")

# ── Configuration ─────────────────────────────────────────────────────────────
REQUIRED_TAGS       = ["environment", "managed_by", "team"]
EXPECTED_MANAGED_BY = "terraform"
STAGING_RG          = os.getenv("STAGING_RESOURCE_GROUP", "rg-myapp-staging")
SUBSCRIPTION_ID     = os.getenv("ARM_SUBSCRIPTION_ID", "")

# Allowed SKU values per resource type in Staging
ALLOWED_SKUS: dict[str, list[str]] = {
    "microsoft.web/serverfarms":           ["B1", "B2", "S1", "S2"],
    "microsoft.compute/virtualmachines":   ["Standard_B2s", "Standard_B4ms", "Standard_D2s_v3"],
    "microsoft.sql/servers/databases":     ["Basic", "S0", "S1", "S2"],
    "microsoft.cache/redis":               ["C0", "C1"],
}

# NSG rules that should never exist in staging
FORBIDDEN_NSG_SOURCES = ["*", "Internet", "0.0.0.0/0"]


# ── Data Classes ──────────────────────────────────────────────────────────────
@dataclass
class DriftFinding:
    category:    str          # tag_drift | sku_drift | unmanaged | nsg | rbac | lock | config
    severity:    str          # LOW | MEDIUM | HIGH | CRITICAL
    resource_id: str
    resource_name: str
    resource_type: str
    location:    str
    description: str
    detail:      dict = field(default_factory=dict)


@dataclass
class DriftReport:
    queried_at:        str
    resource_group:    str
    subscription_id:   str
    total_findings:    int
    highest_severity:  str
    findings:          list[DriftFinding]
    query_errors:      list[str]
    summary_by_category: dict[str, int]

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def has_drift(self) -> bool:
        return self.total_findings > 0


# ── Main Detector Class ───────────────────────────────────────────────────────
class ARGDriftDetector:
    """
    Runs all ARG KQL queries against the live Azure control plane and
    aggregates results into a DriftReport.
    """

    def __init__(self):
        credential = DefaultAzureCredential()
        self._client = ResourceGraphClient(credential)
        self._subscription_ids = [SUBSCRIPTION_ID] if SUBSCRIPTION_ID else []
        self._rg = STAGING_RG
        log.info(
            "ARG detector initialised — subscription: %s  resource_group: %s",
            SUBSCRIPTION_ID, STAGING_RG,
        )

    # ── Public ────────────────────────────────────────────────────────────────

    def run_all_queries(self) -> DriftReport:
        """Execute all KQL drift queries and return a consolidated DriftReport."""
        findings:     list[DriftFinding] = []
        query_errors: list[str]          = []

        # Each method returns (list[DriftFinding], error_str | None)
        query_methods = [
            self._query_tag_drift,
            self._query_sku_drift,
            self._query_unmanaged_resources,
            self._query_nsg_rule_drift,
            self._query_rbac_drift,
            self._query_missing_resource_locks,
            self._query_public_ip_drift,
            self._query_storage_account_drift,
        ]

        for method in query_methods:
            try:
                results, err = method()
                findings.extend(results)
                if err:
                    query_errors.append(err)
            except Exception as exc:
                msg = f"{method.__name__}: {exc}"
                log.error("Query error — %s", msg)
                query_errors.append(msg)

        # Summarise
        severity_order = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        highest = "NONE"
        for sev in severity_order:
            if any(f.severity == sev for f in findings):
                highest = sev
                break

        by_category: dict[str, int] = {}
        for f in findings:
            by_category[f.category] = by_category.get(f.category, 0) + 1

        report = DriftReport(
            queried_at=datetime.now(timezone.utc).isoformat(),
            resource_group=self._rg,
            subscription_id=SUBSCRIPTION_ID,
            total_findings=len(findings),
            highest_severity=highest,
            findings=findings,
            query_errors=query_errors,
            summary_by_category=by_category,
        )

        log.info(
            "ARG scan complete — findings: %d  highest severity: %s",
            len(findings), highest,
        )
        return report

    # ── KQL Query Methods ─────────────────────────────────────────────────────

    def _query_tag_drift(self) -> tuple[list[DriftFinding], str | None]:
        """Q1: Resources missing required tags or with wrong managed_by value."""
        tag_conditions = " or ".join(
            [f"tags['{t}'] == ''" for t in REQUIRED_TAGS]
            + [f"isnull(tags['{t}'])" for t in REQUIRED_TAGS]
        )
        kql = f"""
Resources
| where resourceGroup =~ '{self._rg}'
| where type !startswith 'microsoft.resources/'
| where {tag_conditions}
      or tags['managed_by'] !~ '{EXPECTED_MANAGED_BY}'
| project
    id,
    name,
    type,
    location,
    tags,
    missingTags = bag_pack(
        'has_environment',  isnotnull(tags['environment']),
        'has_managed_by',   isnotnull(tags['managed_by']),
        'has_team',         isnotnull(tags['team']),
        'managed_by_value', tags['managed_by']
    )
| order by name asc
"""
        rows, err = self._run_query(kql)
        findings = []
        for r in rows:
            missing = [t for t in REQUIRED_TAGS if not r.get("missingTags", {}).get(f"has_{t}", False)]
            wrong_mgr = (
                r.get("missingTags", {}).get("managed_by_value", "") != EXPECTED_MANAGED_BY
            )
            desc_parts = []
            if missing:
                desc_parts.append(f"Missing tags: {missing}")
            if wrong_mgr:
                val = r.get("missingTags", {}).get("managed_by_value", "<empty>")
                desc_parts.append(f"managed_by='{val}' (expected '{EXPECTED_MANAGED_BY}')")

            findings.append(DriftFinding(
                category="tag_drift",
                severity="MEDIUM",
                resource_id=r.get("id", ""),
                resource_name=r.get("name", ""),
                resource_type=r.get("type", ""),
                location=r.get("location", ""),
                description="; ".join(desc_parts),
                detail={"tags": r.get("tags", {}), "issues": desc_parts},
            ))
        return findings, err

    def _query_sku_drift(self) -> tuple[list[DriftFinding], str | None]:
        """Q2: Resources with SKUs outside the approved staging list."""
        type_filter = " or ".join(
            f"type =~ '{t}'" for t in ALLOWED_SKUS
        )
        kql = f"""
Resources
| where resourceGroup =~ '{self._rg}'
| where {type_filter}
| extend skuName = coalesce(
    tostring(sku.name),
    tostring(properties.sku.name),
    tostring(properties.hardwareProfile.vmSize),
    'unknown'
  )
| project id, name, type, location, skuName, tags
| order by type asc, name asc
"""
        rows, err = self._run_query(kql)
        findings = []
        for r in rows:
            rtype   = r.get("type", "").lower()
            sku     = r.get("skuName", "unknown")
            allowed = ALLOWED_SKUS.get(rtype, [])
            if allowed and sku not in allowed:
                findings.append(DriftFinding(
                    category="sku_drift",
                    severity="MEDIUM",
                    resource_id=r.get("id", ""),
                    resource_name=r.get("name", ""),
                    resource_type=r.get("type", ""),
                    location=r.get("location", ""),
                    description=f"SKU '{sku}' is not in the approved list {allowed}",
                    detail={"current_sku": sku, "allowed_skus": allowed},
                ))
        return findings, err

    def _query_unmanaged_resources(self) -> tuple[list[DriftFinding], str | None]:
        """Q3: Resources in the staging RG with no Terraform-managed tag."""
        kql = f"""
Resources
| where resourceGroup =~ '{self._rg}'
| where type !startswith 'microsoft.resources/'
| where isnull(tags['managed_by']) or tags['managed_by'] !~ '{EXPECTED_MANAGED_BY}'
| extend ageHours = datetime_diff('hour', now(), todatetime(
    coalesce(tostring(properties.creationTime), tostring(properties.timeCreated), '')
  ))
| project id, name, type, location, tags,
          createdApprox=tostring(properties.creationTime),
          ageHours
| order by ageHours asc
"""
        rows, err = self._run_query(kql)
        findings = []
        for r in rows:
            age = r.get("ageHours")
            sev = "HIGH" if (age is not None and age < 24) else "MEDIUM"
            findings.append(DriftFinding(
                category="unmanaged",
                severity=sev,
                resource_id=r.get("id", ""),
                resource_name=r.get("name", ""),
                resource_type=r.get("type", ""),
                location=r.get("location", ""),
                description=(
                    f"Resource not managed by Terraform "
                    f"(age: {age}h, created: {r.get('createdApprox','unknown')})"
                ),
                detail={"tags": r.get("tags", {}), "age_hours": age},
            ))
        return findings, err

    def _query_nsg_rule_drift(self) -> tuple[list[DriftFinding], str | None]:
        """Q4: NSG rules with overly permissive sources (Internet / *)."""
        sources_filter = " or ".join(
            f"rule.properties.sourceAddressPrefix =~ '{s}'"
            for s in FORBIDDEN_NSG_SOURCES
        )
        kql = f"""
Resources
| where resourceGroup =~ '{self._rg}'
| where type =~ 'microsoft.network/networksecuritygroups'
| mv-expand rule = properties.securityRules
| where rule.properties.access =~ 'Allow'
| where {sources_filter}
| project
    id,
    nsgName    = name,
    type,
    location,
    ruleName   = tostring(rule.name),
    direction  = tostring(rule.properties.direction),
    priority   = toint(rule.properties.priority),
    protocol   = tostring(rule.properties.protocol),
    destPort   = tostring(rule.properties.destinationPortRange),
    source     = tostring(rule.properties.sourceAddressPrefix)
| order by priority asc
"""
        rows, err = self._run_query(kql)
        findings = []
        for r in rows:
            dest_port = r.get("destPort", "*")
            sev = "CRITICAL" if dest_port in ["22", "3389", "443", "*"] else "HIGH"
            findings.append(DriftFinding(
                category="nsg",
                severity=sev,
                resource_id=r.get("id", ""),
                resource_name=r.get("nsgName", ""),
                resource_type=r.get("type", ""),
                location=r.get("location", ""),
                description=(
                    f"NSG rule '{r.get('ruleName')}' allows {r.get('direction')} "
                    f"from '{r.get('source')}' to port {dest_port}"
                ),
                detail={
                    "rule":      r.get("ruleName"),
                    "direction": r.get("direction"),
                    "priority":  r.get("priority"),
                    "source":    r.get("source"),
                    "dest_port": dest_port,
                    "protocol":  r.get("protocol"),
                },
            ))
        return findings, err

    def _query_rbac_drift(self) -> tuple[list[DriftFinding], str | None]:
        """Q5: Non-standard role assignments on the staging resource group."""
        # Approved roles for staging (customize to your org)
        approved_roles = [
            "Contributor", "Reader",
            "Storage Blob Data Contributor",
            "Key Vault Secrets User",
            "Monitoring Reader",
        ]
        approved_filter = " and ".join(
            f"properties.roleDefinitionName !~ '{r}'"
            for r in approved_roles
        )
        kql = f"""
AuthorizationResources
| where type =~ 'microsoft.authorization/roleassignments'
| where resourceGroup =~ '{self._rg}'
| where {approved_filter}
| project
    id,
    name,
    type,
    location = 'global',
    roleName  = tostring(properties.roleDefinitionName),
    principal = tostring(properties.principalId),
    scope     = tostring(properties.scope),
    createdOn = tostring(properties.createdOn)
"""
        rows, err = self._run_query(kql)
        findings = []
        for r in rows:
            findings.append(DriftFinding(
                category="rbac",
                severity="HIGH",
                resource_id=r.get("id", ""),
                resource_name=r.get("name", ""),
                resource_type=r.get("type", ""),
                location=r.get("location", "global"),
                description=(
                    f"Non-standard role '{r.get('roleName')}' assigned to "
                    f"principal {r.get('principal')} (created: {r.get('createdOn')})"
                ),
                detail={
                    "role":       r.get("roleName"),
                    "principal":  r.get("principal"),
                    "scope":      r.get("scope"),
                    "created_on": r.get("createdOn"),
                },
            ))
        return findings, err

    def _query_missing_resource_locks(self) -> tuple[list[DriftFinding], str | None]:
        """Q6: Critical resource types that are missing delete locks."""
        critical_types = [
            "microsoft.sql/servers",
            "microsoft.storage/storageaccounts",
            "microsoft.keyvault/vaults",
            "microsoft.network/virtualnetworks",
        ]
        type_filter = " or ".join(f"type =~ '{t}'" for t in critical_types)

        kql = f"""
Resources
| where resourceGroup =~ '{self._rg}'
| where {type_filter}
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.authorization/locks'
    | where resourceGroup =~ '{self._rg}'
    | extend parentId = tostring(split(id, '/providers/')[0])
    | project parentId, lockLevel=tostring(properties.level)
  ) on $left.id == $right.parentId
| where isnull(lockLevel) or lockLevel =~ 'ReadOnly'
| project id, name, type, location, tags, lockLevel=coalesce(lockLevel,'none')
"""
        rows, err = self._run_query(kql)
        findings = []
        for r in rows:
            findings.append(DriftFinding(
                category="lock",
                severity="MEDIUM",
                resource_id=r.get("id", ""),
                resource_name=r.get("name", ""),
                resource_type=r.get("type", ""),
                location=r.get("location", ""),
                description=(
                    f"Critical resource missing a CanNotDelete lock "
                    f"(current lock: '{r.get('lockLevel', 'none')}')"
                ),
                detail={"lock_level": r.get("lockLevel", "none")},
            ))
        return findings, err

    def _query_public_ip_drift(self) -> tuple[list[DriftFinding], str | None]:
        """Q7: Unexpected public IPs — staging should have minimal public exposure."""
        kql = f"""
Resources
| where resourceGroup =~ '{self._rg}'
| where type =~ 'microsoft.network/publicipaddresses'
| extend allocationMethod = tostring(properties.publicIPAllocationMethod)
| extend ipAddress        = tostring(properties.ipAddress)
| extend associatedTo     = tostring(properties.ipConfiguration.id)
| project id, name, type, location, tags,
          allocationMethod, ipAddress, associatedTo
| order by name asc
"""
        rows, err = self._run_query(kql)
        findings = []
        for r in rows:
            # Any public IP not associated with an expected load balancer is suspicious
            associated = r.get("associatedTo", "")
            is_lb = "loadbalancers" in associated.lower()
            sev = "LOW" if is_lb else "HIGH"
            findings.append(DriftFinding(
                category="public_ip",
                severity=sev,
                resource_id=r.get("id", ""),
                resource_name=r.get("name", ""),
                resource_type=r.get("type", ""),
                location=r.get("location", ""),
                description=(
                    f"Public IP '{r.get('ipAddress', 'unassigned')}' "
                    f"({'attached to LB' if is_lb else 'NOT attached to a load balancer'})"
                ),
                detail={
                    "ip_address":         r.get("ipAddress"),
                    "allocation_method":  r.get("allocationMethod"),
                    "associated_to":      associated,
                },
            ))
        return findings, err

    def _query_storage_account_drift(self) -> tuple[list[DriftFinding], str | None]:
        """Q8: Storage accounts with insecure configuration (public blobs, no HTTPS)."""
        kql = f"""
Resources
| where resourceGroup =~ '{self._rg}'
| where type =~ 'microsoft.storage/storageaccounts'
| extend httpsOnly         = tobool(properties.supportsHttpsTrafficOnly)
| extend publicBlobAccess  = tobool(properties.allowBlobPublicAccess)
| extend minTlsVersion     = tostring(properties.minimumTlsVersion)
| extend allowSharedKey    = tobool(properties.allowSharedKeyAccess)
| extend networkDefaultAction = tostring(properties.networkAcls.defaultAction)
| where httpsOnly == false
     or publicBlobAccess == true
     or minTlsVersion !~ 'TLS1_2'
     or networkDefaultAction =~ 'Allow'
| project id, name, type, location, tags,
          httpsOnly, publicBlobAccess, minTlsVersion,
          allowSharedKey, networkDefaultAction
"""
        rows, err = self._run_query(kql)
        findings = []
        for r in rows:
            issues = []
            if not r.get("httpsOnly", True):
                issues.append("HTTPS not enforced")
            if r.get("publicBlobAccess", False):
                issues.append("Public blob access enabled")
            if r.get("minTlsVersion") != "TLS1_2":
                issues.append(f"TLS version is {r.get('minTlsVersion','unknown')} (expected TLS1_2)")
            if r.get("networkDefaultAction", "").lower() == "allow":
                issues.append("Network ACL default action is Allow (open to internet)")

            sev = "CRITICAL" if r.get("publicBlobAccess") else "HIGH"
            findings.append(DriftFinding(
                category="storage_config",
                severity=sev,
                resource_id=r.get("id", ""),
                resource_name=r.get("name", ""),
                resource_type=r.get("type", ""),
                location=r.get("location", ""),
                description=f"Insecure storage configuration: {'; '.join(issues)}",
                detail={
                    "https_only":           r.get("httpsOnly"),
                    "public_blob_access":   r.get("publicBlobAccess"),
                    "min_tls_version":      r.get("minTlsVersion"),
                    "network_default_action": r.get("networkDefaultAction"),
                    "issues":               issues,
                },
            ))
        return findings, err

    # ── ARG Execution Engine ──────────────────────────────────────────────────

    def _run_query(self, kql: str) -> tuple[list[dict], str | None]:
        """Execute a KQL query against Azure Resource Graph. Returns (rows, error)."""
        query = kql.strip()
        request = QueryRequest(
            subscriptions=self._subscription_ids,
            query=query,
            options=QueryRequestOptions(result_format="objectArray", top=500),
        )
        try:
            response = self._client.resources(request)
            rows = response.data or []
            log.debug("ARG query returned %d rows", len(rows))
            return rows, None
        except Exception as exc:
            log.error("ARG query failed: %s\nKQL:\n%s", exc, query[:300])
            return [], str(exc)


# ── CLI Entry Point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Run ARG drift detection")
    parser.add_argument("--output", default="/tmp/arg_drift_report.json", help="Output JSON path")
    args = parser.parse_args()

    detector = ARGDriftDetector()
    report   = detector.run_all_queries()

    out_path = Path(args.output)
    out_path.write_text(json.dumps(report.to_dict(), indent=2))
    print(f"ARG drift report written to {out_path}")
    print(f"Total findings: {report.total_findings}  Highest severity: {report.highest_severity}")

    if report.has_drift():
        raise SystemExit(2)   # Exit code 2 = drift detected (matches terraform plan convention)
