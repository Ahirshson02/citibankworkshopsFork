#!/usr/bin/env python3
"""
Nightly workshop environment cleanup.

Deletes AWS resources tagged:
  Environment = workshop

UNLESS the resource also has:
  AutoDelete = false   ← set this to protect a resource from cleanup

Safe by default: resources without Environment=workshop are NEVER touched.

Usage:
  DRY_RUN=true python nightly-cleanup.py              # preview, no deletions
  REGIONS=us-west-2,us-east-1 python nightly-cleanup.py
  DRY_RUN=false REGIONS=us-west-2 python nightly-cleanup.py
"""

import boto3
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
REGIONS = [r.strip() for r in os.getenv("REGIONS", "us-west-2,us-east-1").split(",")]

CLEANUP_TAG  = ("Environment", "workshop")
PROTECT_TAG  = ("AutoDelete", "false")

summary = {"deleted": [], "protected": [], "errors": []}


# ── tag helpers ───────────────────────────────────────────────────────────────

def tag_map(tags):
    return {t["Key"]: t["Value"] for t in (tags or [])}

def should_delete(tags):
    m = tag_map(tags)
    if m.get(CLEANUP_TAG[0]) != CLEANUP_TAG[1]:
        return False
    if m.get(PROTECT_TAG[0], "").lower() == PROTECT_TAG[1]:
        return False
    return True

def act(resource_type, resource_id, region, fn):
    label = f"{resource_type} [{resource_id}] ({region})"
    if DRY_RUN:
        log.info(f"DRY RUN — would delete {label}")
        summary["deleted"].append(label)
        return True
    try:
        fn()
        log.info(f"Deleted {label}")
        summary["deleted"].append(label)
        return True
    except Exception as e:
        log.error(f"Failed to delete {label}: {e}")
        summary["errors"].append(f"{label}: {e}")
        return False

