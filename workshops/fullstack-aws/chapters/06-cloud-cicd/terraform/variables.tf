variable "student_name" {
  description = "Your name or slug (e.g. alice). Used to name all resources."
  type        = string
}

variable "cohort" {
  description = "Workshop cohort identifier (e.g. fullstack-aws-july-2026). Tagged on every resource."
  type        = string
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "instance_type" {
  type    = string
  default = "t2.micro"
}
