# Workshop Tools

## nightly-cleanup.py

Deletes AWS resources tagged `Environment=workshop` unless `AutoDelete=false` is set.

### Run locally

```bash
pip install boto3

# Preview what would be deleted (safe — no changes)
DRY_RUN=true REGIONS=us-west-2,us-east-1 python nightly-cleanup.py

# Actually delete
DRY_RUN=false REGIONS=us-west-2 python nightly-cleanup.py
```

### Resources scanned

| Resource | Deletion order |
|---|---|
| Redshift Serverless workgroups | 1st |
| RDS clusters + instances | 2nd |
| EKS clusters + node groups | 3rd |
| DMS replication instances | 4th |
| OpenSearch domains | 5th |
| Step Functions state machines | 6th |
| Lambda functions | 7th |
| Glue jobs + crawlers | 8th |
| EC2 instances | 9th |
| NAT Gateways | 10th |
| Elastic IPs (unattached) | 11th |
| S3 buckets | global |
| WAF WebACLs (CloudFront) | global |

### Tagging standard

| Tag | Value | Required |
|---|---|---|
| `Environment` | `workshop` | Yes — triggers cleanup |
| `Workshop` | e.g. `aws-data-lake` | Yes |
| `CohortDate` | e.g. `2026-07-07` | Yes |
| `Student` | e.g. `alice-johnson` | Yes |
| `AutoDelete` | `true` (default) or `false` | No — omit to allow cleanup |

**To protect a resource:** add tag `AutoDelete = false`. The cleanup script will log it as protected and skip it.

### GitHub Actions setup

Add these secrets to the repository:

| Secret | Value |
|---|---|
| `CLEANUP_AWS_ACCESS_KEY_ID` | Access key for a cleanup IAM user |
| `CLEANUP_AWS_SECRET_ACCESS_KEY` | Secret key for the same user |

The cleanup IAM user needs read + delete permissions on EC2, RDS, Redshift, EKS, DMS, OpenSearch, Lambda, Glue, Step Functions, S3, and WAF. See `terraform/cleanup-iam.tf` for the policy.

The workflow runs nightly at 3 AM EST. It can also be triggered manually from the Actions tab with an optional dry-run toggle.