def wait_for(fn, timeout=300, interval=15):
    """Poll fn() until it raises or returns falsy. Max timeout seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = fn()
            if not result:
                return
        except Exception:
            return
        time.sleep(interval)
    log.warning("Wait timed out — continuing anyway")


# ── resource cleaners ─────────────────────────────────────────────────────────

def clean_redshift_serverless(region):
    log.info(f"[{region}] Scanning Redshift Serverless...")
    rs = boto3.client("redshift-serverless", region_name=region)

    # Workgroups first (namespace can't delete while workgroup exists)
    try:
        wgs = rs.list_workgroups().get("workgroups", [])
    except Exception as e:
        log.warning(f"[{region}] Redshift Serverless not available: {e}")
        return

    workgroup_namespaces = []
    for wg in wgs:
        name = wg["workgroupName"]
        tags_resp = rs.list_tags_for_resource(resourceArn=wg["workgroupArn"])
        tags = [{"Key": k, "Value": v} for k, v in tags_resp.get("tags", {}).items()]
        if should_delete(tags):
            workgroup_namespaces.append(wg.get("namespaceName"))
            act("Redshift Workgroup", name, region, lambda n=name: rs.delete_workgroup(workgroupName=n))
        else:
            summary["protected"].append(f"Redshift Workgroup [{name}]")

    # Wait for workgroups to finish deleting before touching namespaces
    if workgroup_namespaces and not DRY_RUN:
        log.info(f"[{region}] Waiting for Redshift workgroup deletion...")
        for wg_name in [w["workgroupName"] for w in wgs if w.get("namespaceName") in workgroup_namespaces]:
            wait_for(lambda n=wg_name: rs.get_workgroup(workgroupName=n)["workgroup"]["status"] == "DELETING")

    # Namespaces
    for ns in rs.list_namespaces().get("namespaces", []):
        name = ns["namespaceName"]
        if name not in [n for n in workgroup_namespaces if n]:
            continue
        tags_resp = rs.list_tags_for_resource(resourceArn=ns["namespaceArn"])
        tags = [{"Key": k, "Value": v} for k, v in tags_resp.get("tags", {}).items()]
        if should_delete(tags):
            act("Redshift Namespace", name, region, lambda n=name: rs.delete_namespace(namespaceName=n))


def clean_rds(region):
    log.info(f"[{region}] Scanning RDS...")
    rds = boto3.client("rds", region_name=region)

    # Clusters first (instances inside clusters can't be deleted independently)
    try:
        clusters = rds.describe_db_clusters().get("DBClusters", [])
    except Exception:
        clusters = []
    for c in clusters:
        if should_delete(c.get("TagList", [])):
            cid = c["DBClusterIdentifier"]
            act("RDS Cluster", cid, region,
                lambda n=cid: rds.delete_db_cluster(
                    DBClusterIdentifier=n,
                    SkipFinalSnapshot=True,
                    DeleteAutomatedBackups=True,
                ))
        else:
            summary["protected"].append(f"RDS Cluster [{c['DBClusterIdentifier']}]")

    # Standalone instances
    for db in rds.describe_db_instances().get("DBInstances", []):
        if db.get("DBClusterIdentifier"):
            continue  # member of a cluster, handled above
        if should_delete(db.get("TagList", [])):
            did = db["DBInstanceIdentifier"]
            act("RDS Instance", did, region,
                lambda n=did: rds.delete_db_instance(
                    DBInstanceIdentifier=n,
                    SkipFinalSnapshot=True,
                    DeleteAutomatedBackups=True,
                ))
        else:
            summary["protected"].append(f"RDS Instance [{db['DBInstanceIdentifier']}]")


def clean_ec2(region):
    log.info(f"[{region}] Scanning EC2 instances...")
    ec2 = boto3.client("ec2", region_name=region)
    reservations = ec2.describe_instances(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped", "stopping"]}]
    ).get("Reservations", [])

    ids_to_terminate = []
    for r in reservations:
        for inst in r["Instances"]:
            if should_delete(inst.get("Tags", [])):
                ids_to_terminate.append(inst["InstanceId"])
            else:
                name = tag_map(inst.get("Tags", [])).get("Name", inst["InstanceId"])
                summary["protected"].append(f"EC2 [{name}]")

    if ids_to_terminate:
        act("EC2 Instances", ", ".join(ids_to_terminate), region,
            lambda ids=ids_to_terminate: ec2.terminate_instances(InstanceIds=ids))


def clean_nat_gateways(region):
    log.info(f"[{region}] Scanning NAT Gateways...")
    ec2 = boto3.client("ec2", region_name=region)
    nats = ec2.describe_nat_gateways(
        Filters=[{"Name": "state", "Values": ["available", "pending"]}]
    ).get("NatGateways", [])

    for nat in nats:
        if should_delete(nat.get("Tags", [])):
            nid = nat["NatGatewayId"]
            act("NAT Gateway", nid, region, lambda n=nid: ec2.delete_nat_gateway(NatGatewayId=n))
        else:
            summary["protected"].append(f"NAT Gateway [{nat['NatGatewayId']}]")


def clean_elastic_ips(region):
    log.info(f"[{region}] Scanning Elastic IPs...")
    ec2 = boto3.client("ec2", region_name=region)
    addresses = ec2.describe_addresses().get("Addresses", [])
    for addr in addresses:
        if addr.get("AssociationId"):
            continue  # attached to something, skip
        if should_delete(addr.get("Tags", [])):
            alloc_id = addr["AllocationId"]
            act("Elastic IP", alloc_id, region,
                lambda a=alloc_id: ec2.release_address(AllocationId=a))


def clean_eks(region):
    log.info(f"[{region}] Scanning EKS clusters...")
    eks = boto3.client("eks", region_name=region)
    try:
        clusters = eks.list_clusters().get("clusters", [])
    except Exception:
        return

    for cluster_name in clusters:
        desc = eks.describe_cluster(name=cluster_name)["cluster"]
        if not should_delete(desc.get("tags", {}) and
                             [{"Key": k, "Value": v} for k, v in desc["tags"].items()]):
            summary["protected"].append(f"EKS [{cluster_name}]")
            continue

        # Delete node groups first
        try:
            ngs = eks.list_nodegroups(clusterName=cluster_name).get("nodegroups", [])
            for ng in ngs:
                act("EKS NodeGroup", f"{cluster_name}/{ng}", region,
                    lambda c=cluster_name, n=ng: eks.delete_nodegroup(clusterName=c, nodegroupName=n))
            if ngs and not DRY_RUN:
                log.info(f"[{region}] Waiting for EKS nodegroups to delete...")
                time.sleep(60)
        except Exception as e:
            log.warning(f"[{region}] Could not list nodegroups for {cluster_name}: {e}")

        act("EKS Cluster", cluster_name, region,
            lambda n=cluster_name: eks.delete_cluster(name=n))


def clean_dms(region):
    log.info(f"[{region}] Scanning DMS replication instances...")
    dms = boto3.client("dms", region_name=region)
    try:
        instances = dms.describe_replication_instances().get("ReplicationInstances", [])
    except Exception:
        return

    for inst in instances:
        arn = inst["ReplicationInstanceArn"]
        tags_resp = dms.list_tags_for_resource(ResourceArn=arn)
        tags = tags_resp.get("TagList", [])
        if should_delete(tags):
            iid = inst["ReplicationInstanceIdentifier"]
            act("DMS Replication Instance", iid, region,
                lambda a=arn: dms.delete_replication_instance(ReplicationInstanceArn=a))
        else:
            summary["protected"].append(f"DMS [{inst['ReplicationInstanceIdentifier']}]")


def clean_opensearch(region):
    log.info(f"[{region}] Scanning OpenSearch domains...")
    osearch = boto3.client("opensearch", region_name=region)
    try:
        domains = osearch.list_domain_names().get("DomainNames", [])
    except Exception:
        return

    for d in domains:
        name = d["DomainName"]
        arn = osearch.describe_domain(DomainName=name)["DomainStatus"]["ARN"]
        tags = osearch.list_tags(ARN=arn).get("TagList", [])
        if should_delete(tags):
            act("OpenSearch Domain", name, region,
                lambda n=name: osearch.delete_domain(DomainName=n))
        else:
            summary["protected"].append(f"OpenSearch [{name}]")


def clean_stepfunctions(region):
    log.info(f"[{region}] Scanning Step Functions state machines...")
    sf = boto3.client("stepfunctions", region_name=region)
    try:
        machines = sf.list_state_machines().get("stateMachines", [])
    except Exception:
        return

    for sm in machines:
        arn = sm["stateMachineArn"]
        tags = sf.list_tags_for_resource(resourceArn=arn).get("tags", [])
        tag_list = [{"Key": k, "Value": v} for k, v in tags.items()] if isinstance(tags, dict) else tags
        if should_delete(tag_list):
            act("Step Functions SM", sm["name"], region,
                lambda a=arn: sf.delete_state_machine(stateMachineArn=a))
        else:
            summary["protected"].append(f"Step Functions [{sm['name']}]")


def clean_lambda(region):
    log.info(f"[{region}] Scanning Lambda functions...")
    lmb = boto3.client("lambda", region_name=region)
    try:
        functions = lmb.list_functions().get("Functions", [])
    except Exception:
        return

    for fn in functions:
        arn = fn["FunctionArn"]
        tags = lmb.list_tags(Resource=arn).get("Tags", {})
        tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
        if should_delete(tag_list):
            name = fn["FunctionName"]
            act("Lambda", name, region, lambda n=name: lmb.delete_function(FunctionName=n))
        else:
            summary["protected"].append(f"Lambda [{fn['FunctionName']}]")


def clean_glue(region):
    log.info(f"[{region}] Scanning Glue jobs and crawlers...")
    glue = boto3.client("glue", region_name=region)

    try:
        jobs = glue.get_jobs().get("Jobs", [])
        for job in jobs:
            tags = glue.get_tags(ResourceArn=f"arn:aws:glue:{region}:*:job/{job['Name']}").get("Tags", {})
            tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
            if should_delete(tag_list):
                name = job["Name"]
                act("Glue Job", name, region, lambda n=name: glue.delete_job(JobName=n))
    except Exception as e:
        log.warning(f"[{region}] Glue jobs scan failed: {e}")

    try:
        crawlers = glue.get_crawlers().get("Crawlers", [])
        for crawler in crawlers:
            tags = glue.get_tags(ResourceArn=f"arn:aws:glue:{region}:*:crawler/{crawler['Name']}").get("Tags", {})
            tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
            if should_delete(tag_list):
                name = crawler["Name"]
                act("Glue Crawler", name, region, lambda n=name: glue.delete_crawler(Name=n))
    except Exception as e:
        log.warning(f"[{region}] Glue crawlers scan failed: {e}")


def clean_s3(region):
    """S3 is global but buckets are region-bound. Only delete buckets tagged for cleanup."""
    if region != REGIONS[0]:
        return  # run once only
    log.info(f"Scanning S3 buckets...")
    s3 = boto3.client("s3")
    s3r = boto3.resource("s3")

    buckets = s3.list_buckets().get("Buckets", [])
    for bucket in buckets:
        name = bucket["Name"]
        try:
            tags = s3.get_bucket_tagging(Bucket=name).get("TagSet", [])
        except s3.exceptions.ClientError:
            tags = []

        if not should_delete(tags):
            continue

        def delete_bucket(n=name):
            b = s3r.Bucket(n)
            # Empty versioned objects first
            b.object_versions.delete()
            b.objects.delete()
            b.delete()

        act("S3 Bucket", name, "global", delete_bucket)


def clean_waf_cloudfront():
    """CloudFront WAF WebACLs are global — must query us-east-1."""
    log.info("Scanning WAF WebACLs (CloudFront scope)...")
    waf = boto3.client("wafv2", region_name="us-east-1")
    try:
        acls = waf.list_web_acls(Scope="CLOUDFRONT").get("WebACLs", [])
    except Exception:
        return

    for acl in acls:
        arn = acl["ARN"]
        tags = waf.list_tags_for_resource(ResourceARN=arn).get("TagInfoForResource", {}).get("TagList", [])
        if should_delete(tags):
            # Verify no resources are attached before deleting
            attached = waf.list_resources_for_web_acl(WebACLArn=arn).get("ResourceArns", [])
            if attached:
                log.warning(f"WAF WebACL {acl['Name']} still has attached resources — skipping")
                continue
            lock_token = waf.get_web_acl(
                Name=acl["Name"], Scope="CLOUDFRONT", Id=acl["Id"]
            )["LockToken"]
            act("WAF WebACL", acl["Name"], "us-east-1",
                lambda n=acl["Name"], i=acl["Id"], lt=lock_token: waf.delete_web_acl(
                    Name=n, Scope="CLOUDFRONT", Id=i, LockToken=lt
                ))


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    if DRY_RUN:
        log.info("=" * 60)
        log.info("DRY RUN MODE — no resources will be deleted")
        log.info("Set DRY_RUN=false to perform actual cleanup")
        log.info("=" * 60)
    else:
        log.info("=" * 60)
        log.info("LIVE CLEANUP — deleting workshop resources")
        log.info(f"Regions: {REGIONS}")
        log.info("=" * 60)

    # S3 and WAF are global — run once outside the region loop
    clean_s3(REGIONS[0])
    clean_waf_cloudfront()

    for region in REGIONS:
        log.info(f"\n{'─' * 40}")
        log.info(f"Region: {region}")
        log.info(f"{'─' * 40}")

        # Order matters: delete workloads before networking
        clean_redshift_serverless(region)
        clean_rds(region)
        clean_eks(region)
        clean_dms(region)
        clean_opensearch(region)
        clean_stepfunctions(region)
        clean_lambda(region)
        clean_glue(region)
        clean_ec2(region)
        clean_nat_gateways(region)
        clean_elastic_ips(region)

    # Print summary
    log.info(f"\n{'=' * 60}")
    log.info(f"SUMMARY")
    log.info(f"{'=' * 60}")
    log.info(f"Deleted  : {len(summary['deleted'])}")
    log.info(f"Protected: {len(summary['protected'])}")
    log.info(f"Errors   : {len(summary['errors'])}")

    if summary["deleted"]:
        log.info("\nDeleted:")
        for r in summary["deleted"]:
            log.info(f"  ✓ {r}")

    if summary["protected"]:
        log.info("\nProtected (AutoDelete=false):")
        for r in summary["protected"]:
            log.info(f"  🔒 {r}")

    if summary["errors"]:
        log.info("\nErrors:")
        for r in summary["errors"]:
            log.error(f"  ✗ {r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
