# ── Standard tags for all workshop resources ──────────────────────────────────
#
# Every resource in a workshop must carry these tags.
# The nightly cleanup script deletes resources where:
#   Environment = workshop   AND   AutoDelete != false
#
# To protect a specific resource from nightly cleanup:
#   tags = merge(local.common_tags, { AutoDelete = "false" })

variable "workshop" {
  description = "Workshop name (e.g. aws-data-lake, equipment-inspection)"
}

variable "cohort_date" {
  description = "Date this cohort started — ISO format YYYY-MM-DD (e.g. 2026-07-07)"
}

variable "student_name" {
  description = "Student slug used to namespace all resources (e.g. alice-johnson)"
  default     = "instructor"
}

variable "auto_delete" {
  description = "Set to false to protect resources from nightly cleanup"
  default     = "true"
}

locals {
  common_tags = {
    Environment = "workshop"
    Workshop    = var.workshop
    CohortDate  = var.cohort_date
    Student     = var.student_name
    AutoDelete  = var.auto_delete
    ManagedBy   = "terraform"
  }
}
