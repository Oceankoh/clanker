variable "project_id" {
  description = "GCP project that will host the base CTFVM network."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for network resources."
  type        = string
  default     = "ctfvm"
}

variable "region" {
  description = "GCP region for the subnet. Defaults to the region derived from the zone."
  type        = string
  default     = ""
}

variable "zone" {
  description = "Default compute zone used by dynamic worker VMs."
  type        = string
}

variable "subnet_cidr" {
  description = "CIDR for the worker subnet."
  type        = string
  default     = "10.42.0.0/20"
}

variable "control_plane_port" {
  description = "Inbound TCP port for the VM HTTP control plane."
  type        = number
  default     = 443
}

variable "allowed_cidrs" {
  description = "Source CIDRs allowed to reach the control plane."
  type        = list(string)
}

variable "enable_ssh" {
  description = "Whether to allow SSH from the same source CIDRs for break-glass access."
  type        = bool
  default     = true
}

variable "control_plane_tag" {
  description = "Network tag applied to dynamic worker VMs."
  type        = string
  default     = "ctfvm-control"
}
