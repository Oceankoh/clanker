variable "do_token" {
  description = "DigitalOcean API token."
  type        = string
  sensitive   = true
}

variable "name_prefix" {
  description = "Prefix for network resources."
  type        = string
  default     = "ctfvm"
}

variable "region" {
  description = "DigitalOcean region for the VPC."
  type        = string
}

variable "ip_range" {
  description = "CIDR for the VPC."
  type        = string
  default     = "10.43.0.0/16"
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
  description = "Tag applied to worker droplets for discovery and firewall binding."
  type        = string
  default     = "ctfvm-control"
}

variable "fleet_tag" {
  description = "Discovery tag for worker droplets."
  type        = string
  default     = "ctfvm"
}
